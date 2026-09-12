"""Strict loader for the checked-in, non-authoritative voice catalogue.

The registry describes exact artifacts and their human evaluation state.  It
does not download models, infer defaults, approve candidates, or substitute one
voice for another.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from article_reader.domain.speech import (
    Language,
    Script,
    SpeechDomainError,
    VoiceEvaluationStatus,
    VoiceSpec,
)

_MAX_REGISTRY_BYTES = 1_048_576
_TOP_LEVEL_KEYS = frozenset({"schema_version", "voices"})
_VOICE_KEYS = frozenset(
    {
        "id",
        "display_name",
        "language",
        "scripts",
        "engine",
        "engine_version",
        "sample_rate_hz",
        "source_url",
        "source_revision",
        "model_url",
        "model_sha256",
        "config_url",
        "config_sha256",
        "model_license_url",
        "data_license_url",
        "evaluation_status",
        "evaluation_notes",
    }
)


class VoiceRegistryError(ValueError):
    """Base class for invalid registry data and failed lookups."""


class DuplicateVoiceIdError(VoiceRegistryError):
    """Raised for IDs which are ambiguous on case-insensitive filesystems."""


class VoiceNotFoundError(VoiceRegistryError, LookupError):
    """Raised when an exact requested voice ID does not exist."""


class VoiceNotApprovedError(VoiceRegistryError):
    """Raised when callers explicitly require an approved voice."""


@dataclass(frozen=True, slots=True)
class VoiceRegistry:
    """Immutable collection of exact voice specifications."""

    voices: tuple[VoiceSpec, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version != 1:
            raise VoiceRegistryError(f"unsupported voice registry schema: {self.schema_version}")
        try:
            voices = tuple(self.voices)
        except TypeError as error:
            raise VoiceRegistryError("voices must be an iterable of VoiceSpec values") from error
        if any(not isinstance(voice, VoiceSpec) for voice in voices):
            raise VoiceRegistryError("voices must contain only VoiceSpec values")

        identifiers: set[str] = set()
        for voice in voices:
            folded = voice.voice_id.casefold()
            if folded in identifiers:
                raise DuplicateVoiceIdError(
                    f"duplicate or case-ambiguous voice ID: {voice.voice_id}"
                )
            identifiers.add(folded)
        object.__setattr__(self, "voices", voices)

    def __iter__(self) -> Iterator[VoiceSpec]:
        return iter(self.voices)

    def __len__(self) -> int:
        return len(self.voices)

    def get(self, voice_id: str) -> VoiceSpec:
        """Return one exact ID; no language fallback or substitution occurs."""

        for voice in self.voices:
            if voice.voice_id == voice_id:
                return voice
        raise VoiceNotFoundError(f"voice is not present in the registry: {voice_id!r}")

    def require_approved(self, voice_id: str) -> VoiceSpec:
        """Return the exact voice only when human evaluation approved it."""

        voice = self.get(voice_id)
        if voice.evaluation_status is not VoiceEvaluationStatus.APPROVED:
            raise VoiceNotApprovedError(
                f"voice {voice_id!r} is {voice.evaluation_status.value}, not approved"
            )
        return voice

    def approved_for(self, language: Language, script: Script) -> tuple[VoiceSpec, ...]:
        """List compatible approved voices without choosing among them."""

        if not isinstance(language, Language) or not isinstance(script, Script):
            raise VoiceRegistryError("language and script must be supported enum values")
        return tuple(
            voice
            for voice in self.voices
            if voice.evaluation_status is VoiceEvaluationStatus.APPROVED
            and voice.supports(language, script)
        )


def _required_mapping_value(mapping: dict[str, Any], key: str, index: int) -> Any:
    if key not in mapping:
        raise VoiceRegistryError(f"voices[{index}] is missing required key {key!r}")
    return mapping[key]


def _required_string(mapping: dict[str, Any], key: str, index: int) -> str:
    value = _required_mapping_value(mapping, key, index)
    if not isinstance(value, str):
        raise VoiceRegistryError(f"voices[{index}].{key} must be a string")
    return value


def _parse_enum(
    enum_type: type[Language] | type[Script] | type[VoiceEvaluationStatus],
    value: Any,
    field: str,
    index: int,
) -> Language | Script | VoiceEvaluationStatus:
    if not isinstance(value, str):
        raise VoiceRegistryError(f"voices[{index}].{field} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError as error:
        allowed = ", ".join(member.value for member in enum_type)
        raise VoiceRegistryError(f"voices[{index}].{field} must be one of: {allowed}") from error


def _parse_voice(raw_voice: Any, index: int) -> VoiceSpec:
    if not isinstance(raw_voice, dict):
        raise VoiceRegistryError(f"voices[{index}] must be a TOML table")
    unknown = set(raw_voice) - _VOICE_KEYS
    missing = _VOICE_KEYS - set(raw_voice)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise VoiceRegistryError(f"voices[{index}] contains unknown keys: {names}")
    if missing:
        names = ", ".join(sorted(missing))
        raise VoiceRegistryError(f"voices[{index}] is missing required keys: {names}")

    language_value = _parse_enum(Language, raw_voice["language"], "language", index)
    assert isinstance(language_value, Language)

    raw_scripts = raw_voice["scripts"]
    if not isinstance(raw_scripts, list) or not raw_scripts:
        raise VoiceRegistryError(f"voices[{index}].scripts must be a non-empty array")
    scripts: list[Script] = []
    for script_index, raw_script in enumerate(raw_scripts):
        parsed_script = _parse_enum(
            Script,
            raw_script,
            f"scripts[{script_index}]",
            index,
        )
        assert isinstance(parsed_script, Script)
        scripts.append(parsed_script)

    status_value = _parse_enum(
        VoiceEvaluationStatus,
        raw_voice["evaluation_status"],
        "evaluation_status",
        index,
    )
    assert isinstance(status_value, VoiceEvaluationStatus)

    sample_rate = raw_voice["sample_rate_hz"]
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int):
        raise VoiceRegistryError(f"voices[{index}].sample_rate_hz must be an integer")

    try:
        return VoiceSpec(
            voice_id=_required_string(raw_voice, "id", index),
            display_name=_required_string(raw_voice, "display_name", index),
            language=language_value,
            scripts=tuple(scripts),
            engine=_required_string(raw_voice, "engine", index),
            engine_version=_required_string(raw_voice, "engine_version", index),
            sample_rate_hz=sample_rate,
            source_url=_required_string(raw_voice, "source_url", index),
            source_revision=_required_string(raw_voice, "source_revision", index),
            model_url=_required_string(raw_voice, "model_url", index),
            model_sha256=_required_string(raw_voice, "model_sha256", index),
            config_url=_required_string(raw_voice, "config_url", index),
            config_sha256=_required_string(raw_voice, "config_sha256", index),
            model_license_url=_required_string(raw_voice, "model_license_url", index),
            data_license_url=_required_string(raw_voice, "data_license_url", index),
            evaluation_status=status_value,
            evaluation_notes=_required_string(raw_voice, "evaluation_notes", index),
        )
    except SpeechDomainError as error:
        raise VoiceRegistryError(f"voices[{index}] is invalid: {error}") from error


def load_voice_registry(path: str | Path) -> VoiceRegistry:
    """Load a bounded UTF-8 TOML registry and fail closed on any ambiguity."""

    registry_path = Path(path)
    try:
        with registry_path.open("rb") as stream:
            raw_bytes = stream.read(_MAX_REGISTRY_BYTES + 1)
        if len(raw_bytes) > _MAX_REGISTRY_BYTES:
            raise VoiceRegistryError(f"voice registry exceeds the {_MAX_REGISTRY_BYTES}-byte limit")
        raw_document = tomllib.loads(raw_bytes.decode("utf-8"))
    except VoiceRegistryError:
        raise
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise VoiceRegistryError(f"cannot parse voice registry: {registry_path}") from error

    unknown = set(raw_document) - _TOP_LEVEL_KEYS
    missing = _TOP_LEVEL_KEYS - set(raw_document)
    if unknown:
        names = ", ".join(sorted(unknown))
        raise VoiceRegistryError(f"voice registry contains unknown top-level keys: {names}")
    if missing:
        names = ", ".join(sorted(missing))
        raise VoiceRegistryError(f"voice registry is missing top-level keys: {names}")

    schema_version = raw_document["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise VoiceRegistryError("schema_version must be an integer")
    if schema_version != 1:
        raise VoiceRegistryError(f"unsupported voice registry schema: {schema_version}")

    raw_voices = raw_document["voices"]
    if not isinstance(raw_voices, list):
        raise VoiceRegistryError("voices must be an array of tables")
    voices = tuple(_parse_voice(raw_voice, index) for index, raw_voice in enumerate(raw_voices))
    return VoiceRegistry(voices=voices, schema_version=schema_version)
