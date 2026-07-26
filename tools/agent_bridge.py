#!/usr/bin/env python3
"""A small, local Codex <-> Claude/Fable collaboration bridge."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4


VERSION = "0.3.0"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CLAUDE_MODEL = "claude-fable-5"
BRIDGE_ROOT = Path(__file__).resolve().parent.parent
SESSION_DIR = ".agent-bridge/sessions"
MEMORY_DIR = ".agent-bridge/memory"
STATUS_PATTERN = re.compile(
    r"^\s*BRIDGE_STATUS:\s*(DONE|CONTINUE|CHALLENGE|BLOCKED)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
MEMORY_PATTERN = re.compile(
    r"^[ \t]*BRIDGE_MEMORY_BEGIN[ \t]*\r?\n(.*?)^[ \t]*BRIDGE_MEMORY_END[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


class BridgeError(RuntimeError):
    """An operator-actionable bridge error."""


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def extract_status(output: str) -> str:
    matches = STATUS_PATTERN.findall(output)
    return matches[-1].upper() if matches else "CONTINUE"


def tail(value: str, limit: int = 4_000) -> str:
    value = value.strip()
    return value if len(value) <= limit else "..." + value[-limit:]


def extract_memory(output: str) -> str | None:
    """Return the last shared-memory replacement block, or None if absent."""
    matches = MEMORY_PATTERN.findall(output)
    return matches[-1].strip() if matches else None


def strip_memory_blocks(text: str) -> str:
    """Memory is injected into prompts separately; keep it out of the
    rendered conversation so it does not consume the context budget twice."""
    return MEMORY_PATTERN.sub("[shared memory updated]", text)


def memory_path_for(state_root: Path, workspace: Path) -> Path:
    """One shared memory doc per target workspace, stored under the bridge.

    Keeping it out of the workspace matters: the agents audit `git diff` every
    turn, and a memory file inside the repo would show up as noise there.
    """
    digest = hashlib.sha1(str(workspace).casefold().encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.casefold()).strip("-") or "workspace"
    return state_root / MEMORY_DIR / f"{slug}-{digest}.md"


def read_memory(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def write_memory(path: Path, content: str, max_chars: int) -> str:
    content = content.strip()
    if len(content) > max_chars:
        marker = "\n[TRUNCATED AT MEMORY CAP]"
        content = content[: max(0, max_chars - len(marker))] + marker
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8", newline="\n")
    return content


class Console:
    COLORS = {
        "reset": "\033[0m",
        "muted": "\033[2m",
        "cyan": "\033[36m",
        "magenta": "\033[35m",
        "green": "\033[32m",
        "yellow": "\033[33m",
        "red": "\033[31m",
        "bold": "\033[1m",
    }

    def __init__(self, stream: Any = sys.stdout) -> None:
        self.stream = stream
        self.color = bool(getattr(stream, "isatty", lambda: False)()) and not os.getenv(
            "NO_COLOR"
        )

    def _paint(self, text: str, *styles: str) -> str:
        if not self.color:
            return text
        prefix = "".join(self.COLORS[style] for style in styles)
        return f"{prefix}{text}{self.COLORS['reset']}"

    def line(self, text: str = "") -> None:
        try:
            print(text, file=self.stream, flush=True)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "ascii"
            safe_text = text.encode(encoding, errors="replace").decode(encoding)
            print(safe_text, file=self.stream, flush=True)

    def info(self, text: str) -> None:
        self.line(self._paint(text, "muted"))

    def warning(self, text: str) -> None:
        self.line(self._paint(text, "yellow"))

    def error(self, text: str) -> None:
        self.line(self._paint(text, "red"))

    def banner(self, title: str) -> None:
        self.line(self._paint(f"\n== {title} ==", "bold", "cyan"))

    def agent_output(self, name: str, output: str, elapsed: float) -> None:
        color = "cyan" if name == "Codex" else "magenta"
        self.line(self._paint(f"\n{name} | {elapsed:.1f}s", "bold", color))
        self.line(output.strip())


class Transcript:
    """Append-only JSONL state for one bridge session."""

    def __init__(self, path: Path, events: list[dict[str, Any]] | None = None) -> None:
        self.path = path
        self.events = events or []

    @classmethod
    def create(
        cls,
        state_root: Path,
        goal: str,
        metadata: dict[str, Any],
    ) -> "Transcript":
        session_root = state_root / SESSION_DIR
        session_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = session_root / f"{stamp}-{uuid4().hex[:8]}.jsonl"
        transcript = cls(path)
        transcript.append(
            event_type="meta",
            author="bridge",
            content=goal,
            metadata={"version": VERSION, "goal": goal, **metadata},
        )
        transcript.append(event_type="human", author="Human", content=goal)
        return transcript

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        if not path.is_file():
            raise BridgeError(f"Transcript not found: {path}")
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BridgeError(
                    f"Invalid transcript JSON on line {line_number}: {path}"
                ) from exc
            if not isinstance(event, dict):
                raise BridgeError(
                    f"Invalid transcript event on line {line_number}: {path}"
                )
            events.append(event)
        if not events:
            raise BridgeError(f"Transcript is empty: {path}")
        return cls(path, events)

    @property
    def goal(self) -> str:
        for event in self.events:
            if event.get("type") == "meta":
                metadata = event.get("metadata") or {}
                return str(metadata.get("goal") or event.get("content") or "").strip()
        raise BridgeError(f"Transcript has no session metadata: {self.path}")

    @property
    def workspace(self) -> str:
        for event in self.events:
            if event.get("type") == "meta":
                metadata = event.get("metadata") or {}
                return str(metadata.get("workspace") or "").strip()
        return ""

    def append(
        self,
        event_type: str,
        author: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event = {
            "timestamp": utc_timestamp(),
            "type": event_type,
            "author": author,
            "content": content,
        }
        if metadata:
            event["metadata"] = metadata
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.events.append(event)

    def render(self, max_chars: int) -> str:
        segments: list[str] = []
        for event in self.events:
            if event.get("type") not in {"human", "agent", "error"}:
                continue
            author = str(event.get("author") or "Unknown").upper()
            content = strip_memory_blocks(str(event.get("content") or "")).strip()
            segments.append(f"[{author}]\n{content}")

        selected: list[str] = []
        used = 0
        for segment in reversed(segments):
            separator_size = 2 if selected else 0
            remaining = max_chars - used - separator_size
            if remaining <= 0:
                break
            if len(segment) > remaining:
                if not selected:
                    marker = "[EARLIER CONTENT TRUNCATED]\n"
                    tail_size = max(0, remaining - len(marker))
                    selected.append(marker + (segment[-tail_size:] if tail_size else ""))
                break
            selected.append(segment)
            used += len(segment) + separator_size
        return "\n\n".join(reversed(selected)) or "(No collaboration turns yet.)"


def resolve_resume_path(state_root: Path, value: str) -> Path:
    if value.lower() != "latest":
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = state_root / candidate
        return candidate.resolve()

    session_root = state_root / SESSION_DIR
    candidates = sorted(session_root.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    if not candidates:
        raise BridgeError(f"No transcripts found under {session_root}")
    return candidates[-1].resolve()


def resolve_workspace(raw_workspace: str | None, transcript: "Transcript | None") -> Path:
    """Pick the workspace: explicit flag > workspace recorded in the transcript > cwd.

    Resuming must not silently retarget the agents at whatever directory the
    shell happens to be in — that is how a run ends up editing $HOME.
    """
    if raw_workspace:
        workspace = Path(raw_workspace).expanduser()
    elif transcript is not None and transcript.workspace:
        workspace = Path(transcript.workspace).expanduser()
    else:
        workspace = Path.cwd()
    workspace = workspace.resolve()
    if not workspace.is_dir():
        raise BridgeError(f"Workspace is not a directory: {workspace}")
    return workspace


def ensure_git_workspace(workspace: Path) -> None:
    """Refuse to unleash write-capable agents outside a git repository."""
    for candidate in (workspace, *workspace.parents):
        if (candidate / ".git").exists():
            return
    raise BridgeError(
        f"Workspace is not inside a git repository: {workspace}\n"
        "Launch from the project repo, pass --workspace, or use --allow-non-git."
    )


def resolve_executable(command: str, *, home: Path | None = None) -> str:
    candidate = Path(command).expanduser()
    if candidate.parent != Path("."):
        if candidate.is_file():
            return str(candidate.resolve())
        raise BridgeError(f"Command not found: {candidate}")
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if command.casefold() == "codex":
        config_path = (home or Path.home()) / ".codex" / "config.toml"
        try:
            with config_path.open("rb") as handle:
                config = tomllib.load(handle)
            configured_path = config["mcp_servers"]["node_repl"]["env"]["CODEX_CLI_PATH"]
            configured_executable = Path(configured_path).expanduser()
            if configured_executable.is_file():
                return str(configured_executable.resolve())
        except (KeyError, OSError, TypeError, tomllib.TOMLDecodeError):
            pass
    raise BridgeError(f"Command not found on PATH: {command}")


REAP_TIMEOUT_SECONDS = 30


def terminate_process_tree(process: "subprocess.Popen[str]") -> None:
    """Kill the agent and every descendant.

    On Windows the agent CLIs are .CMD shims, so the direct child is a cmd.exe
    wrapper; killing only it leaves the real agent orphaned and still writing
    to the workspace while the bridge has already recorded the turn as failed.
    """
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def drain_killed_process(process: "subprocess.Popen[str]") -> tuple[str, str]:
    """Collect buffered output after a kill without wedging on leaked pipes.

    Descendants can inherit the stdout/stderr write handles; if any survive the
    tree kill, an unbounded communicate() blocks forever. Bound the reap and
    abandon the pipes rather than hang the whole bridge.
    """
    try:
        return process.communicate(timeout=REAP_TIMEOUT_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return "", ""


@dataclass(frozen=True)
class AgentResult:
    output: str
    elapsed_seconds: float


class AgentClient:
    name: str
    role: str

    def command(self, workspace: Path, output_path: Path | None = None) -> list[str]:
        raise NotImplementedError

    def invoke(self, prompt: str, workspace: Path, timeout_seconds: int) -> AgentResult:
        raise NotImplementedError

    @staticmethod
    def _execute(
        command: Sequence[str],
        prompt: str,
        workspace: Path,
        timeout_seconds: int,
    ) -> tuple[subprocess.CompletedProcess[str], float]:
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except OSError as exc:
            raise BridgeError(f"Could not start agent command: {exc}") from exc
        try:
            stdout, stderr = process.communicate(input=prompt, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            # A plain kill only reaps the CLI shim on Windows; take the whole tree
            # down so no orphaned agent keeps editing the repo behind our back.
            terminate_process_tree(process)
            stdout, stderr = drain_killed_process(process)
            message = (
                f"Agent timed out after {timeout_seconds} seconds. "
                "Increase --timeout if the task is legitimately long."
            )
            partial = tail(stdout or "", 2_000)
            if partial:
                message += f"\nPartial output before the timeout:\n{partial}"
            raise BridgeError(message) from None
        completed = subprocess.CompletedProcess(
            list(command), process.returncode, stdout, stderr
        )
        return completed, time.monotonic() - started

    @staticmethod
    def _check_result(
        completed: subprocess.CompletedProcess[str], agent_name: str
    ) -> None:
        if completed.returncode == 0:
            return
        details = tail(completed.stderr or completed.stdout or "No CLI error output.")
        raise BridgeError(f"{agent_name} exited with code {completed.returncode}:\n{details}")


class CodexClient(AgentClient):
    name = "Codex"

    def __init__(
        self, executable: str, model: str, sandbox: str, effort: str | None = None
    ) -> None:
        self.executable = executable
        self.model = model
        self.sandbox = sandbox
        self.effort = effort

    def command(self, workspace: Path, output_path: Path | None = None) -> list[str]:
        if output_path is None:
            output_path = Path("<last-message-file>")
        command = [
            self.executable,
            "exec",
            "--model",
            self.model,
            "--sandbox",
            self.sandbox,
        ]
        if self.effort is not None:
            command.extend(["-c", f"model_reasoning_effort={self.effort}"])
        return command + [
            "--cd",
            str(workspace),
            "--color",
            "never",
            "--ephemeral",
            "--output-last-message",
            str(output_path),
            "-",
        ]

    def invoke(self, prompt: str, workspace: Path, timeout_seconds: int) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="agent-bridge-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            completed, elapsed = self._execute(
                self.command(workspace, output_path), prompt, workspace, timeout_seconds
            )
            self._check_result(completed, self.name)
            output = ""
            if output_path.is_file():
                output = output_path.read_text(encoding="utf-8", errors="replace").strip()
            output = output or completed.stdout.strip()
            if not output:
                raise BridgeError("Codex completed without returning a final message.")
            return AgentResult(output=output, elapsed_seconds=elapsed)


class ClaudeClient(AgentClient):
    name = "Claude/Fable"

    def __init__(
        self,
        executable: str,
        model: str,
        permission_mode: str,
        max_budget_usd: float | None,
        effort: str | None = None,
    ) -> None:
        self.executable = executable
        self.model = model
        self.permission_mode = permission_mode
        self.max_budget_usd = max_budget_usd
        self.effort = effort

    def command(self, workspace: Path, output_path: Path | None = None) -> list[str]:
        del workspace, output_path
        command = [
            self.executable,
            "--print",
            "--model",
            self.model,
            "--permission-mode",
            self.permission_mode,
            "--output-format",
            "text",
            "--no-session-persistence",
        ]
        if self.effort is not None:
            command.extend(["--effort", self.effort])
        if self.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(self.max_budget_usd)])
        return command

    def invoke(self, prompt: str, workspace: Path, timeout_seconds: int) -> AgentResult:
        completed, elapsed = self._execute(
            self.command(workspace), prompt, workspace, timeout_seconds
        )
        self._check_result(completed, self.name)
        output = completed.stdout.strip()
        if not output:
            raise BridgeError("Claude/Fable completed without returning a final message.")
        return AgentResult(output=output, elapsed_seconds=elapsed)


def build_agents(
    args: argparse.Namespace, codex_executable: str, claude_executable: str
) -> list[AgentClient]:
    """Construct both agents as equal co-authors; --lead only sets turn order."""
    codex = CodexClient(
        codex_executable, args.codex_model, args.codex_sandbox, args.codex_effort
    )
    claude = ClaudeClient(
        claude_executable,
        args.claude_model,
        args.claude_permission_mode,
        args.claude_max_budget_usd,
        args.claude_effort,
    )
    first, second = (codex, claude) if args.lead == "codex" else (claude, codex)
    for agent, partner in ((first, second), (second, first)):
        agent.role = (
            f"Act as an equal co-author with {partner.name}. You share full ownership of "
            "this work and full responsibility for policing its quality: audit every "
            f"handoff {partner.name} gives you against the real repository, refute any "
            "claim that does not survive your own testing, and keep challenging until "
            "the work withstands both of you. Neither of you outranks the other, and "
            "neither of you gets a pass."
        )
    return [first, second]


@dataclass
class BridgeConfig:
    workspace: Path
    rounds: int
    timeout_seconds: int
    context_chars: int
    pause: bool
    memory_path: Path | None = None
    memory_chars: int = 8_000


def build_agent_prompt(
    agent: AgentClient,
    transcript: Transcript,
    config: BridgeConfig,
    round_number: int,
    round_limit: int,
    verify_done_by: str | None = None,
) -> str:
    collaboration = transcript.render(config.context_chars)
    memory_section = ""
    memory_instructions = ""
    if config.memory_path is not None:
        memory = read_memory(config.memory_path)
        memory_section = f"""
SHARED MEMORY (compacted context carried across rounds and sessions)
{memory or "(empty - seed it this turn)"}
"""
        memory_instructions = f"""
To update the shared memory, include this block anywhere before the status line:
BRIDGE_MEMORY_BEGIN
<the full replacement memory doc>
BRIDGE_MEMORY_END
The block replaces the whole doc, so carry forward your partner's still-valid entries.
Keep only what future turns need: durable decisions with their why, architecture facts,
verified-DONE claims, gotchas, and open threads. Compact aggressively; hard cap
{config.memory_chars} characters. Never store secrets, credentials, or
machine-specific absolute paths.
"""
    if verify_done_by:
        mission = f"""VERIFICATION TURN
{verify_done_by} claims the goal is complete. Do not take their word for it. Assume the
claim is wrong and spend this turn trying to prove that: re-run every test yourself, read
the full diff, and attack edge cases, failure paths, and requirements the goal implies but
the handoff never mentions. If you find anything real, answer BRIDGE_STATUS: CHALLENGE
with your evidence - and if you fix it yourself, still answer CHALLENGE or CONTINUE so
your partner verifies your fix. Never answer DONE on a turn in which you changed anything
material: that would certify your own unreviewed work. Answer BRIDGE_STATUS: DONE only if
you actively tried to break the work, failed, and changed nothing - and list exactly what
you tried. Should-fix polish alone does not block DONE; record it in CHALLENGES instead."""
    else:
        mission = """Work directly in the shared repository. Inspect the current files, audit the other
agent's handoff, make changes, run tests, and keep moving until the goal works. Do not
just discuss it. The human can add instructions after every round; follow the newest
human instruction."""
    return f"""You are {agent.name}, one half of a local coding pair of equals.

GOAL
{transcript.goal}

ROUND {round_number} OF {round_limit}
Workspace: {config.workspace}
Role: {agent.role}

{mission}

ACCOUNTABILITY PROTOCOL
1. Verify before you build. Audit your partner's latest handoff against the real
   repository: read the actual diff and files, re-run the tests they claim pass, and
   probe what they did not mention. Classify each material claim CONFIRMED (your own
   evidence), REFUTED (your own evidence), or UNVERIFIED. Never accept a claim you
   did not check yourself.
2. Challenge what does not hold. Wrong decisions, weak designs, untested paths, and
   broken claims get named bluntly and either fixed by you or handed back with
   BRIDGE_STATUS: CHALLENGE. Evidence-backed disagreement is the most valuable thing
   you can give your partner; rubber-stamping their work is a failed turn.
3. Then advance the goal yourself and verify your own changes the same way you expect
   your partner to check them. On a verification turn, breaking the claim IS the work -
   make no changes beyond what refuting or fixing it requires.
4. Expect the same treatment: your partner will re-run your tests and attack your
   claims next turn, so hand off evidence, not assertions.
Do not praise, do not soften, and do not agree just to be agreeable. A refutation and a
confirmation earned by a real attack are equally valuable; the failed turn is the
unchecked verdict in either direction. Do not manufacture objections to look rigorous -
mark each challenge must-fix or should-fix and let nits go. If you find nothing wrong,
earn the clean verdict by listing what you attacked and how it survived.
{memory_section}
CONVERSATION SO FAR
{collaboration}

Structure the handoff exactly as:
REVIEW - your verdict on each material claim in your partner's last handoff, with your
  evidence (first turn: audit the current repository state instead)
CHALLENGES - numbered, each with severity and evidence, or "None - attacked <what you
  tried> and the work held"
WORK - what you changed and why
EVIDENCE - the commands you ran and the decisive lines of their output, not full logs
{memory_instructions}
Statuses:
DONE - goal verified end-to-end by you this turn, and your partner's claims checked.
CONTINUE - real work remains; the handoff says what and why.
CHALLENGE - you refuted the last handoff or found must-fix defects in it.
BLOCKED - you cannot proceed without the human.

End your reply with exactly one line containing only the status and nothing else on it,
for example:
BRIDGE_STATUS: CONTINUE
"""


def command_preview(command: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(command))


def print_git_status(workspace: Path, console: Console) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "status", "--short"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.warning(f"Could not read git status: {exc}")
        return
    status = result.stdout.strip()
    console.line(status or "Working tree is clean (bridge transcript is ignored).")


def human_checkpoint(
    transcript: Transcript,
    workspace: Path,
    console: Console,
    at_round_limit: bool,
) -> tuple[bool, int]:
    while True:
        suffix = " | :more [N] to extend" if at_round_limit else ""
        try:
            value = input(
                f"\nYou > Enter to continue | type an interjection | :status | :quit{suffix}\n> "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.warning("\nStopped by human.")
            return True, 0

        if not value:
            return False, 0
        lowered = value.lower()
        if lowered in {":quit", ":q", ":stop"}:
            return True, 0
        if lowered == ":status":
            console.info(f"Transcript: {transcript.path}")
            print_git_status(workspace, console)
            continue
        if lowered.startswith(":more"):
            parts = value.split()
            if len(parts) > 2:
                console.warning("Use :more or :more N")
                continue
            try:
                extra = positive_int(parts[1]) if len(parts) == 2 else 1
            except (ValueError, argparse.ArgumentTypeError):
                console.warning("N must be a positive integer.")
                continue
            return False, extra

        transcript.append("human", "Human", value)
        console.info("Interjection added to both agents' context.")
        return False, 1 if at_round_limit else 0


def seed_completion_state(transcript: Transcript) -> tuple[int, str | None]:
    """Carry an unanswered DONE claim across a resume.

    If the last substantive event is an agent turn that claimed DONE, the other
    agent still owes it an adversarial verification turn. A later human
    instruction or a failed turn supersedes the claim.
    """
    for event in reversed(transcript.events):
        event_type = event.get("type")
        if event_type == "agent":
            metadata = event.get("metadata") or {}
            if str(metadata.get("status") or "").upper() == "DONE":
                author = str(event.get("author") or "").strip()
                if author:
                    return 1, author
            return 0, None
        if event_type in {"human", "error"}:
            return 0, None
    return 0, None


def run_bridge(
    transcript: Transcript,
    agents: Sequence[AgentClient],
    config: BridgeConfig,
    console: Console,
) -> int:
    round_limit = config.rounds
    round_number = 1
    consecutive_done, last_done_author = seed_completion_state(transcript)
    last_status: str | None = None
    agent_runs = 0
    stopped_by_human = False

    console.banner("Codex <-> Claude/Fable")
    console.info(f"Workspace:  {config.workspace}")
    console.info(f"Transcript: {transcript.path}")
    if config.memory_path is not None:
        console.info(f"Memory:     {config.memory_path}")
    console.info(f"Limit:      {round_limit} rounds / {round_limit * len(agents)} agent runs")

    while round_number <= round_limit:
        console.banner(f"Round {round_number} / {round_limit}")
        failures_this_round = 0
        for agent in agents:
            verify_done_by = (
                last_done_author
                if last_done_author and last_done_author != agent.name
                else None
            )
            if verify_done_by:
                console.info(
                    f"{agent.name} is adversarially verifying {verify_done_by}'s DONE claim..."
                )
            else:
                console.info(f"{agent.name} is working...")
            prompt = build_agent_prompt(
                agent, transcript, config, round_number, round_limit, verify_done_by
            )
            try:
                result = agent.invoke(prompt, config.workspace, config.timeout_seconds)
            except BridgeError as exc:
                failures_this_round += 1
                consecutive_done = 0
                last_done_author = None
                message = str(exc)
                transcript.append(
                    "error",
                    agent.name,
                    message,
                    {"round": round_number},
                )
                console.error(f"\n{agent.name} failed\n{message}")
                continue

            agent_runs += 1
            status = extract_status(result.output)
            memory_update = (
                extract_memory(result.output) if config.memory_path is not None else None
            )
            transcript.append(
                "agent",
                agent.name,
                result.output,
                {
                    "round": round_number,
                    "status": status,
                    "elapsed_seconds": round(result.elapsed_seconds, 3),
                    "memory_updated": memory_update is not None,
                },
            )
            console.agent_output(agent.name, result.output, result.elapsed_seconds)
            if memory_update is not None:
                stored = write_memory(
                    config.memory_path, memory_update, config.memory_chars
                )
                console.info(f"Shared memory updated ({len(stored)} chars).")

            last_status = status
            if status == "DONE":
                # Two DONE claims only finish the run when they come from
                # different agents, so a repeated self-claim never self-certifies.
                if agent.name != last_done_author:
                    consecutive_done += 1
                last_done_author = agent.name
                if consecutive_done >= 2:
                    console.banner(
                        f"{verify_done_by}'s DONE claim survived "
                        f"{agent.name}'s adversarial verification"
                    )
                    console.info(f"Agent runs: {agent_runs}")
                    console.info(f"Transcript: {transcript.path}")
                    return 0
            else:
                voided_claimant = last_done_author
                consecutive_done = 0
                last_done_author = None
                if status == "CHALLENGE":
                    message = f"{agent.name} challenged the last handoff"
                    if voided_claimant:
                        message += f"; {voided_claimant}'s DONE claim is void"
                    console.warning(message + ".")

        if failures_this_round == len(agents):
            console.error("\nBoth agent CLIs failed in the same round.")
            if not config.pause:
                console.info(f"Transcript: {transcript.path}")
                return 1
            console.warning("Dropping to the checkpoint so you decide (:quit to stop).")

        if config.pause:
            events_before = len(transcript.events)
            stopped_by_human, extra_rounds = human_checkpoint(
                transcript,
                config.workspace,
                console,
                at_round_limit=round_number == round_limit,
            )
            if any(
                event.get("type") == "human"
                for event in transcript.events[events_before:]
            ):
                # A new human instruction moves the goalposts; a stale DONE
                # claim no longer counts toward completion.
                consecutive_done = 0
                last_done_author = None
            round_limit += extra_rounds
            if stopped_by_human:
                break

        round_number += 1

    console.banner("Bridge stopped" if stopped_by_human else "Round limit reached")
    if last_status in {"CHALLENGE", "BLOCKED"}:
        console.warning(
            f"The final handoff ended {last_status} and was never verified; "
            "exit code 0 means the rounds ran out, not that the work passed."
        )
    console.info(f"Agent runs: {agent_runs}")
    console.info(f"Transcript: {transcript.path}")
    if not stopped_by_human:
        console.info(
            f'Continue later: & "{BRIDGE_ROOT / "bridge.ps1"}" '
            f"--resume latest --rounds {config.rounds}"
        )
    return 0


def cli_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"version check failed: {exc}"
    return (result.stdout or result.stderr).strip() or f"exit {result.returncode}"


def auth_ready(executable: str, arguments: Sequence[str]) -> bool:
    """Check auth without echoing account metadata or secrets to the transcript."""
    try:
        result = subprocess.run(
            [executable, *arguments],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Codex GPT-5.6 Sol and Claude Fable 5 as a local coding pair.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("prompt", nargs="*", help="The project goal or human interjection")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Shared project working directory (default: cwd; on --resume, the recorded workspace)",
    )
    parser.add_argument("--rounds", type=positive_int, default=2, help="Initial hard round cap")
    parser.add_argument(
        "--lead",
        choices=("codex", "claude"),
        default="codex",
        help="Which agent takes the first turn of every round",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=5_400,
        help="Per-agent timeout in seconds (ultra/max effort turns can exceed an hour)",
    )
    parser.add_argument(
        "--context-chars",
        type=positive_int,
        default=32_000,
        help="Recent transcript characters sent to each agent",
    )
    parser.add_argument(
        "--memory-chars",
        type=positive_int,
        default=8_000,
        help="Cap on the shared memory doc the agents maintain between rounds",
    )
    parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable the shared memory doc for this run",
    )
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL)
    parser.add_argument("--claude-model", default=DEFAULT_CLAUDE_MODEL)
    parser.add_argument(
        "--codex-sandbox",
        choices=("read-only", "workspace-write", "danger-full-access"),
        default="workspace-write",
    )
    parser.add_argument(
        "--codex-effort",
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        default="ultra",
        help="Codex reasoning effort (overrides config.toml for this run)",
    )
    parser.add_argument(
        "--claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="max",
        help="Claude effort level (max is Fable 5's ceiling; there is no ultra)",
    )
    parser.add_argument(
        "--claude-permission-mode",
        choices=("acceptEdits", "auto", "dontAsk", "manual", "plan"),
        default="auto",
    )
    parser.add_argument(
        "--claude-max-budget-usd",
        type=positive_float,
        help="Optional Claude CLI budget cap when API billing supports it",
    )
    parser.add_argument(
        "--resume",
        metavar="PATH|latest",
        help="Continue an existing local transcript",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Run to the round cap without human checkpoints",
    )
    parser.add_argument(
        "--allow-non-git",
        action="store_true",
        help="Skip the guardrail requiring the workspace to be inside a git repository",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check local CLI discovery and auth without invoking either model",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show commands without invoking either model or writing a transcript",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return " ".join(args.prompt).strip()
    if args.resume:
        return ""
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return input("What should Codex and Claude/Fable accomplish?\n> ").strip()


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    console = Console()

    try:
        codex_executable = resolve_executable(args.codex_command)
        claude_executable = resolve_executable(args.claude_command)
        agents = build_agents(args, codex_executable, claude_executable)

        transcript: Transcript | None = None
        if args.resume:
            transcript = Transcript.load(resolve_resume_path(BRIDGE_ROOT, args.resume))
        workspace = resolve_workspace(args.workspace, transcript)
        memory_path = None if args.no_memory else memory_path_for(BRIDGE_ROOT, workspace)

        if args.check:
            console.banner("Local CLI check")
            console.line(f"Codex:        {codex_executable}")
            console.line(f"              {cli_version(codex_executable)}")
            console.line(
                "Codex auth:   "
                + ("ready" if auth_ready(codex_executable, ["login", "status"]) else "not ready")
            )
            console.line(f"Claude/Fable: {claude_executable}")
            console.line(f"              {cli_version(claude_executable)}")
            console.line(
                "Claude auth:  "
                + ("ready" if auth_ready(claude_executable, ["auth", "status"]) else "not ready")
            )
            console.line(f"Workspace:    {workspace}")
            console.line(f"Memory:       {memory_path or '(disabled)'}")
            return 0

        prompt = read_prompt(args)

        if args.dry_run:
            console.banner("Dry run")
            for agent in agents:
                console.line(f"{agent.name + ':':<14}{command_preview(agent.command(workspace))}")
            console.line(f"Workspace:    {workspace}")
            console.line(f"Prompt chars: {len(prompt)}")
            return 0

        if not args.allow_non_git:
            ensure_git_workspace(workspace)

        if transcript is not None:
            if prompt:
                transcript.append("human", "Human", prompt)
        else:
            if not prompt:
                raise BridgeError("A non-empty prompt is required for a new session.")
            transcript = Transcript.create(
                BRIDGE_ROOT,
                prompt,
                {
                    "workspace": str(workspace),
                    "codex_model": args.codex_model,
                    "claude_model": args.claude_model,
                    "codex_effort": args.codex_effort,
                    "claude_effort": args.claude_effort,
                    "lead": args.lead,
                    "timeout_seconds": args.timeout,
                },
            )

        config = BridgeConfig(
            workspace=workspace,
            rounds=args.rounds,
            timeout_seconds=args.timeout,
            context_chars=args.context_chars,
            pause=not args.no_pause and sys.stdin.isatty(),
            memory_path=memory_path,
            memory_chars=args.memory_chars,
        )
        return run_bridge(transcript, agents, config, console)
    except BridgeError as exc:
        console.error(f"Bridge error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
