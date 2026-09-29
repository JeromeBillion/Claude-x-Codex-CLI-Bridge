"""repo_hygiene deletes only exactly-merged, clean branches and keeps everything else (real temp git repos)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import repo_hygiene


def run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class RepoHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.origin = base / "origin.git"
        self.repo = base / "bridge"
        self.outside = base / "bridge-outside"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        run(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        run(base, "clone", "-q", str(self.origin), str(self.repo))
        self.commit(self.repo, "base")
        run(self.repo, "push", "-q", "origin", "HEAD:main")
        self.prs: list[dict] = []

    def commit(self, where: Path, name: str) -> str:
        (where / f"{name}.txt").write_text(name, encoding="utf-8")
        run(where, "add", "-A")
        run(where, "commit", "-q", "-m", name)
        return run(where, "rev-parse", "HEAD")

    def branch(self, name: str, *, state: str | None, inside: bool = True, extra_after_pr: bool = False) -> Path:
        path = (self.repo / ".worktrees" / name.replace("/", "-")) if inside else self.outside
        run(self.repo, "worktree", "add", "-q", "-b", name, str(path), "origin/main")
        tip = self.commit(path, name.replace("/", "_"))
        run(path, "push", "-q", "origin", f"HEAD:{name}")
        if state:
            self.prs.append({"number": len(self.prs) + 1, "state": state, "headRefName": name, "headRefOid": tip})
        if extra_after_pr:
            self.commit(path, name.replace("/", "_") + "_more")
            run(path, "push", "-q", "origin", f"HEAD:{name}")
        return path

    def hygiene(self, *args: str) -> str:
        prs = self.repo / ".git" / "prs.json"
        prs.write_text(json.dumps(self.prs), encoding="utf-8")
        with patch("tools.repo_hygiene.Path.cwd", return_value=self.repo), \
                patch("sys.stdout.write") as write:
            code = repo_hygiene.main([*args, "--prs-json", str(prs)])
        self.assertEqual(code, 0)
        return "".join(call.args[0] for call in write.call_args_list)

    def remote_branches(self) -> set[str]:
        return set(run(self.repo, "ls-remote", "--heads", "origin").replace("refs/heads/", "").split()[1::2])

    def test_only_exactly_merged_clean_branches_are_removed(self) -> None:
        done = self.branch("claude/done", state="MERGED")
        moved_on = self.branch("claude/moved-on", state="MERGED", extra_after_pr=True)
        self.branch("codex/open", state="OPEN")
        self.branch("codex/no-pr", state=None)
        dirty = self.branch("codex/dirty", state="MERGED")
        (dirty / "uncommitted.txt").write_text("work in progress", encoding="utf-8")

        preview = self.hygiene("clean")
        self.assertIn("would delete local branch claude/done", preview)
        self.assertTrue(done.exists())  # dry run changed nothing
        self.assertIn("claude/done", self.remote_branches())

        self.hygiene("clean", "--apply")
        self.assertFalse(done.exists())
        locals_ = run(self.repo, "branch", "--format=%(refname:short)").split()
        self.assertNotIn("claude/done", locals_)
        self.assertNotIn("claude/done", self.remote_branches())
        for kept in ("claude/moved-on", "codex/open", "codex/no-pr", "codex/dirty"):
            self.assertIn(kept, locals_, kept)
        self.assertTrue(moved_on.exists())
        self.assertTrue((dirty / "uncommitted.txt").exists())
        self.assertIn("codex/dirty", self.remote_branches())  # kept while someone has local work on it

    def test_a_branch_already_contained_in_main_is_removed_but_an_open_pr_never_is(self) -> None:
        run(self.repo, "push", "-q", "origin", "HEAD:claude/old")
        run(self.repo, "push", "-q", "origin", "HEAD:codex/open-at-main")
        self.prs.append({"number": 9, "state": "OPEN", "headRefName": "codex/open-at-main", "headRefOid": "x"})
        self.hygiene("clean", "--apply")
        remotes = self.remote_branches()
        self.assertNotIn("claude/old", remotes)
        self.assertIn("codex/open-at-main", remotes)

    def test_outside_worktrees_are_reported_and_moved_only_on_request(self) -> None:
        self.branch("codex/open", state="OPEN", inside=False)
        self.assertIn("worktree outside the repo", self.hygiene("status"))
        self.hygiene("clean", "--apply", "--move-outside")
        self.assertFalse(self.outside.exists())
        self.assertTrue((self.repo / ".worktrees" / "outside").exists())
        self.assertIn("codex/open", run(self.repo, "branch", "--format=%(refname:short)").split())

    def test_main_is_fast_forwarded_but_never_rewritten(self) -> None:
        other = self.repo.parent / "other"
        run(self.repo.parent, "clone", "-q", str(self.origin), str(other))
        new_tip = self.commit(other, "upstream")
        run(other, "push", "-q", "origin", "HEAD:main")
        self.hygiene("clean", "--apply")
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"), new_tip)
        self.commit(other, "upstream2")
        run(other, "push", "-q", "origin", "HEAD:main")
        local = self.commit(self.repo, "local-only")
        self.assertIn("main has local commits", self.hygiene("clean", "--apply"))
        self.assertEqual(run(self.repo, "rev-parse", "HEAD"), local)

    def test_new_puts_the_worktree_inside_the_repo(self) -> None:
        with patch("tools.repo_hygiene.Path.cwd", return_value=self.repo), patch("sys.stdout.write"):
            self.assertEqual(repo_hygiene.main(["new", "claude/topic"]), 0)
            self.assertEqual(repo_hygiene.main(["new", "Bad Name"]), 2)
        self.assertTrue((self.repo / ".worktrees" / "claude-topic").exists())


if __name__ == "__main__":
    unittest.main()
