# Reader over Tor: implementation and handoff plan

Updated: 2026-10-19 · Implementation status: T1–T4 implemented/host-verified; T5 phone gate open.

Read [TOR_ARCHITECTURE.md](TOR_ARCHITECTURE.md) first. That file defines the proposed contracts;
this file owns task status and evidence. [HANDOFF.md](HANDOFF.md) is the short entry point.
Implemented commands and verified deployment steps are documented in [TOR_SETUP.md](TOR_SETUP.md).
[TOR_ACCEPTANCE.md](TOR_ACCEPTANCE.md) distinguishes actual host tests from the open phone gate.

## Task board

| ID | Deliverable | Dependencies | Status | Assigned agent / branch |
| --- | --- | --- | --- | --- |
| T0 | Architecture, work breakdown, continuation guidance | Source inspection | DONE: documentation only | Initial planning session |
| T1 | Serving-mode policy, owner authentication, container-safe lock | T0 | DONE | Codex auth/integration; Claude lock |
| T2 | Reproducible images, volumes, Tor identity and socket wiring | T1 contracts fixed | DONE: Linux host trial | Codex; Sonnet broad turn stalled |
| T3 | Storage budgets, eviction, playback leases and physical cleanup | T1 migration coordination | DONE | Storage helper; Codex integration |
| T4 | Onion UI, health/operations, recovery and integration | T1, T2, T3 | DONE: host gates | UI/storage helpers; Codex integration |
| T5 | Actual host and phone acceptance, deployment runbook | T4 | PARTIAL: host verified, phone pending | Owner phone; Codex host/runbook |

T0 marks a proposed design written down, not user acceptance of every implementation choice.
The owner confirmed direct article fetching from the Reader container and owner-password login
with remembered sessions on 2026-10-19. These two preferences are settled. Browser cookie clearing
requires re-login to the same library; it must not create a new viewer or lose reading history.

## T1: modes, access, and process ownership

Primary files: `src/article_reader/config.py`, `cli.py`, `api/app.py`,
`application/services/access_control.py`, `application/ports/persistence.py`,
`db/migrations.py`, `db/repositories/viewers.py`, `storage/process_lock.py`.
Add a narrow password-hasher port/adapter and owner repository only where needed.

Deliverables:

- Explicit desktop/LAN/onion policy and mutually exclusive CLI flags; retain existing config and
  launcher behavior. Onion requires an exact canonical host and Unix socket.
- Owner schema, session audience migration, interactive stopped-service setup/reset/revoke CLI.
  Freeze CLI/API/schema contracts before T2/T3 integration. Record the chosen migration number.
- Password login with bounded body/hash work, persistent global throttle, generic errors, atomic
  session limits and credential-revision checks. Add and lock the Argon2 dependency.
- Onion access never bootstraps a viewer, never accepts a LAN session, and never enables pairing.
  Preserve ownership enforcement on all sensitive routes and cookie/Origin policy by mode.
- Linux advisory lock held across process lifetime and maintenance. Preserve Windows behavior.

Acceptance:

- Anonymous onion root shows only login; `/api/auth` creates no viewer/session. Unauthorized
  readings/voices/jobs/manifests/progress/audio GET/HEAD/Range fail, including through requests
  with missing/loopback peer data or spoofed forwarded headers.
- Missing owner/hostname, malformed onion address, conflicting flags, foreign/null/missing Origin,
  invalid Host, oversized/chunked request bodies, wrong media type, and wrong password fail closed.
- Login/re-login returns the same owner library; token hashes only are persisted; device revocation,
  expiry, password reset, audience separation, and session-cap recovery work across restart.
- Login flooding is bounded before expensive hashing; API/player stay responsive; restart does
  not clear throttle state. Passwords/tokens are absent from captured logs and error responses.
- Existing desktop/LAN tests pass. Two Linux processes/containers cannot own the same volume;
  forced kill/restart recovers without deleting an active lock or depending on namespace PIDs.

## T2: packaging and standalone Compose

Primary files to add: `Dockerfile`, `.dockerignore`, `compose.yaml`, and a focused `deploy/tor/`
directory for Tor image/config/entrypoint/health tools. Update the package manifest only if needed
to include deliberately distributed deployment assets; never include runtime secrets/models.

Deliverables:

- Locked CPython 3.12 CPU wheel installation with all web assets and native speech dependencies.
  Reproducible source revision/base digest records and explicit rebuild/update procedure.
- Standalone project name and separate egress-capable bridges; no chat edits/shared networks,
  published ports, host networking, Docker socket mount, or source-code bind mount.
- Separate data/model/Tor-state/hostname/socket volumes with fixed service IDs, tested permissions,
  non-root runtime, limits and bounded logs. Model volume writable only for explicit provisioning.
- Tor starts independently, creates persistent v3 keys, exports only hostname atomically, and
  forwards to the socket. Reader never sees private keys. Bounded startup waiting/failure and
  deliberate hostname-change handling; no circular readiness dependency.
- One-shot setup tools, explicit three-voice installation/checksum verification, owner initialization,
  public-address display, and read-only-model-compatible startup/diagnostics.

Acceptance:

- Clean image build and a fresh-volume installation work using documented commands. Missing/corrupt
  models fail readiness without downloads during requests. Real synthesis works inside the image
  for English, German, Serbian Latin and Cyrillic; record architecture/RAM/time.
- No host web listener appears. Tor can connect to the socket as its non-root user; an unrelated
  container without the mount cannot. Exact Host checks still apply to direct socket requests.
- Reader and Tor can start/restart in either operational order without requiring the other to be
  healthy first. Missing hostname or wrong permissions produces actionable errors.
- Container replacement preserves onion hostname, library, sessions and model checksums.
  Tor keys are absent from Reader mounts, build context, image layers and public metadata.
- Lock/restart tests use actual Linux containers with a shared test volume, not only mocked PIDs.
- Record actual versions/architecture; unsupported architectures remain explicitly unverified.

## T3: bounded storage for an always-on service

Primary files: cache settings, persistence ports/repositories/migrations, `worker/loop.py`,
`storage/durable_audio.py`, reading service/API, and playback JS for lease renewal.
Coordinate the next migration number with T1; do not concurrently edit the same migration block.

Deliverables:

- Enforce the existing audio budget/free-space floor and history limit. Count staging, orphans,
  and active generation reservations. Bound retained terminal/retry job metadata.
- Whole-rendition LRU with playback leases and active-request protection; no partial rendition
  eviction, in-flight job deletion, or removal of leased playback.
- Worker-owned physical cleanup after soft deletion and cancellation completion. Recovery of
  expired staging/orphan files must prove they are unreferenced before removal.
- Stable `STORAGE_FULL`/eviction outcomes with useful UI retry/regenerate behavior. No full-disk
  crash loop; text/history/progress survive an audio-only eviction.

Acceptance:

- Small deterministic budgets exercise boundaries, cancellation, crashed publication, low disk,
  reservation races, orphan accounting, lease expiry/renewal and active Range response races.
- Deleting a reading eventually reclaims its audio after work stops. A reader under playback is
  not evicted during the measured Tor reconnect grace. Already-evicted audio is reported honestly.
- Restart resumes cleanup safely; DB rows do not advertise missing chunks as playable. Automatic
  eviction leaves enough capacity for a new reading or fails with the documented storage outcome.

This completes only the storage/lease portion of M5; it does not complete portable bundles.

## T4: UI, health, backup and integration

Primary files: `web/templates/index.html`, `web/static/app.js`, `web/static/app.css`, API capability
metadata, deployment health/maintenance tools, and a new `docs/TOR_SETUP.md` runbook.

Deliverables:

- Password login, disconnect, session management and re-login to the same library; clear server
  storage wording. Retain LAN pairing UI only in LAN mode. No claim that cookies survive Tor reset.
- Authenticated progressive playback, capped reconnect backoff and revisioned progress, with
  graceful failure of optional browser APIs. Keep existing audio format unless evidence warrants
  a separate decision.
- Liveness/readiness and Tor bootstrap checks, worker busy-versus-stuck handling, signal cleanup,
  measured shutdown grace, CPU/memory/log limits and redacted operational errors.
- Tested stopped-service backup/restore, migration backup and rollback, owner recovery, hostname
  rebind, model repair, and volume lifecycle instructions. Keep backups outside live volumes/Git.
- An automated offline integration path with fake speech plus a separate opt-in live Tor/real-model
  smoke procedure. Live smoke tests use disposable data and never another project's credentials.

Acceptance:

- Existing desktop/LAN suites, strict typing, lint/format, and JS syntax checks pass. New behavior
  has meaningful negative and recovery tests, including cross-viewer access regression cases.
- SIGTERM and forced kill during synthesis preserve recoverable jobs; restart does not duplicate
  publication. Restart of Tor does not lose Reader history or generate another onion identity.
- Full restore into isolated volumes recovers consistent DB/audio and owner access. Identity
  restoration never publishes a clone alongside the original. Older code refuses newer schemas;
  rollback uses the matching backup and records any readings lost since it.
- Local health creates no session and discloses no article data. Tor health is not reported as
  proof of external reachability. No unauthenticated control port is exposed.

## T5: owner phone and shared-host release

Record evidence in a short dated report linked below and in `STATUS.md`. A desktop browser smoke
is not phone evidence. Request only the missing hands-on checks if the physical phone is unavailable.

- Actual phone OS/browser/version and security setting; onion login and Secure cookie round-trip;
  new browser session/password re-login restores history. No certificate-warning bypass.
- One real news article per language, both Serbian scripts across the samples, successful extraction
  review/selection, real speech, first-chunk latency, sustained playback and section transitions.
- Pause/resume/seek/speed, network change/disconnect/reconnect, expiry/revocation, second-device
  progress conflicts, and app/Tor restart. Background/screen-off behavior is recorded, not promised.
- Measure bitrate, buffering gaps, generation speed, peak memory/CPU and disk growth with chat
  already running. Verify chat remains reachable and usable; resource limits reflect those results.
- Complete fresh setup and restore from the runbook. Confirm stable onion address after replacement,
  no host HTTP port, no chat configuration changes, and usable recovery from full storage.

Only then mark the onion deployment supported. If the owner wants an earlier trial, label it as
a trial and list incomplete gates rather than marking T5 done.

## Collaboration and continuation protocol

Before implementation, read `AGENTS.md`, `HANDOFF.md`, this plan and the architecture. Inspect Git
status and current branch. Claim one task row with agent/session and branch/worktree; preserve
existing work. Agree interface/schema ownership before concurrent edits. Separate worktrees are
preferred for simultaneous agents. This plan does not itself launch or require sub-agents.

T2 packaging work can proceed after T1's mode/CLI contracts are fixed. T3 storage work can proceed
alongside packaging after schema ownership is agreed. API wiring, migrations, lockfile changes,
and the final integration need one integrator. Do not generate conflicting migration numbers or
hand-edit `uv.lock`; regenerate it through the selected package tool and review the diff.

At the end of each implementation slice update its row and add a checkpoint below with: date,
agent/branch/commit (or uncommitted files), changes, exact verification results, unresolved issues,
and the next concrete action. Update `HANDOFF.md` and `STATUS.md` only to reflect actual state.
Do not erase the original Windows M4 evidence or present it as Linux/Tor verification.

### Initial checkpoint — 2026-10-19

- Completed: source-based architecture review and this documentation package.
- Observations: current session bootstrap depends on `lan_mode`; no owner password/onion mode;
  PID-based lock needs container handling; storage limit settings are not enforced; deletion is
  soft only; image packaging and real phone/Tor playback remain unverified.
- Runtime work: none. No image built, model downloaded, credential created, service started,
  container restarted, or running chat configuration changed for this planning task.
- Preferences: initially drafted as defaults; subsequently confirmed by the owner below.
- Next action: claim T1, freeze the mode/auth/schema contracts, and implement its regression tests
  alongside the minimum code changes. T2 must not expose the unmodified desktop application.
- Verification for this checkpoint: `git diff --check` passed; a local link check resolved all
  23 relative Markdown links across the 10 affected documents. Application tests were not rerun
  for this documentation-only change. Add actual command results when implementation starts.

### Preference confirmation — 2026-10-19

- Owner confirmed normal outbound article fetching from the Reader container; the phone uses Tor
  to reach Reader. No Tor-fetch adapter is required for this release.
- Owner selected password login with remembered sessions. Session lifetime follows configuration;
  browser cookie clearing requires password re-login to the existing owner library.
- Updated architecture, decision record, status and handoff. Implementation remains not started;
  the next task is still T1. This confirmation does not change the planning-only task scope.
- Verification: documentation diff/whitespace checks; no application code or runtime changes.

### Collaboration setup — 2026-10-19

- Added `CLAUDE.md`, a scoped CLI launcher/guard, local session persistence and
  [AI_COLLABORATION.md](AI_COLLABORATION.md) with the planned Codex/Claude ownership split.
- Claude created `README_COMBO.md` containing exactly `HELLO WORLD!` plus a newline. A second
  successful turn resumed the same UUID and recalled the setup without tool calls/document reads.
- Ten harness tests, Ruff checks and the whitespace diff check pass. The original permission
  configuration failure was corrected before the successful smoke test.
- Session state is in Git-ignored `.claude/session.local.json`; use the launcher to resume it.
- T1–T5 remain not started. The owner authorized setup; implementation needs its next assignment.

### Completion evidence

Boot follow-up (2026-10-19): Codex owns `deploy/start-on-boot.sh`, the user crontab Reader entry,
and startup documentation. Owner requested automatic Linux startup after confirming Reader works.
Existing chat/update/network cron entries are preserved. Verification is recorded below.

See [TOR_ACCEPTANCE.md](TOR_ACCEPTANCE.md). Do not paste passwords, session tokens, pairing codes,
private keys, or private article content into this file.

### Implementation start — 2026-10-19

- Owner authorized autonomous implementation. Codex owns T1 auth/modes/schema/CLI and integration; Claude resumes saved session for T1 lock and T2 packaging. A storage helper owns T3 implementation with separate files.
- Schema 3 reserved for owner auth; schema 4 reserved for storage. No chat changes.
- Claude Opus 5.5 rejected by installed CLI version (requires 2.1.280); resumed with requested Sonnet alternative.
- Previous automatic approval usage-limit block cleared after owner continuation; no implementation task was executed by that rejected launch.

### Implementation and host trial — 2026-10-19

- T1–T4 implemented, images built, models explicitly verified, owner initialized with a generated
  private password file, standalone Reader/Tor services healthy. No chat edits/restarts.
- Schema 3 owner credential/audience/throttle; schema 4 persistent cache timestamps/playback leases.
- 453 pytest passes, two Windows-only skips, five subtests; strict mypy, Ruff, GJS UI and ten harness
  checks pass. Real Tor article/speech/Range/HEAD, all voice scripts, cross-container lock/kill,
  image/service restart persistence and stopped backup/isolated restore passed.
- One proposed live revoke-all lock test was rejected by approval review; it was not executed.
  Harmless live acquire/release and disposable cross-container contention verified the boundary instead.
- Source is uncommitted. Private runtime backup and unpublished isolated restore volumes retained.
  See runbook/report for commands, measurements, password-file location and remaining phone gate.
- Next: physical-phone Tor browser cookie/listening/network/background checks; no further code task
  is blocked on routine permissions. Portable bundles are still outside this release.

### Automatic Linux startup — 2026-10-19

- Owner confirmed Reader works and requested boot startup using the existing crontab arrangement.
- Verified `docker.service` and `cron.service` are enabled and active, with both Reader containers
  already using `unless-stopped`. Inspected the existing chat update script without changing it.
- Added `deploy/start-on-boot.sh`: bounded Docker wait, private rotating boot log, invocation lock,
  explicit Reader project/services, no builds/pulls/recreation, and bounded health waiting.
- Installed one user `@reboot` entry; compared the complete installed crontab with the prepared
  addition to prove every previous job was preserved. Private original backup:
  `runtime/cron-before-reader-boot.txt`.
- `/bin/sh` syntax and a real run with `env -i PATH=/usr/bin:/bin` passed. Reader/Tor became healthy
  in the startup check; their container IDs/start times stayed identical. `git diff --check` passed.
  No host reboot was performed. Logs: `runtime/boot.log`; runbook documents disable/move behavior.
