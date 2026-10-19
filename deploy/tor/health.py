"""Authenticate locally to Tor control and check bootstrap, not outside reachability."""

import socket
import sys
from pathlib import Path

try:
    cookie = Path("/var/lib/tor/control_auth_cookie").read_bytes()
    if len(cookie) != 32:
        raise ValueError("Missing control cookie")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect("/var/lib/tor/control.sock")
        with connection.makefile("rwb", buffering=0) as stream:
            stream.write(b"AUTHENTICATE " + cookie.hex().encode("ascii") + b"\r\n")
            if stream.readline(1024).strip() != b"250 OK":
                raise ValueError("Control authentication failed")
            stream.write(b"GETINFO status/bootstrap-phase\r\n")
            response = stream.readline(4096)
            healthy = b"PROGRESS=100" in response
    sys.exit(0 if healthy else 1)
except (OSError, ValueError):
    sys.exit(1)
