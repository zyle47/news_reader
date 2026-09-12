from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from article_reader.domain.speech import Language, Script, VoiceEvaluationStatus, VoiceSpec
from article_reader.speech.installer import (
    ArtifactDownloader,
    ChecksumMismatchError,
    DownloadError,
    DownloadTooLargeError,
    UrllibArtifactDownloader,
    VoiceInstaller,
    VoiceInstallError,
)
from article_reader.speech.model_store import ModelStore


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


_MODEL_BYTES = b"fixture model bytes " * 100
_CONFIG_BYTES = b'{"audio": {"sample_rate": 16000}}'


def _voice(*, model_sha256: str | None = None, config_sha256: str | None = None) -> VoiceSpec:
    return VoiceSpec(
        voice_id="fixture-piper-en",
        display_name="Fixture Piper English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine="piper",
        engine_version="1.8.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.test/fixture",
        source_revision="v1.0.0",
        model_url="https://models.example.test/files/fixture.onnx",
        model_sha256=model_sha256 or _digest(_MODEL_BYTES),
        config_url="https://models.example.test/files/fixture.onnx.json",
        config_sha256=config_sha256 or _digest(_CONFIG_BYTES),
        model_license_url="https://models.example.test/licenses/model",
        data_license_url="https://models.example.test/licenses/data",
        evaluation_status=VoiceEvaluationStatus.UNVERIFIED,
        evaluation_notes="Synthetic metadata used by an offline unit test.",
    )


class _FakeDownloader:
    """Deterministic, offline stand-in for :class:`ArtifactDownloader`."""

    def __init__(
        self,
        content_by_url: dict[str, bytes] | None = None,
        *,
        raise_for: dict[str, Exception] | None = None,
        partial_bytes_for: dict[str, bytes] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._content_by_url = content_by_url or {}
        self._raise_for = raise_for or {}
        self._partial_bytes_for = partial_bytes_for or {}

    def download(self, url: str, destination: Path, *, max_bytes: int) -> int:
        self.calls.append(url)
        if url in self._partial_bytes_for:
            destination.write_bytes(self._partial_bytes_for[url])
            raise DownloadError("simulated connection drop mid-transfer")
        if url in self._raise_for:
            raise self._raise_for[url]
        content = self._content_by_url[url]
        if len(content) > max_bytes:
            raise DownloadTooLargeError("fixture: declared content exceeds max_bytes")
        destination.write_bytes(content)
        return len(content)


def _no_leftover_temp_files(voice_directory: Path) -> bool:
    return not any(voice_directory.glob("*.part"))


def test_install_downloads_verifies_and_publishes_both_artifacts(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader(
        {
            voice.model_url: _MODEL_BYTES,
            voice.config_url: _CONFIG_BYTES,
        }
    )
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    report = installer.install(voice)

    assert report.voice_id == voice.voice_id
    assert not report.already_installed
    assert {artifact.kind for artifact in report.artifacts} == {"model", "config"}
    assert all(not artifact.already_installed for artifact in report.artifacts)
    assert store.inspect(voice).installed
    assert sorted(downloader.calls) == sorted([voice.model_url, voice.config_url])
    assert _no_leftover_temp_files(store.voice_directory(voice))


def test_install_is_idempotent_and_never_redownloads_verified_artifacts(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True)
    (voice_directory / "fixture.onnx").write_bytes(_MODEL_BYTES)
    (voice_directory / "fixture.onnx.json").write_bytes(_CONFIG_BYTES)

    downloader = _FakeDownloader()  # any call raises KeyError: nothing should be requested
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    report = installer.install(voice)

    assert report.already_installed
    assert all(artifact.already_installed for artifact in report.artifacts)
    assert downloader.calls == []


def test_install_repairs_only_the_missing_artifact(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True)
    (voice_directory / "fixture.onnx.json").write_bytes(_CONFIG_BYTES)  # config already installed

    downloader = _FakeDownloader({voice.model_url: _MODEL_BYTES})
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    report = installer.install(voice)

    outcomes = {artifact.kind: artifact for artifact in report.artifacts}
    assert outcomes["config"].already_installed
    assert not outcomes["model"].already_installed
    assert downloader.calls == [voice.model_url]
    assert store.inspect(voice).installed


def test_checksum_mismatch_is_rejected_and_leaves_no_artifact(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader(
        {
            voice.model_url: b"wrong bytes entirely",
            voice.config_url: _CONFIG_BYTES,
        }
    )
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    with pytest.raises(ChecksumMismatchError, match="checksum mismatch"):
        installer.install(voice)

    voice_directory = store.voice_directory(voice)
    assert not (voice_directory / "fixture.onnx").exists()
    assert _no_leftover_temp_files(voice_directory)


def test_interrupted_download_leaves_no_partial_artifact(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader(
        partial_bytes_for={voice.model_url: _MODEL_BYTES[:10]},
    )
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    with pytest.raises(DownloadError, match="connection drop"):
        installer.install(voice)

    voice_directory = store.voice_directory(voice)
    assert not (voice_directory / "fixture.onnx").exists()
    assert _no_leftover_temp_files(voice_directory)


def test_empty_download_is_rejected(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader({voice.model_url: b""})
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    with pytest.raises(DownloadError, match="empty"):
        installer.install(voice)

    assert _no_leftover_temp_files(store.voice_directory(voice))


def test_oversized_declared_download_is_rejected(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader({voice.model_url: _MODEL_BYTES})
    installer = VoiceInstaller(store, downloader, max_bytes=len(_MODEL_BYTES) - 1)

    with pytest.raises(DownloadTooLargeError):
        installer.install(voice)

    assert _no_leftover_temp_files(store.voice_directory(voice))


def test_download_failure_propagates_and_cleans_up(tmp_path: Path) -> None:
    voice = _voice()
    store = ModelStore(tmp_path)
    downloader = _FakeDownloader(raise_for={voice.model_url: DownloadError("network unreachable")})
    installer = VoiceInstaller(store, downloader, max_bytes=10_000)

    with pytest.raises(DownloadError, match="network unreachable"):
        installer.install(voice)

    assert _no_leftover_temp_files(store.voice_directory(voice))


def test_installer_rejects_invalid_voice_type(tmp_path: Path) -> None:
    installer = VoiceInstaller(ModelStore(tmp_path), _FakeDownloader(), max_bytes=10_000)

    with pytest.raises(VoiceInstallError, match="VoiceSpec"):
        installer.install("not-a-voice-spec")  # type: ignore[arg-type]


def test_max_bytes_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        VoiceInstaller(ModelStore(tmp_path), _FakeDownloader(), max_bytes=0)


def test_urllib_downloader_rejects_non_https_url_before_connecting(tmp_path: Path) -> None:
    downloader: ArtifactDownloader = UrllibArtifactDownloader(
        connect_timeout_seconds=1,
        overall_timeout_seconds=1,
    )

    with pytest.raises(DownloadError, match="HTTPS"):
        downloader.download(
            "http://models.example.test/insecure.onnx",
            tmp_path / "out.bin",
            max_bytes=1_000,
        )


def test_urllib_downloader_rejects_non_positive_timeouts() -> None:
    with pytest.raises(ValueError, match="positive"):
        UrllibArtifactDownloader(connect_timeout_seconds=0, overall_timeout_seconds=1)
    with pytest.raises(ValueError, match="positive"):
        UrllibArtifactDownloader(connect_timeout_seconds=1, overall_timeout_seconds=0)
