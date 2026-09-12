from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import (
    ArticlePreparationService,
    ArticlePreparationStatus,
)
from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleReviewReason,
    ExtractedArticle,
)
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.preparation import ArticlePreparationDomainError
from article_reader.domain.speech import Language, Script
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer


def _ingestion(*, review: bool = False, language_hint: str | None = "en") -> ArticleIngestionResult:
    body = b"<html><body><article>fixture</article></body></html>"
    page = FetchedPage(
        submitted_url="https://example.com/story",
        final_url="https://example.com/story",
        redirect_chain=("https://example.com/story",),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )
    blocks = [
        ArticleBlock(
            ordinal=0,
            kind=ArticleBlockKind.PARAGRAPH,
            display_text=(
                "The first useful paragraph contains enough realistic words for article "
                "preparation and traceability."
            ),
            speech_text=(
                "The first useful paragraph contains enough realistic words for article "
                "preparation and traceability."
            ),
        ),
        ArticleBlock(
            ordinal=1,
            kind=ArticleBlockKind.PARAGRAPH,
            display_text=(
                "The second useful paragraph preserves source order and supplies another "
                "complete sentence for segmentation."
            ),
            speech_text=(
                "The second useful paragraph preserves source order and supplies another "
                "complete sentence for segmentation."
            ),
        ),
    ]
    reasons: tuple[ArticleReviewReason, ...] = ()
    if review:
        blocks.append(
            ArticleBlock(
                ordinal=2,
                kind=ArticleBlockKind.CODE,
                display_text="print('review me')",
                speech_text=None,
                requires_review=True,
            )
        )
        reasons = (ArticleReviewReason.COMPLEX_CONTENT,)
    article = ExtractedArticle(
        submitted_url=page.submitted_url,
        final_url=page.final_url,
        canonical_url=None,
        title="Fixture article",
        language_hint=language_hint,
        blocks=tuple(blocks),
        review_reasons=reasons,
        extraction_version="fixture-v1",
    )
    return ArticleIngestionResult(page=page, article=article)


def _detection(*candidates: tuple[str, float]) -> LanguageDetection:
    return LanguageDetection(
        candidates=tuple(LanguageCandidate(code, confidence) for code, confidence in candidates),
        detector_version="fixture-detector-v1",
        sample_character_count=300,
        sample_alphabetic_count=250,
        sample_sha256=hashlib.sha256(b"fixture detection sample").hexdigest(),
    )


class _Ingestor:
    def __init__(self, result: ArticleIngestionResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def ingest(self, submitted_url: str) -> ArticleIngestionResult:
        self.calls.append(submitted_url)
        return self.result


class _Detector:
    def __init__(self, detection: LanguageDetection) -> None:
        self.detection = detection
        self.samples: list[str] = []

    def detect(self, text: str) -> LanguageDetection:
        self.samples.append(text)
        return self.detection


def _service(
    result: ArticleIngestionResult,
    detection: LanguageDetection,
    *,
    max_segment_characters: int = 90,
) -> tuple[ArticlePreparationService, _Ingestor, _Detector]:
    ingestor = _Ingestor(result)
    detector = _Detector(detection)
    return (
        ArticlePreparationService(
            ingestor,
            detector,
            UnicodeScriptDetector(),
            RuleBasedTextPreparer(),
            max_segment_characters=max_segment_characters,
        ),
        ingestor,
        detector,
    )


def test_ready_article_preserves_title_block_and_segment_traceability() -> None:
    ingestion = _ingestion()
    service, ingestor, detector = _service(
        ingestion,
        _detection(("en", 0.98), ("de", 0.01)),
    )

    result = service.prepare(ingestion.page.submitted_url)

    assert result.status is ArticlePreparationStatus.READY
    assert ingestor.calls == [ingestion.page.submitted_url]
    assert detector.samples and "Fixture article" in detector.samples[0]
    assert result.prepared_article is not None
    prepared_article = result.prepared_article
    assert prepared_article.prepared_text.original.language is Language.ENGLISH
    assert prepared_article.prepared_text.original.script is Script.LATIN
    assert prepared_article.title_source_span is not None
    assert (
        prepared_article.prepared_text.original.slice(prepared_article.title_source_span)
        == ingestion.article.title
    )
    for mapping in prepared_article.block_source_spans:
        block = ingestion.article.blocks[mapping.block_ordinal]
        assert (
            prepared_article.prepared_text.original.slice(mapping.source_span) == block.speech_text
        )
    assert prepared_article.omitted_block_ordinals == ()
    assert len(prepared_article.prepared_text.segments) >= 2


def test_review_required_article_waits_for_explicit_acceptance() -> None:
    ingestion = _ingestion(review=True)
    service, _ingestor, _detector = _service(
        ingestion,
        _detection(("en", 0.98), ("de", 0.01)),
    )

    waiting = service.prepare(ingestion.page.submitted_url)
    accepted = service.prepare(
        ingestion.page.submitted_url,
        accept_article_review=True,
    )

    assert waiting.status is ArticlePreparationStatus.NEEDS_ARTICLE_REVIEW
    assert waiting.prepared_article is None
    assert accepted.status is ArticlePreparationStatus.READY
    assert accepted.prepared_article is not None
    assert accepted.prepared_article.article_review_accepted is True
    assert accepted.prepared_article.omitted_block_ordinals == (2,)
    assert "review me" not in accepted.prepared_article.prepared_text.original.text


def test_prepare_existing_ingestion_does_not_fetch_again() -> None:
    ingestion = _ingestion(review=True)
    service, ingestor, _detector = _service(
        ingestion,
        _detection(("en", 0.98), ("de", 0.01)),
    )

    initial = service.prepare(ingestion.page.submitted_url)
    resolved = service.prepare_ingestion(
        initial.ingestion,
        requested_language=Language.ENGLISH,
        accept_article_review=True,
    )

    assert initial.status is ArticlePreparationStatus.NEEDS_ARTICLE_REVIEW
    assert resolved.status is ArticlePreparationStatus.READY
    assert resolved.prepared_article is not None
    assert resolved.prepared_article.article_review_accepted is True
    assert ingestor.calls == [ingestion.page.submitted_url]
    assert "review me" not in resolved.prepared_article.prepared_text.original.text


def test_bcms_latin_waits_for_manual_language_override() -> None:
    ingestion = _ingestion(language_hint="sr-Latn")
    service, _ingestor, _detector = _service(
        ingestion,
        _detection(("bs", 0.44), ("hr", 0.39), ("sr", 0.16)),
    )

    waiting = service.prepare(ingestion.page.submitted_url)
    overridden = service.prepare(
        ingestion.page.submitted_url,
        requested_language=Language.SERBIAN,
        requested_script=Script.LATIN,
    )

    assert waiting.status is ArticlePreparationStatus.NEEDS_LANGUAGE_OVERRIDE
    assert waiting.prepared_article is None
    assert overridden.status is ArticlePreparationStatus.READY
    assert overridden.prepared_article is not None
    assert overridden.prepared_article.prepared_text.original.language is Language.SERBIAN


def test_manual_override_does_not_load_or_call_the_statistical_detector() -> None:
    ingestion = _ingestion(language_hint="de")

    class _MustNotRunDetector:
        def detect(self, text: str) -> LanguageDetection:
            del text
            raise AssertionError("manual override must bypass detector model loading")

    result = ArticlePreparationService(
        _Ingestor(ingestion),
        _MustNotRunDetector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=100,
    ).prepare(
        ingestion.page.submitted_url,
        requested_language=Language.ENGLISH,
    )

    assert result.status is ArticlePreparationStatus.READY
    assert result.language_detection is None
    assert result.language_selection.language is Language.ENGLISH


def test_prepared_article_rejects_a_tampered_source_mapping() -> None:
    ingestion = _ingestion()
    service, _ingestor, _detector = _service(
        ingestion,
        _detection(("en", 0.98), ("de", 0.01)),
    )
    result = service.prepare(ingestion.page.submitted_url)
    assert result.prepared_article is not None
    mapping = result.prepared_article.block_source_spans[0]

    with pytest.raises(ArticlePreparationDomainError):
        replace(
            result.prepared_article,
            block_source_spans=(
                replace(mapping, speech_text_sha256="0" * 64),
                *result.prepared_article.block_source_spans[1:],
            ),
        )
