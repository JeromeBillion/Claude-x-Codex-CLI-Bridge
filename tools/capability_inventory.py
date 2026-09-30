"""On-demand, no-model-turn inventory of installed CLI capabilities.

Names remain in the local UI. This module never persists raw CLI responses or
prints them to a report; advertised tools are not treated as exercised tools.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import subprocess
from typing import Any, Sequence
from pathlib import Path

from tools.claude_runtime_probe import child_env as claude_child_env
from tools.codex_app_server import CodexRuntime


@dataclass(frozen=True)
class CapabilityRow:
    provider: str
    kind: str
    name: str
    state: str
    detail: str = ""


def _name(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    clean = "".join(char for char in value if char.isprintable()).strip()[:120]
    return clean or fallback


def _rows(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def codex_capabilities(runtime: CodexRuntime, workspace: Path) -> list[CapabilityRow]:
    """Inventory three documented read-only App Server families after login."""
    if not runtime.account or runtime.account.get("type") != "chatgpt":
        raise ValueError("Connect a ChatGPT-signed-in Codex CLI first")
    result: list[CapabilityRow] = []
    try:
        response = runtime.transport.request("skills/list", {"cwds": [str(workspace.resolve(strict=True))]})
        for group in _rows(response.get("data")):
            for skill in _rows(group.get("skills")):
                result.append(CapabilityRow("Codex", "skill", _name(skill.get("name"), "unnamed skill"),
                                            "enabled" if skill.get("enabled") is True else "disabled",
                                            "listed by installed CLI; execution unverified"))
    except Exception:
        result.append(CapabilityRow("Codex", "skill", "inventory", "unavailable"))
    try:
        cursor = None
        seen: set[str] = set()
        for _ in range(20):
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = runtime.transport.request("mcpServerStatus/list", params)
            for server in _rows(page.get("data")):
                advertised = len(_rows(server.get("tools")))
                auth = server.get("authStatus")
                state = "needs sign-in" if auth == "notLoggedIn" else "configured"
                result.append(CapabilityRow("Codex", "MCP server", _name(server.get("name"), "unnamed server"),
                                            state, f"{advertised} tools advertised; calls unverified"))
            cursor = page.get("nextCursor")
            if not cursor:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Invalid MCP inventory cursor")
            seen.add(cursor)
        else:
            raise ValueError("MCP inventory exceeded page limit")
    except Exception:
        result.append(CapabilityRow("Codex", "MCP server", "inventory", "incomplete"))
    try:
        response = runtime.transport.request("app/installed")
        for app in _rows(response.get("apps")):
            state = "advertised callable" if app.get("enabled") is True and app.get("callable") is True else "unavailable"
            result.append(CapabilityRow("Codex", "installed app",
                                        _name(app.get("runtimeName") or app.get("id"), "unnamed app"),
                                        state, "tool consent and live call unverified"))
    except Exception:
        result.append(CapabilityRow("Codex", "installed app", "inventory", "unavailable"))
    return result


def _claude_command(executable: Sequence[str], args: Sequence[str], workspace: Path) -> tuple[int, str]:
    workspace = workspace.resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("Capability workspace must be a directory")
    process = subprocess.Popen([*executable, *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, encoding="utf-8", errors="replace", env=claude_child_env(),
                               cwd=str(workspace))
    try:
        stdout, _ = process.communicate(timeout=45)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        else:
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise
    return process.returncode, stdout


_MCP_STATE = re.compile(r"(?:Connected|Failed|Pending approval)\s*$", re.IGNORECASE)


def claude_capabilities(executable: Sequence[str], workspace: Path) -> list[CapabilityRow]:
    """List project plugins and MCP health; no chat/session/model is started."""
    result: list[CapabilityRow] = []
    try:
        code, output = _claude_command(executable, ("plugin", "list", "--json"), workspace)
        plugins = json.loads(output) if code == 0 else None
        if not isinstance(plugins, list):
            raise ValueError("Unexpected Claude plugin inventory")
        for plugin in _rows(plugins):
            result.append(CapabilityRow("Claude", "plugin", _name(plugin.get("id"), "unnamed plugin"),
                                        "enabled" if plugin.get("enabled") is True else "disabled",
                                        "installed locally; tool calls unverified"))
    except Exception:
        result.append(CapabilityRow("Claude", "plugin", "inventory", "unavailable"))
    try:
        _, output = _claude_command(executable, ("mcp", "list"), workspace)
        matched = 0
        for line in output.splitlines():
            state_match = _MCP_STATE.search(line)
            if not state_match:
                continue
            matched += 1
            name = line.split(":", 1)[0]
            result.append(CapabilityRow("Claude", "MCP server", _name(name, "unnamed server"),
                                        state_match.group(0).strip().lower(),
                                        "CLI health check; tool calls unverified"))
        if not matched:
            result.append(CapabilityRow("Claude", "MCP server", "inventory", "unavailable"))
    except Exception:
        result.append(CapabilityRow("Claude", "MCP server", "inventory", "unavailable"))
    return result
