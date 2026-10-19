"""Run one scoped Claude turn, automatically resuming the saved session (Linux host)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from claude_guard import ROOT, authorize, checked_path

CONTROL = ROOT / ".claude"
STATE = CONTROL / "session.local.json"


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def command(state: dict, writes: list[str] | None = None, model: str = "sonnet") -> list[str]:
    allowed = [f"Read(/{ROOT}/**)"]
    allowed.extend(f"Edit(/{ROOT / path})" for path in writes or [])
    return [
        "claude",
        "--print",
        "--model",
        model,
        "--output-format",
        "json",
        "--setting-sources",
        "project",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--no-chrome",
        "--tools",
        "Read,Edit,Write",
        "--allowedTools",
        ",".join(allowed),
        "--permission-mode",
        "dontAsk",
        "--resume" if state["started"] else "--session-id",
        state["session_id"],
    ]


def run(args: argparse.Namespace) -> int:
    if args.status:
        print(STATE.read_text() if STATE.exists() else "No managed Claude session yet.")
        return 0
    prompt = (args.prompt_file.read_text() if args.prompt_file else sys.stdin.read()).strip()
    if not prompt:
        raise ValueError("Supply the assignment on stdin.")
    CONTROL.mkdir(exist_ok=True)
    with (CONTROL / "session.local.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another turn owns the managed Claude session.") from None
        writes = [checked_path(ROOT, path, writing=True) for path in args.write_file]
        # Allow only assigned paths; the hook also checks scope, links and control files.
        settings = json.loads((CONTROL / "settings.json").read_text())
        if settings["permissions"]["defaultMode"] != "dontAsk":
            raise ValueError("The project permission mode has changed; review the harness.")
        if settings["permissions"].get("ask") or settings["permissions"].get("allow"):
            raise ValueError("File approvals must come from the launcher's exact assignment.")
        state = (
            json.loads(STATE.read_text())
            if STATE.exists()
            else {
                "session_id": str(uuid4()),
                "started": False,
                "created_at": datetime.now(UTC).isoformat(),
                "project": str(ROOT),
            }
        )
        if state["project"] != str(ROOT):
            raise ValueError("Saved session belongs to another checkout.")
        task = {"session_id": state["session_id"], "write_files": writes}
        write_json(CONTROL / "task.local.json", task)
        # Check policy import/path handling before asking the CLI to load the hook.
        authorize(
            {
                "hook_event_name": "PreToolUse",
                "cwd": str(ROOT),
                "session_id": state["session_id"],
                "tool_name": "Read",
                "tool_input": {"file_path": str(ROOT / "CLAUDE.md")},
            },
            task,
        )
        argv = command(state, writes, args.model)
        state["started"] = True
        state["last_write_files"] = writes
        state["model"] = args.model
        write_json(STATE, state)
        print(f"Claude session: {state['session_id']}", file=sys.stderr, flush=True)
        print(f"Assigned writes: {writes or '(none)'}", file=sys.stderr, flush=True)
        context = (
            f"Managed Reader assignment. Repository: {ROOT}. "
            f"Only these exact files may be changed this turn: {json.dumps(writes)}. "
            "Follow CLAUDE.md. Keep the existing conversation context. "
            "Do not implement future tasks or change the assignment controls.\n\n"
        )
        process = subprocess.Popen(
            argv,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            output, errors = process.communicate(context + prompt, timeout=args.timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, errors = process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, errors = process.communicate()
            print(
                "Claude turn interrupted; session ID retained. No automatic new session.",
                file=sys.stderr,
            )
            return 124
        if errors:
            print(errors, file=sys.stderr, end="")
        try:
            result = json.loads(output)
        except json.JSONDecodeError:
            print(output, end="")
            return process.returncode or 1
        write_json(CONTROL / "last-result.local.json", result)
        if result.get("session_id") != state["session_id"]:
            print("Unexpected session ID; inspect last-result.local.json.", file=sys.stderr)
            return 1
        print(result.get("result", "No text response."))
        denied = result.get("permission_denials", [])
        if denied:
            print(f"Permission denials: {len(denied)} (see local result file).", file=sys.stderr)
        return process.returncode or (1 if result.get("is_error") or denied else 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--model", default="sonnet", help="Explicit Claude model or alias")
    parser.add_argument("--prompt-file", type=Path, help="Read the assignment from a UTF-8 file")
    parser.add_argument(
        "--write-file",
        action="append",
        default=[],
        help="Exact project-relative file owned by Claude for this turn; repeatable",
    )
    parser.add_argument("--timeout", type=int, default=300, help="Maximum turn duration in seconds")
    args = parser.parse_args()
    if args.timeout < 1 or args.timeout > 3600:
        parser.error("--timeout must be between 1 and 3600")
    try:
        return run(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Claude launcher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
