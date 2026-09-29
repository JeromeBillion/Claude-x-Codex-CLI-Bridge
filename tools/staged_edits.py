"""Host-owned, per-edit apply gate for read-only Codex turns.

The model can propose exact text replacements. Only the desktop host writes files,
after showing a diff and receiving a separate user decision for each proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile


MAX_EDITS = 20
MAX_TEXT = 256_000
_FENCE = re.compile(r"```codex-edits\s*\n(.*?)\n```", re.DOTALL)
_WINDOWS_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


class EditProposalError(ValueError):
    pass


def _target(workspace: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise EditProposalError("Edit path must be a relative POSIX path")
    parts = PurePosixPath(relative).parts
    if any(part in {"", ".", ".."} or part.lower() == ".git" or _WINDOWS_DEVICE.match(part)
           or part.endswith((".", " ")) or any(ord(c) < 32 or c in '<>"|?*' for c in part) for part in parts):
        raise EditProposalError("Edit path contains an unsafe component")
    if relative.startswith("/") or parts[0].lower() == ".git":
        raise EditProposalError("Edit path is outside the editable project")
    root = workspace.resolve(strict=True)
    if not root.is_dir():
        raise EditProposalError("Workspace is not a directory")
    target = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        # Windows reparse points and POSIX symlinks must not redirect writes.
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise EditProposalError("Edit path passes through a link")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as exc:
        raise EditProposalError("Edit parent must already exist inside the project") from exc
    if parent != target.parent or not parent.is_dir():
        raise EditProposalError("Edit parent must already exist inside the project")
    if not target.resolve(strict=False).is_relative_to(root):
        raise EditProposalError("Edit path escapes the project")
    return target


def _read(target: Path) -> str | None:
    if not target.exists():
        return None
    if not target.is_file() or target.is_symlink():
        raise EditProposalError("Edit target is not a regular file")
    if target.stat().st_size > MAX_TEXT:
        raise EditProposalError("Edit target is too large for exact review")
    try:
        with target.open("r", encoding="utf-8", newline="") as handle:
            return handle.read()
    except (UnicodeError, OSError) as exc:
        raise EditProposalError("Edit target must be readable UTF-8 text") from exc


@dataclass(frozen=True)
class StagedEdit:
    workspace: Path
    relative: str
    before: str | None
    after: str | None
    diff: str

    def apply(self) -> None:
        """Recheck the entire file just before applying an approved edit."""
        target = _target(self.workspace, self.relative)
        if _read(target) != self.before:
            raise EditProposalError("File changed since review; proposal was not applied")
        try:
            if self.after is None:
                target.unlink()
                return
            # Build the whole replacement before touching the target. A hard link
            # gives creation an exclusive destination; replace is atomic for an
            # existing file on the same volume.
            descriptor, temporary = tempfile.mkstemp(prefix=".codex-edit-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                    handle.write(self.after)
                if _read(target) != self.before:
                    raise EditProposalError("File changed since review; proposal was not applied")
                if self.before is None:
                    os.link(temporary, target)
                else:
                    os.chmod(temporary, target.stat().st_mode)
                    os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        except FileExistsError as exc:
            raise EditProposalError("File appeared since review") from exc
        except OSError as exc:
            raise EditProposalError("Filesystem refused the approved edit") from exc


def stage_proposals(workspace: Path, response: str) -> list[StagedEdit]:
    """Return proposals from one explicit codex-edits fence, or an empty list."""
    matches = _FENCE.findall(response)
    if not matches:
        return []
    if len(matches) != 1 or len(matches[0]) > MAX_TEXT * MAX_EDITS:
        raise EditProposalError("Expected one bounded codex-edits block")
    try:
        raw = json.loads(matches[0])
    except json.JSONDecodeError as exc:
        raise EditProposalError("Edit proposal is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"edits"} or not isinstance(raw["edits"], list):
        raise EditProposalError("Edit proposal needs an edits list")
    if not 1 <= len(raw["edits"]) <= MAX_EDITS:
        raise EditProposalError("Edit proposal count is outside the supported range")
    staged: list[StagedEdit] = []
    seen: set[str] = set()
    for row in raw["edits"]:
        if not isinstance(row, dict) or set(row) != {"path", "old_text", "new_text"}:
            raise EditProposalError("Each edit needs path, old_text and new_text")
        relative, old, new = row["path"], row["old_text"], row["new_text"]
        target = _target(workspace, relative)
        key = str(target).casefold()
        if key in seen:
            raise EditProposalError("Only one edit per file is supported in a turn")
        seen.add(key)
        if old is not None and (not isinstance(old, str) or not old or len(old) > MAX_TEXT):
            raise EditProposalError("old_text must be nonempty text or null for creation")
        if new is not None and (not isinstance(new, str) or len(new) > MAX_TEXT):
            raise EditProposalError("new_text must be text or null for deletion")
        if old is None and new is None:
            raise EditProposalError("Edit has no effect")
        before = _read(target)
        if old is None:
            if before is not None:
                raise EditProposalError("Creation target already exists")
            after = new
        elif before is None or before.count(old) != 1:
            raise EditProposalError("old_text must match exactly once in the current file")
        else:
            after = before.replace(old, new or "", 1)
            if new is None and old != before:
                raise EditProposalError("Deletion must match the whole file")
            if new is None:
                after = None
        if after == before:
            raise EditProposalError("Edit has no effect")
        diff_lines = difflib.unified_diff(
            (before or "").splitlines(keepends=True), (after or "").splitlines(keepends=True),
            fromfile=f"a/{relative}" if before is not None else "/dev/null",
            tofile=f"b/{relative}" if after is not None else "/dev/null",
        )
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in diff_lines)
        if not diff:
            diff = f"Create empty file: {relative}\n"
        staged.append(StagedEdit(workspace.resolve(strict=True), relative, before, after, diff))
    return staged
