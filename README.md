# Yoink

Self-hosted music download portal. Paste a Spotify or YouTube link in a web UI,
get tagged audio files back — from any device on your network (or, behind
auth, anywhere).

Spotify tracks are matched on YouTube Music / YouTube by
[spotDL](https://github.com/spotDL/spotify-downloader) and tagged with real
Spotify metadata + cover art. Everything else (YouTube videos & playlists,
SoundCloud, most media sites) goes through
[yt-dlp](https://github.com/yt-dlp/yt-dlp).

## Run it

```bash
docker compose up -d --build
```

Open **http://localhost:8080**.

## What it accepts

| Input | Engine | Notes |
|---|---|---|
| Spotify track / album / playlist / artist URL | spotdl | no Spotify account or API keys needed |
| Exportify CSV (drag & drop) | spotdl | works for **private** playlists — uses the `Track URI` column |
| YouTube video / playlist URL (and most media sites) | yt-dlp | |
| Free text ("artist song") | spotdl | resolved via Spotify search |

Paste a link → it looks the link up → for anything more than one song you get
a confirm sheet (name, count, size) → Request. Playlists come back numbered
`01 - Artist - Title.mp3` so simple players (swim headphones, car USB) keep the
order, with an `.m3u8` alongside. Tracks that couldn't be found are listed with
a **Retry missing** button that fetches only those.

Defaults (quality 320/192/128 kbps, format MP3 / M4A / Opus / FLAC, numbering)
live behind the gear icon and are saved in your browser.

Files land in `./downloads/<job>/` (configurable via `YOINK_DOWNLOADS` in
`.env`) and are downloadable through the UI — one click for the whole batch,
or per file from the details view. Everything is kept for `YOINK_KEEP_DAYS`
(default 7) and then removed.

## Configuration (.env or environment)

| Variable | Default | Purpose |
|---|---|---|
| `YOINK_DOWNLOADS` | `./downloads` | host folder where audio lands |
| `YOINK_PASSWORD` | *(unset = no auth)* | password for the web UI |
| `YOINK_THREADS` | `4` | parallel downloads within a job |
| `YOINK_KEEP_DAYS` | `7` | how long finished downloads are kept |
| `YOINK_SECRET` | auto-generated, persisted in `/data` | cookie-signing secret override |
| `YOINK_PREFLIGHT_TIMEOUT` | `80` | seconds a link lookup may take before giving up (keep under your proxy's origin timeout) |
| `YOINK_UPDATE_CHECK` | `1` | poll PyPI every 6 h for a newer yt-dlp and show a rebuild banner; `0` disables |

## Deploying on a NAS (Cloudflare Tunnel + Access)

The shape: Yoink listens on `127.0.0.1:8080` on the NAS, `cloudflared` on the
same box forwards a hostname to it, and a Cloudflare Access policy on that
hostname demands your Google account (or an emailed code) before any request
reaches the app. Nothing is port-forwarded and the app itself needs no
password — the tunnel is the only way in, and Access is the lock on it.

1. **Clone and configure**
   ```bash
   git clone <your private repo> yoink && cd yoink
   printf 'YOINK_DOWNLOADS=/volume1/music/Yoink\n' > .env   # wherever downloads should land
   mkdir -p /volume1/music/Yoink && chown 10001:10001 /volume1/music/Yoink
   ```
   The container runs as uid 10001 and can only write to that folder and its
   own data volume. Leave `YOINK_PASSWORD` unset.

2. **Build and start** — the first build fetches ffmpeg, deno and Python
   packages (a few minutes); later rebuilds are seconds.
   ```bash
   docker compose up -d --build
   curl -s localhost:8080/api/health     # {"ok":true,...}
   ```

3. **Tunnel** — in Cloudflare Zero Trust → Networks → Tunnels, create a tunnel,
   run the `cloudflared` connector on the NAS (their Docker one-liner is
   fine), and add a public hostname, e.g. `yoink.example.com` →
   `http://localhost:8080`. If `cloudflared` runs as a container, use
   `network_mode: host` for it or put both on one Docker network and point
   it at `http://yoink:8080` instead of localhost.

4. **Access** — Zero Trust → Access → Applications → add a self-hosted app
   for that hostname with a policy allowing your email (one-time PIN) or your
   Google account. Until this exists the hostname is open to the world — do
   this before sharing the URL anywhere.

5. **Daily refresh** — a scheduled task on the NAS:
   ```bash
   cd /path/to/yoink && YTDLP_REFRESH=$(date +%s) docker compose build && docker compose up -d
   ```
   See *Keeping it working* below for why.

`YOINK_THREADS` is 4 by default; with two lanes that can mean eight ffmpeg
processes at once. Set it to 2 in `.env` on a modest NAS CPU.

**If you also want it reachable by LAN IP** (without Cloudflare), change the
port binding to `"8080:8080"` and set `YOINK_PASSWORD` — on that path the
app password is the only lock. uvicorn trusts proxy headers, so behind HTTPS
the session and download cookies get the `Secure` flag automatically; on
plain LAN HTTP they still work. Link lookups are capped at
`YOINK_PREFLIGHT_TIMEOUT` (80 s) so a slow Spotify day produces an honest
"try again" instead of a Cloudflare 524.

The container is sandboxed regardless: non-root, all capabilities dropped,
`no-new-privileges`, read-only root filesystem, tmpfs `/tmp`. Nothing in
the data volume needs backing up — it holds the job list, a cover-art cache
and the cookie secret, all regenerable.

## Keeping it working

yt-dlp is what breaks. YouTube changes something every few weeks and old
yt-dlp releases stop working; the container's read-only filesystem means it
can't update itself. The fix is a rebuild, and the Dockerfile is arranged so
only the yt-dlp layer is redone:

```bash
YTDLP_REFRESH=$(date +%s) docker compose build && docker compose up -d
```

Yoink checks PyPI every 6 hours; when a newer yt-dlp exists, a banner above
the list says so and clicking it shows that command. A plain restart doesn't
update yt-dlp — it's part of the image. **Options** shows the current versions.

Container logs are capped (3 × 10 MB) in the compose file.

The container is sandboxed regardless: non-root, all capabilities dropped,
`no-new-privileges`, read-only root filesystem, tmpfs `/tmp`.

## Quality, honestly

Spotify is only ever the *metadata* source — audio comes from YouTube (Music),
whose anonymous ceiling is a ~130 kbps Opus/AAC stream. The MP3 320k default
transcodes that with maximum headroom; it sounds good, but it is not a lossless
rip. That trade is what keeps this tool free of account logins and ban risk.

## Notes

- The job queue is SQLite-backed and survives restarts; jobs interrupted
  mid-download are marked `interrupted` and can be re-submitted.
- spotdl exits 0 even when tracks fail, so job status is judged by files
  actually produced (`done_with_errors` = some tracks missing; the log names
  the failures).
- The image patches spotdl's YouTube Music client from German to English
  locale — ytmusicapi's "de" parsing currently returns zero results, which
  silently degrades every match to plain-YouTube fallback. The build fails
  loudly if a spotdl upgrade moves that line (see Dockerfile).
- yt-dlp needs a JS runtime for YouTube's player challenges — the image ships
  deno. If YouTube downloads start failing months from now, rebuild the image
  to pick up a fresh yt-dlp: `docker compose build --no-cache && docker compose up -d`.

## Development

```
app/main.py           FastAPI backend + worker (single file)
app/static/index.html UI (single file, no build step)
```

Built with assistance from Claude. Engine credit: spotDL and yt-dlp do the
actual heavy lifting.

## Two lanes

Jobs run in two lanes, one job at a time each: **quick** (a single track,
video or search) and **bulk** (playlists, albums, artists, CSVs, YouTube
lists). A pasted song therefore never waits behind a playlist, while there
are never more than two download processes talking to YouTube.

## Why lookups are fast (and a note on Spotify rate limits)

spotdl fetches Spotify metadata through **spotapi**, which discovers Spotify's
GraphQL query hashes by downloading the entire web-player JavaScript bundle
(~75 files) — per client, and spotdl makes a fresh client for the track, the
artist and the album of every song. That's ~230 requests and ~10 s for one
track, and minutes for a playlist. `app/spotdl_patch.py` (loaded by the
`spotdl` launcher baked into the image) caches the harvested hashes in
`/data/home/.cache/yoink/spotapi-hashes.json` for a day: one track ≈ 1.5 s,
a 50-track playlist ≈ 1 s, and ~100× fewer requests to Spotify. Override the
cache path with `YOINK_HASH_CACHE`.

Spotify still throttles the anonymous session page after bursts of lookups.
When that happens spotdl can return a *partial* playlist without an error;
Yoink compares the returned count against the playlist's declared length and
retries once, then refuses with "Spotify only returned N of M tracks" rather
than quietly downloading a truncated playlist. Wait a minute and try again.

## Known limitations (v1)

- The Docker healthcheck proves the web server is up, not that the download
  worker is healthy (the worker auto-restarts on errors, so this is cosmetic).
- No retention policy: jobs, logs, and audio accumulate until you remove them
  (by design — they're your files).
- The login throttle is a global progressive delay, not per-IP, and sessions
  are only revoked by changing the password; real brute-force protection should
  come from Cloudflare Access / Tailscale when the port is opened.
