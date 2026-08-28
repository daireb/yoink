"""parse_spotdl_file ordering and the throttle-truncation guard."""
import json


def write(tmp_path, songs):
    p = tmp_path / "tracks.spotdl"
    p.write_text(json.dumps(songs), encoding="utf-8")
    return p


def song(name, **kw):
    return {"name": name, "artists": ["A"], "duration": 60, "url": f"https://open.spotify.com/track/{name}",
            "song_id": name, "cover_url": "", **kw}


def test_playlist_sorted_by_list_position(main, tmp_path):
    p = write(tmp_path, [song("b", list_position=2), song("c", list_position=3), song("a", list_position=1)])
    names = [t["name"] for t in main.parse_spotdl_file(p)]
    assert names == ["a", "b", "c"]
    assert [t["pos"] for t in main.parse_spotdl_file(p)] == [1, 2, 3]


def test_album_sorted_by_disc_then_track(main, tmp_path):
    p = write(tmp_path, [
        song("d2t1", disc_number=2, track_number=1),
        song("d1t2", disc_number=1, track_number=2),
        song("d1t1", disc_number=1, track_number=1),
    ])
    assert [t["name"] for t in main.parse_spotdl_file(p)] == ["d1t1", "d1t2", "d2t1"]


def test_csv_order_hint_by_url(main, tmp_path):
    p = write(tmp_path, [song("x"), song("y"), song("z")])
    hint = [f"https://open.spotify.com/track/{n}" for n in ("z", "x", "y")]
    assert [t["name"] for t in main.parse_spotdl_file(p, order_hint=hint)] == ["z", "x", "y"]


def test_no_sort_keys_preserves_order_and_numbers(main, tmp_path):
    p = write(tmp_path, [song("first"), song("second")])
    tracks = main.parse_spotdl_file(p)
    assert [t["name"] for t in tracks] == ["first", "second"]
    assert [t["pos"] for t in tracks] == [1, 2]
    assert "_disc" not in tracks[0] and "_track" not in tracks[0]


def test_truncation_guard(main, tmp_path):
    full = write(tmp_path, [song("a", list_length=2), song("b", list_length=2)])
    assert main.is_truncated(full) == (False, 2, 2)
    short = write(tmp_path, [song("a", list_length=50)])
    truncated, got, declared = main.is_truncated(short)
    assert truncated and got == 1 and declared == 50
    nolen = write(tmp_path, [song("a"), song("b")])
    assert main.is_truncated(nolen) == (False, 2, None)


def test_truncation_guard_garbage_file(main, tmp_path):
    p = tmp_path / "bad.spotdl"
    p.write_text("not json{", encoding="utf-8")
    assert main.is_truncated(p) == (True, 0, None)
    assert main.is_truncated(tmp_path / "missing.spotdl") == (True, 0, None)
