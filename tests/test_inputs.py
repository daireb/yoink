"""Request parsing, CSV import, and small helpers."""
import pytest
from fastapi import HTTPException


def test_parse_options_defaults_and_validation(main):
    assert main.parse_options({}) == ("mp3", 320, None)
    assert main.parse_options({"format": "opus", "bitrate": 128, "numbered": 1}) == ("opus", 128, True)
    for bad in ({"format": "wav"}, {"bitrate": 999}, {"bitrate": "high"}):
        with pytest.raises(HTTPException):
            main.parse_options(bad)


def test_clean_input(main):
    assert main.clean_input("  hello  ") == "hello"
    with pytest.raises(HTTPException):
        main.clean_input("   ")
    with pytest.raises(HTTPException):
        main.clean_input("--flag-injection")


def test_est_mb(main):
    tracks = [{"duration": 200}, {"duration": 100}, {"duration": None}]
    assert main.est_mb(tracks, 320) == 12.0  # 300s * 320kbps


def test_slugify(main):
    assert main.slugify("https://open.spotify.com/album/Bratté!") == "open-spotify-com-album-bratt"
    assert main.slugify("...") == "job"


CSV = """﻿Track URI,Track Name,Artist Name(s),Album Name,Duration (ms)
spotify:track:AAA111,Song One,Solo Artist,Album,201000
spotify:track:BBB222,Song Two,First;Second,Album,100500
spotify:local:whatever,Local File,Someone,Album,1000
spotify:track:###bad,Injection,Someone,Album,1000
"""


def test_parse_csv_exportify(main):
    urls, tracks = main.parse_csv(CSV.encode("utf-8"))
    assert urls == ["https://open.spotify.com/track/AAA111", "https://open.spotify.com/track/BBB222"]
    assert tracks[0]["name"] == "Song One" and tracks[0]["duration"] == 201
    assert tracks[1]["artists"] == ["First", "Second"]
    assert [t["pos"] for t in tracks] == [1, 2]  # local + malformed rows skipped without renumber gaps


def test_parse_csv_rejects_junk(main):
    with pytest.raises(HTTPException):
        main.parse_csv(b"just,some,columns\n1,2,3\n")
    with pytest.raises(HTTPException):
        main.parse_csv("Track URI\n".encode("utf-16"))


def test_safe_file_blocks_traversal(main, tmp_path):
    (tmp_path / "song.mp3").write_bytes(b"x")
    (tmp_path.parent / "outside.txt").write_bytes(b"secret")
    assert main.safe_file(tmp_path, "song.mp3").name == "song.mp3"
    with pytest.raises(HTTPException) as e:
        main.safe_file(tmp_path, "../outside.txt")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        main.safe_file(tmp_path, "nope.mp3")
    assert e.value.status_code == 404


def test_parse_options_accepts_mp4(main):
    assert main.parse_options({"format": "mp4"}) == ("mp4", 320, None)


def test_list_audio_files_includes_video(main, tmp_path):
    for n in ("a.mp3", "b.mp4", "c.webm", "notes.txt"):
        (tmp_path / n).write_bytes(b"")
    assert [p.name for p in main.list_audio_files(tmp_path)] == ["a.mp3", "b.mp4", "c.webm"]


def test_parse_vres(main):
    import pytest
    from fastapi import HTTPException
    assert main.parse_vres({}) is None
    assert main.parse_vres({"vres": "best"}) is None
    assert main.parse_vres({"vres": "1080"}) == "1080"
    with pytest.raises(HTTPException):
        main.parse_vres({"vres": "4000"})
