"""Storage capacity boundary shared by orchestration and delivery adapters."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol


class StorageFullError(RuntimeError):
    """Safe cache eviction cannot make enough space for the requested write."""


class HistoryFullError(RuntimeError):
    """The owner must delete a saved reading before submitting another."""


class CachePolicy(Protocol):
    def reserve(self, rendition_id: str, byte_count: int) -> AbstractContextManager[None]: ...

    def check_write(self, rendition_id: str, byte_count: int) -> None: ...

    def maintain(self) -> None: ...

    def check_history_capacity(self, viewer_id: str) -> None: ...

    def history_admission(self, viewer_id: str) -> AbstractContextManager[None]: ...
