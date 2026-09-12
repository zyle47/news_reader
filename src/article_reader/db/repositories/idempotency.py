"""Idempotency keys bound to viewer, operation, and canonical request payload."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import IdempotencyRecord
from article_reader.db.connection import Database
from article_reader.db.errors import ConflictError


def _row_to_record(row: sqlite3.Row) -> IdempotencyRecord:
    return IdempotencyRecord(
        viewer_id=row["viewer_id"],
        operation=row["operation"],
        client_key=row["client_key"],
        request_hash=row["request_hash"],
        resource_id=row["resource_id"],
        response_json=row["response_json"],
        created_at=row["created_at"],
    )


class SqliteIdempotencyRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, viewer_id: str, operation: str, client_key: str) -> IdempotencyRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT * FROM idempotency_keys
                WHERE viewer_id = ? AND operation = ? AND client_key = ?
                """,
                (viewer_id, operation, client_key),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def create(self, record: IdempotencyRecord) -> None:
        with self._database.transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO idempotency_keys (
                        viewer_id, operation, client_key, request_hash, resource_id,
                        response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.viewer_id,
                        record.operation,
                        record.client_key,
                        record.request_hash,
                        record.resource_id,
                        record.response_json,
                        record.created_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError(
                    f"idempotency key already recorded for viewer {record.viewer_id!r} "
                    f"operation {record.operation!r}"
                ) from error


__all__ = ["SqliteIdempotencyRepository"]
