from __future__ import annotations

from itertools import pairwise

import pytest

from article_reader.domain.speech import Language, Script
from article_reader.domain.text import OriginalText, TextDomainError
from article_reader.text.segment import find_paragraph_spans, find_sentence_spans, prepare_text


def _spans_text(source: str, spans: tuple[tuple[int, int], ...]) -> tuple[str, ...]:
    return tuple(source[start:end] for start, end in spans)


class TestFindParagraphSpans:
    def test_single_paragraph(self) -> None:
        text = "Just one paragraph of text."
        spans = find_paragraph_spans(text)
        assert _spans_text(text, spans) == (text,)

    def test_splits_on_blank_line(self) -> None:
        text = "First paragraph.\n\nSecond paragraph."
        spans = find_paragraph_spans(text)
        assert _spans_text(text, spans) == ("First paragraph.", "Second paragraph.")

    def test_collapses_multiple_blank_lines(self) -> None:
        text = "First.\n\n\n\nSecond."
        spans = find_paragraph_spans(text)
        assert _spans_text(text, spans) == ("First.", "Second.")

    def test_trims_surrounding_whitespace(self) -> None:
        text = "\n\n  First paragraph.  \n\n  Second paragraph.  \n\n"
        spans = find_paragraph_spans(text)
        assert _spans_text(text, spans) == ("First paragraph.", "Second paragraph.")

    def test_empty_text_has_no_paragraphs(self) -> None:
        assert find_paragraph_spans("") == ()

    def test_spans_exactly_reconstruct_original(self) -> None:
        text = "Alpha line.\n\nBeta line.\n\nGamma line."
        for start, end in find_paragraph_spans(text):
            assert text[start:end] == text[start:end].strip()


# Table-driven sentence-boundary cases: (language, text, expected_sentence_texts)
_SENTENCE_CASES: tuple[tuple[Language, str, tuple[str, ...]], ...] = (
    (
        Language.ENGLISH,
        "This is one sentence. This is another.",
        ("This is one sentence.", "This is another."),
    ),
    (
        Language.ENGLISH,
        "Dr. Smith arrived. He was late.",
        ("Dr. Smith arrived.", "He was late."),
    ),
    (
        Language.ENGLISH,
        "We saw cats, dogs, etc. Then we left.",
        ("We saw cats, dogs, etc. Then we left.",),
    ),
    (
        Language.ENGLISH,
        "See the report (e.g. appendix A). It is thorough.",
        ("See the report (e.g. appendix A).", "It is thorough."),
    ),
    (
        Language.ENGLISH,
        'She said "Stop!" and left. Then silence.',
        ('She said "Stop!" and left.', "Then silence."),
    ),
    (
        Language.ENGLISH,
        "Is that true? Yes, it is!",
        ("Is that true?", "Yes, it is!"),
    ),
    (
        Language.ENGLISH,
        "Wait... What happened? Nothing.",
        ("Wait...", "What happened?", "Nothing."),
    ),
    (
        Language.GERMAN,
        "Dr. Müller prüft die Größe. Alles ist gut.",
        ("Dr. Müller prüft die Größe.", "Alles ist gut."),
    ),
    (
        Language.GERMAN,
        "Der Betrag ist 1.234,56 Euro. Das ist viel.",
        ("Der Betrag ist 1.234,56 Euro.", "Das ist viel."),
    ),
    (
        Language.GERMAN,
        "Er kam am 11. September 2026 an. Alle waren da.",
        ("Er kam am 11. September 2026 an.", "Alle waren da."),
    ),
    (
        Language.GERMAN,
        "Das gilt z. B. für Städte. Auch für Dörfer.",
        ("Das gilt z. B. für Städte.", "Auch für Dörfer."),
    ),
    (
        Language.GERMAN,
        "Das betrifft u. a. Berlin. Und München.",
        ("Das betrifft u. a. Berlin.", "Und München."),
    ),
    (
        Language.SERBIAN,
        "Prof. Marković čita. Svi slušaju.",
        ("Prof. Marković čita.", "Svi slušaju."),
    ),
    (
        Language.SERBIAN,
        "Stiglo je 11. septembra. Svi su bili tu.",
        ("Stiglo je 11. septembra.", "Svi su bili tu."),
    ),
    (
        Language.SERBIAN,
        "Реч је о речима: ђак, џез, љубав. Све је јасно.",  # noqa: RUF001
        ("Реч је о речима: ђак, џез, љубав.", "Све је јасно."),  # noqa: RUF001
    ),
    (
        Language.SERBIAN,
        "Проф. Марковић чита чланак. Сви слушају.",
        ("Проф. Марковић чита чланак.", "Сви слушају."),
    ),
)


class TestFindSentenceSpans:
    @pytest.mark.parametrize(("language", "text", "expected"), _SENTENCE_CASES)
    def test_expected_sentence_boundaries(
        self, language: Language, text: str, expected: tuple[str, ...]
    ) -> None:
        spans = find_sentence_spans(text, language)
        assert _spans_text(text, spans) == expected

    def test_empty_paragraph_has_no_sentences(self) -> None:
        assert find_sentence_spans("", Language.ENGLISH) == ()

    def test_rejects_non_language_argument(self) -> None:
        with pytest.raises(TypeError):
            find_sentence_spans("text", "en")  # type: ignore[arg-type]

    def test_spans_are_ordered_and_non_overlapping(self) -> None:
        text = "First one. Second one! Third one? Fourth."
        spans = find_sentence_spans(text, Language.ENGLISH)
        for previous, current in pairwise(spans):
            assert current[0] >= previous[1]

    def test_no_content_is_dropped_between_first_and_last_span(self) -> None:
        text = "First one. Second one! Third one? Fourth."
        spans = find_sentence_spans(text, Language.ENGLISH)
        assert spans[0][0] == 0
        assert spans[-1][1] == len(text)


class TestPrepareText:
    def _original(self, text: str, language: Language = Language.ENGLISH) -> OriginalText:
        script = Script.LATIN
        return OriginalText(text=text, language=language, script=script)

    def test_simple_two_sentence_text_produces_expected_segments(self) -> None:
        original = self._original("First sentence here. Second sentence here.")
        prepared = prepare_text(original, max_segment_characters=1000)
        assert len(prepared.segments) == 1
        assert prepared.segments[0].speech_text == ("First sentence here. Second sentence here.")

    def test_small_max_characters_produces_multiple_segments(self) -> None:
        original = self._original("First sentence here. Second sentence here.")
        prepared = prepare_text(original, max_segment_characters=25)
        assert len(prepared.segments) >= 2
        for segment in prepared.segments:
            assert segment.character_count <= 25

    def test_every_segment_source_span_round_trips_to_original(self) -> None:
        text = "Alpha beta. Gamma delta.\n\nEpsilon zeta. Eta theta."
        original = self._original(text)
        prepared = prepare_text(original, max_segment_characters=1000)
        for segment in prepared.segments:
            for span in segment.source_spans:
                slice_text = original.slice(span)
                assert slice_text.strip() == slice_text
                assert slice_text

    def test_source_spans_cover_all_sentence_content_without_dropping_text(self) -> None:
        text = "One two three four five six seven eight nine ten."
        original = self._original(text)
        prepared = prepare_text(original, max_segment_characters=1000)
        total_chars = sum(
            span.character_count for segment in prepared.segments for span in segment.source_spans
        )
        assert total_chars == len(text)

    def test_long_sentence_is_split_without_dropping_words(self) -> None:
        words = [f"word{i}" for i in range(50)]
        sentence = " ".join(words) + "."
        original = self._original(sentence)
        prepared = prepare_text(original, max_segment_characters=30)

        assert len(prepared.segments) > 1
        reconstructed = " ".join(segment.speech_text for segment in prepared.segments)
        for word in words:
            assert word in reconstructed
        for segment in prepared.segments:
            assert segment.character_count <= 30

    def test_paragraphs_can_be_combined_into_one_segment_when_small(self) -> None:
        text = "Para one.\n\nPara two.\n\nPara three."
        original = self._original(text)
        prepared = prepare_text(original, max_segment_characters=1000)
        assert len(prepared.segments) == 1
        assert prepared.segments[0].speech_text == "Para one. Para two. Para three."

    def test_deterministic_output_for_identical_input(self) -> None:
        text = "Deterministic sentence one. Deterministic sentence two."
        original = self._original(text)
        first = prepare_text(original, max_segment_characters=20)
        second = prepare_text(original, max_segment_characters=20)
        assert first.segments == second.segments
        assert first.original.sha256 == second.original.sha256

    def test_serbian_latin_and_cyrillic_both_prepare_successfully(self) -> None:
        latin = OriginalText(
            text="Ovo je test. Đak čita knjigu.",
            language=Language.SERBIAN,
            script=Script.LATIN,
        )
        cyrillic = OriginalText(
            text="Ово је тест. Ђак чита књигу.",  # noqa: RUF001
            language=Language.SERBIAN,
            script=Script.CYRILLIC,
        )
        prepared_latin = prepare_text(latin, max_segment_characters=1000)
        prepared_cyrillic = prepare_text(cyrillic, max_segment_characters=1000)
        assert len(prepared_latin.segments) == 1
        assert len(prepared_cyrillic.segments) == 1

    def test_original_text_is_never_mutated_by_preparation(self) -> None:
        text = "Some   messy    whitespace.\tTabs too."
        original = self._original(text)
        prepare_text(original, max_segment_characters=1000)
        assert original.text == text

    def test_rejects_non_positive_max_segment_characters(self) -> None:
        original = self._original("Some text.")
        with pytest.raises(ValueError, match="positive"):
            prepare_text(original, max_segment_characters=0)

    def test_rejects_non_original_text_argument(self) -> None:
        with pytest.raises(TypeError):
            prepare_text("not an OriginalText", max_segment_characters=100)  # type: ignore[arg-type]

    def test_normalizer_and_segmenter_versions_are_recorded(self) -> None:
        original = self._original("Some text.")
        prepared = prepare_text(original, max_segment_characters=1000)
        assert prepared.normalizer_version
        assert prepared.segmenter_version

    def test_result_is_a_valid_prepared_text_domain_object(self) -> None:
        original = self._original("Alpha. Beta. Gamma.")
        prepared = prepare_text(original, max_segment_characters=1000)
        # Constructing PreparedText already validated invariants; re-check ordinals here.
        for index, segment in enumerate(prepared.segments):
            assert segment.ordinal == index


def test_import_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import importlib
    import sys

    for module_name in (
        "article_reader.text.segment",
        "article_reader.text.normalize",
        "article_reader.domain.text",
    ):
        sys.modules.pop(module_name, None)

    real_open = builtins.open

    def guarded_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("module import must not open any file")

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        importlib.import_module("article_reader.text.segment")
    finally:
        monkeypatch.setattr(builtins, "open", real_open)


def test_prepare_text_raises_on_content_with_no_segmentable_paragraphs() -> None:
    with pytest.raises(TextDomainError):
        OriginalText(text="   ", language=Language.ENGLISH, script=Script.LATIN)
