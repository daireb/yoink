"""SSRF guard and session-token behaviour."""
import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://127.0.0.1:8080/x", "http://10.0.0.5/x",
    "http://192.168.1.1/x", "http://169.254.169.254/latest/meta-data",
    "http://[::1]/x", "http://0.0.0.0/x", "http://localhost/x", "notaurl",
])
def test_assert_public_url_blocks_internal(main, url):
    with pytest.raises(HTTPException):
        main.assert_public_url(url)


def test_assert_public_url_allows_global_ip(main):
    main.assert_public_url("http://1.1.1.1/video")  # IP literal: no DNS needed


def test_session_token_depends_on_password(main, monkeypatch):
    t1 = main.session_token()
    monkeypatch.setattr(main, "PASSWORD", "rotated")
    assert main.session_token() != t1  # rotating the password revokes every session


def test_authed_open_mode(main, monkeypatch):
    monkeypatch.setattr(main, "PASSWORD", "")

    class Req:
        cookies = {}

    assert main.authed(Req()) is True
