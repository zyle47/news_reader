"""Onion requests never inherit local bootstrap, including missing/loopback peers."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from article_reader.api.app import create_app
from article_reader.application.services.owner_access import OwnerAccessService
from article_reader.config import PathsSettings, ServerSettings, Settings, ensure_runtime_dirs
from article_reader.db.connection import Database
from article_reader.db.repositories.owner import SqliteOwnerRepository
from article_reader.security.onion import read_onion_hostname, validate_onion_hostname
from article_reader.speech.registry import VoiceRegistry
from tests.test_owner_access import FastHasher


def onion_host() -> str:
    key = b"a" * 32
    checksum = hashlib.sha3_256(b".onion checksum" + key + b"\x03").digest()[:2]
    return base64.b32encode(key + checksum + b"\x03").decode().lower() + ".onion"


def setup(tmp_path: Path, peer: tuple[str, int] | None = None) -> tuple[TestClient, Database]:
    settings = Settings(
        paths=PathsSettings(tmp_path),
        server=ServerSettings(onion_mode=True, onion_hostname=onion_host()),
    )
    ensure_runtime_dirs(settings)
    database = Database(settings.paths.database_path)
    owner = OwnerAccessService(
        SqliteOwnerRepository(database), FastHasher(), session_lifetime_seconds=3600, max_sessions=2
    )
    owner.initialize("a long test password")
    app = create_app(
        settings, database, VoiceRegistry(schema_version=1, voices=()), owner_service=owner
    )
    # HTTPS transport here exercises a Secure cookie jar; real HTTP-onion cookie roundtrip
    # belongs to the physical Tor browser gate, not httpx's generic HTTP policy.
    client = TestClient(app, base_url=f"https://{onion_host()}", client=cast(tuple[str, int], peer))
    return client, database


@pytest.mark.parametrize("peer", [None, ("127.0.0.1", 1234), ("192.168.1.1", 1234)])
def test_onion_never_bootstraps(tmp_path: Path, peer: tuple[str, int] | None) -> None:
    client, database = setup(tmp_path, peer)
    root = client.get("/")
    assert root.status_code == 200
    assert "set-cookie" not in root.headers
    assert client.get("/api/auth").json()["authenticated"] is False
    assert client.get("/health/live").json() == {"alive": True}
    for path in (
        "/api/readings",
        "/api/voices",
        "/api/sessions",
        "/api/jobs/test",
        "/api/renditions/test/manifest",
        "/api/audio/test/0/abc.wav",
    ):
        assert client.get(path, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 401
    assert client.head("/api/audio/test/0/abc.wav").status_code == 401
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM viewers").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM viewer_sessions").fetchone()[0] == 0


def test_onion_login_boundary_revocation_and_library(tmp_path: Path) -> None:
    client, _database = setup(tmp_path)
    host = onion_host()
    login = "/api/auth/login"
    password = {"password": "a long test password", "label": "Phone"}
    for origin in (None, "null", "http://evil.example", f"http://{host}:80"):
        headers = {"Origin": origin} if origin else {}
        assert client.post(login, json=password, headers=headers).status_code == 403
    headers = {"Origin": f"http://{host}"}
    assert (
        client.post(login, json=password, headers={**headers, "Host": "localhost:8765"}).status_code
        == 400
    )
    assert (
        client.post(
            login, content="password", headers={**headers, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    assert (
        client.post(
            login, content=b"x" * 9000, headers={**headers, "Content-Type": "application/json"}
        ).status_code
        == 413
    )
    assert client.post(login, json={"password": "wrong"}, headers=headers).status_code == 401
    response = client.post(login, json=password, headers=headers)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie
    assert client.get("/api/auth").json()["authenticated"] is True
    assert client.get("/api/readings").status_code == 200
    assert client.post("/api/pairings", json={}, headers=headers).status_code == 404
    assert (
        client.post("/api/pairings/redeem", json={"code": "12345678"}, headers=headers).status_code
        == 404
    )
    session = client.get("/api/sessions").json()["sessions"][0]["session_id"]
    assert (
        client.post(f"/api/sessions/{session}/revoke", json={}, headers=headers).status_code == 200
    )
    assert client.get("/api/readings").status_code == 401
    assert client.post(login, json=password, headers=headers).status_code == 200
    assert client.get("/api/readings").json() == {"readings": []}


def test_onion_hostname_validation(tmp_path: Path) -> None:
    hostname = onion_host()
    assert validate_onion_hostname(hostname) == hostname
    path = tmp_path / "hostname"
    path.write_text(hostname + "\n")
    assert read_onion_hostname(path) == hostname
    for value in (
        "a" * 56 + ".onion",
        hostname.upper(),
        "evil.example",
        hostname + ":80",
        hostname + ".evil",
        " " + hostname,
    ):
        with pytest.raises(ValueError):
            validate_onion_hostname(value)
    path.write_text(hostname + "x" * 100)
    with pytest.raises(ValueError):
        read_onion_hostname(path)
