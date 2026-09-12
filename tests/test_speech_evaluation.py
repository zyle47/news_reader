from __future__ import annotations

import json
import os
import platform
import tempfile
import unittest
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from article_reader.application.services.voice_evaluation import (
    EvaluationError,
    EvidenceKind,
    VoiceEvaluationService,
    current_rss_bytes,
)
from article_reader.domain.speech import (
    AudioResult,
    EvaluationPassage,
    Language,
    Script,
    SpeechEngineDescriptor,
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.speech.fake import FakeSpeechEngine


def _voice() -> VoiceSpec:
    return VoiceSpec(
        voice_id="sr_RS-evaluation-medium",
        display_name="Serbian evaluation fixture",
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
        evaluation_notes="Fixture only; Serbian quality remains unverified.",
    )


class InvalidResultEngine:
    descriptor = SpeechEngineDescriptor("invalid", "1.0.0", is_test_double=True)

    def load(self, voice: VoiceSpec) -> None:
        pass

    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        return cast(AudioResult, object())

    def close(self) -> None:
        pass


class FailingSecondPassageEngine(FakeSpeechEngine):
    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        if self.synthesis_count == 1:
            raise RuntimeError("fixture synthesis failure")
        return super().synthesize(text, settings)


class WrongSampleRateEngine(FakeSpeechEngine):
    def synthesize(self, text: str, settings: SpeechSettings) -> AudioResult:
        audio = super().synthesize(text, settings)
        return AudioResult(
            pcm_bytes=audio.pcm_bytes,
            sample_rate_hz=8_000,
            sample_width_bytes=audio.sample_width_bytes,
            channels=audio.channels,
        )


class VoiceEvaluationServiceTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific RSS availability check")
    def test_windows_rss_reader_returns_resident_bytes(self) -> None:
        resident_bytes = current_rss_bytes()

        self.assertIsNotNone(resident_bytes)
        assert resident_bytes is not None
        self.assertGreater(resident_bytes, 0)

    def test_fake_evaluation_writes_verified_wav_and_explicit_non_quality_report(self) -> None:
        engine = FakeSpeechEngine()
        service = VoiceEvaluationService(
            engine,
            utc_clock=lambda: datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        )
        passages = (
            EvaluationPassage(
                passage_id="latin-short",
                text="Četiri čavčića na čunčiću čučeći cijuču.",
                language=Language.SERBIAN,
                script=Script.LATIN,
            ),
            EvaluationPassage(
                passage_id="cyrillic-short",
                text="Љубичасти њивски цвет ђаку прича причу.",
                language=Language.SERBIAN,
                script=Script.CYRILLIC,
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            report = service.evaluate(_voice(), passages, output)

            self.assertEqual(report.evidence_kind, EvidenceKind.FAKE_FUNCTIONAL_ONLY)
            self.assertFalse(report.quality_evidence)
            self.assertIn("not evidence of naturalness", report.notice)
            self.assertEqual(report.created_at_utc, "2026-09-11T12:00:00Z")
            self.assertEqual(engine.load_count, 2)
            self.assertEqual(engine.synthesis_count, 2)
            self.assertEqual(len(report.samples), 2)
            self.assertGreaterEqual(report.load_metrics.cold_wall_seconds, 0)

            for sample in report.samples:
                wav_path = output / report.run_directory_name / sample.wav_filename
                self.assertTrue(wav_path.is_file())
                self.assertGreater(sample.audio_duration_seconds, 0)
                self.assertGreaterEqual(sample.generation_cpu_seconds, 0)
                self.assertGreaterEqual(sample.real_time_factor, 0)
                with wave.open(str(wav_path), "rb") as wav_file:
                    self.assertEqual(wav_file.getnchannels(), 1)
                    self.assertEqual(wav_file.getsampwidth(), 2)
                    self.assertEqual(wav_file.getframerate(), 16_000)

            report_document = json.loads(
                (output / report.run_directory_name / report.report_filename).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(report_document["evidence"]["kind"], "fake_functional_only")
            self.assertIs(report_document["evidence"]["quality_evidence"], False)
            self.assertIn("cold_wall_seconds", report_document["load"])
            self.assertIn("generation_cpu_seconds", report_document["samples"][0])
            self.assertIn("generation_cpu_percent", report_document["samples"][0])
            self.assertIn("real_time_factor", report_document["samples"][0])
            self.assertIsNone(report_document["samples"][0]["time_to_first_audio_seconds"])
            self.assertEqual(report_document["schema_version"], 3)
            self.assertEqual(report_document["samples"][0]["language"], "sr")
            self.assertIn(report_document["samples"][0]["script"], {"latin", "cyrillic"})
            self.assertEqual(
                report_document["voice"]["source"]["revision"], _voice().source_revision
            )
            self.assertEqual(report_document["voice"]["model"]["sha256"], "a" * 64)
            self.assertEqual(report_document["voice"]["config"]["sha256"], "b" * 64)
            self.assertEqual(report_document["voice"]["sample_rate_hz"], 16_000)
            self.assertEqual(len(report_document["voice"]["contract_sha256"]), 64)
            self.assertEqual(
                report_document["runtime"]["python_version"], platform.python_version()
            )
            self.assertFalse(any(path.name.startswith(".") for path in output.iterdir()))

    def test_evaluation_rejects_unsupported_passage_before_creating_artifacts(self) -> None:
        english_passage = EvaluationPassage(
            passage_id="wrong-language",
            text="This is not the registered language.",
            language=Language.ENGLISH,
            script=Script.LATIN,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with self.assertRaisesRegex(EvaluationError, "not supported"):
                VoiceEvaluationService(FakeSpeechEngine()).evaluate(
                    _voice(),
                    (english_passage,),
                    output,
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_invalid_engine_result_is_not_published(self) -> None:
        passage = EvaluationPassage(
            passage_id="invalid-audio",
            text="Valid text, invalid engine response.",
            language=Language.SERBIAN,
            script=Script.LATIN,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with self.assertRaisesRegex(EvaluationError, "invalid audio result"):
                VoiceEvaluationService(InvalidResultEngine()).evaluate(
                    _voice(),
                    (passage,),
                    output,
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_duplicate_passage_ids_are_rejected(self) -> None:
        passage = EvaluationPassage(
            passage_id="duplicate",
            text="Jedan odlomak.",
            language=Language.SERBIAN,
            script=Script.LATIN,
        )

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            self.assertRaisesRegex(EvaluationError, "unique"),
        ):
            VoiceEvaluationService(FakeSpeechEngine()).evaluate(
                _voice(),
                (passage, passage),
                Path(temporary_directory),
            )

    def test_second_passage_failure_publishes_no_partial_run(self) -> None:
        passages = (
            EvaluationPassage("first", "Prvi odlomak.", Language.SERBIAN, Script.LATIN),
            EvaluationPassage("second", "Drugi odlomak.", Language.SERBIAN, Script.LATIN),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with self.assertRaisesRegex(EvaluationError, "second"):
                VoiceEvaluationService(FailingSecondPassageEngine()).evaluate(
                    _voice(), passages, output
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_sample_rate_must_match_registered_voice(self) -> None:
        passage = EvaluationPassage(
            "wrong-rate",
            "Ovo je provera brzine uzorkovanja.",
            Language.SERBIAN,
            Script.LATIN,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            with self.assertRaisesRegex(EvaluationError, "sample rate"):
                VoiceEvaluationService(WrongSampleRateEngine()).evaluate(
                    _voice(), (passage,), output
                )
            self.assertEqual(list(output.iterdir()), [])

    def test_repeated_evaluations_publish_distinct_complete_runs(self) -> None:
        passage = EvaluationPassage(
            "rerun",
            "Ponovljiva tehnička provera.",
            Language.SERBIAN,
            Script.LATIN,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            service = VoiceEvaluationService(FakeSpeechEngine())
            first = service.evaluate(_voice(), (passage,), output)
            second = service.evaluate(_voice(), (passage,), output)

            self.assertNotEqual(first.run_id, second.run_id)
            self.assertTrue((output / first.run_id / first.report_filename).is_file())
            self.assertTrue((output / second.run_id / second.report_filename).is_file())


if __name__ == "__main__":
    unittest.main()
