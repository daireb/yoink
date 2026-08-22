"""Yoink — self-hosted music download portal.

FastAPI app wrapping spotDL (Spotify -> YouTube Music match -> audio) and
yt-dlp (YouTube and most other media sites). Jobs run in a single background
worker; state persists in SQLite so the queue survives restarts.

Job lifecycle (spotify / csv / search kinds):
  queued -> resolving  (spotdl save  -> tracks.spotdl: the expected track list)
         -> downloading (spotdl download tracks.spotdl)
         -> finishing   (match files to expected, number them, write m3u)
         -> done | done_with_errors (some tracks missing; retryable)
Media kind (yt-dlp) follows the same shape with yt-dlp -J as the resolve step.
"""

import asyncio
import csv
import difflib
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
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

# ---------------------------------------------------------------- configuration

DOWNLOAD_DIR = Path(os.environ.get("YOINK_DOWNLOAD_DIR", "/downloads"))
DATA_DIR = Path(os.environ.get("YOINK_DATA_DIR", "/data"))
PASSWORD = os.environ.get("YOINK_PASSWORD", "").strip()
SPOTDL_THREADS = os.environ.get("YOINK_THREADS", "4")
KEEP_DAYS = float(os.environ.get("YOINK_KEEP_DAYS", "7"))
# kill a job step if its process produces no output for this long (seconds)
STALL_TIMEOUT = int(os.environ.get("YOINK_STALL_TIMEOUT", "1800"))
MAX_CSV_ROWS = 1000
MAX_CSV_BYTES = 5 * 1024 * 1024
AUDIO_EXTS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav"}
FORMATS = ("mp3", "m4a", "opus", "flac")
BITRATES = (320, 192, 128)

LOG_DIR = DATA_DIR / "logs"
PREFLIGHT_DIR = DATA_DIR / "preflight"
DB_PATH = DATA_DIR / "yoink.db"

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\r")
NUM_PREFIX_RE = re.compile(r"^\d{2,3} - ")
SPOTDL_FLAGS = ["--simple-tui", "--log-level", "INFO", "--headless"]


def _load_secret() -> str:
    """Cookie-signing secret: env override, else generated once into /data."""
    if env := os.environ.get("YOINK_SECRET", ""):
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
        return secrets.token_hex(32)


SECRET = _load_secret()

# ------------------------------------------------------------------------- db

COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "created": "REAL NOT NULL",
    "finished": "REAL",
    "kind": "TEXT NOT NULL",
    "input": "TEXT NOT NULL",
    "title": "TEXT NOT NULL",
    "format": "TEXT NOT NULL DEFAULT 'mp3'",
    "bitrate": "INTEGER NOT NULL DEFAULT 320",
    "numbered": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL",
    "step": "TEXT",
    "total": "INTEGER",
    "done": "INTEGER NOT NULL DEFAULT 0",
    "error": "TEXT",
    "dir": "TEXT NOT NULL",
    "expected": "TEXT",   # JSON: ordered list of {pos,name,artists,duration,url,id}
    "missing": "TEXT",    # JSON: subset of expected not found on disk
    "parent_id": "TEXT",  # set on retry jobs; hidden from the list
    "cover": "TEXT",      # album/playlist/video artwork URL for the card
}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    for d in (LOG_DIR, PREFLIGHT_DIR, DOWNLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        cols = ", ".join(f"{k} {v}" for k, v in COLUMNS.items())
        conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")
        existing = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        for k, v in COLUMNS.items():
            if k not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {k} {v.replace('PRIMARY KEY', '')}")
        conn.execute(
            "UPDATE jobs SET status='interrupted', error='server restarted mid-job',"
            " finished=? WHERE status IN ('running','retrying')", (time.time(),)
        )
        for r in conn.execute("SELECT id, dir FROM jobs WHERE cover IS NULL").fetchall():
            f = Path(r[1]) / "tracks.spotdl"
            try:
                songs = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
                if songs and songs[0].get("cover_url"):
                    conn.execute("UPDATE jobs SET cover=? WHERE id=?", (songs[0]["cover_url"], r[0]))
            except (OSError, ValueError):
                pass
        # rows from before `finished` existed would otherwise never expire
        conn.execute(
            "UPDATE jobs SET finished=created WHERE finished IS NULL"
            " AND status NOT IN ('queued','running','retrying')"
        )


def job_row(job_id: str) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def update_job(job_id: str, **fields) -> None:
    keys = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {keys} WHERE id=?", (*fields.values(), job_id))


# ------------------------------------------------------------------ job model

class JobHandle:
    def __init__(self, job_id: str):
        self.id = job_id
        self.proc: Optional[subprocess.Popen] = None
        self.cancelled = False


RUNNING: dict[str, JobHandle] = {}
RUNNING_LOCK = threading.Lock()
WAKE = threading.Event()


def slugify(text: str, maxlen: int = 40) -> str:
    text = re.sub(r"https?://", "", text.strip().lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:maxlen] or "job"


def create_job(kind: str, input_text: str, title: str, fmt: str, bitrate: int,
               numbered: bool, total: Optional[int], expected=None,
               job_dir: Optional[Path] = None, parent_id: Optional[str] = None) -> str:
    job_id = uuid.uuid4().hex[:12]
    if job_dir is None:
        job_dir = DOWNLOAD_DIR / f"{slugify(title)}-{job_id[:6]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, created, kind, input, title, format, bitrate, numbered,"
            " status, total, done, dir, expected, parent_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (job_id, time.time(), kind, input_text, title, fmt, bitrate, int(numbered),
             "queued", total, str(job_dir), json.dumps(expected) if expected else None,
             parent_id),
        )
    WAKE.set()
    return job_id


# ------------------------------------------------------------- track matching

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def expected_key(t: dict) -> str:
    artists = ", ".join(t.get("artists") or [])
    return norm(f"{artists} - {t['name']}" if artists else t["name"])


def file_key(p: Path) -> str:
    return norm(NUM_PREFIX_RE.sub("", p.stem))


def parse_spotdl_file(path: Path, order_hint: Optional[list[str]] = None) -> list[dict]:
    """Read a .spotdl save file into an ordered expected-track list.

    spotdl writes songs in fetch order, not list order: sort by list_position
    when present, else by `order_hint` (song ids / urls in the order the user
    gave them), else leave as-is.
    """
    songs = json.loads(path.read_text(encoding="utf-8"))
    tracks = [{
        "name": s.get("name") or "",
        "artists": s.get("artists") or ([s["artist"]] if s.get("artist") else []),
        "duration": s.get("duration") or 0,
        "url": s.get("url") or "",
        "id": s.get("song_id") or "",
        "list_name": s.get("list_name"),
        "cover": s.get("cover_url") or "",
        "pos": s.get("list_position"),
        "_disc": s.get("disc_number") or 0,
        "_track": s.get("track_number") or 0,
    } for s in songs]
    if tracks and any(t["pos"] for t in tracks):
        tracks.sort(key=lambda t: (t["pos"] is None, t["pos"] or 0))
    elif tracks and any(t["_track"] for t in tracks):
        tracks.sort(key=lambda t: (t["_disc"], t["_track"]))  # albums
    elif order_hint:
        rank = {h: i for i, h in enumerate(order_hint)}
        tracks.sort(key=lambda t: min(rank.get(t["id"], 1e9), rank.get(t["url"], 1e9)))
    for i, t in enumerate(tracks, 1):
        t["pos"] = i
        t.pop("_disc", None)
        t.pop("_track", None)
    return tracks


def match_files(expected: list[dict], files: list[Path]) -> tuple[dict[int, Path], list[dict]]:
    """Map expected track -> produced file. Returns (pos->file, missing)."""
    by_key: dict[str, list[Path]] = {}
    for f in files:
        by_key.setdefault(file_key(f), []).append(f)
    matched: dict[int, Path] = {}
    used: set[Path] = set()
    unmatched_exp = []
    seen_keys: set[str] = set()
    for t in expected:
        k = expected_key(t)
        pool = by_key.get(k) or []
        if pool:
            f = pool.pop(0)
            matched[t["pos"]] = f
            used.add(f)
            seen_keys.add(k)
        elif k in seen_keys:
            # same title appears twice in the list; spotdl writes one file for
            # both, so the second is a duplicate rather than a failure
            continue
        else:
            unmatched_exp.append(t)
    # fuzzy second pass for the leftovers (sanitized chars, slight title drift)
    leftover_keys = {file_key(f): f for f in files if f not in used}
    missing = []
    for t in unmatched_exp:
        close = difflib.get_close_matches(expected_key(t), list(leftover_keys), n=1, cutoff=0.8)
        if close:
            f = leftover_keys.pop(close[0])
            matched[t["pos"]] = f
            used.add(f)
        else:
            missing.append(t)
    return matched, missing


def list_audio_files(job_dir: Path) -> list[Path]:
    if not job_dir.is_dir():
        return []
    return sorted(p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def finalize_job_dir(job_id: str, expected: list[dict], numbered: bool, job_dir: Path) -> list[dict]:
    """Match, number, write m3u. Returns the missing list."""
    files = list_audio_files(job_dir)
    matched, missing = match_files(expected, files) if expected else ({}, [])
    width = 3 if len(expected) >= 100 else 2
    ordered: list[Path] = []
    for t in expected:
        f = matched.get(t["pos"])
        if f is None:
            continue
        base = NUM_PREFIX_RE.sub("", f.name)
        # number by position among the files that exist, so gaps (duplicates,
        # missing tracks) don't leave holes in the sequence
        want = f.with_name(f"{len(ordered) + 1:0{width}d} - {base}" if numbered else base)
        if want != f and not want.exists():
            f.rename(want)
            f = want
        ordered.append(f)
    if len(expected) > 1 and ordered:
        m3u = job_dir / "playlist.m3u8"
        m3u.write_text("#EXTM3U\n" + "".join(f"{p.name}\n" for p in ordered), encoding="utf-8")
    return missing


# ------------------------------------------------------------- command builder

def spotdl_download_cmd(spotdl_file: Path, fmt: str, bitrate: int, job_dir: Path) -> list[str]:
    cmd = ["spotdl", "download", str(spotdl_file),
           "--format", fmt, "--threads", SPOTDL_THREADS,
           "--output", str(job_dir / "{artists} - {title}.{output-ext}"),
           *SPOTDL_FLAGS, "--print-errors"]
    # spotdl's config default is 128k(!) — force it for mp3; for source/lossless
    # formats skip the transcode entirely.
    cmd += ["--bitrate", f"{bitrate}k" if fmt == "mp3" else "disable"]
    return cmd


def ytdlp_download_cmd(url: str, fmt: str, bitrate: int, job_dir: Path, is_playlist: bool) -> list[str]:
    tmpl = "%(title)s.%(ext)s"
    cmd = ["yt-dlp", "--extract-audio", "--audio-format", fmt,
           "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg",
           "--parse-metadata", "%(artist,uploader|)s:%(meta_artist)s",
           "--newline", "--no-colors", "--no-overwrites",
           "-o", str(job_dir / tmpl)]
    cmd += ["--yes-playlist"] if is_playlist else ["--no-playlist"]
    if fmt == "mp3":
        cmd += ["--audio-quality", f"{bitrate}K"]
    cmd.append(url)
    return cmd


# ------------------------------------------------------------ progress parsing

SPOTDL_COMPLETE_RE = re.compile(r"^(\d+)/(\d+) complete")
YTDLP_ITEM_RE = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
YTDLP_DONE_RE = re.compile(r"\[ExtractAudio\] Destination:")
YTDLP_FINISHED_RE = re.compile(r"\[download\] 100% of .+ in |has already been downloaded")


def parse_progress(kind: str, line: str, state: dict) -> None:
    if kind == "media":
        if m := YTDLP_ITEM_RE.search(line):
            state["total"] = int(m.group(2))
        if YTDLP_DONE_RE.search(line):
            state["extracted"] = state.get("extracted", 0) + 1
        if YTDLP_FINISHED_RE.search(line):
            state["finished"] = state.get("finished", 0) + 1
        state["done"] = max(state.get("extracted", 0), state.get("finished", 0))
    else:
        if m := SPOTDL_COMPLETE_RE.match(line):
            state["done"] = int(m.group(1))
            state["total"] = max(int(m.group(2)), state.get("total") or 0)


# ------------------------------------------------------------------ the worker

def run_step(handle: JobHandle, cmd: list[str], log, cwd: Path, kind: str, state: dict,
             on_progress) -> int:
    """Run one subprocess, streaming output to the log; returns exit code."""
    log.write("$ " + " ".join(cmd[:6]) + (" ..." if len(cmd) > 6 else "") + "\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, cwd=str(cwd), start_new_session=True)
    handle.proc = proc
    last_output = [time.time()]

    def _watchdog():
        while proc.poll() is None:
            if time.time() - last_output[0] > STALL_TIMEOUT:
                state["stalled"] = True
                kill_group(proc)
                return
            time.sleep(10)

    threading.Thread(target=_watchdog, daemon=True).start()
    last_flush = 0.0
    assert proc.stdout is not None
    for raw in proc.stdout:
        last_output[0] = time.time()
        line = ANSI_RE.sub("", raw).rstrip()
        if not line:
            continue
        log.write(line + "\n")
        parse_progress(kind, line, state)
        now = time.time()
        if now - last_flush > 1.0:
            log.flush()
            on_progress()
            last_flush = now
    code = proc.wait()
    handle.proc = None
    log.flush()
    return code


def kill_group(proc: subprocess.Popen) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        for _ in range(20):  # spotdl takes ~10-20s to wind down politely
            if proc.poll() is not None:
                return
            time.sleep(1)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_job(row: sqlite3.Row) -> None:
    job_id, kind = row["id"], row["kind"]
    job_dir = Path(row["dir"])
    handle = JobHandle(job_id)
    with RUNNING_LOCK:
        RUNNING[job_id] = handle
    state = {"done": 0, "total": row["total"]}
    expected = json.loads(row["expected"]) if row["expected"] else []
    log_path = LOG_DIR / f"{job_id}.log"
    if row["parent_id"] and job_row(row["parent_id"]) is None:
        with db() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        with RUNNING_LOCK:
            RUNNING.pop(job_id, None)
        return

    def progress():
        update_job(job_id, done=state.get("done", 0), total=state.get("total"))

    try:
        with open(log_path, "a", encoding="utf-8") as log:
            # ---- step 1: resolve the expected track list -------------------
            update_job(job_id, step="resolving")
            spotdl_file = job_dir / ("tracks.spotdl" if not row["parent_id"] else f"retry-{job_id}.spotdl")
            if kind == "media":
                is_playlist = "list=" in row["input"] or "/playlist" in row["input"]
                if not expected:
                    info = probe_media(row["input"])
                    expected = info["tracks"]
                    title = info["title"]
                    update_job(job_id, title=title, total=len(expected) or None,
                               expected=json.dumps(expected), cover=info.get("cover") or None)
                    state["total"] = len(expected) or None
            else:
                if not spotdl_file.is_file():
                    queries = json.loads(row["input"]) if kind in ("csv", "retry") else [row["input"]]
                    code = run_step(handle, ["spotdl", "save", *queries, "--save-file",
                                             str(spotdl_file), *SPOTDL_FLAGS],
                                    log, job_dir, kind, state, progress)
                    if handle.cancelled or state.get("stalled"):
                        raise _Stop()
                    if code != 0 or not spotdl_file.is_file():
                        raise RuntimeError("couldn't look up the tracks on Spotify")
                if not expected:
                    hint = json.loads(row["input"]) if kind in ("csv", "retry") else None
                    expected = parse_spotdl_file(spotdl_file, order_hint=hint)
                    if not expected:
                        raise RuntimeError("no tracks found for that input")
                    title = row["title"]
                    if kind == "spotify" and expected[0].get("list_name"):
                        title = expected[0]["list_name"]
                    elif kind in ("spotify", "search") and len(expected) == 1:
                        t = expected[0]
                        title = f"{', '.join(t['artists'])} - {t['name']}" if t["artists"] else t["name"]
                    update_job(job_id, title=title, total=len(expected), expected=json.dumps(expected),
                               cover=expected[0].get("cover") or None)
                    state["total"] = len(expected)

            if handle.cancelled:
                raise _Stop()
            # ---- step 2: download -----------------------------------------
            update_job(job_id, step="downloading")
            if kind == "media":
                cmd = ytdlp_download_cmd(row["input"], row["format"], row["bitrate"], job_dir, is_playlist)
            else:
                cmd = spotdl_download_cmd(spotdl_file, row["format"], row["bitrate"], job_dir)
            code = run_step(handle, cmd, log, job_dir, kind, state, progress)
            if handle.cancelled or state.get("stalled"):
                raise _Stop()

            # ---- step 3: finish -------------------------------------------
            update_job(job_id, step="finishing")
            target_id = row["parent_id"] or job_id
            target = job_row(target_id) if row["parent_id"] else row
            target_expected = json.loads(target["expected"]) if target and target["expected"] else expected
            missing = finalize_job_dir(target_id, target_expected, bool(target["numbered"]), job_dir)
            files = list_audio_files(job_dir)
            status, error = "done", None
            if not files:
                status, error = "error", f"nothing was downloaded (exit code {code})"
            elif missing or (not target_expected and code != 0):
                status = "done_with_errors"
                error = f"{len(missing)} of {len(target_expected)} tracks couldn't be found" if missing else f"exit code {code}"
            update_job(target_id, status=status, error=error, step=None, finished=time.time(),
                       done=len(target_expected) - len(missing) if target_expected else len(files),
                       total=len(target_expected) or state.get("total"),
                       missing=json.dumps(missing) if missing else None)
            if row["parent_id"]:
                spotdl_file.unlink(missing_ok=True)
                with db() as conn:
                    conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    except _Stop:
        target_id = row["parent_id"] or job_id
        if state.get("stalled"):
            update_job(target_id, status="error", step=None, finished=time.time(),
                       error=f"stalled: no output for {STALL_TIMEOUT}s, stopped")
        else:
            update_job(target_id, status="cancelled", step=None, finished=time.time())
        if row["parent_id"]:
            with db() as conn:
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    except Exception as exc:  # noqa: BLE001 - a job must record any failure
        target_id = row["parent_id"] or job_id
        msg = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        update_job(target_id, status="error", step=None, error=msg, finished=time.time())
        if row["parent_id"]:
            with db() as conn:
                conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    finally:
        with RUNNING_LOCK:
            RUNNING.pop(job_id, None)


class _Stop(Exception):
    pass


def probe_media(url: str) -> dict:
    """yt-dlp metadata probe: title + entries, no download."""
    is_playlist = "list=" in url or "/playlist" in url
    cmd = ["yt-dlp", "-J", "--no-warnings", "--flat-playlist" if is_playlist else "--no-playlist", url]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0 or not out.stdout.strip():
        raise RuntimeError("couldn't read that link")
    info = json.loads(out.stdout)

    def thumb(obj: dict) -> str:
        if obj.get("thumbnail"):
            return obj["thumbnail"]
        ts = obj.get("thumbnails") or []
        return ts[-1].get("url", "") if ts else ""

    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        tracks = [{"pos": i, "name": e.get("title") or f"item {i}", "artists": [],
                   "duration": e.get("duration") or 0, "url": e.get("url") or "", "id": e.get("id") or ""}
                  for i, e in enumerate(entries, 1)]
        return {"title": info.get("title") or url, "tracks": tracks, "is_playlist": True,
                "cover": thumb(info) or (thumb(entries[0]) if entries else "")}
    return {"title": info.get("title") or url, "is_playlist": False, "cover": thumb(info),
            "tracks": [{"pos": 1, "name": info.get("title") or url, "artists": [],
                        "duration": info.get("duration") or 0, "url": url, "id": info.get("id") or ""}]}


def worker_loop() -> None:
    while True:
        try:
            claimed = None
            with db() as conn:
                row = conn.execute(
                    "SELECT id FROM jobs WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()
                if row is not None:
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


# ---------------------------------------------------------------- housekeeping

_disk_cache = {"at": 0.0, "bytes": 0}


def disk_used() -> int:
    if time.time() - _disk_cache["at"] > 60:
        total = 0
        for root, _dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        _disk_cache.update(at=time.time(), bytes=total)
    return _disk_cache["bytes"]


def remove_job_files(row: sqlite3.Row) -> None:
    job_dir = Path(row["dir"]).resolve()
    if str(job_dir).startswith(str(DOWNLOAD_DIR.resolve()) + os.sep):
        shutil.rmtree(job_dir, ignore_errors=True)
    (LOG_DIR / f"{row['id']}.log").unlink(missing_ok=True)


def retention_loop() -> None:
    while True:
        try:
            cutoff = time.time() - KEEP_DAYS * 86400
            with db() as conn:
                old = conn.execute(
                    "SELECT * FROM jobs WHERE parent_id IS NULL AND finished IS NOT NULL"
                    " AND finished < ? AND status NOT IN ('running','retrying','queued')", (cutoff,)
                ).fetchall()
            for row in old:
                remove_job_files(row)
                with db() as conn:
                    conn.execute("DELETE FROM jobs WHERE id=? OR parent_id=?", (row["id"], row["id"]))
            for p in PREFLIGHT_DIR.glob("*.spotdl"):
                if p.stat().st_mtime < time.time() - 86400:
                    p.unlink(missing_ok=True)
            # directories no job references (removed records, crashes) age out too
            with db() as conn:
                referenced = {Path(r[0]).name for r in conn.execute("SELECT dir FROM jobs")}
            for d in DOWNLOAD_DIR.iterdir():
                if d.is_dir() and d.name not in referenced and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            _disk_cache["at"] = 0
        except Exception:  # noqa: BLE001
            import traceback
            traceback.print_exc()
        time.sleep(3600)


# ------------------------------------------------------------------------ auth

COOKIE = "yoink_session"
_login_failures: list[float] = []


def session_token() -> str:
    return hmac.new(SECRET.encode(), b"yoink-auth-v1:" + PASSWORD.encode(), hashlib.sha256).hexdigest()


def authed(request: Request) -> bool:
    if not PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token())


# ------------------------------------------------------------------------- app

import mimetypes
mimetypes.add_type("font/woff2", ".woff2")  # slim images lack /etc/mime.types

app = FastAPI(title="Yoink")
PREFLIGHT_SLOTS = asyncio.Semaphore(2)
LOGIN_LOCK = asyncio.Lock()


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
    async with LOGIN_LOCK:  # serialized, so the delay can't be parallelised away
        if not hmac.compare_digest(str(body.get("password", "")), PASSWORD):
            _login_failures.append(now)
            await asyncio.sleep(min(len(_login_failures), 5))
            raise HTTPException(403, "wrong password")
    response.set_cookie(COOKIE, session_token(), httponly=True, samesite="lax",
                        max_age=60 * 60 * 24 * 30)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


def assert_public_url(url: str) -> None:
    """yt-dlp will fetch anything; don't let a link point it at the LAN."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if not host:
        raise HTTPException(400, "bad link")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, "that host doesn't resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise HTTPException(400, "links to private/internal addresses aren't allowed")


def detect_kind(text: str) -> str:
    if "open.spotify.com" in text or text.startswith("spotify:"):
        return "spotify"
    if re.match(r"https?://", text):
        return "media"
    return "search"


def parse_options(body: dict) -> tuple[str, int, Optional[bool]]:
    fmt = str(body.get("format", "mp3"))
    if fmt not in FORMATS:
        raise HTTPException(400, "bad format")
    try:
        bitrate = int(body.get("bitrate", 320))
    except (TypeError, ValueError):
        raise HTTPException(400, "bad bitrate")
    if bitrate not in BITRATES:
        raise HTTPException(400, "bad bitrate")
    numbered = body.get("numbered")
    return fmt, bitrate, None if numbered is None else bool(numbered)


def clean_input(text: str) -> str:
    text = text.strip()
    if not text:
        raise HTTPException(400, "paste a link or type a search")
    if text.startswith("-"):
        raise HTTPException(400, "input cannot start with '-'")
    return text


def est_mb(tracks: list[dict], bitrate: int) -> float:
    secs = sum(t.get("duration") or 0 for t in tracks)
    return round(secs * bitrate * 1000 / 8 / 1_000_000, 1)


@app.post("/api/preflight")
async def preflight(request: Request):
    """Look up what a link points at — name, track count, size — before committing."""
    if PREFLIGHT_SLOTS.locked():
        raise HTTPException(429, "busy looking things up — try again in a moment")
    async with PREFLIGHT_SLOTS:
        return await _preflight(request)


async def _preflight(request: Request):
    body = await request.json()
    text = clean_input(str(body.get("input", "")))
    fmt, bitrate, _ = parse_options(body)
    kind = detect_kind(text)
    if kind == "media":
        await asyncio.to_thread(assert_public_url, text)
        try:
            info = await asyncio.to_thread(probe_media, text)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc) or "couldn't read that link")
        return {"kind": kind, "token": None, "title": info["title"], "count": len(info["tracks"]),
                "tracks": [t["name"] for t in info["tracks"][:300]],
                "est_mb": est_mb(info["tracks"], bitrate), "is_playlist": info["is_playlist"]}
    token = uuid.uuid4().hex[:16]
    out = PREFLIGHT_DIR / f"{token}.spotdl"
    cmd = ["spotdl", "save", text, "--save-file", str(out), *SPOTDL_FLAGS]
    try:
        res = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=300, cwd=str(PREFLIGHT_DIR))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Spotify lookup timed out")
    if res.returncode != 0 or not out.is_file():
        raise HTTPException(400, "couldn't find that on Spotify")
    tracks = parse_spotdl_file(out)
    if not tracks:
        out.unlink(missing_ok=True)
        raise HTTPException(400, "no tracks found")
    if tracks[0].get("list_name") and len(tracks) > 1:
        title = tracks[0]["list_name"]
    else:
        t = tracks[0]
        title = f"{', '.join(t['artists'])} - {t['name']}" if t["artists"] else t["name"]
    return {"kind": kind, "token": token, "title": title, "count": len(tracks),
            "tracks": [f"{', '.join(t['artists'])} - {t['name']}" if t["artists"] else t["name"]
                       for t in tracks[:300]],
            "est_mb": est_mb(tracks, bitrate), "is_playlist": len(tracks) > 1}


@app.post("/api/jobs")
async def submit(request: Request):
    body = await request.json()
    text = clean_input(str(body.get("input", "")))
    fmt, bitrate, numbered = parse_options(body)
    kind = detect_kind(text)
    if kind == "media":
        await asyncio.to_thread(assert_public_url, text)
    title = body.get("title") or (text if len(text) < 80 else text[:77] + "...")
    expected, total = None, None
    token = body.get("token")
    pre: Optional[Path] = None
    if token and re.fullmatch(r"[0-9a-f]{16}", str(token)):
        pre = PREFLIGHT_DIR / f"{token}.spotdl"
        if pre.is_file():
            expected = parse_spotdl_file(pre)
            total = len(expected)
        else:
            pre = None
    if numbered is None:
        numbered = (total or 0) > 1
    job_id = create_job(kind, text, str(title)[:120], fmt, bitrate, numbered, total, expected)
    if expected and expected[0].get("cover"):
        update_job(job_id, cover=expected[0]["cover"])
    if pre is not None:
        shutil.move(str(pre), str(Path(job_row(job_id)["dir"]) / "tracks.spotdl"))
    return {"id": job_id}


def parse_csv(raw: bytes) -> tuple[list[str], list[dict]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(400, "CSV must be UTF-8 (an Exportify export)")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(400, "empty CSV")
    cols = {c.strip().lower(): c for c in reader.fieldnames}
    uri_col, name_col, artist_col, dur_col = (cols.get(k) for k in
                                              ("track uri", "track name", "artist name(s)", "duration (ms)"))
    urls, tracks = [], []
    for i, rec in enumerate(reader):
        if i >= MAX_CSV_ROWS:
            break
        uri = (rec.get(uri_col) or "").strip() if uri_col else ""
        if not uri.startswith("spotify:track:"):
            continue
        track_id = uri.rsplit(":", 1)[1]
        if not re.fullmatch(r"[A-Za-z0-9]+", track_id):
            continue
        url = "https://open.spotify.com/track/" + track_id
        urls.append(url)
        artists = [a.strip() for a in ((rec.get(artist_col) or "") if artist_col else "").split(";") if a.strip()]
        try:
            dur = int(float(rec.get(dur_col) or 0)) // 1000 if dur_col else 0
        except ValueError:
            dur = 0
        tracks.append({"pos": len(urls), "name": (rec.get(name_col) or "").strip() if name_col else "",
                       "artists": artists, "duration": dur, "url": url, "id": track_id})
    if not urls:
        raise HTTPException(400, "no Spotify tracks found — is this an Exportify CSV?")
    return urls, tracks


@app.post("/api/jobs/csv")
async def submit_csv(file: UploadFile, format: str = "mp3", bitrate: int = 320,
                     numbered: Optional[int] = None, dry_run: int = 0):
    fmt, br, num = parse_options({"format": format, "bitrate": bitrate, "numbered": numbered})
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV too large (5MB max)")
    urls, tracks = parse_csv(raw)
    title = (file.filename or "playlist.csv").rsplit(".", 1)[0]
    if dry_run:
        return {"title": title, "count": len(urls), "est_mb": est_mb(tracks, br),
                "tracks": [f"{', '.join(t['artists'])} - {t['name']}" if t["artists"] else t["name"]
                           for t in tracks[:300]]}
    if num is None:
        num = len(urls) > 1
    job_id = create_job("csv", json.dumps(urls), title, fmt, br, num, len(urls), expected=None)
    return {"id": job_id, "count": len(urls)}


def public_job(r: sqlite3.Row, with_files: bool = False) -> dict:
    d = {k: r[k] for k in ("id", "created", "finished", "kind", "title", "format", "bitrate",
                           "numbered", "status", "step", "total", "done", "error", "cover")}
    # the original link/query, so the UI can offer "Try again" (not for CSV jobs: that's a URL list)
    d["input"] = r["input"] if r["kind"] in ("spotify", "media", "search") else None
    job_dir = Path(r["dir"])
    files = list_audio_files(job_dir) if r["status"] != "queued" else []
    d["files_count"] = len(files)
    d["first_file"] = files[0].name if files else None  # for the preview button
    d["size_bytes"] = sum(f.stat().st_size for f in files)
    d["missing"] = [(f"{', '.join(t['artists'])} - {t['name']}" if t.get("artists") else t["name"])
                    for t in json.loads(r["missing"])] if r["missing"] else []
    d["keep_until"] = (r["finished"] + KEEP_DAYS * 86400) if r["finished"] else None
    if with_files:
        d["files"] = [{"name": f.name, "size": f.stat().st_size} for f in files]
    return d


@app.get("/api/jobs")
def jobs():
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE parent_id IS NULL ORDER BY created DESC LIMIT 200").fetchall()
    return {"jobs": [public_job(r) for r in rows], "auth_required": bool(PASSWORD),
            "keep_days": KEEP_DAYS, "disk_used": disk_used()}


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: str):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    d = public_job(row, with_files=True)
    log_path = LOG_DIR / f"{job_id}.log"
    tail = ""
    if log_path.is_file():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-150:])
    d["log"] = tail
    return d


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    with db() as conn:
        child = conn.execute("SELECT id FROM jobs WHERE parent_id=?", (job_id,)).fetchone()
    targets = [job_id] + ([child["id"]] if child else [])
    for tid in targets:
        with RUNNING_LOCK:
            handle = RUNNING.get(tid)
        if handle:
            handle.cancelled = True
            if handle.proc:
                threading.Thread(target=kill_group, args=(handle.proc,), daemon=True).start()
            return {"ok": True}
    with db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='cancelled', finished=? WHERE id IN (?, ?) AND status='queued'",
            (time.time(), job_id, targets[-1]))
        if cur.rowcount and child:
            conn.execute("DELETE FROM jobs WHERE id=?", (child["id"],))
            conn.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=?", (time.time(), job_id))
    if cur.rowcount >= 1:
        return {"ok": True}
    raise HTTPException(409, "job is not running or queued")


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    if row["status"] in ("running", "queued", "retrying"):
        raise HTTPException(409, "job is still active")
    job_dir = Path(row["dir"])
    expected = json.loads(row["expected"]) if row["expected"] else []
    if row["kind"] == "media":
        # yt-dlp skips files that already exist, so just re-run the whole link
        create_job("media", row["input"], row["title"], row["format"], row["bitrate"],
                   bool(row["numbered"]), row["total"], expected, job_dir=job_dir, parent_id=job_id)
    else:
        _matched, missing = match_files(expected, list_audio_files(job_dir))
        if not missing:
            update_job(job_id, status="done", error=None, missing=None)
            raise HTTPException(409, "nothing is missing")
        create_job("retry", json.dumps([t["url"] for t in missing]), row["title"], row["format"],
                   row["bitrate"], bool(row["numbered"]), len(missing), missing,
                   job_dir=job_dir, parent_id=job_id)
    update_job(job_id, status="retrying", step=None, error=None)
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def remove(job_id: str, files: int = 1):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE id=? AND status NOT IN ('running','retrying')", (job_id,))
        if cur.rowcount:
            conn.execute("DELETE FROM jobs WHERE parent_id=? AND status='queued'", (job_id,))
    if cur.rowcount == 0:
        raise HTTPException(409, "stop it first")
    if files:
        remove_job_files(row)
    else:
        (LOG_DIR / f"{job_id}.log").unlink(missing_ok=True)
    _disk_cache["at"] = 0
    return {"ok": True}


def started_cookie(resp, t: Optional[str]):
    """Download-started handshake: the page polls for this cookie to know the
    browser has begun receiving the file (used to end the 'Preparing…' state)."""
    if t and re.fullmatch(r"[a-z0-9]{8,32}", t):
        resp.set_cookie("yoink_dl", t, max_age=60, samesite="lax", path="/")
    return resp


def safe_file(job_dir: Path, name: str) -> Path:
    p = (job_dir / name).resolve()
    if not str(p).startswith(str(job_dir.resolve()) + os.sep):
        raise HTTPException(400, "bad path")
    if not p.is_file():
        raise HTTPException(404, "file not found")
    return p


@app.get("/api/jobs/{job_id}/files/{name:path}")
def download_file(job_id: str, name: str, request: Request, t: Optional[str] = None):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    p = safe_file(Path(row["dir"]), name)
    size = p.stat().st_size
    media_type = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/ogg",
                  ".ogg": "audio/ogg", ".flac": "audio/flac", ".wav": "audio/wav"}.get(p.suffix.lower(), "application/octet-stream")
    rng = request.headers.get("range")
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng or "")
    if not m:
        return started_cookie(FileResponse(p, media_type=media_type, filename=p.name,
                                           headers={"Accept-Ranges": "bytes"}), t)
    # byte ranges: needed for seeking, and iOS Safari won't play audio without them
    start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2) or 0))
    end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})

    def chunks(path=p, pos=start, remaining=end - start + 1):
        with open(path, "rb") as fh:
            fh.seek(pos)
            while remaining > 0:
                buf = fh.read(min(1 << 16, remaining))
                if not buf:
                    break
                remaining -= len(buf)
                yield buf

    from fastapi.responses import StreamingResponse
    return StreamingResponse(chunks(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{p.name}"',
    })


@app.get("/api/jobs/{job_id}/zip")
def download_zip(job_id: str, t: Optional[str] = None):
    row = job_row(job_id)
    if not row:
        raise HTTPException(404, "no such job")
    job_dir = Path(row["dir"])
    files = list_audio_files(job_dir)
    if not files:
        raise HTTPException(404, "no files")
    m3u = job_dir / "playlist.m3u8"
    members = files + ([m3u] if m3u.is_file() else [])
    zpath = job_dir / f"_save-{uuid.uuid4().hex[:8]}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for f in members:
            z.write(f, f.name)
    return started_cookie(FileResponse(zpath, filename=f"{slugify(row['title'])}.zip",
                                       background=BackgroundTask(zpath.unlink, missing_ok=True)), t)


STATIC_DIR = Path(__file__).parent / "static"
STATIC_REF_RE = re.compile(r'(/static/[\w./-]+\.(?:png|jpg|svg|woff2|js|css))(?=["\')])')
_stamp_cache = {"at": 0.0, "value": "0"}


def static_stamp() -> str:
    """Build stamp = newest mtime under /static, so a changed icon or font is
    never served from a stale browser cache."""
    if time.time() - _stamp_cache["at"] > 5:
        newest = 0.0
        for root, _dirs, files in os.walk(STATIC_DIR):
            for f in files:
                try:
                    newest = max(newest, os.stat(os.path.join(root, f)).st_mtime)
                except OSError:
                    pass
        _stamp_cache.update(at=time.time(), value=format(int(newest), "x"))
    return _stamp_cache["value"]


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(STATIC_REF_RE.sub(rf"\1?v={static_stamp()}", html))


@app.get("/manifest.webmanifest")
def manifest():
    text = (STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8")
    return Response(STATIC_REF_RE.sub(rf"\1?v={static_stamp()}", text),
                    media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC_DIR / "sw.js", media_type="application/javascript")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------- startup

init_db()
if not os.access(DOWNLOAD_DIR, os.W_OK):
    print(f"WARNING: {DOWNLOAD_DIR} is not writable by uid {os.getuid()}. On a NAS, chown the "
          "mounted downloads folder to uid 10001. Downloads WILL fail until fixed.", flush=True)
threading.Thread(target=worker_loop, daemon=True, name="yoink-worker").start()
threading.Thread(target=retention_loop, daemon=True, name="yoink-retention").start()
