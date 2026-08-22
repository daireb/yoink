"""Runtime patch for spotapi (used by spotdl for Spotify metadata).

spotapi addresses Spotify's GraphQL operations by persisted-query hash, and
discovers those hashes by downloading Spotify's whole web-player JS bundle
(~75 files) and string-searching it — once per client instance. spotdl uses a
fresh client for the track, the artist and the album of every song, so a single
lookup costs ~230 requests and ~10 s; playlists take minutes.

This replaces BaseClient.part_hash with a process-wide, disk-persisted
name -> hash map. The bundle is downloaded once (or once a day, or when a name
is missing), then every lookup is a dict read. Measured: single track 10 s ->
1.5 s, 50-track playlist ~90 s -> ~1.2 s. Metadata is unchanged.
"""

import json
import os
import re
import threading
import time

import spotapi.client as _client

CACHE_PATH = os.environ.get(
    "YOINK_HASH_CACHE",
    os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "yoink", "spotapi-hashes.json"),
)
MAX_AGE = 24 * 3600  # Spotify redeploys the web player regularly; refresh daily
_PAIR = re.compile(r'"([A-Za-z0-9_]+)","(?:query|mutation)","([0-9a-f]{64})"')
_lock = threading.Lock()
_state = {"map": None, "ts": 0.0}
_orig_get_sha256_hash = _client.BaseClient.get_sha256_hash


def _load():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("map"), dict):
            _state["map"], _state["ts"] = d["map"], float(d.get("ts") or 0)
            return
    except (OSError, ValueError):
        pass
    _state["map"], _state["ts"] = {}, 0.0


def _save():
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"map": _state["map"], "ts": _state["ts"]}, f)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass  # cache is an optimisation; never fail a lookup over it


def _refresh(client) -> None:
    """Download the bundle once (the original, expensive path) and harvest
    every operation hash from it."""
    _orig_get_sha256_hash(client)
    pairs = dict(_PAIR.findall(str(client.raw_hashes)))
    if pairs:
        _state["map"] = {**(_state["map"] or {}), **pairs}
        _state["ts"] = time.time()
        _save()


def part_hash(self, name: str) -> str:
    with _lock:
        if _state["map"] is None:
            _load()
        stale = time.time() - _state["ts"] > MAX_AGE
        h = None if stale else _state["map"].get(name)
        if h:
            return h
        _refresh(self)
        h = _state["map"].get(name)
        if not h:
            raise ValueError(f"Could not find GraphQL hash for {name!r}")
        return h


_client.BaseClient.part_hash = part_hash
