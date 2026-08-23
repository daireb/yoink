#!/bin/sh
# Bring this Yoink install up to date. Safe to run on a schedule (cron, NAS
# Task Scheduler); weekly is plenty, daily is fine. Prints one line.
#
# Published image (the default): pull the newest and restart if it changed.
# Images are rebuilt upstream whenever yt-dlp releases, so this is the whole
# update story. Local image (YOINK_IMAGE without a registry, e.g. yoink:local):
# rebuild from the source next to this script and switch only if yt-dlp moved.
set -eu
cd "$(dirname "$0")"

image=$(docker compose config --images | head -1)
cid=$(docker compose ps -q yoink 2>/dev/null || true)
running=$([ -n "$cid" ] && docker inspect -f '{{.Image}}' "$cid" 2>/dev/null || true)

ver='import os, importlib.metadata as m; print(os.environ.get("YOINK_BUILD","?"), "· yt-dlp", m.version("yt-dlp"))'
report() { docker compose exec -T yoink python -c "$ver" 2>/dev/null || echo "?"; }

case "$image" in
  */*)
    docker compose pull -q ;;
  *)
    [ -f Dockerfile ] || { echo "local image '$image' but no Dockerfile here — nothing to update from"; exit 1; }
    # Build aside; a forced-fresh yt-dlp layer always yields a new image id,
    # so only adopt the candidate if its yt-dlp version actually differs.
    docker build -q -t "$image.candidate" --build-arg "YTDLP_REFRESH=$(date +%s)" . >/dev/null
    if [ "$(report)" != "$(docker run --rm --entrypoint python "$image.candidate" -c "$ver" 2>/dev/null)" ]; then
      docker tag "$image.candidate" "$image"
    fi
    docker rmi "$image.candidate" >/dev/null 2>&1 || true ;;
esac
newest=$(docker image inspect -f '{{.Id}}' "$image")

if [ "$running" = "$newest" ]; then
  [ -n "$cid" ] && [ "$(docker inspect -f '{{.State.Running}}' "$cid")" = "true" ] || docker compose up -d >/dev/null 2>&1
  echo "current · $(report)"; exit 0
fi

# Don't restart under a running download; the new image waits for the next run.
busy=$(docker compose exec -T yoink python -c "import sqlite3; print(sqlite3.connect('/data/yoink.db').execute(\"select count(*) from jobs where status in ('running','retrying')\").fetchone()[0])" 2>/dev/null || echo 0)
if [ "${busy:-0}" -gt 0 ]; then
  echo "new image ready, but $busy download(s) running — restart deferred to next run"; exit 0
fi

docker compose up -d >/dev/null 2>&1
docker image prune -f >/dev/null 2>&1 || true
echo "updated → $(report)"
