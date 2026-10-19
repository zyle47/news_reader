"""Capacity, playback races, restart recovery, and physical deletion use real SQLite/files."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from article_reader.application.ports.cache import HistoryFullError, StorageFullError
from article_reader.application.ports.persistence import AudioChunkRecord, ChunkState, ReadingState
from article_reader.clock import now_iso
from article_reader.db.connection import Database
from article_reader.db.repositories.cache import SqliteCacheRepository
from article_reader.domain.speech import AudioResult
from article_reader.storage.cache_policy import AudioCachePolicy
from article_reader.storage.durable_audio import LocalDurableAudioStore
from tests.conftest import Repos, seed_article_ready_for_voice, seed_reading, seed_viewer
from tests.test_worker_loop import _seed_rendition, _seed_synthesize_job


def _cache(database: Database, root: Path, **options: object) -> AudioCachePolicy:
    defaults = {
        "max_audio_bytes": 10_000,
        "min_free_disk_bytes": 0,
        "playback_lease_seconds": 180.0,
        "max_saved_readings_per_viewer": 200,
        "inactive_days": 0,
        "free_bytes": lambda _path: 1_000_000,
    }
    defaults.update(options)
    return AudioCachePolicy(repository=SqliteCacheRepository(database), audio_root=root, **defaults)  # type: ignore[arg-type]


def _cached(repos: Repos, root: Path, number: int) -> str:
    reading_id = f"reading-{number}"
    seed_reading(repos, "viewer-1", reading_id)
    article_id = seed_article_ready_for_voice(
        repos, reading_id, f"article-{number}", segment_texts=("Fixture.",)
    )
    rendition_id = f"{number:032x}"
    rendition = _seed_rendition(repos, reading_id, article_id, 1, rendition_id)
    _seed_synthesize_job(repos, reading_id, rendition_id, f"job-{number}")
    job = repos.jobs.claim_next(worker_generation="worker", now=now_iso(), lease_seconds=100)
    assert job is not None
    artifact = LocalDurableAudioStore(root).stage(
        rendition_id,
        0,
        AudioResult(
            pcm_bytes=b"\0\0" * 30, sample_rate_hz=16_000, sample_width_bytes=2, channels=1
        ),
    )
    outcome = repos.audio_chunks.publish_chunk(
        job_id=job.job_id,
        worker_generation="worker",
        total_chunks=1,
        now=now_iso(),
        chunk=AudioChunkRecord(
            rendition_id=rendition_id,
            ordinal=0,
            state=ChunkState.READY,
            relative_path=artifact.relative_path,
            sha256=artifact.sha256,
            byte_count=artifact.byte_count,
            duration_seconds=artifact.duration_seconds,
            publication_generation="worker",
            published_at=now_iso(),
        ),
    )
    assert outcome.committed
    repos.jobs.complete(job.job_id, worker_generation="worker", now=now_iso())
    repos.readings.update_state(reading_id, ReadingState.READY)
    assert repos.renditions.get(rendition.rendition_id) is not None
    return rendition_id


def test_reservation_evicts_whole_oldest_rendition_but_preserves_history(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    first = _cached(repos, root, 1)
    second = _cached(repos, root, 2)
    cache = _cache(database, root, max_audio_bytes=250)
    with cache.reserve("3" * 32, 100):
        assert not (root / first).exists()
        assert (root / second).exists()
    chunk = repos.audio_chunks.get(first, 0)
    assert chunk is not None and chunk.state is ChunkState.EVICTED
    rendition = repos.renditions.get(first)
    assert rendition is not None and rendition.error_code == "AUDIO_EVICTED"
    assert repos.articles.get_by_reading("reading-1") is not None
    assert repos.readings.get("reading-1") is not None


def test_renewed_lease_protects_playback_across_policy_restart(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    clock = [datetime.now(UTC)]
    cache = _cache(database, root, max_audio_bytes=120, clock=lambda: clock[0])
    assert cache.renew_playback(rendition)
    restarted = _cache(database, root, max_audio_bytes=120, clock=lambda: clock[0])
    with pytest.raises(StorageFullError), restarted.reserve("2" * 32, 50):
        pass
    clock[0] += timedelta(seconds=181)
    with restarted.reserve("2" * 32, 50):
        assert not (root / rendition).exists()


def test_response_pin_outlasts_lease_and_closes_even_on_exception(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    clock = [datetime.now(UTC)]
    cache = _cache(database, root, max_audio_bytes=120, clock=lambda: clock[0])
    with pytest.raises(RuntimeError, match="disconnect"), cache.pin(rendition):
        clock[0] += timedelta(seconds=1000)
        with pytest.raises(StorageFullError), cache.reserve("2" * 32, 50):
            pass
        raise RuntimeError("disconnect")
    with cache.reserve("2" * 32, 50):
        assert not (root / rendition).exists()


def test_queued_generation_is_never_evicted(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    _seed_synthesize_job(repos, "reading-1", rendition, "retry")
    cache = _cache(database, root, max_audio_bytes=120)
    with pytest.raises(StorageFullError), cache.reserve("2" * 32, 50):
        pass
    assert (root / rendition).exists()


def test_queued_retry_reclaims_crashed_publication_orphans_but_keeps_ready_chunks(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    _seed_synthesize_job(repos, "reading-1", rendition, "retry")
    orphan = root / rendition / ("000001-" + "b" * 64 + ".wav")
    orphan.write_bytes(b"x" * 1000)
    temporary = root / rendition / ".000001-dead.tmp"
    temporary.write_bytes(b"x" * 1000)
    cache = _cache(database, root, max_audio_bytes=200)
    cache.maintain()
    assert not orphan.exists() and not temporary.exists()
    assert cache.used_bytes() == 104
    chunk = repos.audio_chunks.get(rendition, 0)
    assert chunk is not None and chunk.state is ChunkState.READY


def test_low_disk_floor_fails_before_writing_and_releases_reservation(
    database: Database, tmp_path: Path
) -> None:
    cache = _cache(database, tmp_path / "audio", min_free_disk_bytes=100, free_bytes=lambda _: 130)
    with pytest.raises(StorageFullError), cache.reserve("1" * 32, 31):
        pass
    with cache.reserve("1" * 32, 30):
        cache.check_write("1" * 32, 20)
    assert cache.used_bytes() == 0


def test_all_orphans_and_staging_count_toward_capacity(database: Database, tmp_path: Path) -> None:
    root = tmp_path / "audio"
    directory = root / ("1" * 32)
    directory.mkdir(parents=True)
    (directory / ".000000-interrupted.tmp").write_bytes(b"x" * 100)
    (directory / ("000000-" + "a" * 64 + ".wav")).write_bytes(b"x" * 100)
    # Unknown files are counted but never guessed safe to delete.
    (root / "operator-file").write_bytes(b"x" * 20)
    cache = _cache(database, root, max_audio_bytes=30)
    assert cache.used_bytes() == 220
    with cache.reserve("2" * 32, 10):
        assert cache.used_bytes() == 20
    with pytest.raises(StorageFullError), cache.reserve("2" * 32, 11):
        pass


def test_reservation_race_has_one_winner(database: Database, tmp_path: Path) -> None:
    cache = _cache(database, tmp_path / "audio", max_audio_bytes=100)
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def reserve() -> None:
        try:
            with cache.reserve("1" * 32, 75):
                acquired.set()
                assert release.wait(5)
        except BaseException as error:
            errors.append(error)
        finally:
            database.close()

    thread = threading.Thread(target=reserve)
    thread.start()
    try:
        assert acquired.wait(5)
        with pytest.raises(StorageFullError), cache.reserve("2" * 32, 75):
            pass
    finally:
        release.set()
        thread.join(5)
    assert not errors


def test_deleted_reading_reclaims_files_only_after_cancellation_stops(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    _seed_synthesize_job(repos, "reading-1", rendition, "active")
    job = repos.jobs.claim_next(worker_generation="active", now=now_iso(), lease_seconds=100)
    assert job is not None
    repos.readings.soft_delete("reading-1", deleted_at=now_iso())
    repos.jobs.request_cancel("active", now=now_iso())
    cache = _cache(database, root)
    cache.maintain()
    assert (root / rendition).exists()
    repos.jobs.mark_cancelled("active", worker_generation="active", now=now_iso())
    cache.maintain()
    assert not (root / rendition).exists()
    assert repos.readings.get("reading-1") is None


def test_missing_file_invalidates_entire_rendition_on_restart(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    for path in (root / rendition).iterdir():
        path.unlink()
    cache = _cache(database, root)
    cache.maintain()
    chunk = repos.audio_chunks.get(rendition, 0)
    assert chunk is not None and chunk.state is ChunkState.EVICTED


def test_evicted_chunk_can_be_republished_by_current_job_generation(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    cache = _cache(database, root, max_audio_bytes=120)
    with cache.reserve("2" * 32, 50):
        pass
    old = repos.audio_chunks.get(rendition, 0)
    assert old is not None and old.state is ChunkState.EVICTED
    _seed_synthesize_job(repos, "reading-1", rendition, "retry")
    repos.jobs.claim_next(worker_generation="new", now=now_iso(), lease_seconds=100)
    outcome = repos.audio_chunks.publish_chunk(
        job_id="retry",
        worker_generation="new",
        chunk=replace(old, state=ChunkState.READY),
        total_chunks=1,
        now=now_iso(),
    )
    assert outcome.committed
    assert repos.audio_chunks.get(rendition, 0).state is ChunkState.READY  # type: ignore[union-attr]


def test_history_is_bounded_without_silently_removing_existing_readings(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    viewer = seed_viewer(repos)
    seed_reading(repos, viewer)
    cache = _cache(database, tmp_path / "audio", max_saved_readings_per_viewer=1)
    with pytest.raises(HistoryFullError):
        cache.check_history_capacity(viewer)
    assert repos.readings.get("reading-1") is not None
    repos.readings.soft_delete("reading-1", deleted_at=now_iso())
    cache.check_history_capacity(viewer)


def test_symlink_rendition_cannot_delete_outside_audio_root(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    actual = tmp_path / "outside"
    (root / rendition).rename(actual)
    (root / rendition).symlink_to(actual, target_is_directory=True)
    cache = _cache(database, root)
    with pytest.raises(StorageFullError, match="linked"):
        cache.maintain()
    assert next(actual.iterdir()).is_file()


def test_terminal_job_history_is_bounded_without_breaking_retry_references(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    from article_reader.application.ports.persistence import JobState
    from tests.test_worker_loop import _seed_prepare_job

    seed_viewer(repos)
    seed_reading(repos, "viewer-1")
    for number in range(90):
        _seed_prepare_job(repos, "reading-1", f"job-{number:03d}")
        job = repos.jobs.claim_next(worker_generation="worker", now=now_iso(), lease_seconds=100)
        assert job is not None
        repos.jobs.fail(
            job.job_id,
            worker_generation="worker",
            now=now_iso(),
            error_code="FIXTURE",
            error_message="Fixture failure",
        )
        if number:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET previous_job_id = ? WHERE job_id = ?",
                    (f"job-{number - 1:03d}", job.job_id),
                )
    _cache(database, tmp_path / "audio").maintain()
    retained = repos.jobs.list_for_reading("reading-1")
    assert len(retained) == 64
    assert all(job.state is JobState.FAILED for job in retained)
    repos.jobs.create_retry(retained[-1].job_id, "retry", now=now_iso())


def test_inactive_expiry_evicts_once_without_revision_churn(
    database: Database, repos: Repos, tmp_path: Path
) -> None:
    seed_viewer(repos)
    root = tmp_path / "audio"
    rendition = _cached(repos, root, 1)
    cache = _cache(
        database, root, inactive_days=7, clock=lambda: datetime.now(UTC) + timedelta(days=8)
    )
    cache.maintain()
    first = repos.renditions.get(rendition)
    assert first is not None and first.error_code == "AUDIO_EVICTED"
    cache.maintain()
    second = repos.renditions.get(rendition)
    assert second is not None and second.manifest_revision == first.manifest_revision
