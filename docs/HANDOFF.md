# AI handoff

Updated: 2026-09-12

## Current boundary

M0–M3 are implemented for this local-first English/German/Serbian reader. The CLI installs and
evaluates approved Piper voices, prepares local UTF-8 text, or runs a public article from safe
fetch through extraction (unchanged since M2). The loopback browser reader now runs on durable
SQLite-backed jobs executed by one background worker thread: URL submission returns immediately,
review/language resolution happens against the persisted snapshot, voice selection queues a
durable synthesize job, and playback starts progressively as chunks are published. Reading history
and playback position survive restarts. LAN access, sessions with access codes, and cache eviction
remain for M4/M5.

## Architecture and important files

Dependencies point inward: CLI/API/worker -> application services -> application-owned ports ->
domain. Concrete network, parser, detector, database, and speech libraries stay in adapters and
are composed only in `cli.py`. Imports must not perform I/O, download, or load models.

- `fetch/safe_http.py`, `extract/trafilatura_adapter.py`: unchanged since M2; preserve every
  SSRF/revalidation invariant in ADR-014.
- `application/services/article_preparation.py`: fetch/extract/detect/prepare orchestration. Its
  `build_prepared_article` is now a public function (M3 refactor) so the API's review/language
  resolve step can rebuild segments from a DB-reconstructed `ExtractedArticle` without re-fetching.
- `db/connection.py`, `db/migrations.py`, `db/repositories/*.py`: the only modules allowed to
  import `sqlite3`. WAL, foreign keys, busy timeout, `BEGIN IMMEDIATE` transactions, versioned
  schema that refuses to open a newer-than-known database. Eight repositories implement the
  `Protocol`s in `application/ports/persistence.py`.
- `application/services/reading_service.py`: the API-side durable orchestration (submission,
  idempotency, review/language resolve, rendition creation with contract-hash reuse via
  `rendition_contract.py`, cancel/retry, progress). Never fetches or synthesizes — only DB reads
  and cheap CPU-bound recomputation.
- `worker/loop.py`: the one durable worker. Claims jobs (fresh generation token per claim),
  executes `prepare` (calls `ArticlePreparationService`, persists article/blocks/decision/segments)
  or `synthesize` (loads a voice, synthesizes each unpublished segment, publishes via
  `storage/durable_audio.py`, checks cancellation before every chunk and every publication, renews
  its lease on a heartbeat). See ADR-020/021 in `docs/DECISIONS.md` for the accepted
  thread-not-process and best-effort-timeout limitations.
- `storage/durable_audio.py`: stage-fsync-validate-hash-rename publication; `db/repositories/
  audio.py`'s `publish_chunk` is the only place a chunk row is committed, gated on a fresh
  generation-token and cancellation check.
- `storage/process_lock.py`: single-instance-per-data-directory enforcement with real stale-lock
  recovery. Its Windows liveness check needs exact `ctypes` `HANDLE` typing and a
  `GetExitCodeProcess`/`STILL_ACTIVE` check — see ADR-024 for a real bug this caught during the
  real-article smoke test, not during unit testing.
- `api/app.py`: composition root for the durable HTTP surface (readings, resolve, renditions,
  manifest, jobs cancel/retry, progress, audio, viewer cookie identity). Never imports Piper or the
  fetch/extract adapters; those live only in `worker/loop.py`'s dependencies, composed in `cli.py`.
- `cli.py`: composition root. `_run_serve` acquires an `InstanceLock`, opens one shared
  `db.connection.Database`, builds the FastAPI app and the `WorkerLoop` against it, runs the worker
  on a background thread, and joins/releases everything in a `finally` block on shutdown.
- `web/templates/index.html`, `web/static/app.js`: the M2-era page kept in place, rewired to poll
  durable state, play back progressively, and add working Cancel/Retry and a history list.
- `speech/`, `resources/voices.toml`: unchanged since M1/M2.

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
- No transaction ever wraps a network fetch or a speech-synthesis call; `db.transaction()` is for
  short, bounded DB writes only. A worker must re-check its generation token and cancellation
  before every commit that publishes something (see `publish_chunk`).
- Only `db/` (plus `worker/` and `api/app.py` composing it) may import `sqlite3`; domain and
  application modules depend only on `application/ports/persistence.py`'s protocols and records.
- Keep routine tests offline and deterministic. Real detector, fetch, model, worker-concurrency,
  and listening evidence are separate checks — see `docs/STATUS.md`'s "Real runtime evidence."

## Reproduce and try

```powershell
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
uv run --locked pytest -q
uv run --locked article-reader --data-dir runtime/voice-data serve
```

Windows owner path: double-click `start-reader.cmd`; it selects the existing CPython 3.12
environment and `runtime/voice-data` automatically. Submitting a URL now returns instantly; watch
the page poll through queued -> preparing -> (review/language, if needed) -> ready_for_voice, then
after picking a voice: queued -> generating -> ready, with playback starting on the first section.

On this host `.venv` is Python 3.14 and only supplies `uv`; project verification uses
`runtime/venv312` via `UV_PROJECT_ENVIRONMENT`. Models/audio/the SQLite database live under ignored
`runtime/voice-data/`. This offline environment's `uv` package cache is split across
`runtime/uv-cache` (this project's historical cache) and the machine-default `%LOCALAPPDATA%\uv\
cache`; a from-scratch offline `uv sync --no-editable` into a brand-new venv needs both merged
(`cp -rn "$LOCALAPPDATA/uv/cache/." runtime/uv-cache/`) before every package resolves from cache.
`uv sync`'s local-path build cache can also go stale after editing source without bumping the
version; if a rebuilt wheel doesn't reflect a source change, force it with
`uv pip install --no-deps --reinstall <path-to-the-freshly-built .whl>`.

## Next task

M4: real-phone/browser seam testing of the now-progressive reader (buffering feel, "waiting for the
next section," seek-ahead-of-generated messaging, resume behavior across a voice change), and any
UX refinement real devices surface. M5 after that: LAN sessions/access codes, audio cache eviction,
and portable reading bundles.
