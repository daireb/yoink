"""HTTP surface via TestClient: auth gate, login flow, health, static shell."""


def test_health_is_open_and_reports_versions(client):
    d = client.get("/api/health").json()
    assert d["ok"] is True and d["auth_required"] is True
    assert set(d["versions"]) == {"yt_dlp", "spotdl", "deno"}


def test_api_requires_auth(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/preflight", json={"input": "x"}).status_code == 401


def test_wrong_password_rejected(client):
    assert client.post("/api/login", json={"password": "nope"}).status_code == 403


def test_login_sets_cookie_and_unlocks(logged_in, main):
    assert logged_in.cookies.get(main.COOKIE)
    d = logged_in.get("/api/jobs").json()
    assert isinstance(d["jobs"], list)
    assert d["keep_days"] == 7.0
    assert set(d["update"]) >= {"current", "latest", "behind_days", "stale", "build"}


def test_index_serves_stamped_shell(client):
    html = client.get("/").text
    assert "<title>Yoink</title>" in html
    assert "?v=" in html  # cache-busting stamp applied


def test_manifest_and_sw(client):
    assert client.get("/manifest.webmanifest").status_code == 200
    assert "addEventListener" in client.get("/sw.js").text


def test_video_rejected_for_spotify_and_csv(logged_in):
    r = logged_in.post("/api/jobs", json={"input": "https://open.spotify.com/track/AAA", "format": "mp4"})
    assert r.status_code == 400 and "video" in r.json()["detail"]
    r = logged_in.post("/api/jobs/csv?format=mp4", files={"file": ("x.csv", b"Track URI\nspotify:track:AAA1\n")})
    assert r.status_code == 400
