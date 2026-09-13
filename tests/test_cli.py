from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

import article_reader.cli as cli
from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import (
    ArticlePreparationResult,
    ArticlePreparationService,
    ArticlePreparationStatus,
)
from article_reader.application.services.diagnostics import (
    DiagnosticCheck,
    DiagnosticReport,
    DiagnosticStatus,
)
from article_reader.config import ServerSettings, Settings
from article_reader.domain.article import ArticleBlock, ArticleBlockKind, ExtractedArticle
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.speech import Language, VoiceEvaluationStatus
from article_reader.speech.registry import VoiceRegistry
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer


def test_voices_list_reports_the_packaged_catalogue_as_not_installed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    exit_code = cli.main(["--data-dir", str(data_directory), "voices", "list", "--json"])

    assert exit_code == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema_version"] == 1
    voice_ids = {voice["id"] for voice in document["voices"]}
    assert voice_ids == {"en_US-ljspeech-medium", "de_DE-thorsten-medium", "sr-marko-medium"}
    assert all(voice["installed"] is False for voice in document["voices"])
    assert all(voice["evaluation_status"] == "approved" for voice in document["voices"])
    assert not data_directory.exists(), "read-only catalogue listing must not create runtime paths"


def test_doctor_returns_failure_when_required_check_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = DiagnosticReport(
        checks=(
            DiagnosticCheck(
                name="audio_directory_writable",
                status=DiagnosticStatus.ERROR,
                value=False,
                detail="fixture failure",
            ),
        )
    )

    def collect_fixture(
        settings: Settings,
        *,
        model_probe: object,
    ) -> DiagnosticReport:
        assert settings.paths.data_dir == tmp_path
        assert callable(model_probe)
        return report

    monkeypatch.setattr(cli, "collect_diagnostics", collect_fixture)

    exit_code = cli.main(["--data-dir", str(tmp_path), "doctor", "--json"])

    assert exit_code == 1
    document = json.loads(capsys.readouterr().out)
    assert document["healthy"] is False
    assert document["ready"] is False


def test_explicit_fake_evaluation_writes_non_quality_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    output_directory = tmp_path / "evaluation"

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--language",
            "sr",
            "--output",
            str(output_directory),
            "--json",
        ]
    )

    assert exit_code == 0
    printed_report = json.loads(capsys.readouterr().out)
    assert printed_report["evidence"]["quality_evidence"] is False
    assert printed_report["evidence"]["kind"] == "fake_functional_only"
    assert len(printed_report["samples"]) == 2
    run_directory = output_directory / printed_report["run_id"]
    report_path = run_directory / "development-fake-sr--evaluation.json"
    assert report_path.is_file()
    assert len(list(run_directory.glob("*.wav"))) == 2
    persisted_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted_report["voice"]["voice_id"] == "development-fake-sr"


def test_real_evaluation_without_voice_id_is_a_usage_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    exit_code = cli.main(["--data-dir", str(data_directory), "evaluate-voices"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "requires --voice-id" in captured.err.lower()
    assert not data_directory.exists()


def test_real_evaluation_of_an_uninstalled_registry_voice_is_unavailable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--voice-id",
            "en_US-ljspeech-medium",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "is not installed" in captured.err.lower()


def test_voices_install_rejects_non_approved_voice_without_flag(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_directory = tmp_path / "data"
    packaged = cli._packaged_registry()
    candidate = replace(
        packaged.get("en_US-ljspeech-medium"),
        evaluation_status=VoiceEvaluationStatus.CANDIDATE,
        evaluation_notes="Synthetic candidate used to test the explicit evaluation gate.",
    )
    monkeypatch.setattr(
        cli,
        "_packaged_registry",
        lambda: VoiceRegistry(schema_version=1, voices=(candidate,)),
    )

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "voices",
            "install",
            "en_US-ljspeech-medium",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "not approved" in captured.err.lower()
    assert "--for-evaluation" in captured.err
    assert not data_directory.exists()


def test_long_form_text_file_requires_voice_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    text_file = tmp_path / "article.txt"
    text_file.write_text("Some long-form article text.", encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--text-file",
            str(text_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--text-file requires --voice-id" in captured.err
    assert not data_directory.exists()


def test_long_form_text_file_with_fake_engine_produces_a_real_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    output_directory = tmp_path / "evaluation"
    text_file = tmp_path / "article.txt"
    long_text = "First sentence of the article. " * 20 + "\n\nSecond paragraph follows here."
    # write_bytes avoids platform newline translation so the character count matches exactly.
    text_file.write_bytes(long_text.encode("utf-8"))

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--voice-id",
            "en_US-ljspeech-medium",
            "--text-file",
            str(text_file),
            "--output",
            str(output_directory),
            "--json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert len(report["samples"]) == 1
    sample = report["samples"][0]
    assert sample["passage_id"] == "long-form"
    assert sample["language"] == "en"
    assert sample["script"] == "latin"
    assert sample["text_characters"] == len(long_text)
    assert len(sample["text_sha256"]) == 64
    assert report["voice"]["voice_id"] == "en_US-ljspeech-medium"
    assert report["schema_version"] == 3
    run_directory = output_directory / report["run_id"]
    assert (run_directory / sample["wav_filename"]).is_file()


def test_long_form_text_file_requires_explicit_script_for_multiscript_voice(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    text_file = tmp_path / "article.txt"
    text_file.write_text("Neki tekst za proveru.", encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--voice-id",
            "sr-marko-medium",
            "--text-file",
            str(text_file),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "supports multiple scripts" in captured.err.lower()


def test_long_form_text_file_accepts_explicit_script_for_multiscript_voice(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    text_file = tmp_path / "article.txt"
    text_file.write_text("Ово је ћирилични текст за проверу.", encoding="utf-8")  # noqa: RUF001

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--voice-id",
            "sr-marko-medium",
            "--text-file",
            str(text_file),
            "--script",
            "cyrillic",
            "--json",
        ]
    )

    assert exit_code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["samples"][0]["script"] == "cyrillic"


def test_long_form_text_file_missing_is_a_controlled_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "evaluate-voices",
            "--fake",
            "--voice-id",
            "en_US-ljspeech-medium",
            "--text-file",
            str(tmp_path / "missing.txt"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "does not exist" in captured.err.lower()


def test_prepare_text_english_prints_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"
    text_file = tmp_path / "article.txt"
    text_file.write_text("First sentence. Second sentence.", encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "prepare-text",
            str(text_file),
            "--language",
            "en",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "segment(s)" in captured.out
    assert "sha256" in captured.out
    assert not data_directory.exists()


def test_prepare_text_json_manifest_round_trips_source_spans(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text_file = tmp_path / "article.txt"
    content = "First sentence. Second sentence."
    text_file.write_text(content, encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "prepare-text",
            str(text_file),
            "--language",
            "en",
            "--json",
        ]
    )

    assert exit_code == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["original"]["character_count"] == len(content)
    assert manifest["original"]["language"] == "en"
    assert manifest["original"]["script"] == "latin"
    for segment in manifest["segments"]:
        for span in segment["source_spans"]:
            assert content[span["start"] : span["end"]]
        assert segment["speech_text"]
        assert len(segment["speech_text_sha256"]) == 64


def test_prepare_text_serbian_requires_explicit_script(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text_file = tmp_path / "article.txt"
    text_file.write_text("Neki tekst.", encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "prepare-text",
            str(text_file),
            "--language",
            "sr",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "--script" in captured.err


def test_prepare_text_english_rejects_cyrillic_script(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text_file = tmp_path / "article.txt"
    text_file.write_text("Some text.", encoding="utf-8")

    exit_code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "prepare-text",
            str(text_file),
            "--language",
            "en",
            "--script",
            "cyrillic",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "latin" in captured.err.lower()


def test_prepare_text_writes_manifest_to_output_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    text_file = tmp_path / "article.txt"
    text_file.write_text("First sentence. Second sentence.", encoding="utf-8")
    output_file = tmp_path / "manifest.json"

    exit_code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "prepare-text",
            str(text_file),
            "--language",
            "en",
            "--output",
            str(output_file),
        ]
    )

    assert exit_code == 0
    manifest = json.loads(output_file.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert "manifest written to" in capsys.readouterr().out


def test_prepare_text_missing_file_is_a_controlled_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "prepare-text",
            str(tmp_path / "missing.txt"),
            "--language",
            "en",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "does not exist" in captured.err.lower()


def test_voices_install_rejects_unknown_voice_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_directory = tmp_path / "data"

    exit_code = cli.main(
        [
            "--data-dir",
            str(data_directory),
            "voices",
            "install",
            "--for-evaluation",
            "not-a-real-voice-id",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "not present in the registry" in captured.err.lower()


def _article_result() -> ArticleIngestionResult:
    page = FetchedPage(
        submitted_url="https://example.com/story#part",
        final_url="https://www.example.com/story",
        redirect_chain=("https://example.com/story", "https://www.example.com/story"),
        status_code=200,
        media_type="text/html",
        body=b"<html><body><article><p>Useful extracted paragraph.</p></article></body></html>",
        response_bytes=82,
    )
    article = ExtractedArticle(
        submitted_url=page.submitted_url,
        final_url=page.final_url,
        canonical_url="https://www.example.com/canonical",
        title="Fixture article",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="Useful extracted paragraph.",
                speech_text="Useful extracted paragraph.",
            ),
        ),
        review_reasons=(),
        extraction_version="fixture-v1",
    )
    return ArticleIngestionResult(page=page, article=article)


def test_fetch_article_prints_and_atomically_writes_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _article_result()

    class _Service:
        def ingest(self, submitted_url: str) -> ArticleIngestionResult:
            assert submitted_url == result.page.submitted_url
            return result

    monkeypatch.setattr(cli, "_article_ingestion_service", lambda settings: _Service())
    output = tmp_path / "nested" / "article.json"

    exit_code = cli.main(
        [
            "fetch-article",
            result.page.submitted_url,
            "--output",
            str(output),
            "--json",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert printed == persisted
    assert printed["article"]["title"] == "Fixture article"
    assert printed["source"]["body_sha256"] == result.page.body_sha256
    assert not list(output.parent.glob("*.tmp"))


def test_fetch_article_rejects_non_http_url_without_network(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["fetch-article", "file:///private/article.html"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "FETCH_INVALID_URL" in captured.err


def _language_detection(*candidates: tuple[str, float]) -> LanguageDetection:
    return LanguageDetection(
        candidates=tuple(LanguageCandidate(code, confidence) for code, confidence in candidates),
        detector_version="fixture-detector-v1",
        sample_character_count=300,
        sample_alphabetic_count=250,
        sample_sha256=hashlib.sha256(b"fixture").hexdigest(),
    )


class _PreparationIngestor:
    def __init__(self, result: ArticleIngestionResult) -> None:
        self.result = result

    def ingest(self, submitted_url: str) -> ArticleIngestionResult:
        assert submitted_url == self.result.page.submitted_url
        return self.result


class _PreparationDetector:
    def __init__(self, detection: LanguageDetection) -> None:
        self.detection = detection

    def detect(self, text: str) -> LanguageDetection:
        assert text
        return self.detection


def _preparation_result(
    detection: LanguageDetection,
    *,
    language: str = "auto",
) -> ArticlePreparationResult:
    service = ArticlePreparationService(
        _PreparationIngestor(_article_result()),
        _PreparationDetector(detection),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=600,
    )
    requested = None if language == "auto" else Language(language)
    return service.prepare(
        _article_result().page.submitted_url,
        requested_language=requested,
    )


def test_prepare_article_writes_ready_traceable_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preparation_result(_language_detection(("en", 0.98), ("de", 0.01)))

    class _Service:
        def prepare(self, submitted_url: str, **kwargs: object) -> ArticlePreparationResult:
            assert submitted_url == result.ingestion.page.submitted_url
            assert kwargs["requested_language"] is None
            return result

    monkeypatch.setattr(cli, "_article_preparation_service", lambda settings: _Service())
    output = tmp_path / "nested" / "prepared-article.json"

    exit_code = cli.main(
        [
            "prepare-article",
            result.ingestion.page.submitted_url,
            "--output",
            str(output),
            "--json",
        ]
    )

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert printed == persisted
    assert printed["status"] == "ready"
    assert printed["language"]["selected_language"] == "en"
    assert printed["preparation"]["segments"][0]["includes_title"] is True
    assert printed["preparation"]["segments"][0]["source_block_ordinals"] == [0]
    assert not list(output.parent.glob("*.tmp"))


def test_prepare_article_returns_unavailable_with_actionable_language_state(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _preparation_result(_language_detection(("bs", 0.44), ("hr", 0.39), ("sr", 0.16)))
    assert result.status is ArticlePreparationStatus.NEEDS_LANGUAGE_OVERRIDE

    class _Service:
        def prepare(self, submitted_url: str, **kwargs: object) -> ArticlePreparationResult:
            del submitted_url, kwargs
            return result

    monkeypatch.setattr(cli, "_article_preparation_service", lambda settings: _Service())

    exit_code = cli.main(["prepare-article", result.ingestion.page.submitted_url])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "needs_language_override" in captured.out
    assert "--language en|de|sr" in captured.out


def test_serve_bind_requires_explicit_lan_flag(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["serve", "--bind", "192.168.1.20", "--no-open"])

    assert exit_code == 2
    assert "requires explicit --lan" in capsys.readouterr().err


def test_configured_lan_mode_still_requires_launch_opt_in(
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = cli.build_parser().parse_args(["serve", "--no-open"])
    settings = Settings(server=ServerSettings(bind="0.0.0.0", lan_mode=True))

    assert cli._run_serve(arguments, settings) == 3
    assert "requires explicit 'serve --lan'" in capsys.readouterr().err


def test_ctrl_c_is_a_clean_successful_stop(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(arguments: object, settings: object) -> int:
        del arguments, settings
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run_serve", interrupt)

    assert cli.main(["serve", "--no-open"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "Article Reader stopped.\n"
    assert captured.err == ""
