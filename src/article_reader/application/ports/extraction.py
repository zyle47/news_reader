"""Application-owned article extraction boundary."""

from __future__ import annotations

from typing import Protocol

from article_reader.application.ports.fetch import FetchedPage
from article_reader.domain.article import ExtractedArticle


class ArticleExtractor(Protocol):
    def extract(self, page: FetchedPage) -> ExtractedArticle:
        """Extract ordered article blocks from already-fetched bytes."""
        ...


__all__ = ["ArticleExtractor"]
