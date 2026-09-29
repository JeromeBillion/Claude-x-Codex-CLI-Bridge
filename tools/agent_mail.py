#!/usr/bin/env python3
"""Local mailbox and watchdog between the Claude and Codex lanes.

    python -m tools.agent_mail send --from claude --to codex --subject "PR #20 ready" --body "..."
    python -m tools.agent_mail inbox --agent claude            # unread messages (does not mark read)
    python -m tools.agent_mail ack --agent claude --all        # mark read after acting on them
    python -m tools.agent_mail watch --agent claude --remote   # one line per new event, for a watchdog

Messages live under the repository's common `.git/agent-mail/`, so every
worktree shares them and nothing is ever pushed. `watch` prints one line per
new message. With `--remote`, it also reports new commits the other lane
pushes to origin/main (its COMMS lane or any commit whose subject starts with
its name) and new comments on open pull requests. It never runs a model, never
reads credentials and never changes the repository beyond `git fetch`.
Do not put secrets in messages.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Callable, Iterator
import uuid

LANES = ("claude", "codex")
MAX_BODY = 20_000


def git(repo: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed")
    return done.stdout.strip()


def mail_root(start: Path) -> Path:
    common = Path(git(start, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    return common / "agent-mail"


def _other(agent: str) -> str:
    return "codex" if agent == "claude" else "claude"


def _replace(source: Path, target: Path, attempts: int = 20) -> None:
    """os.replace with a short retry: on Windows a reader holding the file makes it fail briefly."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def send(root: Path, sender: str, to: str, subject: str, body: str, ref: str | None = None) -> Path:
    if sender not in LANES or to not in LANES or sender == to:
        raise ValueError("mail goes from one lane to the other")
    if not subject.strip():
        raise ValueError("a subject is required")
    now = datetime.now(timezone.utc)
    message = {"id": f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}", "from": sender, "to": to,
               "subject": " ".join(subject.split())[:200], "body": body[:MAX_BODY], "ref": ref,
               "sent_at": now.isoformat(timespec="seconds")}
    inbox = root / to / "unread"
    inbox.mkdir(parents=True, exist_ok=True)
    temporary = inbox / f".{message['id']}.tmp"
    temporary.write_text(json.dumps(message, ensure_ascii=False, indent=2), encoding="utf-8")
    final = inbox / f"{message['id']}.json"
    _replace(temporary, final)  # readers never see a half-written message
    return final


def unread(root: Path, agent: str) -> list[dict[str, Any]]:
    messages = []
    for path in sorted((root / agent / "unread").glob("*.json")):
        try:
            messages.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue  # being written or damaged; picked up on the next pass
    return messages


def ack(root: Path, agent: str, message_id: str | None) -> int:
    done = root / agent / "read"
    done.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted((root / agent / "unread").glob("*.json")):
        if message_id is None or path.stem == message_id:
            _replace(path, done / path.name)
            count += 1
    return count


def line(message: dict[str, Any]) -> str:
    ref = f" [{message['ref']}]" if message.get("ref") else ""
    return (f"MAIL {message['id']} from {message['from']}{ref}: {message['subject']}"
            f" -- read with: python -m tools.agent_mail inbox --agent {message['to']}")


class RemoteWatch:
    """New commits by the other lane on origin/main, and new PR comments, as one-line events."""

    def __init__(self, repo: Path, agent: str, gh: Callable[[list[str]], str] | None = None) -> None:
        self.repo, self.other = repo, _other(agent)
        self.gh = gh or self._gh
        git(repo, "fetch", "-q", "origin", "main", check=False)
        self.last_main = git(repo, "rev-parse", "origin/main", check=False)
        # Existing comments are not news; only ones posted after the watch started are reported.
        self.seen_comments: set[str] = set(self._comment_ids())

    @staticmethod
    def _gh(args: list[str]) -> str:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=False)
        return done.stdout if done.returncode == 0 else ""

    def _comments(self) -> list[dict[str, Any]]:
        raw = self.gh(["pr", "list", "--state", "open", "--json", "number,comments", "--limit", "50"])
        try:
            prs = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            return []
        return [{"pr": pr["number"], **comment} for pr in prs for comment in pr.get("comments") or []]

    def _comment_ids(self) -> Iterator[str]:
        for comment in self._comments():
            yield str(comment.get("id") or comment.get("url") or comment.get("createdAt"))

    def poll(self) -> list[str]:
        events = []
        git(self.repo, "fetch", "-q", "origin", "main", check=False)
        head = git(self.repo, "rev-parse", "origin/main", check=False)
        if head and self.last_main and head != self.last_main:
            log = git(self.repo, "log", "--format=%h%x09%s", f"{self.last_main}..{head}", check=False)
            for entry in log.splitlines():
                sha, _, subject = entry.partition("\t")
                touched = git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha, check=False)
                if f"comms/{self.other}/" in touched or subject.lower().startswith(("comms: " + self.other,
                                                                                    self.other)):
                    events.append(f"MAIN {sha} by {self.other}: {subject[:160]}")
        self.last_main = head or self.last_main
        for comment in self._comments():
            key = str(comment.get("id") or comment.get("url") or comment.get("createdAt"))
            if key in self.seen_comments:
                continue
            self.seen_comments.add(key)
            first = (comment.get("body") or "").strip().splitlines()[:1]
            events.append(f"PR #{comment['pr']} comment: {(first[0] if first else '')[:160]}")
        return events


def watch(root: Path, repo: Path, agent: str, *, interval: float, remote: bool, rounds: int | None = None,
          out: Callable[[str], None] = print, remote_watch: RemoteWatch | None = None) -> None:
    announced: set[str] = set()
    remote_watch = remote_watch or (RemoteWatch(repo, agent) if remote else None)
    remote_every = max(1, round(60 / max(interval, 1)))  # GitHub at most once a minute
    tick = 0
    while rounds is None or tick < rounds:
        for message in unread(root, agent):
            if message["id"] not in announced:
                announced.add(message["id"])
                out(line(message))
        if remote_watch is not None and tick % remote_every == 0 and tick > 0:
            for event in remote_watch.poll():
                out(event)
        tick += 1
        if rounds is None or tick < rounds:
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Claude/Codex mailbox and watchdog")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", choices=LANES, required=True)
    s.add_argument("--to", choices=LANES, required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file", type=Path)
    s.add_argument("--ref", help="PR number, commit or ticket this is about")
    i = sub.add_parser("inbox")
    i.add_argument("--agent", choices=LANES, required=True)
    i.add_argument("--brief", action="store_true", help="one line per message (for session-start hooks)")
    a = sub.add_parser("ack")
    a.add_argument("--agent", choices=LANES, required=True)
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--id")
    group.add_argument("--all", action="store_true")
    w = sub.add_parser("watch")
    w.add_argument("--agent", choices=LANES, required=True)
    w.add_argument("--interval", type=float, default=15.0)
    w.add_argument("--remote", action="store_true", help="also report the other lane's pushes and PR comments")
    args = parser.parse_args(argv)
    repo = Path.cwd()
    try:
        root = mail_root(repo)
        if args.command == "send":
            body = args.body_file.read_text(encoding="utf-8") if args.body_file else args.body
            print(f"sent {send(root, args.sender, args.to, args.subject, body, args.ref).stem}")
        elif args.command == "inbox":
            messages = unread(root, args.agent)
            if args.brief:
                print(f"agent-mail: {len(messages)} unread for {args.agent}"
                      + ("" if not messages else ". Read them: python -m tools.agent_mail inbox --agent " + args.agent))
                for message in messages:
                    print("  " + line(message))
            else:
                for message in messages:
                    print(f"--- {message['id']} from {message['from']} at {message['sent_at']}"
                          + (f" re {message['ref']}" if message.get("ref") else ""))
                    print(f"Subject: {message['subject']}\n{message['body']}\n")
                if not messages:
                    print("no unread mail")
        elif args.command == "ack":
            print(f"marked {ack(root, args.agent, None if args.all else args.id)} read")
        elif args.command == "watch":
            if not 1 <= args.interval <= 300:
                parser.error("interval must be 1-300 seconds")
            watch(root, repo, args.agent, interval=args.interval, remote=args.remote,
                  out=lambda text: print(text, flush=True))
        return 0
    except (RuntimeError, ValueError, OSError) as error:
        print(f"agent_mail: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
