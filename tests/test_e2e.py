"""End-to-end test: real pulse + real Classifier + TEST EQ sinks + silent fake streams.

Skipped unless ``OTO_EQ_E2E=1``.  Run:

    OTO_EQ_E2E=1 python3 -m unittest -v tests.test_e2e

Safety (the user is listening on the headset while this runs):
* The router only ever sees streams that carry the fake_stream marker AND this
  run's ``oto_eq.e2e_run`` id (``ScopedPulse.list_streams``); ``move_stream``
  re-checks that before every move and refuses anything else.
* The real default sink is never changed: ``default_sink_name`` is wrapped and
  presents the real default under an alias (``e2e_hs80_kulaklik`` = active,
  ``e2e_hoparlor_degil`` = gating); moves to the alias go to the real default.
* Fake streams play digital silence at volume 0. The sinks process is a
  separate ``pipewire -c <temp conf>``. Everything is torn down in
  tearDownClass, which also checks that no ``oto_eq_*`` sink is left over.
* It refuses to run (skip) if ``oto_eq_*`` sinks or ``pipewire -c`` processes
  already exist (names would collide with the real service / another test).
* The mode is kept in memory (the real mod.json is never touched). Rules are
  the shipped repo defaults (deterministic; the user's ~/.config copy and
  GUI assignments are ignored).
"""
from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from oto_eq import config
from oto_eq.models import (ALL_SINK_PROFILES, FLAT_PROFILE, SINK_PREFIX, Category,
                           is_eq_sink, sink_name)

E2E = os.environ.get("OTO_EQ_E2E") == "1"
RUN_KEY = "oto_eq.e2e_run"
ALIAS_ON = "e2e_hs80_kulaklik"         # matches (?i)hs80 -> router active
ALIAS_OFF = "e2e_hoparlor_degil"       # does not match -> gating
FLAT = sink_name(FLAT_PROFILE)          # oto_eq_duz: level-matched "no EQ" (CHANGE 1)
ROUTE_TIMEOUT = 1.0                    # new stream -> right sink
SWITCH_TIMEOUT = 3.5                   # mode switch (includes the 2 s anti-flap rule)

log = logging.getLogger("oto_eq.test_e2e")


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=config.C_ENV, capture_output=True, text=True,
                          timeout=timeout)


def eq_sinks_present() -> list[str]:
    out = _run(["pactl", "list", "short", "sinks"]).stdout
    return [ln.split("\t")[1] for ln in out.splitlines()
            if "\t" in ln and ln.split("\t")[1].startswith(SINK_PREFIX)]


def _conf_may_create_sinks(pgrep_line: str) -> bool:
    """A `pipewire -c <conf>` only conflicts if its conf can create sinks.

    E.g. oto-eq-mic.service (mikrofon.conf: one Audio/Source) is harmless. An
    unreadable/vanished conf (temp file of another test) counts as a conflict.
    """
    parts = pgrep_line.split()
    try:
        conf = Path(parts[parts.index("-c") + 1]).expanduser()
        text = conf.read_text(encoding="utf-8", errors="replace")
    except (ValueError, IndexError, OSError):
        return True
    return "Audio/Sink" in text


def live_conflicts() -> list[str]:
    """Reasons not to run now (another sinks process / test is live)."""
    problems = []
    try:
        pg = _run(["pgrep", "-af", "pipewire -c"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pg = ""
    procs = [ln for ln in pg.splitlines() if ln.split(None, 1)[-1].startswith(
        ("pipewire -c", "/usr/bin/pipewire -c")) and _conf_may_create_sinks(ln)]
    if procs:
        problems.append("çalışan 'pipewire -c' süreçleri: " + "; ".join(procs))
    sinks = eq_sinks_present()
    if sinks:
        problems.append("zaten var olan oto_eq_* çıkışları: " + ", ".join(sinks))
    return problems


class ScopedPulse:
    """Real oto_eq.pulse, narrowed to this run's fake streams, with a default-sink alias."""

    def __init__(self, pulse, run_id: str, marker_key: str):
        self._p = pulse
        self.run_id = run_id
        self.marker_key = marker_key
        self.PulseError = pulse.PulseError
        self.EventListener = pulse.EventListener
        self.real_default = pulse.default_sink_name()
        self.alias = ALIAS_ON
        self.violations: list[str] = []
        self.moves: list[tuple[int, str]] = []

    def _ours(self, s) -> bool:
        return bool(s.props.get(self.marker_key)) and s.props.get(RUN_KEY) == self.run_id

    def list_streams(self):
        return [s for s in self._p.list_streams() if self._ours(s)]

    def list_sinks(self):
        from oto_eq.models import Sink
        out = []
        for s in self._p.list_sinks():
            if s.name == self.real_default:
                s = Sink(s.index, self.alias, s.description, s.state)
            out.append(s)
        return out

    def default_sink_name(self) -> str:
        return self.alias

    def _check(self, index: int, sink: str) -> str | None:
        """Real target sink name if the move is allowed, else None (recorded)."""
        ours = {s.index for s in self.list_streams()}
        if index not in ours:
            self.violations.append(f"yabancı akış taşınmak istendi: #{index} → {sink}")
            return None
        real = self.real_default if sink in (ALIAS_ON, ALIAS_OFF) else sink
        if not (real == self.real_default or is_eq_sink(real)):
            self.violations.append(f"beklenmeyen hedef: #{index} → {sink}")
            return None
        return real

    def move_stream(self, index: int, sink: str) -> bool:
        real = self._check(index, sink)
        if real is None:
            return False
        self.moves.append((index, real))
        return self._p.move_stream(index, real)

    def mover(self, stream, sink: str, to_default: bool) -> bool:
        """Router's mover: guard, then the production MetadataMover (pw-metadata)."""
        from oto_eq.router import MetadataMover
        real = self._check(stream.index, sink)
        if real is None:
            return False
        self.moves.append((stream.index, real))
        return MetadataMover(fallback=self._p.move_stream)(stream, real, to_default)


class MemStore:
    def __init__(self):
        self.value = ("auto", None)

    def load(self):
        return self.value

    def save(self, mode, profile):
        self.value = (mode, profile)


# name, kind, props, expected category
SPECS = [
    ("cs2", "native", {"application.name": "Counter-Strike 2",
                       "application.process.binary": "cs2"}, Category.CS2),
    ("proton", "native", {"application.name": "Game-Win64-Shipping.exe",
                          "application.process.binary": "Game-Win64-Shipping.exe"},
     Category.GAME),
    ("spotify", "pulse", {"application.name": "Spotify",
                          "application.process.binary": "spotify",
                          "application.id": "com.spotify.Client",
                          "pipewire.access.portal.app_id": "com.spotify.Client"},
     Category.MUSIC),
    ("flatpak", "pulse", {"application.name": "oto-eq-e2e-flatpak",
                          "application.process.binary": "oto-eq-e2e-flatpak-bin",
                          "application.id": "tv.kodi.Kodi"}, Category.MOVIE),
    ("discord", "pulse", {"application.name": "WEBRTC VoiceEngine",
                          "application.process.binary": "Discord"}, Category.VOICE),
    ("speech", "pulse", {"application.name": "speech-dispatcher-dummy",
                         "application.process.binary": "sd_dummy"}, Category.SYSTEM),
    # pulse kind: pw-play always sets media.role=Music (-> "rol" step = muzik)
    ("bilinmeyen", "pulse", {"application.name": "oto-eq-e2e-bilinmeyen",
                              "application.process.binary": "oto-eq-e2e-bilinmeyen"},
     Category.UNKNOWN),
]
NO_EQ = {Category.VOICE, Category.SYSTEM}


@unittest.skipUnless(E2E, "uçtan uca test: OTO_EQ_E2E=1 ile çalıştırın")
class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("pipewire", "pactl", "pw-play", "paplay", "pgrep"):
            if not shutil.which(tool):
                raise unittest.SkipTest(f"{tool} yok")
        problems = live_conflicts()
        if problems:
            raise unittest.SkipTest("canlı çakışma, çalıştırılmadı: " + " | ".join(problems))

        from oto_eq import classifier, eqgen, pulse
        from oto_eq.router import Router
        from tests import fake_stream as fsmod

        cls.stack = contextlib.ExitStack()
        cls.sinks_proc = None
        cls.tmp = Path(tempfile.mkdtemp(prefix="oto-eq-e2e-"))
        try:
            real_default = pulse.default_sink_name()
            if not real_default or real_default.startswith(SINK_PREFIX):
                raise unittest.SkipTest(f"varsayılan çıkış uygun değil: {real_default!r}")

            # 1. TEST sinks process
            conf = cls.tmp / "oto-eq-sinks.e2e.conf"
            conf.write_text(eqgen.generate_conf(eqgen.load_profiles()), encoding="utf-8")
            cls.sinks_log = open(cls.tmp / "pipewire.log", "w")
            cls.sinks_proc = subprocess.Popen(["pipewire", "-c", str(conf)], env=config.C_ENV,
                                              stdout=cls.sinks_log, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL)
            want = {sink_name(p) for p in ALL_SINK_PROFILES}
            t0 = time.monotonic()
            while time.monotonic() - t0 < 10:
                if cls.sinks_proc.poll() is not None:
                    break
                if want <= {s.name for s in pulse.list_sinks()}:
                    break
                time.sleep(0.2)
            have = want & {s.name for s in pulse.list_sinks()}
            if have != want:
                raise RuntimeError(f"test EQ çıkışları açılmadı: {sorted(have)}; "
                                   f"günlük {cls.tmp / 'pipewire.log'}")

            # 2. in-process router (real pulse, narrowed; real Classifier, shipped rules)
            cls.run_id = uuid.uuid4().hex
            cls.pulse = ScopedPulse(pulse, cls.run_id, fsmod.MARKER_KEY)
            cls.router = Router(pulse=cls.pulse, classifier_factory=classifier.Classifier,
                                rules_loader=lambda: {}, mode_store=MemStore(),
                                mover=cls.pulse.mover)
            cls.router.start()
            cls.stack.callback(cls.router.stop)

            # 3. fake streams; each must land within ROUTE_TIMEOUT of appearing
            cls.streams = {}
            cls.route_times = {}
            for name, kind, props, cat in SPECS:
                fs = cls.stack.enter_context(fsmod.fake_stream(
                    {**props, RUN_KEY: cls.run_id}, kind=kind, duration=180))
                idx = fs.wait_index(timeout=5.0)
                t_seen = time.monotonic()
                cls.streams[name] = (idx, cat)
                exp = cls.expected_sink(cat)
                cls.wait_for(lambda: cls.sink_of(idx) == exp, ROUTE_TIMEOUT)
                cls.route_times[name] = (time.monotonic() - t_seen, cls.sink_of(idx), exp)
        except BaseException:
            cls.teardown_all()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.teardown_all()

    @classmethod
    def teardown_all(cls):
        try:
            if getattr(cls, "stack", None) is not None:
                cls.stack.close()           # router.stop + kill fake streams (LIFO)
        finally:
            p = getattr(cls, "sinks_proc", None)
            if p is not None and p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=5)
            if getattr(cls, "sinks_log", None):
                cls.sinks_log.close()
            t0 = time.monotonic()
            while eq_sinks_present() and time.monotonic() - t0 < 5:
                time.sleep(0.2)
            left = eq_sinks_present()
            if getattr(cls, "tmp", None):
                shutil.rmtree(cls.tmp, ignore_errors=True)
            cls.stack = cls.sinks_proc = None
            if left:
                raise AssertionError(f"temizlik sonrası oto_eq_* çıkışları kaldı: {left}")

    # helpers ------------------------------------------------------------

    @classmethod
    def expected_sink(cls, cat: Category, mode: str = "auto", fixed: str | None = None) -> str:
        clf = cls.router.classifier
        if cls.pulse.alias == ALIAS_OFF:          # headset not default -> real default
            return cls.pulse.alias
        if mode == "off":                         # active + off -> flat bypass
            return FLAT
        if mode == "fixed":
            return FLAT if cat in NO_EQ else sink_name(fixed)
        prof = clf.unknown_profile if cat == Category.UNKNOWN else clf.profile_for(cat)
        return sink_name(prof) if prof else FLAT

    @classmethod
    def sink_of(cls, idx: int) -> str | None:
        names = {s.index: s.name for s in cls.pulse.list_sinks()}
        for s in cls.pulse.list_streams():
            if s.index == idx:
                return names.get(s.sink_index)
        return None

    @staticmethod
    def wait_for(pred, timeout: float) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if pred():
                return True
            time.sleep(0.02)
        return pred()

    def assert_all(self, mode="auto", fixed=None, timeout=SWITCH_TIMEOUT):
        def ok():
            return all(self.sink_of(i) == self.expected_sink(c, mode, fixed)
                       for i, c in self.streams.values())
        self.wait_for(ok, timeout)
        for name, (idx, cat) in self.streams.items():
            with self.subTest(akış=name, mod=mode):
                self.assertEqual(self.sink_of(idx), self.expected_sink(cat, mode, fixed))

    def entry(self, idx):
        return next(e for e in self.router.status()["streams"] if e["index"] == idx)

    # tests (run in name order) -----------------------------------------

    def test_1_initial_routing(self):
        for name, (dt, got, exp) in self.route_times.items():
            with self.subTest(akış=name):
                self.assertEqual(got, exp, f"{name}: {got} (beklenen {exp})")
                self.assertLess(dt, ROUTE_TIMEOUT)
        for name, (idx, cat) in self.streams.items():
            with self.subTest(akış=name):
                self.assertEqual(self.entry(idx)["category"], cat.value,
                                 self.entry(idx)["reason"])
        st = self.router.status()
        self.assertTrue(st["active"])
        self.assertTrue(st["sinks_ready"])
        self.assertEqual(st["dominant"], "cs2")
        self.assertEqual({e["index"] for e in st["streams"]},
                         {i for i, _ in self.streams.values()})   # nothing else visible
        self.assertEqual(self.pulse.violations, [])

    def test_2_fixed_off_auto(self):
        r = self.router
        self.assertEqual(r.handle_command({"cmd": "set_mode", "mode": "fixed",
                                           "profile": "film"}), {"ok": True})
        self.assert_all("fixed", "film")
        self.assertEqual(r.status()["dominant"], "film")
        self.assertEqual(r.handle_command({"cmd": "set_mode", "mode": "off"}), {"ok": True})
        self.assert_all("off")
        self.assertIsNone(r.status()["dominant"])
        idx = self.streams["cs2"][0]
        self.assertEqual(self.entry(idx)["sink"], FLAT)
        self.assertIsNone(self.entry(idx)["profile"])
        self.assertEqual(r.handle_command({"cmd": "set_mode", "mode": "auto"}), {"ok": True})
        self.assert_all("auto")
        self.assertEqual(r.status()["dominant"], "cs2")
        self.assertEqual(self.pulse.violations, [])

    def test_3_gating(self):
        r = self.router
        time.sleep(2.1)   # let the anti-flap window from test_2 pass
        self.pulse.alias = ALIAS_OFF
        r.request_rescan()
        self.assert_all("auto")        # expected_sink() honours ALIAS_OFF -> default
        self.assertFalse(r.status()["active"])
        self.assertEqual(r.status()["default_sink"], ALIAS_OFF)
        self.pulse.alias = ALIAS_ON
        r.request_rescan()
        self.assert_all("auto")
        self.assertTrue(r.status()["active"])
        self.assertEqual(self.pulse.violations, [])

    def test_4_moves_only_ours(self):
        ours = {i for i, _ in self.streams.values()}
        self.assertTrue(self.pulse.moves)
        self.assertTrue(all(i in ours for i, _ in self.pulse.moves))
        self.assertTrue(all(s == self.pulse.real_default or is_eq_sink(s)
                            for _, s in self.pulse.moves))
        self.assertEqual(self.pulse.violations, [])


if __name__ == "__main__":
    unittest.main()
