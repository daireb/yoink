"""Progress parsing from recorded spotdl / yt-dlp output lines."""


def feed(main, kind, lines, state=None):
    state = state if state is not None else {}
    for line in lines:
        main.parse_progress(kind, line, state)
    return state


def test_spotdl_complete_counter(main):
    state = feed(main, "spotify", ["1/10 complete", "3/10 complete", "2/10 complete"])
    assert state["done"] == 2 and state["total"] == 10  # last line wins; total is sticky max


def test_spotdl_current_track_set_and_cleared(main):
    state = feed(main, "spotify", ["Charli xcx - 360: Downloading"])
    assert state["current"] == "Charli xcx - 360"
    feed(main, "spotify", ["Charli xcx - 360: Done"], state)
    assert state["current"] is None


def test_spotdl_other_songs_done_does_not_clear_current(main):
    state = feed(main, "spotify", ["A - One: Downloading", "B - Two: Done"])
    assert state["current"] == "A - One"


def test_ytdlp_sequence(main):
    lines = [
        "[download] Downloading item 1 of 8",
        "[download] Destination: /downloads/x/First Song.webm",
        "[download] 100% of   3.50MiB in 00:00:01 at 2.91MiB/s",
        "[ExtractAudio] Destination: /downloads/x/First Song.mp3",
        "[download] Downloading item 2 of 8",
        "[download] Destination: /downloads/x/Second Song.webm",
    ]
    state = feed(main, "media", lines)
    assert state["total"] == 8
    assert state["done"] == 1
    assert state["current"] == "Second Song"


def test_ytdlp_already_downloaded_fills_the_stream(main):
    state = feed(main, "media", ["[download] Big Buck Bunny.webm has already been downloaded"])
    assert state["progress"] == 1.0


def test_ytdlp_byte_progress_single_video(main):
    state = feed(main, "media", ["yoink-prog  10.0%", "yoink-prog  52.4%"], {"done": 0, "total": 1})
    assert state["progress"] == 0.524


def test_ytdlp_two_streams_never_move_backwards(main):
    state = feed(main, "media", ["yoink-prog  97.0%",
                                 "[download] 100% of  180.00MiB in 00:01:00 at 3MiB/s",
                                 "yoink-prog   3.0%"], {"done": 0, "total": 1})
    assert state["progress"] == 1.0  # audio stream's restart can't drag the bar back


def test_ytdlp_playlist_progress_folds_items_and_stream(main):
    lines = ["[download] Downloading item 1 of 8", "yoink-prog  80.0%",
             "[download] Downloading item 2 of 8", "yoink-prog  10.0%"]
    state = feed(main, "media", lines)
    assert state["done"] == 1
    assert abs(state["progress"] - (1 + 0.10) / 8) < 1e-9


def test_ytdlp_mp4_playlist_does_not_overcount_done(main):
    # each mp4 item completes TWO streams; done must come from item transitions
    lines = []
    for i in (1, 2):
        lines += [f"[download] Downloading item {i} of 2",
                  "[download] 100% of  10MiB in 00:00:02 at 5MiB/s",
                  "[download] 100% of   1MiB in 00:00:01 at 1MiB/s"]
    state = feed(main, "media", lines)
    assert state["done"] == 1 and state["total"] == 2


def test_spotdl_sets_progress_fraction(main):
    state = feed(main, "spotify", ["3/10 complete"])
    assert state["progress"] == 0.3


def test_ytdlp_stream_suffix_stripped_from_current(main):
    state = feed(main, "media", ["[download] Destination: /d/x/Sintel - Open Movie.f137.mp4"])
    assert state["current"] == "Sintel - Open Movie"
