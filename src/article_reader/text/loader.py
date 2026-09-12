"""Bounded, validated loading of a local UTF-8 text file for manual input.

This is the only place that touches the filesystem for manual/long-form
text input. Nothing here is invoked on import; loading happens only when a
caller explicitly requests it. Symlinks/junctions, oversized files,
non-UTF-8 bytes, and control-character-laden ("malformed") content are all
rejected with a controlled :class:`TextInputError` before any text reaches
the rest of the pipeline. Error messages never include the file's content,
only its path and configured sizes.
"""

from __future__ import annotations

from pathlib import Path


class TextInputError(ValueError):
    """Raised when a local manual-text input file cannot be safely used."""


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def load_bounded_utf8_text_file(path: Path, *, max_characters: int) -> str:
    """Read, validate, and return the exact UTF-8 text content of ``path``.

    Raises :class:`TextInputError` for a missing, non-regular, symlinked,
    oversized, non-UTF-8, empty, or control-character-laden file.
    """

    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    if isinstance(max_characters, bool) or not isinstance(max_characters, int):
        raise TypeError("max_characters must be an integer")
    if max_characters <= 0:
        raise ValueError("max_characters must be positive")

    if not path.exists():
        raise TextInputError(f"text file does not exist: {path}")
    if _is_link_or_junction(path.parent) or _is_link_or_junction(path):
        raise TextInputError(f"text file path must not use a symlink or junction: {path}")
    if not path.is_file():
        raise TextInputError(f"text file path is not a regular file: {path}")

    byte_ceiling = max_characters * 4
    try:
        with path.open("rb") as stream:
            raw_bytes = stream.read(byte_ceiling + 1)
    except OSError as error:
        raise TextInputError(f"cannot read text file: {path}") from error

    if not raw_bytes:
        raise TextInputError(f"text file is empty: {path}")
    if len(raw_bytes) > byte_ceiling:
        raise TextInputError(
            f"text file exceeds the configured {max_characters}-character limit: {path}"
        )

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise TextInputError(f"text file is not valid UTF-8: {path}") from error

    if not text.strip():
        raise TextInputError(f"text file is empty or contains only whitespace: {path}")
    if len(text) > max_characters:
        raise TextInputError(
            f"text file exceeds the configured {max_characters}-character limit: {path}"
        )
    if "\x00" in text:
        raise TextInputError(f"text file contains NUL characters: {path}")
    for character in text:
        if ord(character) < 32 and character not in {"\n", "\r", "\t"}:
            raise TextInputError(f"text file contains a control character: {path}")

    return text


__all__ = ["TextInputError", "load_bounded_utf8_text_file"]
