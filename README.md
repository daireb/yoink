# Yoink

A small self-hosted web app that turns a link into audio files. Paste a
Spotify or YouTube URL, get tagged MP3s back — from your phone, your laptop,
or anything else with a browser.

It is deliberately a **utility, not a music library**: no accounts to link, no
collection to curate, no sync. Files land in a folder you choose, stay there
for a week, and clean themselves up.

```
┌──────────────┐     ┌─────────┐     ┌──────────────────────┐
│ paste a link │ ──▶ │  Yoink  │ ──▶ │ 01 - Artist - Title  │
└──────────────┘     └─────────┘     │ 02 - Artist - Title  │
                                     │ playlist.m3u8        │
                                     └──────────────────────┘
```

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

You need Docker with Compose v2. Works on x86-64 and ARM64.

```bash
git clone https://github.com/daireb/yoink && cd yoink
cp .env.example .env          # optional: pick where downloads land
docker compose up -d --build
```

Open **http://localhost:8080**. The first build takes a few minutes (ffmpeg,
Deno and Python packages); later ones are seconds.

By default the port is published on `127.0.0.1` only, so nothing outside the
machine can reach it — see below before changing that.

## Configuration

Copy `.env.example` to `.env`; it documents every setting inline.

| Variable | Default | Purpose |
|---|---|---|
| `YOINK_DOWNLOADS` | `./downloads` | host folder where audio lands (must be writable by uid 10001) |
| `YOINK_KEEP_DAYS` | `7` | days a finished download is kept |
| `YOINK_PASSWORD` | *(unset — no login)* | password for the web UI |
| `YOINK_SECRET` | auto-generated | cookie-signing secret override |
| `YOINK_THREADS` | `4` | parallel downloads within one job |
| `YOINK_PREFLIGHT_TIMEOUT` | `80` | seconds a link lookup may take before giving up |
| `YOINK_UPDATE_CHECK` | `1` | poll PyPI for newer yt-dlp and show a rebuild banner |

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

1. Leave the port bound to `127.0.0.1` and leave `YOINK_PASSWORD` unset.
2. Run the tunnel daemon on the same host and point your hostname at
   `http://localhost:8080`. (If the daemon is itself a container, it can't see
   the host's loopback — give it `network_mode: host`, or put both on one
   Docker network and target `http://yoink:8080`.)
3. **Add the access policy before you share the URL.** A tunnel alone
   publishes the app to the world with no login; the policy is the lock.

Yoink trusts `X-Forwarded-Proto` from whatever fronts it, so its cookies get
the `Secure` flag automatically over HTTPS.

## Keeping it working

yt-dlp is the part that goes stale: YouTube changes something every few weeks
and older releases stop working. The container has a read-only filesystem and
deliberately can't update itself, so the fix is a rebuild — the Dockerfile is
arranged so only the yt-dlp layer is redone and it takes seconds:

```bash
YTDLP_REFRESH=$(date +%s) docker compose build && docker compose up -d
```

Yoink checks PyPI every six hours and shows a banner when a newer yt-dlp is
out; clicking it repeats that command. Running it weekly on a schedule means
you'll never see the banner. Note that a plain `restart` does **not** update
anything — yt-dlp is baked into the image.

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
app/main.py            backend + workers (single file)
app/static/index.html  UI (single file, no build step)
app/spotdl_patch.py    metadata-lookup cache for spotDL
```

## Credits

The heavy lifting is done by [spotDL](https://github.com/spotDL/spotify-downloader)
and [yt-dlp](https://github.com/yt-dlp/yt-dlp); Yoink is a web UI, a job
queue and some sharp edges filed off. Built with assistance from Claude.
