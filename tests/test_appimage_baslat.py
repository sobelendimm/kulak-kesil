"""AppImage launcher (packaging/appimage/run.sh): with FUSE the AppImage runs directly; without FUSE the extracted
copy is cached under ~/.cache/kulak-kesil/ and reused (no full extraction on every launch). Uses a fake AppImage
(a shell script) in a fake HOME; nothing real is touched."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "packaging/appimage/run.sh"
SETUP = ROOT / "packaging/appimage/setup.sh"

FAKE = """#!/bin/sh
# fake AppImage: --appimage-extract creates squashfs-root/AppRun and counts extractions
if [ "$1" = "--appimage-extract" ]; then
    [ -z "${FAKE_FAIL:-}" ] || { echo "extract failed" >&2; exit 1; }
    echo x >> "$FAKE_COUNT"
    mkdir -p squashfs-root/usr/share/kulak-kesil
    printf '#!/bin/sh\\necho "cached:$*"\\n' > squashfs-root/AppRun
    chmod 755 squashfs-root/AppRun
    exit 0
fi
echo "direct:$*"
"""


class Baslat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.app = self.home / ".local/share/kulak-kesil/kulak-kesil.AppImage"
        self.app.parent.mkdir(parents=True)
        self.app.write_text(FAKE)
        self.app.chmod(0o755)
        self.count = self.tmp / "count"
        self.cache = self.home / ".cache/kulak-kesil"

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def run_(self, *args, fuse="0", **env):
        e = {"PATH": os.environ["PATH"], "HOME": str(self.home), "KULAK_KESIL_FUSE": fuse,
             "FAKE_COUNT": str(self.count), **env}
        return subprocess.run(["sh", str(RUN), *args], capture_output=True, text=True, env=e, timeout=30, check=False)

    def extractions(self):
        return len(self.count.read_text().split()) if self.count.exists() else 0

    def test_fuse_runs_directly_without_cache(self):
        r = self.run_("--cli", "status", fuse="1")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "direct:--cli status"), r.stderr)
        self.assertEqual(self.extractions(), 0)
        self.assertFalse(self.cache.exists())

    def test_no_fuse_extracts_once_and_reuses(self):
        r1 = self.run_("--cli", "a")
        self.assertEqual((r1.returncode, r1.stdout.strip()), (0, "cached:--cli a"), r1.stderr)
        r2 = self.run_("--tray")
        self.assertEqual((r2.returncode, r2.stdout.strip()), (0, "cached:--tray"), r2.stderr)
        self.assertEqual(self.extractions(), 1)
        self.assertEqual(len([p for p in self.cache.iterdir() if p.is_dir()]), 1)

    def test_replaced_appimage_reextracts_and_prunes_old_cache(self):
        self.run_("--app")
        self.app.write_text(FAKE + "# new build\n")
        r = self.run_("--app")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.extractions(), 2)
        self.assertEqual(len([p for p in self.cache.iterdir() if p.is_dir()]), 1)

    def test_stale_copy_in_use_is_not_pruned(self):
        self.run_("--app")
        (old,) = [p for p in self.cache.iterdir() if p.is_dir()]
        (old / "hold.sh").write_text("sleep 20\n")
        holder = subprocess.Popen(["sh", str(old / "hold.sh")])
        try:
            self.app.write_text(FAKE + "# new build\n")
            self.assertEqual(self.run_("--app").returncode, 0)
            self.assertTrue(old.is_dir(), "copy used by a running process was deleted")
        finally:
            holder.kill()
            holder.wait()
        self.assertEqual(self.run_("--app").returncode, 0)     # next start prunes it
        self.assertFalse(old.exists())

    def test_failed_extraction_leaves_no_cache_and_fails(self):
        r = self.run_("--app", FAKE_FAIL="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("extract", r.stderr.lower())
        self.assertEqual([p for p in self.cache.glob("*") if p.is_dir() and not p.name.startswith(".")], [])
        self.assertEqual(self.run_("--app").returncode, 0)   # next launch recovers

    def test_missing_appimage_is_an_error(self):
        self.app.unlink()
        r = self.run_("--app")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not found", r.stderr)

    def test_no_libfuse2_means_cached_path(self):
        empty = self.tmp / "nolib"
        empty.mkdir()
        r = self.run_("--help", fuse="", KULAK_KESIL_LIBDIRS=str(empty))
        self.assertEqual(r.stdout.strip(), "cached:--help", r.stderr)

    def test_fuse_override_values(self):
        self.assertEqual(self.run_("--help", fuse="1").stdout.strip(), "direct:--help")
        self.assertEqual(self.run_("--help", fuse="0").stdout.strip(), "cached:--help")


class Kurulum(unittest.TestCase):
    def test_setup_uses_launcher_not_extract_and_run(self):
        s = SETUP.read_text()
        self.assertNotIn("APPIMAGE_EXTRACT_AND_RUN", s)
        self.assertIn("run.sh", s)
        self.assertIn("ExecStart=/usr/bin/env KULAK_KESIL_APPIMAGE=$DEST $LAUNCH --router", s)
        self.assertIn('"$LAUNCH" --help', s)

    def test_build_installs_launcher_and_ci_lints_it(self):
        self.assertIn("run.sh", (ROOT / "packaging/appimage/build.sh").read_text())
        self.assertIn("packaging/appimage/run.sh", (ROOT / ".github/workflows/ci.yml").read_text())


if __name__ == "__main__":
    unittest.main()
