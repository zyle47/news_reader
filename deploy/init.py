"""Explicit volume initialization; never recursively rewrites existing library ownership."""

import os
from pathlib import Path

DIRECTORIES = (
    ("/data", 10001, 10001, 0o700),
    ("/data/models", 10001, 10001, 0o750),
    ("/var/lib/tor", 10002, 10001, 0o700),
    ("/run/reader-onion", 10002, 10001, 0o2750),
    ("/run/reader-http", 10001, 10001, 0o2770),
)

for name, uid, gid, mode in DIRECTORIES:
    path = Path(name)
    if path.is_symlink():
        raise SystemExit(f"Refusing linked volume directory: {name}")
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)
    path.chmod(mode)
print("Reader volumes initialized.")
