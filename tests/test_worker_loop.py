"""End-to-end durable worker execution: prepare, synthesize, cancellation, recovery."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from article_reader.application.ports.durable_audio import (
    AudioPublicationError,
    DurableAudioStore,
    StagedAudioArtifact,
)
from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.ports.persistence import (
    JobKind,
    JobRecord,
    JobState,
    ReadingState,
    RenditionRecord,
    RenditionState,
)
from article_reader.application.ports.speech import SpeechEngine
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import ArticlePreparationService
from article_reader.clock import now_iso
from article_reader.domain.article import ArticleBlock, ArticleBlockKind, ExtractedArticle
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.speech import (
    AudioResult,
    Language,
    Script,
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.speech.fake import FakeSpeechEngine
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.durable_audio import LocalDurableAudioStore
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer
from article_reader.worker.loop import WorkerLoop, WorkerSettings
from tests.conftest import Repos, seed_article_ready_for_voice, seed_reading, seed_viewer

_URL = "https://example.com/article"


def _page(url: str = _URL) -> FetchedPage:
    body = b"<html><article>fixture</article></html>"
    return FetchedPage(
        submitted_url=url,
        final_url=url,
        redirect_chain=(url,),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )


def _article(needs_review: bool = False) -> ExtractedArticle:
    return ExtractedArticle(
        submitted_url=_URL,
        final_url=_URL,
        canonical_url=None,
        title="Fixture Title",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="The first useful paragraph has enough words here.",
                speech_text="The first useful paragraph has enough words here.",
            ),
            ArticleBlock(
                ordinal=1,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="A second paragraph continues the article nicely.",
                speech_text="A second paragraph continues the article nicely.",
            ),
        ),
        review_reasons=(),
        extraction_version="fixture-v1",
    )


class _Ingestor:
    def __init__(self, article: ExtractedArticle | None = None) -> None:
        self._article = article or _article()
        self.calls = 0

    def ingest(self, submitted_url: str) -> ArticleIngestionResult:
        self.calls += 1
        return ArticleIngestionResult(page=_page(submitted_url), article=self._article)


class _Detector:
    def detect(self, text: str) -> LanguageDetection:
        return LanguageDetection(
            candidates=(LanguageCandidate("en", 0.99), LanguageCandidate("de", 0.01)),
            detector_version="fixture-v1",
            sample_character_count=len(text),
            sample_alphabetic_count=sum(character.isalpha() for character in text),
            sample_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )


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


def _worker(
    repos: Repos,
    tmp_path: Path,
    *,
    ingestor: _Ingestor | None = None,
    engine_factory: Callable[[], SpeechEngine] = FakeSpeechEngine,
    audio_store: DurableAudioStore | None = None,
    chunk_timeout_seconds: float = 30,
    heartbeat_seconds: float = 5,
    lease_seconds: float = 60,
    max_automatic_recoveries: int = 1,
) -> WorkerLoop:
    preparation_service = ArticlePreparationService(
        ingestor or _Ingestor(),
        _Detector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=600,
    )
    return WorkerLoop(
        readings=repos.readings,
        articles=repos.articles,
        renditions=repos.renditions,
        audio_chunks=repos.audio_chunks,
        jobs=repos.jobs,
        preparation_service=preparation_service,
        voice_registry=VoiceRegistry((_voice(),)),
        engine_factory=engine_factory,
        audio_store=audio_store or LocalDurableAudioStore(tmp_path),
        settings=WorkerSettings(
            chunk_timeout_seconds=chunk_timeout_seconds,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
            max_automatic_recoveries=max_automatic_recoveries,
        ),
    )


def _seed_prepare_job(
    repos: Repos, reading_id: str, job_id: str = "job-1", url: str = _URL
) -> None:
    now = now_iso()
    repos.jobs.create(
        JobRecord(
            job_id=job_id,
            kind=JobKind.PREPARE,
            reading_id=reading_id,
            rendition_id=None,
            state=JobState.QUEUED,
            stage=None,
            attempt=1,
            previous_job_id=None,
            worker_generation=None,
            lease_expires_at=None,
            cancel_requested=False,
            recovery_count=0,
            error_code=None,
            error_message=None,
            payload_json=json.dumps({"url": url}),
            created_at=now,
            updated_at=now,
        )
    )


def _seed_synthesize_job(
    repos: Repos, reading_id: str, rendition_id: str, job_id: str = "synth-job-1"
) -> None:
    now = now_iso()
    repos.jobs.create(
        JobRecord(
            job_id=job_id,
            kind=JobKind.SYNTHESIZE,
            reading_id=reading_id,
            rendition_id=rendition_id,
            state=JobState.QUEUED,
            stage=None,
            attempt=1,
            previous_job_id=None,
            worker_generation=None,
            lease_expires_at=None,
            cancel_requested=False,
            recovery_count=0,
            error_code=None,
            error_message=None,
            payload_json="{}",
            created_at=now,
            updated_at=now,
        )
    )


def _seed_rendition(
    repos: Repos,
    reading_id: str,
    article_id: str,
    total_chunks: int,
    rendition_id: str = "1" * 32,
) -> RenditionRecord:
    now = now_iso()
    rendition = RenditionRecord(
        rendition_id=rendition_id,
        reading_id=reading_id,
        article_id=article_id,
        voice_id="fixture-en",
        language="en",
        script="latin",
        sample_rate_hz=16_000,
        model_sha256="b" * 64,
        config_sha256="c" * 64,
        engine_version="1.0.0",
        settings_json=json.dumps(list(SpeechSettings().options)),
        contract_hash="contract-1",
        state=RenditionState.QUEUED,
        total_chunks=total_chunks,
        manifest_revision=0,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )
    repos.renditions.create(rendition)
    return rendition


def test_prepare_job_produces_ready_for_voice_reading(repos: Repos, tmp_path: Path) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.QUEUED)
    _seed_prepare_job(repos, reading_id)
    worker = _worker(repos, tmp_path)

    assert worker.run_once() is True

    reading = repos.readings.get(reading_id)
    assert reading is not None
    assert reading.state is ReadingState.READY_FOR_VOICE
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.COMPLETED
    article = repos.articles.get_by_reading(reading_id)
    assert article is not None
    segments = repos.articles.get_prepared_segments(article.article_id)
    assert len(segments) >= 1
    assert segments[0].source_block_ordinals[0] == 0
    assert segments[-1].source_block_ordinals[-1] == 1


def test_prepare_job_needing_review_stops_before_synthesis(repos: Repos, tmp_path: Path) -> None:
    from article_reader.domain.article import ArticleReviewReason

    reviewed_article = ExtractedArticle(
        submitted_url=_URL,
        final_url=_URL,
        canonical_url=None,
        title=None,
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.CODE,
                display_text="print('omitted')",
                speech_text=None,
                requires_review=True,
            ),
        ),
        review_reasons=(ArticleReviewReason.COMPLEX_CONTENT,),
        extraction_version="fixture-v1",
    )
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.QUEUED)
    _seed_prepare_job(repos, reading_id)
    worker = _worker(repos, tmp_path, ingestor=_Ingestor(reviewed_article))

    worker.run_once()

    reading = repos.readings.get(reading_id)
    assert reading is not None
    assert reading.state is ReadingState.NEEDS_REVIEW


def test_synthesize_job_publishes_chunks_progressively(repos: Repos, tmp_path: Path) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=2)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id)
    worker = _worker(repos, tmp_path)

    assert worker.run_once() is True

    updated_rendition = repos.renditions.get(rendition.rendition_id)
    assert updated_rendition is not None
    assert updated_rendition.state is RenditionState.READY
    chunks = repos.audio_chunks.list_for_rendition(rendition.rendition_id)
    assert len(chunks) == 2
    assert [chunk.ordinal for chunk in chunks] == [0, 1]
    reading = repos.readings.get(reading_id)
    assert reading is not None
    assert reading.state is ReadingState.READY


def test_synthesize_resumes_from_first_unpublished_chunk_on_retry(
    repos: Repos, tmp_path: Path
) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=2)

    class _FailOnSecondChunkStore(LocalDurableAudioStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.calls = 0

        def stage(self, rendition_id: str, ordinal: int, audio: AudioResult) -> StagedAudioArtifact:
            self.calls += 1
            if ordinal == 1:
                raise AudioPublicationError("simulated corrupt write")
            return super().stage(rendition_id, ordinal, audio)

    store = _FailOnSecondChunkStore(tmp_path)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id, "synth-1")
    worker = _worker(repos, tmp_path, audio_store=store)
    worker.run_once()

    job = repos.jobs.get("synth-1")
    assert job is not None
    assert job.state is JobState.FAILED
    assert len(repos.audio_chunks.list_for_rendition(rendition.rendition_id)) == 1

    # Retry: only the missing second chunk should be synthesized again.
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id, "synth-2")
    worker_retry = _worker(repos, tmp_path)  # Real store this time.
    worker_retry.run_once()

    chunks = repos.audio_chunks.list_for_rendition(rendition.rendition_id)
    assert len(chunks) == 2
    rendition_after = repos.renditions.get(rendition.rendition_id)
    assert rendition_after is not None
    assert rendition_after.state is RenditionState.READY


def test_cancellation_mid_synthesis_keeps_ready_prefix_playable(
    repos: Repos, tmp_path: Path
) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(
        repos, reading_id, segment_texts=("First.", "Second.", "Third.")
    )
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=3)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id)

    class _CancelAfterFirstChunkEngine(FakeSpeechEngine):
        def __init__(self, repos: Repos) -> None:
            super().__init__()
            self._repos = repos

        def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
            if self.synthesis_count == 1:
                self._repos.jobs.request_cancel("synth-job-1", now=now_iso())
            return super().synthesize(text, settings)

    worker = _worker(repos, tmp_path, engine_factory=lambda: _CancelAfterFirstChunkEngine(repos))
    worker.run_once()

    job = repos.jobs.get("synth-job-1")
    assert job is not None
    assert job.state is JobState.CANCELLED
    rendition_after = repos.renditions.get(rendition.rendition_id)
    assert rendition_after is not None
    assert rendition_after.state is RenditionState.CANCELLED
    # The first chunk that finished before cancellation was observed remains published.
    chunks = repos.audio_chunks.list_for_rendition(rendition.rendition_id)
    assert len(chunks) == 1
    assert chunks[0].ordinal == 0


def test_worker_crash_then_recovery_resumes_without_duplicating_chunks(
    repos: Repos, tmp_path: Path
) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=2)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id)

    # Simulate a crash: claim the job with a lease that is already expired, but never
    # finish it (as if the process died mid-flight without publishing anything).
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    repos.jobs.claim_next(worker_generation="dead-worker", now=past, lease_seconds=0.01)

    worker = _worker(repos, tmp_path, max_automatic_recoveries=1)
    assert worker.run_once() is True  # recovers the interrupted job, then claims+runs it

    job = repos.jobs.get("synth-job-1")
    assert job is not None
    assert job.state is JobState.COMPLETED
    assert job.recovery_count == 1
    chunks = repos.audio_chunks.list_for_rendition(rendition.rendition_id)
    assert len(chunks) == 2


def test_synthesis_timeout_fails_the_job_and_rendition(repos: Repos, tmp_path: Path) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=2)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id)

    class _HangingEngine(FakeSpeechEngine):
        def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
            time.sleep(1)
            return super().synthesize(text, settings)

    worker = _worker(repos, tmp_path, engine_factory=_HangingEngine, chunk_timeout_seconds=0.05)
    worker.run_once()

    job = repos.jobs.get("synth-job-1")
    assert job is not None
    assert job.state is JobState.FAILED
    assert job.error_code == "SYNTHESIS_TIMEOUT"
    rendition_after = repos.renditions.get(rendition.rendition_id)
    assert rendition_after is not None
    assert rendition_after.state is RenditionState.FAILED


def test_stale_worker_generation_cannot_publish_after_losing_lease(
    repos: Repos, tmp_path: Path
) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer, state=ReadingState.READY_FOR_VOICE)
    article_id = seed_article_ready_for_voice(repos, reading_id)
    rendition = _seed_rendition(repos, reading_id, article_id, total_chunks=1)
    _seed_synthesize_job(repos, reading_id, rendition.rendition_id)

    # Claim with a very short lease, let it expire, and let a second worker take over
    # before the first "finishes" (simulated directly via the repository, since the
    # worker loop itself always uses a fresh per-claim token).
    from datetime import UTC, datetime, timedelta

    past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    stale_job = repos.jobs.claim_next(worker_generation="stale-gen", now=past, lease_seconds=0.01)
    assert stale_job is not None
    repos.jobs.recover_interrupted(now=now_iso(), max_recoveries=5)
    fresh_job = repos.jobs.claim_next(
        worker_generation="fresh-gen", now=now_iso(), lease_seconds=60
    )
    assert fresh_job is not None

    from article_reader.application.ports.persistence import AudioChunkRecord, ChunkState

    outcome = repos.audio_chunks.publish_chunk(
        job_id="synth-job-1",
        worker_generation="stale-gen",
        chunk=AudioChunkRecord(
            rendition_id=rendition.rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=f"{rendition.rendition_id}/000000-{'d' * 64}.wav",
            sha256="d" * 64,
            byte_count=10,
            duration_seconds=1.0,
            publication_generation="stale-gen",
            published_at=now_iso(),
        ),
        total_chunks=1,
        now=now_iso(),
    )
    assert outcome.committed is False
    assert outcome.rejected_reason == "stale_generation"
