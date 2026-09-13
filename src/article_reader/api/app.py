"""FastAPI composition and contracts for the durable, loopback browser reader.

The API process only ever validates input, reads/writes SQLite through the repositories in
``article_reader.db``, and serves already-published audio files. It never fetches a URL or
calls a speech engine: that work happens exclusively in the durable worker
(``article_reader.worker.loop.WorkerLoop``), composed and started separately in ``cli.py``.
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
)
from article_reader.application.services.access_control import (
    AccessError,
    AccessErrorCode,
    AccessService,
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
from article_reader.network import http_authority
from article_reader.security.pairing_qr import pairing_qr_data_url
from article_reader.speech.model_store import ModelStore
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.durable_audio import LocalDurableAudioStore
from article_reader.text.segment import RuleBasedTextPreparer

LOGGER = logging.getLogger(__name__)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_VIEWER_COOKIE = "article_reader_viewer"
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

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


class RedeemPairingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32)
    label: str = Field(default="Paired device", max_length=80)


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


class ResponseSecurityMiddleware:
    """Attach browser hardening headers to HTML, API, static, and audio responses."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message.get("type") == "http.response.start":
                mutable_headers = list(message.get("headers", []))
                existing = {bytes(key).lower() for key, _value in mutable_headers}

                def add(name: str, value: str) -> None:
                    encoded = name.lower().encode("latin-1")
                    if encoded not in existing:
                        mutable_headers.append((encoded, value.encode("latin-1")))

                add("Cache-Control", "no-store")
                add("Content-Security-Policy", _CONTENT_SECURITY_POLICY)
                add("Referrer-Policy", "no-referrer")
                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("Cross-Origin-Resource-Policy", "same-origin")
                add("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                message["headers"] = mutable_headers
            await send(message)

        await self._app(scope, receive, send_with_headers)


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


def _request_boundary_values(
    settings: Settings, lan_address: str | None
) -> tuple[frozenset[str], frozenset[str]]:
    port = settings.server.port
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if settings.server.lan_mode:
        if lan_address is None:
            raise ValueError("lan_address is required when server.lan_mode is enabled")
        hosts.add(http_authority(lan_address, port))
    frozen_hosts = frozenset(hosts)
    origins = frozenset(f"http://{host}" for host in frozen_hosts)
    return frozen_hosts, origins


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


@dataclass(frozen=True, slots=True)
class _AuthenticatedViewer:
    viewer_id: str
    session_id: str


def _is_loopback_request(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        _VIEWER_COOKIE,
        token,
        max_age=settings.access.session_lifetime_hours * 60 * 60,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )


def _resolve_access(
    request: Request,
    response: Response,
    access: AccessService,
    settings: Settings,
    *,
    required: bool,
) -> _AuthenticatedViewer | None:
    session = access.authenticate(request.cookies.get(_VIEWER_COOKIE))
    if session is None and (not settings.server.lan_mode or _is_loopback_request(request)):
        issued = access.create_local_viewer_session()
        _set_session_cookie(response, issued.token, settings)
        session = issued.record
    if session is None:
        if required:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "Pair this device to continue.")
        return None
    return _AuthenticatedViewer(viewer_id=session.viewer_id, session_id=session.session_id)


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
    lan_address: str | None = None,
    access_service: AccessService | None = None,
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
    access = access_service or AccessService(
        repos.viewers,
        session_lifetime_seconds=settings.access.session_lifetime_hours * 60 * 60,
        pairing_lifetime_seconds=settings.access.pairing_lifetime_seconds,
        max_sessions_per_viewer=settings.access.max_sessions_per_viewer,
        max_pairing_attempts=settings.access.max_pairing_attempts,
        pairing_attempt_window_seconds=settings.access.pairing_attempt_window_seconds,
        pairing_block_seconds=settings.access.pairing_block_seconds,
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
    default_hosts, default_origins = _request_boundary_values(settings, lan_address)
    app.add_middleware(
        LocalRequestBoundaryMiddleware,
        allowed_hosts=allowed_hosts or default_hosts,
        allowed_origins=allowed_origins or default_origins,
    )
    app.add_middleware(ResponseSecurityMiddleware)
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
            context={"app_version": __version__, "lan_mode": settings.server.lan_mode},
        )
        _resolve_access(request, response, access, settings, required=False)
        return response

    def meta() -> dict[str, object]:
        return {
            "app_name": "Article Reader",
            "app_version": __version__,
            "mode": "lan_paired" if settings.server.lan_mode else "loopback_durable",
            "capabilities": {
                "article_url": True,
                "extracted_text_review": True,
                "speech": True,
                "durable_history": True,
                "lan_access": settings.server.lan_mode,
            },
        }

    def _require_viewer(request: Request, response: Response) -> _AuthenticatedViewer:
        authenticated = _resolve_access(request, response, access, settings, required=True)
        assert authenticated is not None
        return authenticated

    def voices(request: Request, response: Response) -> dict[str, object]:
        _require_viewer(request, response)
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

    def auth_status(request: Request, response: Response) -> JSONResponse:
        authenticated = _resolve_access(request, response, access, settings, required=False)
        body: dict[str, object] = {
            "authenticated": authenticated is not None,
            "lan_mode": settings.server.lan_mode,
            "loopback": _is_loopback_request(request) or not settings.server.lan_mode,
            "transport_confidential": False,
        }
        if authenticated is not None:
            body["session_id"] = authenticated.session_id
        final = JSONResponse(body)
        _copy_cookies(response, final)
        return final

    def create_pairing_offer(request: Request, response: Response) -> dict[str, object]:
        if not settings.server.lan_mode or lan_address is None:
            raise ApiError(404, "LAN_MODE_DISABLED", "LAN pairing is not enabled.")
        if not _is_loopback_request(request):
            raise ApiError(
                403,
                "LOOPBACK_REQUIRED",
                "New device pairing can only be started from this computer.",
            )
        authenticated = _require_viewer(request, response)
        offer = access.create_pairing(authenticated.viewer_id)
        authority = http_authority(lan_address, settings.server.port)
        pairing_url = f"http://{authority}/#pair={offer.code}"
        return {
            "code": offer.code,
            "expires_at": offer.expires_at,
            "pairing_url": pairing_url,
            "qr_data_url": pairing_qr_data_url(pairing_url),
        }

    def redeem_pairing(
        request: Request, response: Response, payload: RedeemPairingRequest
    ) -> JSONResponse:
        if not settings.server.lan_mode:
            raise ApiError(404, "LAN_MODE_DISABLED", "LAN pairing is not enabled.")
        client_key = request.client.host if request.client is not None else "unknown"
        try:
            issued = access.redeem_pairing(
                code=payload.code, label=payload.label, client_key=client_key
            )
        except AccessError as error:
            status = {
                AccessErrorCode.PAIRING_RATE_LIMITED: 429,
                AccessErrorCode.SESSION_LIMIT_REACHED: 409,
            }.get(error.code, 401)
            raise ApiError(status, error.code.value, str(error), retryable=status == 429) from error
        _set_session_cookie(response, issued.token, settings)
        final = JSONResponse(
            {
                "authenticated": True,
                "session_id": issued.record.session_id,
                "label": issued.record.label,
                "expires_at": issued.record.expires_at,
            }
        )
        _copy_cookies(response, final)
        return final

    def list_sessions(request: Request, response: Response) -> dict[str, object]:
        authenticated = _require_viewer(request, response)
        return {
            "current_session_id": authenticated.session_id,
            "sessions": [
                {
                    "session_id": session.session_id,
                    "label": session.label,
                    "created_at": session.created_at,
                    "last_seen_at": session.last_seen_at,
                    "expires_at": session.expires_at,
                    "current": session.session_id == authenticated.session_id,
                }
                for session in access.list_sessions(authenticated.viewer_id)
            ],
        }

    def revoke_session(request: Request, response: Response, session_id: str) -> JSONResponse:
        authenticated = _require_viewer(request, response)
        if not access.revoke_session(authenticated.viewer_id, session_id):
            raise ApiError(404, "SESSION_NOT_FOUND", "This session is not available.")
        final = JSONResponse({"revoked": True, "session_id": session_id})
        if session_id == authenticated.session_id:
            final.delete_cookie(_VIEWER_COOKIE, path="/", httponly=True, samesite="strict")
        return final

    def logout(request: Request, response: Response) -> Response:
        authenticated = _require_viewer(request, response)
        access.revoke_session(authenticated.viewer_id, authenticated.session_id)
        final = Response(status_code=204)
        final.delete_cookie(_VIEWER_COOKIE, path="/", httponly=True, samesite="strict")
        return final

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
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
        readings = repos.readings.list_for_viewer(
            viewer_id, limit=settings.history.max_saved_readings_per_viewer
        )
        summaries = []
        for reading in readings:
            article = repos.articles.get_by_reading(reading.reading_id)
            summaries.append(_reading_summary(reading, article.title if article else None))
        return {"readings": summaries}

    def get_reading(request: Request, response: Response, reading_id: str) -> dict[str, object]:
        viewer_id = _require_viewer(request, response).viewer_id
        return _reading_detail(viewer_id, reading_id, touch=True)

    def resolve_reading(
        request: Request,
        response: Response,
        reading_id: str,
        payload: ResolveReadingRequest,
    ) -> dict[str, object]:
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
        rendition = repos.renditions.get(rendition_id)
        if rendition is None or repos.readings.get_owned(rendition.reading_id, viewer_id) is None:
            raise ApiError(404, "RENDITION_NOT_FOUND", "This rendition is not available.")
        segments = repos.articles.get_prepared_segments(rendition.article_id)
        chunks = repos.audio_chunks.list_for_rendition(rendition_id)
        return _manifest_document(rendition, segments, chunks)

    def get_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _require_viewer(request, response).viewer_id
        job = repos.jobs.get_owned(job_id, viewer_id)
        if job is None:
            raise ApiError(404, "JOB_NOT_FOUND", "This job is not available.")
        return _job_document(job)

    def cancel_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _require_viewer(request, response).viewer_id
        try:
            job = reading_service.cancel_job(viewer_id=viewer_id, job_id=job_id)
        except ReadingServiceError as error:
            raise _map_service_error(error) from error
        return _job_document(job)

    def retry_job(request: Request, response: Response, job_id: str) -> dict[str, object]:
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
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
        viewer_id = _require_viewer(request, response).viewer_id
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
    app.add_api_route("/api/auth", auth_status, methods=["GET"], response_class=JSONResponse)
    app.add_api_route("/api/pairings", create_pairing_offer, methods=["POST"])
    app.add_api_route(
        "/api/pairings/redeem", redeem_pairing, methods=["POST"], response_class=JSONResponse
    )
    app.add_api_route("/api/sessions", list_sessions, methods=["GET"])
    app.add_api_route(
        "/api/sessions/{session_id}/revoke",
        revoke_session,
        methods=["POST"],
        response_class=JSONResponse,
    )
    app.add_api_route("/api/logout", logout, methods=["POST"], response_class=Response)
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
