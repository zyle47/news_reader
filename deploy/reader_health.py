"""No-auth, no-session health over the same private Unix socket used by Tor."""

import http.client
import socket
import sys
from pathlib import Path


class UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect("/run/reader-http/http.sock")


try:
    hostname = Path("/run/reader-onion/hostname").read_text(encoding="ascii").strip()
    connection = UnixHTTPConnection(hostname, timeout=3)
    connection.request("GET", "/health/ready", headers={"Host": hostname})
    response = connection.getresponse()
    healthy = response.status == 200
    response.read(128)
    connection.close()
    sys.exit(0 if healthy else 1)
except (OSError, ValueError, http.client.HTTPException):
    sys.exit(1)
