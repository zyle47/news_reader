"""Application policy for loopback sessions and short-lived LAN pairing.

Pairing codes exist only as digests in bounded process memory and are invalidated by a
restart. Browser sessions are high-entropy, revocable, expiry-checked records whose raw
tokens are returned once and never persisted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import uuid4

from article_reader.application.ports.persistence import (
    ViewerRecord,
    ViewerRepository,
    ViewerSessionRecord,
)


class AccessErrorCode(StrEnum):
    PAIRING_REJECTED = "PAIRING_REJECTED"
    PAIRING_RATE_LIMITED = "PAIRING_RATE_LIMITED"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    SESSION_LIMIT_REACHED = "SESSION_LIMIT_REACHED"


class AccessError(RuntimeError):
    def __init__(self, code: AccessErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedSession:
    record: ViewerSessionRecord
    token: str


@dataclass(frozen=True, slots=True)
class PairingOffer:
    code: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class _PairingRecord:
    viewer_id: str
    code_hash: str
    created_at: datetime
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class AccessService:
    """Own authentication, session lifecycle, and brute-force-resistant pairing policy."""

    def __init__(
        self,
        viewers: ViewerRepository,
        *,
        session_lifetime_seconds: int,
        pairing_lifetime_seconds: int,
        max_sessions_per_viewer: int,
        max_pairing_attempts: int,
        pairing_attempt_window_seconds: int,
        pairing_block_seconds: int,
        max_pairing_offers: int = 32,
        max_rate_limit_clients: int = 1_024,
        clock: Callable[[], datetime] = _utc_now,
        token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
        code_factory: Callable[[], int] = lambda: secrets.randbelow(100_000_000),
    ) -> None:
        self._viewers = viewers
        self._session_lifetime = timedelta(seconds=session_lifetime_seconds)
        self._pairing_lifetime = timedelta(seconds=pairing_lifetime_seconds)
        self._max_sessions = max_sessions_per_viewer
        self._max_attempts = max_pairing_attempts
        self._attempt_window = timedelta(seconds=pairing_attempt_window_seconds)
        self._block_time = timedelta(seconds=pairing_block_seconds)
        self._max_pairing_offers = max_pairing_offers
        self._max_rate_limit_clients = max_rate_limit_clients
        self._clock = clock
        self._token_factory = token_factory
        self._code_factory = code_factory
        self._pairings: dict[str, _PairingRecord] = {}
        self._failures: dict[str, list[datetime]] = {}
        self._blocked_until: dict[str, datetime] = {}
        self._lock = threading.Lock()

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def authenticate(self, token: str | None) -> ViewerSessionRecord | None:
        if not token or len(token) > 256:
            return None
        now = self._clock()
        session = self._viewers.find_session_by_token(self.token_hash(token), now=_iso(now))
        if session is None:
            return None
        last_seen = datetime.fromisoformat(session.last_seen_at)
        if now - last_seen >= timedelta(minutes=5):
            self._viewers.touch_session(session.session_id, seen_at=_iso(now))
        return session

    def create_local_viewer_session(self, *, label: str = "This browser") -> IssuedSession:
        now = self._clock()
        viewer_id = uuid4().hex
        self._viewers.create(ViewerRecord(viewer_id=viewer_id, label=label, created_at=_iso(now)))
        return self._issue_session(viewer_id, label=label, now=now)

    def create_pairing(self, viewer_id: str) -> PairingOffer:
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            while len(self._pairings) >= self._max_pairing_offers:
                oldest_key = min(self._pairings, key=lambda key: self._pairings[key].created_at)
                self._pairings.pop(oldest_key)
            for _attempt in range(16):
                code = f"{self._code_factory():08d}"
                digest = self.token_hash(code)
                if digest not in self._pairings:
                    break
            else:
                raise RuntimeError("could not allocate a unique pairing code")
            expires_at = now + self._pairing_lifetime
            self._pairings[digest] = _PairingRecord(
                viewer_id=viewer_id,
                code_hash=digest,
                created_at=now,
                expires_at=expires_at,
            )
        return PairingOffer(code=code, expires_at=_iso(expires_at))

    def redeem_pairing(self, *, code: str, label: str, client_key: str) -> IssuedSession:
        now = self._clock()
        normalized_label = " ".join(label.split()).strip() or "Paired device"
        normalized_label = normalized_label[:40]
        with self._lock:
            self._cleanup(now)
            blocked_until = self._blocked_until.get(client_key)
            if blocked_until is not None and blocked_until > now:
                raise AccessError(
                    AccessErrorCode.PAIRING_RATE_LIMITED,
                    "Too many pairing attempts. Wait a moment and try again.",
                )
            if len(code) != 8 or not code.isascii() or not code.isdigit():
                self._record_failure(client_key, now)
                raise AccessError(
                    AccessErrorCode.PAIRING_REJECTED,
                    "The pairing code is invalid or has expired.",
                )
            submitted_hash = self.token_hash(code)
            matching_key = next(
                (
                    digest
                    for digest in self._pairings
                    if hmac.compare_digest(digest, submitted_hash)
                ),
                None,
            )
            if matching_key is None:
                self._record_failure(client_key, now)
                raise AccessError(
                    AccessErrorCode.PAIRING_REJECTED,
                    "The pairing code is invalid or has expired.",
                )
            pairing = self._pairings.pop(matching_key)
            self._failures.pop(client_key, None)
            self._blocked_until.pop(client_key, None)
        return self._issue_session(pairing.viewer_id, label=normalized_label, now=now)

    def list_sessions(self, viewer_id: str) -> tuple[ViewerSessionRecord, ...]:
        return self._viewers.list_sessions(viewer_id, now=_iso(self._clock()))

    def revoke_session(self, viewer_id: str, session_id: str) -> bool:
        return self._viewers.revoke_session(viewer_id, session_id, revoked_at=_iso(self._clock()))

    def _issue_session(self, viewer_id: str, *, label: str, now: datetime) -> IssuedSession:
        active = self._viewers.list_sessions(viewer_id, now=_iso(now))
        if len(active) >= self._max_sessions:
            raise AccessError(
                AccessErrorCode.SESSION_LIMIT_REACHED,
                "The paired-device limit is reached. Revoke an old device and try again.",
            )
        token = self._token_factory()
        if len(token) < 32:
            raise RuntimeError("session token factory returned insufficient entropy")
        record = ViewerSessionRecord(
            session_id=uuid4().hex,
            viewer_id=viewer_id,
            label=label,
            token_hash=self.token_hash(token),
            created_at=_iso(now),
            last_seen_at=_iso(now),
            expires_at=_iso(now + self._session_lifetime),
        )
        self._viewers.create_session(record)
        return IssuedSession(record=record, token=token)

    def _record_failure(self, client_key: str, now: datetime) -> None:
        cutoff = now - self._attempt_window
        recent = [moment for moment in self._failures.get(client_key, []) if moment > cutoff]
        recent.append(now)
        self._failures[client_key] = recent
        if len(recent) >= self._max_attempts:
            self._blocked_until[client_key] = now + self._block_time
            self._failures.pop(client_key, None)
        if len(self._failures) + len(self._blocked_until) > self._max_rate_limit_clients:
            keys = sorted(
                set(self._failures) | set(self._blocked_until),
                key=lambda key: (
                    self._blocked_until.get(key)
                    or self._failures.get(key, [datetime.min.replace(tzinfo=UTC)])[-1]
                ),
            )
            for key in keys[: len(keys) // 4 or 1]:
                self._failures.pop(key, None)
                self._blocked_until.pop(key, None)

    def _cleanup(self, now: datetime) -> None:
        self._pairings = {
            digest: record for digest, record in self._pairings.items() if record.expires_at > now
        }
        cutoff = now - self._attempt_window
        self._failures = {
            key: [moment for moment in moments if moment > cutoff]
            for key, moments in self._failures.items()
            if any(moment > cutoff for moment in moments)
        }
        self._blocked_until = {
            key: until for key, until in self._blocked_until.items() if until > now
        }


__all__ = [
    "AccessError",
    "AccessErrorCode",
    "AccessService",
    "IssuedSession",
    "PairingOffer",
]
