from __future__ import annotations

import base64

import pytest

from article_reader.security.pairing_qr import pairing_qr_data_url


def test_pairing_qr_is_a_bounded_inline_svg() -> None:
    result = pairing_qr_data_url("http://192.168.1.20:8765/#pair=12345678")
    prefix, encoded = result.split(",", maxsplit=1)
    rendered = base64.b64decode(encoded)

    assert prefix == "data:image/svg+xml;base64"
    assert rendered.startswith(b"<svg")
    assert len(rendered) < 128 * 1024
    assert b"<script" not in rendered


def test_pairing_qr_rejects_unbounded_or_non_http_input() -> None:
    with pytest.raises(ValueError):
        pairing_qr_data_url("https://example.com/#pair=12345678")
    with pytest.raises(ValueError):
        pairing_qr_data_url("http://192.168.1.20/" + "x" * 600)
