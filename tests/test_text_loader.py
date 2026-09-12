from __future__ import annotations

import os
from pathlib import Path

import pytest

from article_reader.text.loader import TextInputError, load_bounded_utf8_text_file


def test_reads_exact_utf8_content(tmp_path: Path) -> None:
    file_path = tmp_path / "article.txt"
    content = "Prvi pasus.\n\nDrugi pasus sa čšž đ nj lj i München's straße."
    # write_bytes (not write_text) avoids platform newline translation, so this
    # verifies byte-exact round-tripping, matching the loader's own binary reads.
    file_path.write_bytes(content.encode("utf-8"))

    result = load_bounded_utf8_text_file(file_path, max_characters=10_000)

    assert result == content


def test_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(TextInputError, match="does not exist"):
        load_bounded_utf8_text_file(tmp_path / "missing.txt", max_characters=1000)


def test_rejects_empty_file(tmp_path: Path) -> None:
    file_path = tmp_path / "empty.txt"
    file_path.write_bytes(b"")
    with pytest.raises(TextInputError, match="empty"):
        load_bounded_utf8_text_file(file_path, max_characters=1000)


def test_rejects_whitespace_only_file(tmp_path: Path) -> None:
    file_path = tmp_path / "blank.txt"
    file_path.write_text("   \n\t  \n  ", encoding="utf-8")
    with pytest.raises(TextInputError, match="whitespace"):
        load_bounded_utf8_text_file(file_path, max_characters=1000)


def test_rejects_oversized_file_by_byte_ceiling(tmp_path: Path) -> None:
    file_path = tmp_path / "big.txt"
    file_path.write_text("a" * 500, encoding="utf-8")
    with pytest.raises(TextInputError, match="exceeds"):
        load_bounded_utf8_text_file(file_path, max_characters=100)


def test_rejects_oversized_file_by_character_count_under_byte_ceiling(tmp_path: Path) -> None:
    # 350 ASCII characters is under max_characters*4 (400) but over max_characters (100).
    file_path = tmp_path / "sneaky.txt"
    file_path.write_text("a" * 350, encoding="utf-8")
    with pytest.raises(TextInputError, match="exceeds"):
        load_bounded_utf8_text_file(file_path, max_characters=100)


def test_rejects_non_utf8_bytes(tmp_path: Path) -> None:
    file_path = tmp_path / "latin1.txt"
    file_path.write_bytes("café".encode("latin-1"))
    with pytest.raises(TextInputError, match="UTF-8"):
        load_bounded_utf8_text_file(file_path, max_characters=1000)


def test_rejects_nul_bytes(tmp_path: Path) -> None:
    file_path = tmp_path / "nul.txt"
    file_path.write_bytes(b"before\x00after")
    with pytest.raises(TextInputError, match="NUL"):
        load_bounded_utf8_text_file(file_path, max_characters=1000)


def test_rejects_control_characters(tmp_path: Path) -> None:
    file_path = tmp_path / "control.txt"
    file_path.write_bytes(b"before\x07after")
    with pytest.raises(TextInputError, match="control character"):
        load_bounded_utf8_text_file(file_path, max_characters=1000)


def test_allows_newlines_tabs_and_carriage_returns(tmp_path: Path) -> None:
    file_path = tmp_path / "whitespace.txt"
    content = "line one\r\nline two\tindented"
    file_path.write_bytes(content.encode("utf-8"))
    result = load_bounded_utf8_text_file(file_path, max_characters=1000)
    assert result == content


def test_rejects_directory_path(tmp_path: Path) -> None:
    with pytest.raises(TextInputError, match="regular file"):
        load_bounded_utf8_text_file(tmp_path, max_characters=1000)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires elevated privileges on CI")
def test_rejects_symlinked_file(tmp_path: Path) -> None:
    real_file = tmp_path / "real.txt"
    real_file.write_text("content", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real_file)
    with pytest.raises(TextInputError, match="symlink"):
        load_bounded_utf8_text_file(link, max_characters=1000)


def test_error_messages_never_include_file_content(tmp_path: Path) -> None:
    secret_text = "SECRET-MARKER-CONTENT-DO-NOT-LEAK"
    file_path = tmp_path / "leaky.txt"
    file_path.write_bytes((secret_text * 10).encode("utf-8"))
    with pytest.raises(TextInputError) as excinfo:
        load_bounded_utf8_text_file(file_path, max_characters=5)
    assert secret_text not in str(excinfo.value)


def test_rejects_non_path_argument() -> None:
    with pytest.raises(TypeError):
        load_bounded_utf8_text_file("not-a-path", max_characters=1000)  # type: ignore[arg-type]


def test_rejects_non_positive_max_characters(tmp_path: Path) -> None:
    file_path = tmp_path / "file.txt"
    file_path.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="positive"):
        load_bounded_utf8_text_file(file_path, max_characters=0)


def test_import_performs_no_io(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import importlib
    import sys

    sys.modules.pop("article_reader.text.loader", None)
    real_open = builtins.open

    def guarded_open(*args: object, **kwargs: object) -> object:
        raise AssertionError("module import must not open any file")

    monkeypatch.setattr(builtins, "open", guarded_open)
    try:
        importlib.import_module("article_reader.text.loader")
    finally:
        monkeypatch.setattr(builtins, "open", real_open)
