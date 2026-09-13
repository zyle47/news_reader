"""Command-line composition root for Article Reader."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
from collections.abc import Mapping, Sequence
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from article_reader.db.connection import Database
    from article_reader.worker.loop import WorkerLoop

from article_reader import __version__
from article_reader.application.ports.speech import SpeechEngine
from article_reader.application.services.article_ingestion import (
    ArticleIngestionResult,
    ArticleIngestionService,
)
from article_reader.application.services.article_preparation import (
    ArticlePreparationResult,
    ArticlePreparationService,
    ArticlePreparationStatus,
)
from article_reader.application.services.diagnostics import (
    DiagnosticReport,
    DiagnosticStatus,
    ModelAvailability,
    ModelAvailabilityProbe,
    collect_diagnostics,
)
from article_reader.application.services.language_selection import LanguageSelectionError
from article_reader.application.services.voice_evaluation import (
    EvaluationError,
    VoiceEvaluationService,
)
from article_reader.config import ConfigError, Settings, ensure_runtime_dirs, load_settings
from article_reader.domain.speech import (
    EvaluationPassage,
    Language,
    Script,
    VoiceEvaluationStatus,
    VoiceSpec,
)
from article_reader.domain.speech import (
    SpeechSettings as SynthesisSettings,
)
from article_reader.domain.text import OriginalText, PreparedText, TextDomainError
from article_reader.extract.trafilatura_adapter import (
    ArticleExtractionError,
    TrafilaturaArticleExtractor,
)
from article_reader.fetch.safe_http import (
    ArticleFetchError,
    FetchErrorCode,
    SafeHttpArticleFetcher,
)
from article_reader.speech.evaluation_corpus import (
    EvaluationCorpus,
    EvaluationCorpusError,
    load_evaluation_corpus,
)
from article_reader.speech.fake import FakeSpeechEngine
from article_reader.speech.installer import (
    UrllibArtifactDownloader,
    VoiceInstaller,
    VoiceInstallError,
)
from article_reader.speech.model_store import ModelStore
from article_reader.speech.piper_engine import PiperEngine
from article_reader.speech.registry import (
    VoiceRegistry,
    VoiceRegistryError,
    load_voice_registry,
)
from article_reader.text.loader import TextInputError, load_bounded_utf8_text_file
from article_reader.text.py3langid_detector import LanguageDetectorError, Py3LangidDetector
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer, prepare_text

_EXIT_OK = 0
_EXIT_FAILED = 1
_EXIT_INVALID = 2
_EXIT_UNAVAILABLE = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="article-reader",
        description="Local-first article reading service administration.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        help="TOML configuration file (defaults to ./config.toml when present).",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Explicit machine-local runtime directory; overrides file and environment settings.",
    )

    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Inspect this host without uploading diagnostics.")
    doctor.add_argument("--json", action="store_true", help="Emit stable JSON output.")

    voices = commands.add_parser("voices", help="Inspect the checksummed voice catalogue.")
    voice_commands = voices.add_subparsers(dest="voices_command", required=True)
    voices_list = voice_commands.add_parser("list", help="List catalogue and installation state.")
    voices_list.add_argument("--json", action="store_true", help="Emit stable JSON output.")

    voices_install = voice_commands.add_parser(
        "install",
        help="Download and SHA-256-verify one exact catalogue voice's artifacts.",
    )
    voices_install.add_argument("voice_id", help="Exact catalogue voice ID.")
    voices_install.add_argument(
        "--for-evaluation",
        action="store_true",
        help="Required to install a voice that is not yet human-approved.",
    )
    voices_install.add_argument("--json", action="store_true", help="Emit stable JSON output.")

    evaluate = commands.add_parser(
        "evaluate-voices",
        help="Generate technical voice samples and a benchmark report.",
    )
    evaluate.add_argument(
        "--voice-id",
        help="Exact catalogue voice ID. No language fallback or substitution is performed.",
    )
    evaluate.add_argument(
        "--fake",
        action="store_true",
        help="Use the deterministic test engine; output is never voice-quality evidence.",
    )
    evaluate.add_argument(
        "--language",
        choices=[language.value for language in Language],
        default=Language.ENGLISH.value,
        help="Language for the explicit fake smoke evaluation (default: en).",
    )
    evaluate.add_argument(
        "--output",
        type=Path,
        help="Output directory (defaults beneath the configured data directory).",
    )
    evaluate.add_argument(
        "--text-file",
        type=Path,
        help=(
            "A local UTF-8 text file for a long-form evaluation passage. "
            "Requires --voice-id; never committed to the repository."
        ),
    )
    evaluate.add_argument(
        "--script",
        choices=[script.value for script in Script],
        help="Script of --text-file's content; required when the voice supports more than one.",
    )
    evaluate.add_argument("--json", action="store_true", help="Print the report as JSON.")

    prepare = commands.add_parser(
        "prepare-text",
        help="Normalize and segment a local UTF-8 text file into a speech-ready manifest.",
    )
    prepare.add_argument("text_file", type=Path, help="Local UTF-8 text file to prepare.")
    prepare.add_argument(
        "--language",
        required=True,
        choices=[language.value for language in Language],
        help="Language of the supplied text.",
    )
    prepare.add_argument(
        "--script",
        choices=[script.value for script in Script],
        help="Script of the supplied text; required for Serbian, ignored otherwise.",
    )
    prepare.add_argument("--output", type=Path, help="Also write the JSON manifest to this file.")
    prepare.add_argument("--json", action="store_true", help="Print the full JSON manifest.")

    fetch_article = commands.add_parser(
        "fetch-article",
        help="Safely retrieve one public HTML page and extract an article preview.",
    )
    fetch_article.add_argument("url", help="Public HTTP(S) article URL.")
    fetch_article.add_argument(
        "--output",
        type=Path,
        help="Atomically write the full extracted-article JSON manifest.",
    )
    fetch_article.add_argument("--json", action="store_true", help="Print the full JSON manifest.")

    prepare_article = commands.add_parser(
        "prepare-article",
        help="Fetch, review, select language, and prepare a public article for speech.",
    )
    prepare_article.add_argument("url", help="Public HTTP(S) article URL.")
    prepare_article.add_argument(
        "--language",
        choices=["auto", *(language.value for language in Language)],
        default="auto",
        help="Language override, or conservative local detection (default: auto).",
    )
    prepare_article.add_argument(
        "--script",
        choices=[script.value for script in Script],
        help="Required with --language sr; omit for auto, English, or German.",
    )
    prepare_article.add_argument(
        "--accept-review",
        action="store_true",
        help="Confirm that review warnings and any omitted unspeakable blocks were inspected.",
    )
    prepare_article.add_argument(
        "--output",
        type=Path,
        help="Atomically write the full article-preparation JSON manifest.",
    )
    prepare_article.add_argument(
        "--json", action="store_true", help="Print the full JSON manifest."
    )

    serve = commands.add_parser(
        "serve",
        help="Run the browser reader (loopback-only unless --lan is explicit).",
    )
    serve.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the reader in the default browser automatically.",
    )
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Also listen on one private LAN interface and require phone pairing.",
    )
    serve.add_argument(
        "--bind",
        metavar="PRIVATE_IP",
        help="Private LAN address to use with --lan (default: safely auto-detect).",
    )

    return parser


def _load_cli_settings(arguments: argparse.Namespace) -> Settings:
    explicit_config = arguments.config
    default_config = Path.cwd() / "config.toml"
    config_file = explicit_config
    if config_file is None and default_config.is_file():
        config_file = default_config
    overrides: dict[str, object] = {"data_dir": arguments.data_dir}
    if arguments.command == "serve":
        if arguments.bind is not None and not arguments.lan:
            raise ConfigError("serve --bind requires explicit --lan")
        if arguments.lan:
            overrides["server.lan_mode"] = True
    return load_settings(
        config_file,
        overrides=overrides,
    )


def _packaged_registry() -> VoiceRegistry:
    registry_resource = resources.files("article_reader.resources").joinpath("voices.toml")
    with resources.as_file(registry_resource) as registry_path:
        return load_voice_registry(registry_path)


def _packaged_smoke_corpus() -> EvaluationCorpus:
    corpus_resource = resources.files("article_reader.resources").joinpath("evaluation_smoke.toml")
    with resources.as_file(corpus_resource) as corpus_path:
        return load_evaluation_corpus(corpus_path)


def _model_probe(registry: VoiceRegistry) -> ModelAvailabilityProbe:
    def inspect(models_dir: Path) -> ModelAvailability:
        store = ModelStore(models_dir)
        installed_voices = tuple(voice for voice in registry if store.inspect(voice).installed)
        installed = tuple(voice.voice_id for voice in installed_voices)
        approved = tuple(
            voice
            for voice in installed_voices
            if voice.evaluation_status is VoiceEvaluationStatus.APPROVED
        )
        required_coverage = (
            (Language.ENGLISH, Script.LATIN),
            (Language.GERMAN, Script.LATIN),
            (Language.SERBIAN, Script.LATIN),
            (Language.SERBIAN, Script.CYRILLIC),
        )
        missing = tuple(
            f"{language.value}/{script.value}"
            for language, script in required_coverage
            if not any(voice.supports(language, script) for voice in approved)
        )
        if not missing:
            return ModelAvailability(
                status=DiagnosticStatus.OK,
                installed_voice_ids=installed,
                detail="Approved installed voices cover English, German, and both Serbian scripts.",
            )
        if not registry.voices:
            return ModelAvailability(
                status=DiagnosticStatus.WARNING,
                detail="The verified voice catalogue is empty; no model has been approved yet.",
            )
        return ModelAvailability(
            status=DiagnosticStatus.WARNING,
            installed_voice_ids=installed,
            detail=(
                "Approved installed voice coverage is incomplete; missing: "
                + ", ".join(missing)
                + ". Candidate or unapproved voices never satisfy readiness."
            ),
        )

    return inspect


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("byte formatter exhausted its fixed unit list")


def _display_value(name: str, value: object) -> str:
    if value is None:
        return "unknown"
    if name in {"total_ram_bytes", "free_disk_bytes"} and isinstance(value, int):
        return _format_bytes(value)
    if isinstance(value, tuple):
        return ", ".join(value) if value else "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _print_diagnostics(report: DiagnosticReport) -> None:
    for check in report.checks:
        displayed_value = _display_value(check.name, check.value)
        print(f"[{check.status.value.upper():7}] {check.name}: {displayed_value}")
        if check.detail:
            print(f"          {check.detail}")
    if report.ready:
        print("Result: ready")
    elif report.healthy:
        print("Result: host checks passed, but speech setup is not ready")
    else:
        print("Result: action required")


def _run_doctor(arguments: argparse.Namespace, settings: Settings) -> int:
    ensure_runtime_dirs(settings)
    registry = _packaged_registry()
    report = collect_diagnostics(settings, model_probe=_model_probe(registry))
    if arguments.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_diagnostics(report)
    return _EXIT_OK if report.ready else _EXIT_FAILED


def _voice_document(voice: VoiceSpec, store: ModelStore) -> dict[str, object]:
    installation = store.inspect(voice)
    return {
        "id": voice.voice_id,
        "display_name": voice.display_name,
        "language": voice.language.value,
        "scripts": [script.value for script in voice.scripts],
        "engine": voice.engine,
        "engine_version": voice.engine_version,
        "evaluation_status": voice.evaluation_status.value,
        "installed": installation.installed,
        "artifacts": [
            {
                "kind": artifact.kind,
                "filename": artifact.filename,
                "state": artifact.state.value,
            }
            for artifact in installation.artifacts
        ],
    }


def _run_voices_list(arguments: argparse.Namespace, settings: Settings) -> int:
    registry = _packaged_registry()
    store = ModelStore(settings.paths.models_dir)
    documents = [_voice_document(voice, store) for voice in registry]
    if arguments.json:
        print(
            json.dumps(
                {"schema_version": registry.schema_version, "voices": documents},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    elif not documents:
        print("No verified voice releases are registered yet.")
        print(
            "Voice installation remains disabled until revisions, digests, and licenses are pinned."
        )
    else:
        for voice in registry:
            scripts = ",".join(script.value for script in voice.scripts)
            installed = "installed" if store.inspect(voice).installed else "not installed"
            print(
                f"{voice.voice_id}  {voice.language.value}/{scripts}  "
                f"{voice.evaluation_status.value}  {installed}"
            )
    return _EXIT_OK


def _run_voices_install(arguments: argparse.Namespace, settings: Settings) -> int:
    registry = _packaged_registry()
    voice = registry.get(arguments.voice_id)
    if (
        voice.evaluation_status is not VoiceEvaluationStatus.APPROVED
        and not arguments.for_evaluation
    ):
        print(
            f"error: voice {voice.voice_id!r} is {voice.evaluation_status.value}, not approved. "
            "Pass --for-evaluation to install a non-approved voice for evaluation only.",
            file=sys.stderr,
        )
        return _EXIT_UNAVAILABLE

    ensure_runtime_dirs(settings)
    model_store = ModelStore(settings.paths.models_dir)
    downloader = UrllibArtifactDownloader(
        connect_timeout_seconds=settings.speech.model_download_connect_timeout_seconds,
        overall_timeout_seconds=settings.speech.model_download_overall_timeout_seconds,
    )
    installer = VoiceInstaller(
        model_store,
        downloader,
        max_bytes=settings.speech.max_model_download_bytes,
    )
    report = installer.install(voice)

    if arguments.json:
        print(
            json.dumps(
                {
                    "voice_id": report.voice_id,
                    "already_installed": report.already_installed,
                    "artifacts": [
                        {
                            "kind": artifact.kind,
                            "filename": artifact.filename,
                            "already_installed": artifact.already_installed,
                            "byte_count": artifact.byte_count,
                        }
                        for artifact in report.artifacts
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for artifact in report.artifacts:
            state = "already installed" if artifact.already_installed else "downloaded"
            print(f"{artifact.kind}: {artifact.filename} ({state}, {artifact.byte_count} bytes)")
        print(f"voice {voice.voice_id!r} is installed: {model_store.inspect(voice).installed}")
    return _EXIT_OK


def _development_fake_voice(language: Language) -> VoiceSpec:
    scripts = (Script.LATIN, Script.CYRILLIC) if language is Language.SERBIAN else (Script.LATIN,)
    fixture_digest = hashlib.sha256(f"development-fake-{language.value}".encode()).hexdigest()
    return VoiceSpec(
        voice_id=f"development-fake-{language.value}",
        display_name=f"Development fake ({language.value})",
        language=language,
        scripts=scripts,
        engine="fake",
        engine_version="1.0.0",
        sample_rate_hz=16_000,
        source_url="https://example.invalid/article-reader/development-fake",
        source_revision="fixture-v1",
        model_url="https://example.invalid/article-reader/development-fake.onnx",
        model_sha256=fixture_digest,
        config_url="https://example.invalid/article-reader/development-fake.onnx.json",
        config_sha256=fixture_digest,
        model_license_url="https://example.invalid/article-reader/not-a-real-model",
        data_license_url="https://example.invalid/article-reader/not-a-real-dataset",
        evaluation_status=VoiceEvaluationStatus.UNVERIFIED,
        evaluation_notes="Synthetic CLI smoke fixture; not a model or voice-quality candidate.",
    )


def _absolute_output_path(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _resolve_passage_script(voice: VoiceSpec, requested: str | None) -> Script:
    if requested is not None:
        script = Script(requested)
        if script not in voice.scripts:
            raise EvaluationCorpusError(
                f"voice {voice.voice_id!r} does not support script {script.value!r}"
            )
        return script
    if len(voice.scripts) == 1:
        return voice.scripts[0]
    raise EvaluationCorpusError(
        f"voice {voice.voice_id!r} supports multiple scripts "
        f"({', '.join(script.value for script in voice.scripts)}); pass --script explicitly"
    )


def _run_evaluate(arguments: argparse.Namespace, settings: Settings) -> int:
    registry = _packaged_registry()
    engine: SpeechEngine

    if arguments.text_file is not None and not arguments.voice_id:
        print(
            "error: --text-file requires --voice-id <exact catalogue id>; "
            "there is no default real voice.",
            file=sys.stderr,
        )
        return _EXIT_INVALID

    if arguments.fake:
        voice = (
            registry.get(arguments.voice_id)
            if arguments.voice_id
            else _development_fake_voice(Language(arguments.language))
        )
        engine = FakeSpeechEngine()
    else:
        if not arguments.voice_id:
            print(
                "error: real evaluation requires --voice-id <exact catalogue id>; "
                "there is no default real voice. Use --fake for an offline plumbing check.",
                file=sys.stderr,
            )
            return _EXIT_INVALID
        voice = registry.get(arguments.voice_id)
        if voice.engine.casefold() != "piper":
            print(
                f"error: no real speech adapter is implemented for engine {voice.engine!r}",
                file=sys.stderr,
            )
            return _EXIT_UNAVAILABLE
        ensure_runtime_dirs(settings)
        model_store = ModelStore(settings.paths.models_dir)
        if not model_store.inspect(voice).installed:
            evaluation_flag = (
                ""
                if voice.evaluation_status is VoiceEvaluationStatus.APPROVED
                else " --for-evaluation"
            )
            print(
                f"error: voice {voice.voice_id!r} is not installed; run "
                f"'article-reader voices install{evaluation_flag} {voice.voice_id}' first.",
                file=sys.stderr,
            )
            return _EXIT_UNAVAILABLE
        engine = PiperEngine(model_store)

    passages: tuple[EvaluationPassage, ...]
    if arguments.text_file is not None:
        script = _resolve_passage_script(voice, arguments.script)
        text = load_bounded_utf8_text_file(
            _absolute_output_path(arguments.text_file),
            max_characters=settings.fetch.max_article_characters,
        )
        passages = (
            EvaluationPassage(
                passage_id="long-form",
                # A trailing newline is a near-universal text-file convention, not
                # meaningful article content; EvaluationPassage requires exact,
                # unpadded text. OriginalText (prepare-text) has no such
                # requirement, so the file's exact bytes are preserved there.
                text=text.strip(),
                language=voice.language,
                script=script,
            ),
        )
    else:
        corpus = _packaged_smoke_corpus()
        passages = corpus.compatible_with(voice.language, voice.scripts)
        if not passages:
            raise EvaluationCorpusError(
                f"the smoke corpus has no passage compatible with voice {voice.voice_id!r}"
            )

    ensure_runtime_dirs(settings)
    output_directory = (
        _absolute_output_path(arguments.output)
        if arguments.output is not None
        else settings.paths.data_dir / "evaluations" / voice.voice_id
    )
    report = VoiceEvaluationService(engine).evaluate(
        voice,
        passages,
        output_directory,
        SynthesisSettings(),
    )
    if arguments.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Wrote {len(report.samples)} sample(s) and {report.report_filename}")
        print(f"Output: {output_directory / report.run_directory_name}")
        print(report.notice)
    return _EXIT_OK


def _resolve_text_script(language: Language, requested: str | None) -> Script | None:
    if language is Language.SERBIAN:
        if requested is None:
            return None
        return Script(requested)
    if requested is not None and Script(requested) is not Script.LATIN:
        return None
    return Script.LATIN


def _prepared_text_manifest(prepared: PreparedText) -> dict[str, object]:
    return {
        "schema_version": 1,
        "original": {
            "sha256": prepared.original.sha256,
            "character_count": prepared.original.character_count,
            "language": prepared.original.language.value,
            "script": prepared.original.script.value,
        },
        "normalizer_version": prepared.normalizer_version,
        "segmenter_version": prepared.segmenter_version,
        "segments": [
            {
                "ordinal": segment.ordinal,
                "source_spans": [
                    {"start": span.start, "end": span.end} for span in segment.source_spans
                ],
                "speech_text": segment.speech_text,
                "speech_text_sha256": segment.speech_text_sha256,
                "character_count": segment.character_count,
            }
            for segment in prepared.segments
        ],
    }


def _run_prepare_text(arguments: argparse.Namespace, settings: Settings) -> int:
    language = Language(arguments.language)
    if language is Language.SERBIAN and arguments.script is None:
        print(
            "error: --script {latin,cyrillic} is required when --language sr.",
            file=sys.stderr,
        )
        return _EXIT_INVALID
    script = _resolve_text_script(language, arguments.script)
    if script is None:
        print(
            f"error: {language.value} text must be declared as Latin script.",
            file=sys.stderr,
        )
        return _EXIT_INVALID

    text = load_bounded_utf8_text_file(
        _absolute_output_path(arguments.text_file),
        max_characters=settings.fetch.max_article_characters,
    )
    original = OriginalText(text=text, language=language, script=script)
    prepared = prepare_text(original, max_segment_characters=settings.speech.max_chunk_characters)
    manifest = _prepared_text_manifest(prepared)

    if arguments.output is not None:
        _write_json_atomically(arguments.output, manifest)

    if arguments.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            f"{len(prepared.segments)} segment(s); "
            f"{prepared.original.character_count} original character(s); "
            f"{prepared.total_speech_characters} speech character(s)"
        )
        print(f"original sha256: {prepared.original.sha256}")
        if arguments.output is not None:
            print(f"manifest written to: {_absolute_output_path(arguments.output)}")
    return _EXIT_OK


def _article_ingestion_service(settings: Settings) -> ArticleIngestionService:
    fetcher = SafeHttpArticleFetcher(
        connect_timeout_seconds=settings.fetch.connect_timeout_seconds,
        overall_timeout_seconds=settings.fetch.overall_timeout_seconds,
        max_redirects=settings.fetch.max_redirects,
        max_response_bytes=settings.fetch.max_response_bytes,
        max_decoded_bytes=settings.fetch.max_decoded_bytes,
        max_url_characters=settings.fetch.max_url_characters,
    )
    extractor = TrafilaturaArticleExtractor(
        max_article_characters=settings.fetch.max_article_characters
    )
    return ArticleIngestionService(fetcher, extractor)


def _article_manifest(result: ArticleIngestionResult) -> dict[str, object]:
    page = result.page
    article = result.article
    return {
        "schema_version": 1,
        "source": {
            "submitted_url": article.submitted_url,
            "final_url": article.final_url,
            "canonical_url": article.canonical_url,
            "redirect_chain": list(page.redirect_chain),
            "status_code": page.status_code,
            "media_type": page.media_type,
            "response_bytes": page.response_bytes,
            "decoded_bytes": len(page.body),
            "body_sha256": page.body_sha256,
        },
        "article": {
            "title": article.title,
            "language_hint": article.language_hint,
            "extraction_version": article.extraction_version,
            "needs_review": article.needs_review,
            "review_reasons": [reason.value for reason in article.review_reasons],
            "blocks": [
                {
                    "ordinal": block.ordinal,
                    "kind": block.kind.value,
                    "display_text": block.display_text,
                    "speech_text": block.speech_text,
                    "text_sha256": block.text_sha256,
                    "requires_review": block.requires_review,
                }
                for block in article.blocks
            ],
        },
    }


def _write_json_atomically(path: Path, document: Mapping[str, object]) -> None:
    destination = _absolute_output_path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _run_fetch_article(arguments: argparse.Namespace, settings: Settings) -> int:
    result = _article_ingestion_service(settings).ingest(arguments.url)
    manifest = _article_manifest(result)
    if arguments.output is not None:
        _write_json_atomically(arguments.output, manifest)
    if arguments.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        article = result.article
        print(f"Title: {article.title or '(missing)'}")
        print(f"Final URL: {article.final_url}")
        print(f"Blocks: {len(article.blocks)}")
        if article.needs_review:
            reasons = ", ".join(reason.value for reason in article.review_reasons)
            print(f"Review required: {reasons or 'block-specific review'}")
        else:
            print("Review required: no")
        if arguments.output is not None:
            print(f"Manifest written to: {_absolute_output_path(arguments.output)}")
    return _EXIT_OK


def _article_preparation_service(settings: Settings) -> ArticlePreparationService:
    return ArticlePreparationService(
        _article_ingestion_service(settings),
        Py3LangidDetector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=settings.speech.max_chunk_characters,
    )


def _article_preparation_manifest(result: ArticlePreparationResult) -> dict[str, object]:
    article_document = _article_manifest(result.ingestion)
    selection = result.language_selection
    detection = result.language_detection
    script_detection = result.script_detection
    detector_evidence: dict[str, object] | None = None
    if detection is not None:
        detector_evidence = {
            "detector_version": detection.detector_version,
            "sample_character_count": detection.sample_character_count,
            "sample_alphabetic_count": detection.sample_alphabetic_count,
            "sample_sha256": detection.sample_sha256,
            "confidence_margin": detection.confidence_margin,
            "candidates": [
                {"code": candidate.code, "confidence": candidate.confidence}
                for candidate in detection.candidates
            ],
        }
    preparation: dict[str, object] | None = None
    if result.prepared_article is not None:
        prepared_article = result.prepared_article
        prepared_text = prepared_article.prepared_text
        block_mappings = prepared_article.block_source_spans
        title_span = prepared_article.title_source_span
        prepared_manifest = _prepared_text_manifest(prepared_text)
        segment_documents = prepared_manifest["segments"]
        assert isinstance(segment_documents, list)
        segments = []
        for segment_document, segment in zip(
            segment_documents,
            prepared_text.segments,
            strict=True,
        ):
            assert isinstance(segment_document, dict)
            source_blocks = [
                mapping.block_ordinal
                for mapping in block_mappings
                if any(
                    span.start < mapping.source_span.end and mapping.source_span.start < span.end
                    for span in segment.source_spans
                )
            ]
            includes_title = bool(
                title_span is not None
                and any(
                    span.start < title_span.end and title_span.start < span.end
                    for span in segment.source_spans
                )
            )
            segments.append(
                {
                    **segment_document,
                    "source_block_ordinals": source_blocks,
                    "includes_title": includes_title,
                }
            )
        preparation = {
            "schema_version": prepared_manifest["schema_version"],
            "original": prepared_manifest["original"],
            "normalizer_version": prepared_manifest["normalizer_version"],
            "segmenter_version": prepared_manifest["segmenter_version"],
            "segments": segments,
            "article_review_accepted": prepared_article.article_review_accepted,
            "title_source_span": (
                {"start": title_span.start, "end": title_span.end}
                if title_span is not None
                else None
            ),
            "block_source_spans": [
                {
                    "block_ordinal": mapping.block_ordinal,
                    "start": mapping.source_span.start,
                    "end": mapping.source_span.end,
                    "speech_text_sha256": mapping.speech_text_sha256,
                }
                for mapping in block_mappings
            ],
            "omitted_block_ordinals": list(prepared_article.omitted_block_ordinals),
        }
    return {
        "schema_version": 1,
        "status": result.status.value,
        "source": article_document["source"],
        "article": article_document["article"],
        "language": {
            "selected_language": selection.language.value if selection.language else None,
            "selected_script": selection.script.value if selection.script else None,
            "selection_reason": selection.reason.value,
            "policy_version": selection.policy_version,
            "metadata_hint": result.ingestion.article.language_hint,
            "detector_evidence": detector_evidence,
            "script_evidence": {
                "classification": script_detection.script.value,
                "latin_letter_count": script_detection.latin_letter_count,
                "cyrillic_letter_count": script_detection.cyrillic_letter_count,
                "detector_version": script_detection.detector_version,
            },
        },
        "preparation": preparation,
    }


def _run_prepare_article(arguments: argparse.Namespace, settings: Settings) -> int:
    requested_language = None if arguments.language == "auto" else Language(arguments.language)
    requested_script = Script(arguments.script) if arguments.script is not None else None
    result = _article_preparation_service(settings).prepare(
        arguments.url,
        requested_language=requested_language,
        requested_script=requested_script,
        accept_article_review=arguments.accept_review,
    )
    manifest = _article_preparation_manifest(result)
    if arguments.output is not None:
        _write_json_atomically(arguments.output, manifest)
    if arguments.json:
        print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"Status: {result.status.value}")
        print(f"Title: {result.ingestion.article.title or '(missing)'}")
        selection = result.language_selection
        if selection.language is None:
            assert result.language_detection is not None
            top = result.language_detection.top_candidate
            print(
                f"Language: override required ({selection.reason.value}); "
                f"top detector candidate {top.code} at {top.confidence:.3f}"
            )
        else:
            assert selection.script is not None
            print(
                f"Language: {selection.language.value}/{selection.script.value} "
                f"({selection.reason.value})"
            )
        if result.status is ArticlePreparationStatus.NEEDS_ARTICLE_REVIEW:
            reasons = ", ".join(reason.value for reason in result.ingestion.article.review_reasons)
            print(f"Review required: {reasons or 'block-specific review'}")
            print("Inspect the preview, then rerun with --accept-review if it is complete.")
        elif result.status is ArticlePreparationStatus.NEEDS_LANGUAGE_OVERRIDE:
            print("Rerun with --language en|de|sr; Serbian also requires --script latin|cyrillic.")
        else:
            assert result.prepared_article is not None
            prepared = result.prepared_article.prepared_text
            print(
                f"Prepared: {len(prepared.segments)} segment(s), "
                f"{prepared.total_speech_characters} speech character(s)"
            )
        if arguments.output is not None:
            print(f"Manifest written to: {_absolute_output_path(arguments.output)}")
    return _EXIT_OK if result.status is ArticlePreparationStatus.READY else _EXIT_UNAVAILABLE


def _worker_dependencies(
    settings: Settings, registry: VoiceRegistry, database: Database
) -> WorkerLoop:
    from article_reader.db.repositories import (
        SqliteArticleRepository,
        SqliteAudioChunkRepository,
        SqliteJobRepository,
        SqliteReadingRepository,
        SqliteRenditionRepository,
    )
    from article_reader.storage.durable_audio import LocalDurableAudioStore
    from article_reader.worker.loop import WorkerLoop, WorkerSettings

    model_store = ModelStore(settings.paths.models_dir)
    audio_store = LocalDurableAudioStore(settings.paths.audio_dir / "renditions")
    removed = audio_store.clear_stale_temporary_files()
    if removed:
        print(f"Removed {removed} stale temporary audio file(s) from a previous run.")

    return WorkerLoop(
        readings=SqliteReadingRepository(database),
        articles=SqliteArticleRepository(database),
        renditions=SqliteRenditionRepository(database),
        audio_chunks=SqliteAudioChunkRepository(database),
        jobs=SqliteJobRepository(database),
        preparation_service=_article_preparation_service(settings),
        voice_registry=registry,
        engine_factory=lambda: PiperEngine(model_store),
        audio_store=audio_store,
        settings=WorkerSettings(
            chunk_timeout_seconds=settings.speech.chunk_timeout_seconds,
            heartbeat_seconds=settings.worker.heartbeat_seconds,
            lease_seconds=settings.worker.lease_seconds,
            max_automatic_recoveries=settings.worker.max_automatic_recoveries,
        ),
    )


def _create_listening_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(2_048)
        listener.set_inheritable(True)
    except OSError:
        listener.close()
        raise
    return listener


def _run_serve(arguments: argparse.Namespace, settings: Settings) -> int:
    if settings.server.lan_mode and not arguments.lan:
        print(
            "error: LAN exposure requires explicit 'serve --lan' on every launch; a config-file "
            "setting alone is not sufficient.",
            file=sys.stderr,
        )
        return _EXIT_UNAVAILABLE

    import threading
    import webbrowser

    import uvicorn

    from article_reader.api.app import create_app
    from article_reader.db.connection import Database
    from article_reader.network import (
        LanAddressError,
        discover_private_lan_addresses,
        http_authority,
        is_private_lan_address,
        select_private_lan_address,
    )
    from article_reader.storage.process_lock import InstanceLock, InstanceLockError

    lan_address: str | None = None
    if arguments.lan:
        configured = settings.server.bind
        requested = arguments.bind
        if requested is None and is_private_lan_address(configured):
            requested = configured
        discovered = discover_private_lan_addresses()
        try:
            lan_address = select_private_lan_address(requested, discovered)
        except LanAddressError as error:
            print(f"error: {error}", file=sys.stderr)
            return _EXIT_UNAVAILABLE

    ensure_runtime_dirs(settings)
    lock = InstanceLock(settings.paths.data_dir / "instance.lock")
    try:
        lock.acquire()
    except InstanceLockError as error:
        print(f"error: {error}", file=sys.stderr)
        return _EXIT_UNAVAILABLE

    try:
        database = Database(settings.paths.database_path)
        registry = _packaged_registry()
        app = create_app(settings, database, registry, lan_address=lan_address)
        worker = _worker_dependencies(settings, registry, database)

        listeners: list[socket.socket] = []
        if lan_address is not None:
            try:
                listeners.append(_create_listening_socket("127.0.0.1", settings.server.port))
                listeners.append(_create_listening_socket(lan_address, settings.server.port))
            except OSError as error:
                for listener in listeners:
                    listener.close()
                database.close()
                print(f"error: could not open the local reader sockets: {error}", file=sys.stderr)
                return _EXIT_UNAVAILABLE

        stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=worker.run_forever, args=(stop_event,), name="article-reader-worker"
        )
        worker_thread.start()

        desktop_url = f"http://localhost:{settings.server.port}/"
        print(f"Article Reader (this computer): {desktop_url}")
        if lan_address is not None:
            phone_url = f"http://{http_authority(lan_address, settings.server.port)}/"
            print(f"Article Reader (phone): {phone_url}")
            print("Open the desktop page, choose 'Pair a phone', then enter the one-time code.")
            print("LAN traffic uses HTTP and is not confidential against local-network sniffing.")
        print("Press Ctrl+C to stop.")

        if not arguments.no_open:

            def open_browser() -> None:
                try:
                    webbrowser.open(desktop_url, new=2)
                except OSError:
                    print(f"Could not open a browser automatically; open {desktop_url} manually.")

            timer = threading.Timer(0.8, open_browser)
            timer.daemon = True
            timer.start()

        try:
            if listeners:
                config = uvicorn.Config(
                    app,
                    access_log=False,
                    log_level="info",
                    proxy_headers=False,
                )
                uvicorn.Server(config).run(sockets=listeners)
            else:
                uvicorn.run(
                    app,
                    host=settings.server.bind,
                    port=settings.server.port,
                    access_log=False,
                    log_level="info",
                    proxy_headers=False,
                )
        finally:
            stop_event.set()
            worker_thread.join(timeout=30)
            for listener in listeners:
                listener.close()
            database.close()
    finally:
        lock.release()
    return _EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        settings = _load_cli_settings(arguments)
        if arguments.command == "doctor":
            return _run_doctor(arguments, settings)
        if arguments.command == "voices" and arguments.voices_command == "list":
            return _run_voices_list(arguments, settings)
        if arguments.command == "voices" and arguments.voices_command == "install":
            return _run_voices_install(arguments, settings)
        if arguments.command == "evaluate-voices":
            return _run_evaluate(arguments, settings)
        if arguments.command == "prepare-text":
            return _run_prepare_text(arguments, settings)
        if arguments.command == "fetch-article":
            return _run_fetch_article(arguments, settings)
        if arguments.command == "prepare-article":
            return _run_prepare_article(arguments, settings)
        if arguments.command == "serve":
            return _run_serve(arguments, settings)
    except (
        ConfigError,
        EvaluationCorpusError,
        EvaluationError,
        VoiceRegistryError,
        TextInputError,
        TextDomainError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_INVALID
    except VoiceInstallError as exc:
        print(f"error: voice installation failed: {exc}", file=sys.stderr)
        return _EXIT_FAILED
    except ArticleFetchError as exc:
        print(f"error [{exc.code.value}]: {exc}", file=sys.stderr)
        if exc.code in {FetchErrorCode.INVALID_URL, FetchErrorCode.BLOCKED_DESTINATION}:
            return _EXIT_INVALID
        return _EXIT_FAILED
    except ArticleExtractionError as exc:
        print(f"error [{exc.code.value}]: {exc}", file=sys.stderr)
        return _EXIT_FAILED
    except LanguageSelectionError as exc:
        print(f"error [{exc.code.value}]: {exc}", file=sys.stderr)
        return _EXIT_INVALID
    except LanguageDetectorError as exc:
        print(f"error [{exc.code.value}]: {exc}", file=sys.stderr)
        return _EXIT_FAILED
    except KeyboardInterrupt:
        print("Article Reader stopped.")
        return _EXIT_OK
    except OSError as exc:
        print(f"error: local operation failed: {exc}", file=sys.stderr)
        return _EXIT_FAILED

    parser.error("unsupported command")
    return _EXIT_INVALID


__all__ = ["build_parser", "main"]
