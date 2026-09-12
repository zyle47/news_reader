"""End-to-end article preview, language selection, and text preparation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from article_reader.application.ports.language import LanguageDetector
from article_reader.application.ports.text import ScriptDetector, TextPreparer
from article_reader.application.services.article_ingestion import (
    ArticleIngestionResult,
)
from article_reader.application.services.language_selection import select_language
from article_reader.domain.article import ExtractedArticle
from article_reader.domain.language import (
    LanguageDetection,
    LanguageSelection,
    LanguageSelectionReason,
    ScriptDetection,
)
from article_reader.domain.preparation import (
    ArticleBlockSourceSpan,
    PreparedArticle,
)
from article_reader.domain.speech import Language, Script
from article_reader.domain.text import OriginalText, SourceSpan

_MAX_LANGUAGE_SAMPLE_CHARACTERS = 100_000


class ArticlePreparationStatus(StrEnum):
    READY = "ready"
    NEEDS_ARTICLE_REVIEW = "needs_article_review"
    NEEDS_LANGUAGE_OVERRIDE = "needs_language_override"


class ArticleIngestor(Protocol):
    def ingest(self, submitted_url: str) -> ArticleIngestionResult: ...


@dataclass(frozen=True, slots=True)
class ArticlePreparationResult:
    ingestion: ArticleIngestionResult
    language_detection: LanguageDetection | None
    script_detection: ScriptDetection
    language_selection: LanguageSelection
    status: ArticlePreparationStatus
    prepared_article: PreparedArticle | None

    def __post_init__(self) -> None:
        if (
            self.language_selection.reason is not LanguageSelectionReason.MANUAL_OVERRIDE
            and self.language_detection is None
        ):
            raise ValueError("automatic language selection requires detector evidence")
        if self.status is ArticlePreparationStatus.READY:
            if self.prepared_article is None or self.language_selection.requires_override:
                raise ValueError("ready article preparation requires prepared text and a language")
        elif self.prepared_article is not None:
            raise ValueError("an awaiting-input result cannot contain prepared text")


def _detection_text(result: ArticleIngestionResult) -> str:
    article = result.article
    parts: list[str] = []
    if article.title is not None:
        parts.append(article.title)
    parts.extend(block.speech_text or block.display_text for block in article.blocks)
    text = "\n\n".join(parts)
    if len(text) <= _MAX_LANGUAGE_SAMPLE_CHARACTERS:
        return text
    separator = "\n"
    piece_size = (_MAX_LANGUAGE_SAMPLE_CHARACTERS - len(separator) * 2) // 3
    middle_start = (len(text) - piece_size) // 2
    sample = separator.join(
        (
            text[:piece_size],
            text[middle_start : middle_start + piece_size],
            text[-piece_size:],
        )
    )
    return sample[:_MAX_LANGUAGE_SAMPLE_CHARACTERS]


def build_prepared_article(
    article: ExtractedArticle,
    *,
    language: Language,
    script: Script,
    max_segment_characters: int,
    review_accepted: bool,
    text_preparer: TextPreparer,
) -> PreparedArticle:
    """Build source-traceable speech segments for one resolved article snapshot.

    This depends only on the immutable ``ExtractedArticle`` and the resolved language/script,
    never on the original fetch. Reviewing or overriding language for an already-persisted
    article snapshot therefore never re-fetches the source: both the durable worker's initial
    automatic pass and the API's manual review/override resolution call this same function.
    """

    parts: list[str] = []
    title_span: SourceSpan | None = None
    mappings: list[ArticleBlockSourceSpan] = []
    omitted: list[int] = []
    cursor = 0

    def append(text: str) -> SourceSpan:
        nonlocal cursor
        if parts:
            cursor += 2
        start = cursor
        parts.append(text)
        cursor += len(text)
        return SourceSpan(start, cursor)

    if article.title is not None:
        title_span = append(article.title)
    for block in article.blocks:
        if block.speech_text is None:
            omitted.append(block.ordinal)
            continue
        span = append(block.speech_text)
        mappings.append(
            ArticleBlockSourceSpan(
                block_ordinal=block.ordinal,
                source_span=span,
                speech_text_sha256=hashlib.sha256(block.speech_text.encode("utf-8")).hexdigest(),
            )
        )

    original = OriginalText(
        text="\n\n".join(parts),
        language=language,
        script=script,
    )
    prepared = text_preparer.prepare(
        original,
        max_segment_characters=max_segment_characters,
    )
    return PreparedArticle(
        article=article,
        prepared_text=prepared,
        title_source_span=title_span,
        block_source_spans=tuple(mappings),
        omitted_block_ordinals=tuple(omitted),
        article_review_accepted=review_accepted,
    )


class ArticlePreparationService:
    def __init__(
        self,
        ingestion: ArticleIngestor,
        detector: LanguageDetector,
        script_detector: ScriptDetector,
        text_preparer: TextPreparer,
        *,
        max_segment_characters: int,
    ) -> None:
        if type(max_segment_characters) is not int or max_segment_characters <= 0:
            raise ValueError("max_segment_characters must be a positive integer")
        self._ingestion = ingestion
        self._detector = detector
        self._script_detector = script_detector
        self._text_preparer = text_preparer
        self._max_segment_characters = max_segment_characters

    def prepare(
        self,
        submitted_url: str,
        *,
        requested_language: Language | None = None,
        requested_script: Script | None = None,
        accept_article_review: bool = False,
    ) -> ArticlePreparationResult:
        ingestion = self._ingestion.ingest(submitted_url)
        return self.prepare_ingestion(
            ingestion,
            requested_language=requested_language,
            requested_script=requested_script,
            accept_article_review=accept_article_review,
        )

    def prepare_ingestion(
        self,
        ingestion: ArticleIngestionResult,
        *,
        requested_language: Language | None = None,
        requested_script: Script | None = None,
        accept_article_review: bool = False,
    ) -> ArticlePreparationResult:
        """Prepare an already-fetched immutable snapshot.

        Interactive clients use this seam to resolve a review or language choice
        without silently downloading a potentially changed article a second time.
        """

        if not isinstance(ingestion, ArticleIngestionResult):
            raise TypeError("ingestion must be an ArticleIngestionResult")
        if type(accept_article_review) is not bool:
            raise TypeError("accept_article_review must be a boolean")
        detection_text = _detection_text(ingestion)
        script_detection = self._script_detector.detect(detection_text)
        language_detection = (
            self._detector.detect(detection_text) if requested_language is None else None
        )
        selection = select_language(
            language_detection,
            script_detection,
            language_hint=ingestion.article.language_hint,
            requested_language=requested_language,
            requested_script=requested_script,
        )

        no_speech_content = ingestion.article.title is None and all(
            block.speech_text is None for block in ingestion.article.blocks
        )
        if (ingestion.article.needs_review and not accept_article_review) or no_speech_content:
            return ArticlePreparationResult(
                ingestion=ingestion,
                language_detection=language_detection,
                script_detection=script_detection,
                language_selection=selection,
                status=ArticlePreparationStatus.NEEDS_ARTICLE_REVIEW,
                prepared_article=None,
            )
        if selection.requires_override:
            return ArticlePreparationResult(
                ingestion=ingestion,
                language_detection=language_detection,
                script_detection=script_detection,
                language_selection=selection,
                status=ArticlePreparationStatus.NEEDS_LANGUAGE_OVERRIDE,
                prepared_article=None,
            )

        assert selection.language is not None
        assert selection.script is not None
        prepared_article = build_prepared_article(
            ingestion.article,
            language=selection.language,
            script=selection.script,
            max_segment_characters=self._max_segment_characters,
            review_accepted=accept_article_review,
            text_preparer=self._text_preparer,
        )
        return ArticlePreparationResult(
            ingestion=ingestion,
            language_detection=language_detection,
            script_detection=script_detection,
            language_selection=selection,
            status=ArticlePreparationStatus.READY,
            prepared_article=prepared_article,
        )


__all__ = [
    "ArticleIngestor",
    "ArticlePreparationResult",
    "ArticlePreparationService",
    "ArticlePreparationStatus",
    "build_prepared_article",
]
