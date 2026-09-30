"""Build a source ZIP for the personal Windows alpha; no model calls or bundler.

Run from a clean checkout: py -3 -m tools.package_windows_alpha
The archive requires Python 3.11+ and separately installed provider CLIs.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import zipfile


ARCHIVE_ROOT = "ClaudeCodexDesktop-alpha"
MAX_FILE_SIZE = 5_000_000


def files_for_archive(source: Path) -> list[Path]:
    for name in ("docs", "tools", ".githooks"):
        directory = source / name
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)() \
                or (directory.exists() and not directory.resolve(strict=True).is_relative_to(source)):
            raise ValueError("Alpha source directory is linked outside the checkout")
    tracked_output = subprocess.run(["git", "ls-files", "-z"], cwd=source,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    check=True).stdout
    tracked = {entry.decode("utf-8") for entry in tracked_output.split(b"\0") if entry}
    required = [source / name for name in ("README.md", "LICENSE", "bridge.ps1", "launch-desktop.cmd")]
    if not all(path.is_file() and path.relative_to(source).as_posix() in tracked for path in required):
        raise ValueError("Required alpha files are missing")
    paths = required + sorted(path for path in (source / "docs").glob("*.md")
                              if path.relative_to(source).as_posix() in tracked)
    paths += sorted(path for path in (source / "tools").glob("*.py")
                    if path.relative_to(source).as_posix() in tracked)
    paths += sorted(path for path in (source / "tools" / "tests").glob("*.py")
                    if path.relative_to(source).as_posix() in tracked)
    if (source / ".githooks").is_dir():
        paths += sorted(path for path in (source / ".githooks").iterdir()
                        if path.is_file() and path.relative_to(source).as_posix() in tracked)
    for path in paths:
        # A file under a linked directory is not itself reported as a symlink.
        # Resolve the whole path before reading it into a distributable archive.
        resolved = path.resolve(strict=True)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)() \
                or not resolved.is_relative_to(source) \
                or not path.is_file() or path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("Alpha file is unsafe or oversized")
    return paths


def build_archive(source: Path, destination: Path, revision: str) -> dict[str, object]:
    """Build and verify an archive with only explicitly selected source paths."""
    source = source.resolve(strict=True)
    if not revision or not all(char in "0123456789abcdef" for char in revision):
        raise ValueError("Revision must be a Git hex SHA")
    paths = files_for_archive(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {"revision": revision, "files": {}}
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative = path.relative_to(source).as_posix()
            data = path.read_bytes()
            manifest["files"][relative] = hashlib.sha256(data).hexdigest()
            archive.writestr(f"{ARCHIVE_ROOT}/{relative}", data)
        archive.writestr(f"{ARCHIVE_ROOT}/manifest.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for relative, digest in manifest["files"].items():
            actual = hashlib.sha256(archive.read(f"{ARCHIVE_ROOT}/{relative}")).hexdigest()
            if actual != digest:
                raise ValueError("ZIP manifest verification failed")
    return manifest


def main() -> None:
    source = Path(__file__).resolve().parents[1]
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                            cwd=source, capture_output=True, text=True, check=True)
    if status.stdout.strip():
        raise SystemExit("Refusing to label an archive from a dirty checkout")
    revision = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=source, capture_output=True, text=True, check=True).stdout.strip()
    destination = source / "dist" / f"ClaudeCodexDesktop-alpha-{revision}.zip"
    manifest = build_archive(source, destination, revision)
    print(f"Built {destination.name} with {len(manifest['files'])} files; ZIP and SHA-256 manifest verified")


if __name__ == "__main__":
    main()
