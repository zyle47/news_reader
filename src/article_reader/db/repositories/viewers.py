"""Durable viewer identities and revocable browser sessions."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import ViewerRecord, ViewerSessionRecord
from article_reader.db.connection import Database


def _session_from_row(row: sqlite3.Row) -> ViewerSessionRecord:
    return ViewerSessionRecord(
        session_id=str(row["session_id"]),
        viewer_id=str(row["viewer_id"]),
        label=str(row["label"]),
        token_hash=str(row["token_hash"]),
        created_at=str(row["created_at"]),
        last_seen_at=str(row["last_seen_at"]),
        expires_at=str(row["expires_at"]),
        revoked_at=None if row["revoked_at"] is None else str(row["revoked_at"]),
    )


class SqliteViewerRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, viewer: ViewerRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO viewers (viewer_id, label, created_at) VALUES (?, ?, ?)",
                (viewer.viewer_id, viewer.label, viewer.created_at),
            )

    def create_session(self, session: ViewerSessionRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO viewer_sessions (
                    session_id, token_hash, viewer_id, label, created_at,
                    last_seen_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.token_hash,
                    session.viewer_id,
                    session.label,
                    session.created_at,
                    session.last_seen_at,
                    session.expires_at,
                    session.revoked_at,
                ),
            )

    def find_session_by_token(self, token_hash: str, *, now: str) -> ViewerSessionRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM viewer_sessions
                WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        return None if row is None else _session_from_row(row)

    def list_sessions(self, viewer_id: str, *, now: str) -> tuple[ViewerSessionRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM viewer_sessions
                WHERE viewer_id = ? AND revoked_at IS NULL AND expires_at > ?
                ORDER BY last_seen_at DESC, session_id ASC
                """,
                (viewer_id, now),
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def touch_session(self, session_id: str, *, seen_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE viewer_sessions SET last_seen_at = ?
                WHERE session_id = ? AND revoked_at IS NULL AND expires_at > ?
                """,
                (seen_at, session_id, seen_at),
            )

    def revoke_session(self, viewer_id: str, session_id: str, *, revoked_at: str) -> bool:
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE viewer_sessions SET revoked_at = ?
                WHERE viewer_id = ? AND session_id = ? AND revoked_at IS NULL
                """,
                (revoked_at, viewer_id, session_id),
            )
        return cursor.rowcount == 1


__all__ = ["SqliteViewerRepository"]
