"""Application-owned durable persistence boundary.

Concrete SQLite repositories implement these protocols in ``article_reader.db``. Domain and
application modules depend only on the records and protocols declared here, never on
:mod:`sqlite3` or a connection object, so the durable job/worker orchestration in
``application/services`` stays testable with in-memory fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ReadingState(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    NEEDS_REVIEW = "needs_review"
    NEEDS_LANGUAGE = "needs_language"
    READY_FOR_VOICE = "ready_for_voice"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobKind(StrEnum):
    PREPARE = "prepare"
    SYNTHESIZE = "synthesize"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_JOB_STATES = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})


class RenditionState(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ChunkState(StrEnum):
    READY = "ready"
    EVICTED = "evicted"


@dataclass(frozen=True, slots=True)
class ViewerRecord:
    viewer_id: str
    label: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ReadingRecord:
    reading_id: str
    viewer_id: str
    submitted_url: str
    requested_language: str | None
    requested_script: str | None
    state: ReadingState
    created_at: str
    last_opened_at: str
    deleted_at: str | None = None


@dataclass(frozen=True, slots=True)
class ArticleBlockRecord:
    ordinal: int
    kind: str
    display_text: str
    speech_text: str | None
    requires_review: bool


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    article_id: str
    reading_id: str
    submitted_url: str
    final_url: str
    canonical_url: str | None
    title: str | None
    language_hint: str | None
    extraction_version: str
    needs_review: bool
    review_reasons: tuple[str, ...]
    created_at: str
    blocks: tuple[ArticleBlockRecord, ...]


@dataclass(frozen=True, slots=True)
class ArticleDecisionRecord:
    article_id: str
    detection_evidence_json: str | None
    script_evidence_json: str
    selected_language: str | None
    selected_script: str | None
    selection_reason: str
    policy_version: str
    review_accepted: bool
    updated_at: str


@dataclass(frozen=True, slots=True)
class PreparedSegmentRecord:
    ordinal: int
    speech_text: str
    speech_text_sha256: str
    source_block_ordinals: tuple[int, ...]
    includes_title: bool
    normalizer_version: str
    segmenter_version: str


@dataclass(frozen=True, slots=True)
class RenditionRecord:
    rendition_id: str
    reading_id: str
    article_id: str
    voice_id: str
    language: str
    script: str
    sample_rate_hz: int
    model_sha256: str
    config_sha256: str
    engine_version: str
    settings_json: str
    contract_hash: str
    state: RenditionState
    total_chunks: int
    manifest_revision: int
    error_code: str | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AudioChunkRecord:
    rendition_id: str
    ordinal: int
    state: ChunkState
    relative_path: str
    sha256: str
    byte_count: int
    duration_seconds: float
    publication_generation: str
    published_at: str


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    kind: JobKind
    reading_id: str
    rendition_id: str | None
    state: JobState
    stage: str | None
    attempt: int
    previous_job_id: str | None
    worker_generation: str | None
    lease_expires_at: str | None
    cancel_requested: bool
    recovery_count: int
    error_code: str | None
    error_message: str | None
    payload_json: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    viewer_id: str
    operation: str
    client_key: str
    request_hash: str
    resource_id: str | None
    response_json: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ProgressRecord:
    viewer_id: str
    reading_id: str
    rendition_id: str | None
    chunk_ordinal: int | None
    offset_seconds: float | None
    speed: float | None
    revision: int
    updated_at: str


class ViewerRepository(Protocol):
    def create(self, viewer: ViewerRecord) -> None: ...

    def issue_token(self, viewer_id: str, token_hash: str, created_at: str) -> None: ...

    def find_viewer_id_by_token(self, token_hash: str) -> str | None: ...


class ReadingRepository(Protocol):
    def create(self, reading: ReadingRecord) -> None: ...

    def get(self, reading_id: str) -> ReadingRecord | None: ...

    def get_owned(self, reading_id: str, viewer_id: str) -> ReadingRecord | None: ...

    def list_for_viewer(self, viewer_id: str, *, limit: int) -> tuple[ReadingRecord, ...]: ...

    def update_state(self, reading_id: str, state: ReadingState) -> None: ...

    def touch_opened(self, reading_id: str, *, opened_at: str) -> None: ...

    def soft_delete(self, reading_id: str, *, deleted_at: str) -> None: ...

    def count_active_for_viewer(self, viewer_id: str) -> int: ...


class ArticleRepository(Protocol):
    def create(self, article: ArticleRecord) -> None: ...

    def get_by_reading(self, reading_id: str) -> ArticleRecord | None: ...

    def save_decision(self, decision: ArticleDecisionRecord) -> None: ...

    def get_decision(self, article_id: str) -> ArticleDecisionRecord | None: ...

    def save_prepared_segments(
        self, article_id: str, segments: tuple[PreparedSegmentRecord, ...]
    ) -> None: ...

    def get_prepared_segments(self, article_id: str) -> tuple[PreparedSegmentRecord, ...]: ...


class RenditionRepository(Protocol):
    def create(self, rendition: RenditionRecord) -> None: ...

    def get(self, rendition_id: str) -> RenditionRecord | None: ...

    def find_by_contract(self, article_id: str, contract_hash: str) -> RenditionRecord | None: ...

    def list_for_reading(self, reading_id: str) -> tuple[RenditionRecord, ...]: ...

    def update_state(
        self,
        rendition_id: str,
        state: RenditionState,
        *,
        updated_at: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PublicationOutcome:
    """Result of one atomic, generation-checked chunk publication attempt."""

    committed: bool
    manifest_revision: int | None
    rendition_state: RenditionState | None
    rejected_reason: str | None = None
    """One of ``"stale_generation"``, ``"cancelled"``, or ``"duplicate"`` when not committed."""


class AudioChunkRepository(Protocol):
    def list_for_rendition(self, rendition_id: str) -> tuple[AudioChunkRecord, ...]: ...

    def get(self, rendition_id: str, ordinal: int) -> AudioChunkRecord | None: ...

    def publish_chunk(
        self,
        *,
        job_id: str,
        worker_generation: str,
        chunk: AudioChunkRecord,
        total_chunks: int,
        now: str,
    ) -> PublicationOutcome: ...


class JobRepository(Protocol):
    def create(self, job: JobRecord) -> None: ...

    def get(self, job_id: str) -> JobRecord | None: ...

    def get_owned(self, job_id: str, viewer_id: str) -> JobRecord | None: ...

    def list_for_reading(self, reading_id: str) -> tuple[JobRecord, ...]: ...

    def claim_next(
        self, *, worker_generation: str, now: str, lease_seconds: float
    ) -> JobRecord | None: ...

    def renew_lease(
        self, job_id: str, *, worker_generation: str, now: str, lease_seconds: float
    ) -> bool: ...

    def set_stage(self, job_id: str, *, worker_generation: str, stage: str, now: str) -> bool: ...

    def complete(self, job_id: str, *, worker_generation: str, now: str) -> bool: ...

    def fail(
        self,
        job_id: str,
        *,
        worker_generation: str,
        now: str,
        error_code: str,
        error_message: str,
    ) -> bool: ...

    def mark_cancelled(self, job_id: str, *, worker_generation: str, now: str) -> bool: ...

    def request_cancel(self, job_id: str, *, now: str) -> JobRecord: ...

    def is_cancel_requested(self, job_id: str) -> bool: ...

    def recover_interrupted(self, *, now: str, max_recoveries: int) -> int: ...

    def create_retry(self, old_job_id: str, new_job_id: str, *, now: str) -> JobRecord: ...

    def count_queued(self) -> int: ...


class IdempotencyRepository(Protocol):
    def get(self, viewer_id: str, operation: str, client_key: str) -> IdempotencyRecord | None: ...

    def create(self, record: IdempotencyRecord) -> None: ...


class ProgressRepository(Protocol):
    def get(self, viewer_id: str, reading_id: str) -> ProgressRecord | None: ...

    def save(self, record: ProgressRecord, *, expected_revision: int | None) -> ProgressRecord: ...


__all__ = [
    "TERMINAL_JOB_STATES",
    "ArticleBlockRecord",
    "ArticleDecisionRecord",
    "ArticleRecord",
    "ArticleRepository",
    "AudioChunkRecord",
    "AudioChunkRepository",
    "ChunkState",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "JobKind",
    "JobRecord",
    "JobRepository",
    "JobState",
    "PreparedSegmentRecord",
    "ProgressRecord",
    "ProgressRepository",
    "PublicationOutcome",
    "ReadingRecord",
    "ReadingRepository",
    "ReadingState",
    "RenditionRecord",
    "RenditionRepository",
    "RenditionState",
    "ViewerRecord",
    "ViewerRepository",
]
