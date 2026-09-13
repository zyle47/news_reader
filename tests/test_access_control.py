"""Pairing and durable-session policy tests with injected time and randomness."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from article_reader.application.services.access_control import (
    AccessError,
    AccessErrorCode,
    AccessService,
)
from article_reader.db.connection import Database
from article_reader.db.repositories.viewers import SqliteViewerRepository


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _Tokens:
    def __init__(self) -> None:
        self.counter = 0

    def __call__(self) -> str:
        self.counter += 1
        return f"session-token-{self.counter:04d}-" + "x" * 32


def _service(
    path: Path,
    clock: _Clock,
    tokens: _Tokens,
    *,
    code: int = 12_345_678,
    max_sessions: int = 4,
) -> AccessService:
    return AccessService(
        SqliteViewerRepository(Database(path)),
        session_lifetime_seconds=3_600,
        pairing_lifetime_seconds=60,
        max_sessions_per_viewer=max_sessions,
        max_pairing_attempts=3,
        pairing_attempt_window_seconds=60,
        pairing_block_seconds=30,
        clock=clock,
        token_factory=tokens,
        code_factory=lambda: code,
    )


def test_session_survives_service_restart_then_expires_and_can_be_revoked(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite3"
    clock = _Clock()
    tokens = _Tokens()
    first = _service(path, clock, tokens)
    issued = first.create_local_viewer_session(label="Desktop")

    restarted = _service(path, clock, tokens)
    restored = restarted.authenticate(issued.token)
    assert restored is not None
    assert restored.viewer_id == issued.record.viewer_id
    assert restarted.revoke_session(restored.viewer_id, restored.session_id) is True
    assert restarted.authenticate(issued.token) is None

    replacement = restarted.create_local_viewer_session(label="Other desktop")
    clock.advance(3_601)
    assert restarted.authenticate(replacement.token) is None


def test_pairing_is_single_use_and_links_to_the_existing_viewer(tmp_path: Path) -> None:
    clock = _Clock()
    tokens = _Tokens()
    service = _service(tmp_path / "app.sqlite3", clock, tokens)
    desktop = service.create_local_viewer_session(label="Desktop")
    offer = service.create_pairing(desktop.record.viewer_id)

    phone = service.redeem_pairing(
        code=offer.code, label="  My   phone  ", client_key="192.168.1.20"
    )
    assert phone.record.viewer_id == desktop.record.viewer_id
    assert phone.record.label == "My phone"
    assert service.token_hash(phone.token) == phone.record.token_hash

    with pytest.raises(AccessError) as reused:
        service.redeem_pairing(code=offer.code, label="Second phone", client_key="192.168.1.21")
    assert reused.value.code is AccessErrorCode.PAIRING_REJECTED


def test_expired_and_malformed_codes_fail_without_revealing_state(tmp_path: Path) -> None:
    clock = _Clock()
    tokens = _Tokens()
    service = _service(tmp_path / "app.sqlite3", clock, tokens)
    desktop = service.create_local_viewer_session()
    offer = service.create_pairing(desktop.record.viewer_id)
    clock.advance(61)

    for code in (offer.code, "not-a-code"):
        with pytest.raises(AccessError) as rejected:
            service.redeem_pairing(code=code, label="Phone", client_key=code)
        assert rejected.value.code is AccessErrorCode.PAIRING_REJECTED
        assert "invalid or has expired" in str(rejected.value)


def test_pairing_attempts_are_rate_limited_per_client(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "app.sqlite3", clock, _Tokens())

    for _attempt in range(3):
        with pytest.raises(AccessError) as rejected:
            service.redeem_pairing(code="00000000", label="Phone", client_key="192.168.1.99")
        assert rejected.value.code is AccessErrorCode.PAIRING_REJECTED

    with pytest.raises(AccessError) as blocked:
        service.redeem_pairing(code="00000000", label="Phone", client_key="192.168.1.99")
    assert blocked.value.code is AccessErrorCode.PAIRING_RATE_LIMITED

    clock.advance(31)
    with pytest.raises(AccessError) as after_block:
        service.redeem_pairing(code="00000000", label="Phone", client_key="192.168.1.99")
    assert after_block.value.code is AccessErrorCode.PAIRING_REJECTED


def test_session_limit_requires_explicit_revocation(tmp_path: Path) -> None:
    clock = _Clock()
    service = _service(tmp_path / "app.sqlite3", clock, _Tokens(), max_sessions=1)
    desktop = service.create_local_viewer_session()
    offer = service.create_pairing(desktop.record.viewer_id)

    with pytest.raises(AccessError) as limited:
        service.redeem_pairing(code=offer.code, label="Phone", client_key="192.168.1.2")
    assert limited.value.code is AccessErrorCode.SESSION_LIMIT_REACHED
    assert service.authenticate(desktop.token) is not None
