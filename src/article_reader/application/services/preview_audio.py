"""Synchronous preview rendition use case for the loopback UI scaffold."""

from __future__ import annotations

import re
from dataclasses import dataclass

from article_reader.application.ports.audio import PreviewAudioPublisher
from article_reader.application.ports.speech import SpeechEngine
from article_reader.domain.preparation import PreparedArticle
from article_reader.domain.speech import (
    SpeechSettings,
    VoiceEvaluationStatus,
    VoiceSpec,
)

_OPAQUE_ID = re.compile(r"^[0-9a-f]{32}$")


class PreviewAudioError(RuntimeError):
    """Raised when a prepared article cannot become a preview rendition."""


@dataclass(frozen=True, slots=True)
class PreviewAudioChunk:
    ordinal: int
    speech_text: str
    source_block_ordinals: tuple[int, ...]
    includes_title: bool
    sha256: str
    byte_count: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class PreviewRendition:
    rendition_id: str
    voice_id: str
    sample_rate_hz: int
    chunks: tuple[PreviewAudioChunk, ...]

    @property
    def total_duration_seconds(self) -> float:
        return sum(chunk.duration_seconds for chunk in self.chunks)


def _source_blocks(prepared: PreparedArticle, segment_ordinal: int) -> tuple[int, ...]:
    segment = prepared.prepared_text.segments[segment_ordinal]
    return tuple(
        mapping.block_ordinal
        for mapping in prepared.block_source_spans
        if any(
            span.start < mapping.source_span.end and mapping.source_span.start < span.end
            for span in segment.source_spans
        )
    )


def _includes_title(prepared: PreparedArticle, segment_ordinal: int) -> bool:
    title_span = prepared.title_source_span
    if title_span is None:
        return False
    return any(
        span.start < title_span.end and title_span.start < span.end
        for span in prepared.prepared_text.segments[segment_ordinal].source_spans
    )


class PreviewAudioService:
    """Render all prepared segments and atomically publish each complete WAV.

    This intentionally synchronous seam is only for the loopback preview UI.
    M3 replaces its request-scoped execution with durable worker jobs while the
    browser consumes the same ordered-chunk concept.
    """

    def __init__(self, publisher: PreviewAudioPublisher) -> None:
        self._publisher = publisher

    def render(
        self,
        rendition_id: str,
        prepared: PreparedArticle,
        voice: VoiceSpec,
        engine: SpeechEngine,
        settings: SpeechSettings | None = None,
    ) -> PreviewRendition:
        if _OPAQUE_ID.fullmatch(rendition_id) is None:
            raise ValueError("rendition_id must be a generated 128-bit lowercase hex ID")
        if not isinstance(prepared, PreparedArticle):
            raise TypeError("prepared must be a PreparedArticle")
        if not isinstance(voice, VoiceSpec):
            raise TypeError("voice must be a VoiceSpec")
        if voice.evaluation_status is not VoiceEvaluationStatus.APPROVED:
            raise PreviewAudioError("the selected voice is not approved")
        original = prepared.prepared_text.original
        if not voice.supports(original.language, original.script):
            raise PreviewAudioError("the selected voice does not support the prepared text")

        synthesis_settings = settings or SpeechSettings()
        chunks: list[PreviewAudioChunk] = []
        try:
            engine.load(voice)
            for segment in prepared.prepared_text.segments:
                audio = engine.synthesize(segment.speech_text, synthesis_settings)
                artifact = self._publisher.publish(rendition_id, segment.ordinal, audio)
                chunks.append(
                    PreviewAudioChunk(
                        ordinal=segment.ordinal,
                        speech_text=segment.speech_text,
                        source_block_ordinals=_source_blocks(prepared, segment.ordinal),
                        includes_title=_includes_title(prepared, segment.ordinal),
                        sha256=artifact.sha256,
                        byte_count=artifact.byte_count,
                        duration_seconds=artifact.duration_seconds,
                    )
                )
        except Exception:
            self._publisher.discard(rendition_id)
            raise
        finally:
            engine.close()

        if not chunks:
            self._publisher.discard(rendition_id)
            raise PreviewAudioError("the prepared article contains no speech segments")
        return PreviewRendition(
            rendition_id=rendition_id,
            voice_id=voice.voice_id,
            sample_rate_hz=voice.sample_rate_hz,
            chunks=tuple(chunks),
        )


__all__ = [
    "PreviewAudioChunk",
    "PreviewAudioError",
    "PreviewAudioService",
    "PreviewRendition",
]
