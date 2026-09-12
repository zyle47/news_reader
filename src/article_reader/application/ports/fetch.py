"""Application-owned article retrieval boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class FetchedPage:
    submitted_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    status_code: int
    media_type: str
    body: bytes
    response_bytes: int

    def __post_init__(self) -> None:
        if not self.submitted_url or not self.final_url:
            raise ValueError("fetched page requires submitted and final URLs")
        if not self.redirect_chain or self.redirect_chain[-1] != self.final_url:
            raise ValueError("redirect chain must end at the final URL")
        if type(self.status_code) is not int or not 200 <= self.status_code <= 299:
            raise ValueError("fetched page requires a successful HTTP status")
        if self.media_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("fetched page requires an HTML media type")
        if not isinstance(self.body, bytes) or not self.body:
            raise ValueError("fetched page body must be non-empty bytes")
        if type(self.response_bytes) is not int or self.response_bytes <= 0:
            raise ValueError("response_bytes must be a positive integer")

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class ArticleFetcher(Protocol):
    def fetch(self, submitted_url: str) -> FetchedPage:
        """Retrieve one HTML page without following unvalidated subresources."""
        ...


__all__ = ["ArticleFetcher", "FetchedPage"]
