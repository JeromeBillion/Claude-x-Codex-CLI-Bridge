# Agent rules for this repo (Claude and Codex)

These rules apply to every agent working on this repo, in every session. The
lane protocols in `comms/claude/COMMS.md` and `comms/codex/COMMS.md` still
apply; this file adds the workspace rules Jerome set on 2026-09-29.

## Workspace rules

1. **Work only inside the repo.** Never create clones, worktrees, scratch
   folders or branch copies next to the repo (for example
   `Desktop\Codex-Fable-Bridge-*`) or anywhere else on the machine.
2. **One worktree per branch, under `.worktrees/`.** It is git-ignored.
   Create it with:
   ```powershell
   python -m tools.repo_hygiene new claude/<topic>   # or codex/<topic>
   ```
   This makes `.worktrees\claude-<topic>` on a new branch from `origin/main`.
   Branch names are `claude/<topic>` or `codex/<topic>`, in lowercase.
3. **Never switch branches in the main checkout.** Another agent may be
   working there. The main checkout stays on `main`, and all branch work
   happens in your own worktree.
4. **Temporary files** go in `.worktrees/<your worktree>` or your session's
   scratch directory. Never write them to `Desktop` or to other repos. Do not
   leave `claude-desktop-*`, probe or smoke folders behind.
5. **After every merge to GitHub, the agent that merged cleans up.** Run from
   the repo:
   ```powershell
   python -m tools.repo_hygiene status          # read-only preview
   python -m tools.repo_hygiene clean --apply   # delete merged branches and worktrees, fast-forward main
   ```
   - **Deletes** a branch, local and remote, only when it has no open PR and
     its tip is either the exact head of a merged PR or already in
     `origin/main`. Its worktree is removed only if it has no uncommitted
     changes.
   - **Never deletes** a branch with an open PR, a branch with no PR and
     unmerged commits, or a worktree with uncommitted changes. It never
     touches another agent's unmerged work.
   - **Syncs `main`** in the main checkout by fast-forward only. Rebase your
     own open branches onto `origin/main` in your own worktree:
     `git fetch` then `git rebase origin/main`. Force-push only your own
     branch, with `--force-with-lease`. Never rebase or force-push the other
     lane's branches.
   - **Reports** your cleanup result (`clean --apply` output) in your COMMS
     reply.
6. **Worktrees left outside the repo** must be moved in when they are clean:
   `python -m tools.repo_hygiene clean --apply --move-outside`. If one has
   uncommitted work, its owner commits or pushes first.

## Unchanged safety rules (see the COMMS protocols)

- No API keys, credential interception, hidden billing or deployment.
- Fable needs a confirmation for each session.
- CLI mode (`bridge.ps1`) stays.
- Merge only green, reviewed work, and only in this repo.
