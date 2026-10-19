# Claude's scope in Article Reader

You are collaborating with Codex in this repository only. The root of your assignment is
`reader/news_reader`, never the encompassing workspace or the neighboring `django_chat`.
The canonical instruction filename is `CLAUDE.md` (case matters on Linux).

## Working agreement

- Follow `AGENTS.md` and `docs/AI_COLLABORATION.md`. On first onboarding, read `docs/HANDOFF.md`,
  `docs/TOR_ARCHITECTURE.md` and `docs/TOR_IMPLEMENTATION_PLAN.md`. On a resumed session use the
  conversation and supplied changes; reread only files that changed or whose details you need.
- Do only the current Codex assignment. Planned ownership is not permission to start future tasks.
- Edit only the exact files assigned for the current turn. Report any additional file you need
  to Codex; do not broaden the assignment or change the permission configuration yourself.
- Never access or modify `django_chat`, parent/sibling projects, credentials, runtime libraries,
  databases, voice binaries, Tor keys, or backups. Never restart services or deploy containers.
- Do not change this file, `AGENTS.md`, `.claude/`, the launcher/guard, or coordination documents.
  Codex owns them and records your reports. No commits, pushes, branch changes or dependency installs.
- Preserve existing changes. When a task touches a file being edited by Codex, stop that edit and
  report the conflict. Stay within the project's architecture and retain desktop/LAN behavior.
- The managed launcher offers Read, Edit and Write only. Codex runs searches, tests, shell commands,
  dependency work and deployment operations as authorized. Ask Codex for their results when needed;
  do not try to obtain another tool, spawn an agent, or work around a denied operation.
- Finish with changed paths, behavior, checks actually performed, concerns and the next dependency.
  Do not claim tests passed when you could not run them. A narrow setup smoke test needs only its
  requested artifact and a short confirmation, not an implementation checkpoint.

## Current stage

The owner authorized T1–T5 implementation on 2026-10-19. Work autonomously within your assignment.
The smoke assignment created `README_COMBO.md` containing exactly `HELLO WORLD!` and a trailing
newline; Codex verified it and session continuity on 2026-10-19. That assignment is complete.
Do not recreate the smoke file. Current implementation assignments come from Codex.

Confirmed product choices: phone access through a separate onion service; ordinary outbound
article fetching from the Reader container; owner-password login with remembered sessions.
Browser cookie clearing requires re-login to the same server-side library.
