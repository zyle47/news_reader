"""Explicit, safe voice artifact installer.

Downloading is a distinct, explicitly invoked action driven by the ``voices
install`` CLI command. Nothing in this module runs during package import,
diagnostics, or synthesis. Every request targets exactly the HTTPS URL
recorded on a validated, registry-pinned :class:`VoiceSpec`; there is no
user-supplied or article-derived URL anywhere in this path. Redirects are
bounded and must stay on HTTPS; downloads are bounded in size and time;
artifacts are staged in a temporary file and only published (via an atomic
rename) once their SHA-256 digest matches the registry exactly.
"""

from __future__ import annotations

import http.client
import os
import tempfile
import time
import typing
import urllib.error
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from article_reader.domain.speech import VoiceSpec
from article_reader.speech.model_store import ArtifactState, ModelStore, portable_filename

_CHUNK_BYTES = 1024 * 1024
_MAX_REDIRECTS = 5
_USER_AGENT = "article-reader-voice-installer/1 (explicit local installation; not a browser)"


class VoiceInstallError(RuntimeError):
    """Base error for a failed, safely-cleaned-up voice installation attempt."""


class DownloadError(VoiceInstallError):
    """Raised when an artifact could not be retrieved safely over HTTPS."""


class DownloadTooLargeError(DownloadError):
    """Raised when a response exceeds the configured byte bound."""


class ChecksumMismatchError(VoiceInstallError):
    """Raised when downloaded bytes do not match the registry's pinned digest."""


@dataclass(frozen=True, slots=True)
class ArtifactInstallOutcome:
    """What happened to one of a voice's two artifacts during installation."""

    kind: str
    filename: str
    already_installed: bool
    byte_count: int


@dataclass(frozen=True, slots=True)
class VoiceInstallReport:
    """Result of one :meth:`VoiceInstaller.install` call."""

    voice_id: str
    artifacts: tuple[ArtifactInstallOutcome, ...]

    @property
    def already_installed(self) -> bool:
        return bool(self.artifacts) and all(
            artifact.already_installed for artifact in self.artifacts
        )


class ArtifactDownloader(Protocol):
    """Boundary around retrieving one HTTPS URL into a local file.

    Implementations must write at most ``max_bytes`` to ``destination`` and
    raise :class:`DownloadError` (or a subclass) on any failure, leaving no
    guarantee about partially written bytes at ``destination`` — the caller
    owns cleanup of its own temporary file.
    """

    def download(self, url: str, destination: Path, *, max_bytes: int) -> int:
        """Stream ``url`` into ``destination`` and return the byte count."""
        ...


def _require_https(url: str, *, context: str) -> None:
    if urlsplit(url).scheme != "https":
        raise DownloadError(f"{context} is not HTTPS: refusing to use it")


class _BoundedHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Cap redirect count and forbid any hop leaving HTTPS."""

    def __init__(self, max_redirects: int) -> None:
        super().__init__()
        self._max_redirects = max_redirects
        self._redirect_count = 0

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: typing.IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._redirect_count += 1
        if self._redirect_count > self._max_redirects:
            raise DownloadError("voice artifact download exceeded the maximum redirect count")
        _require_https(newurl, context="voice artifact redirect target")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibArtifactDownloader:
    """Default downloader: HTTPS-only, bounded, timeout-enforced streaming."""

    def __init__(
        self,
        *,
        connect_timeout_seconds: float,
        overall_timeout_seconds: float,
        user_agent: str = _USER_AGENT,
    ) -> None:
        if connect_timeout_seconds <= 0 or overall_timeout_seconds <= 0:
            raise ValueError("download timeouts must be positive")
        self._connect_timeout = connect_timeout_seconds
        self._overall_timeout = overall_timeout_seconds
        self._user_agent = user_agent

    def download(self, url: str, destination: Path, *, max_bytes: int) -> int:
        _require_https(url, context="voice artifact URL")
        opener = urllib.request.build_opener(_BoundedHTTPSRedirectHandler(_MAX_REDIRECTS))
        request = urllib.request.Request(url, headers={"User-Agent": self._user_agent})
        deadline = time.monotonic() + self._overall_timeout
        byte_count = 0
        try:
            with opener.open(request, timeout=self._connect_timeout) as response:
                _require_https(response.geturl(), context="voice artifact response URL")
                declared_length = response.headers.get("Content-Length")
                if declared_length is not None:
                    try:
                        parsed_length: int | None = int(declared_length)
                    except ValueError:
                        parsed_length = None
                    if parsed_length is not None and parsed_length > max_bytes:
                        raise DownloadTooLargeError(
                            f"declared download size {parsed_length} exceeds the "
                            f"{max_bytes}-byte limit"
                        )
                with destination.open("wb") as output:
                    while True:
                        if time.monotonic() > deadline:
                            raise DownloadError(
                                "voice artifact download exceeded its overall time budget"
                            )
                        chunk = response.read(_CHUNK_BYTES)
                        if not chunk:
                            break
                        byte_count += len(chunk)
                        if byte_count > max_bytes:
                            raise DownloadTooLargeError(
                                f"download exceeded the {max_bytes}-byte limit"
                            )
                        output.write(chunk)
        except DownloadError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise DownloadError(f"failed to download {url}: {error}") from error
        return byte_count


def _sha256_of(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while block := stream.read(_CHUNK_BYTES):
            digest.update(block)
    return digest.hexdigest()


class VoiceInstaller:
    """Install one exact registry voice's artifacts, verified and atomic.

    Installation is idempotent: artifacts already present with a matching
    SHA-256 digest are left untouched and never re-downloaded.
    """

    def __init__(
        self,
        model_store: ModelStore,
        downloader: ArtifactDownloader,
        *,
        max_bytes: int,
    ) -> None:
        self._model_store = model_store
        self._downloader = downloader
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes

    def install(self, voice: VoiceSpec) -> VoiceInstallReport:
        if not isinstance(voice, VoiceSpec):
            raise VoiceInstallError("voice installation requires a validated VoiceSpec")

        installation = self._model_store.inspect(voice)
        voice_directory = self._model_store.voice_directory(voice)
        try:
            voice_directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise VoiceInstallError(
                f"cannot create install directory for voice {voice.voice_id!r}"
            ) from error
        if not voice_directory.is_dir():
            raise VoiceInstallError(f"voice install path is not a directory: {voice_directory}")

        specs = (
            ("model", voice.model_url, voice.model_sha256),
            ("config", voice.config_url, voice.config_sha256),
        )
        outcomes: list[ArtifactInstallOutcome] = []
        for artifact, (kind, url, expected_sha256) in zip(
            installation.artifacts, specs, strict=True
        ):
            if artifact.kind != kind:
                raise VoiceInstallError("internal error: artifact ordering mismatch")
            if artifact.state is ArtifactState.VERIFIED:
                outcomes.append(
                    ArtifactInstallOutcome(
                        kind=kind,
                        filename=artifact.filename,
                        already_installed=True,
                        byte_count=artifact.byte_count or 0,
                    )
                )
                continue
            outcomes.append(
                self._install_one(
                    voice_directory,
                    kind=kind,
                    url=url,
                    filename=artifact.filename,
                    expected_sha256=expected_sha256,
                )
            )
        return VoiceInstallReport(voice_id=voice.voice_id, artifacts=tuple(outcomes))

    def _install_one(
        self,
        voice_directory: Path,
        *,
        kind: str,
        url: str,
        filename: str,
        expected_sha256: str,
    ) -> ArtifactInstallOutcome:
        final_path = voice_directory / filename
        if portable_filename(url) != filename:
            raise VoiceInstallError(
                f"internal error: {kind} artifact filename does not match its registry URL"
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{filename}-", suffix=".part", dir=voice_directory
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            byte_count = self._downloader.download(url, temporary_path, max_bytes=self._max_bytes)
            if byte_count <= 0:
                raise DownloadError(f"downloaded {kind} artifact for {filename!r} is empty")
            actual_sha256 = _sha256_of(temporary_path)
            if actual_sha256 != expected_sha256:
                raise ChecksumMismatchError(
                    f"{kind} artifact {filename!r} checksum mismatch: "
                    f"expected {expected_sha256}, got {actual_sha256}"
                )
            os.replace(temporary_path, final_path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise
        return ArtifactInstallOutcome(
            kind=kind, filename=filename, already_installed=False, byte_count=byte_count
        )


__all__ = [
    "ArtifactDownloader",
    "ArtifactInstallOutcome",
    "ChecksumMismatchError",
    "DownloadError",
    "DownloadTooLargeError",
    "UrllibArtifactDownloader",
    "VoiceInstallError",
    "VoiceInstallReport",
    "VoiceInstaller",
]
