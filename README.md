# Yoink

A small self-hosted web app that turns a link into audio files. Paste a
Spotify or YouTube URL, get tagged MP3s back — from your phone, your laptop,
or anything else with a browser.

It is deliberately a **utility, not a music library**: no accounts to link, no
collection to curate, no sync. Files land in a folder you choose, stay there
for a week, and clean themselves up.

![Yoink](docs/screenshot.png)

## What it does

- **Spotify links** — track, album, playlist or artist. spotDL matches each
  song on YouTube Music and tags it with real Spotify metadata and cover art.
  No Spotify account or API key needed.
- **YouTube and 1,700+ other sites** — videos and playlists, via yt-dlp.
- **Private playlists and Liked Songs** — Spotify hides these from links, so
  Yoink accepts a CSV exported from [exportify.net](https://exportify.net).
- **Playlists come out ordered** — files numbered `01 - Artist - Title.mp3`
  with an `.m3u8` alongside, so dumb players (car USB sticks, sports
  headphones) play them in the right order.
- **Failures are visible** — songs that couldn't be found are listed, with a
  *Retry missing* button that re-fetches only those.

## Quick start

You need Docker on x86-64 or ARM64 — a NAS, a Raspberry Pi, a spare laptop.
Nothing is built on your machine and nothing needs configuring: downloads
live in a Docker volume and you fetch them through the UI.

```bash
docker run -d --name yoink -p 127.0.0.1:8080:8080 \
  -v yoink-data:/data -v yoink-downloads:/downloads ghcr.io/daireb/yoink
```

Or, better — the compose file adds the sandbox hardening and an optional
Cloudflare Tunnel, and is what the update script expects:

```bash
mkdir yoink && cd yoink
curl -fsSLO https://raw.githubusercontent.com/daireb/yoink/main/docker-compose.yml
docker compose up -d
```

Open **http://localhost:8080**.

The port is published on `127.0.0.1` only, so nothing outside the machine
can reach it — see *Putting it on the internet* for LAN or remote access.
To land files in a real folder instead of a volume, set `YOINK_DOWNLOADS`
(see Configuration).

## Configuration

Copy `.env.example` to `.env`; it documents every setting inline.

| Variable | Default | Purpose |
|---|---|---|
| `YOINK_IMAGE` | `ghcr.io/daireb/yoink:latest` | image to run; pin a date tag, or `yoink:local` for your own build |
| `YOINK_DOWNLOADS` | *(Docker volume)* | set a host folder to keep files on your machine (must be writable by uid 10001) |
| `YOINK_KEEP_DAYS` | `7` | days a finished download is kept |
| `YOINK_PASSWORD` | *(unset — no login)* | password for the web UI |
| `YOINK_SECRET` | auto-generated | cookie-signing secret override |
| `TUNNEL_TOKEN` | *(unset)* | Cloudflare Tunnel token for the optional `tunnel` profile |
| `YOINK_THREADS` | `4` | parallel downloads within one job |
| `YOINK_PREFLIGHT_TIMEOUT` | `80` | seconds a link lookup may take before giving up |
| `YOINK_UPDATE_CHECK` | `1` | check PyPI for yt-dlp releases and warn when this install has fallen behind |

Per-request defaults — 320/192/128 kbps, MP3/M4A/Opus/FLAC, track numbering —
live behind the gear icon in the UI and are saved in your browser.

## Putting it on the internet

Yoink has no multi-user model: everyone who reaches it sees the same
downloads and can start new ones. Pick one of these.

**Local only (default).** The port is bound to `127.0.0.1`. Nothing to do.

**Your LAN.** Change the port mapping in `docker-compose.yml` to
`"8080:8080"` and set `YOINK_PASSWORD` to a long passphrase. The app password
is a single shared secret with a global rate limit — fine for a home network,
not something to expose to the internet on its own.

**From anywhere — recommended: put an identity proxy in front.** Something
like [Cloudflare Tunnel + Access](https://developers.cloudflare.com/cloudflare-one/)
or [Tailscale](https://tailscale.com/) authenticates people *before* any
request reaches Yoink, and needs no ports opened on your router:

For Cloudflare, the connector is already in the compose file behind a
profile — no second setup:

1. Zero Trust → Networks → Tunnels → create a tunnel, copy its token into
   `.env` as `TUNNEL_TOKEN`, and add a public hostname pointing at
   `http://localhost:8080`.
2. Start with `docker compose --profile tunnel up -d`.
3. **Add the Access policy before you share the URL** (Zero Trust → Access →
   Applications → your hostname → allow your email). A tunnel alone publishes
   the app to the world with no login; the policy is the lock.

For Tailscale, `tailscale serve 8080` on the host does the whole job. Leave
the port loopback-bound and `YOINK_PASSWORD` unset in both cases.

Yoink trusts `X-Forwarded-Proto` from whatever fronts it, so its cookies get
the `Secure` flag automatically over HTTPS.

## Keeping it working

yt-dlp is the part that goes stale: YouTube changes something every few weeks
and older releases stop working. Yoink handles that upstream so you don't
have to — CI checks PyPI daily and, whenever yt-dlp has released, builds a
fresh image, smoke-tests it, and publishes it as `latest` (also tagged by date
and by yt-dlp version, so you can pin if a release ever misbehaves).

Your side is one command, run on a schedule:

```bash
./update.sh        # = docker compose pull && docker compose up -d, plus cleanup
```

Weekly is plenty; daily is fine. On a NAS, use its task scheduler to run the
script as the Docker user; elsewhere, cron:

```cron
0 4 * * *  /path/to/yoink/update.sh >> /var/log/yoink-update.log 2>&1
```

It prints one line (`current · 2026.08.24-3f1c9a2 · yt-dlp 2026.8.24` or
`updated → …`) so the log stays readable. A plain `docker compose restart`
updates nothing — yt-dlp is part of the image.

If the schedule ever breaks — token revoked, NAS offline, CI stuck — the UI
tells you: once this install's yt-dlp trails the newest release by more than
two weeks, a banner appears above the list with the command to run. During
normal operation you will never see it. Versions and the build id are in
**Options**.

### Running from source

```bash
docker build -t yoink:local .
echo YOINK_IMAGE=yoink:local >> .env
docker compose up -d
```

Rebuild to update; `--build-arg YTDLP_REFRESH=$(date +%s)` forces a fresh
yt-dlp without busting the rest of the cache.

## Audio quality, honestly

Spotify is only ever the *metadata* source. The audio comes from YouTube,
where the anonymous ceiling is a ~130 kbps Opus/AAC stream, so the 320 kbps
default is that stream transcoded with plenty of headroom rather than a
lossless rip. It sounds good on normal listening gear, and that trade is
what keeps the tool free of account logins and ban risk. If you want bit-
perfect copies, buy the files.

## Legal

This is a personal tool for getting copies of music you already have access
to — offline listening, a USB stick for the car, media you own. Downloading
from these services may conflict with their terms, and redistributing what
you download is copyright infringement in most places. What you do with it
is on you.

## How it works

Two containers' worth of machinery in one small image: a FastAPI app that
takes requests, and two worker threads that shell out to spotDL and yt-dlp.
Jobs run in two lanes — a single song never waits behind a 200-track playlist.

For the design, the engine quirks it works around, and the security model,
see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

```
app/main.py                  backend + workers (single file)
app/static/index.html        UI (single file, no build step)
app/spotdl_patch.py          metadata-lookup cache for spotDL
.github/workflows/image.yml  builds, tests and publishes the image
update.sh                    pull the newest image (run on the host)
```

## Credits

The heavy lifting is done by [spotDL](https://github.com/spotDL/spotify-downloader)
and [yt-dlp](https://github.com/yt-dlp/yt-dlp); Yoink is a web UI, a job
queue and some sharp edges filed off. Built with assistance from Claude.
