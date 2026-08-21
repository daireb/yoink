# Reprise

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

Formats: MP3 320k (default), or M4A / Opus / FLAC at source quality.
Files land in `./downloads/<job>/` (configurable via `REPRISE_DOWNLOADS` in
`.env`) and are also downloadable through the UI, individually or as a zip.

## Configuration (.env or environment)

| Variable | Default | Purpose |
|---|---|---|
| `REPRISE_DOWNLOADS` | `./downloads` | host folder where audio lands |
| `REPRISE_PASSWORD` | *(unset = no auth)* | password for the web UI |
| `REPRISE_THREADS` | `4` | parallel downloads within a job |
| `REPRISE_SECRET` | auto-generated, persisted in `/data` | cookie-signing secret override |

## Exposing it (NAS / Cloudflare)

The compose file binds to `127.0.0.1` only. To serve your LAN or the world:

1. Change the port mapping to `"8080:8080"`.
2. Set `REPRISE_PASSWORD`.
3. Put Cloudflare Tunnel + Access (or Tailscale) in front. Don't port-forward raw.

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

## Known limitations (v1)

- The Docker healthcheck proves the web server is up, not that the download
  worker is healthy (the worker auto-restarts on errors, so this is cosmetic).
- No retention policy: jobs, logs, and audio accumulate until you remove them
  (by design — they're your files).
- The login throttle is a global progressive delay, not per-IP; real
  brute-force protection should come from Cloudflare Access / Tailscale when
  the port is opened.
