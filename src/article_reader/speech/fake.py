"""Deterministic speech test double producing valid mono PCM."""

from __future__ import annotations

import hashlib
import struct

from article_reader.domain.speech import (
    AudioResult,
    SpeechDomainError,
    SpeechEngineDescriptor,
    SpeechEngineNotLoadedError,
    SpeechSettings,
    VoiceSpec,
)


class FakeSpeechEngine:
    """Small offline engine for pipeline tests, never voice-quality evidence."""

    _DESCRIPTOR = SpeechEngineDescriptor(
        engine_id="fake",
        engine_version="1.0.0",
        is_test_double=True,
    )

    def __init__(self) -> None:
        self._voice: VoiceSpec | None = None
        self._load_count = 0
        self._synthesis_count = 0

    @property
    def descriptor(self) -> SpeechEngineDescriptor:
        return self._DESCRIPTOR

    @property
    def load_count(self) -> int:
        return self._load_count

    @property
    def synthesis_count(self) -> int:
        return self._synthesis_count

    @property
    def loaded_voice(self) -> VoiceSpec | None:
        return self._voice

    def load(self, voice: VoiceSpec) -> None:
        if not isinstance(voice, VoiceSpec):
            raise SpeechDomainError("fake engine requires a validated VoiceSpec")
        self._voice = voice
        self._load_count += 1

    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        voice = self._voice
        if voice is None:
            raise SpeechEngineNotLoadedError("load a voice before requesting synthesis")
        if not isinstance(text, str):
            raise SpeechDomainError("speech text must be a string")
        if not text.strip():
            raise SpeechDomainError("speech text cannot be empty or whitespace-only")
        if "\x00" in text:
            raise SpeechDomainError("speech text cannot contain NUL characters")
        if len(text) > 100_000:
            raise SpeechDomainError("speech text exceeds the fake engine safety limit")
        if not isinstance(settings, SpeechSettings):
            raise SpeechDomainError("settings must be a validated SpeechSettings value")

        sample_rate = voice.sample_rate_hz
        frames_per_character = max(1, sample_rate // 100)
        edge_silence_frames = max(1, sample_rate // 100)
        content_frames = max(frames_per_character, len(text) * frames_per_character)
        frame_count = edge_silence_frames * 2 + content_frames
        pcm = bytearray(frame_count * 2)

        seed_material = "\u241f".join(
            (
                voice.voice_id,
                voice.model_sha256,
                text,
                repr(settings.options),
            )
        ).encode("utf-8")
        digest = hashlib.sha256(seed_material).digest()

        for frame_index in range(content_frames):
            text_index = min(len(text) - 1, frame_index // frames_per_character)
            character = text[text_index]
            if character.isspace():
                sample = 0
            else:
                digest_index = (text_index + ord(character)) % len(digest)
                period = 12 + digest[digest_index] % 52
                amplitude = 1_200 + digest[(digest_index + 1) % len(digest)] * 28
                half_period = max(1, period // 2)
                sample = amplitude if (frame_index // half_period) % 2 == 0 else -amplitude
            struct.pack_into("<h", pcm, (edge_silence_frames + frame_index) * 2, sample)

        self._synthesis_count += 1
        return AudioResult(
            pcm_bytes=bytes(pcm),
            sample_rate_hz=sample_rate,
            sample_width_bytes=2,
            channels=1,
        )

    def close(self) -> None:
        self._voice = None
