"""Canonical, owner-independent hash identifying one rendition's exact contract.

Two submissions of the same prepared text through the same voice/engine/settings must
resolve to the same contract hash so a rendition can be safely reused; any change to the
prepared speech text, voice, model/config digest, engine version, or synthesis settings must
change it. Instance and owner identifiers are intentionally excluded so the hash matches the
project plan's "portable contract hash" and stays comparable across machines.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from article_reader.application.ports.persistence import PreparedSegmentRecord
from article_reader.domain.speech import SpeechSettings, VoiceSpec


def compute_contract_hash(
    *,
    segments: Sequence[PreparedSegmentRecord],
    language: str,
    script: str,
    voice: VoiceSpec,
    settings: SpeechSettings,
) -> str:
    canonical = {
        "segment_hashes": [segment.speech_text_sha256 for segment in segments],
        "language": language,
        "script": script,
        "voice_id": voice.voice_id,
        "engine": voice.engine,
        "engine_version": voice.engine_version,
        "model_sha256": voice.model_sha256,
        "config_sha256": voice.config_sha256,
        "sample_rate_hz": voice.sample_rate_hz,
        "settings": [list(option) for option in settings.options],
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["compute_contract_hash"]
