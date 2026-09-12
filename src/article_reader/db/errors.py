"""Stable database error types crossing the persistence boundary."""

from __future__ import annotations


class DatabaseError(RuntimeError):
    """Base error for the SQLite adapter."""


class IncompatibleSchemaError(DatabaseError):
    """Raised when the on-disk schema is newer than this build understands.

    The database is never reset automatically; the owner must upgrade the application or
    restore an older database file explicitly.
    """


class NotFoundError(DatabaseError):
    """Raised when a required row does not exist."""


class ConflictError(DatabaseError):
    """Raised when a write would violate an ownership, uniqueness, or version invariant."""


__all__ = ["ConflictError", "DatabaseError", "IncompatibleSchemaError", "NotFoundError"]
