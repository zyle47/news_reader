from __future__ import annotations

from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionService
from article_reader.domain.article import ArticleBlock, ArticleBlockKind, ExtractedArticle


class _Fetcher:
    def __init__(self, page: FetchedPage) -> None:
        self.page = page
        self.calls: list[str] = []

    def fetch(self, submitted_url: str) -> FetchedPage:
        self.calls.append(submitted_url)
        return self.page


class _Extractor:
    def __init__(self, article: ExtractedArticle) -> None:
        self.article = article
        self.pages: list[FetchedPage] = []

    def extract(self, page: FetchedPage) -> ExtractedArticle:
        self.pages.append(page)
        return self.article


def test_service_passes_only_fetched_bytes_to_extractor() -> None:
    page = FetchedPage(
        submitted_url="https://example.com/a",
        final_url="https://example.com/a",
        redirect_chain=("https://example.com/a",),
        status_code=200,
        media_type="text/html",
        body=b"<html><body><article>safe bytes</article></body></html>",
        response_bytes=58,
    )
    article = ExtractedArticle(
        submitted_url=page.submitted_url,
        final_url=page.final_url,
        canonical_url=None,
        title="Title",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="safe bytes",
                speech_text="safe bytes",
            ),
        ),
        review_reasons=(),
        extraction_version="fixture-v1",
    )
    fetcher = _Fetcher(page)
    extractor = _Extractor(article)

    result = ArticleIngestionService(fetcher, extractor).ingest(page.submitted_url)

    assert result.page is page
    assert result.article is article
    assert fetcher.calls == [page.submitted_url]
    assert extractor.pages == [page]
