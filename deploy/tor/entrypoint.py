"""Run Tor, atomically publish only its hostname, and forward termination signals."""

import os
import signal
import subprocess
import time
from pathlib import Path

os.umask(0o077)
process = subprocess.Popen(["tor", "-f", "/etc/tor/torrc"])


def stop(signum: int, _frame: object) -> None:
    if process.poll() is None:
        process.send_signal(signum)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
source = Path("/var/lib/tor/reader/hostname")
destination = Path("/run/reader-onion/hostname")
deadline = time.monotonic() + 60
published = False
while process.poll() is None:
    if not published:
        try:
            hostname = source.read_text(encoding="ascii")
            if len(hostname) != 63 or not hostname.endswith(".onion\n"):
                raise ValueError("Unexpected Tor v3 hostname")
            temporary = destination.with_suffix(".tmp")
            temporary.write_text(hostname, encoding="ascii")
            temporary.chmod(0o640)
            temporary.replace(destination)
            published = True
        except FileNotFoundError:
            if time.monotonic() >= deadline:
                process.terminate()
                raise SystemExit("Tor did not create a hostname within 60 seconds.") from None
    time.sleep(0.2)
raise SystemExit(process.returncode)
