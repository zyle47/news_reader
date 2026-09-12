"""Concrete Piper speech engine adapter behind the application's SpeechEngine port.

This is the only module in the project allowed to import ``piper``. Domain and
application code depend solely on
:class:`article_reader.application.ports.speech.SpeechEngine`; nothing inward
of this adapter knows Piper exists. Loading a voice never downloads anything:
it only reads artifacts that a prior, explicit ``voices install`` run already
placed on disk and verified by SHA-256 (see
:mod:`article_reader.speech.installer`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path
from typing import Any, Protocol

from article_reader.domain.speech import (
    AudioResult,
    SpeechDomainError,
    SpeechEngineDescriptor,
    SpeechEngineError,
    SpeechEngineNotLoadedError,
    SpeechSettings,
    VoiceSpec,
)
from article_reader.speech.model_store import ModelStore, portable_filename

_ENGINE_ID = "piper"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_TEXT_CHARACTERS = 100_000
_SUPPORTED_SETTINGS = frozenset(
    {"speaker_id", "length_scale", "noise_scale", "noise_w_scale", "normalize_audio", "volume"}
)


class PiperEngineError(SpeechEngineError):
    """Raised for Piper-specific load or synthesis failures."""


class PiperConfigError(PiperEngineError):
    """Raised when a voice's installed config file is missing, malformed, or incompatible."""


class _AudioChunk(Protocol):
    @property
    def sample_rate(self) -> int: ...

    @property
    def sample_width(self) -> int: ...

    @property
    def sample_channels(self) -> int: ...

    @property
    def audio_int16_bytes(self) -> bytes: ...


class _PiperVoice(Protocol):
    def synthesize(self, text: str, syn_config: Any = ...) -> Iterable[_AudioChunk]: ...


PiperVoiceFactory = Callable[[Path, Path, bool], _PiperVoice]


def _default_piper_voice_factory(
    model_path: Path, config_path: Path, use_cuda: bool
) -> _PiperVoice:
    try:
        from piper import PiperVoice
    except ImportError as error:
        raise PiperEngineError(
            "the piper-tts package is not installed in this environment"
        ) from error
    try:
        return PiperVoice.load(model_path, config_path=config_path, use_cuda=use_cuda)
    except Exception as error:
        raise PiperEngineError(f"piper failed to load model file {model_path.name!r}") from error


def _installed_piper_version() -> str:
    try:
        return _installed_version("piper-tts")
    except PackageNotFoundError as error:
        raise PiperEngineError(
            "the piper-tts package is not installed in this environment"
        ) from error


def _load_and_validate_config(config_path: Path, voice: VoiceSpec) -> None:
    try:
        raw_bytes = config_path.read_bytes()
    except OSError as error:
        raise PiperConfigError(
            f"cannot read the installed config for voice {voice.voice_id!r}: {config_path}"
        ) from error
    if len(raw_bytes) > _MAX_CONFIG_BYTES:
        raise PiperConfigError(
            f"installed config for voice {voice.voice_id!r} exceeds the "
            f"{_MAX_CONFIG_BYTES}-byte safety limit"
        )
    try:
        config = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PiperConfigError(
            f"installed config for voice {voice.voice_id!r} is not valid JSON"
        ) from error
    if not isinstance(config, dict):
        raise PiperConfigError(
            f"installed config for voice {voice.voice_id!r} must be a JSON object"
        )

    audio = config.get("audio")
    if not isinstance(audio, dict):
        raise PiperConfigError(
            f"installed config for voice {voice.voice_id!r} is missing an 'audio' section"
        )
    sample_rate = audio.get("sample_rate")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise PiperConfigError(
            f"installed config for voice {voice.voice_id!r} has a non-integer audio.sample_rate"
        )
    if sample_rate != voice.sample_rate_hz:
        raise PiperConfigError(
            f"installed config sample rate {sample_rate} for voice {voice.voice_id!r} does "
            f"not match the registered sample_rate_hz {voice.sample_rate_hz}"
        )


def _build_synthesis_config(settings: SpeechSettings) -> Any:
    from piper.config import SynthesisConfig

    values = settings.as_mapping()
    unsupported = set(values) - _SUPPORTED_SETTINGS
    if unsupported:
        raise SpeechDomainError(
            f"the piper engine does not support speech settings: {sorted(unsupported)}"
        )
    kwargs: dict[str, Any] = {key: values[key] for key in _SUPPORTED_SETTINGS if key in values}
    try:
        return SynthesisConfig(**kwargs)
    except (TypeError, ValueError) as error:
        raise SpeechDomainError(f"invalid piper synthesis settings: {error}") from error


class PiperEngine:
    """Loads one exact, installed Piper voice at a time and returns owned PCM bytes."""

    def __init__(
        self,
        model_store: ModelStore,
        *,
        use_cuda: bool = False,
        voice_factory: PiperVoiceFactory | None = None,
        engine_version: str | None = None,
    ) -> None:
        self._model_store = model_store
        self._use_cuda = use_cuda
        self._voice_factory: PiperVoiceFactory = voice_factory or _default_piper_voice_factory
        self._engine_version = engine_version or _installed_piper_version()
        self._voice: VoiceSpec | None = None
        self._piper_voice: _PiperVoice | None = None

    @property
    def descriptor(self) -> SpeechEngineDescriptor:
        return SpeechEngineDescriptor(
            engine_id=_ENGINE_ID,
            engine_version=self._engine_version,
            is_test_double=False,
        )

    def load(self, voice: VoiceSpec) -> None:
        if not isinstance(voice, VoiceSpec):
            raise SpeechDomainError("piper engine requires a validated VoiceSpec")
        if voice.engine.casefold() != _ENGINE_ID:
            raise PiperEngineError(
                f"piper engine cannot load a voice registered for engine {voice.engine!r}"
            )
        if self._voice is not None and self._voice.voice_id == voice.voice_id:
            return

        self.close()

        installation = self._model_store.inspect(voice)
        if not installation.installed:
            raise PiperEngineError(
                f"voice {voice.voice_id!r} is not installed; run the voice installer first"
            )

        voice_directory = self._model_store.voice_directory(voice)
        model_path = voice_directory / portable_filename(voice.model_url)
        config_path = voice_directory / portable_filename(voice.config_url)

        _load_and_validate_config(config_path, voice)

        self._piper_voice = self._voice_factory(model_path, config_path, self._use_cuda)
        self._voice = voice

    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        piper_voice = self._piper_voice
        voice = self._voice
        if piper_voice is None or voice is None:
            raise SpeechEngineNotLoadedError("load a voice before requesting synthesis")
        if not isinstance(text, str):
            raise SpeechDomainError("speech text must be a string")
        if not text.strip():
            raise SpeechDomainError("speech text cannot be empty or whitespace-only")
        if "\x00" in text:
            raise SpeechDomainError("speech text cannot contain NUL characters")
        if len(text) > _MAX_TEXT_CHARACTERS:
            raise SpeechDomainError("speech text exceeds the piper adapter safety limit")
        if not isinstance(settings, SpeechSettings):
            raise SpeechDomainError("settings must be a validated SpeechSettings value")

        syn_config = _build_synthesis_config(settings)

        pcm_chunks: list[bytes] = []
        sample_rate: int | None = None
        sample_width: int | None = None
        channels: int | None = None
        try:
            for chunk in piper_voice.synthesize(text, syn_config):
                if sample_rate is None:
                    sample_rate = chunk.sample_rate
                    sample_width = chunk.sample_width
                    channels = chunk.sample_channels
                pcm_chunks.append(chunk.audio_int16_bytes)
        except Exception as error:
            raise PiperEngineError(
                f"piper failed to synthesize speech for voice {voice.voice_id!r}"
            ) from error

        if not pcm_chunks or sample_rate is None or sample_width is None or channels is None:
            raise PiperEngineError(f"piper produced no audio for voice {voice.voice_id!r}")
        if sample_rate != voice.sample_rate_hz:
            raise PiperEngineError(
                f"piper produced sample rate {sample_rate}, expected {voice.sample_rate_hz} "
                f"for voice {voice.voice_id!r}"
            )

        return AudioResult(
            pcm_bytes=b"".join(pcm_chunks),
            sample_rate_hz=sample_rate,
            sample_width_bytes=sample_width,
            channels=channels,
        )

    def close(self) -> None:
        self._piper_voice = None
        self._voice = None


__all__ = [
    "PiperConfigError",
    "PiperEngine",
    "PiperEngineError",
    "PiperVoiceFactory",
]
