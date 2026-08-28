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


def test_ytdlp_already_downloaded_counts(main):
    state = feed(main, "media", ["[download] Big Buck Bunny.webm has already been downloaded"])
    assert state["done"] == 1
