"""Opt-in Linux-container contention/kill recovery check using a disposable volume only."""

from __future__ import annotations

import subprocess
import time
from uuid import uuid4


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments], check=check, text=True, capture_output=True, timeout=30
    )


def main() -> None:
    suffix = uuid4().hex[:12]
    volume = f"reader-lock-smoke-{suffix}"
    holder = f"reader-lock-holder-{suffix}"
    image = "reader-onion-app:local"
    code = (
        "from pathlib import Path; "
        "from article_reader.storage.process_lock import InstanceLock; "
        "lock=InstanceLock(Path('/data/instance.lock')); "
        "lock.acquire(); print('LOCKED',flush=True)"
    )
    docker("volume", "create", volume)
    try:
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--entrypoint",
            "python",
            "--volume",
            f"{volume}:/data",
            image,
            "-c",
            "import os; os.chown('/data',10001,10001)",
        )
        docker(
            "run",
            "-d",
            "--name",
            holder,
            "--network",
            "none",
            "--read-only",
            "--volume",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            code + "; import time; time.sleep(120)",
        )
        deadline = time.monotonic() + 15
        while "LOCKED" not in docker("logs", holder).stdout:
            if time.monotonic() >= deadline:
                raise RuntimeError("Test holder failed to acquire lock")
            time.sleep(0.1)
        before = docker(
            "exec",
            holder,
            "python",
            "-c",
            "import os; print(os.stat('/data/instance.lock').st_ino)",
        ).stdout
        contender = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--volume",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            code,
            check=False,
        )
        assert contender.returncode != 0 and "InstanceLockError" in contender.stderr
        docker("kill", "--signal", "KILL", holder)
        recovered = docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--volume",
            f"{volume}:/data",
            "--entrypoint",
            "python",
            image,
            "-c",
            code + "; import os; print(os.stat('/data/instance.lock').st_ino)",
        )
        assert recovered.stdout.splitlines()[-1] == before.strip()
        print(
            "PASS: container PID namespaces exclude concurrent ownership; "
            "SIGKILL recovers on the same inode."
        )
    finally:
        docker("rm", "-f", holder, check=False)
        docker("volume", "rm", volume)


if __name__ == "__main__":
    main()
