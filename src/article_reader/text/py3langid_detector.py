"""Concrete, offline py3langid adapter behind the language-detector port."""

from __future__ import annotations

import hashlib
import importlib
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol, cast

from article_reader.domain.language import LanguageCandidate, LanguageDetection

_MAX_DETECTION_CHARACTERS = 101_000
_MAX_RECORDED_CANDIDATES = 10


class LanguageDetectorErrorCode(StrEnum):
    INVALID_INPUT = "LANGUAGE_DETECTOR_INVALID_INPUT"
    UNAVAILABLE = "LANGUAGE_DETECTOR_UNAVAILABLE"
    FAILED = "LANGUAGE_DETECTION_FAILED"


class LanguageDetectorError(RuntimeError):
    """Controlled failure at the third-party detector boundary."""

    def __init__(self, code: LanguageDetectorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class _Ranker(Protocol):
    def rank(self, text: str) -> list[tuple[str, float]]: ...


def _default_ranker() -> _Ranker:
    try:
        module = importlib.import_module("py3langid.langid")
        identifier_type = module.LanguageIdentifier
        model_file = module.MODEL_FILE
        return cast(_Ranker, identifier_type.from_model_file(model_file, norm_probs=True))
    except Exception as error:
        raise LanguageDetectorError(
            LanguageDetectorErrorCode.UNAVAILABLE,
            "the local language detector could not be loaded",
        ) from error


def _installed_detector_version() -> str:
    try:
        return f"py3langid-{version('py3langid')}-default"
    except PackageNotFoundError as error:
        raise LanguageDetectorError(
            LanguageDetectorErrorCode.UNAVAILABLE,
            "the py3langid package is not installed",
        ) from error


class Py3LangidDetector:
    def __init__(
        self,
        *,
        ranker: _Ranker | None = None,
        detector_version: str | None = None,
    ) -> None:
        self._ranker = ranker
        self._detector_version = detector_version

    def detect(self, text: str) -> LanguageDetection:
        if not isinstance(text, str):
            raise LanguageDetectorError(
                LanguageDetectorErrorCode.INVALID_INPUT,
                "language detector input must be text",
            )
        if not text.strip():
            raise LanguageDetectorError(
                LanguageDetectorErrorCode.INVALID_INPUT,
                "language detector input cannot be empty",
            )
        if len(text) > _MAX_DETECTION_CHARACTERS:
            raise LanguageDetectorError(
                LanguageDetectorErrorCode.INVALID_INPUT,
                "language detector input exceeds its bounded limit",
            )
        try:
            if self._ranker is None:
                self._ranker = _default_ranker()
            if self._detector_version is None:
                self._detector_version = _installed_detector_version()
            raw_candidates = self._ranker.rank(text)
            candidates = tuple(
                LanguageCandidate(code=str(code), confidence=float(confidence))
                for code, confidence in raw_candidates[:_MAX_RECORDED_CANDIDATES]
            )
            return LanguageDetection(
                candidates=candidates,
                detector_version=self._detector_version,
                sample_character_count=len(text),
                sample_alphabetic_count=sum(character.isalpha() for character in text),
                sample_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )
        except LanguageDetectorError:
            raise
        except Exception as error:
            raise LanguageDetectorError(
                LanguageDetectorErrorCode.FAILED,
                "local language detection failed",
            ) from error


__all__ = ["LanguageDetectorError", "LanguageDetectorErrorCode", "Py3LangidDetector"]
