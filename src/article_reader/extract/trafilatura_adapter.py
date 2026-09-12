"""Trafilatura adapter that converts extracted structure into domain blocks."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from configparser import ConfigParser
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

from article_reader.application.ports.fetch import FetchedPage
from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleReviewReason,
    ExtractedArticle,
)

_SPACE = re.compile(r"[\t\f\v ]+")
_BLANK_LINES = re.compile(r"\n\s*\n+")
_RESTRICTION_PHRASES = (
    "verify you are human",
    "enable javascript to continue",
    "sign in to continue reading",
    "subscribe to continue reading",
    "access denied",
    "captcha",
)


class ExtractionErrorCode(StrEnum):
    PARSER_FAILED = "EXTRACTION_FAILED"
    EMPTY = "EXTRACTION_EMPTY"
    TOO_LARGE = "EXTRACTION_TOO_LARGE"


class ArticleExtractionError(RuntimeError):
    def __init__(self, code: ExtractionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Element(Protocol):
    tag: str

    def __iter__(self) -> Iterator[_Element]: ...

    def iter(self, tag: str | None = None) -> Iterator[_Element]: ...

    def itertext(self) -> Iterator[str]: ...

    def get(self, key: str, default: str | None = None) -> str | None: ...


class _Document(Protocol):
    title: str | None
    url: str | None
    language: str | None
    body: _Element


class _MetadataParser(HTMLParser):
    """Read inert head metadata; HTMLParser never retrieves external resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.canonical_url: str | None = None
        self.language_hint: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = {key.casefold(): value for key, value in attrs}
        if tag.casefold() == "html" and self.language_hint is None:
            self.language_hint = attributes.get("lang")
        if tag.casefold() == "link" and self.canonical_url is None:
            relationships = (attributes.get("rel") or "").casefold().split()
            if "canonical" in relationships:
                self.canonical_url = attributes.get("href")


def _page_metadata(body: bytes) -> _MetadataParser:
    parser = _MetadataParser()
    try:
        parser.feed(body[:65_536].decode("utf-8", errors="ignore"))
        parser.close()
    except Exception:
        return _MetadataParser()
    return parser


def _tag(element: _Element) -> str:
    return str(element.tag).rsplit("}", 1)[-1].casefold()


def _element_text(element: _Element, *, preserve_lines: bool = False) -> str:
    pieces = [str(piece) for piece in element.itertext()]
    text = unicodedata.normalize("NFC", "".join(pieces)).replace("\r\n", "\n").replace("\r", "\n")
    if preserve_lines:
        lines = [_SPACE.sub(" ", line).strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line).strip()
    return " ".join(text.split()).strip()


def _safe_metadata_url(value: object, final_url: str) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4_096:
        return None
    candidate = urljoin(final_url, value.strip())
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (port is not None and port not in {80, 443})
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _clean_title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    title = " ".join(unicodedata.normalize("NFC", value).split()).strip()
    if not title or len(title) > 500 or any(ord(character) < 32 for character in title):
        return None
    return title


def _language_hint(document: _Document, metadata: _MetadataParser) -> str | None:
    raw = document.language
    if isinstance(raw, str) and raw.strip():
        normalized = raw.strip().casefold().replace("_", "-")
        if len(normalized) <= 35 and all(
            character.isalnum() or character == "-" for character in normalized
        ):
            return normalized
    hint = metadata.language_hint
    if hint is None:
        return None
    normalized = hint.strip().casefold().replace("_", "-")
    if not normalized or len(normalized) > 35:
        return None
    valid = all(character.isalnum() or character == "-" for character in normalized)
    return normalized if valid else None


def _table_block(
    element: _Element,
    ordinal: int,
    *,
    maximum_characters: int,
) -> ArticleBlock | None:
    rows: list[list[tuple[str, bool]]] = []
    for row in element.iter("row"):
        cells: list[tuple[str, bool]] = []
        for cell in row.iter("cell"):
            text = _element_text(cell)
            if text:
                cells.append((text, cell.get("role") == "head"))
        if cells:
            rows.append(cells)
    if not rows:
        return None
    display_text = "\n".join("\t".join(text for text, _head in row) for row in rows)
    if len(display_text) > maximum_characters:
        raise ArticleExtractionError(
            ExtractionErrorCode.TOO_LARGE,
            "extracted article exceeds the configured character limit",
        )
    complex_table = len(rows) > 20 or any(len(row) > 10 for row in rows)
    speech_text: str | None = None
    if not complex_table:
        headers = [text for text, is_header in rows[0] if is_header]
        data_rows = rows[1:] if headers and len(headers) == len(rows[0]) else rows
        spoken_rows: list[str] = []
        for data_row in data_rows:
            values = [text for text, _is_header in data_row]
            if headers and len(values) == len(headers):
                spoken_rows.append(
                    "; ".join(
                        f"{header}: {value}" for header, value in zip(headers, values, strict=True)
                    )
                )
            else:
                spoken_rows.append("; ".join(values))
        speech_text = ". ".join(spoken_rows).strip() or None
        if speech_text is not None and len(speech_text) > maximum_characters:
            raise ArticleExtractionError(
                ExtractionErrorCode.TOO_LARGE,
                "extracted article exceeds the configured character limit",
            )
    return ArticleBlock(
        ordinal=ordinal,
        kind=ArticleBlockKind.TABLE,
        display_text=display_text,
        speech_text=speech_text,
        requires_review=complex_table or speech_text is None,
    )


class TrafilaturaArticleExtractor:
    VERSION = "trafilatura-2.2-blocks-v1"

    def __init__(self, *, max_article_characters: int = 100_000) -> None:
        if type(max_article_characters) is not int or not 1 <= max_article_characters <= 100_000:
            raise ValueError("max_article_characters must be between 1 and 100000")
        from trafilatura.settings import use_config

        self._max_article_characters = max_article_characters
        self._config: ConfigParser = use_config()
        self._config.set("DEFAULT", "MAX_FILE_SIZE", str(max_article_characters * 4))
        self._config.set("DEFAULT", "MAX_TREE_SIZE", str(max_article_characters * 2))

    def extract(self, page: FetchedPage) -> ExtractedArticle:
        from trafilatura import bare_extraction
        from trafilatura.settings import Document

        try:
            result = bare_extraction(
                page.body,
                url=page.final_url,
                with_metadata=True,
                include_comments=False,
                include_tables=True,
                include_images=False,
                include_formatting=True,
                include_links=False,
                deduplicate=True,
                config=self._config,
                as_dict=False,
            )
        except Exception as error:
            raise ArticleExtractionError(
                ExtractionErrorCode.PARSER_FAILED,
                "article extraction failed",
            ) from error
        if not isinstance(result, Document):
            raise ArticleExtractionError(
                ExtractionErrorCode.EMPTY,
                "no useful article content could be extracted; use the manual-text path",
            )

        document = cast(_Document, result)
        title = _clean_title(document.title)
        metadata = _page_metadata(page.body)
        body = document.body
        pending: list[ArticleBlock] = []

        def add(
            kind: ArticleBlockKind,
            text: str,
            *,
            speech: str | None = None,
            review: bool = False,
        ) -> None:
            if not text:
                return
            if len(text) > self._max_article_characters or (
                speech is not None and len(speech) > self._max_article_characters
            ):
                raise ArticleExtractionError(
                    ExtractionErrorCode.TOO_LARGE,
                    "extracted article exceeds the configured character limit",
                )
            if pending and pending[-1].kind is kind and pending[-1].display_text == text:
                return
            pending.append(
                ArticleBlock(
                    ordinal=len(pending),
                    kind=kind,
                    display_text=text,
                    speech_text=text if speech is None and not review else speech,
                    requires_review=review,
                )
            )

        def visit(element: _Element) -> None:
            name = _tag(element)
            if name == "head":
                text = _element_text(element)
                if not (not pending and title is not None and text.casefold() == title.casefold()):
                    add(ArticleBlockKind.HEADING, text)
                return
            if name == "p":
                add(ArticleBlockKind.PARAGRAPH, _element_text(element))
                return
            if name in {"quote", "blockquote"}:
                add(ArticleBlockKind.QUOTE, _element_text(element))
                return
            if name == "list":
                for item in element.iter("item"):
                    add(ArticleBlockKind.LIST_ITEM, _element_text(item))
                return
            if name == "table":
                table = _table_block(
                    element,
                    len(pending),
                    maximum_characters=self._max_article_characters,
                )
                if table is not None:
                    pending.append(table)
                return
            if name in {"code", "pre"}:
                add(
                    ArticleBlockKind.CODE,
                    _element_text(element, preserve_lines=True),
                    speech=None,
                    review=True,
                )
                return
            for child in element:
                visit(child)

        visit(body)
        if not pending:
            raise ArticleExtractionError(
                ExtractionErrorCode.EMPTY,
                "no useful article content could be extracted; use the manual-text path",
            )
        total_characters = sum(len(block.display_text) for block in pending)
        if total_characters > self._max_article_characters:
            raise ArticleExtractionError(
                ExtractionErrorCode.TOO_LARGE,
                "extracted article exceeds the configured character limit",
            )

        review_reasons: list[ArticleReviewReason] = []
        if title is None:
            review_reasons.append(ArticleReviewReason.MISSING_TITLE)
        if total_characters < 200:
            review_reasons.append(ArticleReviewReason.SHORT_CONTENT)
        if len(pending) < 2:
            review_reasons.append(ArticleReviewReason.FEW_BLOCKS)
        extracted_text = " ".join(block.display_text for block in pending)
        source_prefix = page.body[:65_536].decode("utf-8", errors="ignore")
        restriction_text = f"{extracted_text} {source_prefix}".casefold()
        if any(phrase in restriction_text for phrase in _RESTRICTION_PHRASES):
            review_reasons.append(ArticleReviewReason.RESTRICTION_PAGE)
        if any(block.requires_review for block in pending):
            review_reasons.append(ArticleReviewReason.COMPLEX_CONTENT)

        return ExtractedArticle(
            submitted_url=page.submitted_url,
            final_url=page.final_url,
            canonical_url=_safe_metadata_url(
                metadata.canonical_url or document.url,
                page.final_url,
            ),
            title=title,
            language_hint=_language_hint(document, metadata),
            blocks=tuple(pending),
            review_reasons=tuple(review_reasons),
            extraction_version=self.VERSION,
        )


__all__ = [
    "ArticleExtractionError",
    "ExtractionErrorCode",
    "TrafilaturaArticleExtractor",
]
