"""Private-interface discovery and validation for explicit LAN serving."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable

_PRIVATE_LAN_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


class LanAddressError(ValueError):
    """Raised when no safe, usable private LAN bind address can be selected."""


def is_private_lan_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        any(address in network for network in _PRIVATE_LAN_NETWORKS)
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def _source_address(family: socket.AddressFamily, target: tuple[object, ...]) -> str | None:
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as candidate:
            candidate.connect(target)
            return str(candidate.getsockname()[0])
    except OSError:
        return None


def discover_private_lan_addresses(
    *,
    hostname: str | None = None,
    resolver: Callable[..., Iterable[tuple[object, ...]]] = socket.getaddrinfo,
    source_probe: Callable[[socket.AddressFamily, tuple[object, ...]], str | None] = (
        _source_address
    ),
) -> tuple[str, ...]:
    """Return local private addresses without contacting an external web service.

    UDP ``connect`` is used only as an OS routing-table query; it sends no datagram.
    Hostname resolution supplies addresses from all locally registered interfaces.
    """

    found: list[str] = []

    def add(candidate: str | None) -> None:
        if candidate is None or not is_private_lan_address(candidate):
            return
        canonical = str(ipaddress.ip_address(candidate))
        if canonical not in found:
            found.append(canonical)

    probes = (
        (socket.AF_INET, ("192.0.2.1", 9)),
        (socket.AF_INET6, ("2001:db8::1", 9, 0, 0)),
    )
    for family, target in probes:
        add(source_probe(family, target))

    try:
        answers = resolver(hostname or socket.gethostname(), None, type=socket.SOCK_STREAM)
    except OSError:
        answers = ()
    for answer in answers:
        try:
            sockaddr = answer[4]
            candidate = str(sockaddr[0])  # type: ignore[index]
        except (IndexError, TypeError):
            continue
        add(candidate)

    return tuple(found)


def select_private_lan_address(
    requested: str | None,
    discovered: tuple[str, ...],
) -> str:
    if requested is not None:
        try:
            canonical = str(ipaddress.ip_address(requested))
        except ValueError as exc:
            raise LanAddressError("--bind must be a bare private IP address") from exc
        if not is_private_lan_address(canonical):
            raise LanAddressError("--bind must be a private, non-loopback LAN address")
        if canonical not in discovered:
            raise LanAddressError("--bind is not assigned to a discovered local interface")
        return canonical
    if not discovered:
        raise LanAddressError(
            "no private LAN address was found; connect this computer to the local network or "
            "pass --bind with an assigned private address"
        )
    return discovered[0]


def http_authority(address: str, port: int) -> str:
    parsed = ipaddress.ip_address(address)
    host = f"[{parsed}]" if parsed.version == 6 else str(parsed)
    return f"{host}:{port}"


__all__ = [
    "LanAddressError",
    "discover_private_lan_addresses",
    "http_authority",
    "is_private_lan_address",
    "select_private_lan_address",
]
