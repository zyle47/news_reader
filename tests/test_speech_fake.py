from __future__ import annotations

import unittest
import wave
from io import BytesIO

from article_reader.application.ports.speech import SpeechEngine
from article_reader.domain.speech import (
    Language,
    Script,
    SpeechDomainError,
    SpeechEngineNotLoadedError,
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.speech.fake import FakeSpeechEngine


def _voice() -> VoiceSpec:
    return VoiceSpec(
        voice_id="sr_RS-functional-medium",
        display_name="Serbian functional fixture",
        language=Language.SERBIAN,
        scripts=(Script.LATIN, Script.CYRILLIC),
        engine="piper",
        engine_version="1.3.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.com/voices/sr",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        model_url="https://models.example.com/voices/sr/model.onnx",
        model_sha256="a" * 64,
        config_url="https://models.example.com/voices/sr/model.onnx.json",
        config_sha256="b" * 64,
        model_license_url="https://models.example.com/licenses/model",
        data_license_url="https://models.example.com/licenses/data",
        evaluation_status=VoiceEvaluationStatus.CANDIDATE,
        evaluation_notes="Fixture only; Serbian quality is unverified.",
    )


class FakeSpeechEngineTests(unittest.TestCase):
    def test_implements_protocol_and_is_explicitly_a_test_double(self) -> None:
        engine = FakeSpeechEngine()

        self.assertIsInstance(engine, SpeechEngine)
        self.assertTrue(engine.descriptor.is_test_double)
        self.assertEqual(engine.descriptor.engine_id, "fake")

    def test_synthesis_is_deterministic_valid_mono_pcm(self) -> None:
        first_engine = FakeSpeechEngine()
        second_engine = FakeSpeechEngine()
        for engine in (first_engine, second_engine):
            engine.load(_voice())

        first = first_engine.synthesize("Zdravo, svete!", SpeechSettings())
        second = second_engine.synthesize("Zdravo, svete!", SpeechSettings())

        self.assertEqual(first, second)
        self.assertEqual(first.channels, 1)
        self.assertEqual(first.sample_width_bytes, 2)
        self.assertGreater(first.duration_seconds, 0)
        with wave.open(BytesIO(first.as_wav_bytes()), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getcomptype(), "NONE")
            self.assertEqual(wav_file.readframes(wav_file.getnframes()), first.pcm_bytes)

    def test_text_and_settings_are_part_of_deterministic_output(self) -> None:
        engine = FakeSpeechEngine()
        engine.load(_voice())

        baseline = engine.synthesize("Isti tekst", SpeechSettings())
        changed_text = engine.synthesize("Drugi tekst", SpeechSettings())
        changed_settings = engine.synthesize(
            "Isti tekst",
            SpeechSettings.from_mapping({"fixture_variant": 2}),
        )

        self.assertNotEqual(baseline.pcm_sha256, changed_text.pcm_sha256)
        self.assertNotEqual(baseline.pcm_sha256, changed_settings.pcm_sha256)

    def test_requires_loaded_voice_and_non_empty_text(self) -> None:
        engine = FakeSpeechEngine()

        with self.assertRaises(SpeechEngineNotLoadedError):
            engine.synthesize("text", SpeechSettings())
        engine.load(_voice())
        with self.assertRaises(SpeechDomainError):
            engine.synthesize("  ", SpeechSettings())
        engine.close()
        with self.assertRaises(SpeechEngineNotLoadedError):
            engine.synthesize("text", SpeechSettings())

    def test_counts_cold_and_warm_load_calls(self) -> None:
        engine = FakeSpeechEngine()
        voice = _voice()

        engine.load(voice)
        engine.load(voice)

        self.assertEqual(engine.load_count, 2)


if __name__ == "__main__":
    unittest.main()
