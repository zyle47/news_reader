"""Browser/viewer ownership seam, durable even while the server stays loopback-only."""

from __future__ import annotations

from article_reader.application.ports.persistence import ViewerRecord
from article_reader.db.connection import Database


class SqliteViewerRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, viewer: ViewerRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO viewers (viewer_id, label, created_at) VALUES (?, ?, ?)",
                (viewer.viewer_id, viewer.label, viewer.created_at),
            )

    def issue_token(self, viewer_id: str, token_hash: str, created_at: str) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                "INSERT INTO viewer_tokens (token_hash, viewer_id, created_at) VALUES (?, ?, ?)",
                (token_hash, viewer_id, created_at),
            )

    def find_viewer_id_by_token(self, token_hash: str) -> str | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT viewer_id FROM viewer_tokens WHERE token_hash = ? AND revoked_at IS NULL",
                (token_hash,),
            ).fetchone()
        return None if row is None else str(row["viewer_id"])


__all__ = ["SqliteViewerRepository"]
