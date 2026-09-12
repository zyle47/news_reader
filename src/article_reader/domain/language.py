"""Immutable language and script evidence used by preparation policy."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from article_reader.domain.speech import Language, Script

_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class LanguageDomainError(ValueError):
    """Raised when language evidence violates a domain invariant."""


class DetectedScript(StrEnum):
    LATIN = "latin"
    CYRILLIC = "cyrillic"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class LanguageSelectionReason(StrEnum):
    AUTO_DETECTED = "auto_detected"
    MANUAL_OVERRIDE = "manual_override"
    INSUFFICIENT_TEXT = "insufficient_text"
    LOW_CONFIDENCE = "low_confidence"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    BCMS_AMBIGUOUS = "serbian_croatian_bosnian_ambiguous"
    SCRIPT_UNCERTAIN = "script_uncertain"
    METADATA_CONFLICT = "metadata_conflict"


@dataclass(frozen=True, slots=True)
class LanguageCandidate:
    code: str
    confidence: float

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or _LANGUAGE_CODE.fullmatch(self.code) is None:
            raise LanguageDomainError("candidate code must be a lower-case ISO language code")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise LanguageDomainError("candidate confidence must be a number")
        confidence = float(self.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise LanguageDomainError("candidate confidence must be between zero and one")
        object.__setattr__(self, "confidence", confidence)


@dataclass(frozen=True, slots=True)
class LanguageDetection:
    candidates: tuple[LanguageCandidate, ...]
    detector_version: str
    sample_character_count: int
    sample_alphabetic_count: int
    sample_sha256: str

    def __post_init__(self) -> None:
        try:
            candidates = tuple(self.candidates)
        except TypeError as error:
            raise LanguageDomainError("candidates must be an iterable") from error
        if not candidates or len(candidates) > 10:
            raise LanguageDomainError("language detection requires between 1 and 10 candidates")
        if any(not isinstance(candidate, LanguageCandidate) for candidate in candidates):
            raise LanguageDomainError("candidates must contain LanguageCandidate values")
        if len({candidate.code for candidate in candidates}) != len(candidates):
            raise LanguageDomainError("candidate language codes must be unique")
        if any(
            current.confidence > previous.confidence for previous, current in pairwise(candidates)
        ):
            raise LanguageDomainError("candidates must be ordered by descending confidence")
        object.__setattr__(self, "candidates", candidates)

        if not isinstance(self.detector_version, str) or not self.detector_version.strip():
            raise LanguageDomainError("detector_version must be a non-empty string")
        if len(self.detector_version) > 100:
            raise LanguageDomainError("detector_version exceeds 100 characters")
        for name in ("sample_character_count", "sample_alphabetic_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise LanguageDomainError(f"{name} must be a non-negative integer")
        if self.sample_alphabetic_count > self.sample_character_count:
            raise LanguageDomainError("alphabetic count cannot exceed sample character count")
        if _SHA256.fullmatch(self.sample_sha256) is None:
            raise LanguageDomainError("sample_sha256 must be a lower-case SHA-256 digest")

    @property
    def top_candidate(self) -> LanguageCandidate:
        return self.candidates[0]

    @property
    def confidence_margin(self) -> float:
        runner_up = self.candidates[1].confidence if len(self.candidates) > 1 else 0.0
        return self.top_candidate.confidence - runner_up


@dataclass(frozen=True, slots=True)
class ScriptDetection:
    script: DetectedScript
    latin_letter_count: int
    cyrillic_letter_count: int
    detector_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.script, DetectedScript):
            raise LanguageDomainError("script must be a DetectedScript")
        for name in ("latin_letter_count", "cyrillic_letter_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise LanguageDomainError(f"{name} must be a non-negative integer")
        total = self.latin_letter_count + self.cyrillic_letter_count
        if self.script is DetectedScript.UNKNOWN and total != 0:
            raise LanguageDomainError("unknown script cannot have Latin or Cyrillic evidence")
        if self.script is not DetectedScript.UNKNOWN and total == 0:
            raise LanguageDomainError("detected script requires letter evidence")
        if not isinstance(self.detector_version, str) or not self.detector_version.strip():
            raise LanguageDomainError("script detector_version must be a non-empty string")


@dataclass(frozen=True, slots=True)
class LanguageSelection:
    language: Language | None
    script: Script | None
    reason: LanguageSelectionReason
    policy_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.reason, LanguageSelectionReason):
            raise LanguageDomainError("reason must be a LanguageSelectionReason")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise LanguageDomainError("policy_version must be a non-empty string")
        selected = self.language is not None or self.script is not None
        if selected and (
            not isinstance(self.language, Language) or not isinstance(self.script, Script)
        ):
            raise LanguageDomainError(
                "language and script must either both be selected or both be absent"
            )
        if self.language in {Language.ENGLISH, Language.GERMAN} and self.script is not Script.LATIN:
            raise LanguageDomainError("English and German selections require Latin script")
        resolved_reasons = {
            LanguageSelectionReason.AUTO_DETECTED,
            LanguageSelectionReason.MANUAL_OVERRIDE,
        }
        if self.language is None and self.reason in resolved_reasons:
            raise LanguageDomainError("a resolved reason requires a language selection")
        if self.language is not None and self.reason not in resolved_reasons:
            raise LanguageDomainError("an unresolved reason cannot carry a language selection")

    @property
    def requires_override(self) -> bool:
        return self.language is None


__all__ = [
    "DetectedScript",
    "LanguageCandidate",
    "LanguageDetection",
    "LanguageDomainError",
    "LanguageSelection",
    "LanguageSelectionReason",
    "ScriptDetection",
]
