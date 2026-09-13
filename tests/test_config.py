from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from article_reader.config import (
    ConfigError,
    PathsSettings,
    ServerSettings,
    Settings,
    default_data_dir,
    ensure_runtime_dirs,
    load_settings,
)


def test_defaults_match_the_implementation_contract_and_are_immutable(tmp_path: Path) -> None:
    settings = load_settings(env={"LOCALAPPDATA": str(tmp_path)})

    assert settings.paths.data_dir == tmp_path / "ArticleReader"
    assert settings.server.port == 8765
    assert settings.server.bind == "127.0.0.1"
    assert settings.server.lan_mode is False
    assert settings.access.session_lifetime_hours == 720
    assert settings.access.max_sessions_per_viewer == 8
    assert settings.worker.tts_threads == 2
    assert settings.fetch.max_decoded_bytes == 5_242_880
    assert settings.fetch.max_response_bytes == 5_242_880
    assert settings.speech.max_chunk_characters == 600
    assert settings.cache.max_audio_bytes == 2_147_483_648
    assert settings.history.max_saved_readings_per_viewer == 200

    with pytest.raises(FrozenInstanceError):
        settings.server.port = 9000  # type: ignore[misc]


def test_loading_has_no_filesystem_side_effect_and_directory_creation_is_explicit(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "not-created-by-load"

    settings = load_settings(env={"ARTICLE_READER_DATA_DIR": str(data_dir)})

    assert not data_dir.exists()
    paths = ensure_runtime_dirs(settings)
    assert paths is settings.paths
    assert all(path.is_dir() for path in paths.runtime_directories)
    assert not paths.database_path.exists()


def test_precedence_is_defaults_then_toml_then_environment_then_overrides(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "reader.toml"
    config_file.write_text(
        """
data_dir = "relative-data"

[server]
port = 8100

[worker]
tts_threads = 3
""".strip(),
        encoding="utf-8",
    )

    settings = load_settings(
        config_file,
        env={
            "ARTICLE_READER_SERVER_PORT": "8200",
            "ARTICLE_READER_WORKER_TTS_THREADS": "4",
        },
        overrides={"server.port": 8300, "worker": {"tts_threads": 5}},
    )

    assert settings.paths.data_dir == tmp_path / "relative-data"
    assert settings.server.port == 8300
    assert settings.worker.tts_threads == 5
    assert settings.server.bind == "127.0.0.1"


def test_environment_values_are_typed_and_alias_data_dir(tmp_path: Path) -> None:
    settings = load_settings(
        env={
            "ARTICLE_READER_DATA_DIR": str(tmp_path / "runtime"),
            "ARTICLE_READER_SERVER_LAN_MODE": "yes",
            "ARTICLE_READER_SERVER_BIND": "0.0.0.0",
            "ARTICLE_READER_WORKER_HEARTBEAT_SECONDS": "2.5",
            "ARTICLE_READER_WORKER_LEASE_SECONDS": "10",
        }
    )

    assert settings.paths.data_dir == tmp_path / "runtime"
    assert settings.server == ServerSettings(bind="0.0.0.0", port=8765, lan_mode=True)
    assert settings.worker.heartbeat_seconds == 2.5
    assert settings.worker.lease_seconds == 10.0


@pytest.mark.parametrize(
    ("source", "expected_fragment"),
    [
        ({"unknown": {}}, "unknown configuration section"),
        ({"server": {"porrt": 9000}}, "server.porrt"),
        ({"server.port.extra": 9000}, "unknown setting"),
    ],
)
def test_explicit_overrides_reject_unknown_keys(
    source: dict[str, object], expected_fragment: str
) -> None:
    with pytest.raises(ConfigError, match=expected_fragment):
        load_settings(env={}, overrides=source)


def test_prefixed_environment_typo_is_not_silently_ignored() -> None:
    with pytest.raises(ConfigError, match="ARTICLE_READER_SERVER_PORRT"):
        load_settings(env={"ARTICLE_READER_SERVER_PORRT": "9000"})


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"server.port": 0}, "server.port"),
        ({"server.port": True}, "server.port"),
        ({"fetch.overall_timeout_seconds": 1}, "overall_timeout_seconds"),
        ({"fetch.max_article_characters": 100_001}, "max_article_characters"),
        ({"worker.heartbeat_seconds": 60, "worker.lease_seconds": 100}, "twice"),
        ({"server.bind": "0.0.0.0"}, "lan_mode"),
        (
            {"server.bind": "8.8.8.8", "server.lan_mode": True},
            "private RFC1918/ULA",
        ),
        ({"access.pairing_lifetime_seconds": 2}, "pairing_lifetime_seconds"),
    ],
)
def test_validation_errors_name_the_invalid_setting(
    overrides: dict[str, object], expected_fragment: str
) -> None:
    with pytest.raises(ConfigError, match=expected_fragment):
        load_settings(env={}, overrides=overrides)


def test_none_explicit_override_means_cli_option_was_not_set() -> None:
    settings = load_settings(env={}, overrides={"server.port": None})
    assert settings.server.port == 8765


def test_default_data_dir_uses_each_platform_convention(tmp_path: Path) -> None:
    assert (
        default_data_dir(
            platform_name="win32", env={"LOCALAPPDATA": str(tmp_path / "local")}, home=tmp_path
        )
        == tmp_path / "local" / "ArticleReader"
    )
    assert default_data_dir(platform_name="darwin", env={}, home=tmp_path) == (
        tmp_path / "Library" / "Application Support" / "ArticleReader"
    )
    assert (
        default_data_dir(
            platform_name="linux", env={"XDG_DATA_HOME": str(tmp_path / "xdg")}, home=tmp_path
        )
        == tmp_path / "xdg" / "article-reader"
    )


def test_runtime_data_directory_cannot_be_a_filesystem_root() -> None:
    with pytest.raises(ConfigError, match="filesystem root"):
        PathsSettings(Path(Path.cwd().anchor))


def test_settings_rejects_wrong_nested_types() -> None:
    with pytest.raises(ConfigError, match=r"settings\.paths"):
        Settings(paths="not paths")  # type: ignore[arg-type]
