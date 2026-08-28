"""Test env must exist before app.main is imported: importing it creates the
data dir, opens the DB, and starts the (daemon) worker threads."""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="yoink-test-"))
os.environ.setdefault("YOINK_DATA_DIR", str(_tmp / "data"))
os.environ.setdefault("YOINK_DOWNLOAD_DIR", str(_tmp / "downloads"))
os.environ.setdefault("YOINK_UPDATE_CHECK", "0")
os.environ.setdefault("YOINK_PASSWORD", "test-password")
(_tmp / "downloads").mkdir(parents=True, exist_ok=True)

import app.main  # noqa: E402,F401  (single import, with the env above in place)

import pytest  # noqa: E402


@pytest.fixture
def main():
    return app.main


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(app.main.app)


@pytest.fixture
def logged_in(client):
    r = client.post("/api/login", json={"password": "test-password"})
    assert r.status_code == 200
    return client
