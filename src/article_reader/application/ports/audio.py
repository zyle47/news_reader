"""Application-owned boundary for publishing preview audio artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from article_reader.domain.speech import AudioResult


@dataclass(frozen=True, slots=True)
class PublishedAudioArtifact:
    ordinal: int
    sha256: str
    byte_count: int
    duration_seconds: float


class PreviewAudioPublisher(Protocol):
    def publish(
        self,
        rendition_id: str,
        ordinal: int,
        audio: AudioResult,
    ) -> PublishedAudioArtifact: ...

    def resolve(self, rendition_id: str, ordinal: int, sha256: str) -> Path | None: ...

    def discard(self, rendition_id: str) -> None: ...


__all__ = ["PreviewAudioPublisher", "PublishedAudioArtifact"]
