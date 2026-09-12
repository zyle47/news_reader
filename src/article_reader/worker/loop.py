"""The one durable worker: sequential FIFO claiming, leases, and atomic audio publication.

This is the only place that performs network fetching or speech synthesis; the API layer
(``article_reader.application.services.reading_service``) never does either. See
``docs/DECISIONS.md`` ADR-020 for why the worker currently runs as an in-process daemon
thread rather than a separate OS process, and ADR-021 for the chunk-timeout limitation.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from article_reader.application.ports.durable_audio import AudioPublicationError, DurableAudioStore
from article_reader.application.ports.persistence import (
    ArticleDecisionRecord,
    ArticleRecord,
    ArticleRepository,
    AudioChunkRecord,
    AudioChunkRepository,
    ChunkState,
    JobKind,
    JobRecord,
    JobRepository,
    PreparedSegmentRecord,
    ReadingRepository,
    ReadingState,
    RenditionRepository,
    RenditionState,
)
from article_reader.application.ports.speech import SpeechEngine
from article_reader.application.services.article_preparation import (
    ArticlePreparationService,
    ArticlePreparationStatus,
)
from article_reader.application.services.persistence_mapping import (
    article_blocks_from_domain,
    detection_to_json,
    prepared_segments_from_domain,
    script_detection_to_json,
)
from article_reader.clock import now_iso
from article_reader.db.errors import ConflictError
from article_reader.domain.speech import (
    AudioResult,
    Language,
    Script,
    SpeechSettings,
    SpeechSettingValue,
    VoiceSpec,
)
from article_reader.speech.registry import VoiceRegistry, VoiceRegistryError

LOGGER = logging.getLogger(__name__)
_CLAIM_POLL_SECONDS = 0.5


class SynthesisTimeoutError(RuntimeError):
    """Raised when one chunk's synthesis exceeds the configured timeout."""


def _run_with_timeout[T](
    function: Callable[[], T], *, timeout_seconds: float, description: str
) -> T:
    """Run ``function`` on a daemon thread and enforce a wall-clock timeout.

    A genuinely hung native call cannot be forcibly killed from Python. Using a daemon
    thread (rather than :class:`concurrent.futures.ThreadPoolExecutor`, whose worker threads
    are joined by an ``atexit`` hook) at least guarantees a timed-out call never blocks
    process shutdown; see ADR-021 in ``docs/DECISIONS.md`` for the accepted limitation that
    the abandoned call keeps running in the background until it naturally returns.
    """

    outcome: list[T | BaseException] = []

    def target() -> None:
        try:
            outcome.append(function())
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=target, daemon=True, name=f"article-reader-{description}")
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise SynthesisTimeoutError(f"{description} exceeded {timeout_seconds:g}s")
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


@dataclass(slots=True)
class WorkerSettings:
    chunk_timeout_seconds: float
    heartbeat_seconds: float
    lease_seconds: float
    max_automatic_recoveries: int


class WorkerLoop:
    """Claims and executes at most one job at a time, sequentially, in FIFO order."""

    def __init__(
        self,
        *,
        readings: ReadingRepository,
        articles: ArticleRepository,
        renditions: RenditionRepository,
        audio_chunks: AudioChunkRepository,
        jobs: JobRepository,
        preparation_service: ArticlePreparationService,
        voice_registry: VoiceRegistry,
        engine_factory: Callable[[], SpeechEngine],
        audio_store: DurableAudioStore,
        settings: WorkerSettings,
    ) -> None:
        self._readings = readings
        self._articles = articles
        self._renditions = renditions
        self._audio_chunks = audio_chunks
        self._jobs = jobs
        self._preparation_service = preparation_service
        self._voice_registry = voice_registry
        self._engine_factory = engine_factory
        self._audio_store = audio_store
        self._settings = settings

    def run_once(self) -> bool:
        """Claim and fully execute at most one job. Returns whether one was found."""

        self._jobs.recover_interrupted(
            now=now_iso(), max_recoveries=self._settings.max_automatic_recoveries
        )
        token = uuid4().hex
        job = self._jobs.claim_next(
            worker_generation=token, now=now_iso(), lease_seconds=self._settings.lease_seconds
        )
        if job is None:
            return False
        self._execute(job, token)
        return True

    def run_forever(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                processed = self.run_once()
            except Exception:
                LOGGER.exception("Unhandled error in the durable worker loop")
                processed = True  # avoid a tight crash loop; back off like an empty queue
            if not processed:
                stop_event.wait(_CLAIM_POLL_SECONDS)

    def _execute(self, job: JobRecord, token: str) -> None:
        try:
            if job.kind is JobKind.PREPARE:
                self._run_prepare(job, token)
            else:
                self._run_synthesize(job, token)
        except Exception as error:
            LOGGER.exception("Job %s failed", job.job_id)
            if self._jobs.fail(
                job.job_id,
                worker_generation=token,
                now=now_iso(),
                error_code="INTERNAL_ERROR",
                error_message=str(error)[:500],
            ):
                self._readings.update_state(job.reading_id, ReadingState.FAILED)

    def _run_prepare(self, job: JobRecord, token: str) -> None:
        payload = json.loads(job.payload_json)
        url = payload["url"]
        reading = self._readings.get(job.reading_id)
        assert reading is not None
        requested_language = (
            Language(reading.requested_language) if reading.requested_language else None
        )
        requested_script = Script(reading.requested_script) if reading.requested_script else None

        self._readings.update_state(job.reading_id, ReadingState.PREPARING)
        if self._jobs.is_cancel_requested(job.job_id):
            if self._jobs.mark_cancelled(job.job_id, worker_generation=token, now=now_iso()):
                self._readings.update_state(job.reading_id, ReadingState.CANCELLED)
            return

        result = self._preparation_service.prepare(
            url,
            requested_language=requested_language,
            requested_script=requested_script,
            accept_article_review=False,
        )
        if self._jobs.is_cancel_requested(job.job_id):
            if self._jobs.mark_cancelled(job.job_id, worker_generation=token, now=now_iso()):
                self._readings.update_state(job.reading_id, ReadingState.CANCELLED)
            return

        article = result.ingestion.article
        article_id = uuid4().hex
        now = now_iso()
        try:
            self._articles.create(
                ArticleRecord(
                    article_id=article_id,
                    reading_id=job.reading_id,
                    submitted_url=article.submitted_url,
                    final_url=article.final_url,
                    canonical_url=article.canonical_url,
                    title=article.title,
                    language_hint=article.language_hint,
                    extraction_version=article.extraction_version,
                    needs_review=article.needs_review,
                    review_reasons=tuple(reason.value for reason in article.review_reasons),
                    created_at=now,
                    blocks=article_blocks_from_domain(article),
                )
            )
        except ConflictError:
            # Another attempt for this reading already persisted a snapshot; stand down.
            return

        self._articles.save_decision(
            ArticleDecisionRecord(
                article_id=article_id,
                detection_evidence_json=detection_to_json(result.language_detection),
                script_evidence_json=script_detection_to_json(result.script_detection),
                selected_language=(
                    result.language_selection.language.value
                    if result.language_selection.language
                    else None
                ),
                selected_script=(
                    result.language_selection.script.value
                    if result.language_selection.script
                    else None
                ),
                selection_reason=result.language_selection.reason.value,
                policy_version=result.language_selection.policy_version,
                review_accepted=False,
                updated_at=now,
            )
        )

        if result.status is ArticlePreparationStatus.READY:
            assert result.prepared_article is not None
            self._articles.save_prepared_segments(
                article_id, prepared_segments_from_domain(result.prepared_article)
            )
            new_state = ReadingState.READY_FOR_VOICE
        elif result.status is ArticlePreparationStatus.NEEDS_ARTICLE_REVIEW:
            new_state = ReadingState.NEEDS_REVIEW
        else:
            new_state = ReadingState.NEEDS_LANGUAGE

        if self._jobs.complete(job.job_id, worker_generation=token, now=now_iso()):
            self._readings.update_state(job.reading_id, new_state)

    def _load_settings_for_rendition(self, settings_json: str) -> SpeechSettings:
        raw = json.loads(settings_json)
        options = cast(
            "tuple[tuple[str, SpeechSettingValue], ...]",
            tuple(tuple(item) for item in raw),
        )
        return SpeechSettings(options)

    def _run_synthesize(self, job: JobRecord, token: str) -> None:
        assert job.rendition_id is not None
        rendition_id = job.rendition_id
        rendition = self._renditions.get(rendition_id)
        assert rendition is not None

        self._renditions.update_state(rendition_id, RenditionState.GENERATING, updated_at=now_iso())
        self._readings.update_state(job.reading_id, ReadingState.GENERATING)

        segments = self._articles.get_prepared_segments(rendition.article_id)
        already_published = {
            chunk.ordinal for chunk in self._audio_chunks.list_for_rendition(rendition_id)
        }

        try:
            voice: VoiceSpec = self._voice_registry.get(rendition.voice_id)
        except VoiceRegistryError as error:
            self._fail_synthesis(job, token, rendition_id, "VOICE_UNAVAILABLE", str(error))
            return

        settings = self._load_settings_for_rendition(rendition.settings_json)
        engine = self._engine_factory()
        last_heartbeat_monotonic = 0.0
        try:
            engine.load(voice)
            for segment in segments:
                if segment.ordinal in already_published:
                    continue
                if self._jobs.is_cancel_requested(job.job_id):
                    cancelled = self._jobs.mark_cancelled(
                        job.job_id, worker_generation=token, now=now_iso()
                    )
                    if cancelled:
                        self._renditions.update_state(
                            rendition_id, RenditionState.CANCELLED, updated_at=now_iso()
                        )
                        self._readings.update_state(job.reading_id, ReadingState.CANCELLED)
                    return

                now_monotonic = time.monotonic()
                if now_monotonic - last_heartbeat_monotonic >= self._settings.heartbeat_seconds:
                    if not self._jobs.renew_lease(
                        job.job_id,
                        worker_generation=token,
                        now=now_iso(),
                        lease_seconds=self._settings.lease_seconds,
                    ):
                        return  # Lost ownership; another attempt now owns this job.
                    last_heartbeat_monotonic = now_monotonic

                def synthesize_current_segment(
                    segment: PreparedSegmentRecord = segment,
                ) -> AudioResult:
                    return engine.synthesize(segment.speech_text, settings)

                try:
                    audio = _run_with_timeout(
                        synthesize_current_segment,
                        timeout_seconds=self._settings.chunk_timeout_seconds,
                        description=f"chunk-{segment.ordinal}",
                    )
                except SynthesisTimeoutError as error:
                    self._fail_synthesis(job, token, rendition_id, "SYNTHESIS_TIMEOUT", str(error))
                    return

                try:
                    staged = self._audio_store.stage(rendition_id, segment.ordinal, audio)
                except AudioPublicationError as error:
                    self._fail_synthesis(job, token, rendition_id, "SYNTHESIS_FAILED", str(error))
                    return

                outcome = self._audio_chunks.publish_chunk(
                    job_id=job.job_id,
                    worker_generation=token,
                    chunk=AudioChunkRecord(
                        rendition_id=rendition_id,
                        ordinal=segment.ordinal,
                        state=ChunkState.READY,
                        relative_path=staged.relative_path,
                        sha256=staged.sha256,
                        byte_count=staged.byte_count,
                        duration_seconds=staged.duration_seconds,
                        publication_generation=token,
                        published_at=now_iso(),
                    ),
                    total_chunks=len(segments),
                    now=now_iso(),
                )
                if not outcome.committed:
                    if outcome.rejected_reason == "cancelled":
                        # Cancellation landed between our pre-check and this commit attempt.
                        # The chunk file we just staged is discarded as an orphan; finalize
                        # the cancellation exactly as the pre-loop check above would have.
                        if self._jobs.mark_cancelled(
                            job.job_id, worker_generation=token, now=now_iso()
                        ):
                            self._renditions.update_state(
                                rendition_id, RenditionState.CANCELLED, updated_at=now_iso()
                            )
                            self._readings.update_state(job.reading_id, ReadingState.CANCELLED)
                        return
                    if outcome.rejected_reason != "duplicate":
                        # Stale generation token: another attempt now owns this job. Stand
                        # down without touching rendition/reading state.
                        return

            if self._jobs.complete(job.job_id, worker_generation=token, now=now_iso()):
                self._readings.update_state(job.reading_id, ReadingState.READY)
        except Exception as error:
            self._fail_synthesis(job, token, rendition_id, "SYNTHESIS_FAILED", str(error)[:500])
        finally:
            engine.close()

    def _fail_synthesis(
        self, job: JobRecord, token: str, rendition_id: str, error_code: str, message: str
    ) -> None:
        now = now_iso()
        if self._jobs.fail(
            job.job_id,
            worker_generation=token,
            now=now,
            error_code=error_code,
            error_message=message[:500],
        ):
            self._renditions.update_state(
                rendition_id,
                RenditionState.FAILED,
                updated_at=now,
                error_code=error_code,
                error_message=message[:500],
            )
            self._readings.update_state(job.reading_id, ReadingState.FAILED)


__all__ = ["SynthesisTimeoutError", "WorkerLoop", "WorkerSettings"]
