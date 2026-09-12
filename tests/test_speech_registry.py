from __future__ import annotations

import tempfile
import textwrap
import unittest
from pathlib import Path

from article_reader.domain.speech import Language, Script
from article_reader.speech.registry import (
    DuplicateVoiceIdError,
    VoiceNotApprovedError,
    VoiceNotFoundError,
    VoiceRegistryError,
    load_voice_registry,
)


def _voice_table(
    *,
    voice_id: str = "en_US-test-medium",
    status: str = "candidate",
    engine: str = "piper",
) -> str:
    return textwrap.dedent(
        f'''\
        [[voices]]
        id = "{voice_id}"
        display_name = "English test voice"
        language = "en"
        scripts = ["latin"]
        engine = "{engine}"
        engine_version = "1.3.0"
        sample_rate_hz = 16000
        source_url = "https://models.example.com/voices/test"
        source_revision = "0123456789abcdef0123456789abcdef01234567"
        model_url = "https://models.example.com/voices/test/model.onnx"
        model_sha256 = "{"a" * 64}"
        config_url = "https://models.example.com/voices/test/model.onnx.json"
        config_sha256 = "{"b" * 64}"
        model_license_url = "https://models.example.com/licenses/model"
        data_license_url = "https://models.example.com/licenses/data"
        evaluation_status = "{status}"
        evaluation_notes = "Fixture only; no quality claim."
        '''
    )


class VoiceRegistryTests(unittest.TestCase):
    def _load(self, document: str):  # type: ignore[no-untyped-def]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "voices.toml"
            path.write_text(document, encoding="utf-8")
            return load_voice_registry(path)

    def test_accepts_intentionally_empty_registry(self) -> None:
        registry = self._load("schema_version = 1\nvoices = []\n")

        self.assertEqual(len(registry), 0)
        self.assertEqual(registry.voices, ())

    def test_loads_exact_valid_voice(self) -> None:
        registry = self._load("schema_version = 1\n" + _voice_table())

        voice = registry.get("en_US-test-medium")
        self.assertEqual(voice.language, Language.ENGLISH)
        self.assertEqual(voice.scripts, (Script.LATIN,))
        self.assertEqual(voice.model_sha256, "a" * 64)

    def test_rejects_duplicate_case_ambiguous_ids(self) -> None:
        document = (
            "schema_version = 1\n"
            + _voice_table(voice_id="en_US-test-medium")
            + _voice_table(voice_id="EN_us-TEST-medium")
        )

        with self.assertRaises(DuplicateVoiceIdError):
            self._load(document)

    def test_rejects_unknown_enum_url_revision_hash_and_key(self) -> None:
        valid = "schema_version = 1\n" + _voice_table()
        invalid_documents = {
            "language": valid.replace('language = "en"', 'language = "fr"'),
            "url": valid.replace(
                "https://models.example.com/voices/test", "http://localhost/test", 1
            ),
            "revision": valid.replace(
                'source_revision = "0123456789abcdef0123456789abcdef01234567"',
                'source_revision = "main"',
            ),
            "hash": valid.replace(f'model_sha256 = "{"a" * 64}"', 'model_sha256 = "abc"'),
            "key": valid.replace(
                'display_name = "English test voice"',
                'display_name = "English test voice"\ntyop = true',
            ),
        }

        for label, document in invalid_documents.items():
            with self.subTest(label=label), self.assertRaises(VoiceRegistryError):
                self._load(document)

    def test_evaluation_status_is_required_and_never_defaults_to_approved(self) -> None:
        document = "schema_version = 1\n" + _voice_table()
        document = document.replace('evaluation_status = "candidate"\n', "")

        with self.assertRaisesRegex(VoiceRegistryError, "evaluation_status"):
            self._load(document)

    def test_exact_lookup_never_substitutes_an_approved_voice(self) -> None:
        document = (
            "schema_version = 1\n"
            + _voice_table(voice_id="candidate", status="candidate")
            + _voice_table(voice_id="approved", status="approved")
        )
        registry = self._load(document)

        with self.assertRaises(VoiceNotApprovedError):
            registry.require_approved("candidate")
        with self.assertRaises(VoiceNotFoundError):
            registry.require_approved("missing")
        self.assertEqual(
            [voice.voice_id for voice in registry.approved_for(Language.ENGLISH, Script.LATIN)],
            ["approved"],
        )

    def test_approved_fake_voice_is_rejected(self) -> None:
        document = "schema_version = 1\n" + _voice_table(
            status="approved",
            engine="fake",
        )

        with self.assertRaisesRegex(VoiceRegistryError, "fake engine"):
            self._load(document)


if __name__ == "__main__":
    unittest.main()
