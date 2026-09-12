"""Atomic filesystem storage for loopback preview WAV files."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

from article_reader.application.ports.audio import PublishedAudioArtifact
from article_reader.domain.speech import AudioResult

_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


class LocalPreviewAudioStore:
    """Publish generated files beneath one controlled runtime directory."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("preview audio root must be an absolute pathlib.Path")
        self._root = root

    @staticmethod
    def _validate_id(rendition_id: str) -> None:
        if not isinstance(rendition_id, str) or _OPAQUE_ID.fullmatch(rendition_id) is None:
            raise ValueError("invalid preview rendition ID")

    @staticmethod
    def _validate_ordinal(ordinal: int) -> None:
        if type(ordinal) is not int or ordinal < 0 or ordinal > 999_999:
            raise ValueError("invalid preview chunk ordinal")

    def publish(
        self,
        rendition_id: str,
        ordinal: int,
        audio: AudioResult,
    ) -> PublishedAudioArtifact:
        self._validate_id(rendition_id)
        self._validate_ordinal(ordinal)
        if not isinstance(audio, AudioResult):
            raise TypeError("audio must be an AudioResult")
        rendition_directory = self._root / rendition_id
        rendition_directory.mkdir(parents=True, exist_ok=True)
        if _is_link_or_junction(self._root) or _is_link_or_junction(rendition_directory):
            raise OSError("preview audio storage cannot use a link or junction")

        wav_bytes = audio.as_wav_bytes()
        digest = hashlib.sha256(wav_bytes).hexdigest()
        destination = rendition_directory / f"{ordinal:06d}-{digest}.wav"
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=rendition_directory,
                prefix=f".{ordinal:06d}-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(wav_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return PublishedAudioArtifact(
            ordinal=ordinal,
            sha256=digest,
            byte_count=len(wav_bytes),
            duration_seconds=audio.duration_seconds,
        )

    def resolve(self, rendition_id: str, ordinal: int, sha256: str) -> Path | None:
        self._validate_id(rendition_id)
        self._validate_ordinal(ordinal)
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("invalid preview audio digest")
        rendition_directory = self._root / rendition_id
        candidate = rendition_directory / f"{ordinal:06d}-{sha256}.wav"
        if (
            _is_link_or_junction(self._root)
            or _is_link_or_junction(rendition_directory)
            or _is_link_or_junction(candidate)
            or not candidate.is_file()
        ):
            return None
        return candidate

    def discard(self, rendition_id: str) -> None:
        self._validate_id(rendition_id)
        rendition_directory = self._root / rendition_id
        if not rendition_directory.exists():
            return
        if _is_link_or_junction(self._root) or _is_link_or_junction(rendition_directory):
            raise OSError("refusing to delete linked preview audio storage")
        shutil.rmtree(rendition_directory)


__all__ = ["LocalPreviewAudioStore"]
