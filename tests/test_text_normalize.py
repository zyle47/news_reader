from __future__ import annotations

import unicodedata

import pytest

from article_reader.text.normalize import NORMALIZER_VERSION, normalize_speech_text


def test_collapses_internal_whitespace_runs() -> None:
    assert normalize_speech_text("Hello    world\n\nfoo\tbar") == "Hello world foo bar"


def test_strips_leading_and_trailing_whitespace() -> None:
    assert normalize_speech_text("   padded text   ") == "padded text"


def test_applies_nfc_normalization() -> None:
    decomposed = "éllo"  # 'e' + combining acute accent (U+0301) + "llo"
    composed = "éllo"  # single precomposed e-acute (U+00E9) + "llo"
    assert decomposed != composed  # sanity: the two encodings really differ
    assert unicodedata.normalize("NFC", decomposed) == composed  # sanity: NFC target is right
    assert normalize_speech_text(decomposed) == composed


def test_preserves_diacritics_and_punctuation() -> None:
    text = "Čšž đ nj lj — ćirilica: 1.234,56!"
    assert normalize_speech_text(text) == text


def test_preserves_cyrillic_verbatim_never_transliterates() -> None:
    text = "Ћирилица su different writing systems."
    assert normalize_speech_text(text) == text


def test_empty_or_whitespace_only_becomes_empty_string() -> None:
    assert normalize_speech_text("   \n\t  ") == ""


def test_rejects_non_string_input() -> None:
    with pytest.raises(TypeError):
        normalize_speech_text(123)  # type: ignore[arg-type]


def test_version_constant_is_a_non_empty_string() -> None:
    assert isinstance(NORMALIZER_VERSION, str) and NORMALIZER_VERSION
