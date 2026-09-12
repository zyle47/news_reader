# Implementation status

Updated: 2026-09-12

## Current milestone

M0 architecture, M1 voice feasibility, and M2 article extraction/text preparation are implemented.
A functional loopback browser preview now runs the real URL-to-Piper path without preparation CLI
steps. Persistence, durable worker jobs, cancellation/recovery, saved history, and LAN access have
not started.

## What works

- Three checksum-pinned Piper voices are owner-approved and locally verified for English, German,
  Serbian Latin, and Serbian Cyrillic.
- `fetch-article` accepts bounded public HTTP(S), revalidates DNS/IP policy on every redirect,
  connects to the validated address with the original TLS/HTTP identity, and extracts ordered
  reviewable blocks without parser-triggered downloads.
- `prepare-article` composes fetch/extract with a local py3langid adapter, independent Unicode
  script evidence, a versioned fail-closed selection policy, review acknowledgement, and the
  existing normalizer/segmenter.
- Automatic selection considers the detector's full language set. It abstains on insufficient or
  low-confidence text, unsupported languages, metadata conflict, script uncertainty, and Latin
  Serbian/Croatian/Bosnian ambiguity. Manual overrides are recorded and bypass detector loading.
- Prepared manifests map the title and every spoken block to exact character spans and SHA-256
  digests. Every speech segment records its source spans, title membership, and block ordinals.
  Unspeakable review blocks are visibly listed as omitted; they never enter speech silently.
- `prepare-text` remains the bounded UTF-8 manual fallback and now uses the shared atomic JSON
  writer.
- `serve` opens a responsive FastAPI/Jinja reader at `http://localhost:8765/`: paste a URL, inspect
  every extracted block and omission, resolve review/language/script, choose a compatible approved
  installed voice, generate atomic WAV sections, and play them in the page.
- The player has native play/pause/seeking, previous/next section navigation, 0.75x-1.5x speed,
  same-browser position restore, active-source-block highlighting, and cross-tab pause coordination.
- Preview mutations enforce exact loopback Host/Origin and JSON content boundaries. Extracted text
  is inserted with DOM `textContent`; the page has a restrictive CSP and audio paths use generated
  IDs plus content digests. Byte-range delivery is covered by an HTTP integration test.

## Real runtime evidence

- All three installed voice model/config pairs pass exact SHA-256 checks;
  `doctor --data-dir runtime/voice-data` reports ready.
- Real Piper output was non-silent mono PCM at 22050 Hz and about 24x faster than playback on the
  inspected Windows 11 / Ryzen 5 5600X host.
- Real py3langid 0.4.0 checks identified representative English, German, Serbian Cyrillic, and
  unsupported French text correctly. A Serbian Latin sample ranked Bosnian/Croatian/Serbian
  closely and was correctly routed to manual override.
- A real `https://example.com/` run completed the pinned network, extraction, automatic English
  selection, review acceptance, preparation, and atomic manifest path. The manifest maps its title
  and both extracted blocks into one prepared segment.
- The loopback UI fetched the real Intermagazin article used by the owner, auto-selected
  `sr/cyrillic`, exposed 1,608 speech characters in three source-traceable sections, selected the
  installed approved Marko voice, generated real audio within the five-second observation interval,
  and successfully played/paused it in the embedded browser player.
- That live check exposed and fixed a manual-script integrity gap: an explicit script that conflicts
  with dominant extracted script evidence is now rejected rather than mislabeled.

## Quality gates

```text
ruff check .                  All checks passed
ruff format --check .         82 files already formatted
mypy                          no issues in 74 source files
pytest -q                     287 passed, 1 skipped, 5 subtests passed
uv lock --check               lockfile current (56 packages)
uv sync --locked --offline    checked 55 installed packages with no network access
uv build --offline            source and wheel distributions built
```

A fresh CPython 3.12 environment installed the built wheel and all 42 runtime packages from the
local cache with networking disabled. The packaged `serve` command then started successfully,
served its packaged Jinja/CSS/JavaScript assets, and reported all three approved voices installed.
The one skipped test requires Windows symlink privileges unavailable to this account; equivalent
link rejection is covered elsewhere.

## Known gaps

- The provisional detector thresholds and extraction quality still need the planned human review
  of twelve representative articles across sites/languages, including both Serbian scripts.
- Automatic Serbian Latin selection is intentionally unsupported; the reader must explicitly
  choose `--language sr --script latin`.
- Paywalls, authentication, consent gates, CAPTCHAs, and JavaScript-only sources intentionally use
  the manual-text fallback; the app does not bypass them.
- The browser preview is intentionally transient and synchronous: generation finishes all sections
  before playback starts, state disappears on restart, and preview WAV cleanup is not yet the M3
  cache lifecycle. Durable jobs, progressive ready-prefix playback, cancellation/recovery, history,
  and authenticated ownership remain.
- The preview is loopback-only. Phone/LAN use waits for access codes and durable browser sessions;
  the app refuses LAN mode instead of exposing article/audio data without authentication.
- PC B and real phone/browser targets are unverified. This folder is not a Git repository.

## Next concrete task

Start M3 with the persistence contract: versioned SQLite migrations plus repositories for immutable
article snapshots, language/review decisions, prepared manifests, renditions, and durable job
attempts. Then move preview synthesis behind the durable worker while preserving the working page
and ordered audio manifest contract.
