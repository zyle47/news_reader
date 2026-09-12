"""Canonical, lexicographically sortable UTC timestamps for durable storage.

SQLite compares the ``TEXT`` timestamp columns used throughout ``db/migrations.py`` (job
leases, ``ORDER BY created_at``, and so on) as plain strings. ``datetime.isoformat()`` alone
is not safe for that: it omits the microsecond field entirely when it is zero, which breaks
lexicographic ordering between two timestamps that differ only in whether one happens to
land on an exact second. Always format through :func:`now_iso` instead.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


__all__ = ["now_iso"]
