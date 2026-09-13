"""Migration application, incompatible-schema rejection, and pragma configuration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from article_reader.db.connection import Database
from article_reader.db.errors import IncompatibleSchemaError
from article_reader.db.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS


def test_fresh_database_applies_all_migrations(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    with database.read() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == CURRENT_SCHEMA_VERSION
    assert {"readings", "articles", "renditions", "audio_chunks", "jobs"} <= tables


def test_database_enables_foreign_keys_and_wal(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    with database.read() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_reopening_an_up_to_date_database_is_a_no_op(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite3"
    Database(path)
    second = Database(path)
    with second.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_incompatible_future_schema_is_refused_without_resetting_data(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite3"
    Database(path)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(IncompatibleSchemaError):
        Database(path)

    # The file was not reset/truncated by the refused open.
    verify = sqlite3.connect(str(path))
    try:
        assert verify.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION + 1
    finally:
        verify.close()


def test_foreign_key_violation_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "INSERT INTO readings (reading_id, viewer_id, submitted_url, requested_language,"
            " requested_script, state, created_at, last_opened_at, deleted_at) VALUES"
            " ('r1', 'missing-viewer', 'https://example.com', NULL, NULL, 'queued', 't', 't',"
            " NULL)"
        )


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.sqlite3")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO viewers (viewer_id, label, created_at) VALUES ('v1', 'Browser', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            "INSERT INTO viewers (viewer_id, label, created_at) VALUES ('v2', 'Browser', 't')"
        )
        connection.execute(
            "INSERT INTO viewers (viewer_id, label, created_at) VALUES ('v2', 'Dup', 't')"
        )
    with database.read() as connection:
        row = connection.execute("SELECT COUNT(*) FROM viewers WHERE viewer_id = 'v2'").fetchone()
    assert row[0] == 0


def test_restart_persists_committed_data(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite3"
    first = Database(path)
    with first.transaction() as connection:
        connection.execute(
            "INSERT INTO viewers (viewer_id, label, created_at) VALUES ('v1', 'Browser', 't')"
        )
    first.close()

    second = Database(path)
    with second.read() as connection:
        row = connection.execute("SELECT label FROM viewers WHERE viewer_id = 'v1'").fetchone()
    assert row["label"] == "Browser"


def test_m3_database_migrates_legacy_viewer_tokens_to_expiring_sessions(tmp_path: Path) -> None:
    path = tmp_path / "m3.sqlite3"
    connection = sqlite3.connect(str(path))
    try:
        for statement in MIGRATIONS[0][2]:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO viewers (viewer_id, label, created_at) VALUES (?, ?, ?)",
            ("viewer-1", "Existing browser", "2026-09-12T10:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO viewer_tokens (token_hash, viewer_id, created_at) VALUES (?, ?, ?)",
            ("a" * 64, "viewer-1", "2026-09-12T10:00:00+00:00"),
        )
        connection.execute("PRAGMA user_version = 1")
        connection.commit()
    finally:
        connection.close()

    database = Database(path)
    with database.read() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
        assert (
            migrated.execute(
                "SELECT COUNT(*) FROM viewer_sessions WHERE viewer_id = 'viewer-1'"
            ).fetchone()[0]
            == 1
        )
        assert (
            migrated.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'viewer_tokens'"
            ).fetchone()[0]
            == 0
        )
