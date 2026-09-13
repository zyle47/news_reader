"""Typed application configuration and runtime path management.

Configuration loading is deliberately explicit. Importing this module does not read a
configuration file, inspect the environment, or create directories.
"""

from __future__ import annotations

import ctypes
import ipaddress
import math
import os
import re
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Final, Literal

from article_reader.network import is_private_lan_address

ENV_PREFIX: Final = "ARTICLE_READER_"
_DNS_LABEL: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_WINDOWS_REMOTE_DRIVE: Final = 4


class ConfigError(ValueError):
    """Raised when configuration cannot be loaded or validated."""


def _require_bool(name: str, value: object) -> None:
    if type(value) is not bool:
        raise ConfigError(f"{name} must be a boolean, got {value!r}")


def _require_int(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if type(value) is not int:
        raise ConfigError(f"{name} must be an integer, got {value!r}")
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}, got {value}")


def _require_number(
    name: str,
    value: object,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}, got {value}")


def _absolute_path(path: Path) -> Path:
    expanded = path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    try:
        return absolute.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"could not canonicalize filesystem path {absolute}: {exc}") from exc


def _has_windows_network_or_device_syntax(path: Path) -> bool:
    rendered = str(path)
    return rendered.startswith((r"\\", "//"))


def _is_windows_mapped_network_drive(path: Path) -> bool:
    if os.name != "nt" or not path.drive:
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_drive_type = kernel32.GetDriveTypeW
        get_drive_type.argtypes = [ctypes.c_wchar_p]
        get_drive_type.restype = ctypes.c_uint
        drive_root = f"{path.drive}\\"
        return int(get_drive_type(drive_root)) == _WINDOWS_REMOTE_DRIVE
    except (AttributeError, OSError, ValueError):
        return False


def _canonical_local_data_path(path: Path) -> Path:
    """Canonicalize a runtime path and reject Windows network-backed locations.

    SQLite, process locks, and atomic artifact publication require a local filesystem.
    Windows UNC, device, and mapped network paths are therefore intentionally unsupported.
    """

    expanded = path.expanduser()
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if _has_windows_network_or_device_syntax(absolute) or _is_windows_mapped_network_drive(
        absolute
    ):
        raise ConfigError(
            "paths.data_dir must be on a local filesystem; Windows UNC, device, and mapped "
            "network paths are not supported"
        )
    canonical = _absolute_path(absolute)
    if _has_windows_network_or_device_syntax(canonical) or _is_windows_mapped_network_drive(
        canonical
    ):
        raise ConfigError(
            "paths.data_dir must be on a local filesystem; Windows UNC, device, and mapped "
            "network paths are not supported"
        )
    return canonical


def default_data_dir(
    *,
    platform_name: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform-local data directory without creating it.

    Optional inputs make path selection deterministic in tests and diagnostics. Empty
    platform environment variables are ignored rather than interpreted as the current
    directory.
    """

    current_platform = (platform_name or sys.platform).lower()
    environment = os.environ if env is None else env
    user_home = _absolute_path(home or Path.home())

    if current_platform.startswith("win"):
        local_app_data = environment.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else user_home / "AppData" / "Local"
        return _canonical_local_data_path(base / "ArticleReader")

    if current_platform == "darwin":
        return _canonical_local_data_path(
            user_home / "Library" / "Application Support" / "ArticleReader"
        )

    xdg_data_home = environment.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data_home) if xdg_data_home else user_home / ".local" / "share"
    return _canonical_local_data_path(base / "article-reader")


@dataclass(frozen=True, slots=True)
class PathsSettings:
    """Machine-local paths. Derived paths never depend on article content."""

    data_dir: Path = field(default_factory=default_data_dir)

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, Path):
            raise ConfigError(f"paths.data_dir must be a pathlib.Path, got {self.data_dir!r}")
        canonical = _canonical_local_data_path(self.data_dir)
        if canonical == Path(canonical.anchor):
            raise ConfigError("paths.data_dir cannot be a filesystem root")
        object.__setattr__(self, "data_dir", canonical)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "article-reader.sqlite3"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def temp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def runtime_directories(self) -> tuple[Path, ...]:
        return (self.data_dir, self.models_dir, self.audio_dir, self.temp_dir)


@dataclass(frozen=True, slots=True)
class ServerSettings:
    bind: str = "127.0.0.1"
    port: int = 8765
    lan_mode: bool = False

    def __post_init__(self) -> None:
        _require_int("server.port", self.port, minimum=1, maximum=65_535)
        _require_bool("server.lan_mode", self.lan_mode)
        loopback, wildcard = _classify_bind_host(self.bind)
        if wildcard and not self.lan_mode:
            raise ConfigError("server.bind wildcard addresses require server.lan_mode to be true")
        if not self.lan_mode and not loopback:
            raise ConfigError(
                "server.bind must be loopback unless server.lan_mode is true; "
                "use 127.0.0.1 or ::1 for local-only operation"
            )
        if (
            self.lan_mode
            and not loopback
            and not wildcard
            and not is_private_lan_address(self.bind)
        ):
            raise ConfigError(
                "server.bind in LAN mode must be loopback, an unspecified auto-bind marker, "
                "or a private RFC1918/ULA address"
            )


@dataclass(frozen=True, slots=True)
class AccessSettings:
    session_lifetime_hours: int = 720
    pairing_lifetime_seconds: int = 300
    max_sessions_per_viewer: int = 8
    max_pairing_attempts: int = 5
    pairing_attempt_window_seconds: int = 60
    pairing_block_seconds: int = 60

    def __post_init__(self) -> None:
        _require_int(
            "access.session_lifetime_hours",
            self.session_lifetime_hours,
            minimum=1,
            maximum=8_760,
        )
        _require_int(
            "access.pairing_lifetime_seconds",
            self.pairing_lifetime_seconds,
            minimum=30,
            maximum=3_600,
        )
        _require_int(
            "access.max_sessions_per_viewer",
            self.max_sessions_per_viewer,
            minimum=1,
            maximum=64,
        )
        _require_int(
            "access.max_pairing_attempts", self.max_pairing_attempts, minimum=1, maximum=100
        )
        _require_int(
            "access.pairing_attempt_window_seconds",
            self.pairing_attempt_window_seconds,
            minimum=1,
            maximum=3_600,
        )
        _require_int(
            "access.pairing_block_seconds",
            self.pairing_block_seconds,
            minimum=1,
            maximum=86_400,
        )


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    processes: int = 1
    tts_threads: int = 2
    resident_models: int = 1
    max_waiting_chains: int = 10
    max_unfinished_request_chains_per_viewer: int = 1
    heartbeat_seconds: float = 5.0
    lease_seconds: float = 120.0
    max_automatic_recoveries: int = 1

    def __post_init__(self) -> None:
        _require_int("worker.processes", self.processes, minimum=1, maximum=32)
        _require_int("worker.tts_threads", self.tts_threads, minimum=1, maximum=256)
        _require_int("worker.resident_models", self.resident_models, minimum=1, maximum=32)
        _require_int(
            "worker.max_waiting_chains", self.max_waiting_chains, minimum=1, maximum=100_000
        )
        _require_int(
            "worker.max_unfinished_request_chains_per_viewer",
            self.max_unfinished_request_chains_per_viewer,
            minimum=1,
            maximum=1_000,
        )
        _require_number(
            "worker.heartbeat_seconds", self.heartbeat_seconds, minimum=0.1, maximum=3_600
        )
        _require_number("worker.lease_seconds", self.lease_seconds, minimum=1, maximum=86_400)
        _require_int(
            "worker.max_automatic_recoveries",
            self.max_automatic_recoveries,
            minimum=0,
            maximum=100,
        )
        if self.lease_seconds < self.heartbeat_seconds * 2:
            raise ConfigError(
                "worker.lease_seconds must be at least twice worker.heartbeat_seconds"
            )


@dataclass(frozen=True, slots=True)
class FetchSettings:
    connect_timeout_seconds: float = 5.0
    overall_timeout_seconds: float = 25.0
    max_redirects: int = 5
    max_response_bytes: int = 5_242_880
    max_decoded_bytes: int = 5_242_880
    max_article_characters: int = 100_000
    max_url_characters: int = 4_096

    def __post_init__(self) -> None:
        _require_number(
            "fetch.connect_timeout_seconds",
            self.connect_timeout_seconds,
            minimum=0.1,
            maximum=300,
        )
        _require_number(
            "fetch.overall_timeout_seconds",
            self.overall_timeout_seconds,
            minimum=0.1,
            maximum=3_600,
        )
        if self.overall_timeout_seconds < self.connect_timeout_seconds:
            raise ConfigError(
                "fetch.overall_timeout_seconds must be greater than or equal to "
                "fetch.connect_timeout_seconds"
            )
        _require_int("fetch.max_redirects", self.max_redirects, minimum=0, maximum=20)
        _require_int(
            "fetch.max_response_bytes",
            self.max_response_bytes,
            minimum=1_024,
            maximum=1_073_741_824,
        )
        _require_int(
            "fetch.max_decoded_bytes",
            self.max_decoded_bytes,
            minimum=1_024,
            maximum=1_073_741_824,
        )
        _require_int(
            "fetch.max_article_characters",
            self.max_article_characters,
            minimum=1,
            maximum=100_000,
        )
        _require_int(
            "fetch.max_url_characters",
            self.max_url_characters,
            minimum=1,
            maximum=1_000_000,
        )


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    max_chunk_characters: int = 600
    chunk_timeout_seconds: float = 120.0
    max_model_download_bytes: int = 209_715_200
    model_download_connect_timeout_seconds: float = 10.0
    model_download_overall_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        _require_int(
            "speech.max_chunk_characters",
            self.max_chunk_characters,
            minimum=1,
            maximum=100_000,
        )
        _require_number(
            "speech.chunk_timeout_seconds",
            self.chunk_timeout_seconds,
            minimum=1,
            maximum=86_400,
        )
        _require_int(
            "speech.max_model_download_bytes",
            self.max_model_download_bytes,
            minimum=1_024,
            maximum=10_737_418_240,
        )
        _require_number(
            "speech.model_download_connect_timeout_seconds",
            self.model_download_connect_timeout_seconds,
            minimum=0.1,
            maximum=300,
        )
        _require_number(
            "speech.model_download_overall_timeout_seconds",
            self.model_download_overall_timeout_seconds,
            minimum=0.1,
            maximum=7_200,
        )
        if (
            self.model_download_overall_timeout_seconds
            < self.model_download_connect_timeout_seconds
        ):
            raise ConfigError(
                "speech.model_download_overall_timeout_seconds must be greater than or equal "
                "to speech.model_download_connect_timeout_seconds"
            )


@dataclass(frozen=True, slots=True)
class CacheSettings:
    max_audio_bytes: int = 2_147_483_648
    inactive_days: int = 7
    playback_lease_seconds: float = 180.0
    min_free_disk_bytes: int = 1_073_741_824

    def __post_init__(self) -> None:
        _require_int(
            "cache.max_audio_bytes",
            self.max_audio_bytes,
            minimum=1,
            maximum=1_125_899_906_842_624,
        )
        _require_int("cache.inactive_days", self.inactive_days, minimum=0, maximum=36_500)
        _require_number(
            "cache.playback_lease_seconds",
            self.playback_lease_seconds,
            minimum=1,
            maximum=86_400,
        )
        _require_int(
            "cache.min_free_disk_bytes",
            self.min_free_disk_bytes,
            minimum=0,
            maximum=1_125_899_906_842_624,
        )


@dataclass(frozen=True, slots=True)
class HistorySettings:
    max_saved_readings_per_viewer: int = 200

    def __post_init__(self) -> None:
        _require_int(
            "history.max_saved_readings_per_viewer",
            self.max_saved_readings_per_viewer,
            minimum=1,
            maximum=1_000_000,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    paths: PathsSettings = field(default_factory=PathsSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    access: AccessSettings = field(default_factory=AccessSettings)
    worker: WorkerSettings = field(default_factory=WorkerSettings)
    fetch: FetchSettings = field(default_factory=FetchSettings)
    speech: SpeechSettings = field(default_factory=SpeechSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    history: HistorySettings = field(default_factory=HistorySettings)

    def __post_init__(self) -> None:
        expected = (
            ("paths", self.paths, PathsSettings),
            ("server", self.server, ServerSettings),
            ("access", self.access, AccessSettings),
            ("worker", self.worker, WorkerSettings),
            ("fetch", self.fetch, FetchSettings),
            ("speech", self.speech, SpeechSettings),
            ("cache", self.cache, CacheSettings),
            ("history", self.history, HistorySettings),
        )
        for name, value, expected_type in expected:
            if not isinstance(value, expected_type):
                raise ConfigError(
                    f"settings.{name} must be {expected_type.__name__}, got {value!r}"
                )


def _classify_bind_host(bind: object) -> tuple[bool, bool]:
    if not isinstance(bind, str) or not bind:
        raise ConfigError("server.bind must be a non-empty host or IP address")
    if len(bind) > 253:
        raise ConfigError("server.bind cannot exceed 253 characters")
    if bind != bind.strip() or any(character.isspace() for character in bind):
        raise ConfigError("server.bind cannot contain whitespace")
    if any(character in bind for character in "/?#@\\[]"):
        raise ConfigError(f"server.bind is not a valid bare host or IP address: {bind!r}")

    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        if ":" in bind:
            raise ConfigError(
                "server.bind must not include a port; configure server.port separately"
            ) from None
        hostname = bind[:-1] if bind.endswith(".") else bind
        if not hostname:
            raise ConfigError(f"server.bind is not a valid DNS hostname: {bind!r}") from None
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ConfigError(f"server.bind is not a valid DNS hostname: {bind!r}") from exc
        if len(ascii_hostname) > 253:
            raise ConfigError(
                "server.bind DNS hostname cannot exceed 253 ASCII characters"
            ) from None
        labels = ascii_hostname.split(".")
        if any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            raise ConfigError(f"server.bind is not a valid DNS hostname: {bind!r}") from None
        if all(character.isdigit() or character == "." for character in ascii_hostname):
            raise ConfigError(
                "server.bind numeric addresses must use canonical IPv4 or IPv6 notation"
            ) from None
        return ascii_hostname.casefold() == "localhost", False
    return address.is_loopback, address.is_unspecified


SettingPath = tuple[str, str]
SettingKind = Literal["bool", "int", "float", "str", "path"]

_FIELD_KINDS: Final[dict[SettingPath, SettingKind]] = {
    ("paths", "data_dir"): "path",
    ("server", "bind"): "str",
    ("server", "port"): "int",
    ("server", "lan_mode"): "bool",
    ("access", "session_lifetime_hours"): "int",
    ("access", "pairing_lifetime_seconds"): "int",
    ("access", "max_sessions_per_viewer"): "int",
    ("access", "max_pairing_attempts"): "int",
    ("access", "pairing_attempt_window_seconds"): "int",
    ("access", "pairing_block_seconds"): "int",
    ("worker", "processes"): "int",
    ("worker", "tts_threads"): "int",
    ("worker", "resident_models"): "int",
    ("worker", "max_waiting_chains"): "int",
    ("worker", "max_unfinished_request_chains_per_viewer"): "int",
    ("worker", "heartbeat_seconds"): "float",
    ("worker", "lease_seconds"): "float",
    ("worker", "max_automatic_recoveries"): "int",
    ("fetch", "connect_timeout_seconds"): "float",
    ("fetch", "overall_timeout_seconds"): "float",
    ("fetch", "max_redirects"): "int",
    ("fetch", "max_response_bytes"): "int",
    ("fetch", "max_decoded_bytes"): "int",
    ("fetch", "max_article_characters"): "int",
    ("fetch", "max_url_characters"): "int",
    ("speech", "max_chunk_characters"): "int",
    ("speech", "chunk_timeout_seconds"): "float",
    ("speech", "max_model_download_bytes"): "int",
    ("speech", "model_download_connect_timeout_seconds"): "float",
    ("speech", "model_download_overall_timeout_seconds"): "float",
    ("cache", "max_audio_bytes"): "int",
    ("cache", "inactive_days"): "int",
    ("cache", "playback_lease_seconds"): "float",
    ("cache", "min_free_disk_bytes"): "int",
    ("history", "max_saved_readings_per_viewer"): "int",
}

_SECTIONS: Final = frozenset(section for section, _ in _FIELD_KINDS)
_ENV_TO_FIELD: Final[dict[str, SettingPath]] = {
    f"{ENV_PREFIX}{section}_{name}".upper(): (section, name) for section, name in _FIELD_KINDS
}
_ENV_TO_FIELD[f"{ENV_PREFIX}DATA_DIR"] = ("paths", "data_dir")


def _default_values(environment: Mapping[str, str]) -> dict[SettingPath, object]:
    defaults = Settings(paths=PathsSettings(data_dir=default_data_dir(env=environment)))
    values: dict[SettingPath, object] = {}
    for section_name in _SECTIONS:
        section = getattr(defaults, section_name)
        for setting_field in fields(section):
            values[(section_name, setting_field.name)] = getattr(section, setting_field.name)
    return values


def _flatten_mapping(
    raw: Mapping[str, object],
    *,
    source: str,
    skip_none: bool = False,
) -> dict[SettingPath, object]:
    flattened: dict[SettingPath, object] = {}

    def add(path: SettingPath, value: object) -> None:
        if path not in _FIELD_KINDS:
            raise ConfigError(f"unknown setting {path[0]}.{path[1]} in {source}")
        if path in flattened:
            raise ConfigError(f"setting {path[0]}.{path[1]} is specified twice in {source}")
        if skip_none and value is None:
            return
        flattened[path] = value

    for raw_key, value in raw.items():
        if not isinstance(raw_key, str):
            raise ConfigError(f"configuration keys in {source} must be strings, got {raw_key!r}")

        key = raw_key.strip()
        if key == "data_dir":
            add(("paths", "data_dir"), value)
            continue

        if "." in key:
            parts = key.split(".")
            if len(parts) != 2:
                raise ConfigError(f"unknown setting {raw_key!r} in {source}")
            add((parts[0], parts[1]), value)
            continue

        if key not in _SECTIONS:
            raise ConfigError(f"unknown configuration section or setting {raw_key!r} in {source}")
        if value is None and skip_none:
            continue
        if not isinstance(value, Mapping):
            raise ConfigError(f"configuration section {key!r} in {source} must be a table")

        for raw_name, nested_value in value.items():
            if not isinstance(raw_name, str):
                raise ConfigError(
                    f"configuration keys in section {key!r} in {source} must be strings"
                )
            add((key, raw_name.strip()), nested_value)

    return flattened


def _toml_values(config_file: Path) -> dict[SettingPath, object]:
    try:
        with config_file.open("rb") as stream:
            raw = tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file does not exist: {config_file}") from exc
    except PermissionError as exc:
        raise ConfigError(f"configuration file is not readable: {config_file}") from exc
    except OSError as exc:
        raise ConfigError(f"could not read configuration file {config_file}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in configuration file {config_file}: {exc}") from exc

    flattened = _flatten_mapping(raw, source=str(config_file))
    data_dir_key = ("paths", "data_dir")
    if data_dir_key in flattened:
        raw_data_dir = flattened[data_dir_key]
        if isinstance(raw_data_dir, (str, Path)):
            data_dir = Path(raw_data_dir).expanduser()
            if not data_dir.is_absolute():
                flattened[data_dir_key] = config_file.parent / data_dir
    return flattened


def _environment_values(environment: Mapping[str, str]) -> dict[SettingPath, object]:
    flattened: dict[SettingPath, object] = {}
    for raw_name, value in environment.items():
        name = raw_name.upper()
        if not name.startswith(ENV_PREFIX):
            continue
        path = _ENV_TO_FIELD.get(name)
        if path is None:
            raise ConfigError(f"unknown Article Reader environment setting {raw_name!r}")
        if path in flattened:
            raise ConfigError(
                f"environment specifies {path[0]}.{path[1]} more than once (including aliases)"
            )
        flattened[path] = value
    return flattened


def _coerce(path: SettingPath, value: object, *, source: str) -> object:
    label = f"{path[0]}.{path[1]}"
    kind = _FIELD_KINDS[path]

    try:
        if kind == "str":
            if not isinstance(value, str):
                raise ValueError("must be a string")
            return value

        if kind == "path":
            if not isinstance(value, (str, Path)):
                raise ValueError("must be a filesystem path")
            if not str(value).strip():
                raise ValueError("cannot be empty")
            return _canonical_local_data_path(Path(value))

        if kind == "bool":
            if type(value) is bool:
                return value
            if isinstance(value, str):
                normalized = value.strip().casefold()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            raise ValueError("must be true or false")

        if kind == "int":
            if type(value) is int:
                return value
            if isinstance(value, str):
                return int(value.strip(), 10)
            raise ValueError("must be an integer")

        if isinstance(value, bool):
            raise ValueError("must be a number")
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip())
        else:
            raise ValueError("must be a number")
        if not math.isfinite(number):
            raise ValueError("must be finite")
        return number
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value for {label} from {source}: {value!r} ({exc})") from exc


def _apply_values(
    destination: dict[SettingPath, object],
    source_values: Mapping[SettingPath, object],
    *,
    source: str,
) -> None:
    for path, value in source_values.items():
        destination[path] = _coerce(path, value, source=source)


def _value[T](
    values: Mapping[SettingPath, object],
    section: str,
    name: str,
    expected_type: type[T],
) -> T:
    value = values[(section, name)]
    if not isinstance(value, expected_type):
        raise ConfigError(f"internal configuration type error for {section}.{name}")
    return value


def _build_settings(values: Mapping[SettingPath, object]) -> Settings:
    return Settings(
        paths=PathsSettings(data_dir=_value(values, "paths", "data_dir", Path)),
        server=ServerSettings(
            bind=_value(values, "server", "bind", str),
            port=_value(values, "server", "port", int),
            lan_mode=_value(values, "server", "lan_mode", bool),
        ),
        access=AccessSettings(
            session_lifetime_hours=_value(values, "access", "session_lifetime_hours", int),
            pairing_lifetime_seconds=_value(values, "access", "pairing_lifetime_seconds", int),
            max_sessions_per_viewer=_value(values, "access", "max_sessions_per_viewer", int),
            max_pairing_attempts=_value(values, "access", "max_pairing_attempts", int),
            pairing_attempt_window_seconds=_value(
                values, "access", "pairing_attempt_window_seconds", int
            ),
            pairing_block_seconds=_value(values, "access", "pairing_block_seconds", int),
        ),
        worker=WorkerSettings(
            processes=_value(values, "worker", "processes", int),
            tts_threads=_value(values, "worker", "tts_threads", int),
            resident_models=_value(values, "worker", "resident_models", int),
            max_waiting_chains=_value(values, "worker", "max_waiting_chains", int),
            max_unfinished_request_chains_per_viewer=_value(
                values, "worker", "max_unfinished_request_chains_per_viewer", int
            ),
            heartbeat_seconds=_value(values, "worker", "heartbeat_seconds", float),
            lease_seconds=_value(values, "worker", "lease_seconds", float),
            max_automatic_recoveries=_value(values, "worker", "max_automatic_recoveries", int),
        ),
        fetch=FetchSettings(
            connect_timeout_seconds=_value(values, "fetch", "connect_timeout_seconds", float),
            overall_timeout_seconds=_value(values, "fetch", "overall_timeout_seconds", float),
            max_redirects=_value(values, "fetch", "max_redirects", int),
            max_response_bytes=_value(values, "fetch", "max_response_bytes", int),
            max_decoded_bytes=_value(values, "fetch", "max_decoded_bytes", int),
            max_article_characters=_value(values, "fetch", "max_article_characters", int),
            max_url_characters=_value(values, "fetch", "max_url_characters", int),
        ),
        speech=SpeechSettings(
            max_chunk_characters=_value(values, "speech", "max_chunk_characters", int),
            chunk_timeout_seconds=_value(values, "speech", "chunk_timeout_seconds", float),
            max_model_download_bytes=_value(values, "speech", "max_model_download_bytes", int),
            model_download_connect_timeout_seconds=_value(
                values, "speech", "model_download_connect_timeout_seconds", float
            ),
            model_download_overall_timeout_seconds=_value(
                values, "speech", "model_download_overall_timeout_seconds", float
            ),
        ),
        cache=CacheSettings(
            max_audio_bytes=_value(values, "cache", "max_audio_bytes", int),
            inactive_days=_value(values, "cache", "inactive_days", int),
            playback_lease_seconds=_value(values, "cache", "playback_lease_seconds", float),
            min_free_disk_bytes=_value(values, "cache", "min_free_disk_bytes", int),
        ),
        history=HistorySettings(
            max_saved_readings_per_viewer=_value(
                values, "history", "max_saved_readings_per_viewer", int
            )
        ),
    )


def load_settings(
    config_file: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    """Load validated settings in increasing order of precedence.

    Precedence is built-in defaults, then an optional TOML file, then
    ``ARTICLE_READER_`` environment variables, then explicit overrides. Explicit
    overrides may be nested mappings (``{"server": {"port": 9000}}``) or dotted
    keys (``{"server.port": 9000}``). ``None`` explicit values are ignored so CLI
    parsers can pass unset optional arguments directly.
    """

    environment = os.environ if env is None else env
    values = _default_values(environment)

    if config_file is not None:
        config_path = _absolute_path(Path(config_file))
        _apply_values(values, _toml_values(config_path), source=str(config_path))

    _apply_values(values, _environment_values(environment), source="environment")

    if overrides is not None:
        explicit_values = _flatten_mapping(overrides, source="explicit overrides", skip_none=True)
        _apply_values(values, explicit_values, source="explicit overrides")

    return _build_settings(values)


def ensure_runtime_dirs(settings: Settings | PathsSettings) -> PathsSettings:
    """Create and verify the machine-local runtime directories.

    This is intentionally separate from configuration loading so imports and read-only
    commands do not mutate the filesystem.
    """

    paths = settings.paths if isinstance(settings, Settings) else settings
    if not isinstance(paths, PathsSettings):
        raise TypeError("settings must be Settings or PathsSettings")

    for directory in paths.runtime_directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ConfigError(f"could not create runtime directory {directory}: {exc}") from exc
        if not directory.is_dir():
            raise ConfigError(f"runtime path exists but is not a directory: {directory}")
    return paths


__all__ = [
    "AccessSettings",
    "CacheSettings",
    "ConfigError",
    "FetchSettings",
    "HistorySettings",
    "PathsSettings",
    "ServerSettings",
    "Settings",
    "SpeechSettings",
    "WorkerSettings",
    "default_data_dir",
    "ensure_runtime_dirs",
    "load_settings",
]
