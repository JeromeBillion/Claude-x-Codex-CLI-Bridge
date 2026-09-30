"""Commit relay tests with no provider calls or subscription usage."""

from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.agent_relay import (Request, _review_codex, claim, common_state, complete, enqueue,
                               main,
                               enqueue_head, hook_copy_current, hook_effective, install_hook, lane_from_branch, watch)


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

    def test_claim_uses_created_at_order_instead_of_sha_order(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            state = Path(tmp)
            late = Request("a" * 40, "codex", "claude", "codex/late", "2026-09-29T02:00:00Z")
            early = Request("f" * 40, "codex", "claude", "codex/early", "2026-09-29T01:00:00Z")
            enqueue(state, late)
            enqueue(state, early)
            self.assertEqual(claim(state, "claude")[1], early)

    @unittest.skipUnless(os.name == "nt", "Windows file-sharing semantics")
    def test_open_pending_file_does_not_crash_claim_or_queue_extra_review(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            state = Path(tmp)
            old, new = self.request("a" * 40), self.request("b" * 40)
            self.assertTrue(enqueue(state, old))
            pending = state / "pending" / f"{old.key}.json"
            with pending.open("rb") as held:
                self.assertFalse(held.closed)
                self.assertIsNone(claim(state, "claude"))
                self.assertFalse(enqueue(state, new))
                self.assertFalse((state / "pending" / f"{new.key}.json").exists())
            self.assertTrue(enqueue(state, new))
            self.assertEqual(claim(state, "claude")[1], new)

    def test_common_hook_queues_commit_from_pre_relay_worktree(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            root = Path(tmp)
            repo, old = root / "repo", root / "old-worktree"
            repo.mkdir()
            def run(where, *args):
                return subprocess.run(["git", *args], cwd=where, capture_output=True, check=True)
            run(repo, "init", "-q")
            run(repo, "config", "user.email", "test@example.invalid")
            run(repo, "config", "user.name", "Test")
            (repo / "initial.txt").write_text("initial\n", encoding="utf-8")
            run(repo, "add", "initial.txt")
            run(repo, "commit", "-qm", "initial")
            install_hook(repo)
            self.assertTrue(hook_copy_current(repo))
            run(repo, "worktree", "add", "-qb", "claude/old", str(old), "HEAD")
            self.assertTrue(hook_effective(old))
            self.assertFalse((old / "tools" / "agent_relay.py").exists())
            (old / "change.py").write_text("x = 1\n", encoding="utf-8")
            run(old, "add", "change.py")
            run(old, "commit", "-qm", "feat: pre-relay branch")
            pending = list((common_state(repo) / "pending").glob("*-codex.json"))
            self.assertEqual(len(pending), 1)
            self.assertFalse((common_state(repo) / "hook-errors.log").exists())
            installed = common_state(repo).parent / "hooks" / "agent-relay.py"
            installed.write_bytes(installed.read_bytes() + b"\n# stale copy\n")
            self.assertFalse(hook_copy_current(repo))
            output = StringIO()
            with patch("tools.agent_relay.Path.cwd", return_value=repo), \
                 patch.object(sys, "argv", ["agent_relay", "status", "--agent", "codex"]), \
                 redirect_stdout(output):
                self.assertEqual(main(), 0)
            self.assertIn("re-run install-hook", output.getvalue())
            self.assertIn("Codex unattended reviews disabled", output.getvalue())
            run(repo, "worktree", "remove", "-f", str(old))

    def test_codex_unattended_review_fails_before_transport_spawn(self):
        with self.assertRaisesRegex(RuntimeError, "disabled until tools can be isolated"):
            _review_codex(Path.cwd(), Path.cwd(), self.request(), "untrusted diff")

    def test_missing_report_and_retry_are_fixed_messages(self):
        for command in ("show-report", "retry"):
            result = subprocess.run([sys.executable, "-m", "tools.agent_relay", command,
                                     "f" * 40, "--agent", "claude"], cwd=Path.cwd(),
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)
            self.assertNotIn(str(Path.cwd()), result.stderr)

    def test_done_review_can_be_explicitly_requested_again(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as tmp:
            state = Path(tmp)
            request = self.request()
            enqueue(state, request)
            processing, _ = claim(state, "claude")
            complete(state, processing, request, "old review", ok=True)
            with patch("tools.agent_relay.common_state", return_value=state), \
                 patch.object(sys, "argv", ["agent_relay", "retry", SHA, "--agent", "claude", "--from-done"]), \
                 redirect_stdout(StringIO()):
                self.assertEqual(main(), 0)
            self.assertTrue((state / "pending" / f"{request.key}.json").exists())
            self.assertFalse((state / "done" / f"{request.key}.json").exists())

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
