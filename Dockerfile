# syntax=docker/dockerfile:1
#
# Yoink image. Three stages: Python deps in a venv, static ffmpeg and Deno
# copied from their official multi-arch images, and a slim runtime that holds
# only what runs. Builds natively for linux/amd64 and linux/arm64.

ARG PYTHON_VERSION=3.12
ARG FFMPEG_VERSION=7.1.1
ARG DENO_VERSION=2.9.5

# ---- binaries -----------------------------------------------------------
# Static ffmpeg instead of Debian's: the apt package drags in ~400 MB of codec
# and GPU libraries that audio extraction never touches.
FROM mwader/static-ffmpeg:${FFMPEG_VERSION} AS ffmpeg
# Deno: the JS runtime yt-dlp uses for YouTube's player challenges. Without
# one, yt-dlp falls back to limited clients and some videos become unavailable.
FROM denoland/deno:bin-${DENO_VERSION} AS deno

# ---- python dependencies ------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# yt-dlp is the one dependency that must stay fresh (YouTube changes break
# old releases within weeks). Scheduled CI builds pass a new YTDLP_REFRESH so
# only this layer is redone; everything above stays cached.
ARG YTDLP_REFRESH=0
RUN echo "yt-dlp refresh ${YTDLP_REFRESH}" && pip install --upgrade yt-dlp

# spotdl 4.5.2 hardcodes a German-locale YTMusic client; ytmusicapi's "de"
# parsing currently returns ZERO results for every search, so all Spotify
# matching silently degrades to plain-YouTube fallback (worse matches).
# Empirically: language="de" -> 0 results, language="en" -> 60 results for the
# same query from the same IP. The grep makes the build fail loudly if a
# spotdl upgrade moves this line, so it gets re-evaluated rather than lost.
RUN f=/opt/venv/lib/python*/site-packages/spotdl/providers/audio/ytmusic.py \
    && sed -i 's/YTMusic(language="de")/YTMusic(language="en")/' $f \
    && grep -q 'YTMusic(language="en")' $f

# Trim what the runtime never imports: pip itself and packaged test suites.
RUN rm -rf /opt/venv/lib/python*/site-packages/pip* \
    && find /opt/venv/lib -type d -name tests -prune -exec rm -rf {} +

# ---- runtime ------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.source="https://github.com/daireb/yoink" \
      org.opencontainers.image.description="Self-hosted Spotify/YouTube → MP3 portal" \
      org.opencontainers.image.title="Yoink"

# tini: PID 1 signal handling and zombie reaping for the ffmpeg/yt-dlp
# subprocesses. It is the only apt package the runtime needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/* \
    # nothing installs Python packages at runtime; drop the system pip too
    && rm -rf /usr/local/lib/python3*/site-packages/pip* /usr/local/lib/python3*/ensurepip

# Both binaries. ffprobe is a second 99 MB copy of libav and most of yt-dlp
# copes without it, but FFmpegMetadataPP._fixup_chapters hard-requires it
# whenever a site reports chapters without end times — a silent job failure
# is not worth the saving.
COPY --from=ffmpeg /ffmpeg /ffprobe /usr/local/bin/
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=deps /opt/venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /srv
COPY app /srv/app

# Route the `spotdl` CLI through the spotapi hash-cache patch (see
# app/spotdl_patch.py): one Spotify lookup drops from ~10 s to ~1.5 s and a
# 50-track playlist from minutes to ~1 s. Fails loudly if spotdl's entry
# point ever moves, rather than silently running unpatched.
RUN python -c "from spotdl import console_entry_point" \
    && printf '%s\n' '#!/opt/venv/bin/python' 'import sys' 'sys.path.insert(0, "/srv")' \
       'import app.spotdl_patch  # noqa: F401  (spotapi hash cache)' \
       'from spotdl import console_entry_point' \
       'sys.exit(console_entry_point())' > /opt/venv/bin/spotdl \
    && chmod +x /opt/venv/bin/spotdl \
    && ffmpeg -version | head -1 && deno --version | head -1 && yt-dlp --version

# Build identity, shown in the UI so a user can tell which image they run.
ARG YOINK_BUILD=dev
ENV YOINK_BUILD=${YOINK_BUILD}

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
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# --proxy-headers: trust X-Forwarded-Proto/For from whatever fronts the
# container (cloudflared, a NAS reverse proxy) so cookies get the Secure flag
# on HTTPS. Nothing security-relevant keys off the client IP, so allowing any
# proxy source is harmless.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
