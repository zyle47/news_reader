# Linux/onion trial evidence — 2026-10-19

Status: running owner trial. T1–T4 implementation is complete and host-tested; T5 physical-phone
acceptance remains open. No phone/browser-version or background-playback result is claimed.

## Actual checks

- Standalone Compose project `reader-onion`, Reader uid/gid `10001:10001`, Tor `10002:10001`.
  Both healthy; no published host ports. Reader model/hostname mounts read-only and no Tor-state
  mount. The services use separate outgoing bridge networks and a shared private Unix socket.
- Linux x86_64 host: 4 logical CPUs, about 13.6 GiB RAM. Container CPython 3.12.15, SQLite 3.40.1,
  Piper 1.8.0, Tor 0.4.9.11. Python dependencies locked; base-image digests pinned in Dockerfiles.
  Runtime limits: Reader 2 CPUs/1536 MiB, Tor 0.5 CPU/256 MiB, bounded logs and process counts.
  Final app image: `sha256:b3d3e775ee3e6c07d4ab6941a55adf590a4729ba0912115156bbe3c541f357ca`;
  Tor image: `sha256:51c886c9ef285125dd12d4afc577d76f0248d05dec042736e7b161838e585542`.
  The final app replacement passed socket readiness/login/library/logout again.
- All three exact registry models downloaded explicitly and SHA-256 verified. Serving/benchmark
  containers used read-only models. Technical benchmark reports remain in the private data volume.
- Separate disposable Tor client reached the actual onion address, checked readiness, denied
  anonymous library access, logged in, read owner data, logged out, and confirmed revocation.
  The Python smoke explicitly returns the cookie to one hostname; it does not test a browser's
  Secure-cookie exception for HTTP onion origins.
- Full live API path over Tor: public Python.org essay submitted with English override, direct
  server-side fetch/extraction completed, real Piper audio published progressively, authenticated
  `Range: bytes=0-9` returned 206/RIFF bytes, authenticated HEAD returned 200, anonymous HEAD 401.
  The public smoke reading remains in history. No extraction-review bypass was added.
- Real cross-container PID-namespace contention refused duplicate ownership. SIGKILL of a
  disposable holder released the lock and a fresh container acquired the same inode. The live
  Reader also refused a harmless duplicate acquisition. Owner administration and serving share
  this lock; Linux release never unlinks it.
- Reader image replacement and stopping/restarting both Reader/Tor preserved the owner/library,
  model installation, and public hostname. Measured idle stop of both services completed in under
  one second; synthesis-stop behavior has an explicit regression test. Extraction remains bounded
  by the fetch deadline; an unresponsive worker forces process exit after the shutdown bound.
- Stopped-service backup created private `data.tar`, `identity.tar`, and checksummed manifest.
  Restore into fresh `reader-restore-20261019` volumes did not start Tor. The restored owner could
  log in, SQLite opened at schema 4, every ready audio chunk matched its recorded SHA-256, and
  numeric data/Tor ownership and original public hostname were preserved. Those isolated volumes
  remain unpublished. The private backup remains under ignored `runtime/backups/20261019-first`.
- Chat containers remained running with the same configuration and existing Git changes throughout.
  No chat edits or restarts were performed. This proves coexistence, not a phone usability test.

## Real synthesis measurements

Technical samples, not new human voice-quality acceptance. The owner's previous voice approvals
remain documented separately. Peak sampled RSS was about 318 MiB; benchmark CPU used up to about
two logical cores. First-audio latency across Tor was not measured precisely.

| Sample | Generated audio | Synthesis wall time | Real-time factor |
| --- | ---: | ---: | ---: |
| English | 10.63 s | 1.32 s | 0.124 |
| German | 10.81 s | 1.30 s | 0.120 |
| Serbian Latin | 9.43 s | 0.73 s | 0.077 |
| Serbian Cyrillic | 9.33 s | 0.60 s | 0.064 |

## Automated gates

- 453 pytest tests passed, two Windows-only cases skipped, five subtests passed. One upstream
  Starlette/AnyIO deprecation warning remains. Later checks should record their actual counts.
- Strict mypy passed across 121 source/test files; Ruff lint/format and Git whitespace checks pass.
- Ten Claude harness tests pass; the saved UUID was resumed, using Sonnet (reported model
  `claude-sonnet-4-6`). Opus 5.5 required a newer CLI than installed. A stalled broad turn was stopped
  without edits; Claude completed the focused advisory-lock implementation and subprocess tests.
- SpiderMonkey/GJS syntax and UI smoke passed login, password clearing, owner device UI,
  unauthenticated recovery, evicted audio, paused leases, and existing LAN pairing.
- Deterministic fake-speech tests cover storage budgets/floors, reservations, active response pins,
  durable leases, cancellation/deletion, orphan restart cleanup, missing files, regeneration,
  bounded history/jobs, and shutdown. Security tests cover streaming body limits, concurrency,
  durable login throttle, session reuse/revision/revocation, real audio HEAD/Range denial,
  unsafe hostname handoff files and CLI rejection without TCP fallback.

## Remaining physical-phone gate

Record phone OS, exact Tor browser/version/security level, successful Secure-cookie login and
remembered session, cookie-clear/password re-login restoring history, and actual listening to
news in all languages/both Serbian scripts. Check seek/speed/section transitions, network changes,
revocation, second-browser progress conflicts, buffering, and screen-off/background behavior.
Measure sustained Tor playback and chat responsiveness together before calling the deployment
fully supported. Portable reading bundles remain outside this upgrade.
