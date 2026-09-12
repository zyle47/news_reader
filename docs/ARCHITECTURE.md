# Architecture

## Shape of the application

Article Reader is a modular monolith with two runtime processes and one local data directory:

```text
browser -> API/UI process -> SQLite + protected audio files
                    worker -> fetch -> extract -> prepare text -> speech engine
```

The API process will authenticate viewers, validate requests, enqueue durable work, expose state,
and serve authorized immutable audio. One worker will claim jobs from SQLite and perform bounded
network, parsing, and CPU-heavy speech work. SQLite is authoritative; files are artifacts whose
metadata and publication state live in the database.

M0/M1 and M2 implement the framework-independent foundation, real speech, text preparation, and
safe fetch/extraction adapters. A loopback-only FastAPI/Jinja preview adapter now proves the full
browser interaction and real audio path. Persistence and worker supervision enter in M3; the
preview adapter's synchronous renderer is replaced by those durable jobs without moving business
logic into routes or JavaScript.

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
- `api/`, `worker/`, and `cli.py` are inbound adapters. `cli.py` is the current composition root.
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
   block/span traceability are implemented. `prepare-article` is the current end-to-end preview
   surface; awaiting-review/language states deliberately stop before speech preparation.
3. Preview UI scaffold: loopback FastAPI/Jinja page, extracted-text review, language/voice choice,
   atomic WAV chunks, range delivery, player controls, and transient same-browser resume. This is
   implemented early at the owner's request so the real pipeline can be tested without the CLI.
4. M3: migrations, repositories, durable jobs, worker, cancellation/recovery, and authenticated
   ownership replace the preview's bounded in-memory/request-scoped execution.
5. M4: progressive generation, durable resume/history, and real-phone/browser seam testing mature
   the scaffold into the full browser reader.
5. M5: sessions/LAN hardening, cleanup, and portable reading bundles.

This ordering keeps high-risk security and durability logic testable without requiring a large
framework graph or a running neural model.
