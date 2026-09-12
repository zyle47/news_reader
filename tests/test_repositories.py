"""CRUD, ownership, and constraint behavior for the non-job durable repositories."""

from __future__ import annotations

import sqlite3

import pytest

from article_reader.application.ports.persistence import (
    AudioChunkRecord,
    ChunkState,
    IdempotencyRecord,
    JobKind,
    JobRecord,
    JobState,
    ProgressRecord,
    PublicationOutcome,
    RenditionRecord,
    RenditionState,
)
from article_reader.clock import now_iso
from article_reader.db.connection import Database
from article_reader.db.errors import ConflictError
from tests.conftest import (
    Repos,
    make_repos,
    seed_article_ready_for_voice,
    seed_reading,
    seed_viewer,
)


def test_reading_ownership_and_listing(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    other_viewer = seed_viewer(repos, "viewer-2")
    reading_id = seed_reading(repos, viewer)

    assert repos.readings.get_owned(reading_id, viewer) is not None
    assert repos.readings.get_owned(reading_id, other_viewer) is None
    assert [r.reading_id for r in repos.readings.list_for_viewer(viewer, limit=10)] == [reading_id]
    assert repos.readings.list_for_viewer(other_viewer, limit=10) == ()


def test_reading_soft_delete_hides_it_from_listing(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    repos.readings.soft_delete(reading_id, deleted_at=now_iso())
    assert repos.readings.list_for_viewer(viewer, limit=10) == ()
    # A direct get still resolves it (the row still exists for audit/cleanup purposes).
    assert repos.readings.get(reading_id) is not None


def test_article_requires_an_existing_reading(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    with pytest.raises(ConflictError):
        seed_article_ready_for_voice(repos, "does-not-exist")
    # Sanity: the real reading id works.
    seed_article_ready_for_voice(repos, reading_id)


def test_article_is_unique_per_reading(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    seed_article_ready_for_voice(repos, reading_id, article_id="article-1")
    with pytest.raises(ConflictError):
        seed_article_ready_for_voice(repos, reading_id, article_id="article-2")


def test_article_round_trips_blocks_and_prepared_segments(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)

    article = repos.articles.get_by_reading(reading_id)
    assert article is not None
    assert len(article.blocks) == 2
    assert article.blocks[0].ordinal == 0

    segments = repos.articles.get_prepared_segments(article_id)
    assert len(segments) == 2
    assert segments[0].source_block_ordinals == (0,)

    decision = repos.articles.get_decision(article_id)
    assert decision is not None
    assert decision.selected_language == "en"


def _seed_rendition(repos: Repos, reading_id: str, article_id: str) -> RenditionRecord:
    now = now_iso()
    rendition = RenditionRecord(
        rendition_id="rendition-1",
        reading_id=reading_id,
        article_id=article_id,
        voice_id="voice-1",
        language="en",
        script="latin",
        sample_rate_hz=16_000,
        model_sha256="a" * 64,
        config_sha256="b" * 64,
        engine_version="1.0.0",
        settings_json="[]",
        contract_hash="contract-1",
        state=RenditionState.QUEUED,
        total_chunks=2,
        manifest_revision=0,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    repos.renditions.create(rendition)
    return rendition


def test_rendition_contract_hash_is_unique_per_article(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    _seed_rendition(repos, reading_id, article_id)

    with pytest.raises(sqlite3.IntegrityError):
        repos.renditions.create(
            RenditionRecord(
                rendition_id="rendition-2",
                reading_id=reading_id,
                article_id=article_id,
                voice_id="voice-1",
                language="en",
                script="latin",
                sample_rate_hz=16_000,
                model_sha256="a" * 64,
                config_sha256="b" * 64,
                engine_version="1.0.0",
                settings_json="[]",
                contract_hash="contract-1",
                state=RenditionState.QUEUED,
                total_chunks=2,
                manifest_revision=0,
                error_code=None,
                error_message=None,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
        )

    found = repos.renditions.find_by_contract(article_id, "contract-1")
    assert found is not None
    assert found.rendition_id == "rendition-1"


def _seed_job(repos: Repos, reading_id: str, rendition_id: str, job_id: str = "job-1") -> None:
    now = now_iso()
    repos.jobs.create(
        JobRecord(
            job_id=job_id,
            kind=JobKind.SYNTHESIZE,
            reading_id=reading_id,
            rendition_id=rendition_id,
            state=JobState.RUNNING,
            stage="claimed",
            attempt=1,
            previous_job_id=None,
            worker_generation="gen-1",
            lease_expires_at=now,
            cancel_requested=False,
            recovery_count=0,
            error_code=None,
            error_message=None,
            payload_json="{}",
            created_at=now,
            updated_at=now,
        )
    )


def test_publish_chunk_is_atomic_and_updates_manifest_revision(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id)
    _seed_job(repos, reading_id, rendition.rendition_id)

    outcome = repos.audio_chunks.publish_chunk(
        job_id="job-1",
        worker_generation="gen-1",
        chunk=AudioChunkRecord(
            rendition_id=rendition.rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=f"{rendition.rendition_id}/000000-{'c' * 64}.wav",
            sha256="c" * 64,
            byte_count=100,
            duration_seconds=1.5,
            publication_generation="gen-1",
            published_at=now_iso(),
        ),
        total_chunks=2,
        now=now_iso(),
    )
    assert isinstance(outcome, PublicationOutcome)
    assert outcome.committed is True
    assert outcome.manifest_revision == 1
    assert outcome.rendition_state is RenditionState.GENERATING

    chunks = repos.audio_chunks.list_for_rendition(rendition.rendition_id)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0


def test_publish_chunk_rejects_exact_duplicate(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id)
    _seed_job(repos, reading_id, rendition.rendition_id)

    def publish() -> PublicationOutcome:
        return repos.audio_chunks.publish_chunk(
            job_id="job-1",
            worker_generation="gen-1",
            chunk=AudioChunkRecord(
                rendition_id=rendition.rendition_id,
                ordinal=0,
                state=ChunkState.READY,
                relative_path=f"{rendition.rendition_id}/000000-{'c' * 64}.wav",
                sha256="c" * 64,
                byte_count=100,
                duration_seconds=1.5,
                publication_generation="gen-1",
                published_at=now_iso(),
            ),
            total_chunks=2,
            now=now_iso(),
        )

    first = publish()
    assert first.committed is True
    second = publish()
    assert second.committed is False
    assert second.rejected_reason == "duplicate"
    assert len(repos.audio_chunks.list_for_rendition(rendition.rendition_id)) == 1


def test_publish_chunk_rejects_stale_generation_token(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id)
    _seed_job(repos, reading_id, rendition.rendition_id)

    outcome = repos.audio_chunks.publish_chunk(
        job_id="job-1",
        worker_generation="a-different-generation",
        chunk=AudioChunkRecord(
            rendition_id=rendition.rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=f"{rendition.rendition_id}/000000-{'c' * 64}.wav",
            sha256="c" * 64,
            byte_count=100,
            duration_seconds=1.5,
            publication_generation="a-different-generation",
            published_at=now_iso(),
        ),
        total_chunks=2,
        now=now_iso(),
    )
    assert outcome.committed is False
    assert outcome.rejected_reason == "stale_generation"
    assert repos.audio_chunks.list_for_rendition(rendition.rendition_id) == ()


def test_publish_chunk_rejects_when_cancellation_requested(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id)
    _seed_job(repos, reading_id, rendition.rendition_id)
    repos.jobs.request_cancel("job-1", now=now_iso())

    outcome = repos.audio_chunks.publish_chunk(
        job_id="job-1",
        worker_generation="gen-1",
        chunk=AudioChunkRecord(
            rendition_id=rendition.rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=f"{rendition.rendition_id}/000000-{'c' * 64}.wav",
            sha256="c" * 64,
            byte_count=100,
            duration_seconds=1.5,
            publication_generation="gen-1",
            published_at=now_iso(),
        ),
        total_chunks=2,
        now=now_iso(),
    )
    assert outcome.committed is False
    assert outcome.rejected_reason == "cancelled"


def test_idempotency_same_key_same_payload_and_conflict(repos: Repos) -> None:
    record = IdempotencyRecord(
        viewer_id="viewer-1",
        operation="create_reading",
        client_key="key-1",
        request_hash="hash-a",
        resource_id="reading-1",
        response_json='{"reading_id": "reading-1"}',
        created_at=now_iso(),
    )
    repos.idempotency.create(record)
    fetched = repos.idempotency.get("viewer-1", "create_reading", "key-1")
    assert fetched is not None
    assert fetched.request_hash == "hash-a"

    with pytest.raises(ConflictError):
        repos.idempotency.create(record)


def test_progress_optimistic_concurrency(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)

    first = repos.progress.save(
        ProgressRecord(
            viewer_id=viewer,
            reading_id=reading_id,
            rendition_id=None,
            chunk_ordinal=0,
            offset_seconds=1.0,
            speed=1.0,
            revision=0,
            updated_at=now_iso(),
        ),
        expected_revision=None,
    )
    assert first.revision == 1

    with pytest.raises(ConflictError):
        repos.progress.save(
            ProgressRecord(
                viewer_id=viewer,
                reading_id=reading_id,
                rendition_id=None,
                chunk_ordinal=1,
                offset_seconds=5.0,
                speed=1.0,
                revision=0,
                updated_at=now_iso(),
            ),
            expected_revision=0,  # Stale: the real current revision is 1.
        )

    second = repos.progress.save(
        ProgressRecord(
            viewer_id=viewer,
            reading_id=reading_id,
            rendition_id=None,
            chunk_ordinal=1,
            offset_seconds=5.0,
            speed=1.0,
            revision=0,
            updated_at=now_iso(),
        ),
        expected_revision=1,
    )
    assert second.revision == 2
    assert second.chunk_ordinal == 1


def test_restart_persistence_across_database_reopen(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    path = tmp_path_factory.mktemp("restart") / "app.sqlite3"
    first_db = Database(path)
    first_repos = make_repos(first_db)
    viewer = seed_viewer(first_repos)
    reading_id = seed_reading(first_repos, viewer)
    first_db.close()

    second_db = Database(path)
    second_repos = make_repos(second_db)
    reading = second_repos.readings.get_owned(reading_id, viewer)
    assert reading is not None
    assert reading.submitted_url == "https://example.com/article"
