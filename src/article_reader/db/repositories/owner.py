"""Atomic owner credential, durable throttle and onion session persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from article_reader.application.ports.owner import OwnerAccessError, OwnerCredential
from article_reader.application.ports.persistence import ViewerSessionRecord
from article_reader.db.connection import Database
from article_reader.db.repositories.viewers import _session_from_row


def _credential(row: sqlite3.Row) -> OwnerCredential:
    return OwnerCredential(
        str(row["viewer_id"]), str(row["password_hash"]), int(row["revision"]), row["hostname"]
    )


class SqliteOwnerRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self) -> OwnerCredential | None:
        with self._database.read() as connection:
            row = connection.execute("SELECT * FROM owner_credentials WHERE id = 1").fetchone()
        return None if row is None else _credential(row)

    def initialize(self, encoded: str, *, now: str) -> None:
        with self._database.transaction() as connection:
            if connection.execute("SELECT 1 FROM owner_credentials").fetchone():
                raise OwnerAccessError("Owner already exists; use 'owner password' to reset.")
            viewer_id = uuid4().hex
            connection.execute("INSERT INTO viewers VALUES (?, 'Owner', ?)", (viewer_id, now))
            connection.execute(
                "INSERT INTO owner_credentials(id, viewer_id, password_hash, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (viewer_id, encoded, now),
            )

    def reset_password(self, encoded: str, *, now: str) -> None:
        with self._database.transaction() as connection:
            changed = connection.execute(
                "UPDATE owner_credentials SET password_hash=?, revision=revision+1, "
                "updated_at=? WHERE id=1",
                (encoded, now),
            )
            if not changed.rowcount:
                raise OwnerAccessError("Owner is missing; run 'owner init' first.")
            connection.execute(
                "UPDATE viewer_sessions SET revoked_at=? WHERE audience='onion_owner'", (now,)
            )
            connection.execute("DELETE FROM owner_login_throttle")

    def revoke_all(self, *, now: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE viewer_sessions SET revoked_at=? WHERE audience='onion_owner'", (now,)
            )

    def bind_hostname(self, hostname: str, *, allow_change: bool, now: str) -> None:
        with self._database.transaction() as connection:
            row = connection.execute("SELECT * FROM owner_credentials WHERE id=1").fetchone()
            if row is None:
                raise OwnerAccessError("Owner is missing; run 'owner init' first.")
            previous = row["hostname"]
            if previous is not None and previous != hostname and not allow_change:
                raise OwnerAccessError(
                    "Onion identity changed; restore Tor state or explicitly run "
                    "'owner bind-hostname' while stopped."
                )
            if previous != hostname:
                connection.execute(
                    "UPDATE owner_credentials SET hostname=?, updated_at=? WHERE id=1",
                    (hostname, now),
                )
                connection.execute(
                    "UPDATE viewer_sessions SET revoked_at=? WHERE audience='onion_owner'", (now,)
                )

    def begin_attempt(self, *, now: str) -> OwnerCredential:
        limited = False
        credential: OwnerCredential | None = None
        with self._database.transaction() as connection:
            row = connection.execute("SELECT * FROM owner_credentials WHERE id=1").fetchone()
            if row is None:
                raise OwnerAccessError("Password not accepted.")
            credential = _credential(row)
            throttle = connection.execute(
                "SELECT * FROM owner_login_throttle WHERE id=1"
            ).fetchone()
            moment = datetime.fromisoformat(now)
            if (
                throttle is not None
                and throttle["blocked_until"]
                and str(throttle["blocked_until"]) > now
            ):
                limited = True
            else:
                started = now
                attempts = 0
                if throttle is not None and datetime.fromisoformat(
                    str(throttle["window_started_at"])
                ) > moment - timedelta(seconds=60):
                    started = str(throttle["window_started_at"])
                    attempts = int(throttle["attempts"])
                if attempts >= 5:
                    limited = True
                attempts += 1
                blocked = (
                    (moment + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                    if attempts >= 5
                    else None
                )
                connection.execute(
                    "INSERT OR REPLACE INTO owner_login_throttle VALUES(1, ?, ?, ?)",
                    (started, attempts, blocked),
                )
        if limited:
            raise OwnerAccessError("Please wait before trying again.", rate_limited=True)
        assert credential is not None
        return credential

    def finish_login(self, session: ViewerSessionRecord, *, max_sessions: int) -> bool:
        with self._database.transaction() as connection:
            owner = connection.execute("SELECT * FROM owner_credentials WHERE id=1").fetchone()
            if (
                owner is None
                or owner["revision"] != session.credential_revision
                or owner["viewer_id"] != session.viewer_id
            ):
                return False
            active = connection.execute(
                "SELECT session_id FROM viewer_sessions WHERE audience='onion_owner' "
                "AND revoked_at IS NULL AND expires_at>? ORDER BY last_seen_at, session_id",
                (session.created_at,),
            ).fetchall()
            if len(active) >= max_sessions:
                raise OwnerAccessError(
                    "Device limit reached. Revoke an old device from a signed-in browser, "
                    "or use the server's owner revoke-all command.",
                    session_limit=True,
                )
            connection.execute(
                """INSERT INTO viewer_sessions(session_id, token_hash, viewer_id, label,
                created_at, last_seen_at, expires_at, audience, credential_revision)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'onion_owner', ?)""",
                (
                    session.session_id,
                    session.token_hash,
                    session.viewer_id,
                    session.label,
                    session.created_at,
                    session.last_seen_at,
                    session.expires_at,
                    session.credential_revision,
                ),
            )
            connection.execute("DELETE FROM owner_login_throttle")
            # Remove only expired/revoked metadata; never the stable viewer or library.
            connection.execute(
                "DELETE FROM viewer_sessions WHERE audience='onion_owner' "
                "AND (revoked_at IS NOT NULL OR expires_at<=?)",
                (session.created_at,),
            )
        return True

    def authenticate(self, token_hash: str, *, now: str) -> ViewerSessionRecord | None:
        with self._database.transaction() as connection:
            row = connection.execute(
                """SELECT s.* FROM viewer_sessions s JOIN owner_credentials o
                ON s.viewer_id=o.viewer_id AND s.credential_revision=o.revision
                WHERE s.token_hash=? AND s.audience='onion_owner'
                AND s.revoked_at IS NULL AND s.expires_at>?""",
                (token_hash, now),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE viewer_sessions SET last_seen_at=? WHERE session_id=?",
                    (now, row["session_id"]),
                )
        return None if row is None else _session_from_row(row)

    def sessions(self, *, now: str) -> tuple[ViewerSessionRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                """SELECT s.* FROM viewer_sessions s JOIN owner_credentials o
                ON s.viewer_id=o.viewer_id AND s.credential_revision=o.revision
                WHERE s.audience='onion_owner' AND s.revoked_at IS NULL AND s.expires_at>?
                ORDER BY s.last_seen_at DESC""",
                (now,),
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def revoke(self, session_id: str, *, now: str) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                "UPDATE viewer_sessions SET revoked_at=? WHERE session_id=? "
                "AND audience='onion_owner' AND revoked_at IS NULL",
                (now, session_id),
            )
        return cursor.rowcount == 1
