"""Single-instance process lock: duplicate refusal and stale-lock recovery."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from article_reader.storage.process_lock import (
    InstanceLock,
    InstanceLockError,
    _process_is_alive,
)


def test_acquire_and_release_round_trip(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "instance.lock")
    lock.acquire()
    assert lock.path.is_file()
    lock.release()
    assert not lock.path.exists()


def test_second_live_process_is_refused(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "instance.lock")
    first.acquire()
    second = InstanceLock(tmp_path / "instance.lock")
    with pytest.raises(InstanceLockError):
        second.acquire()
    first.release()


def test_context_manager_releases_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    with InstanceLock(path):
        assert path.is_file()
    assert not path.exists()


def test_stale_lock_from_a_dead_process_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    # A PID that is extremely unlikely to be a live process on this host.
    dead_pid = 999_999
    path.write_text(f"{dead_pid}\n", encoding="ascii")

    lock = InstanceLock(path)
    lock.acquire()  # Should reap the stale lock and succeed.
    assert path.read_text(encoding="ascii").strip() == str(os.getpid())
    lock.release()


def test_liveness_check_reports_a_real_exited_process_as_dead() -> None:
    """Regression test for a Windows ``ctypes`` truncation bug.

    ``OpenProcess`` returns a pointer-sized ``HANDLE`` (8 bytes on 64-bit Windows). Without
    explicit ``argtypes``/``restype``, ctypes defaults to a 32-bit ``c_int`` return value and
    silently truncates the real handle, which can misreport an exited process's now-recycled
    PID as alive. A synthetic PID (as in the tests above) does not exercise this path
    reliably; only a real PID that the OS has just legitimately reused/reclaimed does.
    """

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    exited_pid = process.pid
    assert process.wait(timeout=10) == 0
    assert _process_is_alive(exited_pid) is False


def test_lock_with_unparsable_contents_is_not_silently_reaped(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    path.write_text("not-a-pid", encoding="ascii")
    lock = InstanceLock(path)
    with pytest.raises(InstanceLockError):
        lock.acquire()


def test_double_acquire_by_the_same_lock_object_is_a_no_op(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "instance.lock")
    lock.acquire()
    lock.acquire()
    lock.release()
    assert not lock.path.exists()
