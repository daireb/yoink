"""Reprise — self-hosted music download portal.

FastAPI web app wrapping spotDL (Spotify -> YouTube Music match -> audio) and
yt-dlp (YouTube and most other media sites). Jobs run in a single background
worker; state persists in SQLite so the queue survives restarts.
"""

import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------- configuration

DOWNLOAD_DIR = Path(os.environ.get("REPRISE_DOWNLOAD_DIR", "/downloads"))
DATA_DIR = Path(os.environ.get("REPRISE_DATA_DIR", "/data"))
PASSWORD = os.environ.get("REPRISE_PASSWORD", "").strip()
def _load_secret() -> str:
    """Cookie-signing secret: env override, else generated once and kept in /data
    so sessions survive container restarts."""
    if env := os.environ.get("REPRISE_SECRET", ""):
        return env
    path = DATA_DIR / "secret"
    try:
        if path.is_file():
            return path.read_text().strip()
        path.parent.mkdir(parents=True, exist_ok=True)
        tok = secrets.token_hex(32)
        path.write_text(tok)
        path.chmod(0o600)
        return tok
    except OSError:
        return secrets.token_hex(32)  # fall back to per-boot secret


SECRET = _load_secret()
SPOTDL_THREADS = os.environ.get("REPRISE_THREADS", "4")
MAX_CSV_ROWS = 1000
MAX_CSV_BYTES = 5 * 1024 * 1024
# kill a job if its process produces no output for this long (seconds)
STALL_TIMEOUT = int(os.environ.get("REPRISE_STALL_TIMEOUT", "1800"))
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}

LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "reprise.db"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")

# ------------------------------------------------------------------------- db

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                created REAL NOT NULL,
                kind TEXT NOT NULL,
                input TEXT NOT NULL,
                title TEXT NOT NULL,
                format TEXT NOT NULL,
                status TEXT NOT NULL,
                total INTEGER,
                done INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                dir TEXT NOT NULL
            )"""
        )
        # A previous process died mid-download; don't leave ghosts "running".
        conn.execute(
            "UPDATE jobs SET status='interrupted', error='server restarted mid-job'"
            " WHERE status='running'"
        )


# ------------------------------------------------------------------ job model

class JobHandle:
    """In-memory side of a running job (process + live log tail)."""

    def __init__(self, job_id: str):
        self.id = job_id
        self.proc: Optional[subprocess.Popen] = None
        self.cancelled = False


RUNNING: dict[str, JobHandle] = {}
RUNNING_LOCK = threading.Lock()
ZIP_LOCK = threading.Lock()
WAKE = threading.Event()


def job_row(job_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def update_job(job_id: str, **fields) -> None:
    keys = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), job_id))


def slugify(text: str, maxlen: int = 40) -> str:
    text = re.sub(r"https?://", "", text.strip().lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen] or "job"


def create_job(kind: str, input_text: str, title: str, fmt: str, total: Optional[int]) -> str:
    job_id = uuid.uuid4().hex[:12]
    job_dir = DOWNLOAD_DIR / f"{slugify(title)}-{job_id[:6]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created, kind, input, title, format, status, total, done, dir)"
            " VALUES (?,?,?,?,?,?,?,?,0,?)",
            (job_id, time.time(), kind, input_text, title, fmt, "queued", total, str(job_dir)),
        )
    WAKE.set()
    return job_id


# ------------------------------------------------------------- command builder

def build_command(kind: str, input_text: str, fmt: str, job_dir: Path) -> list[str]:
    if kind in ("spotify", "csv", "search"):
        cmd = ["spotdl", "download"]
        if kind == "csv":
            cmd += json.loads(input_text)  # list of track URLs
        else:
            cmd.append(input_text)
        cmd += [
            "--format", fmt,
            "--threads", SPOTDL_THREADS,
            "--output", str(job_dir / "{artists} - {title}.{output-ext}"),
            # plain parseable log lines instead of rich progress bars
            "--simple-tui", "--log-level", "INFO", "--headless",
            "--print-errors",
        ]
        # spotdl's config default is 128k(!) — force 320 for mp3, and for
        # lossless/source formats skip the transcode entirely.
        if fmt == "mp3":
            cmd += ["--bitrate", "320k"]
        else:
            cmd += ["--bitrate", "disable"]
        if "/playlist/" in input_text or "/album/" in input_text:
            cmd += ["--m3u"]
        return cmd

    # yt-dlp: youtube and every other site it supports
    cmd = [
        "yt-dlp",
        "--extract-audio",
        "--audio-format", fmt,
        "--embed-metadata",
        "--embed-thumbnail",
        "--convert-thumbnails", "jpg",  # webp cover art breaks many players
        "--parse-metadata", "%(artist,uploader|)s:%(meta_artist)s",
        "--newline",
        "--no-colors",
        "-o", str(job_dir / "%(title)s.%(ext)s"),
        input_text,
    ]
    if fmt == "mp3":
        cmd += ["--audio-quality", "320K"]
    return cmd


# ------------------------------------------------------------ progress parsing

SPOTDL_FOUND_RE = re.compile(r"Found (\d+) songs")
SPOTDL_COMPLETE_RE = re.compile(r"^(\d+)/(\d+) complete")
SPOTDL_LIST_RE = re.compile(r"Found \d+ songs in (.+?) \((Playlist|Album|Artist)\)")
YTDLP_ITEM_RE = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
YTDLP_DONE_RE = re.compile(r"\[ExtractAudio\] Destination:")
YTDLP_FINISHED_RE = re.compile(r"\[download\] 100% of .+ in |has already been downloaded")


def parse_progress(kind: str, line: str, state: dict) -> None:
    if kind in ("spotify", "csv", "search"):
        if m := SPOTDL_FOUND_RE.search(line):
            state["total"] = int(m.group(1))
        if m := SPOTDL_COMPLETE_RE.match(line):
            state["done"] = int(m.group(1))
            state["total"] = max(int(m.group(2)), state.get("total") or 0)
        if m := SPOTDL_LIST_RE.search(line):
            state["title"] = m.group(1)
    else:
        if m := YTDLP_ITEM_RE.search(line):
            state["total"] = int(m.group(2))
        if YTDLP_DONE_RE.search(line):
            state["extracted"] = state.get("extracted", 0) + 1
        if YTDLP_FINISHED_RE.search(line):
            state["finished"] = state.get("finished", 0) + 1
        state["done"] = max(state.get("extracted", 0), state.get("finished", 0))


# ------------------------------------------------------------------ the worker

def run_job(row: sqlite3.Row) -> None:
    job_id = row["id"]
    job_dir = Path(row["dir"])
    handle = JobHandle(job_id)
    with RUNNING_LOCK:
        RUNNING[job_id] = handle

    log_path = LOG_DIR / f"{job_id}.log"
    state = {"done": 0, "total": row["total"]}
    exit_code: Optional[int] = None

    try:
        cmd = build_command(row["kind"], row["input"], row["format"], job_dir)
        with open(log_path, "w", encoding="utf-8") as log:
            log.write("$ " + " ".join(cmd[:8]) + (" ..." if len(cmd) > 8 else "") + "\n")
            log.flush()
            handle.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(job_dir),
                start_new_session=True,
            )
            assert handle.proc.stdout is not None
            last_output = [time.time()]

            def _watchdog(proc=handle.proc):
                while proc.poll() is None:
                    if time.time() - last_output[0] > STALL_TIMEOUT:
                        state["stalled"] = True
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                            time.sleep(10)
                            if proc.poll() is None:
                                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        return
                    time.sleep(10)

            threading.Thread(target=_watchdog, daemon=True).start()
            last_flush = 0.0
            for raw in handle.proc.stdout:
                last_output[0] = time.time()
                line = ANSI_RE.sub("", raw).rstrip()
                if not line:
                    continue
                log.write(line + "\n")
                parse_progress(row["kind"], line, state)
                now = time.time()
                if now - last_flush > 1.0:
                    log.flush()
                    fields = {"done": state.get("done", 0), "total": state.get("total")}
                    if state.get("title"):
                        fields["title"] = state.pop("title")
                    update_job(job_id, **fields)
                    last_flush = now
            exit_code = handle.proc.wait()
    except Exception as exc:  # noqa: BLE001 - job must record any failure
        update_job(job_id, status="error", error=f"{type(exc).__name__}: {exc}")
        return
    finally:
        with RUNNING_LOCK:
            RUNNING.pop(job_id, None)

    files = list_audio_files(job_dir)
    done = max(state.get("done", 0), len(files))
    if state.get("total"):
        done = min(done, state["total"])
    if state.get("title"):
        update_job(job_id, title=state["title"])
    elif len(files) == 1 and row["title"].startswith(("http://", "https://")):
        update_job(job_id, title=Path(files[0]["name"]).stem)
    total = state.get("total")
    if state.get("stalled"):
        update_job(job_id, status="error", done=done, total=total,
                   error=f"stalled: no output for {STALL_TIMEOUT}s, killed")
    elif handle.cancelled:
        update_job(job_id, status="cancelled", done=done, total=total)
    elif not files:
        update_job(job_id, status="error", done=done, total=total,
                   error=f"no audio produced (exit code {exit_code})")
    elif (total and len(files) < total) or exit_code != 0:
        # spotdl exits 0 even when songs fail; judge by produced files.
        missing = f"{total - len(files)} of {total} tracks failed" if total else f"exit code {exit_code}"
        update_job(job_id, status="done_with_errors", done=done, total=total, error=missing)
    else:
        update_job(job_id, status="done", done=done, total=total, error=None)


def worker_loop() -> None:
    while True:
        try:
            claimed = None
            with db() as conn:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row is not None:
                    # atomic claim: loses cleanly to a concurrent cancel/delete
                    cur = conn.execute(
                        "UPDATE jobs SET status='running' WHERE id=? AND status='queued'",
                        (row["id"],),
                    )
                    if cur.rowcount == 1:
                        claimed = row["id"]
            if claimed is None:
                WAKE.wait(timeout=5.0)
                WAKE.clear()
                continue
            run_job(job_row(claimed))
        except Exception:  # noqa: BLE001 - the worker must never die
            import traceback
            traceback.print_exc()
            time.sleep(2)


# ------------------------------------------------------------------ file utils

def list_audio_files(job_dir: Path) -> list[dict]:
    files = []
    if job_dir.is_dir():
        for p in sorted(job_dir.rglob("*")):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                files.append({"name": str(p.relative_to(job_dir)), "size": p.stat().st_size})
    return files


def safe_file(job_dir: Path, name: str) -> Path:
    p = (job_dir / name).resolve()
    if not str(p).startswith(str(job_dir.resolve()) + os.sep):
        raise HTTPException(400, "bad path")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return p


# ------------------------------------------------------------------------ auth

COOKIE = "reprise_session"
_login_failures: list[float] = []


def session_token() -> str:
    return hmac.new(SECRET.encode(), b"reprise-auth-v1:" + PASSWORD.encode(), hashlib.sha256).hexdigest()


def authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token())


# ------------------------------------------------------------------------- app

app = FastAPI(title="Reprise")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    open_paths = {"/api/health", "/api/login"}
    if request.url.path.startswith("/api") and request.url.path not in open_paths:
        if not authed(request):
            return JSONResponse({"detail": "auth required"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
def health():
    return {"ok": True, "auth_required": bool(PASSWORD)}


@app.post("/api/login")
async def login(request: Request, response: Response):
    if not PASSWORD:
        return {"ok": True}
    now = time.time()
    _login_failures[:] = [t for t in _login_failures if now - t < 300]
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), PASSWORD):
        _login_failures.append(now)
        # progressive delay on failures only — a hard lockout would let an
        # attacker lock the real user out
        await asyncio.sleep(min(len(_login_failures), 5))
        raise HTTPException(403, "wrong password")
    response.set_cookie(COOKIE, session_token(), httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


def detect_kind(text: str) -> str:
    if "open.spotify.com" in text or text.startswith("spotify:"):
        return "spotify"
    if re.match(r"https?://", text):
        return "media"
    return "search"


@app.post("/api/jobs")
async def submit(request: Request):
    body = await request.json()
    text = str(body.get("input", "")).strip()
    if not text:
        raise HTTPException(400, "input required")
    if text.startswith("-"):
        # never let input masquerade as a CLI flag for spotdl/yt-dlp
        raise HTTPException(400, "input cannot start with '-'")
    fmt = str(body.get("format", "mp3"))
    if fmt not in ("mp3", "m4a", "opus", "flac"):
        raise HTTPException(400, "bad format")
    kind = detect_kind(text)
    title = text if len(text) < 80 else text[:77] + "..."
    job_id = create_job(kind, text, title, fmt, None)
    return {"id": job_id}


@app.post("/api/jobs/csv")
async def submit_csv(file: UploadFile, format: str = "mp3"):
    if format not in ("mp3", "m4a", "opus", "flac"):
        raise HTTPException(400, "bad format")
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV too large (5MB max)")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must be UTF-8 (Exportify export)")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "empty CSV")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    uri_col = cols.get("track uri")
    urls: list[str] = []
    for i, rec in enumerate(reader):
        if i >= MAX_CSV_ROWS:
            break
        if uri_col and (uri := (rec.get(uri_col) or "").strip()).startswith("spotify:track:"):
            track_id = uri.rsplit(":", 1)[1]
            if re.fullmatch(r"[A-Za-z0-9]+", track_id):
                urls.append("https://open.spotify.com/track/" + track_id)
    if not urls:
        raise HTTPException(400, "no spotify track URIs found — is this an Exportify CSV?")
    title = (file.filename or "playlist.csv").rsplit(".", 1)[0]
    job_id = create_job("csv", json.dumps(urls), title, format, len(urls))
    return {"id": job_id, "tracks": len(urls)}


@app.get("/api/jobs")
def jobs():
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 200").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("input", None)
        d["files"] = len(list_audio_files(Path(r["dir"]))) if r["status"] != "queued" else 0
        out.append(d)
    return {"jobs": out, "auth_required": bool(PASSWORD)}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    log_path = LOG_DIR / f"{job_id}.log"
    tail = ""
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-120:])
    d = dict(row)
    d["files"] = list_audio_files(Path(row["dir"]))
    d["log"] = tail
    return d


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    with RUNNING_LOCK:
        handle = RUNNING.get(job_id)
    if handle and handle.proc:
        handle.cancelled = True
        proc = handle.proc
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return {"ok": True}

        def _escalate():
            time.sleep(20)  # spotdl takes ~10-20s to wind down politely
            if proc.poll() is None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        threading.Thread(target=_escalate, daemon=True).start()
        return {"ok": True}
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=? AND status='queued'", (job_id,)
        )
    if cur.rowcount == 1:
        return {"ok": True}
    raise HTTPException(409, "job is not running or queued")


@app.delete("/api/jobs/{job_id}")
def remove(job_id: str, files: int = 0):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE id=? AND status NOT IN ('running','queued')"
            " OR id=? AND status='queued'", (job_id, job_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(409, "cancel it first")
    if files:
        job_dir = Path(row["dir"]).resolve()
        if str(job_dir).startswith(str(DOWNLOAD_DIR.resolve()) + os.sep):
            shutil.rmtree(job_dir, ignore_errors=True)
    (LOG_DIR / f"{job_id}.log").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{name:path}")
def download_file(job_id: str, name: str):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    p = safe_file(Path(row["dir"]), name)
    return FileResponse(p, filename=p.name)


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    job_dir = Path(row["dir"])
    files = list_audio_files(job_dir)
    if not files:
        raise HTTPException(404, "no files")
    zpath = job_dir / "_all.zip"
    newest = max((job_dir / f["name"]).stat().st_mtime for f in files)
    with ZIP_LOCK:
        if not zpath.is_file() or zpath.stat().st_mtime < newest:
            tmp = job_dir / f"_all.zip.tmp-{uuid.uuid4().hex[:8]}"
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                    for f in files:
                        z.write(job_dir / f["name"], f["name"])
                os.replace(tmp, zpath)
            finally:
                tmp.unlink(missing_ok=True)
    return FileResponse(zpath, filename=f"{slugify(row['title'])}.zip")


STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
def index():
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------- startup

init_db()
if not os.access(DOWNLOAD_DIR, os.W_OK):
    print(
        f"WARNING: {DOWNLOAD_DIR} is not writable by uid {os.getuid()}. "
        "On a NAS, chown the mounted downloads folder to uid 10001 "
        "(or adjust its permissions). Downloads WILL fail until fixed.",
        flush=True,
    )
threading.Thread(target=worker_loop, daemon=True, name="reprise-worker").start()
