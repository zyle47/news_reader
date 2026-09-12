"""Strict loader for versioned, local voice-evaluation passages."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from article_reader.domain.speech import EvaluationPassage, Language, Script

_MAX_CORPUS_BYTES = 2 * 1024 * 1024
_TOP_LEVEL_KEYS = frozenset({"schema_version", "passages"})
_PASSAGE_KEYS = frozenset({"id", "text", "language", "script"})


class EvaluationCorpusError(ValueError):
    """Raised when a local evaluation corpus is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class EvaluationCorpus:
    schema_version: int
    passages: tuple[EvaluationPassage, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise EvaluationCorpusError(
                f"unsupported evaluation corpus schema: {self.schema_version}"
            )
        if not self.passages:
            raise EvaluationCorpusError("evaluation corpus must contain at least one passage")
        identifiers = [passage.passage_id for passage in self.passages]
        if len(set(identifiers)) != len(identifiers):
            raise EvaluationCorpusError("evaluation passage IDs must be unique")

    def compatible_with(
        self, language: Language, scripts: tuple[Script, ...]
    ) -> tuple[EvaluationPassage, ...]:
        return tuple(
            passage
            for passage in self.passages
            if passage.language is language and passage.script in scripts
        )


def load_evaluation_corpus(path: str | Path) -> EvaluationCorpus:
    corpus_path = Path(path)
    try:
        with corpus_path.open("rb") as stream:
            raw_bytes = stream.read(_MAX_CORPUS_BYTES + 1)
        if len(raw_bytes) > _MAX_CORPUS_BYTES:
            raise EvaluationCorpusError(
                f"evaluation corpus exceeds the {_MAX_CORPUS_BYTES}-byte limit"
            )
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except EvaluationCorpusError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise EvaluationCorpusError(f"cannot parse evaluation corpus: {corpus_path}") from exc

    unknown = set(document) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(document)
    if unknown:
        raise EvaluationCorpusError(
            f"evaluation corpus contains unknown top-level keys: {', '.join(sorted(unknown))}"
        )
    if missing:
        raise EvaluationCorpusError(
            f"evaluation corpus is missing top-level keys: {', '.join(sorted(missing))}"
        )

    schema_version = document["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise EvaluationCorpusError("evaluation corpus schema_version must be an integer")

    raw_passages = document["passages"]
    if not isinstance(raw_passages, list):
        raise EvaluationCorpusError("evaluation corpus passages must be an array of tables")

    passages: list[EvaluationPassage] = []
    for index, raw_passage in enumerate(raw_passages):
        if not isinstance(raw_passage, dict):
            raise EvaluationCorpusError(f"passages[{index}] must be a TOML table")
        unknown_fields = set(raw_passage) - _PASSAGE_KEYS
        missing_fields = _PASSAGE_KEYS - set(raw_passage)
        if unknown_fields or missing_fields:
            detail = []
            if unknown_fields:
                detail.append(f"unknown: {', '.join(sorted(unknown_fields))}")
            if missing_fields:
                detail.append(f"missing: {', '.join(sorted(missing_fields))}")
            raise EvaluationCorpusError(f"passages[{index}] has invalid keys ({'; '.join(detail)})")
        try:
            passage = EvaluationPassage(
                passage_id=raw_passage["id"],
                text=raw_passage["text"],
                language=Language(raw_passage["language"]),
                script=Script(raw_passage["script"]),
            )
        except (TypeError, ValueError) as exc:
            raise EvaluationCorpusError(f"passages[{index}] is invalid: {exc}") from exc
        passages.append(passage)

    return EvaluationCorpus(schema_version=schema_version, passages=tuple(passages))


__all__ = [
    "EvaluationCorpus",
    "EvaluationCorpusError",
    "load_evaluation_corpus",
]
