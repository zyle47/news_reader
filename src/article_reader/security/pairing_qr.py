"""Bounded, local QR rendering for a one-time fragment pairing URL."""

from __future__ import annotations

import base64
from io import BytesIO

import segno

_MAX_PAIRING_URL_CHARACTERS = 512
_MAX_SVG_BYTES = 128 * 1024


def pairing_qr_data_url(pairing_url: str) -> str:
    if not pairing_url.startswith("http://") or len(pairing_url) > _MAX_PAIRING_URL_CHARACTERS:
        raise ValueError("pairing URL is not a bounded local HTTP URL")
    stream = BytesIO()
    code = segno.make_qr(pairing_url, error="m")
    code.save(stream, kind="svg", scale=5, border=2, xmldecl=False)
    rendered = stream.getvalue()
    if len(rendered) > _MAX_SVG_BYTES:
        raise ValueError("pairing QR image exceeded its size bound")
    encoded = base64.b64encode(rendered).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


__all__ = ["pairing_qr_data_url"]
