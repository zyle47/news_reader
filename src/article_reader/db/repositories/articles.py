"""Immutable article snapshots, extracted blocks, review/language decisions, and segments."""

from __future__ import annotations

import json
import sqlite3

from article_reader.application.ports.persistence import (
    ArticleBlockRecord,
    ArticleDecisionRecord,
    ArticleRecord,
    PreparedSegmentRecord,
)
from article_reader.db.connection import Database
from article_reader.db.errors import ConflictError


def _blocks_from_rows(rows: list[sqlite3.Row]) -> tuple[ArticleBlockRecord, ...]:
    return tuple(
        ArticleBlockRecord(
            ordinal=row["ordinal"],
            kind=row["kind"],
            display_text=row["display_text"],
            speech_text=row["speech_text"],
            requires_review=bool(row["requires_review"]),
        )
        for row in rows
    )


class SqliteArticleRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, article: ArticleRecord) -> None:
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO articles (
                        article_id, reading_id, submitted_url, final_url, canonical_url, title,
                        language_hint, extraction_version, needs_review, review_reasons_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article.article_id,
                        article.reading_id,
                        article.submitted_url,
                        article.final_url,
                        article.canonical_url,
                        article.title,
                        article.language_hint,
                        article.extraction_version,
                        int(article.needs_review),
                        json.dumps(list(article.review_reasons)),
                        article.created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO article_blocks (
                        article_id, ordinal, kind, display_text, speech_text, requires_review
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            article.article_id,
                            block.ordinal,
                            block.kind,
                            block.display_text,
                            block.speech_text,
                            int(block.requires_review),
                        )
                        for block in article.blocks
                    ],
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError(
                f"reading {article.reading_id!r} already has an article snapshot"
            ) from error

    def get_by_reading(self, reading_id: str) -> ArticleRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM articles WHERE reading_id = ?", (reading_id,)
            ).fetchone()
            if row is None:
                return None
            block_rows = connection.execute(
                "SELECT * FROM article_blocks WHERE article_id = ? ORDER BY ordinal",
                (row["article_id"],),
            ).fetchall()
        return ArticleRecord(
            article_id=row["article_id"],
            reading_id=row["reading_id"],
            submitted_url=row["submitted_url"],
            final_url=row["final_url"],
            canonical_url=row["canonical_url"],
            title=row["title"],
            language_hint=row["language_hint"],
            extraction_version=row["extraction_version"],
            needs_review=bool(row["needs_review"]),
            review_reasons=tuple(json.loads(row["review_reasons_json"])),
            created_at=row["created_at"],
            blocks=_blocks_from_rows(list(block_rows)),
        )

    def save_decision(self, decision: ArticleDecisionRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO article_decisions (
                    article_id, detection_evidence_json, script_evidence_json,
                    selected_language, selected_script, selection_reason, policy_version,
                    review_accepted, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (article_id) DO UPDATE SET
                    detection_evidence_json = excluded.detection_evidence_json,
                    script_evidence_json = excluded.script_evidence_json,
                    selected_language = excluded.selected_language,
                    selected_script = excluded.selected_script,
                    selection_reason = excluded.selection_reason,
                    policy_version = excluded.policy_version,
                    review_accepted = excluded.review_accepted,
                    updated_at = excluded.updated_at
                """,
                (
                    decision.article_id,
                    decision.detection_evidence_json,
                    decision.script_evidence_json,
                    decision.selected_language,
                    decision.selected_script,
                    decision.selection_reason,
                    decision.policy_version,
                    int(decision.review_accepted),
                    decision.updated_at,
                ),
            )

    def get_decision(self, article_id: str) -> ArticleDecisionRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT * FROM article_decisions WHERE article_id = ?", (article_id,)
            ).fetchone()
        if row is None:
            return None
        return ArticleDecisionRecord(
            article_id=row["article_id"],
            detection_evidence_json=row["detection_evidence_json"],
            script_evidence_json=row["script_evidence_json"],
            selected_language=row["selected_language"],
            selected_script=row["selected_script"],
            selection_reason=row["selection_reason"],
            policy_version=row["policy_version"],
            review_accepted=bool(row["review_accepted"]),
            updated_at=row["updated_at"],
        )

    def save_prepared_segments(
        self, article_id: str, segments: tuple[PreparedSegmentRecord, ...]
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM prepared_segments WHERE article_id = ?", (article_id,))
            connection.executemany(
                """
                INSERT INTO prepared_segments (
                    article_id, ordinal, speech_text, speech_text_sha256,
                    source_block_ordinals_json, includes_title, normalizer_version,
                    segmenter_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        article_id,
                        segment.ordinal,
                        segment.speech_text,
                        segment.speech_text_sha256,
                        json.dumps(list(segment.source_block_ordinals)),
                        int(segment.includes_title),
                        segment.normalizer_version,
                        segment.segmenter_version,
                    )
                    for segment in segments
                ],
            )

    def get_prepared_segments(self, article_id: str) -> tuple[PreparedSegmentRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM prepared_segments WHERE article_id = ? ORDER BY ordinal",
                (article_id,),
            ).fetchall()
        return tuple(
            PreparedSegmentRecord(
                ordinal=row["ordinal"],
                speech_text=row["speech_text"],
                speech_text_sha256=row["speech_text_sha256"],
                source_block_ordinals=tuple(json.loads(row["source_block_ordinals_json"])),
                includes_title=bool(row["includes_title"]),
                normalizer_version=row["normalizer_version"],
                segmenter_version=row["segmenter_version"],
            )
            for row in rows
        )


__all__ = ["SqliteArticleRepository"]
