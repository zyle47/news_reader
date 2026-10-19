"""Strict v3 onion public hostname validation; never reads private service keys."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import stat
import time
from pathlib import Path


def validate_onion_hostname(hostname: str) -> str:
    if re.fullmatch(r"[a-z2-7]{56}\.onion", hostname) is None:
        raise ValueError("Expected one canonical v3 onion hostname.")
    decoded = base64.b32decode(hostname[:-6].upper())
    public_key, checksum, version = decoded[:32], decoded[32:34], decoded[34:]
    expected = hashlib.sha3_256(b".onion checksum" + public_key + version).digest()[:2]
    if version != b"\x03" or checksum != expected:
        raise ValueError("Invalid v3 onion hostname checksum or version.")
    return hostname


def read_onion_hostname(path: Path, *, timeout: float = 0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            for parent in path.absolute().parents:
                if parent.is_symlink():
                    raise ValueError("Onion hostname path cannot contain symbolic links.")
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("Onion hostname must be a regular file.")
                raw = stream.read(65)
            if len(raw) > 63:
                raise ValueError("Onion hostname file is oversized.")
            return validate_onion_hostname(raw.decode("ascii").removesuffix("\n"))
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                raise ValueError("Onion hostname is missing; start reader-tor first.") from None
            time.sleep(0.25)


__all__ = ["read_onion_hostname", "validate_onion_hostname"]
