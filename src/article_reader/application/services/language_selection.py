"""Fail-closed policy for selecting one supported language and script."""

from __future__ import annotations

from enum import StrEnum

from article_reader.domain.language import (
    DetectedScript,
    LanguageDetection,
    LanguageSelection,
    LanguageSelectionReason,
    ScriptDetection,
)
from article_reader.domain.speech import Language, Script

LANGUAGE_POLICY_VERSION = "1"
MINIMUM_ALPHABETIC_CHARACTERS = 40
MINIMUM_CONFIDENCE = 0.80
MINIMUM_MARGIN = 0.20
_BCMS_CODES = frozenset({"bs", "hr", "sr"})
_SUPPORTED_CODES = {
    "en": Language.ENGLISH,
    "de": Language.GERMAN,
    "sr": Language.SERBIAN,
}


class LanguageSelectionErrorCode(StrEnum):
    INVALID_OVERRIDE = "LANGUAGE_OVERRIDE_INVALID"
    SCRIPT_REQUIRED = "LANGUAGE_SCRIPT_REQUIRED"


class LanguageSelectionError(ValueError):
    def __init__(self, code: LanguageSelectionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unresolved(reason: LanguageSelectionReason) -> LanguageSelection:
    return LanguageSelection(
        language=None,
        script=None,
        reason=reason,
        policy_version=LANGUAGE_POLICY_VERSION,
    )


def select_language(
    detection: LanguageDetection | None,
    script_detection: ScriptDetection,
    *,
    language_hint: str | None = None,
    requested_language: Language | None,
    requested_script: Script | None,
) -> LanguageSelection:
    """Apply an explicit override or conservatively resolve detector evidence."""

    if not isinstance(script_detection, ScriptDetection):
        raise TypeError("script_detection must be a ScriptDetection")
    if requested_script is not None and not isinstance(requested_script, Script):
        raise TypeError("requested_script must be a Script or None")

    if requested_language is not None:
        if not isinstance(requested_language, Language):
            raise TypeError("requested_language must be a Language or None")
        if requested_language is Language.SERBIAN:
            if requested_script is None:
                raise LanguageSelectionError(
                    LanguageSelectionErrorCode.SCRIPT_REQUIRED,
                    "Serbian requires an explicit Latin or Cyrillic script",
                )
        elif requested_script not in {None, Script.LATIN}:
            raise LanguageSelectionError(
                LanguageSelectionErrorCode.INVALID_OVERRIDE,
                "English and German overrides require Latin script",
            )
        selected_script = requested_script or Script.LATIN
        detected_script = {
            DetectedScript.LATIN: Script.LATIN,
            DetectedScript.CYRILLIC: Script.CYRILLIC,
        }.get(script_detection.script)
        if detected_script is not None and selected_script is not detected_script:
            raise LanguageSelectionError(
                LanguageSelectionErrorCode.INVALID_OVERRIDE,
                f"the selected script conflicts with dominant {detected_script.value} "
                "script evidence in the article",
            )
        return LanguageSelection(
            language=requested_language,
            script=selected_script,
            reason=LanguageSelectionReason.MANUAL_OVERRIDE,
            policy_version=LANGUAGE_POLICY_VERSION,
        )

    if requested_script is not None:
        raise LanguageSelectionError(
            LanguageSelectionErrorCode.INVALID_OVERRIDE,
            "a script override is accepted only with an explicit language override",
        )
    if not isinstance(detection, LanguageDetection):
        raise TypeError("automatic selection requires a LanguageDetection")

    if detection.sample_alphabetic_count < MINIMUM_ALPHABETIC_CHARACTERS:
        return _unresolved(LanguageSelectionReason.INSUFFICIENT_TEXT)

    top = detection.top_candidate
    if top.code in _BCMS_CODES:
        if script_detection.script is DetectedScript.LATIN:
            return _unresolved(LanguageSelectionReason.BCMS_AMBIGUOUS)
        if top.code != "sr":
            return _unresolved(LanguageSelectionReason.BCMS_AMBIGUOUS)

    language = _SUPPORTED_CODES.get(top.code)
    if language is None:
        return _unresolved(LanguageSelectionReason.UNSUPPORTED_LANGUAGE)
    if top.confidence < MINIMUM_CONFIDENCE or detection.confidence_margin < MINIMUM_MARGIN:
        return _unresolved(LanguageSelectionReason.LOW_CONFIDENCE)
    if language_hint is not None:
        primary_hint = language_hint.casefold().split("-", 1)[0]
        if (
            primary_hint.isalpha()
            and 2 <= len(primary_hint) <= 3
            and primary_hint not in {"mul", "und", "zxx"}
            and primary_hint != top.code
        ):
            return _unresolved(LanguageSelectionReason.METADATA_CONFLICT)

    if language in {Language.ENGLISH, Language.GERMAN}:
        if script_detection.script is not DetectedScript.LATIN:
            return _unresolved(LanguageSelectionReason.SCRIPT_UNCERTAIN)
        script = Script.LATIN
    else:
        if script_detection.script is not DetectedScript.CYRILLIC:
            return _unresolved(LanguageSelectionReason.SCRIPT_UNCERTAIN)
        script = Script.CYRILLIC

    return LanguageSelection(
        language=language,
        script=script,
        reason=LanguageSelectionReason.AUTO_DETECTED,
        policy_version=LANGUAGE_POLICY_VERSION,
    )


__all__ = [
    "LANGUAGE_POLICY_VERSION",
    "MINIMUM_ALPHABETIC_CHARACTERS",
    "MINIMUM_CONFIDENCE",
    "MINIMUM_MARGIN",
    "LanguageSelectionError",
    "LanguageSelectionErrorCode",
    "select_language",
]
