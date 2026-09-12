"""Durable readings: a viewer's saved item, independent of its article snapshot."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import ReadingRecord, ReadingState
from article_reader.db.connection import Database


def _row_to_record(row: sqlite3.Row) -> ReadingRecord:
    return ReadingRecord(
        reading_id=row["reading_id"],
        viewer_id=row["viewer_id"],
        submitted_url=row["submitted_url"],
        requested_language=row["requested_language"],
        requested_script=row["requested_script"],
        state=ReadingState(row["state"]),
        created_at=row["created_at"],
        last_opened_at=row["last_opened_at"],
        deleted_at=row["deleted_at"],
    )


class SqliteReadingRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, reading: ReadingRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO readings (
                    reading_id, viewer_id, submitted_url, requested_language,
                    requested_script, state, created_at, last_opened_at, deleted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reading.reading_id,
                    reading.viewer_id,
                    reading.submitted_url,
                    reading.requested_language,
                    reading.requested_script,
                    reading.state.value,
                    reading.created_at,
                    reading.last_opened_at,
                    reading.deleted_at,
                ),
            )

    def get(self, reading_id: str) -> ReadingRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM readings WHERE reading_id = ?", (reading_id,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def get_owned(self, reading_id: str, viewer_id: str) -> ReadingRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM readings WHERE reading_id = ? AND viewer_id = ? "
                "AND deleted_at IS NULL",
                (reading_id, viewer_id),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_for_viewer(self, viewer_id: str, *, limit: int) -> tuple[ReadingRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM readings
                WHERE viewer_id = ? AND deleted_at IS NULL
                ORDER BY last_opened_at DESC, reading_id DESC
                LIMIT ?
                """,
                (viewer_id, limit),
            ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def update_state(self, reading_id: str, state: ReadingState) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE readings SET state = ? WHERE reading_id = ?",
                (state.value, reading_id),
            )

    def touch_opened(self, reading_id: str, *, opened_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE readings SET last_opened_at = ? WHERE reading_id = ?",
                (opened_at, reading_id),
            )

    def soft_delete(self, reading_id: str, *, deleted_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE readings SET deleted_at = ? WHERE reading_id = ?",
                (deleted_at, reading_id),
            )

    def count_active_for_viewer(self, viewer_id: str) -> int:
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM readings
                WHERE viewer_id = ? AND deleted_at IS NULL
                    AND state IN ('queued', 'preparing', 'generating')
                """,
                (viewer_id,),
            ).fetchone()
        return int(row["count"])


__all__ = ["SqliteReadingRepository"]
