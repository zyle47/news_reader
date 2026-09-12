from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from article_reader.domain.speech import Language, Script, VoiceEvaluationStatus, VoiceSpec
from article_reader.speech.model_store import ArtifactState, ModelStore


def _voice(model_sha256: str, config_sha256: str) -> VoiceSpec:
    return VoiceSpec(
        voice_id="fixture-en",
        display_name="Fixture English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine="fixture",
        engine_version="1.0.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.test/fixture",
        source_revision="v1.0.0",
        model_url="https://models.example.test/files/fixture.onnx",
        model_sha256=model_sha256,
        config_url="https://models.example.test/files/fixture.onnx.json",
        config_sha256=config_sha256,
        model_license_url="https://models.example.test/licenses/model",
        data_license_url="https://models.example.test/licenses/data",
        evaluation_status=VoiceEvaluationStatus.UNVERIFIED,
        evaluation_notes="Synthetic metadata used by an offline unit test.",
    )


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_missing_voice_artifacts_are_not_installed(tmp_path: Path) -> None:
    voice = _voice("0" * 64, "1" * 64)

    result = ModelStore(tmp_path).inspect(voice)

    assert not result.installed
    assert [artifact.state for artifact in result.artifacts] == [
        ArtifactState.MISSING,
        ArtifactState.MISSING,
    ]


def test_voice_is_installed_only_when_every_digest_matches(tmp_path: Path) -> None:
    model_bytes = b"model fixture"
    config_bytes = b'{"fixture": true}'
    voice = _voice(_digest(model_bytes), _digest(config_bytes))
    voice_directory = tmp_path / voice.voice_id
    voice_directory.mkdir()
    (voice_directory / "fixture.onnx").write_bytes(model_bytes)
    (voice_directory / "fixture.onnx.json").write_bytes(config_bytes)

    result = ModelStore(tmp_path).inspect(voice)

    assert result.installed
    assert all(artifact.state is ArtifactState.VERIFIED for artifact in result.artifacts)


def test_digest_mismatch_is_reported_without_accepting_voice(tmp_path: Path) -> None:
    voice = _voice("0" * 64, "1" * 64)
    voice_directory = tmp_path / voice.voice_id
    voice_directory.mkdir()
    (voice_directory / "fixture.onnx").write_bytes(b"wrong")
    (voice_directory / "fixture.onnx.json").write_bytes(b"also wrong")

    result = ModelStore(tmp_path).inspect(voice)

    assert not result.installed
    assert all(artifact.state is ArtifactState.DIGEST_MISMATCH for artifact in result.artifacts)


def test_models_directory_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute"):
        ModelStore(Path("relative/models"))


def test_registry_artifact_filename_must_be_portable(tmp_path: Path) -> None:
    voice = _voice("0" * 64, "1" * 64)
    object.__setattr__(voice, "model_url", "https://models.example.test/files/bad%3Fname.onnx")

    with pytest.raises(ValueError, match="portable"):
        ModelStore(tmp_path).inspect(voice)


def test_windows_reserved_artifact_filename_is_rejected(tmp_path: Path) -> None:
    voice = _voice("0" * 64, "1" * 64)
    object.__setattr__(voice, "model_url", "https://models.example.test/files/NUL.onnx")

    with pytest.raises(ValueError, match="reserved"):
        ModelStore(tmp_path).inspect(voice)


def test_model_and_config_filenames_must_be_distinct(tmp_path: Path) -> None:
    voice = _voice("0" * 64, "1" * 64)
    object.__setattr__(voice, "config_url", voice.model_url)

    with pytest.raises(ValueError, match="distinct"):
        ModelStore(tmp_path).inspect(voice)
