"""Application-facing speech engine port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from article_reader.domain.speech import (
    AudioResult,
    SpeechEngineDescriptor,
    SpeechSettings,
    VoiceSpec,
)


@runtime_checkable
class SpeechEngine(Protocol):
    """Replaceable stateful speech synthesis boundary.

    Implementations load one exact voice release and return controlled PCM
    bytes.  They never choose output paths or publish files themselves.
    """

    @property
    def descriptor(self) -> SpeechEngineDescriptor:
        """Return stable engine identity and whether it is a test double."""

    def load(self, voice: VoiceSpec) -> None:
        """Load (or idempotently retain) the exact supplied voice."""

    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        """Synthesize non-empty prepared speech text into owned PCM bytes."""

    def close(self) -> None:
        """Release resident model resources; repeated calls must be safe."""
