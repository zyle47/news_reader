"""Application-owned boundaries for deterministic text preparation."""

from __future__ import annotations

from typing import Protocol

from article_reader.domain.language import ScriptDetection
from article_reader.domain.text import OriginalText, PreparedText


class ScriptDetector(Protocol):
    def detect(self, text: str) -> ScriptDetection:
        """Return local writing-system evidence without rewriting text."""
        ...


class TextPreparer(Protocol):
    def prepare(
        self,
        original: OriginalText,
        *,
        max_segment_characters: int,
    ) -> PreparedText:
        """Normalize and segment text while preserving exact source spans."""
        ...


__all__ = ["ScriptDetector", "TextPreparer"]
