from __future__ import annotations

from pathlib import Path

import pytest

from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import ArticlePreparationService
from article_reader.application.services.preview_audio import PreviewAudioError, PreviewAudioService
from article_reader.domain.article import ArticleBlock, ArticleBlockKind, ExtractedArticle
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.preparation import PreparedArticle
from article_reader.domain.speech import (
    Language,
    Script,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.speech.fake import FakeSpeechEngine
from article_reader.storage.preview_audio import LocalPreviewAudioStore
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer


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
        title="Fixture article",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="One useful article sentence. Another sentence follows it.",
                speech_text="One useful article sentence. Another sentence follows it.",
            ),
        ),
        review_reasons=(),
        extraction_version="fixture-v1",
    )
    return ArticleIngestionResult(page=page, article=article)


class _Ingestor:
    def ingest(self, _submitted_url: str) -> ArticleIngestionResult:
        return _ingestion()


class _Detector:
    def detect(self, text: str) -> LanguageDetection:
        return LanguageDetection(
            candidates=(LanguageCandidate("en", 0.99), LanguageCandidate("de", 0.01)),
            detector_version="fixture-v1",
            sample_character_count=len(text),
            sample_alphabetic_count=50,
            sample_sha256="a" * 64,
        )


def _prepared_article() -> PreparedArticle:
    service = ArticlePreparationService(
        _Ingestor(),
        _Detector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=45,
    )
    result = service.prepare(
        "https://example.com/article",
        requested_language=Language.ENGLISH,
    )
    assert result.prepared_article is not None
    return result.prepared_article


def _voice(status: VoiceEvaluationStatus = VoiceEvaluationStatus.APPROVED) -> VoiceSpec:
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
        evaluation_status=status,
        evaluation_notes="Offline fixture.",
    )


def test_preview_audio_renders_ordered_atomic_wav_chunks(tmp_path: Path) -> None:
    store = LocalPreviewAudioStore(tmp_path.resolve())
    service = PreviewAudioService(store)

    rendition = service.render(
        "1" * 32,
        _prepared_article(),
        _voice(),
        FakeSpeechEngine(),
    )

    assert len(rendition.chunks) >= 2
    assert [chunk.ordinal for chunk in rendition.chunks] == list(range(len(rendition.chunks)))
    for chunk in rendition.chunks:
        path = store.resolve(rendition.rendition_id, chunk.ordinal, chunk.sha256)
        assert path is not None
        assert path.read_bytes().startswith(b"RIFF")
    assert not list(tmp_path.rglob("*.tmp"))


def test_preview_audio_refuses_unapproved_voice_without_writing(tmp_path: Path) -> None:
    store = LocalPreviewAudioStore(tmp_path.resolve())

    with pytest.raises(PreviewAudioError, match="not approved"):
        PreviewAudioService(store).render(
            "2" * 32,
            _prepared_article(),
            _voice(VoiceEvaluationStatus.CANDIDATE),
            FakeSpeechEngine(),
        )

    assert not list(tmp_path.rglob("*.wav"))


@pytest.mark.parametrize(
    "rendition_id",
    ["../escape", "A" * 32, "1" * 31, "1" * 33],
)
def test_preview_audio_store_rejects_uncontrolled_ids(tmp_path: Path, rendition_id: str) -> None:
    store = LocalPreviewAudioStore(tmp_path.resolve())

    with pytest.raises(ValueError, match="invalid preview rendition ID"):
        store.resolve(rendition_id, 0, "a" * 64)
