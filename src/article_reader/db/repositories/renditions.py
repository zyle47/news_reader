"""Renditions: one specific audio interpretation of an immutable article snapshot."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import RenditionRecord, RenditionState
from article_reader.db.connection import Database


def _row_to_record(row: sqlite3.Row) -> RenditionRecord:
    return RenditionRecord(
        rendition_id=row["rendition_id"],
        reading_id=row["reading_id"],
        article_id=row["article_id"],
        voice_id=row["voice_id"],
        language=row["language"],
        script=row["script"],
        sample_rate_hz=row["sample_rate_hz"],
        model_sha256=row["model_sha256"],
        config_sha256=row["config_sha256"],
        engine_version=row["engine_version"],
        settings_json=row["settings_json"],
        contract_hash=row["contract_hash"],
        state=RenditionState(row["state"]),
        total_chunks=row["total_chunks"],
        manifest_revision=row["manifest_revision"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SqliteRenditionRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, rendition: RenditionRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO renditions (
                    rendition_id, reading_id, article_id, voice_id, language, script,
                    sample_rate_hz, model_sha256, config_sha256, engine_version,
                    settings_json, contract_hash, state, total_chunks, manifest_revision,
                    error_code, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rendition.rendition_id,
                    rendition.reading_id,
                    rendition.article_id,
                    rendition.voice_id,
                    rendition.language,
                    rendition.script,
                    rendition.sample_rate_hz,
                    rendition.model_sha256,
                    rendition.config_sha256,
                    rendition.engine_version,
                    rendition.settings_json,
                    rendition.contract_hash,
                    rendition.state.value,
                    rendition.total_chunks,
                    rendition.manifest_revision,
                    rendition.error_code,
                    rendition.error_message,
                    rendition.created_at,
                    rendition.updated_at,
                ),
            )

    def get(self, rendition_id: str) -> RenditionRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM renditions WHERE rendition_id = ?", (rendition_id,)
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def find_by_contract(self, article_id: str, contract_hash: str) -> RenditionRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM renditions WHERE article_id = ? AND contract_hash = ?",
                (article_id, contract_hash),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_for_reading(self, reading_id: str) -> tuple[RenditionRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM renditions WHERE reading_id = ? ORDER BY created_at DESC",
                (reading_id,),
            ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def update_state(
        self,
        rendition_id: str,
        state: RenditionState,
        *,
        updated_at: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE renditions
                SET state = ?, updated_at = ?, error_code = ?, error_message = ?
                WHERE rendition_id = ?
                """,
                (state.value, updated_at, error_code, error_message, rendition_id),
            )


__all__ = ["SqliteRenditionRepository"]
