# Implementation status

Updated: 2026-09-12

## Current milestone

M0 architecture, M1 voice feasibility, M2 article extraction/text preparation, and M3 durable
jobs and audio are implemented. The loopback browser reader now runs entirely on durable SQLite
jobs executed by one background worker: URL submission returns immediately, review/language
resolution and voice selection happen against the persisted snapshot, and audio plays back
progressively as chunks are published — before the whole article finishes generating. Reading
history and playback position survive closing the page and restarting the app. LAN access,
sessions with access codes, cache eviction, and portable reading bundles remain for M4/M5.

## What works

- Three checksum-pinned Piper voices are owner-approved and locally verified for English, German,
  Serbian Latin, and Serbian Cyrillic (unchanged from M1/M2).
- `fetch-article`/`prepare-article` and the underlying safe-fetch, extraction, language-selection,
  and segmentation pipeline are unchanged and still exercised directly by the CLI (M2).
- **Durable persistence (`db/`).** `db/connection.py` opens per-thread SQLite connections with
  foreign keys, WAL, and a busy timeout, and applies `db/migrations.py`'s versioned schema inside
  `BEGIN IMMEDIATE` transactions tracked by `PRAGMA user_version`. Opening a database with a newer
  `user_version` than the running build understands raises `IncompatibleSchemaError` and leaves the
  file untouched — never resets user data. Eight repositories (viewers, readings, articles,
  renditions, audio chunks, jobs, idempotency, progress) implement the `Protocol`s in
  `application/ports/persistence.py`; only `db/`, `worker/`, and `api/app.py` know the concrete
  SQLite classes.
- **One durable worker (`worker/loop.py`).** Claims the oldest queued job (prepare or synthesize)
  in one `BEGIN IMMEDIATE` transaction with a fresh per-claim generation token, so two concurrent
  claimers always resolve to exactly one winner (verified with a real two-thread race, not just
  reasoned about). Renews its lease on a heartbeat, checks cancellation before every chunk and
  before every publication, resumes a retried rendition from the first unpublished chunk, and
  recovers a crashed attempt's interrupted job up to `worker.max_automatic_recoveries` times before
  requiring an explicit retry. Runs as a dedicated background thread inside the same process as the
  API (see ADR-020) — never on the API's asyncio event loop, and never via FastAPI
  `BackgroundTasks`.
- **Atomic audio publication (`storage/durable_audio.py`, `db/repositories/audio.py`).** Each
  chunk is staged to a temporary file beside its final location, flushed, `fsync`ed, re-parsed as a
  WAV file to confirm its format matches the synthesized audio exactly, hashed, and only then
  atomically renamed to its content-addressed final path. The database row is committed in a
  separate step that re-checks the job's generation token and cancellation flag first, so a worker
  that lost its lease — or whose job was cancelled between finishing synthesis and committing —
  can never publish. A cancelled-mid-synthesis rendition keeps every chunk it finished before
  cancellation was observed, playable.
- **Single-instance enforcement (`storage/process_lock.py`).** A second `serve` against the same
  data directory is refused; a lock left behind by a process that has actually exited is
  automatically reaped (verified against a real spawned-then-exited subprocess, not just a
  synthetic PID — see ADR-024 for a real Windows `ctypes` bug this caught and fixed).
- **Durable API (`api/app.py`, `application/services/reading_service.py`).** `POST /api/readings`
  returns 202 with `queued` state immediately (no network or Piper work on the API thread) and
  supports an `Idempotency-Key` header bound to the exact request payload. `GET /api/readings/{id}`
  reports article/review/language state, the most relevant job, and a rendition summary.
  `POST .../resolve` re-runs language selection and segmentation against the persisted snapshot
  (no re-fetch). `POST .../renditions` validates the voice, computes a portable contract hash, and
  either reuses an existing matching rendition or queues a new synthesize job. `GET
  /api/renditions/{id}/manifest` reports `ready_prefix_count` and per-chunk audio URLs.
  `POST /api/jobs/{id}/cancel` and `.../retry`, `PUT .../progress` (with revision-based optimistic
  concurrency), `DELETE /api/readings/{id}`, and `GET /api/readings` (history) round out the
  surface. Audio is served only by resolving `(rendition_id, ordinal, sha256)` through the
  database; GET/HEAD, byte ranges, ETags, and 404/409/410 states are all covered. A lightweight
  opaque viewer cookie (SHA-256-hashed server-side) scopes ownership without implementing the full
  LAN session/access-code system (see ADR-022).
- **Browser reader (`web/`).** The M2-era page's design and controls are preserved; the JavaScript
  is rewired to poll `GET /api/readings/{id}` and `GET /api/renditions/{id}/manifest`, start
  playback as soon as the first chunk is ready, show queued/preparing/generating/ready/
  cancelling/cancelled/failed/interrupted states with working Cancel and Retry buttons, list recent
  readings with resume/delete, and persist playback position to the server (with local-storage
  speed as a same-tab convenience only). Cross-tab pause coordination is unchanged from M2.
- The M2-era transient preview scaffold (`application/services/preview_audio.py`,
  `storage/preview_audio.py`, `application/ports/audio.py`, and their tests) is deleted; it is
  fully superseded by the durable path.

## Real runtime evidence

- All quality gates below were run for real on this host, not assumed.
- **Real Piper synthesis of the real, previously-used Intermagazin Serbian article
  (`https://www.intermagazin.rs/dugin-otkrio-sta-rusija-mora-da-uradi/`)**, driven entirely through
  the durable HTTP API against a live `serve` process (no test doubles): submission auto-selected
  `sr/cyrillic` again, queued a `prepare` job that fetched and extracted the real page, and a
  queued `synthesize` job against the approved `sr-marko-medium` voice produced 3 chunks (~101
  seconds of audio) using real Piper inference. Precise timing captured against the live manifest
  endpoint: **chunk 0 (37.7s of audio) became playable at 3.797s** into generation, while the full
  rendition (all 3 chunks) did not reach `ready` until **6.391s** — the first section was
  downloadable and played back (verified `RIFF`-prefixed WAV bytes) roughly 2.6 seconds before the
  rest of the article finished, a real, measured instance of progressive playback, not a claim.
- **Restart persistence with real data.** With the server stopped and restarted against the same
  data directory, the same reading's `GET /api/readings/{id}` still reported `state: "ready"` with
  its rendition intact, and the previously published chunk 0 still served correctly with the same
  digest and correct byte-range (`206`) behavior — proving durability across a real process
  restart, not just an in-memory test double.
- **A real bug found and fixed by this real-environment test** (not by unit tests, which used a
  synthetic PID that did not exercise the failure path): the initial `storage/process_lock.py`
  Windows liveness check used `ctypes.windll.kernel32.OpenProcess` without explicit
  `argtypes`/`restype`, which silently truncates the 64-bit `HANDLE` return value and can
  misreport an exited process as alive; and even with correct typing, a successful `OpenProcess`
  alone does not prove a process is still running (its kernel object can persist until fully
  reaped). Both are now fixed: exact `ctypes` signatures plus a `GetExitCodeProcess` /
  `STILL_ACTIVE` check. See ADR-024. A new regression test spawns and waits on a real subprocess
  rather than using an arbitrary PID, so this class of bug is now caught automatically.
- Prior M1/M2 real-model evidence (checksum verification, ~24x-realtime Piper throughput on this
  host, py3langid detection accuracy, the original `example.com`/Intermagazin runs) remains valid
  and is not repeated here.

## Quality gates

```text
ruff check .                       All checks passed
ruff format --check .              109 files already formatted
mypy                                Success: no issues found in 101 source files
pytest -q                           359 passed, 1 skipped, 5 subtests passed
uv lock --check                     Resolved 56 packages (lockfile current, unchanged from M2)
uv sync --locked --offline          Checked 55 installed packages with no network access
uv build --offline                  Source and wheel distributions built
```

A fresh CPython 3.12 environment (`uv sync --locked --offline --no-editable --no-dev`, pointed at a
brand-new venv) installed the built wheel and all 42 runtime packages from the local `uv` cache
with networking disabled — the same package count reported at the end of M2. The packaged `serve`
command started successfully in that clean environment and answered `GET /api/meta` correctly. The
one skipped test still requires Windows symlink privileges unavailable to this account.

Offline deterministic failure-injection coverage added for M3 (all pass; see the listed test files
for exact scenarios):

| Area | Test file |
| --- | --- |
| Migration upgrade, incompatible future schema, FK/uniqueness, restart persistence | `tests/test_db_connection.py`, `tests/test_repositories.py` |
| Duplicate submissions / idempotency conflicts | `tests/test_repositories.py`, `tests/test_reading_service.py`, `tests/test_api.py` |
| Two concurrent claimers (real two-thread race) | `tests/test_job_queue.py` |
| Lease expiry and stale generation tokens | `tests/test_job_queue.py`, `tests/test_worker_loop.py` |
| Cancellation at every publication boundary (queued, running, mid-synthesis) | `tests/test_job_queue.py`, `tests/test_worker_loop.py` |
| Worker crash and bounded automatic recovery | `tests/test_job_queue.py`, `tests/test_worker_loop.py` |
| Partial/missing/corrupt WAV files | `tests/test_durable_audio_storage.py`, `tests/test_worker_loop.py` |
| Duplicate chunk publication | `tests/test_repositories.py` |
| Restart persistence | `tests/test_db_connection.py`, `tests/test_repositories.py`, real smoke test above |
| Audio authorization and byte ranges | `tests/test_api.py` |
| UI job polling, progressive playback, cancellation, retry, error states | `tests/test_api.py` (HTTP-level, driven together with the real worker loop) |
| Single-instance lock and real stale-lock recovery | `tests/test_process_lock.py` |

## Known gaps

- The worker is one background thread inside the API process, not a separate OS process (ADR-020).
  This satisfies "never on the API event loop" and "never FastAPI background tasks," and per-claim
  generation tokens correctly reject a stale attempt, but it does not fence against a hand-rolled
  second process bypassing `serve` for the same data directory (the `InstanceLock` does, as long as
  both attempts go through `serve`).
- Chunk timeout uses a daemon thread, not a killable subprocess (ADR-021): a genuinely hung Piper
  call is not forcibly terminated, only abandoned in the background while the worker moves on. The
  documented recovery is restarting `serve`; no state is lost because everything durable is in
  SQLite. Real Piper measured far faster than playback in M1/M2 and again in this milestone's real
  smoke test (~6s for 3 chunks / ~101s of audio), so this has not been observed in practice.
- Audio cache eviction, LRU, and disk-cap enforcement (project plan section 12) are not
  implemented; published audio grows unbounded until a reading is explicitly deleted.
- Viewer identity is a lightweight opaque cookie (ownership seam only), not the full LAN
  session/access-code system (ADR-022); `serve --lan` (well, `lan_mode`) is still refused.
- Cache reuse is scoped to one article snapshot; resubmitting the same URL always creates a fresh
  snapshot and rendition rather than deduplicating across readings (ADR-023, matches the plan's
  "refresh is explicit" requirement).
- Idempotency keys are never purged (no 24-hour expiry sweep yet); acceptable at personal-use scale.
- Provisional detector thresholds and extraction quality still need the planned human review of
  twelve representative articles (carried over from M2, unchanged).
- PC B and real phone/browser targets remain unverified (carried over; real-device seam testing is
  M4).

## Next concrete milestone

M4: real-phone/browser seam testing of the progressive-playback reader (buffering feel, "waiting
for the next section" messaging, seek-ahead-of-generated behavior, resume across a voice change),
plus any UX refinement those real devices surface. M5 remains sessions/LAN hardening with real
access codes, audio cache eviction, and portable reading bundles.
