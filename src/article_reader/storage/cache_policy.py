"""Whole-rendition cache eviction with persistent leases and in-process response pins.

One instance is shared by the single worker and all HTTP handlers. The process-wide
instance lock excludes another Reader; this lock serializes local eviction with pinning.
No filesystem timestamps or source IP addresses establish playback ownership.
"""

from __future__ import annotations

import re
import shutil
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from article_reader.application.ports.cache import HistoryFullError, StorageFullError
from article_reader.db.repositories.cache import SqliteCacheRepository

_RENDITION = re.compile(r"^[a-f0-9]{32}$")
_ARTIFACT = re.compile(r"^(?:\.[0-9]{6}-.+\.tmp|[0-9]{6}-[a-f0-9]{64}\.wav)$")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class AudioCachePolicy:
    def __init__(
        self,
        *,
        repository: SqliteCacheRepository,
        audio_root: Path,
        max_audio_bytes: int,
        min_free_disk_bytes: int,
        playback_lease_seconds: float,
        max_saved_readings_per_viewer: int,
        inactive_days: int = 7,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        free_bytes: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
    ) -> None:
        if not audio_root.is_absolute() or max_audio_bytes < 0 or min_free_disk_bytes < 0:
            raise ValueError("invalid audio cache root or capacity")
        self._repository = repository
        self._root = audio_root
        self._budget = max_audio_bytes
        self._floor = min_free_disk_bytes
        self._lease_seconds = playback_lease_seconds
        self._history_limit = max_saved_readings_per_viewer
        self._inactive_days = inactive_days
        self._clock = clock
        self._free_bytes = free_bytes
        self._lock = threading.RLock()
        self._pins: Counter[str] = Counter()
        self._reservations: dict[str, int] = {}
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink():
            raise ValueError("audio cache root cannot be a symlink")

    def _files(self) -> Iterator[Path]:
        # Do not follow symlinks, including a substituted rendition directory.
        for directory in self._root.iterdir():
            if directory.is_file() and not directory.is_symlink():
                yield directory
                continue
            if directory.is_symlink() or not directory.is_dir():
                continue
            for path in directory.iterdir():
                if not path.is_symlink() and path.is_file():
                    yield path

    def used_bytes(self) -> int:
        with self._lock:
            return sum(path.stat().st_size for path in self._files())

    def _protected(self) -> set[str]:
        return (
            self._repository.protected(now=_timestamp(self._clock()))
            | set(self._pins)
            | set(self._reservations)
        )

    def renew_playback(self, rendition_id: str) -> bool:
        """Caller must authorize the rendition before invoking this adapter."""
        with self._lock:
            now = self._clock()
            return self._repository.touch(
                rendition_id,
                now=_timestamp(now),
                expires_at=_timestamp(now + timedelta(seconds=self._lease_seconds)),
            )

    @contextmanager
    def pin(self, rendition_id: str) -> Iterator[None]:
        """Hold from BEFORE resolve() until the HTTP body closes, including cancellation."""
        with self._lock:
            if not self.renew_playback(rendition_id):
                raise FileNotFoundError("Audio is unavailable; generate it again.")
            self._pins[rendition_id] += 1
        try:
            yield
        finally:
            with self._lock:
                self._pins[rendition_id] -= 1
                if not self._pins[rendition_id]:
                    del self._pins[rendition_id]

    def _evict(self, rendition_id: str) -> None:
        if _RENDITION.fullmatch(rendition_id) is None:
            raise ValueError("invalid rendition identifier in cache")
        directory = self._root / rendition_id
        if directory.is_symlink():
            raise StorageFullError("Refusing linked audio storage.")
        self._repository.mark_evicted(rendition_id, now=_timestamp(self._clock()))
        if directory.exists():
            shutil.rmtree(directory)

    def _reclaim_orphans(self) -> None:
        referenced = self._repository.referenced_paths()
        # Only this worker writes files, and it calls maintenance between jobs.
        # Queued jobs may own leftovers from a crashed publication; their durable
        # metadata protects ready chunks, but must not protect unreferenced debris
        # forever. Reservations cover a currently executing publication instead.
        writing_or_reading = set(self._reservations) | set(self._pins)
        for path in self._files():
            if path.parent.name in writing_or_reading:
                continue
            if (
                _RENDITION.fullmatch(path.parent.name) is None
                or _ARTIFACT.fullmatch(path.name) is None
            ):
                continue
            if path.relative_to(self._root).as_posix() not in referenced:
                path.unlink(missing_ok=True)

    def _make_space(self, requested: int) -> None:
        protected = self._protected()
        self._reclaim_orphans()
        candidates = iter(self._repository.candidates())
        reserved = sum(self._reservations.values())
        while (
            self.used_bytes() + reserved + requested > self._budget
            or self._free_bytes(self._root) - reserved - requested < self._floor
        ):
            candidate = next(candidates, None)
            if candidate is None:
                raise StorageFullError(
                    "Audio storage is full; free space or delete cached readings."
                )
            rendition_id, _deleted, _access = candidate
            if rendition_id in protected:
                continue
            self._evict(rendition_id)

    @contextmanager
    def reserve(self, rendition_id: str, byte_count: int) -> Iterator[None]:
        if byte_count < 0:
            raise ValueError("negative storage reservation")
        with self._lock:
            if rendition_id in self._reservations:
                raise RuntimeError("rendition already has a storage reservation")
            self._make_space(byte_count)
            self._reservations[rendition_id] = byte_count
        try:
            yield
        finally:
            with self._lock:
                self._reservations.pop(rendition_id, None)

    def check_write(self, rendition_id: str, byte_count: int) -> None:
        """Resize the reservation to actual bytes before staging any file."""
        with self._lock:
            old = self._reservations.get(rendition_id)
            if old is None:
                raise RuntimeError("write requires an active reservation")
            self._reservations[rendition_id] = 0
            try:
                self._make_space(byte_count)
            except BaseException:
                self._reservations[rendition_id] = old
                raise
            self._reservations[rendition_id] = byte_count

    def check_history_capacity(self, viewer_id: str) -> None:
        with self._lock:
            if self._repository.live_reading_count(viewer_id) >= self._history_limit:
                raise HistoryFullError(
                    "Saved reading limit reached. Delete an older reading first."
                )

    @contextmanager
    def history_admission(self, viewer_id: str) -> Iterator[None]:
        """Serialize admission with the following reading insert across HTTP threads."""
        with self._lock:
            self.check_history_capacity(viewer_id)
            yield

    def maintain(self) -> None:
        with self._lock:
            protected = self._protected()
            available = {path.relative_to(self._root).as_posix() for path in self._files()}
            for rendition_id in self._repository.missing_ready_renditions(available) - protected:
                self._evict(rendition_id)
            cutoff = _timestamp(self._clock() - timedelta(days=self._inactive_days))
            for rendition_id, deleted, last_access in self._repository.candidates():
                if rendition_id in protected:
                    continue
                if deleted or (self._inactive_days > 0 and last_access < cutoff):
                    self._evict(rendition_id)
            self._reclaim_orphans()
            self._repository.prune_metadata(now=_timestamp(self._clock()))
            # A full cache is recoverable: jobs receive STORAGE_FULL on reservation;
            # maintenance itself must not keep the durable worker in a crash loop.
            with suppress(StorageFullError):
                self._make_space(0)
