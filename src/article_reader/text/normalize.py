"""Deterministic, non-lossy speech-text normalization.

The submitted original text is never rewritten. This module only derives a
separate, whitespace-collapsed, Unicode-NFC-normalized ``speech_text`` string
for one or more spans of that original. No script transliteration is ever
performed here: Serbian Cyrillic is preserved verbatim. piper-tts's bundled
espeak-ng phonemizer was verified directly to produce equivalent phoneme
output for equivalent Latin- and Cyrillic-script Serbian text (see
docs/DECISIONS.md ADR-009), so no currently tested speech adapter requires
transliteration. Revisit only if a future, explicitly tested adapter proves
otherwise.
"""

from __future__ import annotations

import re
import unicodedata

NORMALIZER_VERSION = "1"

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_speech_text(text: str) -> str:
    """Return a single-line, NFC-normalized, whitespace-collapsed copy of ``text``."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFC", text)
    collapsed = _WHITESPACE_RUN.sub(" ", normalized)
    return collapsed.strip()


__all__ = ["NORMALIZER_VERSION", "normalize_speech_text"]
