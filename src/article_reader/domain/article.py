"""Immutable extracted-article values and review invariants."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit


class ArticleDomainError(ValueError):
    """Raised when extracted article data violates a domain invariant."""


class ArticleBlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    QUOTE = "quote"
    TABLE = "table"
    CODE = "code"


class ArticleReviewReason(StrEnum):
    MISSING_TITLE = "missing_title"
    SHORT_CONTENT = "short_content"
    FEW_BLOCKS = "few_blocks"
    RESTRICTION_PAGE = "restriction_page"
    COMPLEX_CONTENT = "complex_content"


def _validate_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ArticleDomainError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ArticleDomainError(f"{name} must be non-empty without outer whitespace")
    if len(value) > maximum:
        raise ArticleDomainError(f"{name} exceeds {maximum} characters")
    if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in value):
        raise ArticleDomainError(f"{name} contains a control character")
    return value


def _validate_http_url(value: object, name: str) -> str:
    text = _validate_text(value, name, maximum=4_096)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as error:
        raise ArticleDomainError(f"{name} is malformed") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ArticleDomainError(f"{name} must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ArticleDomainError(f"{name} cannot contain user information")
    if port is not None and port not in {80, 443}:
        raise ArticleDomainError(f"{name} uses an unsupported port")
    return text


@dataclass(frozen=True, slots=True)
class ArticleBlock:
    ordinal: int
    kind: ArticleBlockKind
    display_text: str
    speech_text: str | None
    requires_review: bool = False

    def __post_init__(self) -> None:
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise ArticleDomainError("article block ordinal must be a non-negative integer")
        if not isinstance(self.kind, ArticleBlockKind):
            raise ArticleDomainError("article block kind must be an ArticleBlockKind")
        object.__setattr__(
            self,
            "display_text",
            _validate_text(self.display_text, "article block display_text", maximum=100_000),
        )
        if self.speech_text is not None:
            object.__setattr__(
                self,
                "speech_text",
                _validate_text(self.speech_text, "article block speech_text", maximum=100_000),
            )
        if type(self.requires_review) is not bool:
            raise ArticleDomainError("article block requires_review must be a boolean")
        if self.speech_text is None and not self.requires_review:
            raise ArticleDomainError("an omitted speech representation must require review")

    @property
    def text_sha256(self) -> str:
        return hashlib.sha256(self.display_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExtractedArticle:
    submitted_url: str
    final_url: str
    canonical_url: str | None
    title: str | None
    language_hint: str | None
    blocks: tuple[ArticleBlock, ...]
    review_reasons: tuple[ArticleReviewReason, ...]
    extraction_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "submitted_url", _validate_http_url(self.submitted_url, "submitted_url")
        )
        object.__setattr__(self, "final_url", _validate_http_url(self.final_url, "final_url"))
        if self.canonical_url is not None:
            object.__setattr__(
                self,
                "canonical_url",
                _validate_http_url(self.canonical_url, "canonical_url"),
            )
        if self.title is not None:
            object.__setattr__(self, "title", _validate_text(self.title, "title", maximum=500))
        if self.language_hint is not None:
            hint = _validate_text(self.language_hint, "language_hint", maximum=35)
            if not all(character.isalnum() or character in {"-", "_"} for character in hint):
                raise ArticleDomainError("language_hint contains unsupported characters")
            object.__setattr__(self, "language_hint", hint.casefold().replace("_", "-"))
        if not self.blocks:
            raise ArticleDomainError("an extracted article requires at least one block")
        for expected, block in enumerate(self.blocks):
            if not isinstance(block, ArticleBlock) or block.ordinal != expected:
                raise ArticleDomainError("article block ordinals must be contiguous from zero")
        if len(set(self.review_reasons)) != len(self.review_reasons):
            raise ArticleDomainError("article review reasons must be unique")
        if any(not isinstance(reason, ArticleReviewReason) for reason in self.review_reasons):
            raise ArticleDomainError("invalid article review reason")
        object.__setattr__(
            self,
            "extraction_version",
            _validate_text(self.extraction_version, "extraction_version", maximum=100),
        )

    @property
    def needs_review(self) -> bool:
        return bool(self.review_reasons) or any(block.requires_review for block in self.blocks)

    @property
    def readable_text(self) -> str:
        return "\n\n".join(
            block.speech_text for block in self.blocks if block.speech_text is not None
        )


__all__ = [
    "ArticleBlock",
    "ArticleBlockKind",
    "ArticleDomainError",
    "ArticleReviewReason",
    "ExtractedArticle",
]
