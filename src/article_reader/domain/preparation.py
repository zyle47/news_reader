"""Cross-domain invariants for a source-traceable prepared article."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from itertools import pairwise

from article_reader.domain.article import ExtractedArticle
from article_reader.domain.text import PreparedText, SourceSpan

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArticlePreparationDomainError(ValueError):
    """Raised when prepared article mappings are inconsistent."""


@dataclass(frozen=True, slots=True)
class ArticleBlockSourceSpan:
    block_ordinal: int
    source_span: SourceSpan
    speech_text_sha256: str

    def __post_init__(self) -> None:
        if type(self.block_ordinal) is not int or self.block_ordinal < 0:
            raise ArticlePreparationDomainError("block_ordinal must be a non-negative integer")
        if not isinstance(self.source_span, SourceSpan):
            raise ArticlePreparationDomainError("source_span must be a SourceSpan")
        if _SHA256.fullmatch(self.speech_text_sha256) is None:
            raise ArticlePreparationDomainError("speech_text_sha256 must be a SHA-256 digest")


@dataclass(frozen=True, slots=True)
class PreparedArticle:
    article: ExtractedArticle
    prepared_text: PreparedText
    title_source_span: SourceSpan | None
    block_source_spans: tuple[ArticleBlockSourceSpan, ...]
    omitted_block_ordinals: tuple[int, ...]
    article_review_accepted: bool

    def __post_init__(self) -> None:
        if not isinstance(self.article, ExtractedArticle):
            raise ArticlePreparationDomainError("article must be an ExtractedArticle")
        if not isinstance(self.prepared_text, PreparedText):
            raise ArticlePreparationDomainError("prepared_text must be a PreparedText")
        if type(self.article_review_accepted) is not bool:
            raise ArticlePreparationDomainError("article_review_accepted must be a boolean")
        if self.article.needs_review and not self.article_review_accepted:
            raise ArticlePreparationDomainError(
                "a review-required article must be explicitly accepted"
            )

        original = self.prepared_text.original
        if self.article.title is None:
            if self.title_source_span is not None:
                raise ArticlePreparationDomainError(
                    "title span exists for an article without a title"
                )
        else:
            if not isinstance(self.title_source_span, SourceSpan):
                raise ArticlePreparationDomainError("a titled article requires a title source span")
            if original.slice(self.title_source_span) != self.article.title:
                raise ArticlePreparationDomainError(
                    "title source span does not map to the article title"
                )

        try:
            mappings = tuple(self.block_source_spans)
            omitted = tuple(self.omitted_block_ordinals)
        except TypeError as error:
            raise ArticlePreparationDomainError("block mappings must be iterable") from error
        if any(not isinstance(mapping, ArticleBlockSourceSpan) for mapping in mappings):
            raise ArticlePreparationDomainError("invalid article block source mapping")
        if any(type(ordinal) is not int or ordinal < 0 for ordinal in omitted):
            raise ArticlePreparationDomainError(
                "omitted block ordinals must be non-negative integers"
            )
        mapped_ordinals = [mapping.block_ordinal for mapping in mappings]
        if len(set(mapped_ordinals)) != len(mapped_ordinals) or len(set(omitted)) != len(omitted):
            raise ArticlePreparationDomainError("block ordinals cannot be duplicated")
        if mapped_ordinals != sorted(mapped_ordinals) or list(omitted) != sorted(omitted):
            raise ArticlePreparationDomainError("block ordinals must preserve source order")
        if set(mapped_ordinals).intersection(omitted):
            raise ArticlePreparationDomainError("a block cannot be both mapped and omitted")
        expected = set(range(len(self.article.blocks)))
        if set(mapped_ordinals).union(omitted) != expected:
            raise ArticlePreparationDomainError(
                "mapped and omitted blocks must partition the article"
            )

        expected_cursor = 0
        has_part = False
        if self.article.title is not None:
            expected_title_span = SourceSpan(0, len(self.article.title))
            if self.title_source_span != expected_title_span:
                raise ArticlePreparationDomainError(
                    "title source span is not at its expected offset"
                )
            expected_cursor = expected_title_span.end
            has_part = True
        for mapping in mappings:
            block = self.article.blocks[mapping.block_ordinal]
            if block.speech_text is None:
                raise ArticlePreparationDomainError("a mapped block requires speech text")
            if has_part:
                expected_cursor += 2
            expected_span = SourceSpan(expected_cursor, expected_cursor + len(block.speech_text))
            if mapping.source_span != expected_span:
                raise ArticlePreparationDomainError(
                    "block source span is not at its expected offset"
                )
            expected_cursor = expected_span.end
            has_part = True
            source_text = original.slice(mapping.source_span)
            if source_text != block.speech_text:
                raise ArticlePreparationDomainError(
                    "block source span does not map to its speech text"
                )
            digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            if digest != mapping.speech_text_sha256:
                raise ArticlePreparationDomainError("block speech-text digest does not match")
        spans = [mapping.source_span for mapping in mappings]
        if any(current.start < previous.end for previous, current in pairwise(spans)):
            raise ArticlePreparationDomainError(
                "block source spans must be ordered and non-overlapping"
            )

        expected_parts = ([self.article.title] if self.article.title is not None else []) + [
            self.article.blocks[ordinal].speech_text for ordinal in mapped_ordinals
        ]
        if original.text != "\n\n".join(part for part in expected_parts if part is not None):
            raise ArticlePreparationDomainError(
                "prepared original does not match the ordered article parts"
            )
        object.__setattr__(self, "block_source_spans", mappings)
        object.__setattr__(self, "omitted_block_ordinals", omitted)


__all__ = [
    "ArticleBlockSourceSpan",
    "ArticlePreparationDomainError",
    "PreparedArticle",
]
