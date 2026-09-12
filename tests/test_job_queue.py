"""Durable job queue: atomic claiming, leases, cancellation, retry, and recovery."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from article_reader.application.ports.persistence import JobKind, JobRecord, JobState
from article_reader.clock import now_iso
from article_reader.db.connection import Database
from article_reader.db.errors import NotFoundError
from tests.conftest import Repos, seed_reading, seed_viewer


def _seed_prepare_job(repos: Repos, reading_id: str, job_id: str = "job-1") -> None:
    now = now_iso()
    repos.jobs.create(
        JobRecord(
            job_id=job_id,
            kind=JobKind.PREPARE,
            reading_id=reading_id,
            rendition_id=None,
            state=JobState.QUEUED,
            stage=None,
            attempt=1,
            previous_job_id=None,
            worker_generation=None,
            lease_expires_at=None,
            cancel_requested=False,
            recovery_count=0,
            error_code=None,
            error_message=None,
            payload_json="{}",
            created_at=now,
            updated_at=now,
        )
    )


def test_claim_next_returns_none_when_queue_is_empty(repos: Repos) -> None:
    assert repos.jobs.claim_next(worker_generation="gen-a", now=now_iso(), lease_seconds=60) is None


def test_claim_next_is_fifo_and_marks_running(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_a = seed_reading(repos, viewer, "reading-a")
    reading_b = seed_reading(repos, viewer, "reading-b")
    _seed_prepare_job(repos, reading_a, "job-a")
    _seed_prepare_job(repos, reading_b, "job-b")

    claimed = repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)
    assert claimed is not None
    assert claimed.job_id == "job-a"
    assert claimed.state is JobState.RUNNING
    assert claimed.worker_generation == "gen-1"

    second = repos.jobs.claim_next(worker_generation="gen-2", now=now_iso(), lease_seconds=60)
    assert second is not None
    assert second.job_id == "job-b"


def test_two_concurrent_claimers_never_both_win(repos: Repos, database: Database) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)

    from article_reader.db.repositories.jobs import SqliteJobRepository

    other_jobs = SqliteJobRepository(database)
    results: list[JobRecord | None] = [None, None]
    barrier = threading.Barrier(2)

    def claim(index: int, repository: SqliteJobRepository, generation: str) -> None:
        barrier.wait(timeout=5)
        results[index] = repository.claim_next(
            worker_generation=generation, now=now_iso(), lease_seconds=60
        )

    threads = [
        threading.Thread(target=claim, args=(0, repos.jobs, "gen-a")),
        threading.Thread(target=claim, args=(1, other_jobs, "gen-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].job_id == "job-1"


def test_renew_lease_fails_for_wrong_generation(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)

    assert repos.jobs.renew_lease(
        "job-1", worker_generation="gen-1", now=now_iso(), lease_seconds=60
    )
    assert not repos.jobs.renew_lease(
        "job-1", worker_generation="stale-generation", now=now_iso(), lease_seconds=60
    )


def test_complete_and_fail_are_rejected_for_stale_generation(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)

    assert not repos.jobs.complete("job-1", worker_generation="wrong", now=now_iso())
    assert not repos.jobs.fail(
        "job-1", worker_generation="wrong", now=now_iso(), error_code="X", error_message="x"
    )
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.RUNNING

    assert repos.jobs.complete("job-1", worker_generation="gen-1", now=now_iso())
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.COMPLETED


def test_cancel_requested_while_queued_finalizes_immediately(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)

    cancelled = repos.jobs.request_cancel("job-1", now=now_iso())
    assert cancelled.state is JobState.CANCELLED
    assert cancelled.cancel_requested is True
    # A queued-then-cancelled job is never claimable.
    assert repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60) is None


def test_cancel_requested_while_running_marks_cancelling_then_worker_finalizes(
    repos: Repos,
) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)

    cancelling = repos.jobs.request_cancel("job-1", now=now_iso())
    assert cancelling.state is JobState.CANCELLING
    assert repos.jobs.is_cancel_requested("job-1") is True

    assert repos.jobs.mark_cancelled("job-1", worker_generation="gen-1", now=now_iso())
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.CANCELLED


def test_cancel_on_terminal_job_is_idempotent(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)
    repos.jobs.complete("job-1", worker_generation="gen-1", now=now_iso())

    result = repos.jobs.request_cancel("job-1", now=now_iso())
    assert result.state is JobState.COMPLETED


def test_request_cancel_on_missing_job_raises(repos: Repos) -> None:
    with pytest.raises(NotFoundError):
        repos.jobs.request_cancel("does-not-exist", now=now_iso())


def test_lease_expiry_marks_job_interrupted_and_bounded_recovery_requeues(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    repos.jobs.claim_next(worker_generation="gen-1", now=past, lease_seconds=0.01)

    recovered = repos.jobs.recover_interrupted(now=now_iso(), max_recoveries=1)
    assert recovered == 1
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.QUEUED
    assert job.recovery_count == 1
    assert job.worker_generation is None


def test_recovery_is_bounded_by_max_automatic_recoveries(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    repos.jobs.claim_next(worker_generation="gen-1", now=past, lease_seconds=0.01)
    repos.jobs.recover_interrupted(now=now_iso(), max_recoveries=1)
    job = repos.jobs.get("job-1")
    assert job is not None and job.state is JobState.QUEUED

    repos.jobs.claim_next(worker_generation="gen-2", now=past, lease_seconds=0.01)
    repos.jobs.recover_interrupted(now=now_iso(), max_recoveries=1)
    job = repos.jobs.get("job-1")
    assert job is not None
    assert job.state is JobState.INTERRUPTED
    assert job.recovery_count == 1


def test_stale_worker_cannot_publish_after_lease_reassigned(repos: Repos) -> None:
    """A worker holding an old generation token can never resume ownership."""

    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    stale = repos.jobs.claim_next(worker_generation="stale-gen", now=past, lease_seconds=0.01)
    assert stale is not None
    repos.jobs.recover_interrupted(now=now_iso(), max_recoveries=5)
    fresh = repos.jobs.claim_next(worker_generation="fresh-gen", now=now_iso(), lease_seconds=60)
    assert fresh is not None

    # The stale worker's generation token no longer controls this job.
    assert not repos.jobs.complete("job-1", worker_generation="stale-gen", now=now_iso())
    assert not repos.jobs.renew_lease(
        "job-1", worker_generation="stale-gen", now=now_iso(), lease_seconds=60
    )
    assert repos.jobs.complete("job-1", worker_generation="fresh-gen", now=now_iso())


def test_retry_creates_linked_job_and_rejects_non_terminal_source(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id)
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)

    with pytest.raises(ValueError, match="only failed, cancelled, or interrupted"):
        repos.jobs.create_retry("job-1", "job-2", now=now_iso())

    repos.jobs.fail(
        "job-1", worker_generation="gen-1", now=now_iso(), error_code="X", error_message="boom"
    )
    retried = repos.jobs.create_retry("job-1", "job-2", now=now_iso())
    assert retried.previous_job_id == "job-1"
    assert retried.attempt == 2
    assert retried.state is JobState.QUEUED


def test_count_queued_reflects_only_queued_jobs(repos: Repos) -> None:
    viewer = seed_viewer(repos)
    reading_id = seed_reading(repos, viewer)
    _seed_prepare_job(repos, reading_id, "job-1")
    assert repos.jobs.count_queued() == 1
    repos.jobs.claim_next(worker_generation="gen-1", now=now_iso(), lease_seconds=60)
    assert repos.jobs.count_queued() == 0
