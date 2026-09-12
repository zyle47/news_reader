from __future__ import annotations

from pathlib import Path

import pytest

from article_reader.application.ports.fetch import FetchedPage
from article_reader.domain.article import ArticleBlockKind, ArticleReviewReason
from article_reader.extract.trafilatura_adapter import (
    ArticleExtractionError,
    ExtractionErrorCode,
    TrafilaturaArticleExtractor,
)


def _page(html: str, *, final_url: str = "https://example.com/story") -> FetchedPage:
    body = html.encode("utf-8")
    return FetchedPage(
        submitted_url="https://example.com/submitted#section",
        final_url=final_url,
        redirect_chain=(final_url,),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )


def _article_html(*, extra: str = "", title: bool = True) -> str:
    paragraphs = "".join(
        f"<p>Paragraph {index}. This is meaningful original fixture text with enough words "
        "to exercise the article extraction path. It contains another complete sentence.</p>"
        for index in range(1, 8)
    )
    title_markup = (
        '<title>Fixture page</title><link rel="canonical" href="/canonical">' if title else ""
    )
    heading = "<h1>Fixture article title</h1>" if title else ""
    return (
        f'<html lang="en-US"><head>{title_markup}</head><body>'
        "<nav>Navigation should not be retained.</nav><article>"
        f"{heading}{paragraphs}"
        "<blockquote>A meaningful quotation retained in source order for the reader.</blockquote>"
        "<ul><li>First useful list item.</li><li>Second useful list item.</li></ul>"
        f"{extra}</article><section class='comments'>Reader comment should be excluded.</section>"
        "</body></html>"
    )


def test_extracts_ordered_structure_without_downloading_again() -> None:
    page = _page(_article_html())

    article = TrafilaturaArticleExtractor().extract(page)

    assert article.title == "Fixture article title"
    assert article.canonical_url == "https://example.com/canonical"
    assert article.language_hint == "en-us"
    assert article.submitted_url.endswith("#section")
    assert article.needs_review is False
    assert [block.ordinal for block in article.blocks] == list(range(len(article.blocks)))
    kinds = [block.kind for block in article.blocks]
    assert ArticleBlockKind.PARAGRAPH in kinds
    assert ArticleBlockKind.QUOTE in kinds
    assert kinds.count(ArticleBlockKind.LIST_ITEM) == 2
    combined = " ".join(block.display_text for block in article.blocks)
    assert "Navigation should not" not in combined
    assert "Reader comment" not in combined


def test_simple_table_gets_a_speech_representation() -> None:
    table = "<table><tr><th>City</th><th>Value</th></tr><tr><td>Berlin</td><td>12</td></tr></table>"
    article = TrafilaturaArticleExtractor().extract(_page(_article_html(extra=table)))
    block = next(block for block in article.blocks if block.kind is ArticleBlockKind.TABLE)

    assert block.display_text == "City\tValue\nBerlin\t12"
    assert block.speech_text == "City: Berlin; Value: 12"
    assert block.requires_review is False


def test_code_is_preserved_but_requires_explicit_review() -> None:
    article = TrafilaturaArticleExtractor().extract(
        _page(_article_html(extra="<pre><code>value = 42\nprint(value)</code></pre>"))
    )
    block = next(block for block in article.blocks if block.kind is ArticleBlockKind.CODE)

    assert "value = 42" in block.display_text
    assert block.speech_text is None
    assert block.requires_review is True
    assert ArticleReviewReason.COMPLEX_CONTENT in article.review_reasons


def test_questionable_short_or_restriction_content_is_routed_to_review() -> None:
    page = _page(
        "<html><body><article><p>Verify you are human to continue reading.</p>"
        "</article></body></html>"
    )
    article = TrafilaturaArticleExtractor().extract(page)

    assert article.needs_review is True
    assert ArticleReviewReason.MISSING_TITLE in article.review_reasons
    assert ArticleReviewReason.SHORT_CONTENT in article.review_reasons
    assert ArticleReviewReason.FEW_BLOCKS in article.review_reasons
    assert ArticleReviewReason.RESTRICTION_PAGE in article.review_reasons


def test_extracted_character_limit_is_enforced() -> None:
    with pytest.raises(ArticleExtractionError) as caught:
        TrafilaturaArticleExtractor(max_article_characters=100).extract(_page(_article_html()))
    assert caught.value.code is ExtractionErrorCode.TOO_LARGE


def test_extractor_configuration_limit_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 1 and 100000"):
        TrafilaturaArticleExtractor(max_article_characters=100_001)


def test_canonical_fragment_is_removed() -> None:
    html = _article_html().replace('href="/canonical"', 'href="/canonical?edition=full#comments"')

    article = TrafilaturaArticleExtractor().extract(_page(html))

    assert article.canonical_url == "https://example.com/canonical?edition=full"


def test_empty_document_has_a_manual_path_error() -> None:
    with pytest.raises(ArticleExtractionError) as caught:
        TrafilaturaArticleExtractor().extract(
            _page("<html><head><title>Empty</title></head><body></body></html>")
        )
    assert caught.value.code is ExtractionErrorCode.EMPTY
    assert "manual-text" in str(caught.value)


def test_external_entity_markup_is_never_resolved(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("MUST_NOT_APPEAR", encoding="utf-8")
    uri = secret.as_uri()
    html = (
        f'<!DOCTYPE html [<!ENTITY xxe SYSTEM "{uri}">]><html><body><article>'
        + "".join(
            f"<p>Safe paragraph {index} with meaningful fixture content and no external "
            "data. &xxe;</p>"
            for index in range(8)
        )
        + "</article></body></html>"
    )

    article = TrafilaturaArticleExtractor().extract(_page(html))

    assert "MUST_NOT_APPEAR" not in " ".join(block.display_text for block in article.blocks)


def test_declared_legacy_html_charset_is_decoded_without_losing_diacritics() -> None:
    paragraphs = "".join(
        f"<p>Grüße aus Köln, Abschnitt {index}. Dieser längere deutsche Beispieltext "
        "prüft die sichere Zeichendekodierung des bereits geladenen Dokuments.</p>"
        for index in range(8)
    )
    body = (
        '<html lang="de"><head><meta charset="windows-1252"><title>Grüße aus Köln</title>'
        f"</head><body><article><h1>Grüße aus Köln</h1>{paragraphs}</article></body></html>"
    ).encode("windows-1252")
    page = FetchedPage(
        submitted_url="https://example.com/de",
        final_url="https://example.com/de",
        redirect_chain=("https://example.com/de",),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )

    article = TrafilaturaArticleExtractor().extract(page)

    assert article.title == "Grüße aus Köln"
    assert "Grüße aus Köln" in article.readable_text
