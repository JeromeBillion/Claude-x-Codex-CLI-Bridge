"""Commit relay tests with no provider calls or subscription usage."""

from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.agent_relay import Request, claim, common_state, complete, enqueue, enqueue_head, lane_from_branch, watch


SHA = "a" * 40


class RelayTests(unittest.TestCase):
    def test_post_commit_hook_keeps_unix_line_endings_for_git_bash(self):
        hook = Path(__file__).resolve().parents[2] / ".githooks" / "post-commit"
        data = hook.read_bytes()
        self.assertTrue(data.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r\n", data)

    def request(self, commit: str = SHA) -> Request:
        return Request(commit, "codex", "claude", "codex/one", "2026-09-29T00:00:00Z")

    def test_owned_branch_and_invalid_commit(self):
        self.assertEqual(lane_from_branch("claude/fix"), "claude")
        self.assertIsNone(lane_from_branch("main"))
        with self.assertRaises(ValueError):
            self.request("bad; shell command")
        with self.assertRaises(ValueError):
            Request(SHA, "codex", "claude", "claude/wrong", "now")

    def test_queue_is_deduplicated_and_report_remains_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            request = self.request()
            self.assertTrue(enqueue(state, request))
            self.assertFalse(enqueue(state, request))
            processing, claimed = claim(state, "claude")
            self.assertEqual(claimed, request)
            self.assertIsNone(claim(state, "claude"))
            complete(state, processing, request, "find a concrete bug", ok=True)
            self.assertFalse(enqueue(state, request))
            report = json.loads((state / "reports" / f"{request.key}.json").read_text(encoding="utf-8"))
            self.assertEqual(report["report"], "find a concrete bug")

    def test_newer_commit_supersedes_unreviewed_commit_on_same_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            old, new = self.request("a" * 40), self.request("b" * 40)
            self.assertTrue(enqueue(state, old))
            self.assertTrue(enqueue(state, new))
            self.assertTrue((state / "superseded" / f"{old.key}.json").exists())
            self.assertEqual(claim(state, "claude")[1], new)

    def test_capped_watcher_runs_one_fake_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace, state = Path(tmp), Path(tmp) / "state"
            enqueue(state, self.request())
            calls = []
            def fake_review(ws, location, request, prompt):
                calls.append((request.commit, prompt))
                return "reviewed"
            with patch("tools.agent_relay.common_state", return_value=state), \
                 patch("tools.agent_relay.review_prompt", return_value="untrusted diff"):
                count = watch(workspace, "claude", max_turns=1, poll_seconds=0.2,
                              run_models=True, reviewer=fake_review)
            self.assertEqual(count, 1)
            self.assertEqual(calls, [(SHA, "untrusted diff")])
            self.assertTrue((state / "done" / f"{SHA}-claude.json").exists())

    def test_git_hook_queues_code_commit_but_not_comms(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            def run(*args):
                return subprocess.run(["git", *args], cwd=workspace, capture_output=True, check=True)
            run("init", "-q")
            run("config", "user.email", "test@example.invalid")
            run("config", "user.name", "Test")
            run("switch", "-q", "-c", "codex/feature")
            (workspace / "file.py").write_text("x = 1\n", encoding="utf-8")
            run("add", "file.py")
            run("commit", "-qm", "feat: test")
            self.assertEqual(enqueue_head(workspace), "queued")
            self.assertEqual(enqueue_head(workspace), "already queued")
            state = common_state(workspace)
            self.assertEqual(len(list((state / "pending").glob("*.json"))), 1)
            (workspace / "comms").mkdir()
            (workspace / "comms" / "note.md").write_text("hi", encoding="utf-8")
            run("add", "comms")
            run("commit", "-qm", "comms: update")
            self.assertEqual(enqueue_head(workspace), "ignored: communications-only commit")


if __name__ == "__main__":
    unittest.main()
