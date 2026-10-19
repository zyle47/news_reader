# Codex and Claude collaboration

Updated: 2026-10-19. Current authorization: autonomous T1–T5 implementation, granted by the owner on 2026-10-19.
Codex coordinates schema/integration; the persistent Claude conversation owns lock/packaging;
a separately scoped storage helper implements T3 without editing shared API/CLI/migration files.

## Responsibilities

Codex is the integrator and assigns bounded tasks to one persistent Claude CLI conversation.
The intended split is:

| Area | Owner | Integration rule |
| --- | --- | --- |
| Serving modes, owner authentication, API and CLI composition | Codex | Freeze contracts before dependent work |
| Schema migrations, persistence interfaces, dependency lockfile | Codex | One writer/integrator for shared contracts |
| Linux instance-lock implementation and its focused tests | Claude | Completed and integrated; Codex added unowned-release regression |
| Docker/Tor images, entrypoints and deployment scaffolding | Codex | Claude broad turn stalled; Codex completed and host-tested packaging |
| Storage policy, worker integration and cleanup | Codex | Claude can review or take separately assigned helpers/tests |
| Onion login/device UI and focused UI changes | Scoped UI helper | Completed against Codex API contracts |
| Full verification, integration, task-board/status updates | Codex | Review Claude's diff and execute relevant checks before accepting |
| Physical-phone acceptance | Owner with Codex guidance | Record actual device evidence |

These are planned roles, not instructions to start every task. Codex supplies an exact write-file
list for each turn. Neither agent edits the other's assigned files concurrently. Shared-file work
is handed off sequentially; a later isolated worktree needs its own explicit path/session mapping.
Claude reports findings and test commands; Codex owns shell execution and records actual results.

## Session continuity

Run Claude from this checkout through `tools/claude_session.py`. The launcher saves the UUID and
started state in `.claude/session.local.json` and uses that exact UUID with `--resume` on later
turns. It does not use `--continue`, fork the conversation, or disable session persistence.
The transcript remains in Claude's normal per-user storage, outside this repository. Local
state/result/assignment files are Git-ignored; a UUID is a conversation identifier, not an auth token.

Inspect the saved session:

```sh
python3 tools/claude_session.py --status
```

Provide a task on stdin, explicitly assigning every writable file:

```sh
python3 tools/claude_session.py --write-file README_COMBO.md <<'TASK'
Create README_COMBO.md with exactly HELLO WORLD! followed by one newline. Then stop.
TASK
```

Omit `--write-file` for a read-only/review turn. Repeat it for multiple exact paths; there are no
wildcards or default "all project files" write grants. The saved turn result is
`.claude/last-result.local.json`. The assignment in `.claude/task.local.json` is replaced per turn.
A local lock prevents overlapping launcher invocations against one conversation.

On initial onboarding Claude reads its instructions, handoff and architecture/implementation plan
once. Subsequent tasks pass only changed contracts, relevant files and new test results. Session
resume reduces repeated onboarding but does not guarantee unlimited context retention. After
compaction or missing context, use the checkpoint and reread only what is needed. Never silently
create a fresh session when resume/auth fails. If replacement is necessary, Codex records why and
preserves the old UUID before creating another one. Login refresh is a separate CLI operation.
This uses Claude's documented [session resume support](https://code.claude.com/docs/en/headless).

## Tool controls

`CLAUDE.md` is the normal, case-sensitive Claude instruction file. It provides behavioral scope;
the launcher and `tools/claude_guard.py` provide additional file-tool controls:

- Only Read, Edit and Write tools are supplied. Shell, Docker, network/browser tools, skills,
  MCP servers and nested agents are not part of this launcher.
- Project settings only; no inherited user/project-local hook or plugin configuration. Managed
  organization policies still apply. No `--safe-mode` because that would disable project hooks.
- The launcher passes project-read and exact-file edit allowances; `dontAsk` rejects operations
  needing additional approval. The PreToolUse hook also checks matching-session operations in
  this repository, with exact-file write ownership. Edit rules cover the Write tool as well.
- Out-of-repository, parent traversal, symlink, hardlink, private/runtime and special-file paths
  are rejected. Claude cannot edit its controls or Codex-owned coordination documents even if
  someone accidentally includes them in the write list.
- Hook input/configuration errors exit 2. The shell command maps script failures to exit 2;
  base write permissions remain limited to assigned files. No bypass-permissions flag is used.

Claude's [hook interface](https://code.claude.com/docs/en/hooks) supports these approval decisions.
This is a tool-level boundary, not an OS sandbox: the CLI process still uses the host account for
authentication and its own transcript storage. Codex must review generated code before executing
it. The controls do not confine a different manual invocation of Claude or malicious host processes.

## Historical verification and setup checkpoint

Run offline boundary checks with:

```sh
python3 tools/test_claude_harness.py
```

The setup acceptance checks are: guard rejection cases pass; Claude itself writes exactly the
requested file; the returned UUID matches saved state; a resumed turn recalls the setup without
rereading documents; no application/chat changes are introduced. Record actual results here after
the smoke run. The hello file is not an implementation milestone.

Status: setup verified on 2026-10-19.

- Ten offline boundary/session-command tests pass; Ruff lint and formatting checks pass for
  `tools/`; `git diff --check` passes.
- The first live turn was denied by over-restrictive ask rules. Codex corrected the configuration
  to use project-read and exact-file edit allowances, retaining the scope hook and `dontAsk`.
- An interrupted approval/IDE closure did not lose the session. The launcher lock was free and
  no hello file had been created; Codex resumed the existing saved UUID.
- Claude then created `README_COMBO.md`; Codex verified exactly `b"HELLO WORLD!\n"` (13 bytes).
  The successful turn reported no permission denials.
- A further turn used the same saved UUID and recalled the continuity marker, file contents,
  repository boundary and workload split. Transcript inspection confirmed zero tool calls in
  that follow-up: no document rereads were needed.
- At this setup checkpoint no application/chat changes were made; T1–T5 had not started.
  The implementation checkpoint below supersedes this status.

Resume the locally saved session for the next authorized task; no new Claude conversation is
needed. Never overwrite the hello file just to prove another resume worked.

## Implementation checkpoint — 2026-10-19

Owner authorized implementation; T1–T4 are now implemented/host-tested. The saved Claude session
was resumed, first requesting Opus 5.5 (installed CLI needs an update for it), then Sonnet. Its broad
packaging turn stalled without changes and was stopped; Codex took packaging. The same session
completed Linux flock and its actual subprocess contention/kill/inode tests. Codex reviewed and
ran them, then integrated auth/storage/UI with separately scoped helpers. No new Claude UUID.

Actual host/network/container/restore evidence is in TOR_ACCEPTANCE.md. T5 phone gate stays open.
Use `--model sonnet` or an explicitly supported model via the launcher for the next assignment.
