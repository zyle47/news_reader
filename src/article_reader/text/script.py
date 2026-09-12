"""Deterministic Latin/Cyrillic script evidence without transliteration."""

from __future__ import annotations

import unicodedata

from article_reader.domain.language import DetectedScript, ScriptDetection

SCRIPT_DETECTOR_VERSION = "unicode-name-v1"
_DOMINANCE_THRESHOLD = 0.90


def detect_script(text: str) -> ScriptDetection:
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    latin = 0
    cyrillic = 0
    for character in text:
        if not character.isalpha():
            continue
        name = unicodedata.name(character, "")
        if "LATIN" in name:
            latin += 1
        elif "CYRILLIC" in name:
            cyrillic += 1

    total = latin + cyrillic
    if total == 0:
        script = DetectedScript.UNKNOWN
    elif latin / total >= _DOMINANCE_THRESHOLD:
        script = DetectedScript.LATIN
    elif cyrillic / total >= _DOMINANCE_THRESHOLD:
        script = DetectedScript.CYRILLIC
    else:
        script = DetectedScript.MIXED
    return ScriptDetection(
        script=script,
        latin_letter_count=latin,
        cyrillic_letter_count=cyrillic,
        detector_version=SCRIPT_DETECTOR_VERSION,
    )


class UnicodeScriptDetector:
    def detect(self, text: str) -> ScriptDetection:
        return detect_script(text)


__all__ = ["SCRIPT_DETECTOR_VERSION", "UnicodeScriptDetector", "detect_script"]
