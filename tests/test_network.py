"""Private LAN address discovery and selection policy."""

from __future__ import annotations

import socket

import pytest

from article_reader.network import (
    LanAddressError,
    discover_private_lan_addresses,
    http_authority,
    is_private_lan_address,
    select_private_lan_address,
)


@pytest.mark.parametrize("address", ["192.168.1.20", "10.20.30.40", "172.16.2.3", "fd00::2"])
def test_private_lan_addresses_are_accepted(address: str) -> None:
    assert is_private_lan_address(address)


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "0.0.0.0", "169.254.1.1", "224.0.0.1", "8.8.8.8", "192.0.2.1"],
)
def test_non_lan_addresses_are_rejected(address: str) -> None:
    assert not is_private_lan_address(address)


def test_discovery_filters_and_canonicalizes_resolver_answers() -> None:
    def resolver(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.8.4", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fd00::4", 0, 0, 0)),
        ]

    found = discover_private_lan_addresses(
        resolver=resolver, source_probe=lambda _family, _target: None
    )
    assert found == ("192.168.8.4", "fd00::4")


def test_selection_requires_an_address_assigned_to_this_host() -> None:
    discovered = ("192.168.1.10", "192.168.1.20")
    assert select_private_lan_address(None, discovered) == "192.168.1.10"
    assert select_private_lan_address("192.168.1.20", discovered) == "192.168.1.20"
    with pytest.raises(LanAddressError, match="not assigned"):
        select_private_lan_address("192.168.1.30", discovered)
    with pytest.raises(LanAddressError, match="private"):
        select_private_lan_address("8.8.8.8", discovered)


def test_ipv6_authority_is_bracketed() -> None:
    assert http_authority("192.168.1.2", 8765) == "192.168.1.2:8765"
    assert http_authority("fd00::2", 8765) == "[fd00::2]:8765"
