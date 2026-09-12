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

## Open decisions

- A verified authentic (non-Slovenian-derived) Serbian voice, if `sr-marko-medium` fails listening
  quality in future use or a better-provenance alternative becomes available.
- SQLite migration and cross-platform supervisor/process-lock implementations.
- Password hashing implementation and session lifetimes for LAN mode.
- Application release license, after dependency and model licenses are known (see ADR-009 for the
  Lessac-lineage caveat on the German voice).
