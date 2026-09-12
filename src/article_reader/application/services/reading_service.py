"""Durable reading/rendition/job orchestration used by the API layer.

This service performs only cheap, synchronous, CPU-bound work (validation, DB reads/writes,
language-selection recomputation over an already-persisted article, and rendition-contract
hashing). It never fetches a URL or calls a speech engine: that work happens exclusively in
``article_reader.worker.loop.WorkerLoop`` so the API's request/response cycle stays fast and
off any long-running operation, per ``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import uuid4

from article_reader.application.ports.persistence import (
    ArticleDecisionRecord,
    ArticleRepository,
    IdempotencyRecord,
    IdempotencyRepository,
    JobKind,
    JobRecord,
    JobRepository,
    JobState,
    ProgressRecord,
    ProgressRepository,
    ReadingRecord,
    ReadingRepository,
    ReadingState,
    RenditionRecord,
    RenditionRepository,
    RenditionState,
)
from article_reader.application.ports.text import TextPreparer
from article_reader.application.services.article_preparation import build_prepared_article
from article_reader.application.services.language_selection import select_language
from article_reader.application.services.persistence_mapping import (
    detection_from_json,
    extracted_article_from_record,
    prepared_segments_from_domain,
    script_detection_from_json,
)
from article_reader.application.services.rendition_contract import compute_contract_hash
from article_reader.clock import now_iso
from article_reader.db.errors import ConflictError, NotFoundError
from article_reader.domain.speech import Language, Script, SpeechSettings, VoiceSpec
from article_reader.speech.registry import VoiceRegistry, VoiceRegistryError


class ReadingServiceErrorCode(StrEnum):
    INVALID_URL = "INVALID_URL"
    NOT_FOUND = "NOT_FOUND"
    QUEUE_FULL = "QUEUE_FULL"
    REQUEST_ALREADY_ACTIVE = "REQUEST_ALREADY_ACTIVE"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    INVALID_STATE = "INVALID_STATE"
    VOICE_INVALID = "VOICE_INVALID"
    VOICE_UNAVAILABLE = "VOICE_UNAVAILABLE"
    VOICE_LANGUAGE_MISMATCH = "VOICE_LANGUAGE_MISMATCH"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    JOB_NOT_RETRYABLE = "JOB_NOT_RETRYABLE"
    PROGRESS_CONFLICT = "PROGRESS_CONFLICT"


class ReadingServiceError(RuntimeError):
    def __init__(self, code: ReadingServiceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


_ACTIVE_READING_STATES = frozenset(
    {ReadingState.QUEUED, ReadingState.PREPARING, ReadingState.GENERATING}
)
_RENDITION_ELIGIBLE_STATES = frozenset(
    {
        ReadingState.READY_FOR_VOICE,
        ReadingState.GENERATING,
        ReadingState.READY,
        ReadingState.FAILED,
        ReadingState.CANCELLED,
    }
)


def _validate_submitted_url(url: str) -> str:
    text = url.strip()
    if not text:
        raise ReadingServiceError(ReadingServiceErrorCode.INVALID_URL, "A URL is required.")
    try:
        parsed = urlsplit(text)
    except ValueError as error:
        raise ReadingServiceError(
            ReadingServiceErrorCode.INVALID_URL, "This does not look like a valid URL."
        ) from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ReadingServiceError(
            ReadingServiceErrorCode.INVALID_URL,
            "Only public http:// or https:// article URLs are supported.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ReadingServiceError(
            ReadingServiceErrorCode.INVALID_URL, "The URL cannot contain user credentials."
        )
    return text


@dataclass(frozen=True, slots=True)
class SubmitReadingResult:
    reading: ReadingRecord
    job: JobRecord
    reused: bool


@dataclass(frozen=True, slots=True)
class RenditionCreationResult:
    rendition: RenditionRecord
    job: JobRecord | None
    reused: bool


class ReadingService:
    def __init__(
        self,
        *,
        readings: ReadingRepository,
        articles: ArticleRepository,
        renditions: RenditionRepository,
        jobs: JobRepository,
        idempotency: IdempotencyRepository,
        progress: ProgressRepository,
        text_preparer: TextPreparer,
        voice_registry: VoiceRegistry,
        is_installed: Callable[[VoiceSpec], bool],
        max_segment_characters: int,
        max_unfinished_per_viewer: int,
        max_waiting_chains: int,
        id_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._readings = readings
        self._articles = articles
        self._renditions = renditions
        self._jobs = jobs
        self._idempotency = idempotency
        self._progress = progress
        self._text_preparer = text_preparer
        self._voice_registry = voice_registry
        self._is_installed = is_installed
        self._max_segment_characters = max_segment_characters
        self._max_unfinished_per_viewer = max_unfinished_per_viewer
        self._max_waiting_chains = max_waiting_chains
        self._id_factory = id_factory

    def submit_reading(
        self,
        *,
        viewer_id: str,
        url: str,
        requested_language: Language | None,
        requested_script: Script | None,
        idempotency_key: str | None,
    ) -> SubmitReadingResult:
        validated_url = _validate_submitted_url(url)
        payload = {
            "url": validated_url,
            "language": requested_language.value if requested_language else None,
            "script": requested_script.value if requested_script else None,
        }
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()

        if idempotency_key is not None:
            existing = self._idempotency.get(viewer_id, "create_reading", idempotency_key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise ReadingServiceError(
                        ReadingServiceErrorCode.IDEMPOTENCY_CONFLICT,
                        "This idempotency key was already used with different request data.",
                    )
                assert existing.response_json is not None
                stored = json.loads(existing.response_json)
                reading = self._readings.get(stored["reading_id"])
                job = self._jobs.get(stored["job_id"])
                assert reading is not None and job is not None
                return SubmitReadingResult(reading=reading, job=job, reused=True)

        if self._readings.count_active_for_viewer(viewer_id) >= self._max_unfinished_per_viewer:
            raise ReadingServiceError(
                ReadingServiceErrorCode.REQUEST_ALREADY_ACTIVE,
                "A previous reading is still being prepared or generated. "
                "Wait for it to finish, or cancel it first.",
            )
        if self._jobs.count_queued() >= self._max_waiting_chains:
            raise ReadingServiceError(
                ReadingServiceErrorCode.QUEUE_FULL,
                "The local queue is full right now. Try again shortly.",
            )

        now = now_iso()
        reading_id = self._id_factory()
        job_id = self._id_factory()
        reading = ReadingRecord(
            reading_id=reading_id,
            viewer_id=viewer_id,
            submitted_url=validated_url,
            requested_language=requested_language.value if requested_language else None,
            requested_script=requested_script.value if requested_script else None,
            state=ReadingState.QUEUED,
            created_at=now,
            last_opened_at=now,
        )
        self._readings.create(reading)
        job = JobRecord(
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
            payload_json=json.dumps({"url": validated_url}),
            created_at=now,
            updated_at=now,
        )
        self._jobs.create(job)

        if idempotency_key is not None:
            self._idempotency.create(
                IdempotencyRecord(
                    viewer_id=viewer_id,
                    operation="create_reading",
                    client_key=idempotency_key,
                    request_hash=request_hash,
                    resource_id=reading_id,
                    response_json=json.dumps({"reading_id": reading_id, "job_id": job_id}),
                    created_at=now,
                )
            )
        return SubmitReadingResult(reading=reading, job=job, reused=False)

    def _owned_reading(self, viewer_id: str, reading_id: str) -> ReadingRecord:
        reading = self._readings.get_owned(reading_id, viewer_id)
        if reading is None:
            raise ReadingServiceError(
                ReadingServiceErrorCode.NOT_FOUND, "This reading is not available."
            )
        return reading

    def resolve_language(
        self,
        *,
        viewer_id: str,
        reading_id: str,
        requested_language: Language | None,
        requested_script: Script | None,
        accept_review: bool,
    ) -> ReadingRecord:
        reading = self._owned_reading(viewer_id, reading_id)
        if reading.state not in {ReadingState.NEEDS_REVIEW, ReadingState.NEEDS_LANGUAGE}:
            raise ReadingServiceError(
                ReadingServiceErrorCode.INVALID_STATE,
                "This reading is not waiting for a review or language decision.",
            )
        article_record = self._articles.get_by_reading(reading_id)
        if article_record is None:
            raise ReadingServiceError(
                ReadingServiceErrorCode.NOT_FOUND, "The extracted article is not available yet."
            )
        decision_record = self._articles.get_decision(article_record.article_id)
        assert decision_record is not None
        article = extracted_article_from_record(article_record)
        detection = detection_from_json(decision_record.detection_evidence_json)
        script_detection = script_detection_from_json(decision_record.script_evidence_json)

        selection = select_language(
            detection,
            script_detection,
            language_hint=article.language_hint,
            requested_language=requested_language,
            requested_script=requested_script,
        )

        now = now_iso()
        ready = not selection.requires_override and (not article.needs_review or accept_review)
        self._articles.save_decision(
            ArticleDecisionRecord(
                article_id=article_record.article_id,
                detection_evidence_json=decision_record.detection_evidence_json,
                script_evidence_json=decision_record.script_evidence_json,
                selected_language=selection.language.value if selection.language else None,
                selected_script=selection.script.value if selection.script else None,
                selection_reason=selection.reason.value,
                policy_version=selection.policy_version,
                review_accepted=accept_review,
                updated_at=now,
            )
        )
        if ready:
            assert selection.language is not None
            assert selection.script is not None
            prepared = build_prepared_article(
                article,
                language=selection.language,
                script=selection.script,
                max_segment_characters=self._max_segment_characters,
                review_accepted=accept_review,
                text_preparer=self._text_preparer,
            )
            self._articles.save_prepared_segments(
                article_record.article_id, prepared_segments_from_domain(prepared)
            )
            new_state = ReadingState.READY_FOR_VOICE
        elif article.needs_review and not accept_review:
            new_state = ReadingState.NEEDS_REVIEW
        else:
            new_state = ReadingState.NEEDS_LANGUAGE
        self._readings.update_state(reading_id, new_state)
        updated = self._readings.get_owned(reading_id, viewer_id)
        assert updated is not None
        return updated

    def create_rendition(
        self, *, viewer_id: str, reading_id: str, voice_id: str
    ) -> RenditionCreationResult:
        reading = self._owned_reading(viewer_id, reading_id)
        if reading.state not in _RENDITION_ELIGIBLE_STATES:
            raise ReadingServiceError(
                ReadingServiceErrorCode.INVALID_STATE,
                "Resolve the article's review and language before choosing a voice.",
            )
        article_record = self._articles.get_by_reading(reading_id)
        if article_record is None:
            raise ReadingServiceError(ReadingServiceErrorCode.NOT_FOUND, "Article not found.")
        segments = self._articles.get_prepared_segments(article_record.article_id)
        if not segments:
            raise ReadingServiceError(
                ReadingServiceErrorCode.INVALID_STATE,
                "This article has no prepared speech content yet.",
            )
        decision = self._articles.get_decision(article_record.article_id)
        assert decision is not None and decision.selected_language and decision.selected_script

        try:
            voice = self._voice_registry.require_approved(voice_id)
        except VoiceRegistryError as error:
            raise ReadingServiceError(
                ReadingServiceErrorCode.VOICE_INVALID, "Choose an approved voice from the list."
            ) from error
        if voice.language.value != decision.selected_language or decision.selected_script not in (
            script.value for script in voice.scripts
        ):
            raise ReadingServiceError(
                ReadingServiceErrorCode.VOICE_LANGUAGE_MISMATCH,
                "The selected voice does not support this article's language and script.",
            )
        if not self._is_installed(voice):
            raise ReadingServiceError(
                ReadingServiceErrorCode.VOICE_UNAVAILABLE,
                "The selected voice is approved but is not installed in this data directory.",
            )

        settings = SpeechSettings()
        contract_hash = compute_contract_hash(
            segments=segments,
            language=decision.selected_language,
            script=decision.selected_script,
            voice=voice,
            settings=settings,
        )
        now = now_iso()
        existing = self._renditions.find_by_contract(article_record.article_id, contract_hash)
        if existing is not None:
            if existing.state in {
                RenditionState.QUEUED,
                RenditionState.GENERATING,
                RenditionState.READY,
            }:
                active_jobs = [
                    job
                    for job in self._jobs.list_for_reading(reading_id)
                    if job.rendition_id == existing.rendition_id
                    and job.state not in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                ]
                return RenditionCreationResult(
                    rendition=existing,
                    job=active_jobs[0] if active_jobs else None,
                    reused=True,
                )
            if self._jobs.count_queued() >= self._max_waiting_chains:
                raise ReadingServiceError(
                    ReadingServiceErrorCode.QUEUE_FULL,
                    "The local queue is full right now. Try again shortly.",
                )
            job = JobRecord(
                job_id=self._id_factory(),
                kind=JobKind.SYNTHESIZE,
                reading_id=reading_id,
                rendition_id=existing.rendition_id,
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
            self._jobs.create(job)
            self._renditions.update_state(
                existing.rendition_id, RenditionState.QUEUED, updated_at=now
            )
            self._readings.update_state(reading_id, ReadingState.GENERATING)
            return RenditionCreationResult(rendition=existing, job=job, reused=True)

        if self._jobs.count_queued() >= self._max_waiting_chains:
            raise ReadingServiceError(
                ReadingServiceErrorCode.QUEUE_FULL,
                "The local queue is full right now. Try again shortly.",
            )
        rendition_id = self._id_factory()
        rendition = RenditionRecord(
            rendition_id=rendition_id,
            reading_id=reading_id,
            article_id=article_record.article_id,
            voice_id=voice.voice_id,
            language=decision.selected_language,
            script=decision.selected_script,
            sample_rate_hz=voice.sample_rate_hz,
            model_sha256=voice.model_sha256,
            config_sha256=voice.config_sha256,
            engine_version=voice.engine_version,
            settings_json=json.dumps(list(settings.options)),
            contract_hash=contract_hash,
            state=RenditionState.QUEUED,
            total_chunks=len(segments),
            manifest_revision=0,
            error_code=None,
            error_message=None,
            created_at=now,
            updated_at=now,
        )
        self._renditions.create(rendition)
        job = JobRecord(
            job_id=self._id_factory(),
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
        self._jobs.create(job)
        self._readings.update_state(reading_id, ReadingState.GENERATING)
        return RenditionCreationResult(rendition=rendition, job=job, reused=False)

    def cancel_job(self, *, viewer_id: str, job_id: str) -> JobRecord:
        job = self._jobs.get_owned(job_id, viewer_id)
        if job is None:
            raise ReadingServiceError(ReadingServiceErrorCode.NOT_FOUND, "Job not found.")
        if job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return job
        now = now_iso()
        was_queued = job.state is JobState.QUEUED
        updated = self._jobs.request_cancel(job_id, now=now)
        if was_queued and updated.state is JobState.CANCELLED:
            if updated.kind is JobKind.SYNTHESIZE and updated.rendition_id is not None:
                self._renditions.update_state(
                    updated.rendition_id, RenditionState.CANCELLED, updated_at=now
                )
            self._readings.update_state(updated.reading_id, ReadingState.CANCELLED)
        return updated

    def retry_job(self, *, viewer_id: str, job_id: str) -> JobRecord:
        old_job = self._jobs.get_owned(job_id, viewer_id)
        if old_job is None:
            raise ReadingServiceError(ReadingServiceErrorCode.NOT_FOUND, "Job not found.")
        if old_job.state not in {JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED}:
            raise ReadingServiceError(
                ReadingServiceErrorCode.JOB_NOT_RETRYABLE,
                "Only a failed, cancelled, or interrupted job can be retried.",
            )
        now = now_iso()
        new_job_id = self._id_factory()
        try:
            new_job = self._jobs.create_retry(job_id, new_job_id, now=now)
        except (NotFoundError, ValueError) as error:
            raise ReadingServiceError(
                ReadingServiceErrorCode.JOB_NOT_RETRYABLE, str(error)
            ) from error
        if new_job.kind is JobKind.SYNTHESIZE and new_job.rendition_id is not None:
            self._renditions.update_state(
                new_job.rendition_id, RenditionState.QUEUED, updated_at=now
            )
            self._readings.update_state(new_job.reading_id, ReadingState.GENERATING)
        else:
            self._readings.update_state(new_job.reading_id, ReadingState.QUEUED)
        return new_job

    def save_progress(
        self,
        *,
        viewer_id: str,
        reading_id: str,
        rendition_id: str | None,
        chunk_ordinal: int | None,
        offset_seconds: float | None,
        speed: float | None,
        expected_revision: int | None,
    ) -> ProgressRecord:
        self._owned_reading(viewer_id, reading_id)
        record = ProgressRecord(
            viewer_id=viewer_id,
            reading_id=reading_id,
            rendition_id=rendition_id,
            chunk_ordinal=chunk_ordinal,
            offset_seconds=offset_seconds,
            speed=speed,
            revision=0,
            updated_at=now_iso(),
        )
        try:
            return self._progress.save(record, expected_revision=expected_revision)
        except ConflictError as error:
            raise ReadingServiceError(
                ReadingServiceErrorCode.PROGRESS_CONFLICT, str(error)
            ) from error

    def delete_reading(self, *, viewer_id: str, reading_id: str) -> None:
        reading = self._owned_reading(viewer_id, reading_id)
        now = now_iso()
        for job in self._jobs.list_for_reading(reading_id):
            if job.state in {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING}:
                self._jobs.request_cancel(job.job_id, now=now)
        self._readings.soft_delete(reading.reading_id, deleted_at=now)


__all__ = [
    "ReadingService",
    "ReadingServiceError",
    "ReadingServiceErrorCode",
    "RenditionCreationResult",
    "SubmitReadingResult",
]
