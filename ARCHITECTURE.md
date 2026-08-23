# Architecture

Notes on how Yoink is put together, the engine quirks it works around, and
what its security model actually guarantees. For installing and running it,
see [README.md](README.md).

## Shape

One image, one process, three threads that matter:

```
browser ──▶ FastAPI (app/main.py) ──▶ SQLite job table (/data/yoink.db)
                                            │
                        quick worker ◀──────┴──────▶ bulk worker
                              │                          │
                              └── spotdl / yt-dlp ───────┘
                                          │
                                   /downloads/<job-id>/
```

There is no build step, no framework on the frontend, and no message broker.
`app/static/index.html` is a single file that polls `/api/jobs` every two
seconds and patches the DOM in place. Roughly 2,300 lines total.

## Request lifecycle

1. **Detect** — the pasted text is classified as a Spotify URL, a media URL
   (yt-dlp's 1,700+ sites), an Exportify CSV, or free-text search.
2. **Preflight** — anything that could be a playlist is looked up first, so
   the confirm sheet can show a real name, track count and estimated size.
   Single tracks, videos and searches skip this and create a job immediately;
   waiting on a lookup for something obviously small just feels slow.
   The whole endpoint is bounded by `YOINK_PREFLIGHT_TIMEOUT` (80 s default)
   so a slow upstream produces an honest error rather than a proxy 504.
3. **Queue** — a row lands in SQLite with a lane (below). Preflight results
   are handed to the job as a `.spotdl` file, so the metadata is fetched once.
4. **Run** — a worker claims the job with a conditional `UPDATE ... WHERE
   status='queued'`, so two workers can never take the same row. It then
   shells out to spotDL or yt-dlp, parsing progress from stdout.
5. **Finalize** — files are matched against the expected track list, numbered
   in playlist order, an `.m3u8` is written, and anything missing is recorded
   so the UI can offer *Retry missing* (a child job that re-runs just those,
   then re-finalizes the parent).

Statuses: `queued → running → done | done_with_errors | failed`, plus
`cancelled`, `retrying`, and `interrupted` for jobs that were mid-flight when
the process died (marked at startup, never silently resumed).

## Two lanes

Two worker threads, each running one job at a time, fed by the same table:

- **quick** — a single track, a single video, a search
- **bulk** — playlists, albums, artists, CSVs, YouTube lists

The lane is decided from the URL shape at submit time and inherited by retry
jobs. The point is that pasting one song while a 200-track playlist runs
gets you that song in seconds instead of half an hour.

Two alternatives were rejected: *N parallel workers* (two playlists at once
means eight concurrent connections to YouTube, and a third paste still
queues), and *preempting the playlist with SIGSTOP* (true priority, but
freezing yt-dlp mid-transfer breaks its connections).

## Engine quirks worked around

These are all upstream behaviours, discovered the hard way.

**spotDL ships a German YouTube Music client.** `spotdl` constructs
`YTMusic(language="de")`, and ytmusicapi's German parsing currently returns
*zero* results for every search — every match silently degrades to plain
YouTube, which is how you end up with a karaoke cover instead of the song.
The Dockerfile rewrites that literal to `"en"` and greps to confirm the patch
landed, so a spotDL upgrade that moves the line fails the build loudly rather
than quietly restoring the bug.

**spotDL exits 0 when tracks fail.** Job status is therefore judged by files
actually present on disk, never by the exit code.

**spotDL defaults to 128 kbps** regardless of source quality, so the bitrate
is always passed explicitly.

**yt-dlp needs a JavaScript runtime** for YouTube's player challenges. Without
one it falls back to limited clients and some videos become "unavailable"; the
image ships Deno (pinned) for this.

**Metadata lookups used to take ~10 s per track.** spotDL fetches Spotify
metadata through *spotapi*, which discovers Spotify's GraphQL query hashes by
downloading the entire web-player JavaScript bundle — about 75 files — every
time a client is constructed. spotDL constructs a fresh client for the track,
the artist *and* the album of every song, so one track cost ~230 HTTP
requests. `app/spotdl_patch.py` replaces `BaseClient.part_hash` with a
disk-cached name→hash map (24 h TTL, in the data volume), loaded by a launcher
shim that the image installs in place of the `spotdl` entry point. A 50-track
playlist now resolves in about a second and two requests. Worth reporting
upstream.

**Spotify throttles anonymous sessions**, and when it does, spotDL can return
a *partial* playlist with no error at all. Yoink compares the returned count
against the playlist's declared `list_length`, retries once, then refuses with
"Spotify only returned N of M tracks" rather than quietly downloading three
quarters of a playlist. Bursts of lookups trigger this; normal use doesn't.

## Sandboxing

The container runs untrusted-ish media tooling against the open internet, so
it is locked down harder than a typical app image:

- non-root (uid 10001), `cap_drop: ALL`, `no-new-privileges`
- read-only root filesystem; the only writable paths are the downloads mount,
  a named data volume, and a tmpfs `/tmp`
- one host folder mounted, nothing else
- `pids_limit`, capped container logs
- the port is published on loopback by default

`assert_public_url()` resolves link hostnames and rejects private, loopback
and link-local addresses before handing anything to yt-dlp, so a pasted URL
can't be used to probe the LAN it's running on (SSRF).

Nothing in the data volume needs backing up: it holds the job table, a
cover-art cache and the cookie secret, all regenerable.

## Authentication

`YOINK_PASSWORD` is a single shared password. On success the server sets an
`HttpOnly`, `SameSite=Lax` cookie containing
`HMAC-SHA256(secret, "yoink-auth-v1:" + password)`, and every `/api/*` request
is checked against it in constant time. The secret is 32 random bytes
generated once into the data volume. Login attempts are serialized behind a
lock with a progressive delay, capping brute force at roughly 12 guesses a
minute.

Be clear about the limits:

- **The session token is deterministic.** Every login on every device produces
  the same cookie, valid until the password changes. "Log out" only clears it
  from that browser; the token itself keeps working. Rotating the password is
  the only revocation.
- **The throttle is global, not per-IP.** Someone hammering the login endpoint
  slows down legitimate logins too.
- **There is one identity.** No users, no per-download ownership, no audit
  trail — anyone who gets in sees and controls everything.

This is adequate for a home LAN with a long passphrase. For anything reachable
from the internet, put an identity proxy in front and let it do the
authenticating (see the README). If Yoink ever needs to stand on its own,
the fix is random per-login session IDs stored server-side with expiry — about
thirty lines, and the point where per-user ownership would also become worth
adding.

## Updates

yt-dlp lives in its own Docker layer keyed on a `YTDLP_REFRESH` build arg, so
refreshing it rebuilds one layer in seconds instead of the whole image. The
consequence worth knowing: that layer stays cached across an ordinary
`docker compose build`, so only a build that changes the argument actually
pulls a new yt-dlp. `update.sh` exists so nobody has to remember that.

Nothing self-updates at runtime. The filesystem is read-only on purpose, and
an app that downloads and executes fresh code into itself is a worse failure
mode than a stale downloader — an in-container auto-updater was designed and
rejected on those grounds. What is automated is *detection*: a background
thread polls PyPI every six hours and the UI shows a banner when a newer
release exists, turning a silent breakage into a visible one.

## Known limitations

- The Docker healthcheck proves the web server is up, not that the workers are
  healthy. They restart themselves on error, so this is mostly cosmetic.
- Progress is parsed from tool stdout, which is not a stable interface; an
  upstream output change degrades the progress bar (downloads still work).
- Retries re-run whole tracks; there is no partial-file resume.
- Cancelling escalates SIGTERM to SIGKILL, which can leave a partial file in
  the job folder until retention sweeps it.

## Development

```bash
pip install -r requirements.txt
YOINK_DOWNLOAD_DIR=./downloads YOINK_DATA_DIR=./data uvicorn app.main:app --reload
```

You need `ffmpeg` and a JS runtime on `PATH` for downloads to work outside the
container. `app/main.py` is deliberately one file: the whole thing is small
enough to read in a sitting, and splitting it into packages would buy
indirection rather than clarity.
