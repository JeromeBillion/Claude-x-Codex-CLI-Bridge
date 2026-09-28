"""Approval-mode toggle shared by the Claude and Codex desktop adapters (VLI-159).

Jerome's locked decision: **Ask for every edit** is the default, and
**Auto-accept in trusted folders** applies only to a folder the user
explicitly trusted for automatic edits. Trust for automatic edits is a
separate, stronger grant than trusting a folder enough to open a session in it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePath
import json
from typing import Any

ASK_EVERY_EDIT = "ask_every_edit"
AUTO_ACCEPT_TRUSTED = "auto_accept_trusted"
APPROVAL_MODES = (ASK_EVERY_EDIT, AUTO_ACCEPT_TRUSTED)

# Claude Code tools that change files. Only these can ever be auto-accepted.
CLAUDE_FILE_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# Passed to every Claude chat process as `ask` rules. Ask rules outrank allow
# rules from any settings file, so a user or project allow rule cannot let an
# edit or command run without reaching the host (docs: /en/permissions). Claude
# Code has no command sandbox on Windows, so shell commands always go to the
# user, in both modes.
CLAUDE_ASK_RULES = (*sorted(CLAUDE_FILE_EDIT_TOOLS), "Bash", "PowerShell")
# Paths that configure what the agent may run next. An edit there is never
# auto-accepted, even in a trusted folder, because it could grant itself hooks,
# MCP servers or permissions.
PROTECTED_PARTS = frozenset({".git", ".claude", ".claude.json", ".codex", ".mcp.json", ".agent-bridge",
                             ".vscode", ".github", ".husky"})
# 8.3 short names (CLAUDE~1) can alias a protected name that does not exist yet.
SHORT_NAME = re.compile(r"~\d")


class TrustedFolderStore:
    """Folders the user explicitly trusted for automatic edits, shared by both providers."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def _key(workspace: Path) -> str:
        return os.path.normcase(str(workspace.resolve(strict=True)))

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_trusted(self, workspace: Path) -> bool:
        try:
            return self._load().get(self._key(workspace)) is True
        except OSError:
            return False

    def trust(self, workspace: Path) -> None:
        """Call only after the user explicitly trusts this exact folder in the UI."""
        data = self._load()
        data[self._key(workspace)] = True
        self._write(data)

    def revoke(self, workspace: Path) -> None:
        data = self._load()
        data.pop(self._key(workspace), None)
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w",
                       encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(temporary, self.path)


def confined_edit_target(raw: Any, root: Path) -> Path | None:
    """Resolve an edit target; None unless it is a file path strictly inside root."""
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        return None
    if any(part == ".." for part in PurePath(raw.replace("\\", "/")).parts):
        return None
    # NTFS streams (".mcp.json::$DATA", ".claude::$INDEX_ALLOCATION") create the
    # protected file under another spelling. A colon is only legal in the drive.
    drive_free = raw[2:] if len(raw) > 1 and raw[1] == ":" else raw
    if ":" in drive_free:
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        real_root = root.resolve(strict=True)
        resolved = candidate.resolve(strict=False)  # follows existing symlinks and junctions
    except (OSError, RuntimeError):
        return None
    if resolved == real_root or not resolved.is_relative_to(real_root):
        return None
    if resolved.exists() and not resolved.is_file():
        return None
    relative = resolved.relative_to(real_root).parts
    for part in relative:
        # Windows drops trailing dots and spaces: ".claude. " is ".claude".
        name = part.rstrip(". ").lower()
        if name in PROTECTED_PARTS or SHORT_NAME.search(part) or ":" in part:
            return None
    return resolved


def claude_auto_accept(tool: Any, tool_input: Any, workspace: Path) -> dict[str, Any] | None:
    """The pinned input to auto-accept a Claude edit in a trusted folder, else None.

    Only file-edit tools qualify, and only when their single target resolves
    inside the workspace. The approved input carries the validated absolute
    path, so the CLI cannot write somewhere other than what was checked.
    """
    if tool not in CLAUDE_FILE_EDIT_TOOLS or not isinstance(tool_input, dict):
        return None
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    target = confined_edit_target(tool_input.get(key), workspace)
    if target is None:
        return None
    return {**tool_input, key: str(target)}


__all__ = [
    "APPROVAL_MODES", "ASK_EVERY_EDIT", "AUTO_ACCEPT_TRUSTED", "CLAUDE_ASK_RULES",
    "CLAUDE_FILE_EDIT_TOOLS", "PROTECTED_PARTS", "TrustedFolderStore", "claude_auto_accept",
    "confined_edit_target",
]
