"""SQLite metadata used by bounded audio storage; filesystem work stays outside transactions."""

from __future__ import annotations

from article_reader.db.connection import Database


class SqliteCacheRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def touch(self, rendition_id: str, *, now: str, expires_at: str) -> bool:
        with self._database.transaction() as connection:
            found = connection.execute(
                """SELECT 1 FROM renditions r JOIN readings d USING(reading_id)
                WHERE r.rendition_id = ? AND d.deleted_at IS NULL
                AND EXISTS (SELECT 1 FROM audio_chunks c WHERE c.rendition_id = r.rendition_id
                    AND c.state = 'ready')""",
                (rendition_id,),
            ).fetchone()
            if found is None:
                return False
            connection.execute(
                """INSERT INTO cache_access VALUES (?, ?) ON CONFLICT(rendition_id)
                DO UPDATE SET last_access_at = excluded.last_access_at""",
                (rendition_id, now),
            )
            connection.execute(
                """INSERT INTO playback_leases VALUES (?, ?) ON CONFLICT(rendition_id)
                DO UPDATE SET expires_at = excluded.expires_at""",
                (rendition_id, expires_at),
            )
            return True

    def protected(self, *, now: str) -> set[str]:
        with self._database.read() as connection:
            rows = connection.execute(
                """SELECT rendition_id FROM playback_leases WHERE expires_at > ? UNION
                SELECT rendition_id FROM jobs WHERE rendition_id IS NOT NULL
                    AND state IN ('queued', 'running', 'cancelling')""",
                (now,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def candidates(self) -> tuple[tuple[str, bool, str], ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                """SELECT r.rendition_id, d.deleted_at IS NOT NULL AS deleted,
                COALESCE(a.last_access_at, r.updated_at) AS last_access
                FROM renditions r JOIN readings d USING(reading_id)
                LEFT JOIN cache_access a USING(rendition_id)
                WHERE EXISTS (SELECT 1 FROM audio_chunks c
                    WHERE c.rendition_id = r.rendition_id AND c.state = 'ready')
                ORDER BY deleted DESC, last_access, r.rendition_id"""
            ).fetchall()
        return tuple((str(row[0]), bool(row[1]), str(row[2])) for row in rows)

    def referenced_paths(self) -> set[str]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT relative_path FROM audio_chunks WHERE state = 'ready'"
            ).fetchall()
        return {str(row[0]) for row in rows}

    def mark_evicted(self, rendition_id: str, *, now: str) -> None:
        # Commit loss of availability BEFORE unlinking. A crash leaves counted orphans,
        # never a ready row that points to a file this operation already removed.
        with self._database.transaction() as connection:
            connection.execute(
                "UPDATE audio_chunks SET state = 'evicted' WHERE rendition_id = ?",
                (rendition_id,),
            )
            connection.execute(
                """UPDATE renditions SET state = 'failed', error_code = 'AUDIO_EVICTED',
                error_message = 'Cached audio was reclaimed. Generate it again to listen.',
                manifest_revision = manifest_revision + 1, updated_at = ?
                WHERE rendition_id = ?""",
                (now, rendition_id),
            )
            connection.execute(
                "DELETE FROM playback_leases WHERE rendition_id = ?", (rendition_id,)
            )

    def live_reading_count(self, viewer_id: str) -> int:
        with self._database.read() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM readings WHERE viewer_id = ? AND deleted_at IS NULL",
                    (viewer_id,),
                ).fetchone()[0]
            )

    def missing_ready_renditions(self, available_paths: set[str]) -> set[str]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT rendition_id, relative_path FROM audio_chunks WHERE state = 'ready'"
            ).fetchall()
        return {str(row[0]) for row in rows if str(row[1]) not in available_paths}

    def prune_metadata(self, *, now: str, terminal_jobs_per_reading: int = 64) -> None:
        with self._database.transaction() as connection:
            connection.execute("DELETE FROM playback_leases WHERE expires_at <= ?", (now,))
            # Idempotency is bounded too; keep the newest 1024 requests per viewer.
            connection.execute(
                """DELETE FROM idempotency_keys WHERE rowid IN (
                SELECT rowid FROM (SELECT rowid, row_number() OVER (
                    PARTITION BY viewer_id ORDER BY created_at DESC, rowid DESC) AS n
                    FROM idempotency_keys) WHERE n > 1024)"""
            )
            victims = connection.execute(
                """SELECT job_id FROM (
                SELECT j.job_id, row_number() OVER (PARTITION BY j.reading_id
                    ORDER BY j.created_at DESC, j.job_id DESC) AS n FROM jobs j
                WHERE j.state IN ('completed', 'failed', 'cancelled', 'interrupted'))
                WHERE n > ? AND NOT EXISTS (SELECT 1 FROM idempotency_keys k
                    WHERE json_extract(k.response_json, '$.job_id') = job_id)""",
                (terminal_jobs_per_reading,),
            ).fetchall()
            for row in victims:
                connection.execute(
                    "UPDATE jobs SET previous_job_id = NULL WHERE previous_job_id = ?", (row[0],)
                )
                connection.execute("DELETE FROM jobs WHERE job_id = ?", (row[0],))
            # Fully deleted readings no longer need snapshots or job metadata once all
            # their work stops and all audio rows have been invalidated and removed.
            connection.execute(
                """DELETE FROM idempotency_keys WHERE resource_id IN (
                SELECT reading_id FROM readings WHERE deleted_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.reading_id = readings.reading_id
                    AND state IN ('queued', 'running', 'cancelling'))
                AND NOT EXISTS (SELECT 1 FROM audio_chunks c JOIN renditions r USING(rendition_id)
                    WHERE r.reading_id = readings.reading_id AND c.state = 'ready'))"""
            )
            deleted = connection.execute(
                """SELECT reading_id FROM readings WHERE deleted_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.reading_id = readings.reading_id
                    AND state IN ('queued', 'running', 'cancelling'))
                AND NOT EXISTS (SELECT 1 FROM audio_chunks c JOIN renditions r USING(rendition_id)
                    WHERE r.reading_id = readings.reading_id AND c.state = 'ready')"""
            ).fetchall()
            for row in deleted:
                connection.execute(
                    "UPDATE jobs SET previous_job_id = NULL WHERE reading_id = ?", (row[0],)
                )
                connection.execute("DELETE FROM readings WHERE reading_id = ?", (row[0],))
