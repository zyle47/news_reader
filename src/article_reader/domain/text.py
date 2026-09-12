"""Immutable text-preparation domain values.

These values describe a manually supplied article-equivalent text, its
segmentation for speech, and the source-span mapping back to the untouched
original. The original text is never rewritten; only sliced. Preparation
algorithms (normalization, segmentation) live in the concrete
``article_reader.text`` package, not here — this module holds only
validated, immutable data and the invariants that must always hold.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import pairwise

from article_reader.domain.speech import Language, Script

_MAX_ORIGINAL_CHARACTERS = 2_000_000
_MAX_SEGMENT_CHARACTERS = 100_000


class TextDomainError(ValueError):
    """Raised when an invalid value attempts to cross the text-preparation boundary."""


def _check_no_control_characters(text: str, field_name: str, *, allow_newlines: bool) -> None:
    if "\x00" in text:
        raise TextDomainError(f"{field_name} cannot contain NUL characters")
    allowed = {"\n", "\r", "\t"} if allow_newlines else set()
    for character in text:
        if ord(character) < 32 and character not in allowed:
            raise TextDomainError(f"{field_name} contains a control character")


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A half-open ``[start, end)`` character range into an ``OriginalText.text``."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if isinstance(self.start, bool) or not isinstance(self.start, int):
            raise TextDomainError("source span start must be an integer")
        if isinstance(self.end, bool) or not isinstance(self.end, int):
            raise TextDomainError("source span end must be an integer")
        if self.start < 0:
            raise TextDomainError("source span start cannot be negative")
        if self.end <= self.start:
            raise TextDomainError("source span end must be greater than start")

    @property
    def character_count(self) -> int:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class OriginalText:
    """Verbatim submitted text. Never rewritten; only sliced for display or speech."""

    text: str
    language: Language
    script: Script

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TextDomainError("original text must be a string")
        if not self.text.strip():
            raise TextDomainError("original text cannot be empty or whitespace-only")
        if len(self.text) > _MAX_ORIGINAL_CHARACTERS:
            raise TextDomainError(f"original text exceeds {_MAX_ORIGINAL_CHARACTERS} characters")
        _check_no_control_characters(self.text, "original text", allow_newlines=True)
        if not isinstance(self.language, Language):
            raise TextDomainError("language must be a supported Language enum")
        if not isinstance(self.script, Script):
            raise TextDomainError("script must be a supported Script enum")
        if self.language in {Language.ENGLISH, Language.GERMAN} and self.script is not Script.LATIN:
            raise TextDomainError("English and German text must be declared as Latin script")

    @property
    def character_count(self) -> int:
        return len(self.text)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def slice(self, span: SourceSpan) -> str:
        if not isinstance(span, SourceSpan):
            raise TextDomainError("span must be a validated SourceSpan")
        if span.end > len(self.text):
            raise TextDomainError("source span extends beyond the original text")
        return self.text[span.start : span.end]


@dataclass(frozen=True, slots=True)
class TextSegment:
    """One speech-ready unit of text, traceable to one or more source spans."""

    ordinal: int
    source_spans: tuple[SourceSpan, ...]
    speech_text: str

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise TextDomainError("segment ordinal must be a non-negative integer")

        try:
            spans = tuple(self.source_spans)
        except TypeError as error:
            raise TextDomainError(
                "source_spans must be an iterable of SourceSpan values"
            ) from error
        if not spans:
            raise TextDomainError("a text segment requires at least one source span")
        if any(not isinstance(span, SourceSpan) for span in spans):
            raise TextDomainError("source_spans must contain only SourceSpan values")
        for previous, current in pairwise(spans):
            if current.start < previous.end:
                raise TextDomainError("source_spans must be ordered and non-overlapping")
        object.__setattr__(self, "source_spans", spans)

        if not isinstance(self.speech_text, str):
            raise TextDomainError("speech_text must be a string")
        if not self.speech_text.strip():
            raise TextDomainError("speech_text cannot be empty or whitespace-only")
        if len(self.speech_text) > _MAX_SEGMENT_CHARACTERS:
            raise TextDomainError(f"speech_text exceeds {_MAX_SEGMENT_CHARACTERS} characters")
        _check_no_control_characters(self.speech_text, "speech_text", allow_newlines=False)

    @property
    def character_count(self) -> int:
        return len(self.speech_text)

    @property
    def speech_text_sha256(self) -> str:
        return hashlib.sha256(self.speech_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedText:
    """A complete, ordered, source-traceable preparation of one ``OriginalText``."""

    original: OriginalText
    segments: tuple[TextSegment, ...]
    normalizer_version: str
    segmenter_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.original, OriginalText):
            raise TextDomainError("original must be a validated OriginalText")

        try:
            segments = tuple(self.segments)
        except TypeError as error:
            raise TextDomainError("segments must be an iterable of TextSegment values") from error
        if not segments:
            raise TextDomainError("prepared text requires at least one segment")
        if any(not isinstance(segment, TextSegment) for segment in segments):
            raise TextDomainError("segments must contain only TextSegment values")
        for index, segment in enumerate(segments):
            if segment.ordinal != index:
                raise TextDomainError("segment ordinals must be sequential starting at 0")
        object.__setattr__(self, "segments", segments)

        original_length = len(self.original.text)
        highest_end_seen = -1
        for segment in segments:
            for span in segment.source_spans:
                if span.end > original_length:
                    raise TextDomainError("a source span extends beyond the original text")
                if span.start < highest_end_seen:
                    raise TextDomainError("source spans must be non-overlapping across segments")
                highest_end_seen = span.end

        for field_name in ("normalizer_version", "segmenter_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise TextDomainError(f"{field_name} must be a non-empty string")

    @property
    def total_speech_characters(self) -> int:
        return sum(segment.character_count for segment in self.segments)


__all__ = [
    "OriginalText",
    "PreparedText",
    "SourceSpan",
    "TextDomainError",
    "TextSegment",
]
