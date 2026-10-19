"""Single-instance process lock: duplicate refusal and stale-lock recovery."""

from __future__ import annotations

import json
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

# On Linux the lock file inode is kept after release (stable flock object).
# On Windows the PID file is removed on release.
_FILE_SURVIVES_RELEASE = not sys.platform.startswith("win")


def test_acquire_and_release_round_trip(tmp_path: Path) -> None:
    lock = InstanceLock(tmp_path / "instance.lock")
    lock.acquire()
    assert lock.path.is_file()
    lock.release()
    assert lock.path.exists() is _FILE_SURVIVES_RELEASE


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
    assert path.exists() is _FILE_SURVIVES_RELEASE


def test_stale_lock_from_a_dead_process_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    # A PID that is extremely unlikely to be a live process on this host.
    dead_pid = 999_999
    path.write_text(f"{dead_pid}\n", encoding="ascii")

    lock = InstanceLock(path)
    lock.acquire()  # Should succeed regardless of stale content.
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


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="unparsable-content refusal is part of the Windows PID-file path only; "
    "Linux flock ignores file contents",
)
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
    assert lock.path.exists() is _FILE_SURVIVES_RELEASE


def test_release_without_ownership_preserves_active_owner(tmp_path: Path) -> None:
    path = tmp_path / "instance.lock"
    with InstanceLock(path):
        InstanceLock(path).release()
        assert path.exists()
        with pytest.raises(InstanceLockError):
            InstanceLock(path).acquire()


# ---------------------------------------------------------------------------
# Linux flock subprocess tests
# ---------------------------------------------------------------------------

_HOLDER_SCRIPT = """\
import fcntl, json, os, sys, time
from pathlib import Path
path = Path(json.loads(sys.argv[1]))
fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\\n".encode())
os.fsync(fd)
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.readline()
"""

_HOLDER_SLEEP_SCRIPT = """\
import fcntl, json, os, sys, time
from pathlib import Path
path = Path(json.loads(sys.argv[1]))
fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.write(fd, f"{os.getpid()}\\n".encode())
os.fsync(fd)
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(60)
"""


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock is POSIX-only")
def test_subprocess_contention_blocked(tmp_path: Path) -> None:
    """A subprocess holding the flock prevents acquisition by a second process."""
    lock_path = tmp_path / "instance.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, json.dumps(str(lock_path))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == b"ready\n"
        with pytest.raises(InstanceLockError):
            InstanceLock(lock_path).acquire()
    finally:
        assert proc.stdin is not None
        proc.stdin.write(b"\n")
        proc.stdin.flush()
        proc.wait(timeout=10)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock is POSIX-only")
def test_kill_recovery(tmp_path: Path) -> None:
    """After the lock holder is killed the kernel releases the flock automatically."""
    lock_path = tmp_path / "instance.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SLEEP_SCRIPT, json.dumps(str(lock_path))],
        stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline() == b"ready\n"
        # Confirm the lock is held.
        with pytest.raises(InstanceLockError):
            InstanceLock(lock_path).acquire()
        proc.kill()
        proc.wait(timeout=10)
        # Kernel released the flock; we must be able to acquire on the same inode.
        lock = InstanceLock(lock_path)
        lock.acquire()
        assert lock.path.read_text(encoding="ascii").strip() == str(os.getpid())
        lock.release()
        assert lock_path.exists()  # inode kept after release
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="flock is POSIX-only")
def test_lock_file_inode_survives_release(tmp_path: Path) -> None:
    """The lock file is not unlinked on release; a subsequent acquire reuses the inode."""
    lock_path = tmp_path / "instance.lock"
    lock = InstanceLock(lock_path)
    lock.acquire()
    inode_before = lock_path.stat().st_ino
    lock.release()
    assert lock_path.exists()
    assert lock_path.stat().st_ino == inode_before
    # Reacquire on the same inode.
    lock.acquire()
    assert lock_path.stat().st_ino == inode_before
    lock.release()
