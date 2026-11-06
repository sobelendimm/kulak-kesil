"""Live /proc tests for oto_eq.procinfo (only our own child processes are inspected)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import unittest

from oto_eq import procinfo
from oto_eq.config import C_ENV
from oto_eq.models import ProcInfo, Stream


def stream(pid, binary, index=1, props=None):
    return Stream(index=index, sink_index=0, app_name=binary, binary=binary, pid=pid,
                  media_name="", media_role="", node_name="", corked=False, props=props or {})


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    return None


class TestProcInfoLive(unittest.TestCase):
    def setUp(self):
        procinfo.clear_cache()

    def test_own_pid(self):
        info = procinfo.proc_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertEqual(info.pid, os.getpid())
        self.assertTrue(info.comm)
        self.assertTrue(info.exe.startswith("python"), info.exe)
        self.assertTrue(info.cmdline)
        self.assertTrue(info.ancestors)
        self.assertEqual(info.ancestors[0][0], os.getppid())
        self.assertLessEqual(len(info.ancestors[0][2]), procinfo.ANCESTOR_CMD_MAX)
        self.assertNotIn("HOME", info.env)            # only the whitelisted subset

    def test_cache_returns_same_object(self):
        a = procinfo.proc_info(os.getpid())
        b = procinfo.proc_info(os.getpid())
        self.assertIs(a, b)

    def test_missing_and_bad_pids(self):
        for bad in (0, -1, 2 ** 22 + 12345, None, "x"):
            self.assertIsNone(procinfo.proc_info(bad))   # type: ignore[arg-type]

    def test_child_env_subset_and_ancestor(self):
        env = {**C_ENV, "SteamAppId": "999", "WINEPREFIX": "/tmp/pfx",
               "FAUGUS_LOG": "1", "PROTONPATH": "GE-Proton", "SECRET_X": "no"}
        p = subprocess.Popen(["sleep", "30"], env=env)
        try:
            info = wait_for(lambda: (i := procinfo.proc_info(p.pid)) and i.comm == "sleep" and i)
            self.assertIsNotNone(info)
            self.assertEqual(info.env.get("SteamAppId"), "999")
            self.assertEqual(info.env.get("WINEPREFIX"), "/tmp/pfx")
            self.assertEqual(info.env.get("FAUGUS_LOG"), "1")
            self.assertEqual(info.env.get("PROTONPATH"), "GE-Proton")
            self.assertNotIn("SECRET_X", info.env)
            self.assertEqual(info.ancestors[0][0], os.getpid())
            # resolve_pid accepts a valid pid whose binary matches
            self.assertEqual(procinfo.resolve_pid(stream(p.pid, "sleep")), p.pid)
        finally:
            p.kill()
            p.wait(5)


class TestBinaryMatches(unittest.TestCase):
    def info(self, comm, exe="", cmd=()):
        return ProcInfo(pid=1, comm=comm, exe=exe, cmdline=tuple(cmd))

    def test_variants(self):
        bm = procinfo.binary_matches
        self.assertTrue(bm(self.info("cs2", "cs2"), "cs2"))
        self.assertTrue(bm(self.info("Game-Win64-Ship", "wine64-preloader"), "Game-Win64-Shipping.exe"))
        self.assertTrue(bm(self.info("eldenring.exe", "wine64-preloader"), "wine64-preloader"))
        self.assertTrue(bm(self.info("x", "y", ("C:\\Games\\X\\Game.exe",)), "game.exe"))
        self.assertTrue(bm(self.info("spotify", "spotify"), "Spotify"))
        self.assertTrue(bm(self.info("anything"), ""))
        self.assertFalse(bm(self.info("kthreadd"), "spotify"))
        self.assertFalse(bm(None, "x"))


class TestResolveNamespaced(unittest.TestCase):
    """A process inside its own PID namespace reports pid 1/2 -> map to host pid via NSpid."""

    @unittest.skipUnless(shutil.which("unshare"), "unshare yok")
    def test_pid_namespace(self):
        procinfo.clear_cache()
        p = subprocess.Popen(["unshare", "-Urpf", "--mount-proc", "sh", "-c", "exec sleep 30"],
                             env=C_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def find():
                for pid in procinfo._iter_pids():
                    st = procinfo.read_stat(pid)
                    if st and st[0] == "sleep" and st[1] != os.getpid():
                        info = procinfo.proc_info(pid)
                        if info and any(a[0] == p.pid for a in info.ancestors):
                            return pid
                return None
            host = wait_for(find)
            if host is None:
                self.skipTest("kullanıcı ad alanı oluşturulamadı")
            ns = procinfo.ns_pids(host)
            self.assertGreater(len(ns), 1, ns)
            inner = ns[-1]
            # stream claims the namespace-local pid (1): host pid 1 is not 'sleep'
            got = procinfo.resolve_pid(stream(inner, "sleep", index=777))
            # other sandboxes on the machine may also run a 'sleep' as their pid 1,
            # so check the property rather than the exact pid
            self.assertIsNotNone(got)
            self.assertNotEqual(got, inner)
            self.assertEqual(procinfo.proc_info(got).comm, "sleep")
            self.assertEqual(procinfo.ns_pids(got)[-1], inner)
        finally:
            p.kill()
            p.wait(5)

    def test_unresolvable(self):
        procinfo.clear_cache()
        self.assertIsNone(procinfo.resolve_pid(stream(None, "no-such-binary-otoeq", index=778)))
        self.assertIsNone(procinfo.resolve_pid(stream(2 ** 22 + 1, "no-such-binary-otoeq", index=779)))

    def test_never_raises(self):
        self.assertIsNone(procinfo.resolve_pid(None))   # type: ignore[arg-type]


class TestHelpers(unittest.TestCase):
    def test_open_files_own(self):
        with open(__file__, "rb"):
            files = procinfo.open_files(os.getpid())
        self.assertIn(os.path.realpath(__file__), files)

    def test_flatpak_id_host_process(self):
        self.assertEqual(procinfo.flatpak_id(os.getpid()), "")

    def test_basename_any(self):
        self.assertEqual(procinfo.basename_any("Z:\\a\\b\\Game.exe"), "Game.exe")
        self.assertEqual(procinfo.basename_any("/usr/bin/mpv"), "mpv")


if __name__ == "__main__":
    unittest.main()
