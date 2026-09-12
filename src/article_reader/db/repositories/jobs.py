"""Durable job queue: atomic claiming, leases, cancellation, retry, and recovery."""

from __future__ import annotations

import sqlite3

from article_reader.application.ports.persistence import JobKind, JobRecord, JobState
from article_reader.db.connection import Database
from article_reader.db.errors import NotFoundError


def _row_to_record(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        job_id=row["job_id"],
        kind=JobKind(row["kind"]),
        reading_id=row["reading_id"],
        rendition_id=row["rendition_id"],
        state=JobState(row["state"]),
        stage=row["stage"],
        attempt=row["attempt"],
        previous_job_id=row["previous_job_id"],
        worker_generation=row["worker_generation"],
        lease_expires_at=row["lease_expires_at"],
        cancel_requested=bool(row["cancel_requested"]),
        recovery_count=row["recovery_count"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        payload_json=row["payload_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class SqliteJobRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def create(self, job: JobRecord) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, reading_id, rendition_id, state, stage, attempt,
                    previous_job_id, worker_generation, lease_expires_at, cancel_requested,
                    recovery_count, error_code, error_message, payload_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.kind.value,
                    job.reading_id,
                    job.rendition_id,
                    job.state.value,
                    job.stage,
                    job.attempt,
                    job.previous_job_id,
                    job.worker_generation,
                    job.lease_expires_at,
                    int(job.cancel_requested),
                    job.recovery_count,
                    job.error_code,
                    job.error_message,
                    job.payload_json,
                    job.created_at,
                    job.updated_at,
                ),
            )

    def get(self, job_id: str) -> JobRecord | None:
        with self._database.read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return None if row is None else _row_to_record(row)

    def get_owned(self, job_id: str, viewer_id: str) -> JobRecord | None:
        with self._database.read() as connection:
            row = connection.execute(
                """
                SELECT jobs.* FROM jobs
                JOIN readings ON readings.reading_id = jobs.reading_id
                WHERE jobs.job_id = ? AND readings.viewer_id = ?
                """,
                (job_id, viewer_id),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_for_reading(self, reading_id: str) -> tuple[JobRecord, ...]:
        with self._database.read() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE reading_id = ? ORDER BY created_at, job_id",
                (reading_id,),
            ).fetchall()
        return tuple(_row_to_record(row) for row in rows)

    def claim_next(
        self, *, worker_generation: str, now: str, lease_seconds: float
    ) -> JobRecord | None:
        lease_expires_at = _add_seconds(now, lease_seconds)
        with self._database.transaction() as connection:
            candidate = connection.execute(
                """
                SELECT job_id FROM jobs
                WHERE state = 'queued'
                ORDER BY created_at, job_id
                LIMIT 1
                """
            ).fetchone()
            if candidate is None:
                return None
            job_id = candidate["job_id"]
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = 'running', worker_generation = ?, lease_expires_at = ?,
                    stage = 'claimed', updated_at = ?
                WHERE job_id = ? AND state = 'queued'
                """,
                (worker_generation, lease_expires_at, now, job_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _row_to_record(row)

    def renew_lease(
        self, job_id: str, *, worker_generation: str, now: str, lease_seconds: float
    ) -> bool:
        lease_expires_at = _add_seconds(now, lease_seconds)
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND worker_generation = ? AND state IN ('running', 'cancelling')
                """,
                (lease_expires_at, now, job_id, worker_generation),
            )
            row = connection.execute("SELECT changes()").fetchone()
            return int(row[0]) > 0

    def set_stage(self, job_id: str, *, worker_generation: str, stage: str, now: str) -> bool:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET stage = ?, updated_at = ?
                WHERE job_id = ? AND worker_generation = ? AND state IN ('running', 'cancelling')
                """,
                (stage, now, job_id, worker_generation),
            )
            row = connection.execute("SELECT changes()").fetchone()
            return int(row[0]) > 0

    def complete(self, job_id: str, *, worker_generation: str, now: str) -> bool:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET state = 'completed', stage = 'completed', updated_at = ?
                WHERE job_id = ? AND worker_generation = ? AND state IN ('running', 'cancelling')
                """,
                (now, job_id, worker_generation),
            )
            row = connection.execute("SELECT changes()").fetchone()
            return int(row[0]) > 0

    def fail(
        self,
        job_id: str,
        *,
        worker_generation: str,
        now: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'failed', updated_at = ?, error_code = ?, error_message = ?
                WHERE job_id = ? AND worker_generation = ? AND state IN ('running', 'cancelling')
                """,
                (now, error_code, error_message, job_id, worker_generation),
            )
            row = connection.execute("SELECT changes()").fetchone()
            return int(row[0]) > 0

    def mark_cancelled(self, job_id: str, *, worker_generation: str, now: str) -> bool:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs SET state = 'cancelled', stage = 'cancelled', updated_at = ?
                WHERE job_id = ? AND worker_generation = ? AND state IN ('running', 'cancelling')
                """,
                (now, job_id, worker_generation),
            )
            row = connection.execute("SELECT changes()").fetchone()
            return int(row[0]) > 0

    def request_cancel(self, job_id: str, *, now: str) -> JobRecord:
        with self._database.transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"job {job_id!r} does not exist")
            current_state = JobState(row["state"])
            if current_state is JobState.QUEUED:
                connection.execute(
                    """
                    UPDATE jobs SET state = 'cancelled', cancel_requested = 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
            elif current_state is JobState.RUNNING:
                connection.execute(
                    """
                    UPDATE jobs SET state = 'cancelling', cancel_requested = 1, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (now, job_id),
                )
            # Terminal and already-cancelling states are left untouched: cancellation is
            # idempotent and never resurrects a finished attempt.
            updated = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _row_to_record(updated)

    def is_cancel_requested(self, job_id: str) -> bool:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return False if row is None else bool(row["cancel_requested"])

    def recover_interrupted(self, *, now: str, max_recoveries: int) -> int:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET state = 'interrupted', stage = 'interrupted', updated_at = ?
                WHERE state IN ('running', 'cancelling') AND lease_expires_at IS NOT NULL
                    AND lease_expires_at < ?
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE jobs
                SET state = 'queued', stage = NULL, worker_generation = NULL,
                    lease_expires_at = NULL, recovery_count = recovery_count + 1, updated_at = ?
                WHERE state = 'interrupted' AND recovery_count < ?
                """,
                (now, max_recoveries),
            )
            recovered = connection.execute("SELECT changes()").fetchone()[0]
            return int(recovered)

    def create_retry(self, old_job_id: str, new_job_id: str, *, now: str) -> JobRecord:
        with self._database.transaction() as connection:
            old_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (old_job_id,)
            ).fetchone()
            if old_row is None:
                raise NotFoundError(f"job {old_job_id!r} does not exist")
            old = _row_to_record(old_row)
            if old.state not in (JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED):
                raise ValueError(
                    f"job {old_job_id!r} is {old.state.value}; only failed, cancelled, or "
                    "interrupted jobs can be retried"
                )
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, reading_id, rendition_id, state, stage, attempt,
                    previous_job_id, worker_generation, lease_expires_at, cancel_requested,
                    recovery_count, error_code, error_message, payload_json, created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, 'queued', NULL, ?, ?, NULL, NULL, 0, 0, NULL, NULL, ?, ?, ?)
                """,
                (
                    new_job_id,
                    old.kind.value,
                    old.reading_id,
                    old.rendition_id,
                    old.attempt + 1,
                    old_job_id,
                    old.payload_json,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (new_job_id,)
            ).fetchone()
        return _row_to_record(row)

    def count_queued(self) -> int:
        with self._database.read() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE state = 'queued'"
            ).fetchone()
        return int(row["count"])


def _add_seconds(timestamp: str, seconds: float) -> str:
    from datetime import UTC, datetime, timedelta

    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed + timedelta(seconds=seconds)).isoformat()


__all__ = ["SqliteJobRepository"]
