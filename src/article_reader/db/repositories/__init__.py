"""Concrete SQLite repositories implementing ``application.ports.persistence``."""

from __future__ import annotations

from article_reader.db.repositories.articles import SqliteArticleRepository
from article_reader.db.repositories.audio import SqliteAudioChunkRepository
from article_reader.db.repositories.idempotency import SqliteIdempotencyRepository
from article_reader.db.repositories.jobs import SqliteJobRepository
from article_reader.db.repositories.progress import SqliteProgressRepository
from article_reader.db.repositories.readings import SqliteReadingRepository
from article_reader.db.repositories.renditions import SqliteRenditionRepository
from article_reader.db.repositories.viewers import SqliteViewerRepository

__all__ = [
    "SqliteArticleRepository",
    "SqliteAudioChunkRepository",
    "SqliteIdempotencyRepository",
    "SqliteJobRepository",
    "SqliteProgressRepository",
    "SqliteReadingRepository",
    "SqliteRenditionRepository",
    "SqliteViewerRepository",
]
