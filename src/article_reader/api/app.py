"""FastAPI composition and contracts for the durable, loopback browser reader.

The API process only ever validates input, reads/writes SQLite through the repositories in
``article_reader.db``, and serves already-published audio files. It never fetches a URL or
calls a speech engine: that work happens exclusively in the durable worker
(``article_reader.worker.loop.WorkerLoop``), composed and started separately in ``cli.py``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from article_reader import __version__
from article_reader.application.ports.durable_audio import DurableAudioStore
from article_reader.application.ports.persistence import (
    ArticleDecisionRecord,
    ArticleRecord,
    AudioChunkRecord,
    JobRecord,
    JobState,
    PreparedSegmentRecord,
    ReadingRecord,
    RenditionRecord,
    ViewerRecord,
)
from article_reader.application.services.reading_service import (
    ReadingService,
    ReadingServiceError,
    ReadingServiceErrorCode,
)
from article_reader.clock import now_iso
from article_reader.config import Settings, ensure_runtime_dirs
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
from article_reader.domain.speech import Language, Script, VoiceSpec
from article_reader.speech.model_store import ModelStore
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.durable_audio import LocalDurableAudioStore
from article_reader.text.segment import RuleBasedTextPreparer

LOGGER = logging.getLogger(__name__)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_VIEWER_COOKIE = "article_reader_viewer"
_VIEWER_COOKIE_MAX_AGE_SECONDS = 400 * 24 * 60 * 60

_ERROR_STATUS: dict[ReadingServiceErrorCode, int] = {
    ReadingServiceErrorCode.INVALID_URL: 422,
    ReadingServiceErrorCode.NOT_FOUND: 404,
    ReadingServiceErrorCode.QUEUE_FULL: 429,
    ReadingServiceErrorCode.REQUEST_ALREADY_ACTIVE: 429,
    ReadingServiceErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ReadingServiceErrorCode.INVALID_STATE: 409,
    ReadingServiceErrorCode.VOICE_INVALID: 422,
    ReadingServiceErrorCode.VOICE_UNAVAILABLE: 503,
    ReadingServiceErrorCode.VOICE_LANGUAGE_MISMATCH: 422,
    ReadingServiceErrorCode.JOB_NOT_CANCELLABLE: 409,
    ReadingServiceErrorCode.JOB_NOT_RETRYABLE: 409,
    ReadingServiceErrorCode.PROGRESS_CONFLICT: 409,
}


class SubmitReadingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4_096)
    language: Literal["auto", "en", "de", "sr"] = "auto"
    script: Literal["latin", "cyrillic"] | None = None


class ResolveReadingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["auto", "en", "de", "sr"] = "auto"
    script: Literal["latin", "cyrillic"] | None = None
    accept_review: bool = False


class CreateRenditionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class ProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rendition_id: str | None = None
    chunk_ordinal: int | None = Field(default=None, ge=0)
    offset_seconds: float | None = Field(default=None, ge=0)
    speed: float | None = Field(default=None, gt=0)
    revision: int | None = Field(default=None, ge=0)


class ApiError(RuntimeError):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable


class LocalRequestBoundaryMiddleware:
    """Reject DNS-rebinding and cross-origin state changes at the HTTP boundary."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_hosts: frozenset[str],
        allowed_origins: frozenset[str],
    ) -> None:
        self._app = app
        self._allowed_hosts = frozenset(host.casefold() for host in allowed_hosts)
        self._allowed_origins = allowed_origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = headers.get("host", "").casefold()
        if host not in self._allowed_hosts:
            response = _error_response(400, "INVALID_HOST", "The request host is not allowed.")
            await response(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        if method in _MUTATING_METHODS:
            origin = headers.get("origin")
            if origin not in self._allowed_origins:
                response = _error_response(
                    403,
                    "ORIGIN_REJECTED",
                    "State changes are allowed only from this local reader page.",
                )
                await response(scope, receive, send)
                return
            media_type = headers.get("content-type", "").split(";", maxsplit=1)[0].strip().lower()
            if media_type != "application/json":
                response = _error_response(
                    415,
                    "JSON_REQUIRED",
                    "This endpoint requires an application/json request.",
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _copy_cookies(source: Response, destination: Response) -> None:
    """Forward any ``Set-Cookie`` header from a throwaway dependency response.

    ``dict(source.headers)`` would also copy that throwaway response's own auto-computed
    ``content-length``/``content-type`` (it is a real, if bodiless, ``Response``), which would
    then incorrectly override the real body length on ``destination``. Only cookies are ever
    intentionally set on the injected response, so only cookies are forwarded.
    """

    for value in source.headers.getlist("set-cookie"):
        destination.headers.append("set-cookie", value)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


def _language_values(language: str, script: str | None) -> tuple[Language | None, Script | None]:
    if language == "auto":
        if script is not None:
            raise ApiError(422, "LANGUAGE_OVERRIDE_INVALID", "Auto language cannot set a script.")
        return None, None
    requested_language = Language(language)
    if requested_language is Language.SERBIAN and script is None:
        raise ApiError(
            422,
            "LANGUAGE_SCRIPT_REQUIRED",
            "Choose Latin or Cyrillic when selecting Serbian.",
        )
    requested_script = Script(script) if script is not None else None
    if requested_language is not Language.SERBIAN and requested_script is Script.CYRILLIC:
        raise ApiError(
            422,
            "LANGUAGE_OVERRIDE_INVALID",
            "English and German use Latin script.",
        )
    return requested_language, requested_script


def _default_web_directory() -> Path:
    resource = resources.files("article_reader.web")
    return Path(str(resource))


def _request_boundary_values(settings: Settings) -> tuple[frozenset[str], frozenset[str]]:
    port = settings.server.port
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    origins = frozenset(f"http://{host}" for host in hosts)
    return hosts, origins


def _article_document(
    article: ArticleRecord, decision: ArticleDecisionRecord | None
) -> dict[str, object]:
    return {
        "title": article.title,
        "final_url": article.final_url,
        "language_hint": article.language_hint,
        "review": {
            "required": article.needs_review,
            "accepted": bool(decision is not None and decision.review_accepted),
            "reasons": list(article.review_reasons),
        },
        "language": (
            None
            if decision is None
            else {
                "selected_language": decision.selected_language,
                "selected_script": decision.selected_script,
                "selection_reason": decision.selection_reason,
            }
        ),
        "blocks": [
            {
                "ordinal": block.ordinal,
                "kind": block.kind,
                "display_text": block.display_text,
                "will_be_spoken": block.speech_text is not None,
                "requires_review": block.requires_review,
            }
            for block in article.blocks
        ],
    }


def _job_document(job: JobRecord) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "kind": job.kind.value,
        "state": job.state.value,
        "stage": job.stage,
        "attempt": job.attempt,
        "previous_job_id": job.previous_job_id,
        "cancel_requested": job.cancel_requested,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _pick_relevant_job(jobs: tuple[JobRecord, ...]) -> JobRecord | None:
    if not jobs:
        return None
    active_states = {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.CANCELLING,
        JobState.INTERRUPTED,
    }
    active = [job for job in jobs if job.state in active_states]
    if active:
        return max(active, key=lambda job: job.created_at)
    return max(jobs, key=lambda job: job.created_at)


def _ready_prefix_count(total_chunks: int, ready_ordinals: frozenset[int]) -> int:
    count = 0
    while count < total_chunks and count in ready_ordinals:
        count += 1
    return count


def _manifest_document(
    rendition: RenditionRecord,
    segments: tuple[PreparedSegmentRecord, ...],
    chunks: tuple[AudioChunkRecord, ...],
) -> dict[str, object]:
    chunk_by_ordinal = {chunk.ordinal: chunk for chunk in chunks}
    ready_ordinals = frozenset(chunk_by_ordinal)
    chunk_documents = []
    for segment in segments:
        published = chunk_by_ordinal.get(segment.ordinal)
        chunk_documents.append(
            {
                "ordinal": segment.ordinal,
                "state": "ready" if published is not None else "pending",
                "speech_text": segment.speech_text,
                "source_block_ordinals": list(segment.source_block_ordinals),
                "includes_title": segment.includes_title,
                "duration_seconds": published.duration_seconds if published else None,
                "audio_url": (
                    f"/api/audio/{rendition.rendition_id}/{segment.ordinal}/{published.sha256}.wav"
                    if published
                    else None
                ),
            }
        )
    return {
        "rendition_id": rendition.rendition_id,
        "revision": rendition.manifest_revision,
        "state": rendition.state.value,
        "voice_id": rendition.voice_id,
        "language": rendition.language,
        "script": rendition.script,
        "total_chunks": rendition.total_chunks,
        "ready_prefix_count": _ready_prefix_count(rendition.total_chunks, ready_ordinals),
        "error_code": rendition.error_code,
        "error_message": rendition.error_message,
        "chunks": chunk_documents,
    }


def _reading_summary(reading: ReadingRecord, title: str | None) -> dict[str, object]:
    return {
        "reading_id": reading.reading_id,
        "submitted_url": reading.submitted_url,
        "title": title,
        "state": reading.state.value,
        "created_at": reading.created_at,
        "last_opened_at": reading.last_opened_at,
    }


@dataclass(slots=True)
class _Repositories:
    readings: SqliteReadingRepository
    articles: SqliteArticleRepository
    renditions: SqliteRenditionRepository
    audio_chunks: SqliteAudioChunkRepository
    jobs: SqliteJobRepository
    idempotency: SqliteIdempotencyRepository
    progress: SqliteProgressRepository
    viewers: SqliteViewerRepository


def _resolve_viewer(request: Request, response: Response, viewers: SqliteViewerRepository) -> str:
    token = request.cookies.get(_VIEWER_COOKIE)
    if token:
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        viewer_id = viewers.find_viewer_id_by_token(token_hash)
        if viewer_id is not None:
            return viewer_id

    viewer_id = uuid4().hex
    viewers.create(ViewerRecord(viewer_id=viewer_id, label="This browser", created_at=now_iso()))
    new_token = secrets.token_urlsafe(32)
    viewers.issue_token(viewer_id, hashlib.sha256(new_token.encode("utf-8")).hexdigest(), now_iso())
    response.set_cookie(
        _VIEWER_COOKIE,
        new_token,
        max_age=_VIEWER_COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )
    return viewer_id


def create_app(
    settings: Settings,
    database: Database,
    voice_registry: VoiceRegistry,
    *,
    installation_checker: Callable[[VoiceSpec], bool] | None = None,
    audio_store: DurableAudioStore | None = None,
    web_directory: Path | None = None,
    allowed_hosts: frozenset[str] | None = None,
    allowed_origins: frozenset[str] | None = None,
) -> FastAPI:
    """Compose the ASGI application around one shared durable database."""

    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    ensure_runtime_dirs(settings)
    model_store = ModelStore(settings.paths.models_dir)
    is_installed = installation_checker or (lambda voice: model_store.inspect(voice).installed)
    store = audio_store or LocalDurableAudioStore(settings.paths.audio_dir / "renditions")

    repos = _Repositories(
        readings=SqliteReadingRepository(database),
        articles=SqliteArticleRepository(database),
        renditions=SqliteRenditionRepository(database),
        audio_chunks=SqliteAudioChunkRepository(database),
        jobs=SqliteJobRepository(database),
        idempotency=SqliteIdempotencyRepository(database),
        progress=SqliteProgressRepository(database),
        viewers=SqliteViewerRepository(database),
    )
    reading_service = ReadingService(
        readings=repos.readings,
        articles=repos.articles,
        renditions=repos.renditions,
        jobs=repos.jobs,
        idempotency=repos.idempotency,
        progress=repos.progress,
        text_preparer=RuleBasedTextPreparer(),
        voice_registry=voice_registry,
        is_installed=is_installed,
        max_segment_characters=settings.speech.max_chunk_characters,
        max_unfinished_per_viewer=settings.worker.max_unfinished_request_chains_per_viewer,
        max_waiting_chains=settings.worker.max_waiting_chains,
    )

    site_directory = web_directory or _default_web_directory()
    templates = Jinja2Templates(directory=str(site_directory / "templates"))
    app = FastAPI(
        title="Article Reader",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    default_hosts, default_origins = _request_boundary_values(settings)
    app.add_middleware(
        LocalRequestBoundaryMiddleware,
        allowed_hosts=allowed_hosts or default_hosts,
        allowed_origins=allowed_origins or default_origins,
    )
    app.mount("/static", StaticFiles(directory=site_directory / "static"), name="static")

    async def api_error_handler(_request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, ApiError)
        return _error_response(
            error.status_code,
            error.code,
            error.message,
            retryable=error.retryable,
        )

    async def validation_error_handler(_request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, RequestValidationError)
        return _error_response(422, "INVALID_REQUEST", "Check the submitted fields and try again.")

    async def unexpected_error_handler(_request: Request, error: Exception) -> JSONResponse:
        LOGGER.error("Unhandled request failure: %s", type(error).__name__)
        return _error_response(
            500,
            "INTERNAL_ERROR",
            "The local reader hit an unexpected error. Check its terminal for details.",
            retryable=True,
        )

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(Exception, unexpected_error_handler)

    def index(request: Request) -> Response:
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_version": __version__},
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "media-src 'self'; img-src 'none'; object-src 'none'; "
                    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )
        _resolve_viewer(request, response, repos.viewers)
        return response

    def meta() -> dict[str, object]:
        return {
            "app_name": "Article Reader",
            "app_version": __version__,
            "mode": "loopback_durable",
            "capabilities": {
                "article_url": True,
                "extracted_text_review": True,
                "speech": True,
                "durable_history": True,
                "lan_access": False,
            },
        }

    def voices() -> dict[str, object]:
        return {
            "voices": [
                {
                    "id": voice.voice_id,
                    "display_name": voice.display_name,
                    "language": voice.language.value,
                    "scripts": [script.value for script in voice.scripts],
                    "evaluation_status": voice.evaluation_status.value,
                    "installed": is_installed(voice),
                }
                for voice in voice_registry
            ]
        }

    def _reading_detail(
        viewer_id: str, reading_id: str, *, touch: bool = False
    ) -> dict[str, object]:
        reading = repos.readings.get_owned(reading_id, viewer_id)
        if reading is None:
            raise ApiError(404, "READING_NOT_FOUND", "This reading is not available.")
        if touch:
            repos.readings.touch_opened(reading_id, opened_at=now_iso())
        article_record = repos.articles.get_by_reading(reading_id)
        article_document = None
        if article_record is not None:
            decision_record = repos.articles.get_decision(article_record.article_id)
            article_document = _article_document(article_record, decision_record)
        renditions = repos.renditions.list_for_reading(reading_id)
        rendition_document: dict[str, object] | None = None
        if renditions:
            latest = renditions[0]
            chunks = repos.audio_chunks.list_for_rendition(latest.rendition_id)
            rendition_document = {
                "rendition_id": latest.rendition_id,
                "voice_id": latest.voice_id,
                "state": latest.state.value,
                "total_chunks": latest.total_chunks,
                "manifest_revision": latest.manifest_revision,
                "ready_prefix_count": _ready_prefix_count(
                    latest.total_chunks, frozenset(chunk.ordinal for chunk in chunks)
                ),
            }
        job = _pick_relevant_job(repos.jobs.list_for_reading(reading_id))
        progress_record = repos.progress.get(viewer_id, reading_id)
        progress_document = (
            None
            if progress_record is None
            else {
                "rendition_id": progress_record.rendition_id,
                "chunk_ordinal": progress_record.chunk_ordinal,
                "offset_seconds": progress_record.offset_seconds,
                "speed": progress_record.speed,
                "revision": progress_record.revision,
            }
        )
        return {
            **_reading_summary(reading, article_record.title if article_record else None),
            "requested_language": reading.requested_language,
            "requested_script": reading.requested_script,
            "article": article_document,
            "job": _job_document(job) if job else None,
            "rendition": rendition_document,
            "progress": progress_document,
        }

    def submit_reading(
        request: Request, response: Response, payload: SubmitReadingRequest
    ) -> JSONResponse:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        requested_language, requested_script = _language_values(payload.language, payload.script)
        idempotency_key = request.headers.get("Idempotency-Key")
        try:
            result = reading_service.submit_reading(
                viewer_id=viewer_id,
                url=payload.url,
                requested_language=requested_language,
                requested_script=requested_script,
                idempotency_key=idempotency_key,
            )
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        body = {
            "reading_id": result.reading.reading_id,
            "job_id": result.job.job_id,
            "state": result.reading.state.value,
        }
        final = JSONResponse(status_code=202, content=body)
        _copy_cookies(response, final)
        return final

    def list_readings(request: Request, response: Response) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        readings = repos.readings.list_for_viewer(
            viewer_id, limit=settings.history.max_saved_readings_per_viewer
        )
        summaries = []
        for reading in readings:
            article = repos.articles.get_by_reading(reading.reading_id)
            summaries.append(_reading_summary(reading, article.title if article else None))
        return {"readings": summaries}

    def get_reading(request: Request, response: Response, reading_id: str) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        return _reading_detail(viewer_id, reading_id, touch=True)

    def resolve_reading(
        request: Request,
        response: Response,
        reading_id: str,
        payload: ResolveReadingRequest,
    ) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        requested_language, requested_script = _language_values(payload.language, payload.script)
        try:
            reading_service.resolve_language(
                viewer_id=viewer_id,
                reading_id=reading_id,
                requested_language=requested_language,
                requested_script=requested_script,
                accept_review=payload.accept_review,
            )
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        return _reading_detail(viewer_id, reading_id)

    def create_rendition(
        request: Request,
        response: Response,
        reading_id: str,
        payload: CreateRenditionRequest,
    ) -> JSONResponse:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        try:
            result = reading_service.create_rendition(
                viewer_id=viewer_id, reading_id=reading_id, voice_id=payload.voice_id
            )
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        body = {
            "rendition_id": result.rendition.rendition_id,
            "job_id": result.job.job_id if result.job else None,
            "state": result.rendition.state.value,
            "reused": result.reused,
        }
        final = JSONResponse(status_code=202, content=body)
        _copy_cookies(response, final)
        return final

    def get_manifest(request: Request, response: Response, rendition_id: str) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        rendition = repos.renditions.get(rendition_id)
        if rendition is None or repos.readings.get_owned(rendition.reading_id, viewer_id) is None:
            raise ApiError(404, "RENDITION_NOT_FOUND", "This rendition is not available.")
        segments = repos.articles.get_prepared_segments(rendition.article_id)
        chunks = repos.audio_chunks.list_for_rendition(rendition_id)
        return _manifest_document(rendition, segments, chunks)

    def get_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        job = repos.jobs.get_owned(job_id, viewer_id)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", "This job is not available.")
        return _job_document(job)

    def cancel_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        try:
            job = reading_service.cancel_job(viewer_id=viewer_id, job_id=job_id)
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        return _job_document(job)

    def retry_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        try:
            job = reading_service.retry_job(viewer_id=viewer_id, job_id=job_id)
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        return _job_document(job)

    def save_progress(
        request: Request,
        response: Response,
        reading_id: str,
        payload: ProgressRequest,
    ) -> dict[str, object]:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        try:
            record = reading_service.save_progress(
                viewer_id=viewer_id,
                reading_id=reading_id,
                rendition_id=payload.rendition_id,
                chunk_ordinal=payload.chunk_ordinal,
                offset_seconds=payload.offset_seconds,
                speed=payload.speed,
                expected_revision=payload.revision,
            )
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        return {
            "rendition_id": record.rendition_id,
            "chunk_ordinal": record.chunk_ordinal,
            "offset_seconds": record.offset_seconds,
            "speed": record.speed,
            "revision": record.revision,
        }

    def delete_reading(request: Request, response: Response, reading_id: str) -> Response:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        try:
            reading_service.delete_reading(viewer_id=viewer_id, reading_id=reading_id)
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        final = Response(status_code=204)
        _copy_cookies(response, final)
        return final

    def audio(
        request: Request,
        response: Response,
        rendition_id: str,
        ordinal: int,
        sha256: str,
    ) -> Response:
        viewer_id = _resolve_viewer(request, response, repos.viewers)
        rendition = repos.renditions.get(rendition_id)
        if rendition is None or repos.readings.get_owned(rendition.reading_id, viewer_id) is None:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.")
        if ordinal < 0 or ordinal >= rendition.total_chunks:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.")
        chunk = repos.audio_chunks.get(rendition_id, ordinal)
        if chunk is None:
            raise ApiError(
                409, "AUDIO_NOT_READY", "This audio section has not finished generating yet."
            )
        if chunk.sha256 != sha256:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.")
        if chunk.state.value == "evicted":
            raise ApiError(410, "AUDIO_EXPIRED", "This audio has expired and needs regeneration.")
        try:
            path = store.resolve(rendition_id, chunk.relative_path, chunk.sha256)
        except ValueError as error:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.") from error
        if path is None:
            raise ApiError(
                410, "AUDIO_EXPIRED", "This audio file is missing and needs regeneration."
            )
        headers = {
            "Cache-Control": "private, max-age=3600, immutable",
            "Content-Disposition": "inline",
            "ETag": f'"{sha256}"',
            "X-Content-Type-Options": "nosniff",
        }
        final = FileResponse(path, media_type="audio/wav", headers=headers)
        _copy_cookies(response, final)
        return final

    app.add_api_route("/", index, methods=["GET"], response_class=Response)
    app.add_api_route("/api/meta", meta, methods=["GET"])
    app.add_api_route("/api/voices", voices, methods=["GET"])
    app.add_api_route(
        "/api/readings", submit_reading, methods=["POST"], response_class=JSONResponse
    )
    app.add_api_route("/api/readings", list_readings, methods=["GET"])
    app.add_api_route("/api/readings/{reading_id}", get_reading, methods=["GET"])
    app.add_api_route(
        "/api/readings/{reading_id}", delete_reading, methods=["DELETE"], response_class=Response
    )
    app.add_api_route("/api/readings/{reading_id}/resolve", resolve_reading, methods=["POST"])
    app.add_api_route(
        "/api/readings/{reading_id}/renditions",
        create_rendition,
        methods=["POST"],
        response_class=JSONResponse,
    )
    app.add_api_route("/api/readings/{reading_id}/progress", save_progress, methods=["PUT"])
    app.add_api_route("/api/renditions/{rendition_id}/manifest", get_manifest, methods=["GET"])
    app.add_api_route("/api/jobs/{job_id}", get_job, methods=["GET"])
    app.add_api_route("/api/jobs/{job_id}/cancel", cancel_job, methods=["POST"])
    app.add_api_route("/api/jobs/{job_id}/retry", retry_job, methods=["POST"])
    app.add_api_route(
        "/api/audio/{rendition_id}/{ordinal}/{sha256}.wav",
        audio,
        methods=["GET", "HEAD"],
        response_class=Response,
    )
    return app


def _map_service_error(error: ReadingServiceError) -> ApiError:
    status = _ERROR_STATUS.get(error.code, 400)
    retryable = status in {429, 503}
    return ApiError(status, error.code.value, str(error), retryable=retryable)


__all__ = [
    "ApiError",
    "CreateRenditionRequest",
    "LocalRequestBoundaryMiddleware",
    "ProgressRequest",
    "ResolveReadingRequest",
    "SubmitReadingRequest",
    "create_app",
]
