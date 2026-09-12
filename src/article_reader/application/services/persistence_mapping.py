"""Pure conversions between domain values and their durable record representations.

Kept separate from orchestration so the (de)serialization rules for one durable row shape
are defined exactly once, and so the reading/worker services that use them stay readable.
"""

from __future__ import annotations

import json

from article_reader.application.ports.persistence import (
    ArticleBlockRecord,
    ArticleRecord,
    PreparedSegmentRecord,
)
from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleReviewReason,
    ExtractedArticle,
)
from article_reader.domain.language import (
    DetectedScript,
    LanguageCandidate,
    LanguageDetection,
    ScriptDetection,
)
from article_reader.domain.preparation import PreparedArticle


def article_blocks_from_domain(article: ExtractedArticle) -> tuple[ArticleBlockRecord, ...]:
    return tuple(
        ArticleBlockRecord(
            ordinal=block.ordinal,
            kind=block.kind.value,
            display_text=block.display_text,
            speech_text=block.speech_text,
            requires_review=block.requires_review,
        )
        for block in article.blocks
    )


def extracted_article_from_record(record: ArticleRecord) -> ExtractedArticle:
    return ExtractedArticle(
        submitted_url=record.submitted_url,
        final_url=record.final_url,
        canonical_url=record.canonical_url,
        title=record.title,
        language_hint=record.language_hint,
        blocks=tuple(
            ArticleBlock(
                ordinal=block.ordinal,
                kind=ArticleBlockKind(block.kind),
                display_text=block.display_text,
                speech_text=block.speech_text,
                requires_review=block.requires_review,
            )
            for block in record.blocks
        ),
        review_reasons=tuple(ArticleReviewReason(reason) for reason in record.review_reasons),
        extraction_version=record.extraction_version,
    )


def detection_to_json(detection: LanguageDetection | None) -> str | None:
    if detection is None:
        return None
    return json.dumps(
        {
            "detector_version": detection.detector_version,
            "sample_character_count": detection.sample_character_count,
            "sample_alphabetic_count": detection.sample_alphabetic_count,
            "sample_sha256": detection.sample_sha256,
            "candidates": [
                {"code": candidate.code, "confidence": candidate.confidence}
                for candidate in detection.candidates
            ],
        }
    )


def detection_from_json(data: str | None) -> LanguageDetection | None:
    if data is None:
        return None
    payload = json.loads(data)
    return LanguageDetection(
        candidates=tuple(
            LanguageCandidate(candidate["code"], candidate["confidence"])
            for candidate in payload["candidates"]
        ),
        detector_version=payload["detector_version"],
        sample_character_count=payload["sample_character_count"],
        sample_alphabetic_count=payload["sample_alphabetic_count"],
        sample_sha256=payload["sample_sha256"],
    )


def script_detection_to_json(detection: ScriptDetection) -> str:
    return json.dumps(
        {
            "script": detection.script.value,
            "latin_letter_count": detection.latin_letter_count,
            "cyrillic_letter_count": detection.cyrillic_letter_count,
            "detector_version": detection.detector_version,
        }
    )


def script_detection_from_json(data: str) -> ScriptDetection:
    payload = json.loads(data)
    return ScriptDetection(
        script=DetectedScript(payload["script"]),
        latin_letter_count=payload["latin_letter_count"],
        cyrillic_letter_count=payload["cyrillic_letter_count"],
        detector_version=payload["detector_version"],
    )


def prepared_segments_from_domain(prepared: PreparedArticle) -> tuple[PreparedSegmentRecord, ...]:
    records = []
    title_span = prepared.title_source_span
    for segment in prepared.prepared_text.segments:
        source_blocks = tuple(
            mapping.block_ordinal
            for mapping in prepared.block_source_spans
            if any(
                span.start < mapping.source_span.end and mapping.source_span.start < span.end
                for span in segment.source_spans
            )
        )
        includes_title = bool(
            title_span is not None
            and any(
                span.start < title_span.end and title_span.start < span.end
                for span in segment.source_spans
            )
        )
        records.append(
            PreparedSegmentRecord(
                ordinal=segment.ordinal,
                speech_text=segment.speech_text,
                speech_text_sha256=segment.speech_text_sha256,
                source_block_ordinals=source_blocks,
                includes_title=includes_title,
                normalizer_version=prepared.prepared_text.normalizer_version,
                segmenter_version=prepared.prepared_text.segmenter_version,
            )
        )
    return tuple(records)


__all__ = [
    "article_blocks_from_domain",
    "detection_from_json",
    "detection_to_json",
    "extracted_article_from_record",
    "prepared_segments_from_domain",
    "script_detection_from_json",
    "script_detection_to_json",
]
