"""«Музика»: songs are shared by everyone (server/songs.py), streamed by id with Range."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import app as web_app, songs


@pytest.fixture
def client():
    return TestClient(web_app.app)


def _upload(client, name="Пісня", data=b"0123456789", mime="audio/mpeg"):
    return client.post("/api/songs", params={"name": name}, content=data, headers={"Content-Type": mime})


def test_upload_list_stream_delete(client):
    assert client.get("/api/songs").json() == {"songs": []}
    song = _upload(client, "Моя пісня").json()
    assert song["name"] == "Моя пісня" and song["size"] == 10
    assert client.get("/api/songs").json()["songs"] == [song]  # another device sees the same list
    full = client.get(f"/api/songs/{song['id']}/audio")
    assert full.status_code == 200 and full.content == b"0123456789" and full.headers["content-type"] == "audio/mpeg"
    assert full.headers["accept-ranges"] == "bytes"
    assert client.delete(f"/api/songs/{song['id']}").json() == {"ok": True}
    assert client.get("/api/songs").json() == {"songs": []}
    assert client.get(f"/api/songs/{song['id']}/audio").status_code == 404


def test_range_requests_for_safari(client):
    sid = _upload(client).json()["id"]
    part = client.get(f"/api/songs/{sid}/audio", headers={"Range": "bytes=2-4"})
    assert part.status_code == 206 and part.content == b"234" and part.headers["content-range"] == "bytes 2-4/10"
    tail = client.get(f"/api/songs/{sid}/audio", headers={"Range": "bytes=7-"})
    assert tail.status_code == 206 and tail.content == b"789"
    last = client.get(f"/api/songs/{sid}/audio", headers={"Range": "bytes=-3"})
    assert last.status_code == 206 and last.content == b"789"
    assert client.get(f"/api/songs/{sid}/audio", headers={"Range": "bytes=50-"}).status_code == 416


def test_only_audio_and_within_limits(client, monkeypatch):
    assert _upload(client, mime="text/plain").status_code == 415
    assert _upload(client, data=b"").status_code == 400
    monkeypatch.setattr(songs, "MAX_SONG_BYTES", 5)
    assert _upload(client).status_code == 413
    monkeypatch.setattr(songs, "MAX_SONG_BYTES", 100)
    monkeypatch.setattr(songs, "MAX_SONGS", 1)
    assert _upload(client).status_code == 200
    assert _upload(client).status_code == 409
