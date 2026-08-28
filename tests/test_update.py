"""Staleness logic behind the update banner."""


def test_version_date(main):
    import datetime
    assert main.version_date("2026.8.19") == datetime.date(2026, 8, 19)
    assert main.version_date("2026.08.19") == datetime.date(2026, 8, 19)
    assert main.version_date("4.5.2") is None  # spotdl-style versions have no date
    assert main.version_date(None) is None
    assert main.version_date("2026.13.99") is None


def test_update_status_stale_threshold(main, monkeypatch):
    monkeypatch.setitem(main.TOOL_VERSIONS, "yt_dlp", "2026.8.1")
    monkeypatch.setitem(main.UPDATE, "latest", "2026.8.10")
    s = main.update_status()
    assert s["behind_days"] == 9 and s["stale"] is False  # within the pipeline's normal lag
    monkeypatch.setitem(main.UPDATE, "latest", "2026.9.1")
    s = main.update_status()
    assert s["behind_days"] == 31 and s["stale"] is True
    monkeypatch.setitem(main.UPDATE, "latest", None)
    s = main.update_status()
    assert s["behind_days"] is None and s["stale"] is False


def test_update_status_never_negative(main, monkeypatch):
    monkeypatch.setitem(main.TOOL_VERSIONS, "yt_dlp", "2026.9.1")
    monkeypatch.setitem(main.UPDATE, "latest", "2026.8.1")
    assert main.update_status()["behind_days"] == 0
