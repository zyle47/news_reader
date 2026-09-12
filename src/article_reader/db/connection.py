"""Per-thread SQLite connections with WAL, foreign keys, and explicit transactions.

Every process/thread gets its own connection (SQLite connections are not safe to share
across threads). Writers use ``BEGIN IMMEDIATE`` so SQLite serializes concurrent claimers
through its own locking instead of relying on optimistic in-Python coordination: two
workers racing to claim the same job always resolve to exactly one winner. Long-running
work (network fetch, Piper synthesis) must never happen inside a transaction opened here.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from article_reader.db.errors import IncompatibleSchemaError
from article_reader.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS

_BUSY_TIMEOUT_MS = 5_000


def _configure(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path),
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=True,
    )
    _configure(connection)
    return connection


def _apply_migrations(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    current = int(row[0])
    if current > CURRENT_SCHEMA_VERSION:
        raise IncompatibleSchemaError(
            f"database schema version {current} is newer than this application supports "
            f"(version {CURRENT_SCHEMA_VERSION}). Refusing to open it with an older build; "
            "install a matching or newer application version instead of resetting the data."
        )
    if current == CURRENT_SCHEMA_VERSION:
        return
    for version, _description, statements in MIGRATIONS:
        if version <= current:
            continue
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {version}")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")


class Database:
    """Owns migrations for one SQLite file and hands out per-thread connections."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("database path must be an absolute pathlib.Path")
        self._path = path
        self._local = threading.local()
        bootstrap = _connect(path)
        try:
            _apply_migrations(bootstrap)
        finally:
            bootstrap.close()

    @property
    def path(self) -> Path:
        return self._path

    def _connection(self) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is None:
            connection = _connect(self._path)
            self._local.connection = connection
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A short, explicit write transaction. Never wrap network or synthesis calls in this."""

        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        yield self._connection()

    def close(self) -> None:
        connection: sqlite3.Connection | None = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None


__all__ = ["Database"]
