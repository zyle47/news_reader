from __future__ import annotations

import pytest

from article_reader.domain.speech import Language, Script
from article_reader.domain.text import (
    OriginalText,
    PreparedText,
    SourceSpan,
    TextDomainError,
    TextSegment,
)


def _original(text: str = "Prvi deo. Drugi deo.") -> OriginalText:
    return OriginalText(text=text, language=Language.SERBIAN, script=Script.LATIN)


class TestSourceSpan:
    def test_valid_span_reports_character_count(self) -> None:
        span = SourceSpan(3, 10)
        assert span.character_count == 7

    @pytest.mark.parametrize(
        "start,end",
        [
            (-1, 5),
            (5, 5),
            (5, 3),
        ],
    )
    def test_rejects_invalid_bounds(self, start: int, end: int) -> None:
        with pytest.raises(TextDomainError):
            SourceSpan(start, end)

    def test_rejects_non_integer_bounds(self) -> None:
        with pytest.raises(TextDomainError, match="integer"):
            SourceSpan(True, 5)
        with pytest.raises(TextDomainError, match="integer"):
            SourceSpan(0, 5.5)  # type: ignore[arg-type]


class TestOriginalText:
    def test_accepts_valid_text_and_exposes_sha256_and_count(self) -> None:
        original = _original("Hello world")
        assert original.character_count == len("Hello world")
        assert len(original.sha256) == 64
        assert original.language is Language.SERBIAN

    def test_rejects_empty_or_whitespace_only(self) -> None:
        with pytest.raises(TextDomainError, match="empty"):
            OriginalText(text="   ", language=Language.ENGLISH, script=Script.LATIN)

    def test_rejects_nul_and_control_characters_but_allows_newlines(self) -> None:
        OriginalText(text="line one\nline two", language=Language.ENGLISH, script=Script.LATIN)
        with pytest.raises(TextDomainError, match="NUL"):
            OriginalText(text="bad\x00text", language=Language.ENGLISH, script=Script.LATIN)
        with pytest.raises(TextDomainError, match="control character"):
            OriginalText(text="bad\x07text", language=Language.ENGLISH, script=Script.LATIN)

    def test_rejects_oversized_text(self) -> None:
        with pytest.raises(TextDomainError, match="exceeds"):
            OriginalText(text="a" * 2_000_001, language=Language.ENGLISH, script=Script.LATIN)

    def test_english_and_german_must_be_latin_script(self) -> None:
        with pytest.raises(TextDomainError, match="Latin"):
            OriginalText(text="text", language=Language.ENGLISH, script=Script.CYRILLIC)
        with pytest.raises(TextDomainError, match="Latin"):
            OriginalText(text="text", language=Language.GERMAN, script=Script.CYRILLIC)

    def test_serbian_accepts_either_script(self) -> None:
        OriginalText(text="tekst", language=Language.SERBIAN, script=Script.LATIN)
        OriginalText(text="текст", language=Language.SERBIAN, script=Script.CYRILLIC)

    def test_slice_returns_exact_substring(self) -> None:
        original = _original("0123456789")
        assert original.slice(SourceSpan(2, 5)) == "234"

    def test_slice_rejects_span_beyond_text(self) -> None:
        original = _original("short")
        with pytest.raises(TextDomainError, match="beyond"):
            original.slice(SourceSpan(0, 100))

    def test_two_equal_texts_have_equal_sha256(self) -> None:
        assert _original("same text").sha256 == _original("same text").sha256


class TestTextSegment:
    def test_valid_segment(self) -> None:
        segment = TextSegment(ordinal=0, source_spans=(SourceSpan(0, 5),), speech_text="Hello")
        assert segment.character_count == 5
        assert len(segment.speech_text_sha256) == 64

    def test_rejects_negative_or_non_integer_ordinal(self) -> None:
        with pytest.raises(TextDomainError, match="ordinal"):
            TextSegment(ordinal=-1, source_spans=(SourceSpan(0, 5),), speech_text="Hello")
        with pytest.raises(TextDomainError, match="ordinal"):
            TextSegment(ordinal=True, source_spans=(SourceSpan(0, 5),), speech_text="Hello")

    def test_requires_at_least_one_span(self) -> None:
        with pytest.raises(TextDomainError, match="at least one"):
            TextSegment(ordinal=0, source_spans=(), speech_text="Hello")

    def test_rejects_overlapping_or_unordered_spans(self) -> None:
        with pytest.raises(TextDomainError, match="ordered and non-overlapping"):
            TextSegment(
                ordinal=0,
                source_spans=(SourceSpan(5, 10), SourceSpan(0, 6)),
                speech_text="Hello",
            )

    def test_touching_spans_are_allowed(self) -> None:
        TextSegment(
            ordinal=0,
            source_spans=(SourceSpan(0, 5), SourceSpan(5, 10)),
            speech_text="Hello",
        )

    def test_rejects_empty_speech_text(self) -> None:
        with pytest.raises(TextDomainError, match="empty"):
            TextSegment(ordinal=0, source_spans=(SourceSpan(0, 5),), speech_text="   ")

    def test_rejects_control_characters_in_speech_text(self) -> None:
        with pytest.raises(TextDomainError, match="control character"):
            TextSegment(ordinal=0, source_spans=(SourceSpan(0, 5),), speech_text="bad\ntext")


class TestPreparedText:
    def _segment(self, ordinal: int, start: int, end: int, text: str = "x") -> TextSegment:
        return TextSegment(
            ordinal=ordinal, source_spans=(SourceSpan(start, end),), speech_text=text
        )

    def test_valid_prepared_text(self) -> None:
        original = _original("0123456789")
        prepared = PreparedText(
            original=original,
            segments=(self._segment(0, 0, 5), self._segment(1, 5, 10)),
            normalizer_version="1",
            segmenter_version="1",
        )
        assert prepared.total_speech_characters == 2

    def test_rejects_non_sequential_ordinals(self) -> None:
        original = _original("0123456789")
        with pytest.raises(TextDomainError, match="sequential"):
            PreparedText(
                original=original,
                segments=(self._segment(0, 0, 5), self._segment(2, 5, 10)),
                normalizer_version="1",
                segmenter_version="1",
            )

    def test_rejects_span_beyond_original(self) -> None:
        original = _original("short")
        with pytest.raises(TextDomainError, match="beyond"):
            PreparedText(
                original=original,
                segments=(self._segment(0, 0, 100),),
                normalizer_version="1",
                segmenter_version="1",
            )

    def test_rejects_overlap_across_segments(self) -> None:
        original = _original("0123456789")
        with pytest.raises(TextDomainError, match="non-overlapping across segments"):
            PreparedText(
                original=original,
                segments=(self._segment(0, 0, 6), self._segment(1, 3, 9)),
                normalizer_version="1",
                segmenter_version="1",
            )

    def test_requires_at_least_one_segment(self) -> None:
        with pytest.raises(TextDomainError, match="at least one segment"):
            PreparedText(
                original=_original(),
                segments=(),
                normalizer_version="1",
                segmenter_version="1",
            )

    def test_rejects_blank_version_strings(self) -> None:
        original = _original("0123456789")
        with pytest.raises(TextDomainError, match="normalizer_version"):
            PreparedText(
                original=original,
                segments=(self._segment(0, 0, 5),),
                normalizer_version="  ",
                segmenter_version="1",
            )
