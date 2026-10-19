"""Constrain Claude's file tools to this checkout and the current exact write list."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_PATH = ROOT / ".claude" / "task.local.json"
TOOLS = frozenset({"Read", "Edit", "Write"})
PROTECTED_WRITES = frozenset(
    {
        "CLAUDE.md",
        "AGENTS.md",
        ".gitignore",
        "docs/AI_COLLABORATION.md",
        "docs/HANDOFF.md",
        "docs/STATUS.md",
        "docs/DECISIONS.md",
        "docs/TOR_ARCHITECTURE.md",
        "docs/TOR_IMPLEMENTATION_PLAN.md",
        "tools/claude_guard.py",
        "tools/claude_session.py",
        "tools/test_claude_harness.py",
    }
)
PRIVATE_PARTS = frozenset(
    {
        ".git",
        ".claude",
        ".codex",
        ".agents",
        ".aws",
        ".ssh",
        ".venv",
        "runtime",
        "data",
        "models",
        "audio",
        "exports",
        "backups",
        "tor-data",
    }
)


def checked_path(root: Path, raw: object, *, writing: bool) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("A nonempty file_path is required.")
    path = Path(raw)
    if ".." in path.parts:
        raise ValueError("Parent traversal is outside the assignment.")
    path = path if path.is_absolute() else root / path
    relative = path.relative_to(root)
    if not relative.parts:
        raise ValueError("A file, not the repository root, is required.")
    if any(part in PRIVATE_PARTS for part in relative.parts):
        raise ValueError("Private/runtime/control directories are outside the assignment.")
    if any(part.startswith(".env") for part in relative.parts) or path.suffix in {
        ".secret",
        ".pem",
        ".key",
        ".sqlite",
        ".sqlite3",
        ".db",
    }:
        raise ValueError("Credentials and database files are outside the assignment.")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlink paths are not permitted.")
        if current.exists():
            info = current.stat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError("Special files are not permitted.")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("Hard-linked files are not permitted.")
    path.resolve().relative_to(root.resolve())
    if path.exists() and not path.is_file():
        raise ValueError("Only regular files are permitted.")
    name = relative.as_posix()
    if writing and any(character in name for character in "*?[]"):
        raise ValueError("Write assignments must be literal paths, without glob characters.")
    if writing and name in PROTECTED_WRITES:
        raise ValueError("Codex owns the harness and coordination files.")
    return name


def authorize(payload: dict, task: dict, root: Path = ROOT) -> str:
    if payload.get("hook_event_name") != "PreToolUse":
        raise ValueError("Unexpected hook event.")
    if Path(payload.get("cwd", "")).resolve() != root.resolve():
        raise ValueError("Claude must remain in the Reader repository.")
    if not task.get("session_id") or payload.get("session_id") != task["session_id"]:
        raise ValueError("No matching managed session assignment.")
    tool = payload.get("tool_name")
    if tool not in TOOLS:
        raise ValueError("Only Read, Edit and Write are assigned; Codex handles other tools.")
    writing = tool in {"Edit", "Write"}
    name = checked_path(root, payload.get("tool_input", {}).get("file_path"), writing=writing)
    if writing and name not in task.get("write_files", []):
        raise ValueError("File is not on the exact write list for this assignment.")
    return name


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        task = json.loads(TASK_PATH.read_text())
        name = authorize(payload, task)
    except Exception as error:
        # Claude 2.1.178 treats exit 2 as blocking; other failures may be nonblocking.
        print(f"Reader scope guard denied this operation: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": f"Assigned Reader file operation: {name}",
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
