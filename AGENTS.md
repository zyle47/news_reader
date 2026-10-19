# Working on Article Reader

Read `docs/HANDOFF.md` first. `docs/STATUS.md` records implementation evidence;
`docs/DECISIONS.md` explains accepted historical decisions. The original
`article-reader-project-plan.md` is a baseline specification, not the current status.

The current priority is the planned standalone Docker/onion deployment. Read
`docs/TOR_ARCHITECTURE.md` and `docs/TOR_IMPLEMENTATION_PLAN.md` before implementing it.
Onion support is not implemented merely because these documents exist. Preserve desktop/LAN
behavior and keep the neighboring `django_chat` project outside Reader changes.

## Implementation rules

- Keep dependencies pointing inward: delivery adapters → application services/ports → domain.
  SQLite, Piper, password hashing, Tor/socket handling and filesystem work stay in adapters.
- Use CPython 3.12 and the committed lockfile. Do not copy a host/Windows venv into a container.
- Preserve safe public-URL fetching, extraction review, Serbian script handling, approved voices,
  authenticated audio, atomic publication and durable job fencing.
- Onion mode must never inherit desktop session bootstrap or weaken LAN exposure rules.
- Keep models, runtime data, credentials, onion private keys and backups out of Git and images.
- Do not edit or restart chat to implement Reader. Deployment scope follows the user's current
  request; this planning package alone is not a request to deploy.

## Working across agent sessions

For the Codex/Claude arrangement, read `docs/AI_COLLABORATION.md`. Use the managed launcher and
saved `.claude/session.local.json` UUID when delegating to Claude; resume that conversation instead
of creating a fresh one for each task. Codex owns permissions, coordination files and integration.
The owner authorized T1–T5 implementation on 2026-10-19, including autonomous collaboration,
verification and standalone Reader deployment preparation. Preserve the chat boundary.

Inspect Git status; preserve other work. Claim a task in `docs/TOR_IMPLEMENTATION_PLAN.md` before
implementation. When multiple agents are active, agree file/schema ownership and use isolated
worktrees where practical. One integrator owns shared migrations and lockfile updates. No agent
may mark another unfinished task complete without evidence.

Update the task row, checkpoint and `docs/HANDOFF.md` after a meaningful slice. Record what changed,
what was actually tested, failures, uncommitted files/commit reference and the next action. Do not
confuse historical Windows results with Linux containers or a physical-phone Tor test.

Run checks appropriate to the changed behavior. Standard gates are pytest, strict mypy, ruff
lint/format and JavaScript syntax checks; deployment also needs the plan's container and phone
checks. Routine tests remain offline with fake speech. Keep real model/network tests separate.
