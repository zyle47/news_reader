from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest

from article_reader.domain.speech import Language, Script
from article_reader.speech.evaluation_corpus import (
    EvaluationCorpusError,
    load_evaluation_corpus,
)


def test_packaged_smoke_corpus_covers_every_language_and_serbian_script() -> None:
    resource = resources.files("article_reader.resources").joinpath("evaluation_smoke.toml")
    with resources.as_file(resource) as path:
        corpus = load_evaluation_corpus(path)

    assert corpus.compatible_with(Language.ENGLISH, (Script.LATIN,))
    assert corpus.compatible_with(Language.GERMAN, (Script.LATIN,))
    assert len(corpus.compatible_with(Language.SERBIAN, (Script.LATIN, Script.CYRILLIC))) == 2


def test_corpus_rejects_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "corpus.toml"
    path.write_text(
        """
schema_version = 1
[[passages]]
id = "valid-id"
language = "en"
script = "latin"
text = "Valid text."
unexpected = true
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationCorpusError, match="unknown"):
        load_evaluation_corpus(path)


def test_corpus_rejects_duplicate_passage_ids(tmp_path: Path) -> None:
    path = tmp_path / "corpus.toml"
    path.write_text(
        """
schema_version = 1
[[passages]]
id = "same"
language = "en"
script = "latin"
text = "First."
[[passages]]
id = "same"
language = "en"
script = "latin"
text = "Second."
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(EvaluationCorpusError, match="unique"):
        load_evaluation_corpus(path)
