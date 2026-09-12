"""Read-only inspection of explicitly installed voice artifacts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import unquote, urlsplit

from article_reader.domain.speech import VoiceSpec

_WINDOWS_RESERVED_NAMES = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


class ArtifactState(StrEnum):
    """Result of verifying one expected model artifact."""

    MISSING = "missing"
    NOT_REGULAR = "not_regular"
    UNREADABLE = "unreadable"
    DIGEST_MISMATCH = "digest_mismatch"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class ArtifactVerification:
    """Safe verification result without exposing file contents."""

    kind: str
    filename: str
    expected_sha256: str
    actual_sha256: str | None
    byte_count: int | None
    state: ArtifactState


@dataclass(frozen=True, slots=True)
class VoiceInstallation:
    """Verification state for all files needed by one exact voice release."""

    voice_id: str
    artifacts: tuple[ArtifactVerification, ...]

    @property
    def installed(self) -> bool:
        return bool(self.artifacts) and all(
            artifact.state is ArtifactState.VERIFIED for artifact in self.artifacts
        )


def portable_filename(url: str) -> str:
    """Extract a cross-platform-safe basename from a registry-controlled URL."""

    filename = unquote(urlsplit(url).path.rsplit("/", maxsplit=1)[-1])
    if not filename or filename in {".", ".."}:
        raise ValueError("voice artifact URL must end with a filename")
    if Path(filename).name != filename or any(character in filename for character in '<>:"/\\|?*'):
        raise ValueError(f"voice artifact filename is not portable: {filename!r}")
    if filename.endswith((" ", ".")):
        raise ValueError(f"voice artifact filename is not portable: {filename!r}")
    if filename.split(".", maxsplit=1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"voice artifact filename is reserved on Windows: {filename!r}")
    return filename


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
            byte_count += len(block)
    return digest.hexdigest(), byte_count


class ModelStore:
    """Inspect model files beneath one configured, machine-local directory.

    Installation and downloading are deliberately separate. This adapter never
    reaches the network, creates directories, or trusts filenames supplied by an
    article or bundle.
    """

    def __init__(self, models_directory: Path) -> None:
        if not isinstance(models_directory, Path):
            raise TypeError("models_directory must be a pathlib.Path")
        if not models_directory.is_absolute():
            raise ValueError("models_directory must be absolute")
        self._models_directory = models_directory

    def voice_directory(self, voice: VoiceSpec) -> Path:
        return self._models_directory / voice.voice_id

    def inspect(self, voice: VoiceSpec) -> VoiceInstallation:
        expected = (
            ("model", portable_filename(voice.model_url), voice.model_sha256),
            ("config", portable_filename(voice.config_url), voice.config_sha256),
        )
        if expected[0][1].casefold() == expected[1][1].casefold():
            raise ValueError("model and config artifacts must use distinct filenames")
        voice_directory = self.voice_directory(voice)
        artifacts = tuple(
            self._inspect_artifact(voice_directory / filename, kind, filename, digest)
            for kind, filename, digest in expected
        )
        return VoiceInstallation(voice_id=voice.voice_id, artifacts=artifacts)

    @staticmethod
    def _inspect_artifact(
        path: Path,
        kind: str,
        filename: str,
        expected_sha256: str,
    ) -> ArtifactVerification:
        if not path.exists():
            return ArtifactVerification(
                kind=kind,
                filename=filename,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                byte_count=None,
                state=ArtifactState.MISSING,
            )
        if _is_link_or_junction(path.parent) or _is_link_or_junction(path) or not path.is_file():
            return ArtifactVerification(
                kind=kind,
                filename=filename,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                byte_count=None,
                state=ArtifactState.NOT_REGULAR,
            )
        try:
            actual_sha256, byte_count = _sha256(path)
        except OSError:
            return ArtifactVerification(
                kind=kind,
                filename=filename,
                expected_sha256=expected_sha256,
                actual_sha256=None,
                byte_count=None,
                state=ArtifactState.UNREADABLE,
            )
        state = (
            ArtifactState.VERIFIED
            if actual_sha256 == expected_sha256
            else ArtifactState.DIGEST_MISMATCH
        )
        return ArtifactVerification(
            kind=kind,
            filename=filename,
            expected_sha256=expected_sha256,
            actual_sha256=actual_sha256,
            byte_count=byte_count,
            state=state,
        )


__all__ = [
    "ArtifactState",
    "ArtifactVerification",
    "ModelStore",
    "VoiceInstallation",
    "portable_filename",
]
