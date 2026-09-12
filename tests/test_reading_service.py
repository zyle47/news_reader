"""Application-level orchestration: submission, resolution, renditions, cancel/retry."""

from __future__ import annotations

import itertools

import pytest

from article_reader.application.ports.persistence import (
    ArticleBlockRecord,
    ArticleDecisionRecord,
    ArticleRecord,
    JobKind,
    JobState,
    ReadingState,
    RenditionState,
)
from article_reader.application.services.reading_service import (
    ReadingService,
    ReadingServiceError,
    ReadingServiceErrorCode,
)
from article_reader.clock import now_iso
from article_reader.domain.speech import Language, Script, VoiceEvaluationStatus, VoiceSpec
from article_reader.speech.registry import VoiceRegistry
from article_reader.text.segment import RuleBasedTextPreparer
from tests.conftest import Repos, seed_reading, seed_viewer


def _voice(voice_id: str = "fixture-en") -> VoiceSpec:
    return VoiceSpec(
        voice_id=voice_id,
        display_name="Fixture English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine="fake",
        engine_version="1.0.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.com/en",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        model_url="https://models.example.com/en/model.onnx",
        model_sha256="b" * 64,
        config_url="https://models.example.com/en/model.onnx.json",
        config_sha256="c" * 64,
        model_license_url="https://models.example.com/license/model",
        data_license_url="https://models.example.com/license/data",
        evaluation_status=VoiceEvaluationStatus.UNVERIFIED,
        evaluation_notes="Offline fixture.",
    )


def _approved_voice(voice_id: str = "fixture-en") -> VoiceSpec:
    return VoiceSpec(
        voice_id=voice_id,
        display_name="Fixture English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine="piper",
        engine_version="1.8.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.com/en",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        model_url="https://models.example.com/en/model.onnx",
        model_sha256="b" * 64,
        config_url="https://models.example.com/en/model.onnx.json",
        config_sha256="c" * 64,
        model_license_url="https://models.example.com/license/model",
        data_license_url="https://models.example.com/license/data",
        evaluation_status=VoiceEvaluationStatus.APPROVED,
        evaluation_notes="Fixture approval for tests.",
    )


_ids = itertools.count()


def _service(
    repos: Repos,
    *,
    voice: VoiceSpec | None = None,
    is_installed: bool = True,
    max_unfinished_per_viewer: int = 1,
    max_waiting_chains: int = 10,
) -> ReadingService:
    return ReadingService(
        readings=repos.readings,
        articles=repos.articles,
        renditions=repos.renditions,
        jobs=repos.jobs,
        idempotency=repos.idempotency,
        progress=repos.progress,
        text_preparer=RuleBasedTextPreparer(),
        voice_registry=VoiceRegistry((voice or _approved_voice(),)),
        is_installed=lambda _voice: is_installed,
        max_segment_characters=600,
        max_unfinished_per_viewer=max_unfinished_per_viewer,
        max_waiting_chains=max_waiting_chains,
        id_factory=lambda: f"id-{next(_ids)}",
    )


def test_submit_reading_rejects_non_http_url(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    with pytest.raises(ReadingServiceError) as excinfo:
        service.submit_reading(
            viewer_id="viewer-1",
            url="ftp://example.com/file",
            requested_language=None,
            requested_script=None,
            idempotency_key=None,
        )
    assert excinfo.value.code is ReadingServiceErrorCode.INVALID_URL


def test_submit_reading_creates_reading_and_prepare_job(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    result = service.submit_reading(
        viewer_id="viewer-1",
        url="https://example.com/article",
        requested_language=None,
        requested_script=None,
        idempotency_key=None,
    )
    assert result.reading.state is ReadingState.QUEUED
    assert result.job.kind is JobKind.PREPARE
    assert result.job.state is JobState.QUEUED
    assert repos.jobs.get(result.job.job_id) is not None


def test_submit_reading_idempotency_replay_and_conflict(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    first = service.submit_reading(
        viewer_id="viewer-1",
        url="https://example.com/article",
        requested_language=None,
        requested_script=None,
        idempotency_key="key-1",
    )
    replay = service.submit_reading(
        viewer_id="viewer-1",
        url="https://example.com/article",
        requested_language=None,
        requested_script=None,
        idempotency_key="key-1",
    )
    assert replay.reading.reading_id == first.reading.reading_id
    assert replay.reused is True
    assert len(repos.readings.list_for_viewer("viewer-1", limit=10)) == 1

    with pytest.raises(ReadingServiceError) as excinfo:
        service.submit_reading(
            viewer_id="viewer-1",
            url="https://example.com/different-article",
            requested_language=None,
            requested_script=None,
            idempotency_key="key-1",
        )
    assert excinfo.value.code is ReadingServiceErrorCode.IDEMPOTENCY_CONFLICT


def test_submit_reading_enforces_unfinished_chain_limit(repos: Repos) -> None:
    service = _service(repos, max_unfinished_per_viewer=1)
    seed_viewer(repos, "viewer-1")
    service.submit_reading(
        viewer_id="viewer-1",
        url="https://example.com/one",
        requested_language=None,
        requested_script=None,
        idempotency_key=None,
    )
    with pytest.raises(ReadingServiceError) as excinfo:
        service.submit_reading(
            viewer_id="viewer-1",
            url="https://example.com/two",
            requested_language=None,
            requested_script=None,
            idempotency_key=None,
        )
    assert excinfo.value.code is ReadingServiceErrorCode.REQUEST_ALREADY_ACTIVE


def _seed_awaiting_language(repos: Repos, reading_id: str, article_id: str = "article-1") -> None:
    now = now_iso()
    repos.articles.create(
        ArticleRecord(
            article_id=article_id,
            reading_id=reading_id,
            submitted_url="https://example.com/article",
            final_url="https://example.com/article",
            canonical_url=None,
            title="A Title",
            language_hint=None,
            extraction_version="fixture-v1",
            needs_review=False,
            review_reasons=(),
            created_at=now,
            blocks=(
                ArticleBlockRecord(
                    ordinal=0,
                    kind="paragraph",
                    display_text="Ovo je tekst koji je nejasan po jeziku.",
                    speech_text="Ovo je tekst koji je nejasan po jeziku.",
                    requires_review=False,
                ),
            ),
        )
    )
    repos.articles.save_decision(
        ArticleDecisionRecord(
            article_id=article_id,
            detection_evidence_json=None,
            script_evidence_json=(
                '{"script": "latin", "latin_letter_count": 30, "cyrillic_letter_count": 0, '
                '"detector_version": "fixture-v1"}'
            ),
            selected_language=None,
            selected_script=None,
            selection_reason="serbian_croatian_bosnian_ambiguous",
            policy_version="1",
            review_accepted=False,
            updated_at=now,
        )
    )
    repos.readings.update_state(reading_id, ReadingState.NEEDS_LANGUAGE)


def test_resolve_language_with_explicit_override_prepares_segments(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.NEEDS_LANGUAGE)
    _seed_awaiting_language(repos, reading_id)

    updated = service.resolve_language(
        viewer_id="viewer-1",
        reading_id=reading_id,
        requested_language=Language.SERBIAN,
        requested_script=Script.LATIN,
        accept_review=False,
    )
    assert updated.state is ReadingState.READY_FOR_VOICE
    article = repos.articles.get_by_reading(reading_id)
    assert article is not None
    segments = repos.articles.get_prepared_segments(article.article_id)
    assert len(segments) >= 1


def test_resolve_language_rejects_when_reading_not_awaiting_input(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    with pytest.raises(ReadingServiceError) as excinfo:
        service.resolve_language(
            viewer_id="viewer-1",
            reading_id=reading_id,
            requested_language=Language.ENGLISH,
            requested_script=Script.LATIN,
            accept_review=False,
        )
    assert excinfo.value.code is ReadingServiceErrorCode.INVALID_STATE


def test_create_rendition_reuses_matching_ready_rendition(repos: Repos) -> None:
    from tests.conftest import seed_article_ready_for_voice

    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    seed_article_ready_for_voice(repos, reading_id)

    first = service.create_rendition(
        viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en"
    )
    assert first.reused is False
    assert first.job is not None

    second = service.create_rendition(
        viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en"
    )
    assert second.reused is True
    assert second.rendition.rendition_id == first.rendition.rendition_id
    # No duplicate synthesize job was created for the same rendition contract.
    jobs = [
        job for job in repos.jobs.list_for_reading(reading_id) if job.kind is JobKind.SYNTHESIZE
    ]
    assert len(jobs) == 1


def test_create_rendition_rejects_unapproved_voice(repos: Repos) -> None:
    from tests.conftest import seed_article_ready_for_voice

    service = _service(repos, voice=_voice())  # unverified, not approved
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    seed_article_ready_for_voice(repos, reading_id)

    with pytest.raises(ReadingServiceError) as excinfo:
        service.create_rendition(viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en")
    assert excinfo.value.code is ReadingServiceErrorCode.VOICE_INVALID


def test_create_rendition_rejects_uninstalled_voice(repos: Repos) -> None:
    from tests.conftest import seed_article_ready_for_voice

    service = _service(repos, is_installed=False)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    seed_article_ready_for_voice(repos, reading_id)

    with pytest.raises(ReadingServiceError) as excinfo:
        service.create_rendition(viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en")
    assert excinfo.value.code is ReadingServiceErrorCode.VOICE_UNAVAILABLE


def test_cancel_and_retry_job(repos: Repos) -> None:
    from tests.conftest import seed_article_ready_for_voice

    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    seed_article_ready_for_voice(repos, reading_id)
    result = service.create_rendition(
        viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en"
    )
    assert result.job is not None

    cancelled = service.cancel_job(viewer_id="viewer-1", job_id=result.job.job_id)
    assert cancelled.state is JobState.CANCELLED
    rendition = repos.renditions.get(result.rendition.rendition_id)
    assert rendition is not None
    assert rendition.state is RenditionState.CANCELLED

    retried = service.retry_job(viewer_id="viewer-1", job_id=cancelled.job_id)
    assert retried.state is JobState.QUEUED
    assert retried.previous_job_id == cancelled.job_id
    rendition = repos.renditions.get(result.rendition.rendition_id)
    assert rendition is not None
    assert rendition.state is RenditionState.QUEUED


def test_retry_rejects_non_terminal_job(repos: Repos) -> None:
    from tests.conftest import seed_article_ready_for_voice

    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    seed_article_ready_for_voice(repos, reading_id)
    result = service.create_rendition(
        viewer_id="viewer-1", reading_id=reading_id, voice_id="fixture-en"
    )
    assert result.job is not None
    with pytest.raises(ReadingServiceError) as excinfo:
        service.retry_job(viewer_id="viewer-1", job_id=result.job.job_id)
    assert excinfo.value.code is ReadingServiceErrorCode.JOB_NOT_RETRYABLE


def test_cross_viewer_access_is_hidden_as_not_found(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    seed_viewer(repos, "viewer-2")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)

    with pytest.raises(ReadingServiceError) as excinfo:
        service.resolve_language(
            viewer_id="viewer-2",
            reading_id=reading_id,
            requested_language=Language.ENGLISH,
            requested_script=Script.LATIN,
            accept_review=False,
        )
    assert excinfo.value.code is ReadingServiceErrorCode.NOT_FOUND


def test_progress_conflict_surfaces_as_service_error(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    service.save_progress(
        viewer_id="viewer-1",
        reading_id=reading_id,
        rendition_id=None,
        chunk_ordinal=0,
        offset_seconds=1.0,
        speed=1.0,
        expected_revision=None,
    )
    with pytest.raises(ReadingServiceError) as excinfo:
        service.save_progress(
            viewer_id="viewer-1",
            reading_id=reading_id,
            rendition_id=None,
            chunk_ordinal=1,
            offset_seconds=2.0,
            speed=1.0,
            expected_revision=0,
        )
    assert excinfo.value.code is ReadingServiceErrorCode.PROGRESS_CONFLICT


def test_delete_reading_cancels_active_jobs_and_hides_from_listing(repos: Repos) -> None:
    service = _service(repos)
    seed_viewer(repos, "viewer-1")
    reading_id = seed_reading(repos, "viewer-1", state=ReadingState.QUEUED)
    service.delete_reading(viewer_id="viewer-1", reading_id=reading_id)
    assert repos.readings.list_for_viewer("viewer-1", limit=10) == ()
