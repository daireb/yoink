#!/bin/sh
# Rebuild Yoink with the newest yt-dlp and restart it.
#
# yt-dlp is baked into the image, so this is the only thing that updates it —
# `docker compose up -d --build` will NOT, because the layer is cached until
# YTDLP_REFRESH changes. Safe to run on a schedule; takes a few seconds when
# there's nothing new. See "Keeping it working" in the README.
set -e
cd "$(dirname "$0")"

# -q keeps a cron log to one line on success; failures still print to stderr.
YTDLP_REFRESH=$(date +%s) docker compose build -q
docker compose up -d >/dev/null

# Drop the now-untagged previous image so weekly rebuilds don't fill the disk.
# This only removes dangling (untagged, unreferenced) images.
docker image prune -f >/dev/null 2>&1 || true

docker compose exec -T yoink python3 -c \
  "import importlib.metadata as m; print('yt-dlp', m.version('yt-dlp'))" 2>/dev/null || true
