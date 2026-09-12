from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from article_reader.application.services.diagnostics import (
    DiagnosticStatus,
    ModelAvailability,
    SystemDiagnosticProbe,
    collect_diagnostics,
)
from article_reader.config import CacheSettings, PathsSettings, Settings


@dataclass
class FakeProbe:
    free_bytes: int = 10_000
    writable: bool = True
    port_is_available: bool = True
    ram_bytes: int | None = 32_000

    def operating_system(self) -> str:
        return "TestOS 1"

    def architecture(self) -> str:
        return "test64"

    def python_version(self) -> str:
        return "3.12.9"

    def sqlite_version(self) -> str:
        return "3.45.0"

    def logical_cpu_count(self) -> int | None:
        return 8

    def total_memory_bytes(self) -> int | None:
        return self.ram_bytes

    def free_disk_bytes(self, path: Path) -> int:
        del path
        return self.free_bytes

    def directory_writable(self, path: Path) -> bool:
        del path
        return self.writable

    def port_available(self, bind: str, port: int) -> bool:
        del bind, port
        return self.port_is_available


def test_report_contains_all_required_checks_in_stable_order(tmp_path: Path) -> None:
    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )

    report = collect_diagnostics(settings, probe=FakeProbe())

    assert tuple(check.name for check in report.checks) == (
        "operating_system",
        "architecture",
        "python_version",
        "sqlite_version",
        "logical_cpus",
        "total_ram_bytes",
        "free_disk_bytes",
        "model_availability",
        "audio_directory_writable",
        "server_port_available",
    )
    assert report.get("operating_system").value == "TestOS 1"
    assert report.get("architecture").value == "test64"
    assert report.get("python_version").value == "3.12.9"
    assert report.get("sqlite_version").value == "3.45.0"
    assert report.get("logical_cpus").value == 8
    assert report.get("total_ram_bytes").value == 32_000
    assert report.get("free_disk_bytes").value == 10_000
    assert report.get("model_availability").status is DiagnosticStatus.UNKNOWN
    assert report.healthy is True
    assert report.ready is False


def test_resource_failures_are_errors_and_make_report_unhealthy(tmp_path: Path) -> None:
    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )
    probe = FakeProbe(free_bytes=999, writable=False, port_is_available=False)

    report = collect_diagnostics(settings, probe=probe)

    assert report.get("free_disk_bytes").status is DiagnosticStatus.ERROR
    assert report.get("audio_directory_writable").status is DiagnosticStatus.ERROR
    assert report.get("server_port_available").status is DiagnosticStatus.ERROR
    assert report.healthy is False
    assert report.ready is False


def test_unavailable_ram_is_reported_as_unknown_without_hiding_other_results(
    tmp_path: Path,
) -> None:
    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )

    report = collect_diagnostics(settings, probe=FakeProbe(ram_bytes=None))

    assert report.get("total_ram_bytes").status is DiagnosticStatus.UNKNOWN
    assert report.get("architecture").status is DiagnosticStatus.OK
    assert report.healthy is True


def test_probe_exception_is_isolated_in_the_report(tmp_path: Path) -> None:
    class PartlyFailingProbe(FakeProbe):
        def operating_system(self) -> str:
            raise OSError("inspection denied")

    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )

    report = collect_diagnostics(settings, probe=PartlyFailingProbe())

    os_check = report.get("operating_system")
    assert os_check.status is DiagnosticStatus.UNKNOWN
    assert "inspection denied" in (os_check.detail or "")
    assert report.get("sqlite_version").status is DiagnosticStatus.OK


def test_unsupported_python_minor_is_a_readiness_error(tmp_path: Path) -> None:
    class NewerPythonProbe(FakeProbe):
        def python_version(self) -> str:
            return "3.14.0"

    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )

    report = collect_diagnostics(settings, probe=NewerPythonProbe())

    assert report.get("python_version").status is DiagnosticStatus.ERROR
    assert report.healthy is False
    assert report.ready is False


def test_injected_model_availability_is_serializable(tmp_path: Path) -> None:
    settings = Settings(
        paths=PathsSettings(tmp_path / "runtime"),
        cache=CacheSettings(min_free_disk_bytes=1_000),
    )

    def model_probe(models_dir: Path) -> ModelAvailability:
        assert models_dir == settings.paths.models_dir
        return ModelAvailability(
            status=DiagnosticStatus.OK,
            installed_voice_ids=("de-voice", "en-voice"),
            detail="Two approved voices are installed.",
        )

    report = collect_diagnostics(settings, probe=FakeProbe(), model_probe=model_probe)

    model_check = report.get("model_availability")
    assert model_check.status is DiagnosticStatus.OK
    assert model_check.value == ("de-voice", "en-voice")
    serialized = report.as_dict()
    serialized_checks = cast(list[dict[str, object]], serialized["checks"])
    serialized_model = next(
        check for check in serialized_checks if check["name"] == "model_availability"
    )
    assert serialized_model["value"] == ["de-voice", "en-voice"]
    assert report.ready is True


def test_model_availability_cannot_claim_ok_without_an_installed_voice() -> None:
    with pytest.raises(ValueError, match="must name an installed voice"):
        ModelAvailability(status=DiagnosticStatus.OK)


def test_system_writability_probe_does_not_create_a_missing_directory(tmp_path: Path) -> None:
    probe = SystemDiagnosticProbe()
    audio_dir = tmp_path / "audio"

    assert probe.directory_writable(audio_dir) is False
    assert not audio_dir.exists()

    audio_dir.mkdir()
    assert probe.directory_writable(audio_dir) is True
    assert list(audio_dir.iterdir()) == []
