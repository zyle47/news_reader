"""Offline tests for actual permission boundaries and session reuse; no Claude API calls."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_guard import authorize
from claude_session import command


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "reader"
        self.root.mkdir()
        self.task = {"session_id": "test-session", "write_files": ["README_COMBO.md"]}

    def request(self, tool="Write", path="README_COMBO.md"):
        return {
            "hook_event_name": "PreToolUse",
            "cwd": str(self.root),
            "session_id": "test-session",
            "tool_name": tool,
            "tool_input": {"file_path": path},
        }

    def test_assigned_write_and_project_read(self):
        self.assertEqual(authorize(self.request(), self.task, self.root), "README_COMBO.md")
        self.assertEqual(
            authorize(self.request("Read", "CLAUDE.md"), self.task, self.root), "CLAUDE.md"
        )

    def test_outside_unassigned_private_and_control_paths_denied(self):
        for path in (
            "../django_chat/README.md",
            "/etc/passwd",
            "src/unassigned.py",
            ".claude/task.local.json",
            "CLAUDE.md",
            "AGENTS.md",
            ".env",
            "runtime/article-reader.sqlite3",
            "tools/claude_guard.py",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                authorize(self.request(path=path), self.task, self.root)

    def test_private_and_outside_reads_denied(self):
        for path in ("../django_chat/.env", "/etc/passwd", ".git/config", ".env"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                authorize(self.request("Read", path), self.task, self.root)

    def test_control_files_cannot_be_authorized_by_write_list(self):
        for path in ("CLAUDE.md", ".claude/settings.json", "tools/claude_session.py"):
            task = {**self.task, "write_files": [path]}
            with self.subTest(path=path), self.assertRaises(ValueError):
                authorize(self.request(path=path), task, self.root)

    def test_symlink_and_hardlink_escape_denied(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("outside")
        target = self.root / "README_COMBO.md"
        target.symlink_to(outside)
        with self.assertRaises(ValueError):
            authorize(self.request(), self.task, self.root)
        target.unlink()
        os.link(outside, target)
        with self.assertRaises(ValueError):
            authorize(self.request(), self.task, self.root)

    def test_tools_session_and_cwd_denied(self):
        for change in (
            {"tool_name": "Bash"},
            {"tool_name": "Agent"},
            {"session_id": "different"},
            {"cwd": str(self.root.parent)},
            {"hook_event_name": "PostToolUse"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                authorize({**self.request(), **change}, self.task, self.root)

    def test_malformed_hook_input_blocks(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("claude_guard.py"))],
            input="not json",
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("denied", result.stderr)

    def test_resume_keeps_exact_session_without_bypass(self):
        state = {"session_id": "existing-id", "started": True}
        args = command(state, ["README_COMBO.md"])
        self.assertEqual(args[args.index("--resume") + 1], "existing-id")
        self.assertNotIn("--session-id", args)
        self.assertNotIn("--no-session-persistence", args)
        self.assertNotIn("--dangerously-skip-permissions", args)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Edit,Write")
        rules = args[args.index("--allowedTools") + 1]
        self.assertIn("/README_COMBO.md)", rules)
        self.assertNotIn("Write(", rules)
        self.assertNotIn("Edit(**)", rules)
        state["started"] = False
        self.assertIn("--session-id", command(state))

    def test_settings_keep_base_permissions_restrictive(self):
        settings = json.loads(
            (Path(__file__).resolve().parents[1] / ".claude/settings.json").read_text()
        )
        self.assertEqual(settings["permissions"]["defaultMode"], "dontAsk")
        self.assertFalse(settings["permissions"].get("allow"))
        self.assertFalse(settings["permissions"].get("ask"))
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], ".*")

    def test_glob_write_assignment_rejected(self):
        task = {**self.task, "write_files": ["*.md"]}
        with self.assertRaises(ValueError):
            authorize(self.request(path="*.md"), task, self.root)


if __name__ == "__main__":
    unittest.main()
