#!/usr/bin/env python3
"""Repo hygiene for the Claude and Codex lanes (see AGENTS.md, "Workspace rules").

    python -m tools.repo_hygiene new claude/topic       # branch + worktree under .worktrees/
    python -m tools.repo_hygiene status                 # what clean would do (read-only)
    python -m tools.repo_hygiene clean --apply          # after every merge to GitHub

`clean` is a dry run unless `--apply` is given. It only ever deletes a branch
with no OPEN pull request whose tip is either exactly the head of a MERGED
pull request or already contained in origin/main, and only removes a worktree that has no
uncommitted changes. Branches without a PR, open PRs, dirty worktrees and
`main` are reported and left alone. `main` is only fast-forwarded, never
rebased over local commits or force-updated.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

WORKTREE_DIR = ".worktrees"
PROTECTED = frozenset({"main", "HEAD"})
BRANCH_NAME = re.compile(r"(claude|codex|integration)/[a-z0-9][a-z0-9._-]{0,60}")


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def repo_root(start: Path) -> Path:
    """The main checkout, even when called from inside a worktree."""
    common = Path(git(start, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    return common.parent


def pull_requests(repo: Path, prs_json: Path | None) -> list[dict[str, Any]]:
    if prs_json is not None:
        return json.loads(prs_json.read_text(encoding="utf-8"))
    done = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "500", "--json",
                           "number,state,headRefName,headRefOid"], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", check=False)
    if done.returncode != 0:
        raise RuntimeError("gh pr list failed; is gh signed in? Nothing was changed.")
    return json.loads(done.stdout)


@dataclass
class Worktree:
    path: Path
    head: str
    branch: str | None  # short name, or None when detached
    dirty: bool = False


def worktrees(repo: Path) -> list[Worktree]:
    found: list[Worktree] = []
    current: dict[str, str] = {}
    for line in git(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                path = Path(current["worktree"])
                branch = current.get("branch", "").removeprefix("refs/heads/") or None
                dirty = bool(git(path, "status", "--porcelain", check=False).strip()) if path.exists() else False
                found.append(Worktree(path, current.get("HEAD", ""), branch, dirty))
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return found


@dataclass
class Plan:
    delete_local: list[str] = field(default_factory=list)
    delete_remote: list[str] = field(default_factory=list)
    remove_worktrees: list[Path] = field(default_factory=list)
    move_worktrees: list[tuple[Path, Path]] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    fast_forward_main: bool = False


def merged_tip(name: str, tip: str, prs: list[dict[str, Any]]) -> str | None:
    """Why `name` may be deleted, or None. Exact-tip match against a merged PR only."""
    same = [pr for pr in prs if pr.get("headRefName") == name]
    if any(pr.get("state") == "OPEN" for pr in same):
        return None
    for pr in same:
        if pr.get("state") == "MERGED" and pr.get("headRefOid") == tip:
            return f"PR #{pr.get('number')} merged at this exact tip"
    return None


def contained_in_main(repo: Path, tip: str) -> bool:
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tip, "origin/main"],
                          capture_output=True, check=False).returncode == 0


def deletable(repo: Path, name: str, tip: str, prs: list[dict[str, Any]]) -> str | None:
    if any(pr.get("headRefName") == name and pr.get("state") == "OPEN" for pr in prs):
        return None
    return merged_tip(name, tip, prs) or ("tip already on origin/main" if contained_in_main(repo, tip) else None)


def build_plan(repo: Path, prs: list[dict[str, Any]], *, move_outside: bool) -> Plan:
    plan = Plan()
    root = repo.resolve()
    inside = (root / WORKTREE_DIR).resolve()
    trees = worktrees(repo)
    by_branch = {tree.branch: tree for tree in trees if tree.branch}
    locals_ = dict(line.split() for line in git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                                                "refs/heads").splitlines() if line.strip())
    remotes = dict(line.split() for line in git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                                                "refs/remotes/origin").splitlines() if line.strip())
    held: set[str] = set()  # merged, but someone still has local work on it
    for name, tip in sorted(locals_.items()):
        if name in PROTECTED:
            continue
        reason = deletable(repo, name, tip, prs)
        tree = by_branch.get(name)
        if reason is None:
            plan.kept.append(f"{name}: no merged PR at this tip (open PR, unmerged work or no PR) - kept")
        elif tree is not None and tree.dirty:
            held.add(name)
            plan.kept.append(f"{name}: merged, but its worktree has uncommitted changes - kept (remote too)")
        elif tree is not None and tree.path.resolve() == root:
            held.add(name)
            plan.kept.append(f"{name}: checked out in the main checkout - kept (remote too)")
        else:
            plan.delete_local.append(name)
            if tree is not None:
                plan.remove_worktrees.append(tree.path)
    for ref, tip in sorted(remotes.items()):
        name = ref.removeprefix("origin/")
        if name in PROTECTED or ref == "origin":
            continue
        if name not in held and deletable(repo, name, tip, prs) is not None:
            plan.delete_remote.append(name)
    main_tip = remotes.get("origin/main")
    for tree in trees:
        resolved = tree.path.resolve()
        if resolved == root or tree.path in plan.remove_worktrees:
            continue
        if tree.branch is None and not tree.dirty and main_tip and \
                subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", tree.head, main_tip],
                               capture_output=True, check=False).returncode == 0:
            plan.remove_worktrees.append(tree.path)  # detached, clean, and already on main
            continue
        if not resolved.is_relative_to(inside):
            target = inside / resolved.name.removeprefix(root.name + "-")
            if move_outside and not tree.dirty and not target.exists():
                plan.move_worktrees.append((tree.path, target))
            else:
                plan.kept.append(f"worktree outside the repo: {tree.path} "
                                 f"({'dirty' if tree.dirty else 'use --move-outside'})")
    head = git(repo, "rev-parse", "HEAD").strip()
    if git(repo, "branch", "--show-current").strip() == "main" and main_tip and head != main_tip:
        ancestor = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", head, main_tip],
                                  capture_output=True, check=False).returncode == 0
        if ancestor:
            plan.fast_forward_main = True
        else:
            plan.kept.append("main has local commits not on origin/main - not touched")
    return plan


def apply(repo: Path, plan: Plan) -> None:
    for path in plan.remove_worktrees:
        git(repo, "worktree", "remove", str(path))  # refuses a dirty worktree on its own
    for source, target in plan.move_worktrees:
        target.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "move", str(source), str(target))
    for name in plan.delete_local:
        git(repo, "branch", "-D", name)
    for name in plan.delete_remote:
        git(repo, "push", "origin", "--delete", name)
    git(repo, "worktree", "prune")
    git(repo, "fetch", "--prune", "origin", check=False)
    if plan.fast_forward_main:
        git(repo, "merge", "--ff-only", "origin/main")


def report(plan: Plan, applied: bool) -> str:
    verb = "" if applied else "would "
    lines = [f"{verb}remove worktree {p}" for p in plan.remove_worktrees]
    lines += [f"{verb}move worktree {a} -> {b}" for a, b in plan.move_worktrees]
    lines += [f"{verb}delete local branch {n}" for n in plan.delete_local]
    lines += [f"{verb}delete remote branch origin/{n}" for n in plan.delete_remote]
    if plan.fast_forward_main:
        lines.append(f"{verb}fast-forward main to origin/main")
    lines += [f"keep: {k}" for k in plan.kept]
    if not applied and any((plan.remove_worktrees, plan.move_worktrees, plan.delete_local,
                            plan.delete_remote, plan.fast_forward_main)):
        lines.append("dry run: re-run with --apply to do this")
    return "\n".join(lines) or "nothing to clean"


def new_worktree(repo: Path, branch: str) -> Path:
    if not BRANCH_NAME.fullmatch(branch):
        raise RuntimeError("branch must look like claude/<topic> or codex/<topic> (lowercase)")
    git(repo, "fetch", "origin", "main")
    target = repo / WORKTREE_DIR / branch.replace("/", "-")
    git(repo, "worktree", "add", "-b", branch, str(target), "origin/main")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="create a branch and its worktree under .worktrees/")
    new.add_argument("branch")
    for name in ("status", "clean"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--move-outside", action="store_true",
                         help="move clean worktrees that live outside the repo into .worktrees/")
        cmd.add_argument("--prs-json", type=Path, help=argparse.SUPPRESS)  # tests only
        if name == "clean":
            cmd.add_argument("--apply", action="store_true", help="actually delete, remove and move")
    args = parser.parse_args(argv)
    repo = repo_root(Path.cwd())
    try:
        if args.command == "new":
            print(new_worktree(repo, args.branch))
            return 0
        if args.command == "clean" or args.prs_json is None:
            git(repo, "fetch", "--prune", "origin", check=False)
        plan = build_plan(repo, pull_requests(repo, args.prs_json), move_outside=args.move_outside)
        do = args.command == "clean" and args.apply
        if do:
            apply(repo, plan)
        print(report(plan, do))
        return 0
    except RuntimeError as error:
        print(f"repo_hygiene: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    sys.exit(main())
