"""Fetch-and-extract orchestration independent of concrete network/parser libraries."""

from __future__ import annotations

from dataclasses import dataclass

from article_reader.application.ports.extraction import ArticleExtractor
from article_reader.application.ports.fetch import ArticleFetcher, FetchedPage
from article_reader.domain.article import ExtractedArticle


@dataclass(frozen=True, slots=True)
class ArticleIngestionResult:
    page: FetchedPage
    article: ExtractedArticle


class ArticleIngestionService:
    def __init__(self, fetcher: ArticleFetcher, extractor: ArticleExtractor) -> None:
        self._fetcher = fetcher
        self._extractor = extractor

    def ingest(self, submitted_url: str) -> ArticleIngestionResult:
        page = self._fetcher.fetch(submitted_url)
        article = self._extractor.extract(page)
        return ArticleIngestionResult(page=page, article=article)


__all__ = ["ArticleIngestionResult", "ArticleIngestionService"]
