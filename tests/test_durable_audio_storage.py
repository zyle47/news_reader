"""Durable audio staging: fsync-then-rename, WAV validation, and path safety."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from article_reader.application.ports.durable_audio import AudioPublicationError
from article_reader.domain.speech import AudioResult
from article_reader.storage.durable_audio import LocalDurableAudioStore

_RENDITION_ID = "a" * 32


def _audio() -> AudioResult:
    return AudioResult(
        pcm_bytes=b"\x00\x01" * 800,
        sample_rate_hz=16_000,
        sample_width_bytes=2,
        channels=1,
    )


def test_stage_writes_valid_wav_and_no_leftover_temp_file(tmp_path: Path) -> None:
    store = LocalDurableAudioStore(tmp_path)
    artifact = store.stage(_RENDITION_ID, 0, _audio())

    assert artifact.relative_path == f"{_RENDITION_ID}/000000-{artifact.sha256}.wav"
    published = tmp_path / artifact.relative_path
    assert published.is_file()
    with wave.open(str(published), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16_000
    leftovers = list((tmp_path / _RENDITION_ID).glob("*.tmp"))
    assert leftovers == []


def test_resolve_returns_none_for_wrong_digest_or_missing_file(tmp_path: Path) -> None:
    store = LocalDurableAudioStore(tmp_path)
    artifact = store.stage(_RENDITION_ID, 0, _audio())

    assert store.resolve(_RENDITION_ID, artifact.relative_path, artifact.sha256) is not None
    with pytest.raises(ValueError, match="does not match"):
        store.resolve(_RENDITION_ID, artifact.relative_path, "b" * 64)

    (tmp_path / artifact.relative_path).unlink()
    assert store.resolve(_RENDITION_ID, artifact.relative_path, artifact.sha256) is None


def test_resolve_rejects_a_relative_path_outside_its_own_rendition(tmp_path: Path) -> None:
    store = LocalDurableAudioStore(tmp_path)
    other_rendition = "b" * 32
    (tmp_path / other_rendition).mkdir()
    digest = "c" * 64
    traversal_path = f"{other_rendition}/000000-{digest}.wav"
    with pytest.raises(ValueError, match="does not match"):
        store.resolve(_RENDITION_ID, traversal_path, digest)


def test_discard_rendition_removes_all_its_files(tmp_path: Path) -> None:
    store = LocalDurableAudioStore(tmp_path)
    store.stage(_RENDITION_ID, 0, _audio())
    store.stage(_RENDITION_ID, 1, _audio())
    store.discard_rendition(_RENDITION_ID)
    assert not (tmp_path / _RENDITION_ID).exists()
    # Idempotent: discarding an already-removed rendition does not raise.
    store.discard_rendition(_RENDITION_ID)


def test_clear_stale_temporary_files_removes_only_tmp_files(tmp_path: Path) -> None:
    store = LocalDurableAudioStore(tmp_path)
    artifact = store.stage(_RENDITION_ID, 0, _audio())
    stray_temp = tmp_path / _RENDITION_ID / ".000001-stray.tmp"
    stray_temp.write_bytes(b"partial")

    removed = store.clear_stale_temporary_files()

    assert removed == 1
    assert not stray_temp.exists()
    assert (tmp_path / artifact.relative_path).exists()


def test_stage_rejects_a_zero_frame_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalDurableAudioStore(tmp_path)

    def fake_as_wav_bytes(self: AudioResult) -> bytes:
        import io

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(self.channels)
            handle.setsampwidth(self.sample_width_bytes)
            handle.setframerate(self.sample_rate_hz)
            handle.writeframes(b"")
        return buffer.getvalue()

    monkeypatch.setattr(AudioResult, "as_wav_bytes", fake_as_wav_bytes)
    with pytest.raises(AudioPublicationError, match="no frames"):
        store.stage(_RENDITION_ID, 0, _audio())


def test_stage_rejects_metadata_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalDurableAudioStore(tmp_path)

    def fake_as_wav_bytes(self: AudioResult) -> bytes:
        import io

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(2)  # Declares 2 channels while the AudioResult says 1.
            handle.setsampwidth(self.sample_width_bytes)
            handle.setframerate(self.sample_rate_hz)
            handle.writeframes(self.pcm_bytes)
        return buffer.getvalue()

    monkeypatch.setattr(AudioResult, "as_wav_bytes", fake_as_wav_bytes)
    with pytest.raises(AudioPublicationError, match="does not match"):
        store.stage(_RENDITION_ID, 0, _audio())


def test_stage_rejects_corrupt_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = LocalDurableAudioStore(tmp_path)
    monkeypatch.setattr(AudioResult, "as_wav_bytes", lambda self: b"not a wav file at all")
    with pytest.raises(AudioPublicationError, match="not a valid WAV"):
        store.stage(_RENDITION_ID, 0, _audio())
