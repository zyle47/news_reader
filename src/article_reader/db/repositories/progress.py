"""Durable playback position, revisioned to reject stale concurrent writes."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import ProgressRecord
from article_reader.db.connection import Database
from article_reader.db.errors import ConflictError


def _row_to_record(row: sqlite3.Row) -> ProgressRecord:
    return ProgressRecord(
        viewer_id=row["viewer_id"],
        reading_id=row["reading_id"],
        rendition_id=row["rendition_id"],
        chunk_ordinal=row["chunk_ordinal"],
        offset_seconds=row["offset_seconds"],
        speed=row["speed"],
        revision=row["revision"],
        updated_at=row["updated_at"],
    )


class SqliteProgressRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, viewer_id: str, reading_id: str) -> ProgressRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM progress WHERE viewer_id = ? AND reading_id = ?",
                (viewer_id, reading_id),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def save(self, record: ProgressRecord, *, expected_revision: int | None) -> ProgressRecord:
        with self._database.transaction() as connection:
            existing = connection.execute(
                "SELECT revision FROM progress WHERE viewer_id = ? AND reading_id = ?",
                (record.viewer_id, record.reading_id),
            ).fetchone()
            current_revision = None if existing is None else int(existing["revision"])
            if expected_revision is not None and current_revision != expected_revision:
                raise ConflictError(
                    f"progress revision conflict: expected {expected_revision}, "
                    f"found {current_revision}"
                )
            next_revision = (current_revision or 0) + 1
            connection.execute(
                """
                INSERT INTO progress (
                    viewer_id, reading_id, rendition_id, chunk_ordinal, offset_seconds,
                    speed, revision, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (viewer_id, reading_id) DO UPDATE SET
                    rendition_id = excluded.rendition_id,
                    chunk_ordinal = excluded.chunk_ordinal,
                    offset_seconds = excluded.offset_seconds,
                    speed = excluded.speed,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (
                    record.viewer_id,
                    record.reading_id,
                    record.rendition_id,
                    record.chunk_ordinal,
                    record.offset_seconds,
                    record.speed,
                    next_revision,
                    record.updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM progress WHERE viewer_id = ? AND reading_id = ?",
                (record.viewer_id, record.reading_id),
            ).fetchone()
        return _row_to_record(row)


__all__ = ["SqliteProgressRepository"]
