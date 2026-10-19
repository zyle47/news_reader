"""Owner credentials survive browser resets; modes and durable throttle remain separate."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from article_reader.application.ports.owner import OwnerAccessError
from article_reader.application.services.access_control import AccessService
from article_reader.application.services.owner_access import OwnerAccessService
from article_reader.db.connection import Database
from article_reader.db.repositories.owner import SqliteOwnerRepository
from article_reader.db.repositories.viewers import SqliteViewerRepository
from article_reader.security.passwords import Argon2PasswordHasher


class FastHasher:
    calls = 0

    def hash(self, password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def verify(self, encoded: str, password: str) -> bool:
        self.calls += 1
        return self.hash(password) == encoded


def service(repository: SqliteOwnerRepository, hasher: FastHasher) -> OwnerAccessService:
    return OwnerAccessService(repository, hasher, session_lifetime_seconds=3600, max_sessions=2)


def test_owner_relogin_sessions_reset_expiry_and_audience(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    repository = SqliteOwnerRepository(database)
    owner = service(repository, FastHasher())
    owner.initialize("a long test password")
    with pytest.raises(OwnerAccessError, match="already exists"):
        owner.initialize("other test password")
    first = owner.login("a long test password")
    second = owner.login("a long test password")
    with pytest.raises(OwnerAccessError) as capped:
        owner.login("a long test password")
    assert capped.value.session_limit
    assert owner.authenticate(first.token) is not None
    repository.revoke_all(now=datetime.now(UTC).isoformat())
    third = owner.login("a long test password")
    second = owner.login("a long test password")
    assert first.record.viewer_id == second.record.viewer_id == third.record.viewer_id
    assert owner.authenticate(first.token) is None
    assert owner.authenticate(second.token) is not None
    assert first.token != second.token
    with database.read() as connection:
        assert first.token not in str(
            connection.execute("SELECT * FROM viewer_sessions").fetchall()
        )
    local = AccessService(
        SqliteViewerRepository(database),
        session_lifetime_seconds=3600,
        pairing_lifetime_seconds=300,
        max_sessions_per_viewer=8,
        max_pairing_attempts=5,
        pairing_attempt_window_seconds=60,
        pairing_block_seconds=60,
    )
    assert local.authenticate(second.token) is None
    assert owner.authenticate(local.create_local_viewer_session().token) is None
    assert owner.revoke_session(second.record.viewer_id, second.record.session_id)
    assert owner.authenticate(second.token) is None
    future = OwnerAccessService(
        repository,
        FastHasher(),
        session_lifetime_seconds=3600,
        max_sessions=2,
        clock=lambda: datetime.now(UTC) + timedelta(hours=2),
    )
    assert future.authenticate(third.token) is None
    owner.reset_password("a new long test password")
    assert owner.authenticate(third.token) is None
    assert owner.login("a new long test password").record.viewer_id == first.record.viewer_id


def test_throttle_survives_restart_and_bounds_hashing(tmp_path: Path) -> None:
    database = Database(tmp_path / "test.sqlite3")
    hasher = FastHasher()
    owner = service(SqliteOwnerRepository(database), hasher)
    owner.initialize("a long test password")
    for _ in range(5):
        with pytest.raises(OwnerAccessError) as caught:
            owner.login("wrong")
        assert not caught.value.rate_limited
    database.close()
    restarted = service(SqliteOwnerRepository(Database(tmp_path / "test.sqlite3")), hasher)
    with pytest.raises(OwnerAccessError) as caught:
        restarted.login("a long test password")
    assert caught.value.rate_limited
    assert hasher.calls == 5


def test_password_revision_fences_inflight_login_and_identity_change(tmp_path: Path) -> None:
    repository = SqliteOwnerRepository(Database(tmp_path / "test.sqlite3"))
    owner = service(repository, FastHasher())
    owner.initialize("a long test password")

    class ResetDuringVerify(FastHasher):
        def verify(self, encoded: str, password: str) -> bool:
            owner.reset_password("a newer long password")
            return True

    racing = service(repository, ResetDuringVerify())
    with pytest.raises(OwnerAccessError):
        racing.login("a long test password")
    assert repository.sessions(now=datetime.now(UTC).isoformat()) == ()
    repository.bind_hostname("first", allow_change=False, now=datetime.now(UTC).isoformat())
    with pytest.raises(OwnerAccessError, match="identity changed"):
        repository.bind_hostname("second", allow_change=False, now=datetime.now(UTC).isoformat())
    repository.bind_hostname("second", allow_change=True, now=datetime.now(UTC).isoformat())


def test_argon2_adapter_and_password_bounds() -> None:
    hasher = Argon2PasswordHasher()
    encoded = hasher.hash("a long test password")
    assert encoded.startswith("$argon2id$")
    assert hasher.verify(encoded, "a long test password")
    assert not hasher.verify(encoded, "wrong")
    assert not hasher.verify("invalid", "password")
    for password in ("short", "x" * 257, "😀" * 257):
        with pytest.raises(OwnerAccessError):
            OwnerAccessService.validate_password(password)
