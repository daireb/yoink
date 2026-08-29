"""File-to-track matching and finalize numbering — the swim-headphones contract."""


def exp(pos, name, artists=("Artist",)):
    return {"pos": pos, "name": name, "artists": list(artists), "duration": 60, "url": "", "id": ""}


def touch(d, name):
    p = d / name
    p.write_bytes(b"")
    return p


def test_exact_and_prefixed_names_match(main, tmp_path):
    files = [touch(tmp_path, "Artist - One.mp3"), touch(tmp_path, "02 - Artist - Two.mp3")]
    matched, missing = main.match_files([exp(1, "One"), exp(2, "Two")], files)
    assert missing == []
    assert matched[1].name == "Artist - One.mp3"
    assert matched[2].name == "02 - Artist - Two.mp3"


def test_unicode_and_punctuation_normalisation(main, tmp_path):
    files = [touch(tmp_path, "Beyoncé - Déjà Vu (feat. Jay-Z).mp3")]
    matched, missing = main.match_files([exp(1, "Deja Vu feat Jay Z", artists=("Beyonce",))], files)
    assert missing == [] and matched[1] == files[0]


def test_duplicate_title_is_not_reported_missing(main, tmp_path):
    files = [touch(tmp_path, "Artist - Same.mp3")]
    matched, missing = main.match_files([exp(1, "Same"), exp(2, "Same")], files)
    assert matched == {1: files[0]}
    assert missing == []


def test_fuzzy_second_pass_catches_title_drift(main, tmp_path):
    files = [touch(tmp_path, "Artist - Speed Drive (From Barbie Album).mp3")]
    matched, missing = main.match_files([exp(1, "Speed Drive (From Barbie The Album)")], files)
    assert missing == [] and matched[1] == files[0]


def test_genuinely_missing_track(main, tmp_path):
    files = [touch(tmp_path, "Artist - Here.mp3")]
    matched, missing = main.match_files([exp(1, "Here"), exp(2, "Completely Different Song")], files)
    assert [t["name"] for t in missing] == ["Completely Different Song"]


def test_finalize_numbers_without_holes_and_writes_m3u(main, tmp_path):
    for n in ("Artist - A.mp3", "Artist - C.mp3"):
        touch(tmp_path, n)
    expected = [exp(1, "A"), exp(2, "B (never downloaded)"), exp(3, "C")]
    missing = main.finalize_job_dir("j1", expected, numbered=True, job_dir=tmp_path)
    names = sorted(p.name for p in tmp_path.glob("*.mp3"))
    assert names == ["01 - Artist - A.mp3", "02 - Artist - C.mp3"]  # no hole at 02
    assert [t["name"] for t in missing] == ["B (never downloaded)"]
    m3u = (tmp_path / "playlist.m3u8").read_text()
    assert m3u.splitlines() == ["#EXTM3U", "01 - Artist - A.mp3", "02 - Artist - C.mp3"]


def test_finalize_is_idempotent(main, tmp_path):
    touch(tmp_path, "Artist - A.mp3")
    touch(tmp_path, "Artist - B.mp3")
    expected = [exp(1, "A"), exp(2, "B")]
    main.finalize_job_dir("j1", expected, numbered=True, job_dir=tmp_path)
    main.finalize_job_dir("j1", expected, numbered=True, job_dir=tmp_path)  # retry path re-finalizes
    assert sorted(p.name for p in tmp_path.glob("*.mp3")) == ["01 - Artist - A.mp3", "02 - Artist - B.mp3"]


def test_finalize_unnumbered_single_writes_no_m3u(main, tmp_path):
    touch(tmp_path, "Artist - Solo.mp3")
    main.finalize_job_dir("j1", [exp(1, "Solo")], numbered=False, job_dir=tmp_path)
    assert (tmp_path / "Artist - Solo.mp3").exists()
    assert not (tmp_path / "playlist.m3u8").exists()


def test_wide_numbering_for_100_plus(main, tmp_path):
    expected = [exp(i, f"T{i:03d}") for i in range(1, 101)]
    for i in range(1, 101):
        touch(tmp_path, f"Artist - T{i:03d}.mp3")
    main.finalize_job_dir("j1", expected, numbered=True, job_dir=tmp_path)
    assert (tmp_path / "001 - Artist - T001.mp3").exists()
    assert (tmp_path / "100 - Artist - T100.mp3").exists()


def test_media_duration_graceful_on_garbage(main, tmp_path):
    p = tmp_path / "x.mp4"
    p.write_bytes(b"not a real mp4")
    assert main.media_duration(p) is None
    assert main.media_duration(tmp_path / "missing.mp4") is None


def test_duration_recorded_for_a_single_audio_file(main, tmp_path, monkeypatch):
    """The artwork chip shows length for songs too, not just video."""
    (tmp_path / "Artist - Solo.mp3").write_bytes(b"")
    seen = {}
    monkeypatch.setattr(main, "media_duration", lambda p: 214.0)
    monkeypatch.setattr(main, "update_job", lambda jid, **kw: seen.update(kw))
    main.finalize_job_dir("j1", [exp(1, "Solo")], numbered=False, job_dir=tmp_path)
    assert seen.get("duration") == 214.0


def test_duration_not_recorded_for_a_batch(main, tmp_path, monkeypatch):
    for n in ("Artist - A.mp3", "Artist - B.mp3"):
        (tmp_path / n).write_bytes(b"")
    seen = {}
    monkeypatch.setattr(main, "media_duration", lambda p: 100.0)
    monkeypatch.setattr(main, "update_job", lambda jid, **kw: seen.update(kw))
    main.finalize_job_dir("j1", [exp(1, "A"), exp(2, "B")], numbered=True, job_dir=tmp_path)
    assert "duration" not in seen
