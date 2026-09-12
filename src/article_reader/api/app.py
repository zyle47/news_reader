"""FastAPI composition and contracts for the loopback preview reader."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
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
from article_reader.application.ports.audio import PreviewAudioPublisher
from article_reader.application.ports.speech import SpeechEngine
from article_reader.application.services.article_preparation import (
    ArticlePreparationResult,
    ArticlePreparationService,
    ArticlePreparationStatus,
)
from article_reader.application.services.language_selection import LanguageSelectionError
from article_reader.application.services.preview_audio import (
    PreviewAudioError,
    PreviewAudioService,
    PreviewRendition,
)
from article_reader.config import Settings, ensure_runtime_dirs
from article_reader.domain.article import ArticleDomainError
from article_reader.domain.speech import (
    Language,
    Script,
    SpeechDomainError,
    SpeechEngineError,
    VoiceSpec,
)
from article_reader.domain.text import TextDomainError
from article_reader.extract.trafilatura_adapter import ArticleExtractionError
from article_reader.fetch.safe_http import ArticleFetchError, FetchErrorCode
from article_reader.speech.model_store import ModelStore
from article_reader.speech.piper_engine import PiperEngine
from article_reader.speech.registry import VoiceRegistry, VoiceRegistryError
from article_reader.storage.preview_audio import LocalPreviewAudioStore

LOGGER = logging.getLogger(__name__)
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=4_096)
    language: Literal["auto", "en", "de", "sr"] = "auto"
    script: Literal["latin", "cyrillic"] | None = None
    accept_review: bool = False


class ResolvePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal["auto", "en", "de", "sr"] = "auto"
    script: Literal["latin", "cyrillic"] | None = None
    accept_review: bool = False


class RenderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


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


@dataclass(slots=True)
class _PreviewState:
    result: ArticlePreparationResult
    renditions: dict[str, PreviewRendition] = field(default_factory=dict)


class _PreviewStore:
    """Small bounded in-memory state store; durable state arrives in M3."""

    def __init__(self, maximum_previews: int = 32) -> None:
        self._maximum_previews = maximum_previews
        self._items: dict[str, _PreviewState] = {}
        self._lock = threading.Lock()

    def create(self, result: ArticlePreparationResult) -> str:
        with self._lock:
            if len(self._items) >= self._maximum_previews:
                raise ApiError(
                    429,
                    "PREVIEW_LIMIT",
                    "This preview session is full. Restart the local app to clear "
                    "transient previews.",
                    retryable=True,
                )
            preview_id = uuid4().hex
            self._items[preview_id] = _PreviewState(result=result)
            return preview_id

    def _get_locked(self, preview_id: str) -> _PreviewState:
        state = self._items.get(preview_id)
        if state is None:
            raise ApiError(404, "PREVIEW_NOT_FOUND", "This preview is no longer available.")
        return state

    def result(self, preview_id: str) -> ArticlePreparationResult:
        with self._lock:
            return self._get_locked(preview_id).result

    def rendition_for_voice(self, preview_id: str, voice_id: str) -> PreviewRendition | None:
        with self._lock:
            return self._get_locked(preview_id).renditions.get(voice_id)

    def rendition_by_id(self, preview_id: str, rendition_id: str) -> PreviewRendition | None:
        with self._lock:
            return next(
                (
                    rendition
                    for rendition in self._get_locked(preview_id).renditions.values()
                    if rendition.rendition_id == rendition_id
                ),
                None,
            )

    def update_result(
        self,
        preview_id: str,
        expected_result: ArticlePreparationResult,
        result: ArticlePreparationResult,
    ) -> None:
        with self._lock:
            state = self._get_locked(preview_id)
            if state.result is not expected_result:
                raise ApiError(
                    409,
                    "PREVIEW_CHANGED",
                    "The preview changed while this request was running. Try again.",
                    retryable=True,
                )
            state.result = result
            state.renditions.clear()

    def add_rendition(
        self,
        preview_id: str,
        expected_result: ArticlePreparationResult,
        rendition: PreviewRendition,
    ) -> None:
        with self._lock:
            state = self._get_locked(preview_id)
            if state.result is not expected_result:
                raise ApiError(
                    409,
                    "PREVIEW_CHANGED",
                    "The preview changed while audio was generating. Generate it again.",
                    retryable=True,
                )
            state.renditions[rendition.voice_id] = rendition


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


def _preparation_document(preview_id: str, result: ArticlePreparationResult) -> dict[str, object]:
    article = result.ingestion.article
    selection = result.language_selection
    preparation: dict[str, object] | None = None
    if result.prepared_article is not None:
        prepared = result.prepared_article
        segments: list[dict[str, object]] = []
        for segment in prepared.prepared_text.segments:
            source_blocks = [
                mapping.block_ordinal
                for mapping in prepared.block_source_spans
                if any(
                    span.start < mapping.source_span.end and mapping.source_span.start < span.end
                    for span in segment.source_spans
                )
            ]
            title_span = prepared.title_source_span
            includes_title = bool(
                title_span is not None
                and any(
                    span.start < title_span.end and title_span.start < span.end
                    for span in segment.source_spans
                )
            )
            segments.append(
                {
                    "ordinal": segment.ordinal,
                    "speech_text": segment.speech_text,
                    "source_block_ordinals": source_blocks,
                    "includes_title": includes_title,
                }
            )
        preparation = {
            "segment_count": len(segments),
            "speech_character_count": prepared.prepared_text.total_speech_characters,
            "segments": segments,
            "omitted_block_ordinals": list(prepared.omitted_block_ordinals),
        }
    return {
        "preview_id": preview_id,
        "status": result.status.value,
        "title": article.title,
        "final_url": article.final_url,
        "language": {
            "selected_language": selection.language.value if selection.language else None,
            "selected_script": selection.script.value if selection.script else None,
            "selection_reason": selection.reason.value,
        },
        "review": {
            "required": article.needs_review,
            "accepted": bool(
                result.prepared_article is not None
                and result.prepared_article.article_review_accepted
            ),
            "reasons": [reason.value for reason in article.review_reasons],
        },
        "blocks": [
            {
                "ordinal": block.ordinal,
                "kind": block.kind.value,
                "display_text": block.display_text,
                "will_be_spoken": block.speech_text is not None,
                "requires_review": block.requires_review,
            }
            for block in article.blocks
        ],
        "preparation": preparation,
    }


def _rendition_document(preview_id: str, rendition: PreviewRendition) -> dict[str, object]:
    return {
        "preview_id": preview_id,
        "rendition_id": rendition.rendition_id,
        "state": "ready",
        "voice_id": rendition.voice_id,
        "sample_rate_hz": rendition.sample_rate_hz,
        "total_duration_seconds": rendition.total_duration_seconds,
        "chunks": [
            {
                "ordinal": chunk.ordinal,
                "speech_text": chunk.speech_text,
                "source_block_ordinals": list(chunk.source_block_ordinals),
                "includes_title": chunk.includes_title,
                "duration_seconds": chunk.duration_seconds,
                "audio_url": (
                    f"/api/previews/{preview_id}/audio/{rendition.rendition_id}/"
                    f"{chunk.ordinal}/{chunk.sha256}.wav"
                ),
            }
            for chunk in rendition.chunks
        ],
    }


def _default_web_directory() -> Path:
    resource = resources.files("article_reader.web")
    return Path(str(resource))


def _request_boundary_values(settings: Settings) -> tuple[frozenset[str], frozenset[str]]:
    port = settings.server.port
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})
    origins = frozenset(f"http://{host}" for host in hosts)
    return hosts, origins


def create_app(
    settings: Settings,
    preparation_service: ArticlePreparationService,
    voice_registry: VoiceRegistry,
    *,
    installation_checker: Callable[[VoiceSpec], bool] | None = None,
    engine_factory: Callable[[], SpeechEngine] | None = None,
    audio_publisher: PreviewAudioPublisher | None = None,
    web_directory: Path | None = None,
    allowed_hosts: frozenset[str] | None = None,
    allowed_origins: frozenset[str] | None = None,
) -> FastAPI:
    """Compose the ASGI application around injected application services."""

    if not isinstance(settings, Settings):
        raise TypeError("settings must be Settings")
    ensure_runtime_dirs(settings)
    model_store = ModelStore(settings.paths.models_dir)
    is_installed = installation_checker or (lambda voice: model_store.inspect(voice).installed)
    make_engine = engine_factory or (lambda: PiperEngine(model_store))
    publisher = audio_publisher or LocalPreviewAudioStore(settings.paths.audio_dir / "previews")
    renderer = PreviewAudioService(publisher)
    previews = _PreviewStore()
    generation_lock = threading.Lock()

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
        LOGGER.error("Unhandled preview request failure: %s", type(error).__name__)
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
        return templates.TemplateResponse(
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

    def meta() -> dict[str, object]:
        return {
            "app_name": "Article Reader",
            "app_version": __version__,
            "mode": "loopback_preview",
            "capabilities": {
                "article_url": True,
                "extracted_text_review": True,
                "speech": True,
                "durable_history": False,
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

    def create_preview(payload: PreviewRequest) -> dict[str, object]:
        requested_language, requested_script = _language_values(payload.language, payload.script)
        try:
            result = preparation_service.prepare(
                payload.url,
                requested_language=requested_language,
                requested_script=requested_script,
                accept_article_review=payload.accept_review,
            )
        except Exception as error:
            raise _map_application_error(error) from error
        preview_id = previews.create(result)
        return _preparation_document(preview_id, result)

    def resolve_preview(preview_id: str, payload: ResolvePreviewRequest) -> dict[str, object]:
        requested_language, requested_script = _language_values(payload.language, payload.script)
        existing_result = previews.result(preview_id)
        try:
            result = preparation_service.prepare_ingestion(
                existing_result.ingestion,
                requested_language=requested_language,
                requested_script=requested_script,
                accept_article_review=payload.accept_review,
            )
        except Exception as error:
            raise _map_application_error(error) from error
        previews.update_result(preview_id, existing_result, result)
        return _preparation_document(preview_id, result)

    def render_preview(preview_id: str, payload: RenderPreviewRequest) -> dict[str, object]:
        existing = previews.rendition_for_voice(preview_id, payload.voice_id)
        if existing is not None:
            return _rendition_document(preview_id, existing)
        result = previews.result(preview_id)
        if result.status is not ArticlePreparationStatus.READY or result.prepared_article is None:
            raise ApiError(
                409,
                "PREVIEW_NOT_READY",
                "Review the article and resolve its language before generating audio.",
            )
        try:
            voice = voice_registry.require_approved(payload.voice_id)
        except VoiceRegistryError as error:
            raise ApiError(
                422,
                "VOICE_INVALID",
                "Choose an approved voice from the list.",
            ) from error
        original = result.prepared_article.prepared_text.original
        if not voice.supports(original.language, original.script):
            raise ApiError(
                422,
                "VOICE_LANGUAGE_MISMATCH",
                "The selected voice does not support this article language and script.",
            )
        if not is_installed(voice):
            raise ApiError(
                503,
                "VOICE_UNAVAILABLE",
                "The selected voice is approved but is not installed in this data directory.",
            )
        if not generation_lock.acquire(blocking=False):
            raise ApiError(
                409,
                "SYNTHESIS_BUSY",
                "Another preview is generating audio. Try again when it finishes.",
                retryable=True,
            )
        try:
            rendition = renderer.render(
                uuid4().hex,
                result.prepared_article,
                voice,
                make_engine(),
            )
            try:
                previews.add_rendition(preview_id, result, rendition)
            except Exception:
                publisher.discard(rendition.rendition_id)
                raise
        except Exception as error:
            raise _map_application_error(error) from error
        finally:
            generation_lock.release()
        return _rendition_document(preview_id, rendition)

    def audio(
        preview_id: str,
        rendition_id: str,
        ordinal: int,
        sha256: str,
    ) -> Response:
        rendition = previews.rendition_by_id(preview_id, rendition_id)
        if rendition is None:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.")
        chunk = next(
            (
                item
                for item in rendition.chunks
                if item.ordinal == ordinal and item.sha256 == sha256
            ),
            None,
        )
        if chunk is None:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.")
        try:
            path = publisher.resolve(rendition_id, ordinal, sha256)
        except ValueError as error:
            raise ApiError(404, "AUDIO_NOT_FOUND", "This audio chunk is not available.") from error
        if path is None:
            raise ApiError(410, "AUDIO_EXPIRED", "This preview audio has expired.")
        return FileResponse(
            path,
            media_type="audio/wav",
            headers={
                "Cache-Control": "private, max-age=3600, immutable",
                "Content-Disposition": "inline",
                "ETag": f'"{sha256}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    app.add_api_route("/", index, methods=["GET"], response_class=Response)
    app.add_api_route("/api/meta", meta, methods=["GET"])
    app.add_api_route("/api/voices", voices, methods=["GET"])
    app.add_api_route("/api/previews", create_preview, methods=["POST"])
    app.add_api_route(
        "/api/previews/{preview_id}/prepare",
        resolve_preview,
        methods=["POST"],
    )
    app.add_api_route(
        "/api/previews/{preview_id}/audio",
        render_preview,
        methods=["POST"],
    )
    app.add_api_route(
        "/api/previews/{preview_id}/audio/{rendition_id}/{ordinal}/{sha256}.wav",
        audio,
        methods=["GET", "HEAD"],
        response_class=Response,
    )
    return app


def _map_application_error(error: Exception) -> ApiError:
    if isinstance(error, ApiError):
        return error
    if isinstance(error, ArticleFetchError):
        invalid_codes = {FetchErrorCode.INVALID_URL, FetchErrorCode.BLOCKED_DESTINATION}
        status = 422 if error.code in invalid_codes else 502
        return ApiError(status, error.code.value, str(error), retryable=status == 502)
    if isinstance(error, ArticleExtractionError):
        return ApiError(422, error.code.value, str(error))
    if isinstance(error, LanguageSelectionError):
        return ApiError(422, error.code.value, str(error))
    if isinstance(error, (ArticleDomainError, TextDomainError, SpeechDomainError, ValueError)):
        return ApiError(422, "INVALID_INPUT", "The submitted reading data is invalid.")
    if isinstance(error, PreviewAudioError):
        return ApiError(422, "SYNTHESIS_INVALID", str(error))
    if isinstance(error, SpeechEngineError):
        return ApiError(
            503,
            "SYNTHESIS_FAILED",
            "The installed speech engine could not generate this audio.",
            retryable=True,
        )
    if isinstance(error, OSError):
        return ApiError(
            503,
            "STORAGE_UNAVAILABLE",
            "Preview audio could not be stored in the configured data directory.",
            retryable=True,
        )
    return ApiError(
        500,
        "INTERNAL_ERROR",
        "The local reader hit an unexpected error. Check its terminal for details.",
        retryable=True,
    )


__all__ = [
    "ApiError",
    "LocalRequestBoundaryMiddleware",
    "PreviewRequest",
    "RenderPreviewRequest",
    "ResolvePreviewRequest",
    "create_app",
]
