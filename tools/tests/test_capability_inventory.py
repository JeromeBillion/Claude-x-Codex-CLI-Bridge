"""No-turn capability inventory and visibility boundaries."""

import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tools.capability_inventory import CapabilityRow, claude_capabilities, codex_capabilities


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, params=None):
        self.calls.append((method, params))
        if method == "skills/list":
            return {"data": [{"skills": [{"name": "local-skill", "enabled": True,
                                            "path": "C:/private/location"}]}]}
        if method == "mcpServerStatus/list":
            if params.get("cursor") == "page2":
                return {"data": [{"name": "server-two", "authStatus": "notLoggedIn", "tools": []}],
                        "nextCursor": None}
            return {"data": [{"name": "server-one", "authStatus": "bearerToken",
                              "tools": [{"name": "call"}]}], "nextCursor": "page2"}
        if method == "app/installed":
            return {"apps": [{"runtimeName": "local-app", "enabled": True, "callable": True,
                              "id": "private-id"}]}
        raise AssertionError("Model turn or unsupported request: " + method)


class CapabilityInventoryTests(unittest.TestCase):
    def test_codex_inventory_pages_without_a_turn_or_private_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Runtime:
                account = {"type": "chatgpt"}
                transport = FakeTransport()
            rows = codex_capabilities(Runtime(), Path(tmp))
        self.assertEqual([r.kind for r in rows], ["skill", "MCP server", "MCP server", "installed app"])
        self.assertEqual(rows[2].state, "needs sign-in")
        self.assertEqual(rows[3].state, "advertised callable")
        self.assertEqual([m for m, _ in Runtime.transport.calls],
                         ["skills/list", "mcpServerStatus/list", "mcpServerStatus/list", "app/installed"])
        self.assertNotIn("private", repr(rows))

    def test_claude_inventory_lists_plugins_and_health_without_chat(self):
        workspace = Path.cwd()
        def command(_executable, args, command_workspace):
            self.assertEqual(command_workspace, workspace)
            if tuple(args) == ("plugin", "list", "--json"):
                return 0, '[{"id":"review-plugin","enabled":true,"installPath":"C:/private"}]'
            if tuple(args) == ("mcp", "list"):
                return 0, "Checking...\nalpha: ✓ Connected\nbeta: ✗ Failed\ngamma: ⏸ Pending approval\n"
            raise AssertionError("Unexpected Claude command")
        with patch("tools.capability_inventory._claude_command", side_effect=command) as called:
            rows = claude_capabilities(["claude"], workspace)
        self.assertEqual(called.call_count, 2)
        self.assertEqual([r.state for r in rows], ["enabled", "connected", "failed", "pending approval"])
        self.assertNotIn("private", repr(rows))

    def test_unavailable_family_does_not_leak_raw_error(self):
        with patch("tools.capability_inventory._claude_command", side_effect=RuntimeError("secret-token")):
            rows = claude_capabilities(["claude"], Path.cwd())
        self.assertEqual(rows, [CapabilityRow("Claude", "plugin", "inventory", "unavailable"),
                                CapabilityRow("Claude", "MCP server", "inventory", "unavailable")])
        self.assertNotIn("secret-token", repr(rows))

    def test_claude_health_check_runs_in_selected_workspace(self):
        child = Mock()
        child.communicate.return_value = ("[]", "")
        child.returncode = 0
        with patch("tools.capability_inventory.subprocess.Popen", return_value=child) as popen:
            from tools.capability_inventory import _claude_command
            self.assertEqual(_claude_command(["claude"], ["plugin", "list", "--json"], Path.cwd()), (0, "[]"))
        self.assertEqual(popen.call_args.kwargs["cwd"], str(Path.cwd().resolve()))

    def test_desktop_requires_claude_workspace_trust_before_health_check(self):
        from tools.desktop import DesktopHost
        host = DesktopHost.__new__(DesktopHost)
        host.workspace = Path.cwd()
        host.mode = Mock()
        host.mode.get.return_value = "claude_only"
        host.claude_preflight = Mock()
        host.claude_trust = Mock()
        host.claude_trust.is_trusted.return_value = False
        host.status = Mock()
        host._work = Mock()
        with patch("tools.desktop.messagebox.askyesno", return_value=False) as asked:
            host._inspect_capabilities()
        asked.assert_called_once()
        host.claude_trust.trust.assert_not_called()
        host._work.assert_not_called()
        with patch("tools.desktop.messagebox.askyesno", return_value=True):
            host._inspect_capabilities()
        host.claude_trust.trust.assert_called_once_with(host.workspace)
        host._work.assert_called_once()


if __name__ == "__main__":
    unittest.main()
