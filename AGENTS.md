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

## Delivery workflow (Jerome, 2026-09-29)

1. **One merge at a time, in agreed order.** The order lives in
   `docs/STATUS.md`. Rebase onto `origin/main` only when your PR is next,
   then merge, then run cleanup. Never keep a long-lived integration branch
   alongside main; once main has everything, retire it.
2. **Each area has an owner.**
   - Codex: `tools/codex_app_server.py`, `codex_probe.py`, `staged_edits.py`,
     `agent_relay.py`, `capability_inventory.py`, packaging, and the
     collaboration and edit-gate code in `tools/desktop.py` and
     `desktop_state.py`.
   - Claude: `tools/claude_runtime.py`, `claude_runtime_probe.py`,
     `approval_modes.py`, `conversation.py`, `repo_hygiene.py`,
     `agent_mail.py`, and the Claude-session and conversation code in
     `tools/desktop.py`.
   - Before changing the other lane's area, send it a mail (below) naming
     the file and why, or build on top once its PR merges.
3. **At most 2 open PRs per lane.** Finish the reviews you owe before opening
   new work.
4. **Every review ends with one verdict line.** Both agents push as the same
   GitHub account, so formal "changes requested" is not available:
   - `Verdict: CHANGES REQUESTED at <sha>`, or
   - `Verdict: APPROVED at <sha>`.
   A PR merges only with the other lane's `APPROVED` at the exact head being
   merged, plus green tests. Jerome may waive this for a specific PR.
5. **`docs/STATUS.md` is the live board.** It has one row per open PR:
   owner, head, reviewer, verdict, blocker and position in the merge order.
   Update your own rows in the same commit as the change, or with a
   `status:` commit. Codex copies it into Linear. COMMS stays for Jerome's
   prompts and the replies to them.
6. **Every reply ends with its state:** `ready for review by <lane>`,
   `blocked on <what>` or `done`.

## Talking to the other lane (agent mail)

Nothing tells an agent that the other one posted in COMMS or on a PR.
`tools/agent_mail.py` is the signal. Messages live in the shared local
`.git/agent-mail/`, so every worktree sees them and nothing is pushed.
Never put secrets in a message.

```powershell
python -m tools.agent_mail send --from claude --to codex --subject "PR #20 fixed" --ref "PR #20" --body "..."
python -m tools.agent_mail inbox --agent codex        # read unread mail
python -m tools.agent_mail ack --agent codex --all    # mark it read once handled
python -m tools.agent_mail watch --agent codex --remote
```

- **When to send mail:** send it whenever you need the other lane: a review
  is ready, you are blocked, you want to touch its area, or you found a
  problem in its merged work. Put the full detail on the PR or in COMMS, and
  keep the mail to one subject line with a link.
- **Check your inbox** at the start of every task, and again before you end
  your turn. Claude Code does this automatically at session start, through
  `.claude/settings.json`.
- **Watch while you work.** When your harness can run a background process,
  keep `watch --agent <you> --remote` running while you work. It prints one
  line for each new mail, each new COMMS or lane-prefixed commit the other
  lane pushes to main, and each new PR comment.
- **What the watcher cannot do:** it cannot wake an agent whose session is not
  running. Such mail waits in the inbox until that agent's next start or
  check. When something is urgent and the other agent is idle, tell Jerome.

## Unchanged safety rules (see the COMMS protocols)

- No API keys, credential interception, hidden billing or deployment.
- Fable needs a confirmation for each session.
- CLI mode (`bridge.ps1`) stays.
- Merge only green, reviewed work, and only in this repo.
