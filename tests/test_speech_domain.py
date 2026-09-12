from __future__ import annotations

import unittest
import wave
from dataclasses import FrozenInstanceError
from io import BytesIO

from article_reader.domain.speech import (
    AudioResult,
    Language,
    Script,
    SpeechDomainError,
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)


def _voice(**overrides: object) -> VoiceSpec:
    values: dict[str, object] = {
        "voice_id": "en_US-test-medium",
        "display_name": "English test voice",
        "language": Language.ENGLISH,
        "scripts": (Script.LATIN,),
        "engine": "piper",
        "engine_version": "1.3.0",
        "sample_rate_hz": 16_000,
        "source_url": "https://models.example.com/voices/test",
        "source_revision": "0123456789abcdef0123456789abcdef01234567",
        "model_url": "https://models.example.com/voices/test/model.onnx",
        "model_sha256": "a" * 64,
        "config_url": "https://models.example.com/voices/test/model.onnx.json",
        "config_sha256": "b" * 64,
        "model_license_url": "https://models.example.com/licenses/model",
        "data_license_url": "https://models.example.com/licenses/data",
        "evaluation_status": VoiceEvaluationStatus.CANDIDATE,
        "evaluation_notes": "Synthetic fixture; not evaluated.",
    }
    values.update(overrides)
    return VoiceSpec(**values)  # type: ignore[arg-type]


class SpeechDomainTests(unittest.TestCase):
    def test_voice_spec_is_frozen_and_canonicalizes_hashes(self) -> None:
        voice = _voice(model_sha256="A" * 64)

        self.assertEqual(voice.model_sha256, "a" * 64)
        with self.assertRaises(FrozenInstanceError):
            voice.voice_id = "changed"  # type: ignore[misc]

    def test_voice_spec_rejects_unvalidated_enum_and_floating_revision(self) -> None:
        with self.assertRaisesRegex(SpeechDomainError, "Language enum"):
            _voice(language="en")
        with self.assertRaisesRegex(SpeechDomainError, "pinned"):
            _voice(source_revision="main")

    def test_approved_fake_voice_is_impossible(self) -> None:
        with self.assertRaisesRegex(SpeechDomainError, "fake engine voice"):
            _voice(
                engine="fake",
                evaluation_status=VoiceEvaluationStatus.APPROVED,
                evaluation_notes="Approved by mistake.",
            )

    def test_settings_are_immutable_sorted_and_scalar(self) -> None:
        settings = SpeechSettings.from_mapping({"noise_scale": 0.5, "speaker_id": 2})

        self.assertEqual(settings.options, (("noise_scale", 0.5), ("speaker_id", 2)))
        self.assertEqual(dict(settings.as_mapping()), {"noise_scale": 0.5, "speaker_id": 2})
        with self.assertRaisesRegex(SpeechDomainError, "duplicate"):
            SpeechSettings((("speaker_id", 1), ("speaker_id", 2)))
        with self.assertRaisesRegex(SpeechDomainError, "finite"):
            SpeechSettings((("noise_scale", float("nan")),))

    def test_audio_result_validates_frames_and_builds_pcm_wav(self) -> None:
        pcm = b"\x00\x00\x10\x00\xf0\xff"
        audio = AudioResult(
            pcm_bytes=pcm,
            sample_rate_hz=16_000,
            sample_width_bytes=2,
            channels=1,
        )

        self.assertEqual(audio.frame_count, 3)
        self.assertEqual(audio.duration_seconds, 3 / 16_000)
        with wave.open(BytesIO(audio.as_wav_bytes()), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 16_000)
            self.assertEqual(wav_file.readframes(3), pcm)

    def test_audio_result_has_no_path_and_rejects_misaligned_pcm(self) -> None:
        with self.assertRaisesRegex(SpeechDomainError, "aligned"):
            AudioResult(
                pcm_bytes=b"\x00\x01\x02",
                sample_rate_hz=16_000,
                sample_width_bytes=2,
                channels=1,
            )
        self.assertNotIn("path", AudioResult.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main()
