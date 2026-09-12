"""SQLite persistence: connections, migrations, and repositories.

This package is a concrete outbound adapter (see ``docs/ARCHITECTURE.md``). Domain and
application modules must not import :mod:`sqlite3` or anything from this package directly;
they depend on the narrow protocols in ``article_reader.application.ports.persistence``.
"""

from __future__ import annotations

__all__: list[str] = []
