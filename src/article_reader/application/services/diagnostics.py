"""Host diagnostics used by the ``doctor`` command.

The collector depends on a small probe protocol so ordinary tests never depend on the
machine running the suite. The default probe uses only Python's standard library and
performs work only when diagnostics are explicitly collected.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import socket
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from article_reader.config import Settings


class DiagnosticStatus(StrEnum):
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    UNKNOWN = "unknown"


type DiagnosticValue = str | int | float | bool | tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    """One independently collected diagnostic result."""

    name: str
    status: DiagnosticStatus
    value: DiagnosticValue
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    """Stable, ordered diagnostic output suitable for human or JSON rendering."""

    checks: tuple[DiagnosticCheck, ...]

    @property
    def healthy(self) -> bool:
        return all(check.status is not DiagnosticStatus.ERROR for check in self.checks)

    @property
    def ready(self) -> bool:
        """Whether required runtime infrastructure is usable right now."""

        try:
            model_check = self.get("model_availability")
        except KeyError:
            return False
        return self.healthy and model_check.status is DiagnosticStatus.OK

    def get(self, name: str) -> DiagnosticCheck:
        for check in self.checks:
            if check.name == name:
                return check
        raise KeyError(f"diagnostic check not found: {name}")

    def as_dict(self) -> dict[str, object]:
        serialized_checks: list[dict[str, object]] = []
        for check in self.checks:
            value: object = list(check.value) if isinstance(check.value, tuple) else check.value
            serialized_checks.append(
                {
                    "name": check.name,
                    "status": check.status.value,
                    "value": value,
                    "detail": check.detail,
                }
            )
        return {
            "healthy": self.healthy,
            "ready": self.ready,
            "checks": serialized_checks,
        }


@dataclass(frozen=True, slots=True)
class ModelAvailability:
    """Result supplied by the voice registry when that integration is available."""

    status: DiagnosticStatus
    installed_voice_ids: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status is DiagnosticStatus.OK and not self.installed_voice_ids:
            raise ValueError("an OK model availability result must name an installed voice")


class ModelAvailabilityProbe(Protocol):
    def __call__(self, models_dir: Path) -> ModelAvailability: ...


class DiagnosticProbe(Protocol):
    """Boundary around platform inspection and temporary resource probes."""

    def operating_system(self) -> str: ...

    def architecture(self) -> str: ...

    def python_version(self) -> str: ...

    def sqlite_version(self) -> str: ...

    def logical_cpu_count(self) -> int | None: ...

    def total_memory_bytes(self) -> int | None: ...

    def free_disk_bytes(self, path: Path) -> int: ...

    def directory_writable(self, path: Path) -> bool: ...

    def port_available(self, bind: str, port: int) -> bool: ...


class SystemDiagnosticProbe:
    """Cross-platform best-effort implementation of :class:`DiagnosticProbe`."""

    def operating_system(self) -> str:
        system = platform.system() or sys.platform
        release = platform.release()
        version = platform.version()
        components = [component for component in (system, release, version) if component]
        return " ".join(components)

    def architecture(self) -> str:
        return platform.machine() or platform.architecture()[0]

    def python_version(self) -> str:
        return platform.python_version()

    def sqlite_version(self) -> str:
        return sqlite3.sqlite_version

    def logical_cpu_count(self) -> int | None:
        return os.cpu_count()

    def total_memory_bytes(self) -> int | None:
        if sys.platform == "win32":
            return _windows_total_memory_bytes()

        try:
            page_size = os.sysconf("SC_PAGE_SIZE")
            physical_pages = os.sysconf("SC_PHYS_PAGES")
        except (AttributeError, OSError, ValueError):
            return None
        if not isinstance(page_size, int) or not isinstance(physical_pages, int):
            return None
        if page_size <= 0 or physical_pages <= 0:
            return None
        return page_size * physical_pages

    def free_disk_bytes(self, path: Path) -> int:
        existing_path = _nearest_existing_path(path)
        return shutil.disk_usage(existing_path).free

    def directory_writable(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        try:
            with tempfile.NamedTemporaryFile(prefix=".article-reader-doctor-", dir=path):
                pass
        except OSError:
            return False
        return True

    def port_available(self, bind: str, port: int) -> bool:
        try:
            addresses = socket.getaddrinfo(bind, port, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False

        attempted: set[tuple[int, tuple[object, ...]]] = set()
        for family, socket_type, protocol, _, address in addresses:
            identity = (family, tuple(address))
            if identity in attempted:
                continue
            attempted.add(identity)
            try:
                with socket.socket(family, socket_type, protocol) as candidate:
                    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    candidate.bind(address)
            except OSError:
                continue
            return True
        return False


def _nearest_existing_path(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing parent found for {path}")
        candidate = parent
    return candidate


def _windows_total_memory_bytes() -> int | None:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query_memory = kernel32.GlobalMemoryStatusEx
        query_memory.argtypes = [ctypes.POINTER(MemoryStatusEx)]
        query_memory.restype = ctypes.c_int
        if not query_memory(ctypes.byref(status)):
            return None
    except (AttributeError, OSError, ValueError):
        return None
    return int(status.ullTotalPhys) if status.ullTotalPhys > 0 else None


def _placeholder_model_availability(_: Path) -> ModelAvailability:
    return ModelAvailability(
        status=DiagnosticStatus.UNKNOWN,
        detail="Voice registry diagnostics are not connected yet.",
    )


def _information_check(
    name: str,
    operation: Callable[[], DiagnosticValue],
    *,
    unknown_detail: str,
) -> DiagnosticCheck:
    try:
        value = operation()
    except Exception as exc:  # A doctor report should preserve results from other probes.
        return DiagnosticCheck(
            name=name,
            status=DiagnosticStatus.UNKNOWN,
            value=None,
            detail=f"{unknown_detail}: {type(exc).__name__}: {exc}",
        )
    if value is None or value == "":
        return DiagnosticCheck(
            name=name,
            status=DiagnosticStatus.UNKNOWN,
            value=None,
            detail=unknown_detail,
        )
    return DiagnosticCheck(name=name, status=DiagnosticStatus.OK, value=value)


def _free_disk_check(settings: Settings, probe: DiagnosticProbe) -> DiagnosticCheck:
    try:
        free_bytes = probe.free_disk_bytes(settings.paths.data_dir)
    except Exception as exc:  # Keep diagnostics best-effort across restricted hosts.
        return DiagnosticCheck(
            name="free_disk_bytes",
            status=DiagnosticStatus.UNKNOWN,
            value=None,
            detail=f"Could not inspect free disk space: {type(exc).__name__}: {exc}",
        )

    minimum = settings.cache.min_free_disk_bytes
    if free_bytes < 0:
        return DiagnosticCheck(
            name="free_disk_bytes",
            status=DiagnosticStatus.UNKNOWN,
            value=None,
            detail="The platform returned an invalid negative free-space value.",
        )
    if free_bytes < minimum:
        return DiagnosticCheck(
            name="free_disk_bytes",
            status=DiagnosticStatus.ERROR,
            value=free_bytes,
            detail=f"Free space is below the configured minimum of {minimum} bytes.",
        )
    return DiagnosticCheck(
        name="free_disk_bytes",
        status=DiagnosticStatus.OK,
        value=free_bytes,
        detail=f"Measured for {settings.paths.data_dir}.",
    )


def _python_version_check(probe: DiagnosticProbe) -> DiagnosticCheck:
    try:
        version = probe.python_version()
        parts = version.split(".")
        major_minor = (int(parts[0]), int(parts[1]))
    except Exception as exc:
        return DiagnosticCheck(
            name="python_version",
            status=DiagnosticStatus.UNKNOWN,
            value=None,
            detail=f"Could not validate the Python version: {type(exc).__name__}: {exc}",
        )
    if major_minor != (3, 12):
        return DiagnosticCheck(
            name="python_version",
            status=DiagnosticStatus.ERROR,
            value=version,
            detail="This release supports CPython 3.12.x; create the locked project environment.",
        )
    return DiagnosticCheck(name="python_version", status=DiagnosticStatus.OK, value=version)


def _model_check(
    settings: Settings,
    model_probe: Callable[[Path], ModelAvailability],
) -> DiagnosticCheck:
    try:
        availability = model_probe(settings.paths.models_dir)
    except Exception as exc:  # Registry failures must not hide host diagnostics.
        return DiagnosticCheck(
            name="model_availability",
            status=DiagnosticStatus.UNKNOWN,
            value=(),
            detail=f"Could not inspect installed voices: {type(exc).__name__}: {exc}",
        )
    if not isinstance(availability, ModelAvailability):
        return DiagnosticCheck(
            name="model_availability",
            status=DiagnosticStatus.UNKNOWN,
            value=(),
            detail="The model availability probe returned an invalid result.",
        )
    return DiagnosticCheck(
        name="model_availability",
        status=availability.status,
        value=availability.installed_voice_ids,
        detail=availability.detail,
    )


def _audio_directory_check(settings: Settings, probe: DiagnosticProbe) -> DiagnosticCheck:
    try:
        writable = probe.directory_writable(settings.paths.audio_dir)
    except Exception as exc:  # Preserve all other diagnostic checks.
        return DiagnosticCheck(
            name="audio_directory_writable",
            status=DiagnosticStatus.ERROR,
            value=False,
            detail=f"Could not test {settings.paths.audio_dir}: {type(exc).__name__}: {exc}",
        )
    return DiagnosticCheck(
        name="audio_directory_writable",
        status=DiagnosticStatus.OK if writable else DiagnosticStatus.ERROR,
        value=writable,
        detail=(
            f"Audio directory is writable: {settings.paths.audio_dir}"
            if writable
            else f"Audio directory is missing or not writable: {settings.paths.audio_dir}"
        ),
    )


def _port_check(settings: Settings, probe: DiagnosticProbe) -> DiagnosticCheck:
    endpoint = f"{settings.server.bind}:{settings.server.port}"
    try:
        available = probe.port_available(settings.server.bind, settings.server.port)
    except Exception as exc:  # Preserve all other diagnostic checks.
        return DiagnosticCheck(
            name="server_port_available",
            status=DiagnosticStatus.ERROR,
            value=False,
            detail=f"Could not test {endpoint}: {type(exc).__name__}: {exc}",
        )
    return DiagnosticCheck(
        name="server_port_available",
        status=DiagnosticStatus.OK if available else DiagnosticStatus.ERROR,
        value=available,
        detail=(
            f"Port {endpoint} is {'available' if available else 'already in use or unavailable'}."
        ),
    )


def collect_diagnostics(
    settings: Settings,
    *,
    probe: DiagnosticProbe | None = None,
    model_probe: ModelAvailabilityProbe | None = None,
) -> DiagnosticReport:
    """Collect a complete best-effort host report.

    Failed optional inspections become ``unknown`` checks. Failures that prevent the
    configured app from writing audio or binding its server are errors and make
    ``report.healthy`` false.
    """

    if not isinstance(settings, Settings):
        raise TypeError("settings must be an article_reader.config.Settings instance")

    active_probe = probe or SystemDiagnosticProbe()
    active_model_probe: Callable[[Path], ModelAvailability] = (
        model_probe or _placeholder_model_availability
    )

    checks = (
        _information_check(
            "operating_system",
            active_probe.operating_system,
            unknown_detail="Could not detect the operating system.",
        ),
        _information_check(
            "architecture",
            active_probe.architecture,
            unknown_detail="Could not detect the CPU architecture.",
        ),
        _python_version_check(active_probe),
        _information_check(
            "sqlite_version",
            active_probe.sqlite_version,
            unknown_detail="Could not detect the SQLite version.",
        ),
        _information_check(
            "logical_cpus",
            active_probe.logical_cpu_count,
            unknown_detail="Logical CPU count is unavailable.",
        ),
        _information_check(
            "total_ram_bytes",
            active_probe.total_memory_bytes,
            unknown_detail="Total physical memory is unavailable.",
        ),
        _free_disk_check(settings, active_probe),
        _model_check(settings, active_model_probe),
        _audio_directory_check(settings, active_probe),
        _port_check(settings, active_probe),
    )
    return DiagnosticReport(checks=checks)


__all__ = [
    "DiagnosticCheck",
    "DiagnosticProbe",
    "DiagnosticReport",
    "DiagnosticStatus",
    "ModelAvailability",
    "ModelAvailabilityProbe",
    "SystemDiagnosticProbe",
    "collect_diagnostics",
]
