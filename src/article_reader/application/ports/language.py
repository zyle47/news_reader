"""Application-owned local language-detection boundary."""

from __future__ import annotations

from typing import Protocol

from article_reader.domain.language import LanguageDetection


class LanguageDetector(Protocol):
    def detect(self, text: str) -> LanguageDetection:
        """Rank language candidates for already-extracted text without network access."""
        ...


__all__ = ["LanguageDetector"]
