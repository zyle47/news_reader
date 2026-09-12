"""Ordered, durably published audio chunks.

``publish_chunk`` is the one place a synthesis worker commits a completed audio file to the
database. It re-checks the owning job's lease/generation token and cancellation flag inside
the same transaction that inserts the row, so a worker that lost its lease (or whose job was
cancelled) between finishing synthesis and committing can never publish stale audio: see
ADR-020 in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import (
    AudioChunkRecord,
    ChunkState,
    PublicationOutcome,
    RenditionState,
)
from article_reader.db.connection import Database


def _row_to_record(row: sqlite3.Row) -> AudioChunkRecord:
    return AudioChunkRecord(
        rendition_id=row["rendition_id"],
        ordinal=row["ordinal"],
        state=ChunkState(row["state"]),
        relative_path=row["relative_path"],
        sha256=row["sha256"],
        byte_count=row["byte_count"],
        duration_seconds=row["duration_seconds"],
        publication_generation=row["publication_generation"],
        published_at=row["published_at"],
    )


class SqliteAudioChunkRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def list_for_rendition(self, rendition_id: str) -> tuple[AudioChunkRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM audio_chunks WHERE rendition_id = ? ORDER BY ordinal",
                (rendition_id,),
            ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def get(self, rendition_id: str, ordinal: int) -> AudioChunkRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM audio_chunks WHERE rendition_id = ? AND ordinal = ?",
                (rendition_id, ordinal),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def publish_chunk(
        self,
        *,
        job_id: str,
        worker_generation: str,
        chunk: AudioChunkRecord,
        total_chunks: int,
        now: str,
    ) -> PublicationOutcome:
        with self._database.transaction() as connection:
            job_row = connection.execute(
                "SELECT state, worker_generation, cancel_requested FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if (
                job_row is None
                or job_row["worker_generation"] != worker_generation
                or job_row["state"] not in ("running", "cancelling")
            ):
                return PublicationOutcome(
                    committed=False,
                    manifest_revision=None,
                    rendition_state=None,
                    rejected_reason="stale_generation",
                )
            if job_row["cancel_requested"]:
                return PublicationOutcome(
                    committed=False,
                    manifest_revision=None,
                    rendition_state=None,
                    rejected_reason="cancelled",
                )

            existing = connection.execute(
                "SELECT 1 FROM audio_chunks WHERE rendition_id = ? AND ordinal = ?",
                (chunk.rendition_id, chunk.ordinal),
            ).fetchone()
            if existing is not None:
                return PublicationOutcome(
                    committed=False,
                    manifest_revision=None,
                    rendition_state=None,
                    rejected_reason="duplicate",
                )

            connection.execute(
                """
                INSERT INTO audio_chunks (
                    rendition_id, ordinal, state, relative_path, sha256, byte_count,
                    duration_seconds, publication_generation, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chunk.rendition_id,
                    chunk.ordinal,
                    chunk.state.value,
                    chunk.relative_path,
                    chunk.sha256,
                    chunk.byte_count,
                    chunk.duration_seconds,
                    chunk.publication_generation,
                    chunk.published_at,
                ),
            )
            published_count_row = connection.execute(
                "SELECT COUNT(*) AS count FROM audio_chunks WHERE rendition_id = ?",
                (chunk.rendition_id,),
            ).fetchone()
            published_count = int(published_count_row["count"])
            rendition_state = (
                RenditionState.READY
                if published_count >= total_chunks
                else RenditionState.GENERATING
            )
            connection.execute(
                """
                UPDATE renditions
                SET manifest_revision = manifest_revision + 1, state = ?, updated_at = ?
                WHERE rendition_id = ?
                """,
                (rendition_state.value, now, chunk.rendition_id),
            )
            revision_row = connection.execute(
                "SELECT manifest_revision FROM renditions WHERE rendition_id = ?",
                (chunk.rendition_id,),
            ).fetchone()
            return PublicationOutcome(
                committed=True,
                manifest_revision=int(revision_row["manifest_revision"]),
                rendition_state=rendition_state,
            )


__all__ = ["SqliteAudioChunkRepository"]
