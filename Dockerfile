FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg: audio extraction/transcode; tini: signal handling; curl: healthcheck
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

# Deno: the JS runtime yt-dlp uses to solve YouTube's JS challenges. Without
# one, yt-dlp falls back to limited clients and some videos become unavailable.
# Pinned so builds are reproducible; bump deliberately.
ARG DENO_VERSION=2.9.5
RUN arch=$(uname -m) \
    && case "$arch" in \
         aarch64) target=aarch64-unknown-linux-gnu ;; \
         x86_64)  target=x86_64-unknown-linux-gnu ;; \
       esac \
    && curl -fsSL -o /tmp/deno.zip "https://github.com/denoland/deno/releases/download/v${DENO_VERSION}/deno-${target}.zip" \
    && unzip -q /tmp/deno.zip -d /usr/local/bin \
    && rm /tmp/deno.zip \
    && deno --version

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# yt-dlp goes stale fast (YouTube changes break old releases within weeks) and
# the read-only rootfs means it can't self-update. This layer is the only one
# that needs refreshing: build with a new YTDLP_REFRESH value to bust just it.
#   YTDLP_REFRESH=$(date +%s) docker compose build && docker compose up -d
ARG YTDLP_REFRESH=0
RUN echo "yt-dlp refresh ${YTDLP_REFRESH}" && pip install --no-cache-dir --upgrade yt-dlp

# spotdl 4.5.2 hardcodes a German-locale YTMusic client; ytmusicapi 1.12.2's
# "de" parsing currently returns ZERO results for every search, so all Spotify
# matching silently degrades to plain-YouTube fallback (worse matches).
# Empirically: language="de" -> 0 results, language="en" -> 60 results for the
# same query from the same IP. Patch to "en"; the grep makes the build fail
# loudly if a spotdl upgrade changes this line so we can re-evaluate.
RUN sed -i 's/YTMusic(language="de")/YTMusic(language="en")/' \
        /usr/local/lib/python3.12/site-packages/spotdl/providers/audio/ytmusic.py \
    && grep -q 'YTMusic(language="en")' \
        /usr/local/lib/python3.12/site-packages/spotdl/providers/audio/ytmusic.py

COPY app /srv/app

# Route the `spotdl` CLI through our spotapi hash-cache patch (see
# app/spotdl_patch.py): one Spotify lookup drops from ~10 s to ~1.5 s and a
# 50-track playlist from minutes to ~1 s. Fails loudly if spotdl's entry
# point ever moves, rather than silently running unpatched.
RUN python3 -c "from spotdl import console_entry_point" \
    && printf '%s\n' '#!/usr/local/bin/python3' 'import sys' 'sys.path.insert(0, "/srv")' \
       'import app.spotdl_patch  # noqa: F401  (spotapi hash cache)' \
       'from spotdl import console_entry_point' \
       'sys.exit(console_entry_point())' > /usr/local/bin/spotdl \
    && chmod +x /usr/local/bin/spotdl

# Non-root; HOME lives on the /data volume so spotdl/yt-dlp caches persist.
# /data and /downloads are created here so first-run named volumes inherit
# appuser ownership instead of root.
RUN useradd -u 10001 -M appuser \
    && mkdir -p /data/home /downloads \
    && chown -R appuser:appuser /data /downloads
USER appuser
ENV HOME=/data/home \
    XDG_CACHE_HOME=/data/home/.cache

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# --proxy-headers: trust X-Forwarded-Proto/For from whatever fronts the
# container (cloudflared, a NAS reverse proxy) so cookies get the Secure flag
# on HTTPS. Nothing security-relevant keys off the client IP, so allowing any
# proxy source is harmless.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
