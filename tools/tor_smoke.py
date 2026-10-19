"""Opt-in HTTP smoke over UDS or a disposable Tor SOCKS client; never logs secrets.

Use --password-stdin to exercise login. Generic HTTP libraries do not model Tor Browser's
onion Secure-cookie exception: this tool explicitly returns that cookie only to this one host.
It is transport/API evidence, not a physical browser cookie acceptance test.
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
from http.cookies import SimpleCookie
from pathlib import Path


def exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            raise OSError("SOCKS connection closed")
        result.extend(part)
    return bytes(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hostname", required=True)
    parser.add_argument("--socket", default="/run/reader-http/http.sock")
    parser.add_argument("--socks", help="SOCKS endpoint host:port, with remote onion resolution")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument(
        "--article-url", help="Optional public English test article; leaves it in the owner library"
    )
    arguments = parser.parse_args()
    hostname = arguments.hostname
    if len(hostname) != 62 or not hostname.endswith(".onion"):
        parser.error("Expected canonical v3 hostname")
    password = sys.stdin.readline().rstrip("\n") if arguments.password_stdin else None

    class Connection(http.client.HTTPConnection):
        def connect(self) -> None:
            if arguments.socks:
                host, port = arguments.socks.rsplit(":", 1)
                self.sock = socket.create_connection((host, int(port)), timeout=90)
                self.sock.sendall(b"\x05\x01\x00")
                if exact(self.sock, 2) != b"\x05\x00":
                    raise OSError("SOCKS negotiation failed")
                encoded = hostname.encode("ascii")
                self.sock.sendall(
                    b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + b"\x00\x50"
                )
                reply = exact(self.sock, 4)
                if reply[1] != 0:
                    raise OSError("Tor could not reach the onion service")
                length = {1: 4, 4: 16}.get(reply[3])
                if length is None:
                    length = exact(self.sock, 1)[0]
                exact(self.sock, length + 2)
            else:
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(15)
                self.sock.connect(str(Path(arguments.socket)))

    cookie: str | None = None

    def request(
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        authenticated: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes, str | None]:
        headers = {
            "Host": hostname,
            "Origin": f"http://{hostname}",
            "Content-Type": "application/json",
        }
        if authenticated and cookie:
            headers["Cookie"] = cookie
        headers.update(extra_headers or {})
        body = json.dumps(payload).encode() if payload is not None else None
        connection = Connection(hostname)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Set-Cookie")
        finally:
            connection.close()

    assert request("GET", "/health/ready")[0] == 200, "Reader is not ready"
    assert request("GET", "/api/readings")[0] == 401, "Anonymous library access was allowed"
    assert request("GET", "/")[0] == 200
    if password is not None:
        status, _, raw_cookie = request(
            "POST", "/api/auth/login", {"password": password, "label": "Deployment smoke"}
        )
        assert status == 200 and raw_cookie, f"Login failed ({status})"
        parsed: SimpleCookie[str] = SimpleCookie()
        parsed.load(raw_cookie)
        assert parsed["article_reader_owner"]["secure"]
        cookie = parsed["article_reader_owner"].OutputString(attrs=[])
        assert request("GET", "/api/readings", authenticated=True)[0] == 200
        assert request("GET", "/api/sessions", authenticated=True)[0] == 200
        if arguments.article_url:
            status, body, _ = request(
                "POST",
                "/api/readings",
                {"url": arguments.article_url, "language": "en"},
                authenticated=True,
            )
            assert status == 202, f"Submission failed ({status})"
            reading_id = json.loads(body)["reading_id"]
            deadline = time.monotonic() + 120
            while True:
                status, body, _ = request("GET", f"/api/readings/{reading_id}", authenticated=True)
                detail = json.loads(body)
                assert detail["state"] not in {
                    "failed",
                    "needs_review",
                    "needs_language_override",
                }, "Article needs review or could not be prepared"
                if detail["state"] == "ready_for_voice":
                    break
                assert time.monotonic() < deadline, "Preparation timed out"
                time.sleep(1)
            status, body, _ = request(
                "POST",
                f"/api/readings/{reading_id}/renditions",
                {"voice_id": "en_US-ljspeech-medium"},
                authenticated=True,
            )
            assert status == 202
            rendition_id = json.loads(body)["rendition_id"]
            while True:
                status, body, _ = request(
                    "GET", f"/api/renditions/{rendition_id}/manifest", authenticated=True
                )
                manifest = json.loads(body)
                assert manifest["state"] != "failed", "Speech generation failed"
                if manifest["ready_prefix_count"]:
                    audio_url = manifest["chunks"][0]["audio_url"]
                    break
                assert time.monotonic() < deadline, "Speech timed out"
                time.sleep(1)
            status, body, _ = request(
                "GET", audio_url, authenticated=True, extra_headers={"Range": "bytes=0-9"}
            )
            assert status == 206 and body.startswith(b"RIFF") and len(body) == 10
            assert request("HEAD", audio_url, authenticated=True)[0] == 200
            assert request("HEAD", audio_url)[0] == 401
            print(
                "PASS: direct public article fetching, extraction, real speech, "
                "authenticated Tor Range/HEAD"
            )
        assert request("POST", "/api/logout", {}, authenticated=True)[0] == 204
        assert request("GET", "/api/readings", authenticated=True)[0] == 401
    print(
        "PASS: onion transport, readiness, anonymous denial"
        + (", login, owner access, logout" if password else "")
    )


if __name__ == "__main__":
    main()
