from __future__ import annotations

from dataclasses import replace

import pytest

from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleDomainError,
    ArticleReviewReason,
    ExtractedArticle,
)


def _block(ordinal: int = 0) -> ArticleBlock:
    return ArticleBlock(
        ordinal=ordinal,
        kind=ArticleBlockKind.PARAGRAPH,
        display_text="Useful article paragraph.",
        speech_text="Useful article paragraph.",
    )


def _article() -> ExtractedArticle:
    return ExtractedArticle(
        submitted_url="https://example.com/story?edition=full#section",
        final_url="https://www.example.com/story?edition=full",
        canonical_url="https://example.com/canonical",
        title="A useful article",
        language_hint="EN_us",
        blocks=(_block(),),
        review_reasons=(ArticleReviewReason.FEW_BLOCKS,),
        extraction_version="fixture-v1",
    )


def test_article_values_are_immutable_and_derive_stable_hashes() -> None:
    article = _article()

    assert article.language_hint == "en-us"
    assert article.needs_review is True
    assert article.readable_text == "Useful article paragraph."
    assert len(article.blocks[0].text_sha256) == 64


def test_block_without_speech_must_be_visible_for_review() -> None:
    with pytest.raises(ArticleDomainError, match="must require review"):
        replace(_block(), speech_text=None)


def test_reviewable_code_can_preserve_display_text_without_speech() -> None:
    block = ArticleBlock(
        ordinal=0,
        kind=ArticleBlockKind.CODE,
        display_text="print('hello')",
        speech_text=None,
        requires_review=True,
    )

    assert block.requires_review is True


def test_article_requires_contiguous_block_ordinals() -> None:
    with pytest.raises(ArticleDomainError, match="contiguous"):
        replace(_article(), blocks=(_block(1),))


def test_article_rejects_unsafe_metadata_urls() -> None:
    with pytest.raises(ArticleDomainError):
        replace(_article(), submitted_url="file:///tmp/article.html")
    with pytest.raises(ArticleDomainError):
        replace(_article(), final_url="https://user:secret@example.com/story")
    with pytest.raises(ArticleDomainError):
        replace(_article(), canonical_url="https://example.com:8443/story")


def test_article_rejects_duplicate_review_reasons() -> None:
    with pytest.raises(ArticleDomainError, match="unique"):
        replace(
            _article(),
            review_reasons=(ArticleReviewReason.SHORT_CONTENT,) * 2,
        )
