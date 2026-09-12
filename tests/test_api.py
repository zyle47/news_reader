from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from article_reader.api.app import create_app
from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import ArticlePreparationService
from article_reader.config import PathsSettings, Settings
from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleReviewReason,
    ExtractedArticle,
)
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.speech import Language, Script, VoiceEvaluationStatus, VoiceSpec
from article_reader.speech.fake import FakeSpeechEngine
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.preview_audio import LocalPreviewAudioStore
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer

_HOST = "testserver"
_ORIGIN = "http://testserver"


def _ingestion() -> ArticleIngestionResult:
    body = b"<html><article>fixture</article></html>"
    page = FetchedPage(
        submitted_url="https://example.com/article",
        final_url="https://example.com/article",
        redirect_chain=("https://example.com/article",),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )
    article = ExtractedArticle(
        submitted_url=page.submitted_url,
        final_url=page.final_url,
        canonical_url=None,
        title="<script>Fixture title</script>",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text=(
                    "The first useful paragraph has enough words for a realistic reading test."
                ),
                speech_text=(
                    "The first useful paragraph has enough words for a realistic reading test."
                ),
            ),
            ArticleBlock(
                ordinal=1,
                kind=ArticleBlockKind.CODE,
                display_text="print('shown but not spoken')",
                speech_text=None,
                requires_review=True,
            ),
        ),
        review_reasons=(ArticleReviewReason.COMPLEX_CONTENT,),
        extraction_version="fixture-v1",
    )
    return ArticleIngestionResult(page=page, article=article)


class _Ingestor:
    def __init__(self) -> None:
        self.calls = 0

    def ingest(self, _submitted_url: str) -> ArticleIngestionResult:
        self.calls += 1
        return _ingestion()


class _Detector:
    def detect(self, text: str) -> LanguageDetection:
        return LanguageDetection(
            candidates=(LanguageCandidate("en", 0.99), LanguageCandidate("de", 0.01)),
            detector_version="fixture-v1",
            sample_character_count=len(text),
            sample_alphabetic_count=60,
            sample_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )


def _voice() -> VoiceSpec:
    return VoiceSpec(
        voice_id="fixture-en",
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
        evaluation_notes="Offline fixture.",
    )


def _client(tmp_path: Path) -> tuple[TestClient, _Ingestor]:
    ingestor = _Ingestor()
    service = ArticlePreparationService(
        ingestor,
        _Detector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=55,
    )
    settings = Settings(paths=PathsSettings(data_dir=(tmp_path / "data").resolve()))
    web_directory = Path(__file__).parents[1] / "src" / "article_reader" / "web"
    app = create_app(
        settings,
        service,
        VoiceRegistry((_voice(),)),
        installation_checker=lambda _voice_spec: True,
        engine_factory=FakeSpeechEngine,
        audio_publisher=LocalPreviewAudioStore((tmp_path / "audio").resolve()),
        web_directory=web_directory,
        allowed_hosts=frozenset({_HOST}),
        allowed_origins=frozenset({_ORIGIN}),
    )
    return TestClient(app, base_url=_ORIGIN), ingestor


def _post(client: TestClient, path: str, document: dict[str, object]) -> Response:
    return client.post(path, headers={"Origin": _ORIGIN}, json=document)


def _json(response: Response) -> dict[str, Any]:
    document = response.json()
    assert isinstance(document, dict)
    return document


def test_reader_page_has_security_headers_and_packaged_assets(tmp_path: Path) -> None:
    client, _ingestor = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Turn an article into a calm listening session" in response.text
    assert "<script>Fixture title</script>" not in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.css").status_code == 200
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert ".textContent" in script.text
    assert ".innerHTML" not in script.text
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_mutating_requests_require_same_origin_json(tmp_path: Path) -> None:
    client, _ingestor = _client(tmp_path)
    payload = {"url": "https://example.com/article", "language": "en"}

    missing_origin = client.post("/api/previews", json=payload)
    wrong_origin = client.post(
        "/api/previews", headers={"Origin": "https://attacker.example"}, json=payload
    )
    wrong_type = client.post(
        "/api/previews",
        headers={"Origin": _ORIGIN, "Content-Type": "text/plain"},
        content="not-json",
    )

    assert missing_origin.status_code == 403
    assert _json(missing_origin)["error"]["code"] == "ORIGIN_REJECTED"
    assert wrong_origin.status_code == 403
    assert wrong_type.status_code == 415


def test_preview_review_resolution_does_not_refetch_and_audio_supports_ranges(
    tmp_path: Path,
) -> None:
    client, ingestor = _client(tmp_path)
    voices = _json(client.get("/api/voices"))["voices"]
    assert voices[0]["installed"] is True

    created = _post(
        client,
        "/api/previews",
        {"url": "https://example.com/article", "language": "en"},
    )
    assert created.status_code == 200
    preview = _json(created)
    assert preview["status"] == "needs_article_review"
    assert preview["blocks"][1]["will_be_spoken"] is False
    assert ingestor.calls == 1

    preview_id = preview["preview_id"]
    resolved = _post(
        client,
        f"/api/previews/{preview_id}/prepare",
        {"language": "en", "accept_review": True},
    )
    assert resolved.status_code == 200
    resolved_document = _json(resolved)
    assert resolved_document["status"] == "ready"
    assert resolved_document["preparation"]["omitted_block_ordinals"] == [1]
    assert ingestor.calls == 1

    rendered = _post(
        client,
        f"/api/previews/{preview_id}/audio",
        {"voice_id": "fixture-en"},
    )
    assert rendered.status_code == 200
    rendition = _json(rendered)
    assert rendition["state"] == "ready"
    assert len(rendition["chunks"]) >= 2

    audio_url = rendition["chunks"][0]["audio_url"]
    audio = client.get(audio_url)
    partial = client.get(audio_url, headers={"Range": "bytes=0-9"})
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content.startswith(b"RIFF")
    assert partial.status_code == 206
    assert len(partial.content) == 10


def test_preview_rejects_serbian_without_script_before_fetch(tmp_path: Path) -> None:
    client, ingestor = _client(tmp_path)

    response = _post(
        client,
        "/api/previews",
        {"url": "https://example.com/article", "language": "sr"},
    )

    assert response.status_code == 422
    assert _json(response)["error"]["code"] == "LANGUAGE_SCRIPT_REQUIRED"
    assert ingestor.calls == 0
