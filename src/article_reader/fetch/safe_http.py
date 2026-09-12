"""SSRF-resistant HTTP fetcher with DNS-to-connection address pinning."""

from __future__ import annotations

import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol, cast
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

import urllib3
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.response import BaseHTTPResponse
from urllib3.util import Timeout

from article_reader.application.ports.fetch import FetchedPage

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_ALLOWED_MEDIA_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_DNS_LABEL: Final = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AMBIGUOUS_NUMERIC_HOST: Final = re.compile(r"^(?:0x[0-9a-f]+|[0-9.]+)$", re.IGNORECASE)
_HTML_MARKER: Final = re.compile(rb"<(?:!doctype\s+html|html\b|head\b|body\b|article\b)", re.I)
_USER_AGENT: Final = "ArticleReader/0.1 (local personal article reader)"
_READ_SIZE: Final = 64 * 1024


class FetchErrorCode(StrEnum):
    INVALID_URL = "FETCH_INVALID_URL"
    BLOCKED_DESTINATION = "FETCH_BLOCKED_DESTINATION"
    DNS_FAILED = "FETCH_DNS_FAILED"
    TIMEOUT = "FETCH_TIMEOUT"
    NETWORK = "FETCH_NETWORK_FAILED"
    REDIRECT = "FETCH_REDIRECT_INVALID"
    ACCESS_DENIED = "FETCH_ACCESS_DENIED"
    RATE_LIMITED = "FETCH_RATE_LIMITED"
    SERVER_ERROR = "FETCH_SERVER_ERROR"
    HTTP_STATUS = "FETCH_HTTP_STATUS"
    UNSUPPORTED_CONTENT = "FETCH_UNSUPPORTED_CONTENT"
    RESPONSE_TOO_LARGE = "FETCH_RESPONSE_TOO_LARGE"


class ArticleFetchError(RuntimeError):
    """A controlled fetch-boundary failure safe to present to a local user."""

    def __init__(self, code: FetchErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    fetch_url: str
    scheme: str
    hostname: str
    port: int
    request_target: str
    host_header: str


class AddressResolver(Protocol):
    def resolve(self, hostname: str, port: int) -> tuple[IPAddress, ...]: ...


class PinnedResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, amount: int) -> bytes: ...

    def close(self) -> None: ...


class PinnedTransport(Protocol):
    def request(
        self,
        target: ValidatedTarget,
        address: IPAddress,
        headers: Mapping[str, str],
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> PinnedResponse: ...


def _url_error(code: FetchErrorCode, message: str) -> ArticleFetchError:
    return ArticleFetchError(code, message)


def _normalize_hostname(parsed: SplitResult) -> str:
    hostname = parsed.hostname
    if hostname is None or not hostname:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL requires a hostname")
    if "%" in hostname:
        raise _url_error(FetchErrorCode.INVALID_URL, "scoped or percent-encoded hosts are rejected")
    hostname = hostname.rstrip(".")
    if not hostname:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL requires a hostname")
    try:
        ipaddress.ip_address(hostname)
        return hostname.casefold()
    except ValueError:
        pass
    if _AMBIGUOUS_NUMERIC_HOST.fullmatch(hostname):
        raise _url_error(
            FetchErrorCode.INVALID_URL,
            "ambiguous numeric host forms are rejected",
        )
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL hostname is invalid") from error
    if len(ascii_hostname) > 253 or any(
        not _DNS_LABEL.fullmatch(label) for label in ascii_hostname.split(".")
    ):
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL hostname is invalid")
    return ascii_hostname


def validate_article_url(url: str, *, max_characters: int) -> ValidatedTarget:
    """Validate URL syntax without resolving or opening it."""

    if not isinstance(url, str) or not url or url != url.strip():
        raise _url_error(
            FetchErrorCode.INVALID_URL,
            "article URL must be a non-empty string without outer whitespace",
        )
    if len(url) > max_characters:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL exceeds the configured limit")
    if any(ord(character) < 33 for character in url):
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL contains whitespace or controls")
    try:
        parsed = urlsplit(url)
        explicit_port = parsed.port
    except ValueError as error:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL is malformed") from error
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL cannot contain credentials")
    hostname = _normalize_hostname(parsed)
    port = explicit_port if explicit_port is not None else (443 if scheme == "https" else 80)
    if port not in {80, 443}:
        raise _url_error(FetchErrorCode.INVALID_URL, "article URL port must be 80 or 443")

    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_authority = (
        f"{rendered_host}:{explicit_port}" if explicit_port is not None else rendered_host
    )
    path = parsed.path or "/"
    fetch_url = urlunsplit((scheme, rendered_authority, path, parsed.query, ""))
    request_target = urlunsplit(("", "", path, parsed.query, ""))
    host_header = rendered_authority
    return ValidatedTarget(
        fetch_url=fetch_url,
        scheme=scheme,
        hostname=hostname,
        port=port,
        request_target=request_target,
        host_header=host_header,
    )


def _is_public_address(address: IPAddress) -> bool:
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


class SocketAddressResolver:
    """Resolve once; callers connect to the returned address rather than resolving again."""

    def resolve(self, hostname: str, port: int) -> tuple[IPAddress, ...]:
        try:
            answers = socket.getaddrinfo(
                hostname,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as error:
            raise _url_error(
                FetchErrorCode.DNS_FAILED,
                "article hostname could not be resolved",
            ) from error
        unique: list[IPAddress] = []
        for family, _kind, _proto, _canonical, socket_address in answers:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                continue
            try:
                address = ipaddress.ip_address(socket_address[0])
            except ValueError:
                continue
            if address not in unique:
                unique.append(address)
        if not unique:
            raise _url_error(FetchErrorCode.DNS_FAILED, "article hostname has no usable address")
        return tuple(unique)


class _Urllib3Response:
    def __init__(self, response: BaseHTTPResponse, pool: HTTPConnectionPool) -> None:
        self._response = response
        self._pool = pool
        self.status = int(response.status)
        self.headers: Mapping[str, str] = {
            str(key).casefold(): str(value) for key, value in response.headers.items()
        }

    def read(self, amount: int) -> bytes:
        try:
            return self._response.read(amount, decode_content=False)
        except urllib3.exceptions.TimeoutError as error:
            raise _url_error(FetchErrorCode.TIMEOUT, "article response read timed out") from error
        except (urllib3.exceptions.HTTPError, OSError) as error:
            raise _url_error(FetchErrorCode.NETWORK, "article response read failed") from error

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._pool.close()


class Urllib3PinnedTransport:
    """Connect to an already-validated IP while authenticating the original TLS host."""

    def request(
        self,
        target: ValidatedTarget,
        address: IPAddress,
        headers: Mapping[str, str],
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> PinnedResponse:
        timeout = Timeout(connect=connect_timeout_seconds, read=read_timeout_seconds)
        pool: HTTPConnectionPool
        if target.scheme == "https":
            pool = HTTPSConnectionPool(
                str(address),
                target.port,
                timeout=timeout,
                maxsize=1,
                block=True,
                retries=False,
                assert_hostname=target.hostname,
                server_hostname=target.hostname,
                ssl_context=ssl.create_default_context(),
            )
        else:
            pool = HTTPConnectionPool(
                str(address),
                target.port,
                timeout=timeout,
                maxsize=1,
                block=True,
                retries=False,
            )
        try:
            response = pool.urlopen(
                "GET",
                target.request_target,
                headers=dict(headers),
                redirect=False,
                retries=False,
                preload_content=False,
                decode_content=False,
                assert_same_host=False,
                timeout=timeout,
            )
        except urllib3.exceptions.TimeoutError as error:
            pool.close()
            raise _url_error(FetchErrorCode.TIMEOUT, "article request timed out") from error
        except (urllib3.exceptions.HTTPError, OSError) as error:
            pool.close()
            raise _url_error(FetchErrorCode.NETWORK, "article request failed") from error
        return _Urllib3Response(response, pool)


def _bounded_decompress(data: bytes, *, encoding: str, maximum: int) -> bytes:
    def attempt(window_bits: int) -> bytes:
        decoder = zlib.decompressobj(window_bits)
        output = bytearray()
        for offset in range(0, len(data), _READ_SIZE):
            remaining = maximum - len(output)
            decoded = decoder.decompress(data[offset : offset + _READ_SIZE], remaining + 1)
            if len(decoded) > remaining or decoder.unconsumed_tail:
                raise _url_error(
                    FetchErrorCode.RESPONSE_TOO_LARGE,
                    "decoded article response exceeds the configured limit",
                )
            output.extend(decoded)
        remaining = maximum - len(output)
        tail = decoder.flush(remaining + 1)
        if len(tail) > remaining:
            raise _url_error(
                FetchErrorCode.RESPONSE_TOO_LARGE,
                "decoded article response exceeds the configured limit",
            )
        output.extend(tail)
        if not decoder.eof or decoder.unused_data:
            raise _url_error(
                FetchErrorCode.UNSUPPORTED_CONTENT,
                "article response has an invalid compressed body",
            )
        return bytes(output)

    try:
        if encoding in {"gzip", "x-gzip"}:
            return attempt(16 + zlib.MAX_WBITS)
        if encoding == "deflate":
            try:
                return attempt(zlib.MAX_WBITS)
            except zlib.error:
                return attempt(-zlib.MAX_WBITS)
    except zlib.error as error:
        raise _url_error(
            FetchErrorCode.UNSUPPORTED_CONTENT,
            "article response has an invalid compressed body",
        ) from error
    raise _url_error(
        FetchErrorCode.UNSUPPORTED_CONTENT,
        "article response uses an unsupported content encoding",
    )


class SafeHttpArticleFetcher:
    """Retrieve bounded HTML through a manually validated redirect chain."""

    def __init__(
        self,
        *,
        resolver: AddressResolver | None = None,
        transport: PinnedTransport | None = None,
        connect_timeout_seconds: float = 5.0,
        overall_timeout_seconds: float = 25.0,
        max_redirects: int = 5,
        max_response_bytes: int = 5_242_880,
        max_decoded_bytes: int = 5_242_880,
        max_url_characters: int = 4_096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._resolver = resolver or SocketAddressResolver()
        self._transport = transport or Urllib3PinnedTransport()
        self._connect_timeout = connect_timeout_seconds
        self._overall_timeout = overall_timeout_seconds
        self._max_redirects = max_redirects
        self._max_response_bytes = max_response_bytes
        self._max_decoded_bytes = max_decoded_bytes
        self._max_url_characters = max_url_characters
        self._clock = clock

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise _url_error(FetchErrorCode.TIMEOUT, "article fetch exceeded its overall timeout")
        return remaining

    def _resolve_before_deadline(
        self, target: ValidatedTarget, deadline: float
    ) -> tuple[IPAddress, ...]:
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put((True, self._resolver.resolve(target.hostname, target.port)))
            except Exception as error:  # delivered back to the caller thread
                result_queue.put((False, error))

        thread = threading.Thread(target=run, name="article-reader-dns", daemon=True)
        thread.start()
        thread.join(self._remaining(deadline))
        if thread.is_alive():
            raise _url_error(FetchErrorCode.TIMEOUT, "article DNS resolution timed out")
        success, result = result_queue.get_nowait()
        if not success:
            if isinstance(result, ArticleFetchError):
                raise result
            cause = cast(BaseException, result)
            raise _url_error(
                FetchErrorCode.DNS_FAILED,
                "article hostname could not be resolved",
            ) from cause
        addresses = cast(tuple[IPAddress, ...], result)
        if not addresses:
            raise _url_error(FetchErrorCode.DNS_FAILED, "article hostname has no usable address")
        if any(not _is_public_address(address) for address in addresses):
            raise _url_error(
                FetchErrorCode.BLOCKED_DESTINATION,
                "article hostname resolves to a non-public destination",
            )
        return addresses

    def _open(
        self,
        target: ValidatedTarget,
        addresses: tuple[IPAddress, ...],
        deadline: float,
    ) -> PinnedResponse:
        headers = {
            "Host": target.host_header,
            "User-Agent": _USER_AGENT,
            "Accept": "text/html, application/xhtml+xml;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        last_error: ArticleFetchError | None = None
        for address in addresses:
            remaining = self._remaining(deadline)
            try:
                return self._transport.request(
                    target,
                    address,
                    headers,
                    connect_timeout_seconds=min(self._connect_timeout, remaining),
                    read_timeout_seconds=remaining,
                )
            except ArticleFetchError as error:
                if error.code not in {FetchErrorCode.NETWORK, FetchErrorCode.TIMEOUT}:
                    raise
                last_error = error
        if last_error is not None:
            raise last_error
        raise _url_error(FetchErrorCode.NETWORK, "article request failed")

    def _read_body(self, response: PinnedResponse, deadline: float) -> tuple[bytes, int]:
        raw_length = response.headers.get("content-length")
        expected_length: int | None = None
        if raw_length is not None:
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise _url_error(
                    FetchErrorCode.UNSUPPORTED_CONTENT,
                    "article response has an invalid Content-Length",
                )
            expected_length = int(raw_length)
            if expected_length > self._max_response_bytes:
                raise _url_error(
                    FetchErrorCode.RESPONSE_TOO_LARGE,
                    "article response exceeds the configured transfer limit",
                )
        wire = bytearray()
        while True:
            self._remaining(deadline)
            chunk = response.read(min(_READ_SIZE, self._max_response_bytes - len(wire) + 1))
            if not chunk:
                break
            wire.extend(chunk)
            if len(wire) > self._max_response_bytes:
                raise _url_error(
                    FetchErrorCode.RESPONSE_TOO_LARGE,
                    "article response exceeds the configured transfer limit",
                )
        if not wire:
            raise _url_error(FetchErrorCode.UNSUPPORTED_CONTENT, "article response body is empty")
        if expected_length is not None and len(wire) != expected_length:
            raise _url_error(
                FetchErrorCode.UNSUPPORTED_CONTENT,
                "article response ended before its declared Content-Length",
            )
        encoding = response.headers.get("content-encoding", "identity").strip().casefold()
        if encoding in {"", "identity"}:
            if len(wire) > self._max_decoded_bytes:
                raise _url_error(
                    FetchErrorCode.RESPONSE_TOO_LARGE,
                    "decoded article response exceeds the configured limit",
                )
            return bytes(wire), len(wire)
        return (
            _bounded_decompress(bytes(wire), encoding=encoding, maximum=self._max_decoded_bytes),
            len(wire),
        )

    def _read_body_before_deadline(
        self,
        response: PinnedResponse,
        deadline: float,
    ) -> tuple[bytes, int]:
        result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result_queue.put((True, self._read_body(response, deadline)))
            except Exception as error:
                result_queue.put((False, error))

        thread = threading.Thread(target=run, name="article-reader-response", daemon=True)
        thread.start()
        thread.join(self._remaining(deadline))
        if thread.is_alive():
            response.close()
            raise _url_error(FetchErrorCode.TIMEOUT, "article fetch exceeded its overall timeout")
        success, result = result_queue.get_nowait()
        if not success:
            if isinstance(result, ArticleFetchError):
                raise result
            cause = cast(Exception, result)
            raise _url_error(FetchErrorCode.NETWORK, "article response read failed") from cause
        return cast(tuple[bytes, int], result)

    def fetch(self, submitted_url: str) -> FetchedPage:
        deadline = self._clock() + self._overall_timeout
        current_url = submitted_url
        chain: list[str] = []
        seen: set[str] = set()

        while True:
            target = validate_article_url(current_url, max_characters=self._max_url_characters)
            if target.fetch_url in seen:
                raise _url_error(FetchErrorCode.REDIRECT, "article redirect loop detected")
            seen.add(target.fetch_url)
            chain.append(target.fetch_url)
            addresses = self._resolve_before_deadline(target, deadline)
            response = self._open(target, addresses, deadline)
            try:
                if response.status in _REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise _url_error(
                            FetchErrorCode.REDIRECT,
                            "article redirect response has no Location header",
                        )
                    if len(chain) - 1 >= self._max_redirects:
                        raise _url_error(
                            FetchErrorCode.REDIRECT,
                            "article redirect limit exceeded",
                        )
                    current_url = urljoin(target.fetch_url, location)
                    continue
                if response.status in {401, 403}:
                    raise _url_error(
                        FetchErrorCode.ACCESS_DENIED,
                        "article access was denied; use the manual-text path",
                    )
                if response.status == 429:
                    raise _url_error(
                        FetchErrorCode.RATE_LIMITED,
                        "article source is rate-limiting requests; try later",
                    )
                if 500 <= response.status <= 599:
                    raise _url_error(
                        FetchErrorCode.SERVER_ERROR,
                        "article source returned a server error",
                    )
                if not 200 <= response.status <= 299:
                    raise _url_error(
                        FetchErrorCode.HTTP_STATUS,
                        f"article source returned HTTP status {response.status}",
                    )
                raw_media_type = response.headers.get("content-type", "")
                media_type = raw_media_type.split(";", 1)[0].strip().casefold()
                if media_type and media_type not in _ALLOWED_MEDIA_TYPES:
                    raise _url_error(
                        FetchErrorCode.UNSUPPORTED_CONTENT,
                        "article response is not HTML",
                    )
                body, response_bytes = self._read_body_before_deadline(response, deadline)
                prefix = body[:16_384].lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
                if b"\x00" in body[:16_384] or not _HTML_MARKER.search(prefix):
                    raise _url_error(
                        FetchErrorCode.UNSUPPORTED_CONTENT,
                        "article response content does not appear to be HTML",
                    )
                return FetchedPage(
                    submitted_url=submitted_url,
                    final_url=target.fetch_url,
                    redirect_chain=tuple(chain),
                    status_code=response.status,
                    media_type=media_type or "text/html",
                    body=body,
                    response_bytes=response_bytes,
                )
            finally:
                response.close()


__all__ = [
    "AddressResolver",
    "ArticleFetchError",
    "FetchErrorCode",
    "PinnedResponse",
    "PinnedTransport",
    "SafeHttpArticleFetcher",
    "SocketAddressResolver",
    "Urllib3PinnedTransport",
    "ValidatedTarget",
    "validate_article_url",
]
