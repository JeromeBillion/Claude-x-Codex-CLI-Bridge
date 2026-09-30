"""Archive selection and manifest verification for the Windows alpha."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from tools.package_windows_alpha import ARCHIVE_ROOT, build_archive


class WindowsPackageTests(unittest.TestCase):
    def test_archive_contains_only_selected_files_and_verified_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            (root / "docs").mkdir(parents=True)
            (root / "tools" / "tests").mkdir(parents=True)
            for relative in ("README.md", "LICENSE", "bridge.ps1", "launch-desktop.cmd",
                             "docs/windows-alpha.md", "tools/desktop.py", "tools/tests/test_demo.py"):
                path = root / relative
                path.write_text(relative, encoding="utf-8")
            (root / ".env").write_text("private-token", encoding="utf-8")
            (root / ".gitignore").write_text("tools/private.py\n", encoding="utf-8")
            (root / "tools" / "private.py").write_text("private-token", encoding="utf-8")
            (root / "tools" / "__pycache__").mkdir()
            (root / "tools" / "__pycache__" / "private.pyc").write_text("private", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "README.md", "LICENSE", "bridge.ps1",
                            "launch-desktop.cmd", "docs", "tools/desktop.py",
                            "tools/tests/test_demo.py"], cwd=root, check=True)
            archive = Path(temp) / "alpha.zip"
            manifest = build_archive(root, archive, "abc123")
            with zipfile.ZipFile(archive) as package:
                names = package.namelist()
                stored = json.loads(package.read(f"{ARCHIVE_ROOT}/manifest.json"))
                self.assertEqual(stored, manifest)
                self.assertIn(f"{ARCHIVE_ROOT}/launch-desktop.cmd", names)
                self.assertNotIn(f"{ARCHIVE_ROOT}/.env", names)
                self.assertNotIn(f"{ARCHIVE_ROOT}/tools/private.py", names)
                self.assertNotIn("private-token", repr(names) + repr(stored))
                for relative, digest in stored["files"].items():
                    self.assertEqual(hashlib.sha256(package.read(f"{ARCHIVE_ROOT}/{relative}")).hexdigest(), digest)

    def test_invalid_revision_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                build_archive(Path(temp), Path(temp) / "alpha.zip", "not-a-sha")

    def test_linked_document_directory_cannot_package_outside_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            root.mkdir()
            outside = Path(temp) / "outside"
            outside.mkdir()
            (outside / "private.md").write_text("private-content", encoding="utf-8")
            for name in ("README.md", "LICENSE", "bridge.ps1", "launch-desktop.cmd"):
                (root / name).write_text(name, encoding="utf-8")
            try:
                (root / "docs").symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            with self.assertRaises(ValueError):
                build_archive(root, root / "alpha.zip", "abc123")

    @unittest.skipUnless(os.name == "nt", "Windows cmd block expansion")
    def test_smoke_launcher_propagates_python_failure(self):
        launcher = Path(__file__).resolve().parents[2] / "launch-desktop.cmd"
        text = launcher.read_text(encoding="utf-8")
        self.assertIn("from tools.desktop import DesktopHost", text)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp:
            broken = Path(temp) / "launch-desktop.cmd"
            broken.write_text(text.replace("from tools.desktop import DesktopHost", "raise ValueError"),
                              encoding="utf-8")
            result = subprocess.run(["cmd", "/c", str(broken), "--smoke"], cwd=temp,
                                    capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
