"""End-to-end run of packaging/appimage/run.sh with a REAL AppImage (not a fake) in a throw-away HOME.

Set KULAK_KESIL_E2E_APPIMAGE=/path/to/kulak-kesil-X.AppImage to run it (e.g. the file from
`gh release download v1.0.1 -p '*.AppImage'`); without it every test is skipped. Nothing outside the temporary
HOME/XDG_CACHE_HOME is touched; uninstall.sh runs with stub systemctl/pgrep/pactl so no real service is stopped.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:    # CI runs the suite with unittest, where pytest may be missing: the marker is only a convenience
    import pytest
    pytestmark = pytest.mark.e2e
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "packaging/appimage/run.sh"
APP = os.environ.get("KULAK_KESIL_E2E_APPIMAGE", "")
USAGE = "Kulak Kesil (AppImage)"


@unittest.skipUnless(APP and Path(APP).is_file(), "KULAK_KESIL_E2E_APPIMAGE not set")
class RealAppImage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.cache = self.tmp / "cache" / "kulak-kesil"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **kw):
        return {"PATH": os.environ["PATH"], "HOME": str(self.home), "XDG_CACHE_HOME": str(self.tmp / "cache"),
                "KULAK_KESIL_APPIMAGE": APP, **kw}

    def run_(self, *args, **kw):
        return subprocess.run(["sh", str(RUN), *args], env=self.env(**kw), capture_output=True, text=True,
                              timeout=120, check=False)

    def copies(self):
        if not self.cache.exists():
            return []
        return [p for p in self.cache.glob("*") if p.is_dir()]

    def test_with_fuse_runs_directly_without_extraction(self):
        r = self.run_("--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(USAGE, r.stdout)
        if not list(self.cache.glob(".fuse-ok-*")):
            self.skipTest("this host cannot mount the AppImage (no working FUSE)")
        self.assertEqual(self.copies(), [], "FUSE path must not extract")

    def masked_fuse(self):
        """Run run.sh in a mount namespace where /dev/fuse is /dev/null: the mount probe must fail."""
        script = f"mount --bind /dev/null /dev/fuse && sh {RUN} --help"
        r = subprocess.run(["unshare", "-rm", "sh", "-c", script], env=self.env(), capture_output=True,
                           text=True, timeout=120, check=False)
        if "mount: " in r.stderr or "unshare" in r.stderr:
            self.skipTest("cannot create a mount namespace here: " + r.stderr.strip()[:80])
        return r

    def test_without_fuse_extracts_once_and_reuses(self):
        r1 = self.masked_fuse()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn(USAGE, r1.stdout)
        (copy,) = self.copies()
        self.assertTrue((copy / "AppRun").exists())
        self.assertFalse(list(self.cache.glob(".fuse-ok-*")))
        stamp = (copy / "AppRun").stat().st_mtime_ns
        r2 = self.masked_fuse()
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertEqual([p.name for p in self.copies()], [copy.name])
        self.assertEqual((copy / "AppRun").stat().st_mtime_ns, stamp, "reused copy was re-extracted")

    def test_concurrent_starts_share_one_extraction(self):
        procs = [subprocess.Popen(["sh", str(RUN), "--help"], env=self.env(KULAK_KESIL_FUSE="0"),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err)
            self.assertIn(USAGE, out)
        self.assertEqual(len(self.copies()), 1)
        self.assertEqual(list(self.cache.glob(".extract.*")), [])

    def test_uninstall_removes_cache(self):
        self.assertEqual(self.run_("--help", KULAK_KESIL_FUSE="0").returncode, 0)
        self.assertEqual(len(self.copies()), 1)
        stubs = self.tmp / "stubs"
        stubs.mkdir()
        for name, code in (("systemctl", 0), ("pgrep", 1), ("pactl", 1), ("xdg-user-dir", 1)):
            (stubs / name).write_text(f"#!/bin/sh\nexit {code}\n")
            (stubs / name).chmod(0o755)
        env = self.env(PATH=f"{stubs}:/usr/bin:/bin", LANG="C.UTF-8", XDG_RUNTIME_DIR=str(self.home))
        r = subprocess.run(["bash", str(ROOT / "uninstall.sh")], env=env, capture_output=True, text=True,
                           timeout=120, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
