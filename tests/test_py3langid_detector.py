from __future__ import annotations

import importlib
import sys

import pytest

from article_reader.text.py3langid_detector import (
    LanguageDetectorError,
    LanguageDetectorErrorCode,
    Py3LangidDetector,
)


class _Ranker:
    def __init__(self, values: list[tuple[str, float]]) -> None:
        self.values = values
        self.calls: list[str] = []

    def rank(self, text: str) -> list[tuple[str, float]]:
        self.calls.append(text)
        return self.values


def test_adapter_records_bounded_ranked_evidence_without_text() -> None:
    ranker = _Ranker([("en", 0.9), ("de", 0.08), ("fr", 0.02)])
    detector = Py3LangidDetector(ranker=ranker, detector_version="fixture-v1")
    text = "This is enough alphabetic fixture text."

    detection = detector.detect(text)

    assert ranker.calls == [text]
    assert [candidate.code for candidate in detection.candidates] == ["en", "de", "fr"]
    assert detection.sample_character_count == len(text)
    assert detection.sample_alphabetic_count > 0
    assert len(detection.sample_sha256) == 64
    assert text not in repr(detection)


def test_adapter_converts_external_failures_to_controlled_errors() -> None:
    class _BrokenRanker:
        def rank(self, text: str) -> list[tuple[str, float]]:
            del text
            raise RuntimeError("private detector failure")

    with pytest.raises(LanguageDetectorError, match="detection failed") as caught:
        Py3LangidDetector(
            ranker=_BrokenRanker(),
            detector_version="fixture-v1",
        ).detect("Private article body")

    assert "Private article body" not in str(caught.value)
    assert caught.value.code is LanguageDetectorErrorCode.FAILED


def test_real_detector_covers_supported_languages_and_rejectable_neighbor() -> None:
    detector = Py3LangidDetector()
    samples = {
        "en": (
            "This is a detailed English newspaper article about public transport and city "
            "planning. The council approved a new railway station after months of debate."
        ),
        "de": (
            "Dies ist ein ausführlicher deutscher Zeitungsartikel über öffentlichen Verkehr "
            "und Stadtplanung. Der Stadtrat genehmigte nach langer Debatte einen neuen Bahnhof."
        ),
        "sr": (
            "Ово је детаљан чланак на српском језику о јавном превозу и планирању града. "  # noqa: RUF001
            "Скупштина је после дуге расправе одобрила изградњу нове железничке станице."  # noqa: RUF001
        ),
        "fr": (
            "Ceci est un article français détaillé sur les transports publics et la "
            "planification urbaine. Le conseil municipal a approuvé une nouvelle gare."
        ),
    }

    detected = {name: detector.detect(text).top_candidate.code for name, text in samples.items()}

    assert detected == {"en": "en", "de": "de", "sr": "sr", "fr": "fr"}


def test_import_does_not_load_the_detector_model() -> None:
    sys.modules.pop("py3langid.langid", None)
    sys.modules.pop("article_reader.text.py3langid_detector", None)

    module = importlib.import_module("article_reader.text.py3langid_detector")
    module.Py3LangidDetector()

    assert "py3langid.langid" not in sys.modules
