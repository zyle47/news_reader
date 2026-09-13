# Architecture

## Shape of the application

Article Reader is a modular monolith. As of M4 it runs as one OS process containing an API/UI
component and one durable worker thread, sharing one local data directory:

```text
desktop loopback socket ─┐
                         ├─> API (FastAPI) -> authenticated SQLite state + protected audio
private LAN socket ──────┘
                    worker thread (durable loop) -> fetch -> extract -> prepare text -> speech engine
```

The API validates requests, reads/writes durable state through repositories, and serves authorized
immutable audio resolved only through database metadata; it never fetches a URL or calls the speech
engine itself. The worker thread claims jobs from SQLite one at a time in FIFO order and performs
all bounded network, parsing, and CPU-heavy speech work, off the API's asyncio event loop. SQLite is
authoritative; audio files are artifacts whose metadata and publication state live in the database.
See `docs/DECISIONS.md` ADR-020 for why the worker is a thread within the API process rather than a
separate OS process, and what that does and does not fence against.

M0/M1 and M2 implement the framework-independent foundation, real speech, text preparation, and
safe fetch/extraction adapters. M3 replaces the transient loopback preview with a durable SQLite
job queue, one background worker thread, and atomic audio publication; the browser reader now
polls durable state and plays back progressively instead of waiting for a synchronous render.
M4 adds explicit private-interface serving, short-lived one-time pairing, durable revocable
sessions, exact Host/Origin enforcement for both sockets, and the responsive mobile/player seam.
The loopback socket is the only place a browser identity may be bootstrapped without pairing.

## Dependency rule

Dependencies point inward:

```text
CLI / API / worker (inbound adapters)
                 |
                 v
application services -> application-owned ports -> domain values
                 ^                                  ^
                 |                                  |
speech / db / fetch / storage (outbound adapters) --+
```

- `domain/` contains immutable values, enums, and invariants. It uses the standard library only.
- `application/ports/` contains narrow `Protocol` interfaces required by use cases.
- `application/services/` orchestrates use cases without knowing concrete engines or frameworks.
- `speech/`, `db/`, `fetch/`, `extract/`, `storage/`, and `text/` contain concrete outbound
  adapters. `text/` contains pure script/normalization/segmentation logic, a bounded local-file
  loader, and the isolated local py3langid adapter; it has no network or database dependency.
  `db/` owns SQLite connections, migrations, and every repository; it is the only package
  allowed to import `sqlite3`. `storage/durable_audio.py` and `storage/process_lock.py` are the
  concrete durable-audio and single-instance adapters added in M3.
- `network.py` discovers/selects RFC1918 or IPv6 ULA addresses without an external discovery
  service. `security/pairing_qr.py` is the only QR renderer. The application-owned
  `application/services/access_control.py` contains pairing/session/rate-limit policy and depends
  only on the extended `ViewerRepository` port.
- `api/`, `worker/`, and `cli.py` are inbound adapters. `cli.py` is the current composition root:
  it is the only module that constructs both the FastAPI app (`api/app.py`) and the durable
  worker (`worker/loop.py`) against the same shared `db.connection.Database`.
- `resources/` contains versioned, non-secret metadata bundled with the application.

Only a composition root may know both an application service and its concrete adapters. Domain and
application modules must not import FastAPI, SQLite adapters, Piper, or platform-specific process
launchers. No module may open a database, create directories, make a network request, load a model,
or start a process merely because it was imported.

## Core design constraints

- Configuration is immutable after composition and validated before work starts.
- Side effects sit behind narrow boundaries and are injected into application services.
- A speech engine is worker-scoped and treated as stateful and not thread-safe.
- Voice installation, technical evaluation, and human approval are separate states.
- Routine tests use deterministic fakes and require neither a network nor a model download.
- Errors cross boundaries as stable domain/application error codes, not raw library exceptions.
- Local paths never come from article titles, URLs, or imported bundle paths.
- Runtime data stays outside Git and cloud/network-synchronized directories.

## Planned module growth

New modules should be added when their milestone introduces a real use case, not pre-created as
empty abstraction layers:

1. M0/M1: configuration, diagnostics, voice catalogue, speech port, fake engine, evaluation
   reports, safe voice installer, and a real Piper adapter behind the same port.
2. M2: bounded public fetching, structured extraction/review, local language and script evidence,
   explicit override, original/display/speech separation, normalization, segmentation, and
   block/span traceability are implemented.
3. M3 (this milestone): `db/` (connection, migrations, eight repositories),
   `application/ports/persistence.py` and `application/ports/durable_audio.py` (the protocols the
   durable layer depends on), `application/services/reading_service.py` (durable orchestration for
   the API side: submission, review/language resolution, rendition creation with contract-hash
   reuse, cancel/retry, progress), `application/services/rendition_contract.py` and
   `persistence_mapping.py`, `worker/loop.py` (the one durable worker), `storage/durable_audio.py`
   (fsync-validate-rename publication), and `storage/process_lock.py` (single-instance enforcement)
   replace the M2-era preview's bounded in-memory/request-scoped execution. `api/app.py` is
   rewritten around these instead of the transient `_PreviewStore`. The browser reader is rewired
   in place (`web/static/app.js`, `web/templates/index.html`) to poll durable job/rendition state,
   play back progressively as chunks publish, and offer working cancel/retry — it is not a
   separate scaffold-to-final rewrite, since the M2-era preview page's design and controls are
   preserved.
4. M4 (this milestone): responsive mobile UI, Media Session/Wake Lock enhancement, reconnect and
   voice-change recovery, explicit dual-socket LAN serving, QR/code pairing, durable session
   expiry/revocation, rate limiting, and authenticated progress/audio delivery. A real desktop
   browser completed the LAN pairing path; physical-phone playback remains an owner checklist.
5. M5: audio cache eviction/disk-cap enforcement, playback leases, portable reading bundles, and
   the PC B/physical-phone pilot.

This ordering keeps high-risk security and durability logic testable without requiring a large
framework graph or a running neural model.
