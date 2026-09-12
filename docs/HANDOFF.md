# AI handoff

Updated: 2026-09-12

## Current boundary

M0, M1, and M2 are implemented for this local-first English/German/Serbian reader. The CLI installs
and evaluates approved Piper voices, prepares local UTF-8 text, or runs a public article from safe
fetch through extraction, review/language decisions, and source-traceable speech segmentation.
Nothing is persisted yet and article segments are not synthesized as durable jobs. A functional
loopback-only FastAPI/Jinja preview UI now exercises URL -> review/language -> real Piper chunks ->
browser playback without the CLI; its state is intentionally transient until M3.

## Architecture and important files

Dependencies point inward: CLI -> application services -> application-owned ports/domain. Concrete
network, parser, detector, and speech libraries stay in adapters and are composed only in `cli.py`.
Imports must not perform I/O, download, or load models.

- `fetch/safe_http.py`, `extract/trafilatura_adapter.py`: pinned bounded public retrieval and
  byte-only structured extraction. Preserve every SSRF/revalidation invariant in ADR-014.
- `application/services/article_preparation.py`: M2 orchestration and awaiting-input states.
- `application/ports/language.py`, `text/py3langid_detector.py`: local detector boundary and
  lazy py3langid 0.4 adapter.
- `domain/language.py`, `application/services/language_selection.py`, `text/script.py`: immutable
  evidence and versioned policy. HTML hints are evidence, not authority. Latin BCMS always waits
  for an explicit language; strong Serbian Cyrillic can resolve automatically.
- `domain/preparation.py`, `domain/text.py`, `text/normalize.py`, `text/segment.py`: exact
  title/block/source-span mapping and conservative speech preparation without transliteration.
- `speech/`, `resources/voices.toml`: verified Piper installer/adapter and three owner-approved
  voices. Preserve provenance caveats and separate install/evaluate/approve states.
- `cli.py`: current composition root. `prepare-article` exits 3 for review/language input and 0
  only when the manifest is ready.
- `api/app.py`, `web/`: loopback preview HTTP contracts and the responsive reader. State-changing
  requests enforce exact Host/Origin and JSON boundaries; article DOM uses `textContent`.
- `application/services/preview_audio.py`, `storage/preview_audio.py`: approved/compatible voice
  enforcement and atomic generated-ID WAV publication. Review resolution reuses the exact fetched
  snapshot via `ArticlePreparationService.prepare_ingestion`.

## Non-negotiable invariants

- Target CPython `>=3.12,<3.13`; keep `uv.lock` current and never copy virtual environments.
- All submitted URLs use the validated IP for the actual connection; repeat public-address checks
  on every redirect while preserving TLS SNI/certificate and Host identity.
- Article/user text is private: never commit it or include it in errors/logs. Runtime artifacts use
  generated/local paths, never article titles.
- Never silently treat Croatian/Bosnian/Slovenian as Serbian. Manual Serbian selection requires an
  explicit script. Never transliterate the preserved text.
- Review-required content cannot become ready without explicit acceptance. Omitted unspeakable
  blocks remain visible in the manifest.
- Keep routine tests offline and deterministic. Real detector, fetch, model, and listening evidence
  are separate checks.

## Reproduce and try

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
uv run --locked article-reader fetch-article "https://example.com/" --output runtime/article.json
uv run --locked article-reader prepare-article "https://example.com/" --accept-review --output runtime/prepared.json
uv run --locked article-reader prepare-article "<serbian-url>" --language sr --script latin --output runtime/sr.json
uv run --locked article-reader --data-dir runtime/voice-data serve
```

Windows owner path: double-click `start-reader.cmd`; it selects the existing CPython 3.12
environment and `runtime/voice-data` automatically.

On this host `.venv` is Python 3.14 and only supplies `uv`; project verification uses
`runtime/venv312` via `UV_PROJECT_ENVIRONMENT`. Models/audio live under ignored
`runtime/voice-data/`. The folder is not currently a Git repository.

## Next task

Begin M3 with versioned SQLite migrations and repositories. Persist immutable source and prepared
snapshots, language/review decisions, rendition contracts, durable job attempts, and idempotency
records. Then replace request-scoped synthesis with worker claiming/leases, cancellation/recovery,
and durable chunk publication while keeping the existing browser contract and player usable.
