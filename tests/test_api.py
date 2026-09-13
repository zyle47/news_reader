"""HTTP-level integration tests for the durable API, driven together with the worker."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from article_reader.api.app import create_app
from article_reader.application.ports.fetch import FetchedPage
from article_reader.application.services.article_ingestion import ArticleIngestionResult
from article_reader.application.services.article_preparation import ArticlePreparationService
from article_reader.config import PathsSettings, ServerSettings, Settings, ensure_runtime_dirs
from article_reader.db.connection import Database
from article_reader.db.repositories import (
    SqliteArticleRepository,
    SqliteAudioChunkRepository,
    SqliteJobRepository,
    SqliteReadingRepository,
    SqliteRenditionRepository,
)
from article_reader.domain.article import (
    ArticleBlock,
    ArticleBlockKind,
    ArticleReviewReason,
    ExtractedArticle,
)
from article_reader.domain.language import LanguageCandidate, LanguageDetection
from article_reader.domain.speech import Language, Script, VoiceEvaluationStatus, VoiceSpec
from article_reader.speech.fake import FakeSpeechEngine
from article_reader.speech.registry import VoiceRegistry
from article_reader.storage.durable_audio import LocalDurableAudioStore
from article_reader.text.script import UnicodeScriptDetector
from article_reader.text.segment import RuleBasedTextPreparer
from article_reader.worker.loop import WorkerLoop, WorkerSettings

_HOST = "testserver"
_ORIGIN = "http://testserver"
_URL = "https://example.com/article"


def _page(url: str = _URL) -> FetchedPage:
    body = b"<html><article>fixture</article></html>"
    return FetchedPage(
        submitted_url=url,
        final_url=url,
        redirect_chain=(url,),
        status_code=200,
        media_type="text/html",
        body=body,
        response_bytes=len(body),
    )


def _article(needs_review: bool = False) -> ExtractedArticle:
    if needs_review:
        return ExtractedArticle(
            submitted_url=_URL,
            final_url=_URL,
            canonical_url=None,
            title="<script>Fixture title</script>",
            language_hint="en",
            blocks=(
                ArticleBlock(
                    ordinal=0,
                    kind=ArticleBlockKind.PARAGRAPH,
                    display_text=(
                        "The first useful paragraph has enough words for a realistic test."
                    ),
                    speech_text=(
                        "The first useful paragraph has enough words for a realistic test."
                    ),
                ),
                ArticleBlock(
                    ordinal=1,
                    kind=ArticleBlockKind.CODE,
                    display_text="print('shown but not spoken')",
                    speech_text=None,
                    requires_review=True,
                ),
            ),
            review_reasons=(ArticleReviewReason.COMPLEX_CONTENT,),
            extraction_version="fixture-v1",
        )
    return ExtractedArticle(
        submitted_url=_URL,
        final_url=_URL,
        canonical_url=None,
        title="Fixture Title",
        language_hint="en",
        blocks=(
            ArticleBlock(
                ordinal=0,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="The first useful paragraph has enough words for this test.",
                speech_text="The first useful paragraph has enough words for this test.",
            ),
            ArticleBlock(
                ordinal=1,
                kind=ArticleBlockKind.PARAGRAPH,
                display_text="A second paragraph rounds out the fixture article nicely.",
                speech_text="A second paragraph rounds out the fixture article nicely.",
            ),
        ),
        review_reasons=(),
        extraction_version="fixture-v1",
    )


class _Ingestor:
    def __init__(self, article: ExtractedArticle | None = None) -> None:
        self._article = article or _article()
        self.calls = 0

    def ingest(self, submitted_url: str) -> ArticleIngestionResult:
        self.calls += 1
        return ArticleIngestionResult(page=_page(submitted_url), article=self._article)


class _Detector:
    def detect(self, text: str) -> LanguageDetection:
        return LanguageDetection(
            candidates=(LanguageCandidate("en", 0.99), LanguageCandidate("de", 0.01)),
            detector_version="fixture-v1",
            sample_character_count=len(text),
            sample_alphabetic_count=sum(character.isalpha() for character in text),
            sample_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )


def _voice(voice_id: str = "fixture-en") -> VoiceSpec:
    return VoiceSpec(
        voice_id=voice_id,
        display_name="Fixture English",
        language=Language.ENGLISH,
        scripts=(Script.LATIN,),
        engine="piper",
        engine_version="1.8.0",
        sample_rate_hz=16_000,
        source_url="https://models.example.com/en",
        source_revision="0123456789abcdef0123456789abcdef01234567",
        model_url="https://models.example.com/en/model.onnx",
        model_sha256="b" * 64,
        config_url="https://models.example.com/en/model.onnx.json",
        config_sha256="c" * 64,
        model_license_url="https://models.example.com/license/model",
        data_license_url="https://models.example.com/license/data",
        evaluation_status=VoiceEvaluationStatus.APPROVED,
        evaluation_notes="Offline fixture.",
    )


def _client(
    tmp_path: Path,
    *,
    article: ExtractedArticle | None = None,
    settings: Settings | None = None,
    lan_address: str | None = None,
    base_url: str = _ORIGIN,
    client_address: tuple[str, int] = ("testclient", 50_000),
) -> tuple[TestClient, WorkerLoop, _Ingestor]:
    settings = settings or Settings(paths=PathsSettings(data_dir=(tmp_path / "data").resolve()))
    ensure_runtime_dirs(settings)
    database = Database(settings.paths.database_path)
    registry = VoiceRegistry((_voice(),))
    audio_store = LocalDurableAudioStore(settings.paths.audio_dir / "renditions")

    web_directory = Path(__file__).parents[1] / "src" / "article_reader" / "web"
    app = create_app(
        settings,
        database,
        registry,
        installation_checker=lambda _voice_spec: True,
        audio_store=audio_store,
        web_directory=web_directory,
        allowed_hosts=frozenset({_HOST}) if lan_address is None else None,
        allowed_origins=frozenset({_ORIGIN}) if lan_address is None else None,
        lan_address=lan_address,
    )

    ingestor = _Ingestor(article)
    preparation_service = ArticlePreparationService(
        ingestor,
        _Detector(),
        UnicodeScriptDetector(),
        RuleBasedTextPreparer(),
        max_segment_characters=600,
    )
    worker = WorkerLoop(
        readings=SqliteReadingRepository(database),
        articles=SqliteArticleRepository(database),
        renditions=SqliteRenditionRepository(database),
        audio_chunks=SqliteAudioChunkRepository(database),
        jobs=SqliteJobRepository(database),
        preparation_service=preparation_service,
        voice_registry=registry,
        engine_factory=FakeSpeechEngine,
        audio_store=audio_store,
        settings=WorkerSettings(
            chunk_timeout_seconds=30,
            heartbeat_seconds=5,
            lease_seconds=60,
            max_automatic_recoveries=1,
        ),
    )
    return TestClient(app, base_url=base_url, client=client_address), worker, ingestor


def _post(client: TestClient, path: str, document: dict[str, object]) -> Response:
    return client.post(path, headers={"Origin": _ORIGIN}, json=document)


def _json(response: Response) -> dict[str, Any]:
    document = response.json()
    assert isinstance(document, dict)
    return document


def test_reader_page_has_security_headers_and_packaged_assets(tmp_path: Path) -> None:
    client, _worker, _ingestor = _client(tmp_path)

    response = client.get("/")

    assert response.status_code == 200
    assert "Turn an article into a calm listening session" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.css").status_code == 200
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert ".textContent" in script.text
    assert ".innerHTML" not in script.text
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert "set-cookie" in response.headers


def test_mutating_requests_require_same_origin_json(tmp_path: Path) -> None:
    client, _worker, _ingestor = _client(tmp_path)
    payload = {"url": _URL, "language": "en"}

    missing_origin = client.post("/api/readings", json=payload)
    wrong_origin = client.post(
        "/api/readings", headers={"Origin": "https://attacker.example"}, json=payload
    )
    wrong_type = client.post(
        "/api/readings",
        headers={"Origin": _ORIGIN, "Content-Type": "text/plain"},
        content="not-json",
    )

    assert missing_origin.status_code == 403
    assert _json(missing_origin)["error"]["code"] == "ORIGIN_REJECTED"
    assert wrong_origin.status_code == 403
    assert wrong_type.status_code == 415


def test_submission_rejects_serbian_without_script_before_any_work_is_queued(
    tmp_path: Path,
) -> None:
    client, _worker, ingestor = _client(tmp_path)

    response = _post(client, "/api/readings", {"url": _URL, "language": "sr"})

    assert response.status_code == 422
    assert _json(response)["error"]["code"] == "LANGUAGE_SCRIPT_REQUIRED"
    assert ingestor.calls == 0
    assert _json(client.get("/api/readings"))["readings"] == []


def test_submission_returns_quickly_and_queues_durable_work(tmp_path: Path) -> None:
    client, worker, ingestor = _client(tmp_path)

    created = _post(client, "/api/readings", {"url": _URL, "language": "en"})
    assert created.status_code == 202
    body = _json(created)
    assert body["state"] == "queued"
    assert ingestor.calls == 0  # Nothing was fetched synchronously on the API thread.

    detail = _json(client.get(f"/api/readings/{body['reading_id']}"))
    assert detail["state"] in {"queued", "preparing"}
    assert detail["article"] is None
    assert detail["job"]["kind"] == "prepare"

    assert worker.run_once() is True
    assert ingestor.calls == 1

    detail = _json(client.get(f"/api/readings/{body['reading_id']}"))
    assert detail["state"] == "ready_for_voice"
    assert detail["article"]["title"] == "Fixture Title"
    assert detail["job"]["state"] == "completed"


def test_idempotent_submission_returns_the_same_reading(tmp_path: Path) -> None:
    client, _worker, ingestor = _client(tmp_path)
    payload = {"url": _URL, "language": "en"}

    first = client.post(
        "/api/readings",
        headers={"Origin": _ORIGIN, "Idempotency-Key": "same-key"},
        json=payload,
    )
    first_body = _json(first)
    replay = client.post(
        "/api/readings",
        headers={"Origin": _ORIGIN, "Idempotency-Key": "same-key"},
        json=payload,
    )
    assert replay.status_code == 202
    replay_body = _json(replay)
    assert replay_body["reading_id"] == first_body["reading_id"]
    assert len(_json(client.get("/api/readings"))["readings"]) == 1

    conflict = client.post(
        "/api/readings",
        headers={"Origin": _ORIGIN, "Idempotency-Key": "same-key"},
        json={"url": "https://example.com/different", "language": "en"},
    )
    assert conflict.status_code == 409
    assert _json(conflict)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert ingestor.calls == 0


def test_review_required_article_blocks_rendition_until_accepted(tmp_path: Path) -> None:
    client, worker, _ingestor = _client(tmp_path, article=_article(needs_review=True))

    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))
    worker.run_once()

    detail = _json(client.get(f"/api/readings/{created['reading_id']}"))
    assert detail["state"] == "needs_review"
    assert detail["article"]["blocks"][1]["will_be_spoken"] is False

    rejected = _post(
        client, f"/api/readings/{created['reading_id']}/renditions", {"voice_id": "fixture-en"}
    )
    assert rejected.status_code == 409
    assert _json(rejected)["error"]["code"] == "INVALID_STATE"

    resolved = _json(
        _post(
            client,
            f"/api/readings/{created['reading_id']}/resolve",
            {"language": "en", "accept_review": True},
        )
    )
    assert resolved["state"] == "ready_for_voice"
    assert resolved["article"]["review"]["accepted"] is True


def test_full_progressive_playback_flow_with_ranges_and_progress(tmp_path: Path) -> None:
    client, worker, _ingestor = _client(tmp_path)

    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))
    worker.run_once()

    voices = _json(client.get("/api/voices"))["voices"]
    assert voices[0]["installed"] is True

    rendition_started = _json(
        _post(
            client,
            f"/api/readings/{created['reading_id']}/renditions",
            {"voice_id": "fixture-en"},
        )
    )
    assert rendition_started["state"] == "queued"
    manifest = _json(client.get(f"/api/renditions/{rendition_started['rendition_id']}/manifest"))
    assert manifest["ready_prefix_count"] == 0

    assert worker.run_once() is True

    manifest = _json(client.get(f"/api/renditions/{rendition_started['rendition_id']}/manifest"))
    assert manifest["state"] == "ready"
    assert manifest["ready_prefix_count"] == manifest["total_chunks"] >= 1
    first_chunk = manifest["chunks"][0]
    assert first_chunk["audio_url"] is not None

    audio = client.get(first_chunk["audio_url"])
    partial = client.get(first_chunk["audio_url"], headers={"Range": "bytes=0-9"})
    assert audio.status_code == 200
    assert audio.headers["content-type"] == "audio/wav"
    assert audio.content.startswith(b"RIFF")
    assert partial.status_code == 206
    assert len(partial.content) == 10

    progress = _json(
        client.put(
            f"/api/readings/{created['reading_id']}/progress",
            headers={"Origin": _ORIGIN, "Content-Type": "application/json"},
            json={
                "rendition_id": rendition_started["rendition_id"],
                "chunk_ordinal": 0,
                "offset_seconds": 1.5,
                "speed": 1.25,
                "revision": None,
            },
        )
    )
    assert progress["revision"] == 1

    reopened = _json(client.get(f"/api/readings/{created['reading_id']}"))
    assert reopened["progress"]["chunk_ordinal"] == 0
    assert reopened["progress"]["revision"] == 1

    history = _json(client.get("/api/readings"))["readings"]
    assert any(item["reading_id"] == created["reading_id"] for item in history)


def test_audio_not_ready_before_publication_and_wrong_digest_is_404(tmp_path: Path) -> None:
    client, worker, _ingestor = _client(tmp_path)
    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))
    worker.run_once()
    rendition = _json(
        _post(
            client,
            f"/api/readings/{created['reading_id']}/renditions",
            {"voice_id": "fixture-en"},
        )
    )

    not_ready = client.get(f"/api/audio/{rendition['rendition_id']}/0/{'a' * 64}.wav")
    assert not_ready.status_code == 409
    assert _json(not_ready)["error"]["code"] == "AUDIO_NOT_READY"

    unknown_ordinal = client.get(f"/api/audio/{rendition['rendition_id']}/999/{'a' * 64}.wav")
    assert unknown_ordinal.status_code == 404

    worker.run_once()
    manifest = _json(client.get(f"/api/renditions/{rendition['rendition_id']}/manifest"))
    real_url = manifest["chunks"][0]["audio_url"]
    wrong_digest_url = real_url.rsplit("/", 1)[0] + f"/{'f' * 64}.wav"
    wrong_digest = client.get(wrong_digest_url)
    assert wrong_digest.status_code == 404


def test_cancel_and_retry_via_api(tmp_path: Path) -> None:
    client, worker, _ingestor = _client(tmp_path)
    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))
    worker.run_once()
    rendition = _json(
        _post(
            client,
            f"/api/readings/{created['reading_id']}/renditions",
            {"voice_id": "fixture-en"},
        )
    )
    detail = _json(client.get(f"/api/readings/{created['reading_id']}"))
    job_id = detail["job"]["job_id"]

    cancelled = _json(_post(client, f"/api/jobs/{job_id}/cancel", {}))
    assert cancelled["state"] == "cancelled"

    retried = _json(_post(client, f"/api/jobs/{job_id}/retry", {}))
    assert retried["state"] == "queued"
    assert retried["previous_job_id"] == job_id

    assert worker.run_once() is True
    manifest = _json(client.get(f"/api/renditions/{rendition['rendition_id']}/manifest"))
    assert manifest["state"] == "ready"


def test_delete_reading_removes_it_from_history_and_direct_access(tmp_path: Path) -> None:
    client, _worker, _ingestor = _client(tmp_path)
    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))

    deleted = client.delete(
        f"/api/readings/{created['reading_id']}",
        headers={"Origin": _ORIGIN, "Content-Type": "application/json"},
    )
    assert deleted.status_code == 204

    assert client.get(f"/api/readings/{created['reading_id']}").status_code == 404
    assert _json(client.get("/api/readings"))["readings"] == []


def test_cross_viewer_cannot_see_another_browsers_reading(tmp_path: Path) -> None:
    client, _worker, _ingestor = _client(tmp_path)
    created = _json(_post(client, "/api/readings", {"url": _URL, "language": "en"}))

    other_client = TestClient(client.app, base_url=_ORIGIN)  # A fresh cookie jar = new viewer.
    response = other_client.get(f"/api/readings/{created['reading_id']}")
    assert response.status_code == 404


def _lan_clients(tmp_path: Path) -> tuple[TestClient, TestClient, WorkerLoop]:
    lan_address = "192.168.50.12"
    settings = Settings(
        paths=PathsSettings(data_dir=(tmp_path / "lan-data").resolve()),
        server=ServerSettings(bind=lan_address, port=8765, lan_mode=True),
    )
    local, worker, _ingestor = _client(
        tmp_path,
        settings=settings,
        lan_address=lan_address,
        base_url="http://localhost:8765",
        client_address=("127.0.0.1", 50_000),
    )
    remote = TestClient(
        local.app,
        base_url=f"http://{lan_address}:8765",
        client=("192.168.50.44", 50_001),
    )
    return local, remote, worker


def _pair_remote(local: TestClient, remote: TestClient) -> dict[str, Any]:
    offer_response = local.post(
        "/api/pairings", headers={"Origin": "http://localhost:8765"}, json={}
    )
    assert offer_response.status_code == 200
    offer = _json(offer_response)
    assert offer["pairing_url"].endswith(f"#pair={offer['code']}")
    assert offer["qr_data_url"].startswith("data:image/svg+xml;base64,")
    paired = remote.post(
        "/api/pairings/redeem",
        headers={"Origin": "http://192.168.50.12:8765"},
        json={"code": offer["code"], "label": "Test phone"},
    )
    assert paired.status_code == 200
    assert offer["code"] not in paired.headers.get("set-cookie", "")
    return _json(paired)


def test_lan_requires_pairing_and_keeps_public_surface_non_sensitive(tmp_path: Path) -> None:
    local, remote, _worker = _lan_clients(tmp_path)

    assert remote.get("/").status_code == 200
    assert _json(remote.get("/api/auth"))["authenticated"] is False
    assert remote.get("/api/meta").status_code == 200
    assert remote.get("/api/voices").status_code == 401
    assert remote.get("/api/readings").status_code == 401
    assert remote.head(f"/api/audio/missing/0/{'a' * 64}.wav").status_code == 401
    assert "access-control-allow-origin" not in remote.get("/api/meta").headers

    local_auth = local.get("/api/auth")
    assert _json(local_auth)["authenticated"] is True
    assert _json(local_auth)["loopback"] is True
    assert "samesite=strict" in local_auth.headers.get("set-cookie", "").lower()


def test_lan_pairing_shares_library_progress_and_revocation_cuts_off_audio(
    tmp_path: Path,
) -> None:
    local, remote, worker = _lan_clients(tmp_path)
    local.get("/api/auth")
    paired = _pair_remote(local, remote)

    assert remote.get("/api/voices").status_code == 200
    created = _json(
        local.post(
            "/api/readings",
            headers={"Origin": "http://localhost:8765"},
            json={"url": _URL, "language": "en"},
        )
    )
    worker.run_once()
    assert any(
        item["reading_id"] == created["reading_id"]
        for item in _json(remote.get("/api/readings"))["readings"]
    )

    rendition = _json(
        remote.post(
            f"/api/readings/{created['reading_id']}/renditions",
            headers={"Origin": "http://192.168.50.12:8765"},
            json={"voice_id": "fixture-en"},
        )
    )
    worker.run_once()
    manifest = _json(remote.get(f"/api/renditions/{rendition['rendition_id']}/manifest"))
    audio_url = manifest["chunks"][0]["audio_url"]
    assert remote.head(audio_url).status_code == 200
    assert remote.get(audio_url, headers={"Range": "bytes=0-9"}).status_code == 206

    saved = remote.put(
        f"/api/readings/{created['reading_id']}/progress",
        headers={"Origin": "http://192.168.50.12:8765"},
        json={
            "rendition_id": rendition["rendition_id"],
            "chunk_ordinal": 0,
            "offset_seconds": 2.0,
            "speed": 1.25,
            "revision": None,
        },
    )
    assert saved.status_code == 200
    assert (
        _json(local.get(f"/api/readings/{created['reading_id']}"))["progress"]["offset_seconds"]
        == 2.0
    )

    revoked = local.post(
        f"/api/sessions/{paired['session_id']}/revoke",
        headers={"Origin": "http://localhost:8765"},
        json={},
    )
    assert revoked.status_code == 200
    assert remote.get("/api/readings").status_code == 401
    assert remote.get(audio_url).status_code == 401


def test_lan_boundary_rejects_remote_pair_creation_wrong_origin_and_rebinding(
    tmp_path: Path,
) -> None:
    local, remote, _worker = _lan_clients(tmp_path)
    local.get("/api/auth")
    _pair_remote(local, remote)

    remote_offer = remote.post(
        "/api/pairings", headers={"Origin": "http://192.168.50.12:8765"}, json={}
    )
    assert remote_offer.status_code == 403
    wrong_origin = remote.post(
        "/api/readings",
        headers={"Origin": "https://attacker.example"},
        json={"url": _URL, "language": "en"},
    )
    assert wrong_origin.status_code == 403
    rebound = remote.get("/api/readings", headers={"Host": "attacker.example"})
    assert rebound.status_code == 400
    assert "frame-ancestors 'none'" in rebound.headers["content-security-policy"]
