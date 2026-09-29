"""Agent mailbox and watchdog: shared across worktrees, one line per event, nothing pushed."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import agent_mail


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class AgentMailTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        env = patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                                      "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"})
        env.start()
        self.addCleanup(env.stop)
        self.origin, self.repo = base / "origin.git", base / "bridge"
        run(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        run(base, "clone", "-q", str(self.origin), str(self.repo))
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        run(self.repo, "add", "-A")
        run(self.repo, "commit", "-q", "-m", "base")
        run(self.repo, "push", "-q", "origin", "HEAD:main")
        self.worktree = self.repo / ".worktrees" / "codex-x"
        run(self.repo, "worktree", "add", "-q", "-b", "codex/x", str(self.worktree))

    def test_mail_sent_from_one_worktree_arrives_in_the_other(self) -> None:
        agent_mail.send(agent_mail.mail_root(self.worktree), "codex", "claude", "PR #20 reviewed", "5 findings", "PR #20")
        root = agent_mail.mail_root(self.repo)
        self.assertEqual(root, agent_mail.mail_root(self.worktree))  # one shared mailbox
        self.assertTrue(str(root).endswith(os.path.join(".git", "agent-mail")))  # never tracked or pushed
        messages = agent_mail.unread(root, "claude")
        self.assertEqual([m["subject"] for m in messages], ["PR #20 reviewed"])
        self.assertEqual(agent_mail.unread(root, "codex"), [])
        self.assertEqual(agent_mail.ack(root, "claude", None), 1)
        self.assertEqual(agent_mail.unread(root, "claude"), [])
        self.assertNotIn("agent-mail", run(self.repo, "status", "--porcelain", "--untracked-files=all"))

    def test_bad_mail_is_refused(self) -> None:
        root = agent_mail.mail_root(self.repo)
        for sender, to, subject in (("claude", "claude", "s"), ("jerome", "codex", "s"), ("claude", "codex", " ")):
            with self.subTest(sender=sender, to=to), self.assertRaises(ValueError):
                agent_mail.send(root, sender, to, subject, "b")

    def test_watch_announces_each_message_once_as_one_line(self) -> None:
        root = agent_mail.mail_root(self.repo)
        agent_mail.send(root, "codex", "claude", "first\nline", "body")
        seen: list[str] = []
        agent_mail.watch(root, self.repo, "claude", interval=0.01, remote=False, rounds=3, out=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("MAIL ") and "from codex: first line" in seen[0])
        self.assertNotIn("\n", seen[0])

    def test_remote_watch_reports_the_other_lanes_pushes_and_new_pr_comments(self) -> None:
        comments = {"now": []}
        fake_gh = lambda args: json.dumps([{"number": 20, "comments": comments["now"]}])
        comments["now"] = [{"id": "old", "body": "old review"}]
        remote = agent_mail.RemoteWatch(self.repo, "claude", gh=fake_gh)
        other = self.repo.parent / "other"
        run(self.repo.parent, "clone", "-q", str(self.origin), str(other))
        (other / "comms").mkdir()
        (other / "comms" / "codex").mkdir()
        (other / "comms" / "codex" / "COMMS.md").write_text("reply", encoding="utf-8")
        run(other, "add", "-A")
        run(other, "commit", "-q", "-m", "comms: codex reply 6 [skip ci]")
        (other / "b.txt").write_text("b", encoding="utf-8")
        run(other, "add", "-A")
        run(other, "commit", "-q", "-m", "claude: unrelated change")
        run(other, "push", "-q", "origin", "HEAD:main")
        comments["now"] = [{"id": "old", "body": "old review"}, {"id": "new", "body": "## Codex review\nmore"}]
        events = remote.poll()
        self.assertEqual(len(events), 2, events)
        self.assertTrue(events[0].startswith("MAIN ") and "comms: codex reply 6" in events[0])
        self.assertEqual(events[1], "PR #20 comment: ## Codex review")
        self.assertEqual(remote.poll(), [])  # nothing new, nothing repeated


if __name__ == "__main__":
    unittest.main()
