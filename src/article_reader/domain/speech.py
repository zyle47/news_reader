"""Validated, engine-independent speech domain values.

The objects in this module are deliberately immutable.  Engine adapters may hold
mutable model state, but data crossing the application boundary must be safe to
cache, compare, and serialize without an adapter changing it behind the caller's
back.
"""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from types import MappingProxyType
from urllib.parse import urlsplit

_STABLE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_ENGINE_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.+_-]{0,62}[A-Za-z0-9])?$")
_SETTING_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PASSAGE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_REVISION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/+:-]{0,126}[A-Za-z0-9])?$")
_FLOATING_REVISIONS = frozenset({"head", "latest", "main", "master", "stable", "trunk"})
_WINDOWS_RESERVED_NAMES = frozenset(
    {"aux", "con", "nul", "prn"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


class Language(StrEnum):
    """Languages in the product's MVP boundary."""

    ENGLISH = "en"
    GERMAN = "de"
    SERBIAN = "sr"


class Script(StrEnum):
    """Writing systems understood by a voice or evaluation passage."""

    LATIN = "latin"
    CYRILLIC = "cyrillic"


class VoiceEvaluationStatus(StrEnum):
    """Human evaluation state recorded in the checked-in voice registry."""

    UNVERIFIED = "unverified"
    EXPERIMENTAL = "experimental"
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EvaluationPassage:
    """One language/script-labelled input in a versioned evaluation corpus."""

    passage_id: str
    text: str
    language: Language
    script: Script

    def __post_init__(self) -> None:
        if not isinstance(self.passage_id, str) or _PASSAGE_ID.fullmatch(self.passage_id) is None:
            raise ValueError(
                "passage_id must be a lowercase filename-safe identifier up to 64 characters"
            )
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("evaluation passage text cannot be empty")
        if self.text != self.text.strip():
            raise ValueError("evaluation passage text cannot have outer whitespace")
        if "\x00" in self.text:
            raise ValueError("evaluation passage text cannot contain NUL characters")
        if len(self.text) > 100_000:
            raise ValueError("evaluation passage text exceeds 100000 characters")
        if not isinstance(self.language, Language):
            raise ValueError("evaluation passage language must be a Language enum")
        if not isinstance(self.script, Script):
            raise ValueError("evaluation passage script must be a Script enum")


class SpeechDomainError(ValueError):
    """Raised when an invalid value attempts to cross the speech boundary."""


class SpeechEngineError(RuntimeError):
    """Base error reported by speech engine adapters."""


class SpeechEngineNotLoadedError(SpeechEngineError):
    """Raised when synthesis is requested before a voice has been loaded."""


type SpeechSettingValue = str | int | float | bool


def _require_text(
    value: object,
    field_name: str,
    *,
    maximum_length: int,
    allow_newlines: bool = False,
) -> str:
    if not isinstance(value, str):
        raise SpeechDomainError(f"{field_name} must be a string")
    if not value or value != value.strip():
        raise SpeechDomainError(f"{field_name} must be non-empty and have no outer whitespace")
    if len(value) > maximum_length:
        raise SpeechDomainError(f"{field_name} exceeds {maximum_length} characters")
    for character in value:
        if ord(character) < 32 and not (allow_newlines and character in {"\n", "\r", "\t"}):
            raise SpeechDomainError(f"{field_name} contains a control character")
    return value


def _validate_stable_id(value: object, field_name: str) -> str:
    text = _require_text(value, field_name, maximum_length=128)
    if _STABLE_ID.fullmatch(text) is None:
        raise SpeechDomainError(
            f"{field_name} must contain only letters, digits, '.', '_' or '-' and cannot "
            "start or end with punctuation"
        )
    if text.split(".", maxsplit=1)[0].casefold() in _WINDOWS_RESERVED_NAMES:
        raise SpeechDomainError(f"{field_name} is reserved on Windows")
    return text


def _validate_engine_version(value: object) -> str:
    text = _require_text(value, "engine_version", maximum_length=64)
    if _ENGINE_VERSION.fullmatch(text) is None:
        raise SpeechDomainError("engine_version is not a valid pinned version identifier")
    if text.casefold() in _FLOATING_REVISIONS:
        raise SpeechDomainError("engine_version must be pinned, not a floating label")
    return text


def _validate_revision(value: object) -> str:
    text = _require_text(value, "source_revision", maximum_length=128)
    if _REVISION.fullmatch(text) is None or ".." in text or "//" in text:
        raise SpeechDomainError("source_revision is not a valid pinned revision")
    if text.casefold() in _FLOATING_REVISIONS:
        raise SpeechDomainError("source_revision must be pinned, not a floating branch label")
    return text


def _validate_sha256(value: object, field_name: str) -> str:
    text = _require_text(value, field_name, maximum_length=64)
    if _SHA256.fullmatch(text) is None:
        raise SpeechDomainError(f"{field_name} must be exactly 64 hexadecimal characters")
    return text.lower()


def _validate_https_url(value: object, field_name: str) -> str:
    text = _require_text(value, field_name, maximum_length=2048)
    if any(character.isspace() for character in text) or "\\" in text:
        raise SpeechDomainError(f"{field_name} contains invalid URL characters")
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as error:
        raise SpeechDomainError(f"{field_name} is not a valid URL") from error
    if parsed.scheme != "https":
        raise SpeechDomainError(f"{field_name} must use https")
    hostname = parsed.hostname
    if not hostname:
        raise SpeechDomainError(f"{field_name} must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise SpeechDomainError(f"{field_name} cannot contain credentials")
    if port not in {None, 443}:
        raise SpeechDomainError(f"{field_name} cannot use a non-HTTPS port")
    if parsed.fragment:
        raise SpeechDomainError(f"{field_name} cannot contain a fragment")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if "." not in hostname:
            raise SpeechDomainError(f"{field_name} must include a public-style hostname") from None
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError as error:
            raise SpeechDomainError(f"{field_name} contains an invalid hostname") from error
        labels = ascii_hostname.rstrip(".").split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[A-Za-z0-9-]+", label) is None
            for label in labels
        ):
            raise SpeechDomainError(f"{field_name} contains an invalid hostname") from None
    else:
        if not address.is_global:
            raise SpeechDomainError(f"{field_name} cannot use a non-public IP address")
    return text


@dataclass(frozen=True, slots=True)
class SpeechSettings:
    """Canonical immutable synthesis options.

    Generic application code does not invent engine-specific defaults.  An
    adapter owns the meaning and validation of supported option names; this
    value guarantees only that options are deterministic, finite, scalar, and
    safe to include in a rendition contract.
    """

    options: tuple[tuple[str, SpeechSettingValue], ...] = ()

    def __post_init__(self) -> None:
        normalized: list[tuple[str, SpeechSettingValue]] = []
        seen: set[str] = set()
        try:
            supplied = tuple(self.options)
        except TypeError as error:
            raise SpeechDomainError("speech setting options must be key/value pairs") from error

        for entry in supplied:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise SpeechDomainError("each speech setting must be a two-item tuple")
            key, value = entry
            if not isinstance(key, str) or _SETTING_NAME.fullmatch(key) is None:
                raise SpeechDomainError(f"invalid speech setting name: {key!r}")
            if key in seen:
                raise SpeechDomainError(f"duplicate speech setting: {key}")
            seen.add(key)

            if isinstance(value, bool):
                normalized_value: SpeechSettingValue = value
            elif isinstance(value, int):
                if abs(value) > 1_000_000_000:
                    raise SpeechDomainError(f"speech setting {key!r} is outside the safe range")
                normalized_value = value
            elif isinstance(value, float):
                if not math.isfinite(value) or abs(value) > 1_000_000_000:
                    raise SpeechDomainError(f"speech setting {key!r} must be finite and bounded")
                normalized_value = value
            elif isinstance(value, str):
                normalized_value = _require_text(
                    value,
                    f"speech setting {key!r}",
                    maximum_length=256,
                )
            else:
                raise SpeechDomainError(f"speech setting {key!r} must have a scalar value")
            normalized.append((key, normalized_value))

        normalized.sort(key=lambda item: item[0])
        object.__setattr__(self, "options", tuple(normalized))

    @classmethod
    def from_mapping(cls, values: Mapping[str, SpeechSettingValue]) -> SpeechSettings:
        return cls(tuple(values.items()))

    def as_mapping(self) -> Mapping[str, SpeechSettingValue]:
        return MappingProxyType(dict(self.options))


@dataclass(frozen=True, slots=True)
class SpeechEngineDescriptor:
    """Identity and evidence characteristics exposed by an engine adapter."""

    engine_id: str
    engine_version: str
    is_test_double: bool = False

    def __post_init__(self) -> None:
        _validate_stable_id(self.engine_id, "engine_id")
        _validate_engine_version(self.engine_version)
        if not isinstance(self.is_test_double, bool):
            raise SpeechDomainError("is_test_double must be a boolean")


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    """One exact, checksummed voice release from the registry."""

    voice_id: str
    display_name: str
    language: Language
    scripts: tuple[Script, ...]
    engine: str
    engine_version: str
    sample_rate_hz: int
    source_url: str
    source_revision: str
    model_url: str
    model_sha256: str
    config_url: str
    config_sha256: str
    model_license_url: str
    data_license_url: str
    evaluation_status: VoiceEvaluationStatus
    evaluation_notes: str

    def __post_init__(self) -> None:
        _validate_stable_id(self.voice_id, "voice_id")
        _require_text(self.display_name, "display_name", maximum_length=160)
        if not isinstance(self.language, Language):
            raise SpeechDomainError("language must be a supported Language enum")

        try:
            scripts = tuple(self.scripts)
        except TypeError as error:
            raise SpeechDomainError("scripts must be an iterable of Script enum values") from error
        if not scripts:
            raise SpeechDomainError("scripts cannot be empty")
        if any(not isinstance(script, Script) for script in scripts):
            raise SpeechDomainError("scripts must contain only Script enum values")
        if len(set(scripts)) != len(scripts):
            raise SpeechDomainError("scripts cannot contain duplicates")
        if self.language in {Language.ENGLISH, Language.GERMAN} and scripts != (Script.LATIN,):
            raise SpeechDomainError("English and German voices must declare only Latin script")
        object.__setattr__(self, "scripts", scripts)

        _validate_stable_id(self.engine, "engine")
        _validate_engine_version(self.engine_version)
        if isinstance(self.sample_rate_hz, bool) or not isinstance(self.sample_rate_hz, int):
            raise SpeechDomainError("sample_rate_hz must be an integer")
        if not 8_000 <= self.sample_rate_hz <= 192_000:
            raise SpeechDomainError("sample_rate_hz must be between 8000 and 192000")

        _validate_https_url(self.source_url, "source_url")
        _validate_revision(self.source_revision)
        _validate_https_url(self.model_url, "model_url")
        object.__setattr__(
            self,
            "model_sha256",
            _validate_sha256(self.model_sha256, "model_sha256"),
        )
        _validate_https_url(self.config_url, "config_url")
        object.__setattr__(
            self,
            "config_sha256",
            _validate_sha256(self.config_sha256, "config_sha256"),
        )
        _validate_https_url(self.model_license_url, "model_license_url")
        _validate_https_url(self.data_license_url, "data_license_url")

        if not isinstance(self.evaluation_status, VoiceEvaluationStatus):
            raise SpeechDomainError("evaluation_status must be a VoiceEvaluationStatus enum")
        if not isinstance(self.evaluation_notes, str):
            raise SpeechDomainError("evaluation_notes must be a string")
        notes = self.evaluation_notes
        if len(notes) > 4096:
            raise SpeechDomainError("evaluation_notes exceeds 4096 characters")
        if notes and notes != notes.strip():
            raise SpeechDomainError("evaluation_notes cannot have outer whitespace")
        if any(ord(character) < 32 and character not in {"\n", "\r", "\t"} for character in notes):
            raise SpeechDomainError("evaluation_notes contains a control character")
        if self.evaluation_status is VoiceEvaluationStatus.APPROVED and not notes.strip():
            raise SpeechDomainError("approved voices require evaluation notes")
        if (
            self.engine.casefold() == "fake"
            and self.evaluation_status is VoiceEvaluationStatus.APPROVED
        ):
            raise SpeechDomainError("a fake engine voice cannot be approved as quality evidence")

    def supports(self, language: Language, script: Script) -> bool:
        return self.language is language and script in self.scripts


@dataclass(frozen=True, slots=True)
class AudioResult:
    """Owned PCM bytes and the metadata required to package them safely.

    No filesystem path can be supplied by an engine.  Publication remains the
    application's responsibility.
    """

    pcm_bytes: bytes
    sample_rate_hz: int
    sample_width_bytes: int
    channels: int

    def __post_init__(self) -> None:
        if not isinstance(self.pcm_bytes, bytes):
            raise SpeechDomainError("pcm_bytes must be immutable bytes")
        if not self.pcm_bytes:
            raise SpeechDomainError("pcm_bytes cannot be empty")
        if isinstance(self.sample_rate_hz, bool) or not isinstance(self.sample_rate_hz, int):
            raise SpeechDomainError("sample_rate_hz must be an integer")
        if not 8_000 <= self.sample_rate_hz <= 192_000:
            raise SpeechDomainError("sample_rate_hz must be between 8000 and 192000")
        if isinstance(self.sample_width_bytes, bool) or self.sample_width_bytes not in {1, 2, 3, 4}:
            raise SpeechDomainError("sample_width_bytes must be 1, 2, 3, or 4")
        if isinstance(self.channels, bool) or not isinstance(self.channels, int):
            raise SpeechDomainError("channels must be an integer")
        if not 1 <= self.channels <= 8:
            raise SpeechDomainError("channels must be between 1 and 8")
        if len(self.pcm_bytes) % self.frame_width_bytes != 0:
            raise SpeechDomainError("PCM byte length is not aligned to complete audio frames")

    @property
    def frame_width_bytes(self) -> int:
        return self.sample_width_bytes * self.channels

    @property
    def frame_count(self) -> int:
        return len(self.pcm_bytes) // self.frame_width_bytes

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate_hz

    @property
    def pcm_sha256(self) -> str:
        return hashlib.sha256(self.pcm_bytes).hexdigest()

    def as_wav_bytes(self) -> bytes:
        """Return a standard uncompressed PCM WAV container in memory."""

        destination = BytesIO()
        with wave.open(destination, "wb") as wav_file:
            wav_file.setnchannels(self.channels)
            wav_file.setsampwidth(self.sample_width_bytes)
            wav_file.setframerate(self.sample_rate_hz)
            wav_file.writeframes(self.pcm_bytes)
        return destination.getvalue()
