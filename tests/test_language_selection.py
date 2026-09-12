from __future__ import annotations

import hashlib

import pytest

from article_reader.application.services.language_selection import (
    LanguageSelectionError,
    LanguageSelectionErrorCode,
    select_language,
)
from article_reader.domain.language import (
    LanguageCandidate,
    LanguageDetection,
    LanguageSelectionReason,
)
from article_reader.domain.speech import Language, Script
from article_reader.text.script import detect_script


def _detection(
    *candidates: tuple[str, float],
    alphabetic_count: int = 200,
) -> LanguageDetection:
    return LanguageDetection(
        candidates=tuple(LanguageCandidate(code, confidence) for code, confidence in candidates),
        detector_version="fixture-v1",
        sample_character_count=max(300, alphabetic_count),
        sample_alphabetic_count=alphabetic_count,
        sample_sha256=hashlib.sha256(b"fixture").hexdigest(),
    )


def test_auto_selects_high_confidence_english_and_german() -> None:
    english = select_language(
        _detection(("en", 0.95), ("de", 0.03)),
        detect_script("This is a sufficiently long English article sentence. " * 3),
        language_hint="en-US",
        requested_language=None,
        requested_script=None,
    )
    german = select_language(
        _detection(("de", 0.98), ("en", 0.01)),
        detect_script("Dies ist ein ausreichend langer deutscher Artikeltext. " * 3),
        requested_language=None,
        requested_script=None,
    )

    assert (english.language, english.script) == (Language.ENGLISH, Script.LATIN)
    assert (german.language, german.script) == (Language.GERMAN, Script.LATIN)
    assert english.reason is LanguageSelectionReason.AUTO_DETECTED


def test_auto_selects_high_confidence_serbian_cyrillic() -> None:
    selection = select_language(
        _detection(("sr", 0.97), ("mk", 0.02)),
        detect_script("Ово је довољно дугачак текст чланка на српском језику. " * 3),  # noqa: RUF001
        requested_language=None,
        requested_script=None,
    )

    assert (selection.language, selection.script) == (Language.SERBIAN, Script.CYRILLIC)


@pytest.mark.parametrize(
    ("detection", "text", "reason"),
    [
        (
            _detection(("bs", 0.45), ("hr", 0.39), ("sr", 0.16)),
            "Ovo je dovoljno dugačak tekst napisan latinicom. " * 3,
            LanguageSelectionReason.BCMS_AMBIGUOUS,
        ),
        (
            _detection(("sr", 0.45), ("hr", 0.40), ("bs", 0.15)),
            "Ovo je dovoljno dugačak tekst napisan latinicom. " * 3,
            LanguageSelectionReason.BCMS_AMBIGUOUS,
        ),
        (
            _detection(("fr", 0.99), ("en", 0.005)),
            "Ceci est un article français suffisamment long pour ce test. " * 3,
            LanguageSelectionReason.UNSUPPORTED_LANGUAGE,
        ),
        (
            _detection(("en", 0.60), ("de", 0.35)),
            "This is a lower-confidence language fixture with enough letters. " * 3,
            LanguageSelectionReason.LOW_CONFIDENCE,
        ),
        (
            _detection(("en", 0.99), ("de", 0.005), alphabetic_count=12),
            "Short text",
            LanguageSelectionReason.INSUFFICIENT_TEXT,
        ),
    ],
)
def test_automatic_policy_abstains_instead_of_guessing(
    detection: LanguageDetection,
    text: str,
    reason: LanguageSelectionReason,
) -> None:
    selection = select_language(
        detection,
        detect_script(text),
        requested_language=None,
        requested_script=None,
    )

    assert selection.requires_override is True
    assert selection.reason is reason


def test_manual_override_wins_but_serbian_requires_explicit_script() -> None:
    detection = _detection(("hr", 0.90), ("sr", 0.08))
    evidence = detect_script("Ovo je dovoljno dugačak tekst pisan latinicom. " * 3)

    selection = select_language(
        detection,
        evidence,
        requested_language=Language.SERBIAN,
        requested_script=Script.LATIN,
    )

    assert (selection.language, selection.script) == (Language.SERBIAN, Script.LATIN)
    assert selection.reason is LanguageSelectionReason.MANUAL_OVERRIDE

    with pytest.raises(LanguageSelectionError) as caught:
        select_language(
            detection,
            evidence,
            requested_language=Language.SERBIAN,
            requested_script=None,
        )
    assert caught.value.code is LanguageSelectionErrorCode.SCRIPT_REQUIRED


def test_invalid_script_override_is_controlled() -> None:
    detection = _detection(("en", 0.99), ("de", 0.005))
    evidence = detect_script("This is a sufficiently long English fixture. " * 3)

    with pytest.raises(LanguageSelectionError) as caught:
        select_language(
            detection,
            evidence,
            requested_language=Language.ENGLISH,
            requested_script=Script.CYRILLIC,
        )
    assert caught.value.code is LanguageSelectionErrorCode.INVALID_OVERRIDE

    with pytest.raises(LanguageSelectionError):
        select_language(
            detection,
            evidence,
            requested_language=None,
            requested_script=Script.LATIN,
        )


def test_manual_override_rejects_dominant_script_mismatch() -> None:
    evidence = detect_script("Ово је довољно дугачак текст написан ћирилицом. " * 3)  # noqa: RUF001

    with pytest.raises(LanguageSelectionError) as caught:
        select_language(
            _detection(("sr", 0.99), ("mk", 0.005)),
            evidence,
            requested_language=Language.SERBIAN,
            requested_script=Script.LATIN,
        )

    assert caught.value.code is LanguageSelectionErrorCode.INVALID_OVERRIDE
    assert "dominant cyrillic" in str(caught.value)


def test_metadata_disagreement_requires_manual_override() -> None:
    selection = select_language(
        _detection(("en", 0.99), ("de", 0.005)),
        detect_script("This is a sufficiently long English article fixture. " * 3),
        language_hint="de-DE",
        requested_language=None,
        requested_script=None,
    )

    assert selection.requires_override is True
    assert selection.reason is LanguageSelectionReason.METADATA_CONFLICT
