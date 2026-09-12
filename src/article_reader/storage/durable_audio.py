"""Durable, fsync-then-rename audio chunk publication for rendition audio.

Publication order follows ``docs/ARCHITECTURE.md`` M3 exactly: write to a temporary file on
the same filesystem as the destination, flush and fsync it, close it, re-open and validate
it as a well-formed WAV file, compute its SHA-256 from the bytes actually on disk, and only
then atomically rename it to its final immutable, content-addressed name. No database row is
written by this module; the caller commits the corresponding row afterwards (see
``article_reader.db.repositories.audio.SqliteAudioChunkRepository.publish_chunk``), so a
crash between this call returning and that commit can only ever leave an orphan file, never
a row pointing at nothing.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import wave
from pathlib import Path

from article_reader.application.ports.durable_audio import (
    AudioPublicationError,
    StagedAudioArtifact,
)
from article_reader.domain.speech import AudioResult

_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RELATIVE_PATH = re.compile(r"^[0-9a-f]{32}/[0-9]{6}-[0-9a-f]{64}\.wav$")
_READ_CHUNK_BYTES = 1024 * 1024


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_wav_file(path: Path, expected: AudioResult) -> None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (wave.Error, EOFError, OSError) as error:
        raise AudioPublicationError(
            f"staged audio file is not a valid WAV container: {path}"
        ) from error
    if frame_count <= 0:
        raise AudioPublicationError(f"staged audio file has no frames: {path}")
    if (channels, sample_width, frame_rate) != (
        expected.channels,
        expected.sample_width_bytes,
        expected.sample_rate_hz,
    ):
        raise AudioPublicationError(
            f"staged audio file metadata does not match the synthesized audio: {path}"
        )
    if frame_count != expected.frame_count:
        raise AudioPublicationError(f"staged audio file frame count does not match: {path}")


class LocalDurableAudioStore:
    """Publishes rendition audio beneath one controlled runtime directory."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("durable audio root must be an absolute pathlib.Path")
        self._root = root

    @staticmethod
    def _validate_rendition_id(rendition_id: str) -> None:
        if not isinstance(rendition_id, str) or _OPAQUE_ID.fullmatch(rendition_id) is None:
            raise ValueError("invalid rendition ID")

    @staticmethod
    def _validate_ordinal(ordinal: int) -> None:
        if type(ordinal) is not int or ordinal < 0 or ordinal > 999_999:
            raise ValueError("invalid chunk ordinal")

    def stage(self, rendition_id: str, ordinal: int, audio: AudioResult) -> StagedAudioArtifact:
        self._validate_rendition_id(rendition_id)
        self._validate_ordinal(ordinal)
        if not isinstance(audio, AudioResult):
            raise TypeError("audio must be an AudioResult")

        rendition_directory = self._root / rendition_id
        rendition_directory.mkdir(parents=True, exist_ok=True)
        if _is_link_or_junction(self._root) or _is_link_or_junction(rendition_directory):
            raise AudioPublicationError("durable audio storage cannot use a link or junction")

        wav_bytes = audio.as_wav_bytes()
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

            temporary_path = Path(temporary_name)
            _validate_wav_file(temporary_path, audio)
            digest = _sha256_of_file(temporary_path)

            final_name = f"{ordinal:06d}-{digest}.wav"
            destination = rendition_directory / final_name
            os.replace(temporary_path, destination)
            temporary_name = None
            _fsync_directory(rendition_directory)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

        return StagedAudioArtifact(
            rendition_id=rendition_id,
            ordinal=ordinal,
            relative_path=f"{rendition_id}/{final_name}",
            sha256=digest,
            byte_count=len(wav_bytes),
            duration_seconds=audio.duration_seconds,
        )

    def resolve(self, rendition_id: str, relative_path: str, sha256: str) -> Path | None:
        self._validate_rendition_id(rendition_id)
        if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
            raise ValueError("invalid audio digest")
        if not isinstance(relative_path, str) or _RELATIVE_PATH.fullmatch(relative_path) is None:
            raise ValueError("invalid stored relative path")
        if not relative_path.startswith(f"{rendition_id}/") or sha256 not in relative_path:
            raise ValueError("relative path does not match the requested rendition or digest")

        candidate = self._root / relative_path
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        rendition_directory = self._root / rendition_id
        if (
            _is_link_or_junction(self._root)
            or _is_link_or_junction(rendition_directory)
            or _is_link_or_junction(candidate)
            or not candidate.is_file()
        ):
            return None
        return candidate

    def discard_rendition(self, rendition_id: str) -> None:
        self._validate_rendition_id(rendition_id)
        rendition_directory = self._root / rendition_id
        if not rendition_directory.exists():
            return
        if _is_link_or_junction(self._root) or _is_link_or_junction(rendition_directory):
            raise AudioPublicationError("refusing to delete linked durable audio storage")
        shutil.rmtree(rendition_directory)

    def clear_stale_temporary_files(self) -> int:
        """Remove leftover ``.tmp`` files from an interrupted publish. Never touches ``.wav``."""

        if not self._root.is_dir():
            return 0
        removed = 0
        for rendition_directory in self._root.iterdir():
            if not rendition_directory.is_dir() or _is_link_or_junction(rendition_directory):
                continue
            for entry in rendition_directory.glob("*.tmp"):
                if entry.is_file() and not _is_link_or_junction(entry):
                    entry.unlink(missing_ok=True)
                    removed += 1
        return removed


def _fsync_directory(directory: Path) -> None:
    if sys.platform.startswith("win"):
        return  # Windows does not support fsync on directory handles.
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = ["LocalDurableAudioStore"]
