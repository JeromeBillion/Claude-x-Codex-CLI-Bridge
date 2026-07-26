from __future__ import annotations

import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

from agent_bridge import (  # noqa: E402
    MEMORY_DIR,
    AgentClient,
    AgentResult,
    BridgeConfig,
    BridgeError,
    ClaudeClient,
    CodexClient,
    Console,
    Transcript,
    build_agents,
    ensure_git_workspace,
    extract_memory,
    extract_status,
    make_parser,
    memory_path_for,
    read_memory,
    resolve_executable,
    resolve_resume_path,
    resolve_workspace,
    run_bridge,
    seed_completion_state,
    write_memory,
)


class Cp1252Stream:
    encoding = "cp1252"

    def __init__(self) -> None:
        self.value = ""

    def isatty(self) -> bool:
        return False

    def write(self, value: str) -> int:
        value.encode(self.encoding)
        self.value += value
        return len(value)

    def flush(self) -> None:
        pass


class StatusTests(unittest.TestCase):
    def test_extracts_last_status_case_insensitively(self) -> None:
        output = "BRIDGE_STATUS: CONTINUE\nwork\nbridge_status: done\n"
        self.assertEqual(extract_status(output), "DONE")

    def test_missing_status_defaults_to_continue(self) -> None:
        self.assertEqual(extract_status("No explicit status"), "CONTINUE")

    def test_extracts_challenge_status(self) -> None:
        output = "Refuted the ledger fix; test still fails.\nBRIDGE_STATUS: CHALLENGE\n"
        self.assertEqual(extract_status(output), "CHALLENGE")


class ConsoleTests(unittest.TestCase):
    def test_unrepresentable_model_output_does_not_crash_cp1252_console(self) -> None:
        stream = Cp1252Stream()
        console = Console(stream)

        console.line("Result: ∑")

        self.assertEqual(stream.value, "Result: ?\n")


class CommandTests(unittest.TestCase):
    def test_codex_falls_back_to_configured_cli_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_home = Path(temp_dir)
            fake_executable = fake_home / "OpenAI" / "rotated-hash" / "codex.exe"
            fake_executable.parent.mkdir(parents=True)
            fake_executable.touch()
            config_path = fake_home / ".codex" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text(
                "[mcp_servers.node_repl.env]\n"
                f"CODEX_CLI_PATH = '{fake_executable}'\n",
                encoding="utf-8",
            )

            with patch("agent_bridge.shutil.which", return_value=None):
                resolved = resolve_executable("codex", home=fake_home)

            self.assertEqual(resolved, str(fake_executable.resolve()))

    def test_codex_command_uses_stdin_ephemeral_mode_and_workspace_sandbox(self) -> None:
        client = CodexClient("codex", "gpt-5.6-sol", "workspace-write")
        command = client.command(Path("C:/work").resolve(), Path("last.txt"))
        self.assertEqual(command[-1], "-")
        self.assertIn("--ephemeral", command)
        self.assertEqual(command[command.index("--model") + 1], "gpt-5.6-sol")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("-c", command)

    def test_codex_command_passes_reasoning_effort_override(self) -> None:
        client = CodexClient("codex", "gpt-5.6-sol", "workspace-write", "ultra")
        command = client.command(Path("C:/work").resolve(), Path("last.txt"))
        self.assertEqual(command[command.index("-c") + 1], "model_reasoning_effort=ultra")
        self.assertEqual(command[-1], "-")

    def test_claude_command_uses_fable_and_optional_budget_cap(self) -> None:
        client = ClaudeClient("claude", "claude-fable-5", "auto", 1.25)
        command = client.command(Path("."))
        self.assertEqual(command[command.index("--model") + 1], "claude-fable-5")
        self.assertEqual(command[command.index("--permission-mode") + 1], "auto")
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[command.index("--max-budget-usd") + 1], "1.25")
        self.assertNotIn("--effort", command)

    def test_claude_command_passes_effort_level(self) -> None:
        client = ClaudeClient("claude", "claude-fable-5", "auto", None, "max")
        command = client.command(Path("."))
        self.assertEqual(command[command.index("--effort") + 1], "max")
        self.assertNotIn("--max-budget-usd", command)


class TranscriptTests(unittest.TestCase):
    def test_transcript_state_stays_under_bridge_root_not_target_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            bridge_root = temp_root / "bridge"
            target_workspace = temp_root / "target-project"
            target_workspace.mkdir()

            transcript = Transcript.create(
                bridge_root,
                "Build target",
                {"workspace": str(target_workspace)},
            )

            self.assertTrue(transcript.path.is_relative_to(bridge_root))
            self.assertFalse(transcript.path.is_relative_to(target_workspace))

    def test_transcript_round_trip_and_latest_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Build it", {"test": True})
            transcript.append("agent", "Codex", "First result")

            loaded = Transcript.load(transcript.path)

            self.assertEqual(loaded.goal, "Build it")
            self.assertIn("First result", loaded.render(2_000))
            self.assertEqual(resolve_resume_path(workspace, "latest"), transcript.path.resolve())

    def test_render_keeps_recent_content_within_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "Initial", {})
            transcript.append("agent", "Codex", "old-" + ("x" * 100))
            transcript.append("agent", "Claude/Fable", "new-result")

            rendered = transcript.render(40)

            self.assertIn("new-result", rendered)
            self.assertNotIn("old-", rendered)
            self.assertLessEqual(len(rendered), 40)


class FakeAgent:
    role = "Test the bridge"
    RAISE = "RAISE"

    def __init__(self, name: str, *outputs: str) -> None:
        self.name = name
        self.outputs = list(outputs)
        self.calls = 0
        self.prompts: list[str] = []

    def invoke(self, prompt: str, workspace: Path, timeout_seconds: int) -> AgentResult:
        del workspace, timeout_seconds
        if "GOAL\nComplete task" not in prompt:
            raise AssertionError("missing goal")
        self.prompts.append(prompt)
        output = self.outputs[min(self.calls, len(self.outputs) - 1)]
        self.calls += 1
        if output == self.RAISE:
            raise BridgeError("simulated CLI failure")
        return AgentResult(output, 0.01)


class BridgeLoopTests(unittest.TestCase):
    def test_stops_when_both_agents_independently_report_done(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            agents = [
                FakeAgent("Codex", "Implemented and tested.\nBRIDGE_STATUS: DONE"),
                FakeAgent("Claude/Fable", "Reviewed and verified.\nBRIDGE_STATUS: DONE"),
            ]
            output = io.StringIO()
            config = BridgeConfig(workspace, 4, 30, 2_000, False)

            result = run_bridge(transcript, agents, config, Console(output))

            self.assertEqual(result, 0)
            self.assertEqual([agent.calls for agent in agents], [1, 1])
            agent_events = [event for event in transcript.events if event["type"] == "agent"]
            self.assertEqual(len(agent_events), 2)

    def test_done_claim_triggers_adversarial_verification_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent("Codex", "Shipped it.\nBRIDGE_STATUS: DONE")
            claude = FakeAgent("Claude/Fable", "Held up.\nBRIDGE_STATUS: DONE")
            config = BridgeConfig(workspace, 4, 30, 8_000, False)
            output = io.StringIO()

            run_bridge(transcript, [codex, claude], config, Console(output))

            self.assertNotIn("VERIFICATION TURN", codex.prompts[0])
            self.assertIn("VERIFICATION TURN", claude.prompts[0])
            self.assertIn("Codex claims the goal is complete", claude.prompts[0])
            self.assertIn("adversarially verifying", output.getvalue())

    def test_done_claim_survives_the_round_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent(
                "Codex",
                "Working.\nBRIDGE_STATUS: CONTINUE",
                "Attacked it and it held.\nBRIDGE_STATUS: DONE",
            )
            claude = FakeAgent("Claude/Fable", "Shipped it.\nBRIDGE_STATUS: DONE")
            config = BridgeConfig(workspace, 2, 30, 8_000, False)

            result = run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            # Claude's round-1 DONE must still demand verification from Codex
            # at the top of round 2, and Codex's confirming DONE ends the run.
            self.assertEqual(result, 0)
            self.assertIn("VERIFICATION TURN", codex.prompts[1])
            self.assertIn("Claude/Fable claims the goal is complete", codex.prompts[1])
            self.assertEqual([codex.calls, claude.calls], [2, 1])

    def test_challenge_voids_the_done_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            agents = [
                FakeAgent("Codex", "Shipped it.\nBRIDGE_STATUS: DONE"),
                FakeAgent(
                    "Claude/Fable",
                    "Refuted: the test suite fails.\nBRIDGE_STATUS: CHALLENGE",
                ),
            ]
            config = BridgeConfig(workspace, 2, 30, 8_000, False)
            output = io.StringIO()

            result = run_bridge(transcript, agents, config, Console(output))

            self.assertEqual(result, 0)
            self.assertEqual([agent.calls for agent in agents], [2, 2])
            challenge_events = [
                event
                for event in transcript.events
                if event["type"] == "agent" and event["author"] == "Claude/Fable"
            ]
            self.assertTrue(challenge_events)
            for event in challenge_events:
                self.assertEqual(event["metadata"]["status"], "CHALLENGE")
            self.assertIn("challenged the last handoff", output.getvalue())

    def test_challenge_keeps_a_voided_claim_from_completing_later(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent(
                "Codex",
                "Shipped it.\nBRIDGE_STATUS: DONE",
                "Stuck.\nBRIDGE_STATUS: BLOCKED",
                "Stuck.\nBRIDGE_STATUS: BLOCKED",
            )
            claude = FakeAgent(
                "Claude/Fable",
                "Refuted it.\nBRIDGE_STATUS: CHALLENGE",
                "My side is done.\nBRIDGE_STATUS: DONE",
                "Waiting.\nBRIDGE_STATUS: CONTINUE",
            )
            config = BridgeConfig(workspace, 3, 30, 8_000, False)

            result = run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            # If CHALLENGE/BLOCKED left Codex's round-1 claim standing, Claude's
            # round-2 DONE would wrongly complete the pair off a refuted claim.
            self.assertEqual(result, 0)
            self.assertEqual([codex.calls, claude.calls], [3, 3])

    def test_error_voids_a_standing_done_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent("Codex", "Shipped it.\nBRIDGE_STATUS: DONE", FakeAgent.RAISE)
            claude = FakeAgent("Claude/Fable", FakeAgent.RAISE, "Done.\nBRIDGE_STATUS: DONE")
            config = BridgeConfig(workspace, 2, 30, 8_000, False)
            output = io.StringIO()

            result = run_bridge(transcript, [codex, claude], config, Console(output))

            # Codex's round-1 claim died with the failed turns, so Claude's
            # round-2 turn is a fresh claim, not a verification that completes.
            self.assertEqual(result, 0)
            self.assertNotIn("VERIFICATION TURN", claude.prompts[1])
            self.assertNotIn("survived", output.getvalue())

    def test_seeded_done_claim_gives_the_other_agent_a_verification_turn(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            transcript.append(
                "agent",
                "Claude/Fable",
                "Shipped it.\nBRIDGE_STATUS: DONE",
                {"status": "DONE"},
            )
            codex = FakeAgent("Codex", "Attacked it and it held.\nBRIDGE_STATUS: DONE")
            claude = FakeAgent("Claude/Fable", "Should not run.\nBRIDGE_STATUS: CONTINUE")
            config = BridgeConfig(workspace, 2, 30, 8_000, False)

            result = run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            # The resumed claim alone must make Codex's first turn adversarial,
            # and Codex's confirming DONE completes the pair without re-running
            # the original claimant.
            self.assertEqual(result, 0)
            self.assertIn("VERIFICATION TURN", codex.prompts[0])
            self.assertIn("Claude/Fable claims the goal is complete", codex.prompts[0])
            self.assertEqual([codex.calls, claude.calls], [1, 0])

    def test_seeded_done_claim_cannot_be_self_certified_after_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            transcript.append(
                "agent", "Codex", "Shipped it.\nBRIDGE_STATUS: DONE", {"status": "DONE"}
            )
            codex = FakeAgent("Codex", "Still done.\nBRIDGE_STATUS: DONE")
            claude = FakeAgent("Claude/Fable", "Not verified yet.\nBRIDGE_STATUS: CONTINUE")
            config = BridgeConfig(workspace, 1, 30, 8_000, False)

            run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            self.assertEqual([codex.calls, claude.calls], [1, 1])
            self.assertNotIn("VERIFICATION TURN", codex.prompts[0])
            self.assertIn("VERIFICATION TURN", claude.prompts[0])

    def test_human_interjection_resets_the_done_streak(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent(
                "Codex",
                "Working.\nBRIDGE_STATUS: CONTINUE",
                "Now done.\nBRIDGE_STATUS: DONE",
            )
            claude = FakeAgent("Claude/Fable", "Verified.\nBRIDGE_STATUS: DONE")
            config = BridgeConfig(workspace, 2, 30, 8_000, True)

            with patch("builtins.input", side_effect=["Also cover the refund path"]):
                result = run_bridge(
                    transcript, [codex, claude], config, Console(io.StringIO())
                )

            # Claude's round-1 DONE predates the interjection, so Codex's
            # round-2 DONE must not complete the pair; Claude must re-verify.
            self.assertEqual(result, 0)
            self.assertEqual([codex.calls, claude.calls], [2, 2])


class MemoryTests(unittest.TestCase):
    def test_extracts_last_memory_block_and_defaults_to_none(self) -> None:
        output = (
            "BRIDGE_MEMORY_BEGIN\nstale\nBRIDGE_MEMORY_END\n"
            "work notes\n"
            "BRIDGE_MEMORY_BEGIN\n- fact A\n- fact B\nBRIDGE_MEMORY_END\n"
            "BRIDGE_STATUS: CONTINUE\n"
        )
        self.assertEqual(extract_memory(output), "- fact A\n- fact B")
        self.assertIsNone(extract_memory("no block here"))

    def test_memory_path_is_stable_per_workspace_and_stays_under_bridge_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace_a = root / "project-a"
            workspace_b = root / "project-b"
            self.assertEqual(
                memory_path_for(root, workspace_a), memory_path_for(root, workspace_a)
            )
            self.assertNotEqual(
                memory_path_for(root, workspace_a), memory_path_for(root, workspace_b)
            )
            self.assertTrue(
                memory_path_for(root, workspace_a).is_relative_to(root / MEMORY_DIR)
            )

    def test_write_memory_enforces_the_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "memory.md"
            stored = write_memory(path, "x" * 200, 80)
            self.assertLessEqual(len(stored), 80)
            self.assertIn("[TRUNCATED AT MEMORY CAP]", stored)
            self.assertEqual(read_memory(path), stored)

    def test_render_strips_memory_blocks_from_conversation_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            transcript.append(
                "agent",
                "Codex",
                "did work\nBRIDGE_MEMORY_BEGIN\nmemory-only-token\nBRIDGE_MEMORY_END\n"
                "BRIDGE_STATUS: CONTINUE",
            )
            rendered = transcript.render(4_000)
            self.assertNotIn("memory-only-token", rendered)
            self.assertIn("[shared memory updated]", rendered)
            self.assertIn("did work", rendered)

    def test_memory_flows_to_the_next_agent_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            memory_path = workspace / "memory" / "shared.md"
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent(
                "Codex",
                "Working.\nBRIDGE_MEMORY_BEGIN\n- codex fact v1\nBRIDGE_MEMORY_END\n"
                "BRIDGE_STATUS: CONTINUE",
            )
            claude = FakeAgent(
                "Claude/Fable",
                "Merging.\nBRIDGE_MEMORY_BEGIN\n- codex fact v1\n- fable fact v2\n"
                "BRIDGE_MEMORY_END\nBRIDGE_STATUS: CONTINUE",
            )
            config = BridgeConfig(
                workspace, 1, 30, 8_000, False, memory_path=memory_path
            )

            run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            self.assertIn("SHARED MEMORY", codex.prompts[0])
            self.assertIn("(empty - seed it this turn)", codex.prompts[0])
            self.assertIn("- codex fact v1", claude.prompts[0])
            self.assertEqual(
                read_memory(memory_path), "- codex fact v1\n- fable fact v2"
            )
            agent_events = [e for e in transcript.events if e["type"] == "agent"]
            self.assertTrue(all(e["metadata"]["memory_updated"] for e in agent_events))

    def test_disabled_memory_never_injects_or_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            codex = FakeAgent(
                "Codex",
                "Working.\nBRIDGE_MEMORY_BEGIN\nignored\nBRIDGE_MEMORY_END\n"
                "BRIDGE_STATUS: CONTINUE",
            )
            claude = FakeAgent("Claude/Fable", "Fine.\nBRIDGE_STATUS: CONTINUE")
            config = BridgeConfig(workspace, 1, 30, 8_000, False, memory_path=None)

            run_bridge(transcript, [codex, claude], config, Console(io.StringIO()))

            self.assertNotIn("SHARED MEMORY", codex.prompts[0])
            self.assertNotIn("BRIDGE_MEMORY_BEGIN", claude.prompts[0])
            self.assertFalse(list(workspace.glob("**/*.md")))


class SeedCompletionStateTests(unittest.TestCase):
    def test_trailing_done_claim_is_carried_into_the_resumed_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            transcript.append("agent", "Codex", "done", {"status": "DONE"})
            self.assertEqual(seed_completion_state(transcript), (1, "Codex"))

    def test_later_human_instruction_supersedes_the_done_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            transcript.append("agent", "Codex", "done", {"status": "DONE"})
            transcript.append("human", "Human", "now do more")
            self.assertEqual(seed_completion_state(transcript), (0, None))

    def test_trailing_error_supersedes_the_done_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            transcript.append("agent", "Codex", "done", {"status": "DONE"})
            transcript.append("error", "Claude/Fable", "timed out", {"round": 1})
            self.assertEqual(seed_completion_state(transcript), (0, None))

    def test_non_done_trailing_turn_seeds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            transcript.append("agent", "Codex", "work", {"status": "CONTINUE"})
            self.assertEqual(seed_completion_state(transcript), (0, None))

    def test_fresh_session_seeds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            transcript = Transcript.create(Path(temp_dir), "goal", {})
            self.assertEqual(seed_completion_state(transcript), (0, None))


class TimeoutTests(unittest.TestCase):
    def test_timeout_kills_process_tree_and_salvages_partial_output(self) -> None:
        command = [
            sys.executable,
            "-u",
            "-c",
            "print('PARTIAL-MARKER'); import time; time.sleep(60)",
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(BridgeError) as ctx:
                AgentClient._execute(command, "", Path(temp_dir), 2)
        message = str(ctx.exception)
        self.assertIn("timed out", message)
        self.assertIn("PARTIAL-MARKER", message)


class LeadTests(unittest.TestCase):
    def test_default_lead_keeps_codex_first(self) -> None:
        args = make_parser().parse_args([])
        agents = build_agents(args, "codex", "claude")
        self.assertIsInstance(agents[0], CodexClient)

    def test_lead_claude_puts_fable_first(self) -> None:
        args = make_parser().parse_args(["--lead", "claude"])
        agents = build_agents(args, "codex", "claude")
        self.assertIsInstance(agents[0], ClaudeClient)
        self.assertIsInstance(agents[1], CodexClient)

    def test_agents_are_equal_co_authors_regardless_of_order(self) -> None:
        for lead in ("codex", "claude"):
            args = make_parser().parse_args(["--lead", lead])
            agents = build_agents(args, "codex", "claude")
            for agent, partner in ((agents[0], agents[1]), (agents[1], agents[0])):
                self.assertIn("equal co-author", agent.role)
                self.assertIn(partner.name, agent.role)
                self.assertIn("Neither of you outranks the other", agent.role)
            self.assertNotIn("primary builder", agents[0].role + agents[1].role)

    def test_roles_demand_mutual_accountability(self) -> None:
        args = make_parser().parse_args([])
        for agent in build_agents(args, "codex", "claude"):
            self.assertIn("audit every handoff", agent.role)
            self.assertIn("refute", agent.role)


class WorkspaceTests(unittest.TestCase):
    def test_resume_restores_recorded_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorded = Path(temp_dir) / "project"
            recorded.mkdir()
            transcript = Transcript.create(
                Path(temp_dir), "goal", {"workspace": str(recorded)}
            )
            self.assertEqual(resolve_workspace(None, transcript), recorded.resolve())

    def test_explicit_workspace_beats_recorded_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorded = Path(temp_dir) / "recorded"
            explicit = Path(temp_dir) / "explicit"
            recorded.mkdir()
            explicit.mkdir()
            transcript = Transcript.create(
                Path(temp_dir), "goal", {"workspace": str(recorded)}
            )
            self.assertEqual(
                resolve_workspace(str(explicit), transcript), explicit.resolve()
            )

    def test_non_git_workspace_rejected_and_git_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "repo"
            workspace.mkdir()
            with self.assertRaises(BridgeError):
                ensure_git_workspace(workspace)
            (workspace / ".git").mkdir()
            ensure_git_workspace(workspace)  # must not raise


class FailingAgent:
    role = "Fail for the test"

    def __init__(self, name: str) -> None:
        self.name = name

    def invoke(self, prompt: str, workspace: Path, timeout_seconds: int) -> AgentResult:
        raise BridgeError("simulated CLI failure")


class BothFailedTests(unittest.TestCase):
    def test_unattended_run_stops_when_both_agents_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            transcript = Transcript.create(workspace, "Complete task", {})
            agents = [FailingAgent("Codex"), FailingAgent("Claude/Fable")]
            config = BridgeConfig(workspace, 4, 30, 2_000, False)

            result = run_bridge(transcript, agents, config, Console(io.StringIO()))

            self.assertEqual(result, 1)
            errors = [e for e in transcript.events if e["type"] == "error"]
            self.assertEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
