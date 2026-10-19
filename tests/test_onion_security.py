"""Exercise resource bounds, shared-owner recovery and fail-closed onion entry points."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from article_reader import cli
from article_reader.api.app import create_app
from article_reader.application.ports.persistence import AudioChunkRecord, ChunkState
from article_reader.application.services.owner_access import OwnerAccessService
from article_reader.clock import now_iso
from article_reader.config import PathsSettings, ServerSettings, Settings, ensure_runtime_dirs
from article_reader.db.connection import Database
from article_reader.db.repositories.owner import SqliteOwnerRepository
from article_reader.domain.speech import AudioResult
from article_reader.security.onion import read_onion_hostname
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.durable_audio import LocalDurableAudioStore
from tests.conftest import make_repos, seed_article_ready_for_voice, seed_reading
from tests.test_onion_api import onion_host, setup
from tests.test_owner_access import FastHasher
from tests.test_worker_loop import _seed_rendition, _seed_synthesize_job

PASSWORD = "a long test password"
ORIGIN = {"Origin": f"http://{onion_host()}"}


def _app(tmp_path: Path, hasher: FastHasher) -> tuple[FastAPI, Database, OwnerAccessService]:
    settings = Settings(
        paths=PathsSettings(tmp_path),
        server=ServerSettings(onion_mode=True, onion_hostname=onion_host()),
    )
    ensure_runtime_dirs(settings)
    database = Database(settings.paths.database_path)
    owner = OwnerAccessService(
        SqliteOwnerRepository(database), hasher, session_lifetime_seconds=3600, max_sessions=4
    )
    owner.initialize(PASSWORD)
    app = create_app(
        settings, database, VoiceRegistry(schema_version=1, voices=()), owner_service=owner
    )
    return app, database, owner


def test_chunked_oversized_login_is_rejected_before_hashing(tmp_path: Path) -> None:
    hasher = FastHasher()
    app, _database, _owner = _app(tmp_path, hasher)
    sent: list[Message] = []
    consumed = 0

    async def receive() -> Message:
        nonlocal consumed
        consumed += 1
        # Each individual chunk fits; only the streamed aggregate exceeds the limit.
        return {"type": "http.request", "body": b" " * 3000, "more_body": True}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/auth/login",
        "raw_path": b"/api/auth/login",
        "query_string": b"",
        "root_path": "",
        "client": None,
        "server": (onion_host(), 80),
        "headers": [
            (b"host", onion_host().encode()),
            (b"origin", ORIGIN["Origin"].encode()),
            (b"content-type", b"application/json"),
            (b"transfer-encoding", b"chunked"),
        ],
    }
    asyncio.run(app(scope, receive, send))
    assert next(item["status"] for item in sent if item["type"] == "http.response.start") == 413
    assert consumed == 3
    assert hasher.calls == 0


def test_one_password_verification_does_not_block_authenticated_requests(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingHasher(FastHasher):
        armed = False

        def verify(self, encoded: str, password: str) -> bool:
            if self.armed:
                entered.set()
                assert release.wait(5), "Test did not release password verification"
            return super().verify(encoded, password)

    hasher = BlockingHasher()
    app, _database, _owner = _app(tmp_path, hasher)
    base_url = f"https://{onion_host()}"
    signed_in = TestClient(app, base_url=base_url)
    assert (
        signed_in.post("/api/auth/login", json={"password": PASSWORD}, headers=ORIGIN).status_code
        == 200
    )
    first = TestClient(app, base_url=base_url)
    other = TestClient(app, base_url=base_url)
    hasher.armed = True
    with ThreadPoolExecutor(max_workers=3) as executor:
        verifying = executor.submit(
            first.post, "/api/auth/login", json={"password": PASSWORD}, headers=ORIGIN
        )
        try:
            assert entered.wait(2), "Password verification did not start"
            rejected = executor.submit(
                other.post, "/api/auth/login", json={"password": PASSWORD}, headers=ORIGIN
            ).result(timeout=2)
            assert rejected.status_code == 429
            assert rejected.json()["error"]["code"] == "LOGIN_RATE_LIMITED"
            assert rejected.headers["Retry-After"] == "60"
            history = executor.submit(signed_in.get, "/api/readings").result(timeout=2)
            assert history.status_code == 200
            assert hasher.calls == 1, "Only the initial completed login has hashed so far"
        finally:
            release.set()
        assert verifying.result(timeout=2).status_code == 200
    assert hasher.calls == 2


def test_login_throttle_has_retry_after_and_survives_api_restart(tmp_path: Path) -> None:
    client, database = setup(tmp_path)
    for _ in range(5):
        assert (
            client.post(
                "/api/auth/login", json={"password": "incorrect"}, headers=ORIGIN
            ).status_code
            == 401
        )
    database.close()
    settings = Settings(
        paths=PathsSettings(tmp_path),
        server=ServerSettings(onion_mode=True, onion_hostname=onion_host()),
    )
    hasher = FastHasher()
    restarted = Database(settings.paths.database_path)
    owner = OwnerAccessService(
        SqliteOwnerRepository(restarted), hasher, session_lifetime_seconds=3600, max_sessions=4
    )
    app = create_app(
        settings, restarted, VoiceRegistry(schema_version=1, voices=()), owner_service=owner
    )
    response = TestClient(app, base_url=f"https://{onion_host()}").post(
        "/api/auth/login", json={"password": PASSWORD}, headers=ORIGIN
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert hasher.calls == 0


def test_cleared_cookie_returns_to_same_library_and_valid_login_reuses_session(
    tmp_path: Path,
) -> None:
    client, database = setup(tmp_path)
    login = {"password": PASSWORD, "label": "First browser"}
    first = client.post("/api/auth/login", json=login, headers=ORIGIN)
    assert first.status_code == 200
    session_id = first.json()["session_id"]
    created = client.post(
        "/api/readings",
        json={"url": "https://example.com/saved-news", "language": "en"},
        headers=ORIGIN,
    )
    assert created.status_code == 202
    reading_id = created.json()["reading_id"]
    # A duplicate sign-in from an already authenticated browser consumes no device slot.
    duplicate = client.post("/api/auth/login", json=login, headers=ORIGIN)
    assert duplicate.status_code == 200
    assert duplicate.json()["session_id"] == session_id
    assert len(client.get("/api/sessions").json()["sessions"]) == 1
    client.cookies.clear()
    assert client.get("/api/readings").status_code == 401
    assert client.post("/api/auth/login", json=login, headers=ORIGIN).status_code == 200
    assert client.get(f"/api/readings/{reading_id}").status_code == 200
    history = client.get("/api/readings").json()["readings"]
    assert [item["reading_id"] for item in history] == [reading_id]
    with database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM viewers").fetchone()[0] == 1


def _published_audio(database: Database, root: Path, owner_id: str) -> str:
    repos = make_repos(database)
    reading_id = seed_reading(repos, owner_id)
    article_id = seed_article_ready_for_voice(repos, reading_id, segment_texts=("News.",))
    rendition_id = "1" * 32
    _seed_rendition(repos, reading_id, article_id, 1, rendition_id)
    _seed_synthesize_job(repos, reading_id, rendition_id)
    job = repos.jobs.claim_next(worker_generation="test", now=now_iso(), lease_seconds=60)
    assert job is not None
    artifact = LocalDurableAudioStore(root).stage(
        rendition_id,
        0,
        AudioResult(
            pcm_bytes=b"\0\0" * 30, sample_rate_hz=16_000, sample_width_bytes=2, channels=1
        ),
    )
    result = repos.audio_chunks.publish_chunk(
        job_id=job.job_id,
        worker_generation="test",
        total_chunks=1,
        now=now_iso(),
        chunk=AudioChunkRecord(
            rendition_id=rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=artifact.relative_path,
            sha256=artifact.sha256,
            byte_count=artifact.byte_count,
            duration_seconds=artifact.duration_seconds,
            publication_generation="test",
            published_at=now_iso(),
        ),
    )
    assert result.committed
    return f"/api/audio/{rendition_id}/0/{artifact.sha256}.wav"


@pytest.mark.parametrize("action", ["password_reset", "revoke_all"])
def test_owner_maintenance_revokes_previously_playable_audio_and_api(
    tmp_path: Path, action: str
) -> None:
    app, database, owner = _app(tmp_path, FastHasher())
    client = TestClient(app, base_url=f"https://{onion_host()}")
    assert (
        client.post("/api/auth/login", json={"password": PASSWORD}, headers=ORIGIN).status_code
        == 200
    )
    repository = SqliteOwnerRepository(database)
    credential = repository.get()
    assert credential is not None
    audio_url = _published_audio(database, tmp_path / "audio" / "renditions", credential.viewer_id)
    assert client.head(audio_url).status_code == 200
    assert client.get(audio_url, headers={"Range": "bytes=0-9"}).status_code == 206
    if action == "password_reset":
        owner.reset_password("a new long test password")
    else:
        repository.revoke_all(now=now_iso())
    assert client.head(audio_url).status_code == 401
    assert client.get(audio_url, headers={"Range": "bytes=0-9"}).status_code == 401
    assert client.get("/api/readings").status_code == 401
    assert client.get("/api/sessions").status_code == 401


def test_conflicting_onion_lan_flags_are_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(["--data-dir", str(tmp_path / "data"), "serve", "--onion", "--lan"])
    assert caught.value.code == 2
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize(
    "flags",
    [
        ["--onion"],
        ["--onion", "--unix-socket", "http.sock"],
        ["--onion", "--onion-hostname-file", "hostname"],
        ["--unix-socket", "http.sock"],
        ["--onion-hostname-file", "hostname"],
    ],
)
def test_incomplete_onion_arguments_never_fall_back_to_local_serve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    def unexpected_server(*_args: object) -> int:
        pytest.fail("Invalid onion invocation must not start any listener")

    monkeypatch.setattr(cli, "_run_serve", unexpected_server)
    assert cli.main(["--data-dir", str(tmp_path / "data"), "serve", *flags]) == 2
    assert "error:" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux hostname handoff rejects symlinks and FIFO")
def test_onion_hostname_rejects_symlink_leaf_and_parent(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    hostname = target / "hostname"
    hostname.write_text(onion_host() + "\n")
    leaf = tmp_path / "leaf"
    leaf.symlink_to(hostname)
    parent = tmp_path / "parent"
    parent.symlink_to(target, target_is_directory=True)
    for candidate in (leaf, parent / "hostname", target):
        with pytest.raises((ValueError, OSError)):
            read_onion_hostname(candidate)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is a Unix filesystem facility")
def test_onion_hostname_fifo_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "hostname"
    os.mkfifo(fifo)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from article_reader.security.onion import read_onion_hostname; "
            "read_onion_hostname(Path(sys.argv[1]))",
            str(fifo),
        ],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert result.returncode == 1
    assert "regular file" in result.stderr
