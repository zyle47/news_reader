"""One durable owner library with password login and separate onion sessions."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from article_reader.application.ports.owner import OwnerAccessError, OwnerRepository, PasswordHasher
from article_reader.application.ports.persistence import ViewerSessionRecord
from article_reader.application.services.access_control import AccessService, IssuedSession


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class OwnerAccessService:
    def __init__(
        self,
        repository: OwnerRepository,
        hasher: PasswordHasher,
        *,
        session_lifetime_seconds: int,
        max_sessions: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._hasher = hasher
        self._lifetime = timedelta(seconds=session_lifetime_seconds)
        self._max_sessions = max_sessions
        self._clock = clock
        self._verification = threading.Lock()

    @staticmethod
    def validate_password(password: str) -> None:
        if not 12 <= len(password) <= 256 or len(password.encode("utf-8")) > 1024:
            raise OwnerAccessError("Choose a password of 12-256 characters (at most 1024 bytes).")

    def initialize(self, password: str) -> None:
        self.validate_password(password)
        self._repository.initialize(self._hasher.hash(password), now=_iso(self._clock()))

    def reset_password(self, password: str) -> None:
        self.validate_password(password)
        self._repository.reset_password(self._hasher.hash(password), now=_iso(self._clock()))

    def login(self, password: str, label: str = "My phone") -> IssuedSession:
        if not self._verification.acquire(blocking=False):
            raise OwnerAccessError("Please wait before trying again.", rate_limited=True)
        try:
            now = self._clock()
            credential = self._repository.begin_attempt(now=_iso(now))
            if not password or len(password) > 256 or len(password.encode("utf-8")) > 1024:
                raise OwnerAccessError("Password not accepted.")
            if not self._hasher.verify(credential.password_hash, password):
                raise OwnerAccessError("Password not accepted.")
            token = secrets.token_urlsafe(32)
            session = ViewerSessionRecord(
                session_id=uuid4().hex,
                viewer_id=credential.viewer_id,
                label=" ".join(label.split())[:40] or "Owner device",
                token_hash=AccessService.token_hash(token),
                created_at=_iso(now),
                last_seen_at=_iso(now),
                expires_at=_iso(now + self._lifetime),
                audience="onion_owner",
                credential_revision=credential.revision,
            )
            if not self._repository.finish_login(session, max_sessions=self._max_sessions):
                raise OwnerAccessError("Password not accepted.")
            return IssuedSession(session, token)
        finally:
            self._verification.release()

    def authenticate(self, token: str | None) -> ViewerSessionRecord | None:
        if not token or len(token) > 256:
            return None
        return self._repository.authenticate(
            AccessService.token_hash(token), now=_iso(self._clock())
        )

    def list_sessions(self, _viewer_id: str) -> tuple[ViewerSessionRecord, ...]:
        return self._repository.sessions(now=_iso(self._clock()))

    def revoke_session(self, _viewer_id: str, session_id: str) -> bool:
        return self._repository.revoke(session_id, now=_iso(self._clock()))
