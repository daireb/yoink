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

## Exposing it (NAS / Cloudflare)

The compose file binds to `127.0.0.1` only. To serve your LAN or the world:

1. Change the port mapping to `"8080:8080"`.
2. Set `YOINK_PASSWORD`.
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
