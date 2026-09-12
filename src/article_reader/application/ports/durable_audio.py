"""Application-owned boundary for durably publishing rendition audio chunks.

A durable artifact is staged, fsynced, and WAV-validated *before* any database commit, and
the returned relative path is the only thing the API layer is allowed to resolve bytes from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from article_reader.domain.speech import AudioResult


class AudioPublicationError(RuntimeError):
    """Raised when a synthesized chunk cannot be durably staged or validated."""


@dataclass(frozen=True, slots=True)
class StagedAudioArtifact:
    """A fully written, fsynced, and WAV-validated file ready for database commit."""

    rendition_id: str
    ordinal: int
    relative_path: str
    sha256: str
    byte_count: int
    duration_seconds: float


class DurableAudioStore(Protocol):
    def stage(self, rendition_id: str, ordinal: int, audio: AudioResult) -> StagedAudioArtifact:
        """Write, fsync, and validate one chunk under its final immutable name.

        The file exists at its final path (named by ordinal and content digest) when this
        returns successfully, but no database row references it yet: a crash between this
        call and the caller's transaction commit leaves only an orphan file, never a row
        pointing at nothing.
        """

    def resolve(self, rendition_id: str, relative_path: str, sha256: str) -> Path | None:
        """Return the absolute path for a *database-confirmed* published chunk, or ``None``."""

    def discard_rendition(self, rendition_id: str) -> None:
        """Remove every staged/published file for a rendition (used when deleting a reading)."""


__all__ = ["AudioPublicationError", "DurableAudioStore", "StagedAudioArtifact"]
