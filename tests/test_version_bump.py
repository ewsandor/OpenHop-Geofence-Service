"""Tests for the interactive release version helper."""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "packaging/bump-version.py",
    "packaging/check-version.sh",
    "packaging/fedora/openhop-geofence.spec",
    "src/openhop_geofence/__init__.py",
    "debian/changelog",
    "debian/control",
)


class VersionBumpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in FILES:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)

    def run_bump(self, answers):
        return subprocess.run(
            [sys.executable, str(self.root / "packaging/bump-version.py")],
            input=answers, text=True, capture_output=True, cwd="/", timeout=10,
        )

    def test_bump_preserves_history_and_matches_release_tag(self):
        old_deb = (self.root / "debian/changelog").read_text()
        spec_path = self.root / "packaging/fedora/openhop-geofence.spec"
        old_spec = spec_path.read_text().replace("Release:        1%{?dist}", "Release:        3%{?dist}")
        spec_path.write_text(old_spec)
        major = int(re.search(r"^openhop-geofence \((\d+)\.", old_deb).group(1))
        version = f"{major + 1}.0.0"
        result = self.run_bump(f"{version}\nImprove 100% of checks\nRelease Author <author@example.org>\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        deb = (self.root / "debian/changelog").read_text()
        spec = spec_path.read_text()
        self.assertTrue(deb.startswith(f"openhop-geofence ({version})"))
        self.assertTrue(deb.endswith(old_deb))
        self.assertIn("Release Author <author@example.org>", deb)
        self.assertIn("Release:        1%{?dist}", spec)
        self.assertIn("Release Author <author@example.org>", spec)
        self.assertIn("- Improve 100%% of checks", spec)
        self.assertIn(old_spec.split("%changelog\n", 1)[1], spec)
        check = subprocess.run(["sh", "packaging/check-version.sh", f"v{version}"],
                               cwd=self.root, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stderr)

    def test_invalid_or_non_increasing_version_does_not_change_files(self):
        originals = {name: (self.root / name).read_bytes() for name in FILES}
        for version in ("v1.0.0", "0.0.0", "01.0.0", "1.0.0-rc1"):
            with self.subTest(version=version):
                result = self.run_bump(f"{version}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("version must be", result.stderr)
                self.assertEqual({name: (self.root / name).read_bytes() for name in FILES}, originals)


if __name__ == "__main__":
    unittest.main()
