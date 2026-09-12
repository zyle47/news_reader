"""Shared fixtures and seed helpers for durable-persistence and worker tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from article_reader.application.ports.persistence import (
    ArticleBlockRecord,
    ArticleDecisionRecord,
    ArticleRecord,
    PreparedSegmentRecord,
    ReadingRecord,
    ReadingState,
    ViewerRecord,
)
from article_reader.clock import now_iso
from article_reader.db.connection import Database
from article_reader.db.repositories import (
    SqliteArticleRepository,
    SqliteAudioChunkRepository,
    SqliteIdempotencyRepository,
    SqliteJobRepository,
    SqliteProgressRepository,
    SqliteReadingRepository,
    SqliteRenditionRepository,
    SqliteViewerRepository,
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "app.sqlite3")


@dataclass(slots=True)
class Repos:
    readings: SqliteReadingRepository
    articles: SqliteArticleRepository
    renditions: SqliteRenditionRepository
    audio_chunks: SqliteAudioChunkRepository
    jobs: SqliteJobRepository
    idempotency: SqliteIdempotencyRepository
    progress: SqliteProgressRepository
    viewers: SqliteViewerRepository


def make_repos(database: Database) -> Repos:
    return Repos(
        readings=SqliteReadingRepository(database),
        articles=SqliteArticleRepository(database),
        renditions=SqliteRenditionRepository(database),
        audio_chunks=SqliteAudioChunkRepository(database),
        jobs=SqliteJobRepository(database),
        idempotency=SqliteIdempotencyRepository(database),
        progress=SqliteProgressRepository(database),
        viewers=SqliteViewerRepository(database),
    )


@pytest.fixture
def repos(database: Database) -> Repos:
    return make_repos(database)


def seed_viewer(repos: Repos, viewer_id: str = "viewer-1") -> str:
    repos.viewers.create(
        ViewerRecord(viewer_id=viewer_id, label="Test browser", created_at=now_iso())
    )
    return viewer_id


def seed_reading(
    repos: Repos,
    viewer_id: str,
    reading_id: str = "reading-1",
    *,
    state: ReadingState = ReadingState.QUEUED,
    url: str = "https://example.com/article",
) -> str:
    now = now_iso()
    repos.readings.create(
        ReadingRecord(
            reading_id=reading_id,
            viewer_id=viewer_id,
            submitted_url=url,
            requested_language=None,
            requested_script=None,
            state=state,
            created_at=now,
            last_opened_at=now,
        )
    )
    return reading_id


_SCRIPT_EVIDENCE_JSON = (
    '{"script": "latin", "latin_letter_count": 40, "cyrillic_letter_count": 0, '
    '"detector_version": "fixture-v1"}'
)


def seed_article_ready_for_voice(
    repos: Repos,
    reading_id: str,
    article_id: str = "article-1",
    *,
    segment_texts: tuple[str, ...] = ("First sentence here.", "Second sentence follows."),
    language: str = "en",
    script: str = "latin",
) -> str:
    now = now_iso()
    blocks = tuple(
        ArticleBlockRecord(
            ordinal=index,
            kind="paragraph",
            display_text=text,
            speech_text=text,
            requires_review=False,
        )
        for index, text in enumerate(segment_texts)
    )
    repos.articles.create(
        ArticleRecord(
            article_id=article_id,
            reading_id=reading_id,
            submitted_url="https://example.com/article",
            final_url="https://example.com/article",
            canonical_url=None,
            title="Fixture title",
            language_hint=language,
            extraction_version="fixture-v1",
            needs_review=False,
            review_reasons=(),
            created_at=now,
            blocks=blocks,
        )
    )
    repos.articles.save_decision(
        ArticleDecisionRecord(
            article_id=article_id,
            detection_evidence_json=None,
            script_evidence_json=_SCRIPT_EVIDENCE_JSON,
            selected_language=language,
            selected_script=script,
            selection_reason="manual_override",
            policy_version="1",
            review_accepted=False,
            updated_at=now,
        )
    )
    segments = tuple(
        PreparedSegmentRecord(
            ordinal=index,
            speech_text=text,
            speech_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            source_block_ordinals=(index,),
            includes_title=False,
            normalizer_version="fixture-v1",
            segmenter_version="fixture-v1",
        )
        for index, text in enumerate(segment_texts)
    )
    repos.articles.save_prepared_segments(article_id, segments)
    repos.readings.update_state(reading_id, ReadingState.READY_FOR_VOICE)
    return article_id


__all__ = [
    "Repos",
    "database",
    "make_repos",
    "repos",
    "seed_article_ready_for_voice",
    "seed_reading",
    "seed_viewer",
]
