"""Lane assignment and input classification — why a song never waits behind a playlist."""
import pytest


@pytest.mark.parametrize("kind,text,total,lane", [
    ("csv", "[]", 40, "bulk"),
    ("retry", "[]", 1, "bulk"),
    ("search", "charli xcx speed drive", None, "quick"),
    ("spotify", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT", 1, "quick"),
    ("spotify", "https://open.spotify.com/album/2lIZef4lzdvZkiiCzvPKj7", 15, "bulk"),
    ("spotify", "https://open.spotify.com/playlist/37i9dQZF1DX0kbJZpiYdZl", 50, "bulk"),
    ("spotify", "https://open.spotify.com/artist/25uiPmTg16RbhZWAqwLBy5", None, "bulk"),
    ("media", "https://www.youtube.com/watch?v=aqz-KE-bpKQ", 1, "quick"),
    ("media", "https://www.youtube.com/watch?v=x&list=PLa1F2ddGya_x", 8, "bulk"),
    ("media", "https://www.youtube.com/playlist?list=PLa1F2ddGya_x", 8, "bulk"),
    ("media", "https://soundcloud.com/artist/track", None, "quick"),
])
def test_lane_for(main, kind, text, total, lane):
    assert main.lane_for(kind, text, total) == lane


@pytest.mark.parametrize("text,kind", [
    ("https://open.spotify.com/track/abc", "spotify"),
    ("spotify:track:abc", "spotify"),
    ("https://www.youtube.com/watch?v=abc", "media"),
    ("http://example.com/thing.mp4", "media"),
    ("kavinsky nightcall", "search"),
    ("purely words with spotify in them", "search"),
])
def test_detect_kind(main, text, kind):
    assert main.detect_kind(text) == kind
