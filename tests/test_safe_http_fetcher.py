from __future__ import annotations

import gzip
import ipaddress
import time
from collections.abc import Mapping
from typing import Any, ClassVar

import pytest

import article_reader.fetch.safe_http as safe_http
from article_reader.fetch.safe_http import (
    ArticleFetchError,
    FetchErrorCode,
    PinnedResponse,
    SafeHttpArticleFetcher,
    Urllib3PinnedTransport,
    ValidatedTarget,
    validate_article_url,
)

_PUBLIC_1 = ipaddress.ip_address("93.184.216.34")
_PUBLIC_2 = ipaddress.ip_address("1.1.1.1")
_HTML = b"<html><body><article><p>A useful article response.</p></article></body></html>"


class _Resolver:
    def __init__(
        self,
        answers: Mapping[
            str,
            tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
        ],
    ) -> None:
        self.answers = answers
        self.calls: list[tuple[str, int]] = []

    def resolve(
        self, hostname: str, port: int
    ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
        self.calls.append((hostname, port))
        return self.answers[hostname]


class _Response:
    def __init__(
        self,
        status: int = 200,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = _HTML,
    ) -> None:
        self.status: int = status
        self.headers: Mapping[str, str] = {
            key.casefold(): value for key, value in (headers or {}).items()
        }
        self._body = body
        self._offset = 0
        self.closed = False

    def read(self, amount: int) -> bytes:
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self, responses: list[PinnedResponse | ArticleFetchError]) -> None:
        self.responses = responses
        self.calls: list[tuple[ValidatedTarget, object, Mapping[str, str], float, float]] = []

    def request(
        self,
        target: ValidatedTarget,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        headers: Mapping[str, str],
        *,
        connect_timeout_seconds: float,
        read_timeout_seconds: float,
    ) -> PinnedResponse:
        self.calls.append((target, address, headers, connect_timeout_seconds, read_timeout_seconds))
        result = self.responses.pop(0)
        if isinstance(result, ArticleFetchError):
            raise result
        return result


def _fetcher(
    responses: list[PinnedResponse | ArticleFetchError],
    *,
    answers: Mapping[str, tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]] | None = None,
    **limits: Any,
) -> tuple[SafeHttpArticleFetcher, _Resolver, _Transport]:
    resolver = _Resolver(answers or {"example.com": (_PUBLIC_1,)})
    transport = _Transport(responses)
    fetcher = SafeHttpArticleFetcher(resolver=resolver, transport=transport, **limits)
    return fetcher, resolver, transport


@pytest.mark.parametrize(
    "url",
    [
        "",
        " https://example.com/a",
        "file:///tmp/a",
        "https://user:secret@example.com/a",
        "https://example.com:8443/a",
        "https://127.1/a",
        "https://2130706433/a",
        "https://0x7f000001/a",
        "https://bad_host.example/a",
    ],
)
def test_url_policy_rejects_ambiguous_or_unsafe_urls(url: str) -> None:
    with pytest.raises(ArticleFetchError) as caught:
        validate_article_url(url, max_characters=4_096)

    assert caught.value.code is FetchErrorCode.INVALID_URL


def test_fetch_preserves_query_removes_fragment_and_pins_connection() -> None:
    response = _Response(headers={"Content-Type": "text/html; charset=utf-8"})
    fetcher, resolver, transport = _fetcher([response])

    page = fetcher.fetch("https://Example.COM/story?edition=full#section")

    assert page.submitted_url.endswith("#section")
    assert page.final_url == "https://example.com/story?edition=full"
    assert page.redirect_chain == (page.final_url,)
    assert resolver.calls == [("example.com", 443)]
    target, address, headers, _connect, _read = transport.calls[0]
    assert address == _PUBLIC_1
    assert target.request_target == "/story?edition=full"
    assert headers["Host"] == "example.com"
    assert headers["Accept-Encoding"] == "identity"
    assert response.closed is True


@pytest.mark.parametrize(
    "addresses",
    [
        (ipaddress.ip_address("127.0.0.1"),),
        (ipaddress.ip_address("10.0.0.1"),),
        (ipaddress.ip_address("169.254.169.254"),),
        (ipaddress.ip_address("::1"),),
        (_PUBLIC_1, ipaddress.ip_address("192.168.1.1")),
    ],
)
def test_all_dns_answers_must_be_public(
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...],
) -> None:
    fetcher, _resolver, transport = _fetcher([], answers={"example.com": addresses})

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")

    assert caught.value.code is FetchErrorCode.BLOCKED_DESTINATION
    assert transport.calls == []


def test_redirect_is_resolved_and_validated_again() -> None:
    first = _Response(302, headers={"Location": "https://other.example/final?x=1"})
    second = _Response(headers={"Content-Type": "text/html"})
    answers = {"example.com": (_PUBLIC_1,), "other.example": (_PUBLIC_2,)}
    fetcher, resolver, transport = _fetcher([first, second], answers=answers)

    page = fetcher.fetch("https://example.com/start")

    assert resolver.calls == [("example.com", 443), ("other.example", 443)]
    assert [call[1] for call in transport.calls] == [_PUBLIC_1, _PUBLIC_2]
    assert page.final_url == "https://other.example/final?x=1"
    assert first.closed and second.closed


def test_redirect_to_private_destination_is_blocked_before_second_request() -> None:
    first = _Response(302, headers={"Location": "http://internal.example/admin"})
    answers = {
        "example.com": (_PUBLIC_1,),
        "internal.example": (ipaddress.ip_address("10.1.2.3"),),
    }
    fetcher, _resolver, transport = _fetcher([first], answers=answers)

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/start")

    assert caught.value.code is FetchErrorCode.BLOCKED_DESTINATION
    assert len(transport.calls) == 1
    assert first.closed


def test_redirect_loop_and_limit_are_controlled() -> None:
    loop = _Response(302, headers={"Location": "/start"})
    fetcher, _resolver, _transport = _fetcher([loop])
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/start")
    assert caught.value.code is FetchErrorCode.REDIRECT

    redirect = _Response(302, headers={"Location": "/next"})
    fetcher, _resolver, _transport = _fetcher([redirect], max_redirects=0)
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/start")
    assert caught.value.code is FetchErrorCode.REDIRECT


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, FetchErrorCode.ACCESS_DENIED),
        (403, FetchErrorCode.ACCESS_DENIED),
        (429, FetchErrorCode.RATE_LIMITED),
        (503, FetchErrorCode.SERVER_ERROR),
        (404, FetchErrorCode.HTTP_STATUS),
    ],
)
def test_http_failures_are_distinct(status: int, code: FetchErrorCode) -> None:
    fetcher, _resolver, _transport = _fetcher([_Response(status)])
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is code


def test_rejects_non_html_headers_and_non_html_content() -> None:
    fetcher, _resolver, _transport = _fetcher(
        [_Response(headers={"Content-Type": "application/pdf"}, body=b"%PDF-1.7")]
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.UNSUPPORTED_CONTENT

    fetcher, _resolver, _transport = _fetcher(
        [_Response(headers={"Content-Type": "text/html"}, body=b"plain text only")]
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.UNSUPPORTED_CONTENT


def test_wire_and_declared_length_limits_are_enforced() -> None:
    fetcher, _resolver, _transport = _fetcher(
        [_Response(headers={"Content-Length": "9999"})], max_response_bytes=100
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.RESPONSE_TOO_LARGE

    fetcher, _resolver, _transport = _fetcher(
        [_Response(body=b"<html>" + b"x" * 200)], max_response_bytes=100
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.RESPONSE_TOO_LARGE


def test_gzip_is_decoded_with_a_separate_output_limit() -> None:
    compressed = gzip.compress(_HTML)
    response = _Response(
        headers={"Content-Type": "text/html", "Content-Encoding": "gzip"},
        body=compressed,
    )
    fetcher, _resolver, _transport = _fetcher([response])
    assert fetcher.fetch("https://example.com/a").body == _HTML

    bomb = gzip.compress(b"<html>" + b"x" * 10_000 + b"</html>")
    fetcher, _resolver, _transport = _fetcher(
        [_Response(headers={"Content-Encoding": "gzip"}, body=bomb)],
        max_decoded_bytes=100,
    )
    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.RESPONSE_TOO_LARGE


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"Content-Encoding": "gzip"}, b"not-gzip"),
        ({"Content-Encoding": "br"}, _HTML),
        ({"Content-Length": str(len(_HTML) + 1)}, _HTML),
        ({"Content-Length": "12, 12"}, _HTML),
    ],
)
def test_malformed_or_unsupported_response_framing_is_controlled(
    headers: Mapping[str, str],
    body: bytes,
) -> None:
    fetcher, _resolver, _transport = _fetcher([_Response(headers=headers, body=body)])

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")

    assert caught.value.code is FetchErrorCode.UNSUPPORTED_CONTENT


def test_unexpected_response_read_error_is_controlled() -> None:
    class _BrokenResponse(_Response):
        def read(self, amount: int) -> bytes:
            del amount
            raise OSError("fixture socket failure")

    fetcher, _resolver, _transport = _fetcher([_BrokenResponse()])

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")

    assert caught.value.code is FetchErrorCode.NETWORK


def test_transport_failure_tries_the_next_validated_address() -> None:
    failure = ArticleFetchError(FetchErrorCode.NETWORK, "fixture failure")
    fetcher, _resolver, transport = _fetcher(
        [failure, _Response()], answers={"example.com": (_PUBLIC_1, _PUBLIC_2)}
    )

    assert fetcher.fetch("https://example.com/a").status_code == 200
    assert [call[1] for call in transport.calls] == [_PUBLIC_1, _PUBLIC_2]


def test_dns_resolution_is_bounded_by_the_overall_deadline() -> None:
    class _SlowResolver:
        def resolve(
            self, hostname: str, port: int
        ) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
            del hostname, port
            time.sleep(0.1)
            return (_PUBLIC_1,)

    fetcher = SafeHttpArticleFetcher(
        resolver=_SlowResolver(),
        transport=_Transport([]),
        overall_timeout_seconds=0.01,
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.TIMEOUT


def test_slow_response_body_is_bounded_by_the_overall_deadline() -> None:
    class _SlowResponse(_Response):
        def read(self, amount: int) -> bytes:
            del amount
            time.sleep(0.1)
            return _HTML

    response = _SlowResponse()
    fetcher, _resolver, _transport = _fetcher(
        [response],
        overall_timeout_seconds=0.02,
    )

    with pytest.raises(ArticleFetchError) as caught:
        fetcher.fetch("https://example.com/a")
    assert caught.value.code is FetchErrorCode.TIMEOUT
    assert response.closed is True


def test_urllib3_transport_pins_ip_but_authenticates_original_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _RawResponse:
        status = 200
        headers: ClassVar = {"Content-Type": "text/html"}

        def read(self, amount: int, *, decode_content: bool) -> bytes:
            del amount, decode_content
            return b""

        def close(self) -> None:
            captured["response_closed"] = True

    class _Pool:
        def __init__(self, host: str, port: int, **kwargs: object) -> None:
            captured["host"] = host
            captured["port"] = port
            captured["pool_kwargs"] = kwargs

        def urlopen(self, method: str, target: str, **kwargs: object) -> _RawResponse:
            captured["method"] = method
            captured["target"] = target
            captured["request_kwargs"] = kwargs
            return _RawResponse()

        def close(self) -> None:
            captured["pool_closed"] = True

    monkeypatch.setattr(safe_http, "HTTPSConnectionPool", _Pool)
    target = validate_article_url(
        "https://news.example/story?edition=full",
        max_characters=4_096,
    )
    response = Urllib3PinnedTransport().request(
        target,
        _PUBLIC_1,
        {"Host": "news.example"},
        connect_timeout_seconds=1,
        read_timeout_seconds=2,
    )
    response.close()

    assert captured["host"] == str(_PUBLIC_1)
    assert captured["port"] == 443
    pool_kwargs = captured["pool_kwargs"]
    assert isinstance(pool_kwargs, dict)
    assert pool_kwargs["assert_hostname"] == "news.example"
    assert pool_kwargs["server_hostname"] == "news.example"
    assert captured["target"] == "/story?edition=full"
    request_kwargs = captured["request_kwargs"]
    assert isinstance(request_kwargs, dict)
    assert request_kwargs["headers"] == {"Host": "news.example"}
    assert captured["response_closed"] is True
    assert captured["pool_closed"] is True
