from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from article_reader.domain.speech import (
    Language,
    Script,
    SpeechDomainError,
    SpeechEngineNotLoadedError,
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.speech.model_store import ModelStore
from article_reader.speech.piper_engine import PiperConfigError, PiperEngine, PiperEngineError

_SAMPLE_RATE = 16_000
_MODEL_BYTES = b"fixture onnx model bytes"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _config_bytes(sample_rate: int = _SAMPLE_RATE) -> bytes:
    return json.dumps({"audio": {"sample_rate": sample_rate}}).encode("utf-8")


def _voice(*, engine: str = "piper", sample_rate_hz: int = _SAMPLE_RATE) -> VoiceSpec:
    return VoiceSpec(
        voice_id="fixture-piper-en",
        display_name="Fixture Piper English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine=engine,
        engine_version="1.8.0",
        sample_rate_hz=sample_rate_hz,
        source_url="https://models.example.test/fixture",
        source_revision="v1.0.0",
        model_url="https://models.example.test/files/fixture.onnx",
        model_sha256=_digest(_MODEL_BYTES),
        config_url="https://models.example.test/files/fixture.onnx.json",
        config_sha256=_digest(_config_bytes(sample_rate_hz)),
        model_license_url="https://models.example.test/licenses/model",
        data_license_url="https://models.example.test/licenses/data",
        evaluation_status=VoiceEvaluationStatus.UNVERIFIED,
        evaluation_notes="Synthetic metadata used by an offline unit test.",
    )


def _install(store: ModelStore, voice: VoiceSpec, *, config_bytes: bytes | None = None) -> None:
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True, exist_ok=True)
    (voice_directory / "fixture.onnx").write_bytes(_MODEL_BYTES)
    (voice_directory / "fixture.onnx.json").write_bytes(
        config_bytes if config_bytes is not None else _config_bytes(voice.sample_rate_hz)
    )


@dataclass(frozen=True, slots=True)
class _FakeChunk:
    sample_rate: int
    sample_width: int
    sample_channels: int
    audio_int16_bytes: bytes


class _FakeVoice:
    """Stand-in for ``piper.PiperVoice`` implementing only what the adapter uses."""

    def __init__(
        self,
        chunks: list[_FakeChunk] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self._chunks = (
            chunks if chunks is not None else [_FakeChunk(_SAMPLE_RATE, 2, 1, b"\x01\x02")]
        )
        self._error = error
        self.synthesize_calls: list[str] = []

    def synthesize(self, text: str, syn_config: object = None) -> Iterator[_FakeChunk]:
        self.synthesize_calls.append(text)
        if self._error is not None:
            raise self._error
        yield from self._chunks


def _factory(
    fake_voice: _FakeVoice, *, calls: list[tuple[Path, Path, bool]] | None = None
) -> Callable[[Path, Path, bool], _FakeVoice]:
    def factory(model_path: Path, config_path: Path, use_cuda: bool) -> _FakeVoice:
        if calls is not None:
            calls.append((model_path, config_path, use_cuda))
        return fake_voice

    return factory


def test_load_then_synthesize_returns_valid_audio_result(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    fake_voice = _FakeVoice([_FakeChunk(_SAMPLE_RATE, 2, 1, b"\x01\x00\x02\x00")])
    engine = PiperEngine(
        store, voice_factory=_factory(fake_voice), engine_version=voice.engine_version
    )

    engine.load(voice)
    result = engine.synthesize("Hello there.", SpeechSettings())

    assert result.sample_rate_hz == _SAMPLE_RATE
    assert result.sample_width_bytes == 2
    assert result.channels == 1
    assert result.pcm_bytes == b"\x01\x00\x02\x00"
    assert fake_voice.synthesize_calls == ["Hello there."]


def test_descriptor_reports_piper_engine_id_and_version(tmp_path: Path) -> None:
    engine = PiperEngine(
        ModelStore(tmp_path), voice_factory=_factory(_FakeVoice()), engine_version="1.8.0"
    )

    descriptor = engine.descriptor

    assert descriptor.engine_id == "piper"
    assert descriptor.engine_version == "1.8.0"
    assert descriptor.is_test_double is False


def test_synthesize_without_load_raises_not_loaded(tmp_path: Path) -> None:
    engine = PiperEngine(
        ModelStore(tmp_path), voice_factory=_factory(_FakeVoice()), engine_version="1.8.0"
    )

    with pytest.raises(SpeechEngineNotLoadedError):
        engine.synthesize("text", SpeechSettings())


def test_load_fails_when_voice_is_not_installed(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")

    with pytest.raises(PiperEngineError, match="not installed"):
        engine.load(voice)


def test_load_rejects_voice_registered_for_a_different_engine(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice(engine="fake")
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")

    with pytest.raises(PiperEngineError, match="piper engine cannot"):
        engine.load(voice)


def test_load_is_idempotent_for_the_same_voice(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    calls: list[tuple[Path, Path, bool]] = []
    engine = PiperEngine(
        store, voice_factory=_factory(_FakeVoice(), calls=calls), engine_version="1.8.0"
    )

    engine.load(voice)
    engine.load(voice)

    assert len(calls) == 1


def test_malformed_json_config_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True)
    (voice_directory / "fixture.onnx").write_bytes(_MODEL_BYTES)
    bad_config = b"{not valid json"
    object.__setattr__(voice, "config_sha256", _digest(bad_config))
    (voice_directory / "fixture.onnx.json").write_bytes(bad_config)
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")

    with pytest.raises(PiperConfigError, match="not valid JSON"):
        engine.load(voice)


def test_config_missing_audio_section_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True)
    (voice_directory / "fixture.onnx").write_bytes(_MODEL_BYTES)
    bad_config = json.dumps({"not_audio": {}}).encode("utf-8")
    object.__setattr__(voice, "config_sha256", _digest(bad_config))
    (voice_directory / "fixture.onnx.json").write_bytes(bad_config)
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")

    with pytest.raises(PiperConfigError, match="audio"):
        engine.load(voice)


def test_config_sample_rate_mismatch_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice(sample_rate_hz=16_000)
    voice_directory = store.voice_directory(voice)
    voice_directory.mkdir(parents=True)
    (voice_directory / "fixture.onnx").write_bytes(_MODEL_BYTES)
    mismatched_config = _config_bytes(sample_rate=22_050)
    object.__setattr__(voice, "config_sha256", _digest(mismatched_config))
    (voice_directory / "fixture.onnx.json").write_bytes(mismatched_config)
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")

    with pytest.raises(PiperConfigError, match="does not match"):
        engine.load(voice)


def test_engine_synthesis_failure_is_wrapped_and_state_stays_usable(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    fake_voice = _FakeVoice(error=RuntimeError("onnxruntime exploded"))
    engine = PiperEngine(store, voice_factory=_factory(fake_voice), engine_version="1.8.0")
    engine.load(voice)

    with pytest.raises(PiperEngineError, match="failed to synthesize"):
        engine.synthesize("some text", SpeechSettings())


def test_engine_producing_no_audio_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    fake_voice = _FakeVoice(chunks=[])
    engine = PiperEngine(store, voice_factory=_factory(fake_voice), engine_version="1.8.0")
    engine.load(voice)

    with pytest.raises(PiperEngineError, match="no audio"):
        engine.synthesize("some text", SpeechSettings())


def test_engine_sample_rate_mismatch_with_registry_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice(sample_rate_hz=16_000)
    _install(store, voice)
    fake_voice = _FakeVoice([_FakeChunk(22_050, 2, 1, b"\x00\x00")])
    engine = PiperEngine(store, voice_factory=_factory(fake_voice), engine_version="1.8.0")
    engine.load(voice)

    with pytest.raises(PiperEngineError, match="sample rate"):
        engine.synthesize("some text", SpeechSettings())


def test_synthesize_rejects_empty_text(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")
    engine.load(voice)

    with pytest.raises(SpeechDomainError, match="empty"):
        engine.synthesize("   ", SpeechSettings())


def test_unsupported_speech_setting_is_rejected(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice = _voice()
    _install(store, voice)
    engine = PiperEngine(store, voice_factory=_factory(_FakeVoice()), engine_version="1.8.0")
    engine.load(voice)

    with pytest.raises(SpeechDomainError, match="does not support"):
        engine.synthesize("hello", SpeechSettings.from_mapping({"unknown_setting": 1}))


def test_close_allows_reloading_a_different_voice(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    voice_a = _voice()
    _install(store, voice_a)
    calls: list[tuple[Path, Path, bool]] = []
    engine = PiperEngine(
        store, voice_factory=_factory(_FakeVoice(), calls=calls), engine_version="1.8.0"
    )

    engine.load(voice_a)
    engine.close()
    with pytest.raises(SpeechEngineNotLoadedError):
        engine.synthesize("text", SpeechSettings())
