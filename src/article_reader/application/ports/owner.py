"""Owner identity and password hashing contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from article_reader.application.ports.persistence import ViewerSessionRecord


@dataclass(frozen=True, slots=True)
class OwnerCredential:
    viewer_id: str
    password_hash: str
    revision: int
    hostname: str | None


class OwnerAccessError(ValueError):
    def __init__(
        self, message: str, *, rate_limited: bool = False, session_limit: bool = False
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        self.session_limit = session_limit


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...

    def verify(self, encoded: str, password: str) -> bool: ...


class OwnerRepository(Protocol):
    def get(self) -> OwnerCredential | None: ...

    def initialize(self, encoded: str, *, now: str) -> None: ...

    def reset_password(self, encoded: str, *, now: str) -> None: ...

    def revoke_all(self, *, now: str) -> None: ...

    def bind_hostname(self, hostname: str, *, allow_change: bool, now: str) -> None: ...

    def begin_attempt(self, *, now: str) -> OwnerCredential: ...

    def finish_login(self, session: ViewerSessionRecord, *, max_sessions: int) -> bool: ...

    def authenticate(self, token_hash: str, *, now: str) -> ViewerSessionRecord | None: ...

    def sessions(self, *, now: str) -> tuple[ViewerSessionRecord, ...]: ...

    def revoke(self, session_id: str, *, now: str) -> bool: ...
