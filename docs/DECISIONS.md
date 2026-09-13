# Architecture decisions

## ADR-001: Modular monolith with ports and adapters

**Status:** Accepted, 2026-09-11

Use one installable Python package, one API process, one worker process, SQLite, and local immutable
audio files. Keep domain values and application services independent of delivery frameworks and
infrastructure by defining narrow application-owned protocols.

This satisfies the small-group/local-first scope while preserving seams around the highest-risk
parts: untrusted URL retrieval, extraction, speech engines, durable jobs, and file publication. A
distributed task system, frontend framework, or generic dependency-injection container would add
operational cost without solving a current requirement.

## ADR-002: Target CPython 3.12 and use uv with a committed lockfile

**Status:** Accepted, 2026-09-11

The initial supported runtime is CPython 3.12, expressed as `>=3.12,<3.13`, with `.python-version`
requesting 3.12. `uv.lock` is generated, never hand-edited, and is the reproducibility contract for
both PCs. Compatibility with later Python minors must be demonstrated before widening the range.

The pre-existing local `.venv` uses Python 3.14 and is not evidence that the target speech stack is
compatible. It remains machine-local and disposable.

## ADR-003: Keep the initial core standard-library-only

**Status:** Accepted, 2026-09-11

M0 and the model-independent part of M1 use standard-library dataclasses, protocols, TOML parsing,
WAV handling, and CLI parsing. Third-party packages enter only at concrete boundaries when their use
case arrives. This keeps diagnostics and fake-engine tests runnable offline and prevents FastAPI or
Piper from becoming domain dependencies.

## ADR-004: Voice approval is explicit and fail-closed

**Status:** Accepted, 2026-09-11

The packaged catalogue starts empty. A real entry requires a stable ID, immutable source revision,
artifact SHA-256 digests, engine version, language/script declaration, and license/provenance
references. Installation and technical evaluation do not imply human approval. Automatic selection
will consider only voices explicitly marked approved; absence must produce `VOICE_UNAVAILABLE`.

This avoids fabricating model checksums or treating a misleading Serbian catalogue label as product
acceptance.

## ADR-005: Evaluation evidence is immutable and all-or-nothing

**Status:** Accepted, 2026-09-12

Each evaluation uses a unique run directory and records the runtime, engine, exact model contract,
settings, corpus, timings, and canonical contract digest. Files are staged and the complete run is
published with one directory rename. An engine, sample-rate, or write failure therefore cannot
leave a report that mixes old and new evidence. Fake-engine runs are explicitly marked as technical
plumbing evidence, never voice-quality evidence.

## ADR-006: Diagnostics distinguish health from product readiness

**Status:** Accepted, 2026-09-12

`doctor` reports host checks independently from readiness. A writable and compatible host can be
healthy while the application remains not ready until approved, installed voice coverage exists
for English Latin, German Latin, Serbian Latin, and Serbian Cyrillic.

## ADR-007: Piper is a normal pinned dependency; the adapter is the only module that imports it

**Status:** Accepted, 2026-09-12

`piper-tts` (pinned `>=1.8.0,<1.9`) is a regular `pyproject.toml` dependency, not an optional
extra. It installs a stable-ABI wheel for CPython 3.9+ (including 3.12) on Windows, Linux, and
macOS, verified by a real install into `runtime/venv312` and a clean-wheel install into an
isolated venv. Only `src/article_reader/speech/piper_engine.py` imports `piper`; the port
(`application/ports/speech.py`) and every domain/application module remain framework-free.
`PiperEngine` accepts an injectable `voice_factory`, so offline unit tests exercise load/config/
synthesis failure paths with a fake `PiperVoice` and never need a real model or network access.

## ADR-008: Voice installation is a separate, explicit, checksum-gated adapter

**Status:** Accepted, 2026-09-12

`src/article_reader/speech/installer.py` downloads only the exact HTTPS URLs recorded on a
validated `VoiceSpec` (never an article- or user-supplied URL), enforces HTTPS on every redirect
hop (bounded to 5), enforces a configurable byte cap and wall-clock timeout, stages each artifact
in a sibling temporary file, verifies its SHA-256 before an atomic `os.replace`, and deletes the
temporary file on any failure. It does not implement the full SSRF DNS-pinning policy planned for
the article-fetching downloader (`docs/ARCHITECTURE.md` M2): that control exists specifically for
attacker-influenced URLs, which model installation never accepts. Installing a non-approved voice
requires an explicit `--for-evaluation` CLI flag, matching the project plan's distinction between
evaluation installation and automatic-use approval.

## ADR-009: Real candidate voices are registered as unapproved, with sourced provenance notes

**Status:** Accepted, 2026-09-12

Approval status was subsequently updated by ADR-013; this ADR preserves the original candidate
selection and provenance decision.

Three checksum-pinned candidates were added to `resources/voices.toml`, each verified against
primary sources (HuggingFace API tree listings for exact commit-pinned SHA-256 digests, dataset
license pages, and a real, non-fake technical evaluation run with the actual Piper adapter):

- `en_US-ljspeech-medium` (public-domain LJSpeech dataset) â€” status `candidate`.
- `de_DE-thorsten-medium` (CC0 Thorsten-Voice dataset, but finetuned from the Lessac checkpoint,
  which carries a restrictive research license; flagged in `evaluation_notes`) â€” status
  `candidate`.
- `sr-marko-medium` (community model; its own MODEL_CARD mislabels the `sr` code as Slovenian and
  its README says it was finetuned from a Slovenian base model, not trained on Serbian speech) â€”
  status `experimental`, registered for both Latin and Cyrillic since piper-tts's bundled
  espeak-ng phonemizer produces identical IPA output for equivalent text in each script (verified
  directly, not assumed).

The previously catalogued `sr_RS-serbski_institut-medium` (actually Lower Sorbian) is intentionally
never added. At initial registration no voice was approved; the later human decision is ADR-013.

## ADR-010: Long-form evaluation reuses the existing evaluation service; the report schema is
now versioned at 3

**Status:** Accepted, 2026-09-12

`evaluate-voices --text-file <path>` (requiring an exact `--voice-id`, and `--script` whenever the
voice supports more than one) loads one bounded local UTF-8 file into a single long `EvaluationPassage`
and runs it through the unchanged `VoiceEvaluationService`. No new evaluation machinery was needed:
a "long-form" passage is just a passage with much more text. `EvaluationSample` gained explicit
`language`/`script` fields (previously only inferable from the voice's overall registry entry, not
recorded per sample), so `VoiceEvaluationReport.schema_version` moved from 2 to 3. Old schema-2
reports remain on disk as historical evidence but a report is only ever read back by exact revision;
nothing reinterprets old reports under the new schema.

## ADR-011: Model-independent M2 core initially ships without the network downloader or extraction adapter

**Status:** Accepted, 2026-09-12; planned work completed by ADR-014

This slice adds `domain/text.py` (immutable `OriginalText`, `SourceSpan`, `TextSegment`,
`PreparedText`) and the concrete `text/` package (`loader.py`, `normalize.py`, `segment.py`) plus a
`prepare-text` CLI command, all operating only on a local, explicitly supplied UTF-8 file â€” the
project plan's "manual paste" path. The safe pinned HTTP(S) downloader and Trafilatura-based HTML
extraction adapter described in `docs/ARCHITECTURE.md` M2 are deliberately not implemented here:
building genuine SSRF protection (DNS resolution and IP validation repeated at every redirect hop,
connection-level address pinning that still preserves TLS SNI/hostname) is a substantial,
security-critical unit of work in its own right, and the task authorizing this slice explicitly
allowed deferring it rather than weakening those requirements to finish faster. No
`application/ports/fetch.py` or similar port was pre-created either, since a port with no real
adapter or use case yet would be exactly the speculative abstraction the project avoids (ADR-003).
The next M2 task is that downloader and extraction adapter, implemented together so the port is
introduced alongside its first real, fully-hardened implementation.

## ADR-012: Segmentation is a pragmatic rule-based splitter, not a statistical tokenizer

**Status:** Accepted, 2026-09-12

`text/segment.py` finds sentence boundaries by scanning for terminal punctuation while protecting
periods that are (a) between two digits (decimal/thousands separators), (b) part of a small,
per-language abbreviation list (`Dr.`, `z. B.`, `Ð¸Ñ‚Ð´.`, ...), or (c) a 1â€“2 digit ordinal-date marker
for German/Serbian (`11. September`, `11. septembra`) regardless of the following word's case â€”
Serbian ordinal dates are followed by a lowercase month name, so German's capitalization cue does
not generalize. Terminal punctuation inside a quoted or parenthetical aside only ends the sentence
if what follows plausibly starts a new one (uppercase, a digit, end of paragraph, or an opening
quote/bracket); otherwise scanning continues past it. This covers the abbreviation/date/number/
quotation cases the project plan calls out, verified with table-driven tests, but it is not a
general-purpose sentence tokenizer and will mishandle rarer constructions (e.g. a sentence that
truly ends in a bare one- or two-digit number). Source spans are computed on the untouched original
text and only trimmed of surrounding whitespace; normalization (Unicode NFC, whitespace collapse)
is applied solely to derive each segment's separate `speech_text`, never rewriting `OriginalText`.
Serbian Cyrillic is never transliterated, consistent with ADR-009's phonemizer-equivalence finding.

## ADR-013: The fluent owner approves all three voices for local personal use

**Status:** Accepted, 2026-09-12

After real technical evaluation, the fluent project owner listened to short and sustained samples
for English, German, Serbian Latin, and Serbian Cyrillic and explicitly approved all three voices
for the intended personal article-reading use. The catalogue statuses are therefore `approved`,
and installed copies satisfy readiness for their declared languages and scripts.

This quality decision does not erase provenance or redistribution constraints. The German
Lessac-checkpoint warning and the Serbian model's Slovenian-derived lineage and undisclosed source
dataset remain documented in the catalogue. Approval is for local personal use and is not a
release-license determination. The listener record is in `docs/VOICE_REVIEWS.md`.

## ADR-014: URL ingestion uses a DNS-pinned urllib3 transport and byte-only Trafilatura adapter

**Status:** Accepted, 2026-09-12

Article URLs cross an application-owned `ArticleFetcher` port into `SafeHttpArticleFetcher`.
Every submitted or redirected URL is limited to HTTP(S), ports 80/443, no credentials, and an
unambiguous hostname. DNS is resolved explicitly; the request is rejected if any answer is not a
globally routable address. The transport then connects directly to one validated address while
retaining the original hostname for TLS SNI, certificate verification, and the HTTP Host header.
Redirects are handled manually and repeat the complete validation. Direct connection pools avoid
ambient proxy and cookie state.

Wire bytes and decoded bytes have separate caps; DNS, connect, body reads, redirect count, URL
length, compression, status, media type, HTML sniffing, and overall elapsed time are bounded or
classified into stable errors. The policy intentionally does not bypass authentication, paywalls,
consent gates, CAPTCHAs, or JavaScript rendering.

Trafilatura is a concrete extraction adapter and receives the fetched bytes, never a URL it could
download itself. It returns ordered immutable blocks with separate display/speech forms, stable
hashes, and explicit review flags for complex tables/code, suspicious restrictions, short output,
or missing structure. The CLI can publish the complete provenance and preview atomically as a
schema-v1 JSON document. `urllib3>=2.7,<3` and `trafilatura>=2.2,<2.3` are normal locked runtime
dependencies, imported only by their concrete adapters.

## ADR-015: py3langid supplies evidence; a versioned application policy makes language choices

**Status:** Accepted, 2026-09-12

`py3langid>=0.4,<0.5` is the local detector behind the application-owned `LanguageDetector`
port. Its small platform-independent wheel includes English, German, Serbian, Croatian, Bosnian,
and a non-language class, while its full-language ranking lets unsupported languages remain
unsupported instead of forcing every page into one of the product's three languages. The model is
loaded lazily only for automatic selection; an explicit override does not need it.

Detector probabilities are evidence, not product truth. Policy version 1 requires at least 40
alphabetic characters, top confidence 0.80, and a 0.20 lead over the runner-up. It compares the
primary HTML/extractor language hint with the detected language and abstains on disagreement,
unsupported or mixed-script text, low evidence, and low confidence. These conservative thresholds
are recorded in every manifest and remain provisional until representative article fixtures are
reviewed.

Serbian Latin, Croatian, and Bosnian are too close for this application to relabel safely.
Automatic selection therefore always returns an awaiting-language state when a Latin-script BCMS
candidate leads. A user may explicitly choose `sr/latin`. High-confidence `sr` with dominant
Cyrillic evidence may select `sr/cyrillic` automatically. Explicit Serbian overrides always require
the script, and no path transliterates the text.

`PreparedArticle` maps the title and every included block's speech representation into the
immutable `OriginalText`; omitted unspeakable blocks, per-block spans/digests, segment spans, source
block ordinals, policy/detector versions, and review acceptance are all present in the atomic
manifest. Review-required extraction never becomes ready without `--accept-review`.

## ADR-016: Ship a loopback preview reader before durable M3 jobs

**Status:** Accepted, 2026-09-12

The owner requires a minimum UI to test the actual product without translating JSON manifests into
evaluation CLI commands. The plan explicitly permits UI scaffolding before M3, so `serve` now
composes a FastAPI/Jinja inbound adapter around the completed M2 services and the approved Piper
adapter. It shows extracted blocks and omissions before review acceptance, resolves language on the
same immutable fetched snapshot, renders source-traceable WAV sections atomically, and exposes an
ordered browser playlist with navigation, speed, local position restore, and byte-range delivery.

This does not pretend M3/M4 durability exists. Preview records are bounded in process memory;
synthesis is serialized and request-scoped; audio starts after all sections finish; history,
cancellation/recovery, cache eviction, sessions, and LAN mode remain unavailable. The server
therefore binds only to loopback and rejects unexpected Host, Origin, and non-JSON mutation
requests. M3 will replace transient state and request-scoped synthesis behind the existing browser
interaction instead of discarding the useful UI.

FastAPI 0.141, Uvicorn 0.52, and Jinja2 3.1 are normal locked runtime dependencies. Templates and
static assets are packaged with the application; no JavaScript build toolchain or remote asset is
introduced.

## ADR-017: Versioned SQLite migrations and repositories replace the transient preview state

**Status:** Accepted, 2026-09-12

`db/connection.py` owns one `Database` per data directory: it opens per-thread `sqlite3`
connections with `PRAGMA foreign_keys = ON`, WAL journaling, and a 5-second busy timeout,
and applies `db/migrations.py`'s ordered SQL migrations inside `BEGIN IMMEDIATE` transactions
tracked by `PRAGMA user_version`. Opening a database whose `user_version` is newer than the
running build's `CURRENT_SCHEMA_VERSION` raises `IncompatibleSchemaError` instead of
resetting it — verified with a real refused-open test that leaves the file untouched.
Migrations avoid forward-referencing foreign keys entirely (every `REFERENCES` points at a
table already created earlier in the same migration) so schema application order never
depends on SQLite's deferred foreign-key name resolution.

Eight concrete repositories in `db/repositories/` (viewers, readings, articles, renditions,
audio chunks, jobs, idempotency, progress) implement the `Protocol`s declared in the new
`application/ports/persistence.py`. Domain and application modules depend only on those
protocols and the plain-dataclass records they exchange, never on `sqlite3`; only `db/`,
`worker/`, and `api/app.py` (the composition root for API wiring) import the concrete
classes, preserving ADR-001's dependency rule. This let the durable job orchestration in
`application/services/reading_service.py` and `worker/loop.py` be unit-tested against a real
temporary SQLite file without any FastAPI or Piper dependency.

The transient preview scaffold from ADR-016 (`application/services/preview_audio.py`,
`storage/preview_audio.py`, `application/ports/audio.py`, and their tests) is deleted outright
now that it is fully superseded, rather than kept alongside the durable path as dead weight.

## ADR-018: One global FIFO job queue with per-claim generation tokens and `BEGIN IMMEDIATE` claiming

**Status:** Accepted, 2026-09-12

`jobs.claim_next` selects the oldest `queued` row (across both `prepare` and `synthesize`
kinds, matching the plan's single sequential worker) and updates it to `running` inside one
`BEGIN IMMEDIATE` transaction. SQLite's own writer-serialization — not application-level
locking — is what makes two concurrent claimers resolve to exactly one winner; this is
verified with a real two-thread race in `tests/test_job_queue.py`, not just reasoned about.
Every claim mints a fresh random `worker_generation` token (not one token per worker
process): `renew_lease`, `complete`, `fail`, and `mark_cancelled` all require the caller's
token to still match the row, so a worker that loses its lease (or a stale worker resuming
after what it thinks was a hiccup) can never mutate a job another attempt now owns.

Cancellation has two paths tested explicitly: a still-`queued` job is cancelled immediately
(never claimable), while a `running` job moves to `cancelling` and the worker itself
transitions it to `cancelled` the next time it checks — because a worker cannot promise
instantaneous cancellation while native Piper inference is in flight. A crashed worker is
detected by comparing `lease_expires_at` against the current time; `recover_interrupted`
requeues it up to `worker.max_automatic_recoveries` times before leaving it `interrupted` for
an explicit retry. `create_retry` links a new job row to the old one (`previous_job_id`,
`attempt + 1`) and only accepts a `failed`, `cancelled`, or `interrupted` source job. A
partial unique index (`ux_jobs_active_synth`) additionally guarantees at most one active
synthesize job per rendition at the database level, independent of any application check.

## ADR-019: Durable audio publication commits to the database only after the file is proven valid

**Status:** Accepted, 2026-09-12

`storage/durable_audio.py`'s `LocalDurableAudioStore.stage` writes each chunk's WAV bytes to
a temporary file beside its final location, flushes and `fsync`s it, closes it, re-opens and
re-parses it with the standard library `wave` module to confirm its channel count, sample
width, frame rate, and frame count match the synthesized `AudioResult` exactly, computes its
SHA-256 from the bytes actually on disk, and only then `os.replace`s it to its final
content-addressed name (`<ordinal>-<sha256>.wav`). No database row exists yet at this point,
so a crash here can only ever leave an orphan file, never a row pointing at nothing.

`db/repositories/audio.py`'s `publish_chunk` is the sole place a row is committed: inside one
transaction it re-checks that the calling job still holds a `running`/`cancelling` state with
a matching `worker_generation` and that `cancel_requested` is not set, *before* inserting the
chunk row and bumping the rendition's `manifest_revision`. This closes the exact race the
project plan calls out — a worker whose lease was reassigned, or whose job was cancelled
between finishing synthesis and committing, cannot publish stale or unwanted audio. All three
rejection paths (`duplicate`, `stale_generation`, `cancelled`) are exercised by real two-actor
tests, and the worker's cancellation-during-publish path is exercised end to end in
`tests/test_worker_loop.py`.

## ADR-020: The durable worker runs as an in-process daemon thread, not a separate OS process

**Status:** Accepted, 2026-09-12

The project plan's architecture describes "one Python worker" as a separate process from the
API, with a launcher that fences and stops an old worker before starting its replacement.
Building genuine cross-platform process supervision (spawn without `fork`, a real
inter-process fencing protocol beyond lease expiry, coordinated shutdown) is substantial
independent work that this slice defers, matching the plan's own list of remaining facts to
establish. Instead, `cli.py`'s `_run_serve` starts one dedicated non-daemon `WorkerLoop`
thread inside the same OS process as the Uvicorn server, stops it via a `threading.Event` and
joins it (bounded 30 seconds) in a `finally` block, and holds a new
`storage/process_lock.InstanceLock` for the whole process lifetime so a second `serve`
invocation against the same data directory is refused rather than silently running two
workers.

This still satisfies the concrete requirements that motivate the "separate process" framing:
Piper synthesis and article fetching never occupy the API's asyncio event loop (they run on
the dedicated thread), FastAPI `BackgroundTasks` are never used as the queue (SQLite is, via
`WorkerLoop.run_forever`), and per-claim generation tokens make a stale attempt's writes
rejected exactly as if it were a separate process that lost a lease. What it does *not* yet
provide is fencing against two independent OS processes started against the same data
directory from different terminals without going through `InstanceLock` (e.g., a hand-rolled
script bypassing `serve`), or surviving an API-thread crash that takes the whole interpreter
down with it. True multi-process supervision remains a candidate for later hardening if a
single worker thread's blast radius (one unhandled exception can, in principle, affect the
same process as the API) proves insufficient in practice.

## ADR-021: Chunk synthesis timeout is enforced with a daemon thread, not a killable subprocess

**Status:** Accepted, 2026-09-12

The plan requires "a supervisor or bounded subprocess/watchdog" so a chunk timeout is
effective "even if the inference call hangs." Python cannot forcibly terminate a thread, and
`concurrent.futures.ThreadPoolExecutor` specifically is the wrong primitive here: its worker
threads are joined by an `atexit` hook, so a hung call submitted through it would block
interpreter shutdown indefinitely even after the caller gives up waiting. `worker/loop.py`'s
`_run_with_timeout` instead runs the synthesis call on a plain `daemon=True` `threading.Thread`
and joins it with a timeout; on expiry it raises `SynthesisTimeoutError`, fails the job with
`SYNTHESIS_TIMEOUT`, and moves on to the next job. The daemon thread is deliberately abandoned
running in the background rather than tracked further.

This is a real, accepted limitation, not a full fix: the abandoned call keeps consuming a
thread (and whatever CPU/memory Piper is using) until it naturally returns, and a genuinely
hung native call would still block a *second* attempt at the same chunk if the worker loop
ever tried to reuse that thread (it does not — each call gets its own thread). The documented
recovery for a truly hung synthesis is the same as for any other worker fault: stop and
restart the `serve` process; the durable job/lease model ensures no state is lost by doing so.
A subprocess-per-chunk architecture would close this gap completely and is deferred pending
evidence it is actually needed — real Piper synthesis measured roughly 24x faster than
playback in M1/M2, and the M3 real-article smoke test (see `docs/STATUS.md`) synthesized
three chunks of Serbian audio in about 6 seconds total.

## ADR-022: Viewer identity is a lightweight opaque cookie, not the full session system

**Status:** Accepted, 2026-09-12

The project plan's `viewers`/`sessions` entity anticipates password-hashed LAN access codes,
rate-limited session creation, and explicit revocation — all of that is still deferred to the
LAN-hardening milestone, since `serve` continues to refuse `lan_mode`. M3 needs an ownership
seam now (durable per-viewer readings, history, and progress) without pretending that
security work is done. `api/app.py`'s `_resolve_viewer` issues a high-entropy
`secrets.token_urlsafe(32)` cookie (`HttpOnly`, `SameSite=Lax`, no `Secure` flag since the
server is plain HTTP loopback), stores only its SHA-256 hash via
`SqliteViewerRepository.issue_token`, and creates a new `viewers` row on first sight. Every
reading/job/rendition/progress lookup is scoped through this viewer id, and cross-viewer
access is rejected as a plain 404 (verified in `tests/test_api.py`) rather than 403, so a
guessed id does not confirm existence. There is intentionally no login, no revocation
endpoint, and no rate limiting yet: those remain real gaps until LAN mode is implemented, at
which point this same cookie mechanism is the natural seam to extend with an access code.

## ADR-023: Rendition cache reuse is scoped to one article snapshot, not shared across readings

**Status:** Accepted, 2026-09-12

`application/services/rendition_contract.py` hashes the prepared segments' content digests,
language/script, and the full voice/engine/model/config/settings identity into one
`contract_hash`, unique per `(article_id, contract_hash)` at the database level. Within one
reading, re-requesting the same voice returns the existing rendition (or, if it previously
failed/was cancelled, links a new job to resume it) instead of resynthesizing — this is the
plan's "reuse a matching ready rendition where valid." Resubmitting the *same URL* as a new
reading, however, always creates a fresh article snapshot and a fresh rendition, matching the
plan's explicit statement that "refresh is an explicit new submission/snapshot" and that
cross-user (and, in this slice, cross-reading) cache deduplication is deferred. Implementing
article-level dedup by (normalized URL, viewer) would risk silently serving stale content
under a "not actually refreshed" URL and was judged not worth that risk for M3.

## ADR-024: A Windows liveness check needs explicit `HANDLE` typing and an exit-code check

**Status:** Accepted, 2026-09-12

The real-article smoke test (see `docs/STATUS.md`) caught two live bugs in
`storage/process_lock.py`'s stale-lock recovery that no synthetic-PID unit test had
surfaced. First, `ctypes.windll.kernel32.OpenProcess` returns a pointer-sized `HANDLE` (8
bytes on 64-bit Windows); without explicit `argtypes`/`restype` ctypes assumes a 32-bit
`c_int` return and silently truncates it, which can misreport an exited, PID-recycled process
as alive. Second, even with correct typing, `OpenProcess` can succeed against a process that
has already exited but not yet been fully reaped by the OS (its kernel object persists until
every handle to it closes) — a successful open is not proof the process is still *running*.
`_process_is_alive` now declares exact `argtypes`/`restype` for `OpenProcess`, `CloseHandle`,
and `GetExitCodeProcess`, and treats a process as alive only when `GetExitCodeProcess`
reports `STILL_ACTIVE` (259). `tests/test_process_lock.py` regression-tests this by spawning
and waiting on a real subprocess rather than relying on an arbitrary large PID, which is what
let the original bug through.

## ADR-025: LAN mode uses two explicit sockets and one-time pairing into durable sessions

**Status:** Accepted, 2026-09-13

Normal `serve` remains loopback-only. `serve --lan` discovers the private address selected by the
OS default route (or validates an explicit `--bind`) and gives Uvicorn two pre-bound sockets:
`127.0.0.1` and exactly one RFC1918/IPv6-ULA address. It never substitutes `0.0.0.0`, trusts
forwarded headers, opens firewall/router rules, or accepts a public/link-local/reserved address.
Every launch requires the CLI flag even when configuration contains `lan_mode = true`.

Only a request arriving on loopback can create a pairing offer. Its random eight-digit code lives
for five minutes as a SHA-256 digest in a bounded, locked in-process store, is compared in constant
time, works once, and is rate-limited per source address. A restart intentionally invalidates all
offers. The QR contains the phone URL with the one-time code in its fragment, so the code is not
sent in the initial request, access log, or referrer. The browser removes the fragment before
redeeming the code in an exact-origin JSON POST.

Successful redemption issues a high-entropy opaque HttpOnly/SameSite=Strict cookie and stores only
its SHA-256 in SQLite. Schema migration 2 turns the M3 `viewer_tokens` seam into expiring,
labelled, individually revocable `viewer_sessions`; old loopback tokens migrate without losing
their readings. Sessions survive restarts, while expiry/revocation is checked on every protected
route including audio HEAD/Range requests. Pairing joins the phone to the initiating viewer, so the
desktop and phone deliberately share history and revisioned progress. Reaching the bounded session
limit requires explicit revocation rather than silently removing the desktop identity.

The threat model is a trusted home LAN plus hostile webpages. Exact Host/Origin checks, closed
CORS, JSON-only mutations, session ownership, response hardening headers, and no remote assets
address browser-origin and DNS-rebinding attacks. Plain HTTP does not provide confidentiality or
resist an on-path LAN attacker; the UI and launcher say so explicitly. Internet exposure, trusted
local HTTPS, public tunnels, and router forwarding remain out of scope.

## ADR-026: Mobile playback enhances the durable chunk controller without an offline layer

**Status:** Accepted, 2026-09-13

The existing persistent `<audio>` element remains authoritative. M4 adds responsive touch layouts,
pending-section messaging, exponential reconnect polling, `keepalive` progress flushes, clean
rendition switching, Media Session actions, and optional Screen Wake Lock while playing. Every
optional browser API has a fallback. The insecure-LAN page cannot assume secure-context Web Crypto,
so non-security client/idempotency identifiers use `randomUUID` only when available and fall back
to `getRandomValues` or a non-security local identifier; authentication randomness remains wholly
server-side through Python `secrets`.

No service worker, offline promise, background/screen-off guarantee, concatenated WAV, or eager
whole-article buffer is introduced. The browser still begins with the first published chunk and
waits honestly at an unavailable next chunk. A real LAN-IP browser run caught and fixed the
secure-context `crypto.randomUUID` assumption; only a physical-phone pilot can establish device
audio transition and sleep behavior.

## Open decisions

- A verified authentic (non-Slovenian-derived) Serbian voice, if `sr-marko-medium` fails listening
  quality in future use or a better-provenance alternative becomes available.
- Cross-platform multi-*process* worker supervision (see ADR-020); the current single-process,
  single-thread worker plus `InstanceLock` covers the durability and single-ownership
  requirements but not fencing against a hand-rolled second process bypassing `serve`.
- Audio cache eviction/LRU and disk-cap enforcement (project plan section 12) are not implemented;
  M3 durable audio grows unbounded until a reading is explicitly deleted.
- Whether a future internet-facing mode justifies trusted local HTTPS or a separately designed
  authenticated deployment; current LAN HTTP is intentionally limited to trusted networks.
- Application release license, after dependency and model licenses are known (see ADR-009 for the
  Lessac-lineage caveat on the German voice).
