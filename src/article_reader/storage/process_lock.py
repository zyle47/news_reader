"""Single-instance process lock for one data directory, with stale-lock recovery.

The project plan requires refusing a duplicate worker for the same data directory and
recovering a lock left behind by a process that no longer exists, based on actual process
ownership rather than lease expiry alone. This is a cooperative, best-effort guard (any
process willing to delete the lock file can bypass it); it is not a security boundary.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from types import TracebackType


class InstanceLockError(RuntimeError):
    """Raised when another live process already owns the instance lock."""


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # ``HANDLE`` is pointer-sized (8 bytes on 64-bit Windows). Without explicit
        # argtypes/restype, ctypes assumes a 32-bit ``c_int`` return value and silently
        # truncates the real handle, which can misreport a dead PID as alive whenever the
        # (garbled) truncated value happens to be non-zero. Both signatures must be exact.
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL

        query_limited_information = 0x1000
        still_active = 259
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False
        try:
            # A terminated process's kernel object can remain open()able until every
            # handle to it is closed, so a successful OpenProcess alone does not mean the
            # process is still running: it may simply not have been reaped yet. The exit
            # code is the authoritative signal.
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


class InstanceLock:
    """Holds an exclusive lock file for the lifetime of this process."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError("instance lock path must be an absolute pathlib.Path")
        self._path = path
        self._handle: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self._try_acquire(allow_stale_recovery=True)

    def _try_acquire(self, *, allow_stale_recovery: bool) -> None:
        try:
            descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if allow_stale_recovery and self._reap_if_stale():
                self._try_acquire(allow_stale_recovery=False)
                return
            raise InstanceLockError(
                f"another Article Reader process already owns the data directory "
                f"{self._path.parent}; stop it first or choose a different --data-dir"
            ) from None
        else:
            os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            os.fsync(descriptor)
            self._handle = descriptor

    def _reap_if_stale(self) -> bool:
        try:
            content = self._path.read_text(encoding="ascii").strip()
            pid = int(content)
        except (OSError, ValueError):
            return False
        if _process_is_alive(pid):
            return False
        try:
            self._path.unlink()
        except OSError:
            return False
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                os.close(self._handle)
            self._handle = None
        with contextlib.suppress(OSError):
            self._path.unlink(missing_ok=True)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


__all__ = ["InstanceLock", "InstanceLockError"]
