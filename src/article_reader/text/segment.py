"""Sentence/paragraph segmentation with exact source-span tracking.

Segmentation works directly on the untouched original text so every
resulting span slices back to it exactly (``original.text[start:end]``).
Language-aware rules avoid splitting sentences at known abbreviations,
decimal/thousands numbers, or short ordinal-date markers common in German
and Serbian ("11. September", "11. septembra"). This is a pragmatic,
rule-based splitter, not a statistical sentence tokenizer: it is tuned to
the punctuation patterns the project plan calls out (abbreviations,
quotations, dates, decimal commas, numbers), not to every possible edge
case in unrestricted prose.
"""

from __future__ import annotations

import re

from article_reader.domain.speech import Language
from article_reader.domain.text import OriginalText, PreparedText, SourceSpan, TextSegment
from article_reader.text.normalize import NORMALIZER_VERSION, normalize_speech_text

SEGMENTER_VERSION = "1"

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n[ \t\n]*")
_TERMINAL_CHARACTERS = ".!?…"  # . ! ? …
_CLOSING_CHARACTERS = "\"'”’)]»"  # noqa: RUF001 -- straight/curly quotes, brackets, guillemet
_OPENING_CHARACTERS = "\"'“‘([{«"  # noqa: RUF001 -- straight/curly quotes, brackets, guillemet
_SHORT_NUMBER_PERIOD = re.compile(r"(?<![\d.])\d{1,2}\.(?!\d)")

_ABBREVIATION_PATTERNS: dict[Language, tuple[re.Pattern[str], ...]] = {
    Language.ENGLISH: (
        re.compile(r"\b(?:Dr|Mr|Mrs|Ms|Prof|St|Jr|Sr|vs|etc|approx|no|No)\."),
        re.compile(r"\b[Ee]\.g\."),
        re.compile(r"\b[Ii]\.e\."),
    ),
    Language.GERMAN: (
        re.compile(r"\b(?:Dr|Prof|Nr|St|ca|bzw|usw|inkl|exkl|Abb|Kap|Str)\."),
        re.compile(r"\bz\.\s?B\."),
        re.compile(r"\bd\.\s?h\."),
        re.compile(r"\bu\.\s?a\."),
    ),
    Language.SERBIAN: (
        re.compile(r"\b(?:dr|prof|god|gosp|itd|npr|str|sl)\.", re.IGNORECASE),
        re.compile(r"\b(?:др|проф|год|госп|итд|нпр|стр|тзв)\.", re.IGNORECASE),
    ),
}

_ORDINAL_DATE_LANGUAGES = frozenset({Language.GERMAN, Language.SERBIAN})


def _numeric_period_indices(text: str) -> set[int]:
    return {
        index
        for index, character in enumerate(text)
        if character == "."
        and 0 < index < len(text) - 1
        and text[index - 1].isdigit()
        and text[index + 1].isdigit()
    }


def _abbreviation_period_indices(text: str, language: Language) -> set[int]:
    protected: set[int] = set()
    for pattern in _ABBREVIATION_PATTERNS.get(language, ()):
        for match in pattern.finditer(text):
            start = match.start()
            for offset, character in enumerate(match.group()):
                if character == ".":
                    protected.add(start + offset)
    return protected


def _ordinal_date_period_indices(text: str, language: Language) -> set[int]:
    if language not in _ORDINAL_DATE_LANGUAGES:
        return set()
    return {match.end() - 1 for match in _SHORT_NUMBER_PERIOD.finditer(text)}


def _protected_period_indices(text: str, language: Language) -> set[int]:
    return (
        _numeric_period_indices(text)
        | _abbreviation_period_indices(text, language)
        | _ordinal_date_period_indices(text, language)
    )


def _looks_like_new_sentence_start(text: str, position: int) -> bool:
    """Whether the content at/after ``position`` plausibly begins a new sentence.

    Used only to decide whether terminal punctuation found *inside* a quoted
    or parenthetical aside (e.g. ``She said "Stop!" and left.``) should end
    the sentence there, instead of continuing to the aside's true end.
    """

    index = position
    length = len(text)
    while index < length and text[index].isspace():
        index += 1
    if index >= length:
        return True
    character = text[index]
    return character.isupper() or character.isdigit() or character in _OPENING_CHARACTERS


def _sentence_end_positions(text: str, language: Language) -> list[int]:
    protected = _protected_period_indices(text, language)
    boundaries: list[int] = []
    length = len(text)
    index = 0
    while index < length:
        character = text[index]
        if character in _TERMINAL_CHARACTERS:
            if character == "." and index in protected:
                index += 1
                continue
            end = index + 1
            while end < length and text[end] in _TERMINAL_CHARACTERS:
                if text[end] == "." and end in protected:
                    break
                end += 1
            while end < length and text[end] in _CLOSING_CHARACTERS:
                end += 1
            if _looks_like_new_sentence_start(text, end):
                boundaries.append(end)
                index = end
            else:
                index += 1
        else:
            index += 1
    return boundaries


def _trim_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return None
    return (start, end)


def find_paragraph_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return trimmed ``(start, end)`` paragraph spans separated by blank lines."""

    if not text:
        return ()
    spans: list[tuple[int, int]] = []
    cursor = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        trimmed = _trim_span(text, cursor, match.start())
        if trimmed is not None:
            spans.append(trimmed)
        cursor = match.end()
    trimmed = _trim_span(text, cursor, len(text))
    if trimmed is not None:
        spans.append(trimmed)
    return tuple(spans)


def find_sentence_spans(paragraph_text: str, language: Language) -> tuple[tuple[int, int], ...]:
    """Return trimmed ``(start, end)`` sentence spans covering all of ``paragraph_text``."""

    if not isinstance(language, Language):
        raise TypeError("language must be a Language enum")
    if not paragraph_text:
        return ()

    boundaries = _sentence_end_positions(paragraph_text, language)
    spans: list[tuple[int, int]] = []
    start = 0
    for boundary in boundaries:
        trimmed = _trim_span(paragraph_text, start, boundary)
        if trimmed is not None:
            spans.append(trimmed)
        start = boundary
    trimmed = _trim_span(paragraph_text, start, len(paragraph_text))
    if trimmed is not None:
        spans.append(trimmed)
    return tuple(spans)


def _split_long_span(
    original_text: str, span: tuple[int, int], max_characters: int
) -> list[tuple[int, int]]:
    start, end = span
    pieces: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        remaining = end - cursor
        if remaining <= max_characters:
            trimmed = _trim_span(original_text, cursor, end)
            if trimmed is not None:
                pieces.append(trimmed)
            break
        limit = cursor + max_characters
        split_at = None
        for index in range(limit, cursor, -1):
            if original_text[index - 1].isspace():
                split_at = index
                break
        if split_at is None:
            split_at = limit
        trimmed = _trim_span(original_text, cursor, split_at)
        if trimmed is not None:
            pieces.append(trimmed)
        cursor = split_at
        while cursor < end and original_text[cursor].isspace():
            cursor += 1
    return pieces


def _pack_spans_into_segments(
    original: OriginalText,
    sentence_spans: list[tuple[int, int]],
    max_characters: int,
) -> tuple[TextSegment, ...]:
    atomic_spans: list[tuple[int, int]] = []
    for span in sentence_spans:
        start, end = span
        if end - start > max_characters:
            atomic_spans.extend(_split_long_span(original.text, span, max_characters))
        else:
            atomic_spans.append(span)

    segments: list[TextSegment] = []
    current_spans: list[tuple[int, int]] = []
    current_length = 0

    def flush() -> None:
        nonlocal current_spans, current_length
        if not current_spans:
            return
        source_spans = tuple(SourceSpan(s, e) for s, e in current_spans)
        pieces = [normalize_speech_text(original.text[s:e]) for s, e in current_spans]
        speech_text = " ".join(piece for piece in pieces if piece)
        segments.append(
            TextSegment(ordinal=len(segments), source_spans=source_spans, speech_text=speech_text)
        )
        current_spans = []
        current_length = 0

    for start, end in atomic_spans:
        piece_length = end - start
        joiner = 1 if current_spans else 0
        if current_spans and current_length + joiner + piece_length > max_characters:
            flush()
            joiner = 0
        current_spans.append((start, end))
        current_length += joiner + piece_length
    flush()
    return tuple(segments)


def prepare_text(original: OriginalText, *, max_segment_characters: int) -> PreparedText:
    """Normalize and segment ``original`` into a source-traceable ``PreparedText``."""

    if not isinstance(original, OriginalText):
        raise TypeError("original must be a validated OriginalText")
    if isinstance(max_segment_characters, bool) or not isinstance(max_segment_characters, int):
        raise TypeError("max_segment_characters must be an integer")
    if max_segment_characters <= 0:
        raise ValueError("max_segment_characters must be positive")

    paragraph_spans = find_paragraph_spans(original.text)
    if not paragraph_spans:
        raise ValueError("original text has no segmentable content")

    sentence_spans: list[tuple[int, int]] = []
    for paragraph_start, paragraph_end in paragraph_spans:
        paragraph_text = original.text[paragraph_start:paragraph_end]
        for relative_start, relative_end in find_sentence_spans(paragraph_text, original.language):
            sentence_spans.append(
                (paragraph_start + relative_start, paragraph_start + relative_end)
            )

    segments = _pack_spans_into_segments(original, sentence_spans, max_segment_characters)
    return PreparedText(
        original=original,
        segments=segments,
        normalizer_version=NORMALIZER_VERSION,
        segmenter_version=SEGMENTER_VERSION,
    )


class RuleBasedTextPreparer:
    def prepare(
        self,
        original: OriginalText,
        *,
        max_segment_characters: int,
    ) -> PreparedText:
        return prepare_text(original, max_segment_characters=max_segment_characters)


__all__ = [
    "SEGMENTER_VERSION",
    "RuleBasedTextPreparer",
    "find_paragraph_spans",
    "find_sentence_spans",
    "prepare_text",
]
