"""Router policy tests with in-memory fakes (no audio graph is touched)."""
from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from oto_eq import router as R
from oto_eq.models import Category, Decision, Event, MediaPlayer, Sink, Stream

HS80 = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver-00.analog-stereo"
SPEAKERS = "alsa_output.pci-0000_0e_00.6.analog-stereo"
HDMI = "alsa_output.pci-0000_03_00.1.hdmi-stereo"
EQ_SINKS = ["oto_eq_cs2", "oto_eq_oyun", "oto_eq_muzik", "oto_eq_film", "oto_eq_duz"]
DUZ = "oto_eq_duz"


def setUpModule():
    logging.getLogger("oto_eq").setLevel(logging.CRITICAL)


def tearDownModule():
    logging.getLogger("oto_eq").setLevel(logging.NOTSET)


class FakePulseError(Exception):
    pass


class FakePulse:
    """In-memory pactl replacement. Records moves."""

    PulseError = FakePulseError

    def __init__(self, sinks=None, default=HS80):
        self.sinks: list[Sink] = []
        for name in (sinks if sinks is not None else [HS80, SPEAKERS, HDMI, *EQ_SINKS]):
            self.add_sink(name)
        self.default = default
        self.streams: dict[int, Stream] = {}
        self.moves: list[tuple[int, str]] = []
        self.fail_list = False
        self.fail_move: set[int] = set()
        self.noop_moves = 0            # simulate pactl exiting 0 without moving
        self.listeners: list = []
        self.lock = threading.Lock()
        outer = self

        class EventListener:
            def __init__(self, callback):
                self.callback = callback
                self.running = False
                outer.listeners.append(self)

            def start(self):
                self.running = True

            def stop(self):
                self.running = False

        self.EventListener = EventListener

    # helpers
    def add_sink(self, name):
        idx = 100 + len(self.sinks)
        self.sinks.append(Sink(idx, name, name, "IDLE"))

    def remove_sink(self, name):
        self.sinks = [s for s in self.sinks if s.name != name]

    def sink_idx(self, name):
        return next(s.index for s in self.sinks if s.name == name)

    def add_stream(self, index, binary, sink=None, corked=False, node_name="", app=None):
        st = Stream(index=index, sink_index=self.sink_idx(sink or self.default),
                    app_name=app if app is not None else binary, binary=binary, pid=1000 + index,
                    media_name="", media_role="", node_name=node_name, corked=corked)
        with self.lock:
            self.streams[index] = st
        return st

    def sink_of(self, index):
        si = self.streams[index].sink_index
        return next(s.name for s in self.sinks if s.index == si)

    def set_sink(self, index, name):  # "someone else" moves a stream
        with self.lock:
            self.streams[index] = dataclasses.replace(self.streams[index],
                                                      sink_index=self.sink_idx(name))

    def set_corked(self, index, corked):
        self.streams[index] = dataclasses.replace(self.streams[index], corked=corked)

    def emit(self, ev):
        for l in self.listeners:
            l.callback(ev)

    # pulse API
    def default_sink_name(self):
        if self.fail_list:
            raise FakePulseError("bağlantı yok")
        return self.default

    def list_sinks(self):
        if self.fail_list:
            raise FakePulseError("bağlantı yok")
        return list(self.sinks)

    def list_streams(self):
        if self.fail_list:
            raise FakePulseError("bağlantı yok")
        with self.lock:
            return list(self.streams.values())

    def move_stream(self, index, sink_name):
        if index in self.fail_move:
            raise FakePulseError("taşıma hatası")
        with self.lock:
            if index not in self.streams:
                return False
            self.moves.append((index, sink_name))
            if self.noop_moves > 0:
                self.noop_moves -= 1
                return True
            self.streams[index] = dataclasses.replace(self.streams[index],
                                                      sink_index=self.sink_idx(sink_name))
        return True


CATS = {
    "cs2": Decision(Category.CS2, "cs2", "ikili 'cs2'"),
    "game.exe": Decision(Category.GAME, "oyun", "Proton oyunu"),
    "spotify": Decision(Category.MUSIC, "muzik", "müzik uygulaması"),
    "vlc": Decision(Category.MOVIE, "film", "video oynatıcı"),
    "Discord": Decision(Category.VOICE, None, "sesli sohbet"),
    "speech-dispatcher": Decision(Category.SYSTEM, None, "sistem sesi"),
    "weird": Decision(Category.UNKNOWN, None, "bilinmeyen"),
}


class FakeClassifier:
    def __init__(self, rules):
        self.rules = rules
        self.table = dict(CATS)
        self.table.update(rules.get("_tablo", {}))
        self.seen_players = None

    def classify(self, stream, players):
        self.seen_players = players
        if stream.binary == "boom":
            raise RuntimeError("patladı")
        return self.table.get(stream.binary, Decision(Category.UNKNOWN, None, "bilinmeyen"))


class FakeMpris:
    def __init__(self):
        self.started = self.stopped = False
        self.list = []

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def players(self):
        return list(self.list)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class MemStore:
    def __init__(self, mode="auto", profile=None):
        self.value = (mode, profile)
        self.saves = []

    def load(self):
        return self.value

    def save(self, mode, profile):
        self.value = (mode, profile)
        self.saves.append((mode, profile))


class RouterTestBase(unittest.TestCase):
    def make(self, rules=None, mode=("auto", None), **kw):
        self.pulse = kw.pop("pulse", None) or FakePulse()
        self.clock = FakeClock()
        self.mpris = FakeMpris()
        self.store = MemStore(*mode)
        self.rules = rules if rules is not None else {"genel": {"kulaklik_desen": "(?i)hs80"}}
        self.r = R.Router(pulse=self.pulse, classifier_factory=FakeClassifier, mpris=self.mpris,
                          rules_loader=lambda: self.rules, clock=self.clock,
                          mode_store=self.store, **kw)
        self.r.allow_inline = True      # no worker thread in these tests
        self.r.prepare()
        return self.r

    def entry(self, index):
        return next(s for s in self.r.status()["streams"] if s["index"] == index)


class TestRouting(RouterTestBase):
    def test_categories(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        p.add_stream(2, "game.exe")
        p.add_stream(3, "spotify")
        p.add_stream(4, "vlc")
        p.add_stream(5, "Discord")
        p.add_stream(6, "speech-dispatcher")
        p.add_stream(7, "weird")
        self.assertTrue(r.scan())
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")
        self.assertEqual(p.sink_of(2), "oto_eq_oyun")
        self.assertEqual(p.sink_of(3), "oto_eq_muzik")
        self.assertEqual(p.sink_of(4), "oto_eq_film")
        for i in (5, 6, 7):   # no EQ -> flat level-matched sink (CHANGE 1)
            self.assertEqual(p.sink_of(i), DUZ)
        self.assertEqual(len(p.moves), 7)
        st = r.status()
        self.assertTrue(st["active"])
        self.assertTrue(st["sinks_ready"])
        self.assertEqual(st["default_sink"], HS80)
        self.assertEqual(st["mode"], "auto")
        self.assertIsNone(st["fixed_profile"])
        e = self.entry(1)
        self.assertEqual(e, {"index": 1, "app": "cs2", "binary": "cs2", "category": "cs2",
                             "profile": "cs2", "reason": "ikili 'cs2'", "sink": "oto_eq_cs2"})
        self.assertEqual(self.entry(5)["category"], "sesli")
        self.assertIsNone(self.entry(5)["profile"])
        self.assertEqual(self.entry(5)["sink"], DUZ)
        self.assertEqual(st["dominant"], "cs2")
        # second scan: nothing to do
        r.scan()
        self.assertEqual(len(p.moves), 7)

    def test_move_log_line(self):
        r = self.make()
        self.pulse.add_stream(12, "cs2")
        logger = logging.getLogger("oto_eq.router")
        logger.setLevel(logging.INFO)
        try:
            with self.assertLogs("oto_eq.router", "INFO") as cm:
                r.scan()
        finally:
            logger.setLevel(logging.NOTSET)
        self.assertIn("akış #12 cs2 → CS2 (ikili 'cs2')", "\n".join(cm.output))

    def test_profile_none_on_eq_moves_back(self):
        r = self.make()
        self.pulse.add_stream(1, "Discord", sink="oto_eq_muzik")
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), DUZ)

    def test_flat_missing_falls_back_to_default(self):
        pulse = FakePulse(sinks=[HS80, "oto_eq_cs2", "oto_eq_oyun", "oto_eq_muzik",
                                 "oto_eq_film"])
        r = self.make(pulse=pulse)
        pulse.add_stream(1, "Discord", sink="oto_eq_muzik")
        pulse.add_stream(2, "speech-dispatcher")
        r.scan()
        self.assertEqual(pulse.sink_of(1), HS80)
        self.assertEqual(pulse.sink_of(2), HS80)
        self.assertFalse(r.status()["sinks_ready"])   # all 5 needed

    def test_stream_on_flat_is_ours(self):
        r = self.make()
        self.pulse.add_stream(1, "spotify", sink=DUZ)
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_muzik")

    def test_test_sinks_not_ours(self):
        """Exact-name matching: A4/A1 test sinks like oto_eq_test_null are foreign."""
        pulse = FakePulse(sinks=[HS80, *EQ_SINKS, "oto_eq_test_null"])
        r = self.make(pulse=pulse)
        pulse.add_stream(1, "cs2", sink="oto_eq_test_null")
        r.scan()
        self.assertEqual(pulse.sink_of(1), "oto_eq_test_null")
        self.assertEqual(pulse.moves, [])

    def test_classifier_exception_is_unknown(self):
        r = self.make()
        self.pulse.add_stream(1, "boom")
        self.assertTrue(r.scan())
        self.assertEqual(self.entry(1)["category"], "bilinmeyen")
        self.assertEqual(self.pulse.sink_of(1), DUZ)

    def test_voice_profile_forced_none(self):
        rules = {"_tablo": {"tt": Decision(Category.VOICE, "cs2", "hatalı")}}
        r = self.make(rules=rules)
        self.pulse.add_stream(1, "tt")
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), DUZ)

    def test_mpris_players_passed(self):
        r = self.make()
        self.mpris.list = [MediaPlayer("org.mpris.MediaPlayer2.vlc", 5, "VLC", "Playing",
                                       "t", "a", "")]
        self.pulse.add_stream(1, "vlc")
        r.scan()
        self.assertEqual(len(r.classifier.seen_players), 1)


class TestModes(RouterTestBase):
    def test_fixed_mode(self):
        r = self.make(mode=("fixed", "film"))
        p = self.pulse
        p.add_stream(1, "cs2")
        p.add_stream(2, "weird")
        p.add_stream(3, "Discord")
        p.add_stream(4, "speech-dispatcher")
        r.scan()
        self.assertEqual(p.sink_of(1), "oto_eq_film")
        self.assertEqual(p.sink_of(2), "oto_eq_film")
        self.assertEqual(p.sink_of(3), DUZ)
        self.assertEqual(p.sink_of(4), DUZ)
        st = r.status()
        self.assertEqual((st["mode"], st["fixed_profile"]), ("fixed", "film"))
        self.assertIn("sabit mod", self.entry(1)["reason"])

    def test_off_mode(self):
        r = self.make(mode=("off", None))
        p = self.pulse
        p.add_stream(1, "cs2", sink="oto_eq_cs2")
        p.add_stream(2, "spotify")
        p.add_stream(3, "x", sink=HDMI)          # manual move still respected
        r.scan()
        self.assertEqual(p.sink_of(1), DUZ)      # off while active -> flat bypass
        self.assertEqual(p.sink_of(2), DUZ)
        self.assertEqual(p.sink_of(3), HDMI)
        self.assertIsNone(r.status()["dominant"])
        self.assertEqual(self.entry(1)["sink"], DUZ)
        self.assertIsNone(self.entry(1)["profile"])

    def test_set_mode_command(self):
        r = self.make()
        self.pulse.add_stream(1, "spotify")
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_muzik")
        self.clock.advance(5)
        self.assertEqual(r.handle_command({"cmd": "set_mode", "mode": "fixed",
                                           "profile": "cs2"}), {"ok": True})
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_cs2")
        self.assertEqual(self.store.saves[-1], ("fixed", "cs2"))
        self.assertEqual(r.status()["fixed_profile"], "cs2")
        with self.assertRaises(ValueError):
            r.handle_command({"cmd": "set_mode", "mode": "fixed", "profile": "bas"})
        with self.assertRaises(ValueError):
            r.handle_command({"cmd": "set_mode", "mode": "sessiz"})
        self.clock.advance(5)
        r.handle_command({"cmd": "set_mode", "mode": "off"})
        self.assertEqual(self.pulse.sink_of(1), DUZ)
        st = r.status()
        self.assertEqual((st["mode"], st["fixed_profile"]), ("off", None))

    def test_default_mic_retarget_matches_micgen_signature(self):
        # the router called micgen.retarget(target=…) (no such parameter): TypeError in a
        # thread, logged as a warning, the HS80 wired/wireless mic switch never happened
        import inspect
        from oto_eq import micgen, router as routermod
        inspect.signature(micgen.retarget).bind("alsa_input.x", wait=2.0)
        calls = []
        with mock.patch.object(micgen, "retarget", lambda *a, **k: calls.append((a, k)) or True), \
                mock.patch.object(routermod.threading, "Thread") as th:
            self.assertTrue(routermod._default_mic_retarget("alsa_input.x"))
            th.call_args.kwargs["target"]()
        self.assertEqual(calls, [(("alsa_input.x",), {"wait": 2.0})])

    def test_language_command_reloads_the_catalog(self):
        r = self.make()
        with mock.patch("oto_eq.router.i18n.yeniden_yukle", return_value="en") as yy:
            self.assertEqual(r.handle_command({"cmd": "dil"}), {"ok": True, "dil": "en"})
        yy.assert_called_once_with()

    def test_reload(self):
        r = self.make()
        self.pulse.add_stream(1, "weird")
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), DUZ)
        self.clock.advance(5)
        self.rules = {"_tablo": {"weird": Decision(Category.GAME, "oyun", "yeni kural")}}
        self.assertEqual(r.handle_command({"cmd": "reload"}), {"ok": True})
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_oyun")

    def test_reload_error_keeps_old_rules(self):
        r = self.make()
        old = r.classifier

        def bad():
            raise ValueError("TOML bozuk")
        r.rules_loader = bad
        resp = r.handle_command({"cmd": "reload"})
        self.assertFalse(resp["ok"])
        self.assertIn("TOML bozuk", resp["error"])
        self.assertIs(r.classifier, old)

    def test_bad_headset_pattern_rejected(self):
        r = self.make()
        self.rules = {"genel": {"kulaklik_desen": "(hs80"}}
        resp = r.handle_command({"cmd": "reload"})
        self.assertFalse(resp["ok"])
        self.assertIn("kulaklik_desen", resp["error"])

    def test_missing_rules_at_start(self):
        def bad():
            raise FileNotFoundError("kurallar.toml yok")
        pulse = FakePulse()
        r = R.Router(pulse=pulse, classifier_factory=FakeClassifier, rules_loader=bad,
                     clock=FakeClock(), mode_store=MemStore())
        r.prepare()
        pulse.add_stream(1, "cs2")
        self.assertTrue(r.scan())
        self.assertEqual(pulse.sink_of(1), DUZ)   # null classifier -> no EQ
        self.assertEqual(r.status()["streams"][0]["reason"], "kurallar yüklenemedi")

    def test_default_rules_loader_is_config_load_rules(self):
        calls = []
        orig = R.config.load_rules
        R.config.load_rules = lambda: (calls.append(1), {"atama": {}})[1]
        try:
            r = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier,
                         clock=FakeClock(), mode_store=MemStore())
            r.allow_inline = True
            r.prepare()
            self.assertEqual(r.rules, {"atama": {}})
            self.assertEqual(r.handle_command({"cmd": "reload"}), {"ok": True})
            self.assertEqual(len(calls), 2)   # reload re-reads through it
        finally:
            R.config.load_rules = orig

    def test_default_pattern_when_genel_missing(self):
        r = self.make(rules={})
        self.assertEqual(r.headset_re.pattern, "(?i)hs80")


class TestGating(RouterTestBase):
    def test_not_headset_moves_back(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        p.add_stream(2, "spotify")
        r.scan()
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")
        self.clock.advance(5)
        p.default = SPEAKERS
        r.scan()
        self.assertEqual(p.sink_of(1), SPEAKERS)
        self.assertEqual(p.sink_of(2), SPEAKERS)
        st = r.status()
        self.assertFalse(st["active"])
        self.assertEqual(st["default_sink"], SPEAKERS)
        self.assertIsNone(st["dominant"])
        # new stream while inactive stays on default
        p.add_stream(3, "vlc", sink=SPEAKERS)
        r.scan()
        self.assertEqual(p.sink_of(3), SPEAKERS)

    def test_stream_left_on_old_default_by_us_follows(self):
        """A stream we parked on the HS80 (move-back pins it) follows a default change."""
        p = FakePulse(sinks=[HS80, SPEAKERS, "oto_eq_cs2"])   # no duz -> fallback default
        r = self.make(pulse=p)
        p.add_stream(1, "Discord", sink="oto_eq_cs2")
        r.scan()
        self.assertEqual(p.sink_of(1), HS80)   # we moved it -> pinned to HS80
        self.clock.advance(5)
        p.default = SPEAKERS
        r.scan()
        self.assertEqual(p.sink_of(1), SPEAKERS)
        # but a stream we never touched that sits on the HS80 is respected
        p.add_stream(2, "Discord", sink=HS80)
        r.scan()
        self.assertEqual(p.sink_of(2), HS80)

    def test_flat_streams_go_to_default_when_inactive(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "Discord")
        r.scan()
        self.assertEqual(p.sink_of(1), DUZ)
        self.clock.advance(5)
        p.default = SPEAKERS
        r.scan()
        self.assertEqual(p.sink_of(1), SPEAKERS)

    def test_headset_comes_back(self):
        r = self.make()
        p = self.pulse
        p.default = SPEAKERS
        p.add_stream(1, "cs2", sink=SPEAKERS)
        r.scan()
        self.assertEqual(p.sink_of(1), SPEAKERS)
        p.default = HS80
        p.set_sink(1, HS80)   # stream followed the default
        r.scan()
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")
        self.assertTrue(r.status()["active"])

    def test_custom_pattern(self):
        r = self.make(rules={"genel": {"kulaklik_desen": "pci-0000_0e"}})
        self.pulse.default = SPEAKERS
        self.pulse.add_stream(1, "cs2", sink=SPEAKERS)
        r.scan()
        self.assertTrue(r.status()["active"])
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_cs2")

    def test_default_is_eq_sink(self):
        r = self.make(rules={"genel": {"kulaklik_desen": "."}})
        self.pulse.default = "oto_eq_film"
        self.pulse.add_stream(1, "cs2", sink="oto_eq_film")
        r.scan()
        self.assertFalse(r.status()["active"])
        self.assertEqual(self.pulse.moves, [])


class TestIgnore(RouterTestBase):
    def test_user_moved_stream_respected(self):
        r = self.make()
        self.pulse.add_stream(1, "cs2", sink=HDMI)
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), HDMI)
        self.assertEqual(self.pulse.moves, [])
        self.assertIn("elle", self.entry(1)["reason"])
        self.assertIsNone(self.entry(1)["profile"])

    def test_user_moves_after_us(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        r.scan()
        self.clock.advance(5)
        p.set_sink(1, HDMI)
        r.scan()
        self.assertEqual(p.sink_of(1), HDMI)
        self.assertEqual(len(p.moves), 1)

    def test_own_playback_nodes_ignored(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "pipewire", node_name="oto_eq_cs2_out")
        p.add_stream(2, "pipewire", sink="oto_eq_film", node_name="oto_eq_muzik_out")
        r.scan()
        self.assertEqual(p.moves, [])
        self.assertEqual(r.status()["streams"], [])
        self.make(mode=("off", None), pulse=p)
        self.r.scan()
        self.assertEqual(p.moves, [])


class TestMissingSinks(RouterTestBase):
    def test_missing_target_left_on_default(self):
        pulse = FakePulse(sinks=[HS80, "oto_eq_muzik", "oto_eq_film"])
        r = self.make(pulse=pulse)
        pulse.add_stream(1, "cs2")
        pulse.add_stream(2, "spotify")
        r.scan()
        self.assertEqual(pulse.sink_of(1), HS80)
        self.assertEqual(pulse.sink_of(2), "oto_eq_muzik")
        st = r.status()
        self.assertFalse(st["sinks_ready"])
        self.assertIn("oto_eq_cs2 yok", self.entry(1)["reason"])

    def test_no_eq_sinks_fixed_mode(self):
        pulse = FakePulse(sinks=[HS80])
        r = self.make(pulse=pulse, mode=("fixed", "cs2"))
        pulse.add_stream(1, "spotify")
        r.scan()
        self.assertEqual(pulse.moves, [])


class TestAntiFlap(RouterTestBase):
    def test_min_interval(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        r.scan()
        self.assertEqual(p.moves, [(1, "oto_eq_cs2")])
        p.set_sink(1, HS80)            # something undoes our move
        self.clock.advance(1.6)        # outside the verify-retry window
        r.scan()
        self.assertEqual(len(p.moves), 1)
        self.assertIn("bekliyor", self.entry(1)["reason"])
        # a retry scan is scheduled exactly at last_move + 2 s
        self.assertAlmostEqual(r._scan_at, 1002.0)
        self.clock.advance(0.2)
        r.tick()
        self.assertEqual(len(p.moves), 1)
        self.clock.advance(0.2)
        r.tick()
        self.assertEqual(len(p.moves), 2)
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")

    def test_unapplied_move_retried_once(self):
        """pactl can exit 0 without moving: verify scan retries once, then the 2 s rule."""
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        p.noop_moves = 2               # the next 2 moves "succeed" but do nothing
        r.scan()
        self.assertEqual(len(p.moves), 1)
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.VERIFY_DELAY)
        self.clock.advance(R.VERIFY_DELAY)
        r.tick()                       # verification: still on HS80 -> one retry
        self.assertEqual(len(p.moves), 2)
        self.assertEqual(p.sink_of(1), HS80)
        self.clock.advance(R.VERIFY_DELAY)
        r.tick()                       # retried already -> 2 s rule
        self.assertEqual(len(p.moves), 2)
        self.assertIn("bekliyor", self.entry(1)["reason"])
        self.clock.advance(2.0)
        r.tick()
        self.assertEqual(len(p.moves), 3)
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")

    def test_parks_after_repeated_undo(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        for _ in range(R.FLAP_MAX_MOVES):
            r.scan()
            p.set_sink(1, HS80)
            self.clock.advance(2.5)
        self.assertEqual(len(p.moves), R.FLAP_MAX_MOVES)
        r.scan()
        self.assertEqual(len(p.moves), R.FLAP_MAX_MOVES)
        self.assertIn("sürekli", self.entry(1)["reason"])
        self.clock.advance(R.FLAP_PARK + 1)
        r.scan()
        self.assertEqual(len(p.moves), R.FLAP_MAX_MOVES + 1)

    def test_mode_change_resets_park_but_not_interval(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        r.scan()
        self.clock.advance(0.5)
        r.handle_command({"cmd": "set_mode", "mode": "fixed", "profile": "film"})
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")   # 2 s rule still holds
        self.clock.advance(1.6)
        r.tick()
        self.assertEqual(p.sink_of(1), "oto_eq_film")


class TestDominant(RouterTestBase):
    def test_dominant(self):
        r = self.make()
        p = self.pulse
        r.scan()
        self.assertIsNone(r.status()["dominant"])
        p.add_stream(1, "spotify")
        r.scan()
        self.assertEqual(r.status()["dominant"], "muzik")
        p.add_stream(2, "vlc")
        r.scan()
        self.assertEqual(r.status()["dominant"], "film")
        p.add_stream(3, "cs2", corked=True)
        r.scan()
        self.assertEqual(r.status()["dominant"], "film")
        p.set_corked(3, False)
        r.scan()
        self.assertEqual(r.status()["dominant"], "cs2")
        p.add_stream(4, "game.exe")
        r.scan()
        self.assertEqual(r.status()["dominant"], "cs2")
        del p.streams[3]
        r.scan()
        self.assertEqual(r.status()["dominant"], "oyun")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oto-eq-test-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self):
        path = self.dir / "alt" / "mod.json"
        store = R.ModeStore(path)
        self.assertEqual(store.load(), ("auto", None))
        r = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier,
                     rules_loader=dict, clock=FakeClock(), mode_store=store)
        r.allow_inline = True
        r.prepare()
        r.handle_command({"cmd": "set_mode", "mode": "fixed", "profile": "oyun"})
        self.assertEqual(json.loads(path.read_text()), {"mode": "fixed", "profile": "oyun"})
        r2 = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier,
                      rules_loader=dict, clock=FakeClock(), mode_store=R.ModeStore(path))
        r2.prepare()
        self.assertEqual((r2.mode, r2.fixed_profile), ("fixed", "oyun"))
        self.assertEqual(r2.status()["mode"], "fixed")

    def test_corrupt_and_invalid(self):
        path = self.dir / "mod.json"
        path.write_text("{bozuk")
        self.assertEqual(R.ModeStore(path).load(), ("auto", None))
        path.write_text(json.dumps({"mode": "fixed", "profile": "bas"}))
        self.assertEqual(R.ModeStore(path).load(), ("auto", None))
        path.write_text(json.dumps(["off"]))
        self.assertEqual(R.ModeStore(path).load(), ("auto", None))
        path.write_text(json.dumps({"mode": "off", "profile": "cs2"}))
        self.assertEqual(R.ModeStore(path).load(), ("off", None))

    def test_default_path_is_config(self):
        self.assertEqual(R.ModeStore().path, Path(R.config.MODE_FILE))


class TestResilience(RouterTestBase):
    def test_pulse_error_backoff(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        p.fail_list = True
        self.assertFalse(r.scan())
        self.assertEqual(r._backoff, R.BACKOFF_MIN)
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.BACKOFF_MIN)
        # events during backoff do not make it retry earlier
        r.handle_item(("pulse", Event("new", "sink-input", 1)))
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.BACKOFF_MIN)
        self.clock.advance(R.BACKOFF_MIN)
        r.tick()
        self.assertEqual(r._backoff, 2 * R.BACKOFF_MIN)
        for _ in range(10):
            self.clock.advance(R.BACKOFF_MAX)
            r.tick()
        self.assertEqual(r._backoff, R.BACKOFF_MAX)
        p.fail_list = False
        self.clock.advance(R.BACKOFF_MAX)
        r.tick()
        self.assertEqual(r._backoff, 0.0)
        self.assertEqual(p.sink_of(1), "oto_eq_cs2")

    def test_move_error_does_not_stop_others(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        p.add_stream(2, "spotify")
        p.fail_move.add(1)
        self.assertTrue(r.scan())
        self.assertEqual(p.sink_of(1), HS80)
        self.assertEqual(p.sink_of(2), "oto_eq_muzik")
        self.assertIn("hata", self.entry(1)["reason"])

    def test_vanished_stream(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        orig = p.move_stream
        p.move_stream = lambda i, s: False
        r.scan()
        self.assertIsNone(self.entry(1)["profile"])
        p.move_stream = orig

    def test_rescan_reports_error(self):
        r = self.make()
        self.pulse.fail_list = True
        resp = r.handle_command({"cmd": "rescan"})
        self.assertFalse(resp["ok"])

    def test_unlinked_stream_is_rechecked_soon_not_manual(self):
        # sink_index -1 = WirePlumber has not linked it yet: not "moved by hand", and the
        # next look must not wait for the change event + CHANGE_MIN_GAP (≈250 ms un-EQ'd)
        r = self.make()
        p = self.pulse
        st = p.add_stream(7, "cs2")
        p.streams[7] = dataclasses.replace(st, sink_index=-1)
        r.handle_item(("pulse", Event("new", "sink-input", 7)))
        self.clock.advance(R.NEW_STREAM_DELAY)
        r.tick()
        self.assertEqual(p.moves, [])
        self.assertNotIn("elle", self.entry(7)["reason"])
        self.assertAlmostEqual(r._scan_at - self.clock.t, R.UNLINKED_RECHECK)
        p.streams[7] = dataclasses.replace(st, sink_index=p.sink_idx(HS80))   # WP linked it
        r.handle_item(("pulse", Event("change", "sink-input", 7)))
        self.clock.advance(R.UNLINKED_RECHECK)
        r.tick()
        self.assertEqual(p.sink_of(7), "oto_eq_cs2")

    def test_unlinked_recheck_is_bounded(self):
        r = self.make()
        p = self.pulse
        st = p.add_stream(7, "cs2")
        p.streams[7] = dataclasses.replace(st, sink_index=-1)      # never gets linked
        scans = 0
        orig = r.scan

        def counting():
            nonlocal scans
            scans += 1
            return orig()
        r.scan = counting
        r.scan()
        for _ in range(200):
            self.clock.advance(R.UNLINKED_RECHECK)
            r.tick()
        self.assertLess(scans, R.UNLINKED_WINDOW / R.UNLINKED_RECHECK + 5)
        self.assertGreater(r._next_deadline() - self.clock.t, 1.0)   # back to periodic scans
        p.streams.pop(7)
        r.scan()
        self.assertNotIn(7, r._unlinked)                            # pruned with the stream

    def test_classifier_error_reason_is_translated(self):
        from oto_eq import i18n
        self.addCleanup(i18n.sifirla)

        class Bad(FakeClassifier):
            def classify(self, stream, players):
                return "nonsense"
        r = self.make()
        r.classifier = Bad({})
        i18n.sifirla("en", {"sınıflandırma hatası: {hata}": "classification error: {hata}",
                            "beklenmeyen sonuç: {sonuc}": "unexpected result: {sonuc}"})
        self.pulse.add_stream(1, "x")
        r.scan()
        self.assertEqual(self.entry(1)["reason"], "classification error: unexpected result: "
                                                  "'nonsense'")

    def test_device_write_failure_warns_once(self):
        def bad(dev):
            raise OSError("disk dolu")
        r = self.make(device_writer=bad)
        with self.assertLogs("oto_eq.router", "DEBUG") as cm:
            for _ in range(5):
                r.scan()
        warns = [l for l in cm.output if l.startswith("WARNING") and "kulaklık durumu" in l]
        self.assertEqual(len(warns), 1)

    def test_command_wait_below_client_timeouts(self):
        # GUI requests with 1.5 s, the CLI with 2 s: "bekliyor" must reach both
        self.assertLess(R.COMMAND_WAIT, 1.5)


class TestConfigRobustness(unittest.TestCase):
    def setUp(self):
        from oto_eq import config
        self.config = config
        self.dir = Path(tempfile.mkdtemp(prefix="oto-eq-test-cfg-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(config, "ASSIGNMENTS_FILE", self.dir / "atamalar.json")
        p.start()
        self.addCleanup(p.stop)

    def test_malformed_assignments_never_break_the_rules(self):
        for body in ('{"atamalar": null}', '{"atamalar": 5}', '{"atamalar": "x"}', "[1, 2]",
                     '{"atamalar": {"a": 1}}', "bozuk"):
            (self.dir / "atamalar.json").write_text(body, "utf-8")
            self.assertEqual(self.config.load_assignments(), [], body)
            self.assertEqual(self.config.load_rules()["atama"], [], body)

    def test_empty_or_relative_xdg_dirs_fall_back(self):
        home = Path.home()
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "", "XDG_STATE_HOME": "rel/x"}):
            self.assertEqual(self.config._xdg("XDG_CONFIG_HOME", home / ".config"),
                             home / ".config")
            self.assertEqual(self.config._xdg("XDG_STATE_HOME", home / ".local/state"),
                             home / ".local/state")
        with mock.patch.dict("os.environ", {"XDG_CONFIG_HOME": "/tmp/xdg-c"}):
            self.assertEqual(self.config._xdg("XDG_CONFIG_HOME", home / ".config"),
                             Path("/tmp/xdg-c"))


class TestMainSingleInstance(unittest.TestCase):
    def test_second_instance_refuses_before_touching_anything(self):
        # 6 Kas: kisma.restore_on_start() ran BEFORE the socket check → a manual second
        # router undid the live daemon's ducking mid-call
        from oto_eq import ipc, kisma
        with mock.patch.object(ipc, "socket_in_use", return_value=True), \
                mock.patch.object(R, "_setup_logging"), \
                mock.patch.object(kisma, "Ducker") as ducker, \
                mock.patch.object(R, "Router") as router:
            self.assertEqual(R.main([]), 1)
        ducker.assert_not_called()
        router.assert_not_called()


BT = "bluez_output.AA_BB_CC_DD_EE_FF.1"


def _info(bus="", form="", port="", ptype="", desc="", api="alsa", profile=""):
    props = {"device.bus": bus, "device.api": api, "device.description": desc}
    if form:
        props["device.form_factor"] = form
    if profile:
        props["api.bluez5.profile"] = profile
    ports = [{"name": port, "type": ptype, "availability": "available"}] if port else []
    return {"properties": props, "ports": ports, "active_port": port or None}


class TestHeadphoneTypes(RouterTestBase):
    """CHANGE 7: any headphone turns the EQ on; the correction follows its model."""

    def setUp(self):
        self.applied, self.written = [], []

    def mk(self, rules=None, apply_ok=True, writer_fails=False):
        def apply(groups):
            self.applied.append(dict(groups))
            return apply_ok

        def writer(dev):
            if writer_fails:
                raise OSError("disk dolu")
            self.written.append((dev.ad, dev.tur, dev.duzeltme))
        r = self.make(rules=rules, apply_correction=apply, device_writer=writer)
        self.pulse.add_sink(BT)
        self.set_info(BT, _info("bluetooth", "headphone", api="bluez5", desc="WH-1000XM4",
                                profile="a2dp-sink"))
        self.set_info(HS80, _info("usb", port="analog-output-headphones", ptype="Headphones",
                                  desc="CORSAIR HS80 RGB Wireless Gaming Receiver"))
        self.set_info(SPEAKERS, _info("pci", port="analog-output-lineout", ptype="Line",
                                      desc="Ryzen HD Audio Controller"))
        return r

    def set_info(self, name, info):
        self.pulse.sinks = [Sink(s.index, s.name, s.description, s.state, info=info)
                            if s.name == name else s for s in self.pulse.sinks]

    def test_bluetooth_headphone_is_active_without_hs80_correction(self):
        r = self.mk()
        self.pulse.default = BT
        self.pulse.add_stream(1, "cs2", sink=BT)
        r.scan()
        st = r.status()
        self.assertTrue(st["active"])
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_cs2")
        self.assertEqual((st["kulaklik"]["tur"], st["kulaklik"]["duzeltme"],
                          st["kulaklik"]["bt_profil"]), ("bluetooth", False, "a2dp-sink"))
        self.assertEqual(self.written, [(BT, "bluetooth", False)])

    def test_jack_port_switch_turns_eq_on_and_off(self):
        r = self.mk()
        self.pulse.default = SPEAKERS
        r.scan()
        self.assertFalse(r.status()["active"])
        self.assertEqual(r.status()["kulaklik"]["tur"], "hoparlor")
        self.set_info(SPEAKERS, _info("pci", port="analog-output-headphones",
                                      ptype="Headphones", desc="Ryzen HD Audio Controller"))
        r.scan()                                  # same sink name, new active port
        self.assertTrue(r.status()["active"])
        self.assertEqual(r.status()["kulaklik"]["tur"], "jak")

    def test_hdmi_and_speakers_stay_off(self):
        r = self.mk()
        for name in (HDMI, SPEAKERS):
            self.pulse.default = name
            r.scan()
            self.assertFalse(r.status()["active"], name)
        self.assertEqual(self.applied, [])        # EQ bypassed: correction untouched
        self.assertEqual(self.written, [])

    def test_exclude_and_pattern_only_mode(self):
        r = self.mk(rules={"genel": {"kulaklik_desen": "(?i)hs80",
                                     "kulaklik_haric": "(?i)wh-1000"}})
        self.pulse.default = BT
        r.scan()
        self.assertFalse(r.status()["active"])
        self.assertIn("kulaklik_haric", r.status()["kulaklik"]["neden"])
        r = self.mk(rules={"genel": {"kulaklik_desen": "(?i)hs80", "kulaklik_otomatik": False}})
        self.pulse.default = BT
        r.scan()
        self.assertFalse(r.status()["active"])    # old rule: pattern only
        self.pulse.default = HS80
        r.scan()
        self.assertTrue(r.status()["active"])

    def test_invalid_optional_pattern_is_ignored_not_fatal(self):
        # H1: an invalid kulaklik_haric / cihaz_desen used to drop ALL rules (everything EQ-less)
        self.mk()
        self.rules = {"genel": {"kulaklik_haric": "(bad"}}
        with self.assertLogs("oto_eq.router", "WARNING") as cm:
            self.assertIsNone(self.r.load_rules())
        self.assertIn("kulaklik_haric", "\n".join(cm.output))
        self.assertIsNone(self.r.kulaklik_ayar.haric)
        self.assertIsInstance(self.r.classifier, FakeClassifier)    # rules really loaded

    def test_partial_failure_reapplies_on_return_to_previous_headphone(self):
        r = self.mk()
        r.scan()                                   # HS80: applied (True)
        self.pulse.default = BT
        self.apply_ok = False
        r.apply_correction = lambda g: self.applied.append(dict(g)) or False
        r.scan()                                   # BT ramp fails half-way
        r.apply_correction = lambda g: self.applied.append(dict(g)) or True
        self.pulse.default = HS80
        r.scan()                                   # back on HS80 at once: must re-apply
        self.assertEqual(len(self.applied), 3)

    def test_correction_follows_the_headphone(self):
        r = self.mk()
        r.scan()                                   # HS80 (default of FakePulse)
        self.assertEqual(self.written, [(HS80, "usb", True)])
        self.assertEqual(len(self.applied), 1)
        r.scan()
        self.assertEqual(len(self.applied), 1)     # nothing changed: no ramp
        self.pulse.default = BT
        r.scan()
        self.assertEqual(self.written[-1], (BT, "bluetooth", False))
        self.assertEqual(len(self.applied), 2)
        self.assertEqual(set(self.applied[-1]), {"muzik", "oyun"})   # active genres passed
        self.pulse.default = SPEAKERS
        r.scan()
        self.assertEqual(len(self.applied), 2)     # speakers: EQ bypassed, no ramp
        self.pulse.default = BT
        r.scan()
        self.assertEqual(len(self.applied), 2)     # back on BT: live sinks are still "off"
        self.pulse.sinks = [Sink(s.index + 1000, s.name, s.description, s.state, info=s.info)
                            if s.name in EQ_SINKS else s
                            for s in self.pulse.sinks]   # sinks service restarted: new indices
        r.scan()
        self.assertEqual(len(self.applied), 3)

    def test_failed_ramp_is_retried_later_and_writer_failure_blocks_it(self):
        r = self.mk(apply_ok=False)
        r.scan()
        r.scan()
        self.assertEqual(len(self.applied), 1)     # no retry storm
        self.clock.advance(R.CORRECTION_RETRY + 0.1)
        r.scan()
        self.assertEqual(len(self.applied), 2)
        self.applied.clear()
        r = self.mk(writer_fails=True)
        with self.assertLogs("oto_eq.router", "WARNING"):
            r.scan()
        self.assertEqual(self.applied, [])         # eqgen would read a stale device state


class TestScheduling(RouterTestBase):
    def test_new_stream_fast_and_coalesced(self):
        r = self.make()
        r.scan()
        p = self.pulse
        p.add_stream(1, "cs2")
        r.handle_item(("pulse", Event("new", "sink-input", 1)))
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.NEW_STREAM_DELAY)
        for _ in range(20):   # burst does not extend the deadline
            self.clock.advance(0.001)
            r.handle_item(("pulse", Event("change", "sink-input", 1)))
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.NEW_STREAM_DELAY)
        self.clock.advance(0.001)
        r.tick()
        self.assertEqual(p.moves, [(1, "oto_eq_cs2")])
        self.assertAlmostEqual(r._scan_at, r.clock() + R.VERIFY_DELAY)   # verify scan

    def test_irrelevant_events_ignored(self):
        r = self.make()
        r.scan()
        for ev in (Event("change", "sink", 1), Event("new", "source", 2),
                   Event("new", "source-output", 4)):
            r.handle_item(("pulse", ev))
        self.assertIsNone(r._scan_at)
        # CHANGE 7: a card change may be a jack plug (active port) -> rate-limited rescan
        r.handle_item(("pulse", Event("change", "card", 3)))
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.CHANGE_MIN_GAP)
        for ev in (Event("change", "server", -1), Event("new", "sink", 9),
                   Event("remove", "sink", 9)):
            r._scan_at = None
            r.handle_item(("pulse", ev))
            self.assertAlmostEqual(r._scan_at, 1000.0 + R.COALESCE_DELAY)

    def test_change_events_rate_limited(self):
        r = self.make()
        r.scan()                                   # t = 1000
        for kind in ("change", "remove"):
            r._scan_at = None
            r.handle_item(("pulse", Event(kind, "sink-input", 1)))
            self.assertAlmostEqual(r._scan_at, 1000.0 + R.CHANGE_MIN_GAP)
        self.clock.advance(1.0)
        r._scan_at = None
        r.handle_item(("pulse", Event("change", "sink-input", 1)))
        self.assertAlmostEqual(r._scan_at, 1001.0 + R.COALESCE_DELAY)
        # "new" is never rate limited
        self.clock.t = 1000.0
        r.scan()
        r.handle_item(("pulse", Event("new", "sink-input", 2)))
        self.assertAlmostEqual(r._scan_at, 1000.0 + R.NEW_STREAM_DELAY)

    def test_mpris_debounce(self):
        r = self.make()
        r.scan()
        r.handle_item(("mpris", None))
        self.assertAlmostEqual(r._mpris_at, 1001.0)
        self.clock.advance(0.8)
        r.handle_item(("mpris", None))
        self.assertAlmostEqual(r._mpris_at, 1001.8)
        for _ in range(5):
            self.clock.advance(0.8)
            r.handle_item(("mpris", None))
        self.assertAlmostEqual(r._mpris_at, 1000.0 + R.MPRIS_MAX_DELAY)
        self.clock.t = 1000.0 + R.MPRIS_MAX_DELAY
        n = len(self.pulse.moves)
        self.pulse.add_stream(1, "vlc")
        r.tick()
        self.assertEqual(len(self.pulse.moves), n + 1)
        self.assertIsNone(r._mpris_at)

    def test_periodic(self):
        r = self.make()
        r.scan()
        self.assertAlmostEqual(r._next_deadline(), 1000.0 + R.PERIODIC_INTERVAL)
        self.pulse.add_stream(1, "spotify")
        self.clock.advance(9.9)
        r.tick()
        self.assertEqual(self.pulse.moves, [])
        self.clock.advance(0.1)
        r.tick()
        self.assertEqual(self.pulse.moves, [(1, "oto_eq_muzik")])

    def test_status_listener_only_on_change(self):
        r = self.make()
        got = []
        r.add_status_listener(got.append)
        r.scan()
        n = len(got)
        r.scan()
        self.assertEqual(len(got), n)
        self.pulse.add_stream(1, "spotify")
        r.scan()
        self.assertEqual(len(got), n + 1)
        self.assertEqual(got[-1]["dominant"], "muzik")
        r.scan()
        self.assertEqual(len(got), n + 1)
        self.clock.advance(5)
        r.handle_command({"cmd": "set_mode", "mode": "off"})
        self.assertEqual(got[-1]["mode"], "off")


class TestMover(RouterTestBase):
    def test_custom_mover_gets_default_flag(self):
        calls = []

        def mover(st, sink, to_default):
            calls.append((st.index, sink, to_default))
            return self.pulse.move_stream(st.index, sink)
        r = self.make(mover=mover)
        self.pulse.add_stream(1, "cs2")
        self.pulse.add_stream(2, "Discord", sink="oto_eq_cs2")
        r.scan()
        self.clock.advance(5)
        self.pulse.default = SPEAKERS
        r.scan()
        self.assertIn((1, "oto_eq_cs2", False), calls)
        self.assertIn((2, DUZ, False), calls)
        self.assertIn((1, SPEAKERS, True), calls)
        self.assertEqual(self.pulse.sink_of(1), SPEAKERS)

    def test_metadata_mover(self):
        runs = []

        class P:
            def __init__(self, rc):
                self.returncode, self.stderr = rc, "hata"
        rc = [0]

        def run(cmd, **kw):
            runs.append(cmd)
            self.assertEqual(kw["env"]["LC_ALL"], "C")
            self.assertIn("timeout", kw)
            return P(rc[0])
        fb = []
        m = R.MetadataMover(fallback=lambda i, s: fb.append((i, s)) or True, run=run)
        st = Stream(7, 1, "a", "b", None, "", "", "", False, {"object.id": "123"})
        self.assertTrue(m(st, "oto_eq_cs2", False))
        self.assertEqual(runs[-1], ["pw-metadata", "-n", "default", "--", "123",
                                    "target.object", "oto_eq_cs2", "Spa:String"])
        self.assertTrue(m(st, HS80, True))
        self.assertEqual(runs[-1][-2:], ["-1", "Spa:Id"])
        self.assertEqual(fb, [])
        rc[0] = 1                                  # pw-metadata fails -> pactl fallback
        self.assertTrue(m(st, "oto_eq_film", False))
        self.assertEqual(fb, [(7, "oto_eq_film")])
        n = len(runs)                              # no node id -> straight to fallback
        st2 = Stream(8, 1, "a", "b", None, "", "", "", False, {})
        self.assertTrue(m(st2, "oto_eq_film", False))
        self.assertEqual(len(runs), n)
        self.assertEqual(fb[-1], (8, "oto_eq_film"))

        def boom(cmd, **kw):
            raise FileNotFoundError("pw-metadata yok")
        self.assertFalse(R.MetadataMover(run=boom)(st, "oto_eq_cs2", False))


class FakeHistory:
    def __init__(self, fail=False):
        self.records = []
        self.flushes = 0
        self.fail = fail

    def record(self, stream, decision):
        if self.fail:
            raise RuntimeError("disk dolu")
        self.records.append((stream.index, decision.category))

    def flush(self):
        self.flushes += 1
        if self.fail:
            raise OSError("disk dolu")


class TestHistory(RouterTestBase):
    def test_records_classified_streams(self):
        h = FakeHistory()
        r = self.make(history=h)
        self.pulse.add_stream(1, "cs2")
        self.pulse.add_stream(2, "Discord", sink=HDMI)          # user-moved: still seen
        self.pulse.add_stream(3, "pipewire", node_name="oto_eq_cs2_out")   # ours: never
        r.scan()
        self.assertEqual(sorted(h.records), [(1, Category.CS2), (2, Category.VOICE)])
        r.stop()
        self.assertEqual(h.flushes, 1)

    def test_history_errors_ignored(self):
        h = FakeHistory(fail=True)
        r = self.make(history=h)
        self.pulse.add_stream(1, "cs2")
        self.assertTrue(r.scan())
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_cs2")
        r.stop()   # flush error is logged, not raised

    def test_no_history_by_default(self):
        r = self.make()
        self.assertIsNone(r.history)
        self.pulse.add_stream(1, "cs2")
        self.assertTrue(r.scan())
        r.stop()


class TestStopRestore(RouterTestBase):
    def test_stop_moves_eq_streams_back(self):
        calls = []

        def mover(st, sink, to_default):
            calls.append((st.index, sink, to_default))
            return self.pulse.move_stream(st.index, sink)
        r = self.make(mover=mover)
        p = self.pulse
        p.add_stream(1, "cs2")
        p.add_stream(2, "Discord")
        p.add_stream(3, "x", sink=HDMI)                        # user-moved: untouched
        p.add_stream(4, "pipewire", node_name="oto_eq_cs2_out")
        r.scan()
        calls.clear()
        r.stop()
        self.assertEqual(p.sink_of(1), HS80)
        self.assertEqual(p.sink_of(2), HS80)                   # from duz
        self.assertEqual(p.sink_of(3), HDMI)
        self.assertEqual(p.sink_of(4), HS80)
        self.assertEqual(sorted(calls), [(1, HS80, True), (2, HS80, True)])

    def test_stop_without_restore(self):
        r = self.make()
        self.pulse.add_stream(1, "cs2")
        r.scan()
        r.stop(restore=False)
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_cs2")

    def test_stop_restore_tolerates_errors_and_is_bounded(self):
        r = self.make()
        p = self.pulse
        p.add_stream(1, "cs2")
        r.scan()
        p.fail_list = True
        r.stop()                                               # logged, no raise
        p.fail_list = False
        orig = p.move_stream

        def slow(i, s):
            time.sleep(5)
            return orig(i, s)
        r2 = self.make(pulse=p)
        p.move_stream = slow
        t0 = time.monotonic()
        r2.stop()
        self.assertLess(time.monotonic() - t0, R.RESTORE_BUDGET + 1.0)


class TestNoWorker(unittest.TestCase):
    def router(self):
        r = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier, rules_loader=dict,
                     mode_store=MemStore())
        r.prepare()
        return r

    def test_refuses_without_worker(self):
        r = self.router()
        for cmd in ({"cmd": "set_mode", "mode": "off"}, {"cmd": "reload"}, {"cmd": "rescan"}):
            resp = r.handle_command(cmd)
            self.assertFalse(resp["ok"])
            self.assertIn("hazır değil", resp["error"])
        self.assertEqual(r.mode, "auto")
        self.assertTrue(r.handle_command({"cmd": "status"})["ok"])   # status always works

    def test_refuses_after_stop(self):
        r = self.router()
        r.start()
        self.assertTrue(r.handle_command({"cmd": "rescan"})["ok"])
        r.stop()
        self.assertFalse(r.handle_command({"cmd": "set_mode", "mode": "off"})["ok"])
        self.assertEqual(r.mode, "auto")

    def test_timeout_is_not_ok(self):
        pulse = FakePulse()
        r = R.Router(pulse=pulse, classifier_factory=FakeClassifier, rules_loader=dict,
                     mode_store=MemStore())
        r.start()
        try:
            time.sleep(0.1)
            gate = threading.Event()
            orig = pulse.list_streams
            pulse.list_streams = lambda: (gate.wait(5), orig())[1]     # worker hangs in pactl
            old = R.COMMAND_WAIT
            R.COMMAND_WAIT = 0.2
            try:
                r.request_rescan()
                time.sleep(0.05)
                resp = r.handle_command({"cmd": "set_mode", "mode": "off"})
            finally:
                R.COMMAND_WAIT = old
                gate.set()
            self.assertFalse(resp["ok"])
            self.assertTrue(resp.get("bekliyor"))
            t0 = time.monotonic()                # it still runs afterwards
            while r.mode != "off" and time.monotonic() - t0 < 2:
                time.sleep(0.01)
            self.assertEqual(r.mode, "off")
        finally:
            pulse.list_streams = orig
            r.stop()


class TestStaleStatus(RouterTestBase):
    def test_status_blanked_after_repeated_errors(self):
        r = self.make()
        self.pulse.add_stream(1, "cs2")
        r.scan()
        self.assertTrue(r.status()["active"])
        self.pulse.fail_list = True
        for _ in range(R.STATUS_STALE_AFTER - 1):
            r.scan()
        self.assertTrue(r.status()["active"])              # one blip does not blank it
        r.scan()
        st = r.status()
        self.assertEqual((st["active"], st["sinks_ready"], st["dominant"], st["streams"]),
                         (False, False, None, []))
        self.pulse.fail_list = False
        r.scan()
        self.assertTrue(r.status()["active"])

    def test_mpris_used_in_every_mode(self):
        r = self.make(mode=("fixed", "cs2"))
        self.mpris.list = [MediaPlayer("org.mpris.MediaPlayer2.x", 1, "X", "Playing", "", "", "")]
        self.pulse.add_stream(1, "vlc")
        r.scan()
        self.assertEqual(len(r.classifier.seen_players), 1)
        self.pulse.default = SPEAKERS
        r.scan()
        self.assertEqual(len(r.classifier.seen_players), 1)


class TestThreaded(unittest.TestCase):
    """Real worker thread, real clock: event -> move latency and clean shutdown."""

    def test_latency_and_shutdown(self):
        pulse = FakePulse()
        mpris = FakeMpris()
        r = R.Router(pulse=pulse, classifier_factory=FakeClassifier, mpris=mpris,
                     rules_loader=dict, mode_store=MemStore())
        r.start()
        try:
            self.assertTrue(mpris.started)
            self.assertEqual(len(pulse.listeners), 1)
            time.sleep(0.1)  # initial scan
            pulse.add_stream(7, "cs2")
            t0 = time.monotonic()
            pulse.emit(Event("new", "sink-input", 7))
            while pulse.sink_of(7) != "oto_eq_cs2" and time.monotonic() - t0 < 1.0:
                time.sleep(0.002)
            dt = time.monotonic() - t0
            self.assertEqual(pulse.sink_of(7), "oto_eq_cs2")
            self.assertLess(dt, 0.1)
            # IPC-style command goes through the worker and waits for it
            self.assertEqual(r.handle_command({"cmd": "set_mode", "mode": "fixed",
                                               "profile": "film"}), {"ok": True})
            self.assertEqual(r.status()["mode"], "fixed")
            r.notify_mpris()
            r.request_rescan()
        finally:
            r.stop()
        self.assertFalse(r.is_alive())
        self.assertTrue(mpris.stopped)
        self.assertFalse(pulse.listeners[0].running)

    def test_listener_start_failure_tolerated(self):
        pulse = FakePulse()

        class Broken:
            def __init__(self, cb):
                raise OSError("pactl yok")
        pulse.EventListener = Broken
        r = R.Router(pulse=pulse, classifier_factory=FakeClassifier, rules_loader=dict,
                     mode_store=MemStore())
        r.start()
        try:
            pulse.add_stream(1, "spotify")
            r.request_rescan()
            t0 = time.monotonic()
            while pulse.sink_of(1) != "oto_eq_muzik" and time.monotonic() - t0 < 1.0:
                time.sleep(0.005)
            self.assertEqual(pulse.sink_of(1), "oto_eq_muzik")
        finally:
            r.stop()


# ----------------------------------------------------------------------
# HS80 takibi (A2): wired <-> wireless follow

HS80_WL = HS80
HS80_USB = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_USB_Gaming_Headset_1c547fbe-00.analog-stereo"
MIC_WL = "alsa_input.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver-00.mono-fallback"
MIC_USB = "alsa_input.usb-Corsair_CORSAIR_HS80_RGB_USB_Gaming_Headset_1c547fbe-00.mono-fallback"
MB_MIC = "alsa_input.pci-0000_0e_00.6.analog-stereo"
VMIC = "oto_eq_mikrofon"


class Hs80Pulse(FakePulse):
    """FakePulse + volumes, sources and default setters (records calls in order)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.vol = {HS80_WL: 19661}              # 30 %
        self.sources = [MIC_WL, MB_MIC, HS80_WL + ".monitor", VMIC]
        self.default_source = MIC_WL
        self.calls: list[tuple] = []
        self.fail_set_volume = False

    def unplug_wireless_plug_cable(self, sink_vol=52705, mic_vol=221184):
        self.remove_sink(HS80_WL)
        self.sources = [s for s in self.sources if "Wireless" not in s]
        self.default = SPEAKERS                  # what WirePlumber falls back to
        if self.default_source != VMIC:
            self.default_source = MB_MIC

    def appear_wired(self, sink_vol=52705, mic_vol=221184):
        self.add_sink(HS80_USB)
        self.sources += [MIC_USB, HS80_USB + ".monitor"]
        self.vol[HS80_USB] = sink_vol            # WP restores a loud stored route volume
        self.vol[MIC_USB] = mic_vol

    def get_sink_volume(self, name):
        return self.vol.get(name)

    def get_source_volume(self, name):
        return self.vol.get(name)

    def set_sink_volume(self, name, raw):
        self.calls.append(("vol", name, raw))
        if self.fail_set_volume:
            return False
        self.vol[name] = raw
        return True

    set_source_volume = set_sink_volume

    def set_default_sink(self, name):
        self.calls.append(("default_sink", name))
        self.default = name
        return True

    def set_default_source(self, name):
        self.calls.append(("default_source", name))
        self.default_source = name
        return True

    def default_source_name(self):
        return self.default_source

    def list_source_names(self):
        return list(self.sources)


class TestHs80Takip(RouterTestBase):
    def setUp(self):
        self.retargets: list[str] = []

    def make_hs(self, rules=None, **pulse_kw):
        p = Hs80Pulse(**pulse_kw)
        r = self.make(rules=rules, pulse=p, mic_retarget=lambda s: self.retargets.append(s) or s)
        p.vol[MIC_WL] = 59367                     # 91 %
        r.scan()                                 # remember wireless sink + mic volumes
        self.clock.advance(1)
        return r, p

    def test_wireless_to_wired(self):
        r, p = self.make_hs()
        p.add_stream(1, "spotify")
        r.scan()
        self.assertEqual(p.sink_of(1), "oto_eq_muzik")
        p.unplug_wireless_plug_cable()
        self.clock.advance(0.5)
        r.scan()                                  # loss noticed; default = speakers
        self.assertEqual(p.calls, [])
        p.appear_wired()
        self.clock.advance(2)
        r.scan()
        # volume FIRST (30 %, not the loud 80 %), then the default
        self.assertEqual(p.calls[:2], [("vol", HS80_USB, 19661), ("default_sink", HS80_USB)])
        self.assertEqual(p.default, HS80_USB)
        self.assertIn(("vol", MIC_USB, 59367), p.calls)
        self.assertEqual(p.default_source, MIC_USB)
        self.assertEqual(self.retargets, [MIC_USB])
        st = r.status()
        self.assertTrue(st["active"])            # same scan already routes with the new default
        self.assertIn(HS80_USB, st["hs80_takip_son"])
        self.assertEqual(p.sink_of(1), "oto_eq_muzik")

    def test_wired_to_wireless(self):
        p = Hs80Pulse(sinks=[HS80_USB, SPEAKERS, HDMI, *EQ_SINKS], default=HS80_USB)
        p.vol = {HS80_USB: 13107}                # 20 %
        p.sources = [MIC_USB, MB_MIC]
        p.default_source = MIC_USB
        r = self.make(pulse=p, mic_retarget=lambda s: self.retargets.append(s))
        r.scan()
        self.clock.advance(1)
        p.remove_sink(HS80_USB)
        p.sources = [MB_MIC]
        p.default = SPEAKERS
        r.scan()
        p.add_sink(HS80_WL)
        p.vol[HS80_WL] = 60000
        self.clock.advance(3)
        r.scan()
        self.assertEqual(p.calls[:2], [("vol", HS80_WL, 13107), ("default_sink", HS80_WL)])

    def test_user_changed_default_is_respected(self):
        r, p = self.make_hs()
        p.unplug_wireless_plug_cable()
        r.scan()                                  # fallback = speakers
        p.default = HDMI                          # the user picked HDMI on purpose
        self.clock.advance(1)
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertNotIn(("default_sink", HS80_USB), p.calls)
        self.assertEqual(p.default, HDMI)
        self.assertIn("elle", r.status()["hs80_takip_son"])

    def test_stale_old_name_is_not_a_user_choice(self):
        r, p = self.make_hs()
        p.remove_sink(HS80_WL)                    # default still reports the vanished node
        r.scan()
        p.default = SPEAKERS
        self.clock.advance(0.3)
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertEqual(p.default, HS80_USB)

    def test_timeout(self):
        r, p = self.make_hs()
        p.unplug_wireless_plug_cable()
        r.scan()
        self.clock.advance(R.HS80_WINDOW + 1)
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertEqual(p.calls, [])
        self.assertEqual(p.default, SPEAKERS)

    def test_volume_unknown_uses_safe_40(self):
        r, p = self.make_hs()
        r._hs80["sink"].vol = None
        p.vol.pop(HS80_WL)                        # unreadable
        p.unplug_wireless_plug_cable()
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertEqual(p.calls[0], ("vol", HS80_USB, R.HS80_SAFE_VOLUME))
        self.assertEqual(p.calls[1], ("default_sink", HS80_USB))

    def test_volume_clamped_to_100_and_failure_blocks_default(self):
        r, p = self.make_hs()
        r._hs80["sink"].vol = 90000               # >100 % remembered -> clamp
        p.unplug_wireless_plug_cable()
        r.scan()
        p.fail_set_volume = True
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertEqual(p.calls[0], ("vol", HS80_USB, 65536))
        self.assertNotIn(("default_sink", HS80_USB), p.calls)   # no volume -> no switch
        self.assertEqual(p.default, SPEAKERS)

    def test_wp_already_switched_only_lowers_volume(self):
        r, p = self.make_hs()
        p.unplug_wireless_plug_cable()
        r.scan()
        p.appear_wired()
        p.default = HS80_USB                      # WP/user picked the wired HS80 itself
        self.clock.advance(1)
        r.scan()
        self.assertIn(("vol", HS80_USB, 19661), p.calls)        # clamped down from 80 %
        self.assertNotIn(("default_sink", HS80_USB), p.calls)
        # a quieter new device is never raised
        p2r, p2 = self.make_hs()
        p2.unplug_wireless_plug_cable()
        p2r.scan()
        p2.appear_wired(sink_vol=6553)
        p2.default = HS80_USB
        self.clock.advance(1)
        p2r.scan()
        self.assertNotIn(("vol", HS80_USB, 19661), p2.calls)

    def test_virtual_mic_default_kept(self):
        r, p = self.make_hs()
        p.default_source = VMIC
        self.clock.advance(R.HS80_SOURCE_POLL)
        r.scan()                                  # tracks the HS80 mic behind the virtual mic
        p.unplug_wireless_plug_cable()
        self.clock.advance(R.HS80_SOURCE_POLL)
        r.scan()
        p.appear_wired()
        self.clock.advance(R.HS80_SOURCE_POLL)
        r.scan()
        self.assertEqual(p.default_source, VMIC)
        self.assertNotIn(("default_source", MIC_USB), p.calls)
        self.assertEqual(self.retargets, [MIC_USB])

    def test_disabled_by_rules(self):
        r, p = self.make_hs(rules={"genel": {"kulaklik_desen": "(?i)hs80", "hs80_takip": False}})
        p.unplug_wireless_plug_cable()
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertEqual(p.calls, [])

    def test_volume_change_event_refreshes_memory(self):
        r, p = self.make_hs()
        p.vol[HS80_WL] = 6553                     # user turned it down to 10 %
        r.handle_item(("pulse", Event("change", "sink", 1)))
        self.clock.advance(R.HS80_VOL_DEBOUNCE)
        r.tick()
        self.assertEqual(r._hs80["sink"].vol, 6553)

    def test_hs80_not_in_use_is_ignored(self):
        r, p = self.make_hs()
        p.default = SPEAKERS                       # user listens on speakers, HS80 idle
        self.clock.advance(1)
        r.scan()
        p.unplug_wireless_plug_cable()
        r.scan()
        p.appear_wired()
        self.clock.advance(1)
        r.scan()
        self.assertNotIn(("default_sink", HS80_USB), p.calls)
        self.assertEqual(p.default, SPEAKERS)

    def test_parse_volume(self):
        from oto_eq import pulse
        txt = ("Volume: front-left: 19661 /  30% / -31.37 dB,   front-right: 20000 /  31% / -31 dB\n"
               "        balance 0.00")
        self.assertEqual(pulse.parse_volume(txt), 20000)
        self.assertEqual(pulse.parse_volume("Volume: mono: 59367 /  91% / -2.58 dB"), 59367)
        self.assertIsNone(pulse.parse_volume("garbage"))

    def test_plain_fake_pulse_without_helpers_is_harmless(self):
        r = self.make()                            # FakePulse: no volume/default setters
        self.pulse.remove_sink(HS80)
        self.pulse.default = SPEAKERS
        self.assertTrue(r.scan())
        self.pulse.add_sink(HS80_USB)
        self.clock.advance(1)
        self.assertTrue(r.scan())
        self.assertEqual(self.pulse.default, SPEAKERS)



class TestCeviri(RouterTestBase):
    """CHANGE 8: status reasons / HS80 notes reach the status in the UI language."""
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_status(self):
        from oto_eq import i18n
        r = self.make(mode=("off", None))
        self.pulse.add_stream(1, "spotify")
        i18n.sifirla("de", {"mod kapalı": "Modus aus",
                            "çıkış: varsayılan elle değiştirildi ({ad}), dokunulmuyor":
                            "Ausgang: manuell geändert ({ad})"})
        r.scan()
        self.assertEqual(self.entry(1)["reason"], "Modus aus")
        with self.assertLogs("oto_eq.router", "INFO") as cm:
            r._hs80_note(R._HS80_MSG["elle"]["sink"], kind="sink", ad="X")
        r.scan()
        self.assertEqual(r.status()["hs80_takip_son"], "Ausgang: manuell geändert (X)")
        self.assertIn("varsayılan elle değiştirildi (X)", cm.output[0])   # journal stays Turkish


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# CHANGE 3: genre variants

from oto_eq.models import ContentId, GenreResult  # noqa: E402


class FakeResolver:
    def __init__(self):
        self.cache: dict[str, GenreResult] = {}
        self.requests: list = []
        self.stopped = False

    def cached(self, cid):
        return self.cache.get(cid.key)

    def request(self, cid, callback):
        self.requests.append((cid, callback))
        return True

    def set_settings(self, settings):
        self.settings = settings

    def set_title_hints(self, hints):
        self.hints = hints

    def invalidate(self):
        self.invalidated = True

    def answer(self, i, result, cache=True):
        cid, cb = self.requests[i]
        if cache and result is not None:
            self.cache[cid.key] = result
        cb(result)

    def stop(self):
        self.stopped = True


class TestVariants(RouterTestBase):
    def setUp(self):
        self.cids: dict[int, ContentId | None] = {}
        self.applied: list[tuple] = []
        self.resolver = FakeResolver()

    def mk(self, store=None, **kw):
        def content_id(st, players, proc=None):
            return self.cids.get(st.index)

        def apply(profile, group, ramp_s=None):
            self.applied.append((profile, group, ramp_s))
            return True
        kw.setdefault("content_id", content_id)
        kw.setdefault("genre_resolver", self.resolver)
        kw.setdefault("apply_variant", apply)
        r = self.make(**kw)
        if store is not None:
            r.mode_store = store
        return r

    def song(self, idx, artist, title):
        self.cids[idx] = ContentId("muzik", "sanatci:" + artist.lower(), artist, title)

    def game(self, idx, key, name):
        self.cids[idx] = ContentId("oyun", key, name)

    def step(self, dt):
        self.clock.advance(dt)
        self.r.tick()

    def drain(self):
        while True:
            try:
                item = self.r._q.get_nowait()
            except Exception:
                return
            self.r.handle_item(item)

    def var(self, p="muzik"):
        return self.r.status()["variants"][p]

    def test_cached_applied_after_settle(self):
        r = self.mk()
        self.resolver.cache["sanatci:virgin black"] = GenreResult("metal_rock",
                                                                  "MusicBrainz: gothic metal")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "Virgin Black", "Of Your Beauty")
        r.scan()                                   # boundary detected, not applied yet
        self.assertEqual(self.applied, [])
        self.assertIsNotNone(r._scan_at)            # settle (and verify) scans pending
        self.step(0.5)
        self.assertEqual(self.applied, [])
        logger = logging.getLogger("oto_eq.router")
        logger.setLevel(logging.INFO)
        try:
            with self.assertLogs("oto_eq.router", "INFO") as cm:
                self.step(0.5)
        finally:
            logger.setLevel(logging.NOTSET)
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])
        self.assertIn("müzik türü: Metal / Rock (Virgin Black) — MusicBrainz: gothic metal",
                      "\n".join(cm.output))
        self.assertEqual(self.var(), {"group": "metal_rock", "label": "Metal / Rock",
                                      "for": "Virgin Black", "key": "sanatci:virgin black",
                                      "source": "MusicBrainz: gothic metal",
                                      "mode": "auto", "track": "Of Your Beauty", "ses": None})
        self.assertEqual(self.var("oyun")["group"], "")

    def test_request_refused_is_not_waiting(self):
        r = self.mk()
        self.resolver.request = lambda cid, cb: False        # offline / recently failed
        self.pulse.add_stream(1, "spotify")
        self.song(1, "Nobody", "t1")
        r.scan()
        self.step(1.0)
        self.assertFalse(r.variants.state["muzik"].waiting)
        self.assertEqual(self.applied, [])                  # old variant stays

    def test_reload_pushes_settings_and_redecides(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("pop_vokal", "x")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("muzik", "pop_vokal", R.VARIANT_RAMP)])
        # the user assigns "A" to Metal / Rock in the GUI, which then sends "reload"
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "kullanıcı", 1.0)
        with mock.patch.object(R.config, "load_genre_settings",
                               return_value={"cevrimici": True, "sanatci": {"A": "metal_rock"}}):
            self.assertTrue(r._reload()["ok"])
        self.assertEqual(self.resolver.settings["sanatci"], {"A": "metal_rock"})
        self.assertTrue(self.resolver.invalidated)
        self.step(0.0)
        self.assertEqual(self.applied[-1], ("muzik", "metal_rock", R.VARIANT_RAMP))
        self.assertEqual(self.var()["key"], "sanatci:a")

    def test_sinks_restart_reapplies_variant(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])
        r.scan()
        self.step(1.0)
        self.assertEqual(len(self.applied), 1)              # same sink: nothing to do
        # oto-eq-sinks restarted: the muzik sink comes back with a new index, slots at 0 dB
        self.pulse.sinks = [dataclasses.replace(x, index=900) if x.name == "oto_eq_muzik" else x
                            for x in self.pulse.sinks]
        self.pulse.set_sink(1, "oto_eq_muzik")
        r.scan()
        self.step(0.0)
        self.assertEqual(self.applied[-1], ("muzik", "metal_rock", R.VARIANT_RAMP))
        self.assertEqual(len(self.applied), 2)

    def test_track_boundary_and_same_group_noop(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("pop_vokal", "x")
        self.resolver.cache["sanatci:b"] = GenreResult("pop_vokal", "y")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        self.assertEqual(len(self.applied), 1)
        self.song(1, "B", "t2")                    # new artist, same group -> no eqgen call
        self.step(0.1)
        r.scan()
        self.step(1.0)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.var()["for"], "B")
        self.resolver.cache["sanatci:b"] = GenreResult("elektronik", "z")
        self.song(1, "B", "t3")                    # same artist, new title = boundary
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][:2], ("muzik", "elektronik"))

    def test_mpris_noise_is_not_a_boundary(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        n = len(self.applied)
        self.resolver.cache["sanatci:a"] = GenreResult("klasik_akustik", "x2")
        self.song(1, "A", "")                      # title briefly empty
        r.scan()
        self.song(1, "A", "t1")                    # same again (metadata refresh)
        r.scan()
        self.cids[1] = None                        # no info at all
        r.scan()
        self.step(2.0)
        self.assertEqual(len(self.applied), n)
        self.assertEqual(self.var()["group"], "metal_rock")

    def test_pause_resume_and_new_stream_same_song(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        self.pulse.set_corked(1, True)
        r.scan()
        self.pulse.set_corked(1, False)
        del self.pulse.streams[1]
        self.pulse.add_stream(2, "spotify")        # app recreated its stream
        self.song(2, "A", "t1")
        r.scan()
        self.step(1.5)
        self.assertEqual(len(self.applied), 1)

    def test_rapid_skips_debounced(self):
        r = self.mk()
        for a, g in (("a", "metal_rock"), ("b", "elektronik"), ("c", "klasik_akustik")):
            self.resolver.cache["sanatci:" + a] = GenreResult(g, a)
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.clock.advance(0.3)
        self.song(1, "B", "2")
        r.scan()
        self.clock.advance(0.3)
        self.song(1, "C", "3")
        r.scan()
        self.step(0.5)                              # A's settle time passes: nothing
        self.assertEqual(self.applied, [])
        self.step(0.6)
        self.assertEqual(self.applied, [("muzik", "klasik_akustik", R.VARIANT_RAMP)])

    def test_online_answer_in_time(self):
        r = self.mk()
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        self.assertEqual(len(self.resolver.requests), 1)
        self.assertEqual(self.applied, [])
        self.clock.advance(2.5)                     # 3.5 s after the boundary
        self.resolver.answer(0, GenreResult("metal_rock", "MusicBrainz: metal"))
        self.drain()
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])

    def test_late_answer_waits_for_next_boundary(self):
        r = self.mk()
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        self.clock.advance(R.VARIANT_LATE - 0.5)    # VARIANT_LATE + 0.5 s after the boundary
        self.resolver.answer(0, GenreResult("metal_rock", "MusicBrainz: metal"))
        self.drain()
        self.assertEqual(self.applied, [])          # never mid-track
        self.assertEqual(self.var()["group"], "")
        self.song(1, "A", "2")                      # next song of the same artist: cached
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])
        self.assertEqual(len(self.resolver.requests), 1)

    def test_no_answer_in_time_goes_neutral_for_new_artist(self):
        r = self.mk()
        self.resolver.cache["sanatci:ezhel"] = GenreResult("elektronik", "yerleşik liste")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "Ezhel", "Felaket")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("muzik", "elektronik", R.VARIANT_RAMP)])
        self.song(1, "Virgin Black", "Our Wings")   # unknown artist, slow lookup
        r.scan()
        self.step(1.0)
        self.assertEqual(len(self.resolver.requests), 1)
        self.assertEqual(len(self.applied), 1)       # still waiting for the answer
        self.step(R.VARIANT_LATE)                    # no answer: Ezhel's bass must not stay
        self.assertEqual(self.applied[-1], ("muzik", "", R.VARIANT_RAMP))
        self.assertIn("gecikti", self.var()["source"])
        self.assertEqual(self.var()["for"], "Virgin Black")
        self.resolver.answer(0, GenreResult("metal_rock", "MusicBrainz"))   # too late
        self.drain()
        self.assertEqual(len(self.applied), 2)

    def test_offline_unknown_goes_neutral_same_artist_keeps(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("pop_vokal", "x")
        self.resolver.request = lambda cid, cb: False         # offline
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        del self.resolver.cache["sanatci:a"]                  # "A" now unknown (e.g. cache cleared)
        self.song(1, "A", "2")                                # same artist: keeps its variant
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("muzik", "pop_vokal", R.VARIANT_RAMP)])
        self.song(1, "B", "3")                                # other artist, offline -> Genel
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1], ("muzik", "", R.VARIANT_RAMP))
        self.assertIn("çevrimdışı", self.var()["source"])

    def test_answer_for_previous_track_of_same_artist_applies(self):
        r = self.mk()
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)                                        # request for track 1
        self.song(1, "A", "2")                                # skip within the same artist
        r.scan()
        self.step(1.0)                                        # track 2: joins (same key)
        self.resolver.answer(0, GenreResult("metal_rock", "MusicBrainz"))
        self.drain()
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])
        self.assertEqual(self.var()["for"], "A")

    def test_answer_for_old_content_ignored(self):
        r = self.mk()
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        self.song(1, "B", "2")                      # skipped before the answer came
        r.scan()
        self.clock.advance(0.5)
        self.resolver.answer(0, GenreResult("metal_rock", "x"))
        self.drain()
        self.assertEqual(self.applied, [])

    def test_unknown_genre_applies_general(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.resolver.cache["sanatci:b"] = GenreResult("", "MusicBrainz: sonuç yok")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        self.song(1, "B", "2")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][:2], ("muzik", ""))
        self.assertEqual(self.var()["label"], "Genel")

    def test_games_key_boundary(self):
        r = self.mk()
        self.resolver.cache["steam:1"] = GenreResult("rekabetci", "Steam: FPS")
        self.resolver.cache["steam:2"] = GenreResult("korku_hayatta_kalma", "Steam: Horror")
        self.pulse.add_stream(1, "game.exe")
        self.game(1, "steam:1", "Oyun 1")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied, [("oyun", "rekabetci", R.VARIANT_RAMP)])
        self.pulse.add_stream(2, "game.exe")        # 2nd stream of the same game
        self.game(2, "steam:1", "Oyun 1")
        r.scan()
        self.step(1.5)
        self.assertEqual(len(self.applied), 1)
        del self.pulse.streams[1]
        del self.pulse.streams[2]
        self.pulse.add_stream(3, "game.exe")
        self.game(3, "steam:2", "Oyun 2")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][:2], ("oyun", "korku_hayatta_kalma"))
        self.assertEqual(self.var("oyun")["for"], "Oyun 2")

    def test_candidate_most_recent_non_corked(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.resolver.cache["sanatci:b"] = GenreResult("elektronik", "y")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        self.pulse.add_stream(2, "spotify", corked=True)
        self.song(2, "B", "2")
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][1], "metal_rock")
        self.pulse.set_corked(2, False)             # newer stream starts playing
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][1], "elektronik")

    def test_manual_override(self):
        r = self.mk()
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.resolver.cache["sanatci:b"] = GenreResult("elektronik", "y")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        self.step(1.0)
        self.assertEqual(r.handle_command({"cmd": "set_variant", "profile": "muzik",
                                           "group": "klasik_akustik"}), {"ok": True})
        self.assertEqual(self.applied[-1], ("muzik", "klasik_akustik", R.VARIANT_RAMP))  # at once
        self.assertEqual(self.var()["mode"], "fixed")
        self.song(1, "B", "2")                      # boundaries ignored while fixed
        r.scan()
        self.step(1.0)
        self.assertEqual(self.applied[-1][1], "klasik_akustik")
        r.handle_command({"cmd": "set_variant", "profile": "muzik", "group": "auto"})
        self.step(0.0)                               # re-evaluated right away
        self.assertEqual(self.applied[-1][1], "elektronik")
        r.handle_command({"cmd": "set_variant", "profile": "muzik", "group": ""})
        self.assertEqual(self.applied[-1][1], "")
        self.assertEqual((self.var()["mode"], self.var()["label"]), ("fixed", "Genel"))
        with self.assertRaises(ValueError):
            r.handle_command({"cmd": "set_variant", "profile": "cs2", "group": "auto"})
        with self.assertRaises(ValueError):
            r.handle_command({"cmd": "set_variant", "profile": "muzik", "group": "rekabetci"})

    def test_persistence_with_mode(self):
        d = Path(tempfile.mkdtemp(prefix="oto-eq-test-"))
        try:
            store = R.ModeStore(d / "mod.json")
            r = self.mk(store=store)
            r.handle_command({"cmd": "set_variant", "profile": "oyun", "group": "strateji_insa"})
            r.handle_command({"cmd": "set_mode", "mode": "fixed", "profile": "film"})
            data = json.loads((d / "mod.json").read_text())
            self.assertEqual(data["mode"], "fixed")
            self.assertEqual(data["variants"]["oyun"], {"mode": "fixed", "group": "strateji_insa"})
            self.assertEqual(data["variants"]["muzik"], {"mode": "auto", "group": None})
            self.assertEqual(store.load(), ("fixed", "film"))
            # a new router restores it and applies the fixed group on its first scan
            self.applied.clear()
            r2 = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier,
                          rules_loader=dict, clock=FakeClock(), mode_store=R.ModeStore(d / "mod.json"),
                          apply_variant=lambda p, g, ramp_s=None: self.applied.append((p, g)) or True)
            r2.allow_inline = True
            r2.prepare()
            self.assertEqual(r2.status()["variants"]["oyun"]["mode"], "fixed")
            r2.scan()
            self.assertIn(("oyun", "strateji_insa"), self.applied)
            (d / "mod.json").write_text(json.dumps({"mode": "auto", "variants": {"muzik": {
                "mode": "fixed", "group": "rekabetci"}}}))          # invalid -> auto
            r3 = R.Router(pulse=FakePulse(), classifier_factory=FakeClassifier,
                          rules_loader=dict, clock=FakeClock(), mode_store=R.ModeStore(d / "mod.json"))
            r3.prepare()
            self.assertEqual(r3.status()["variants"]["muzik"]["mode"], "auto")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_status_listener_on_variant_change(self):
        r = self.mk()
        got = []
        r.add_status_listener(got.append)
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "1")
        r.scan()
        n = len(got)
        self.step(1.0)
        self.assertEqual(len(got), n + 1)
        self.assertEqual(got[-1]["variants"]["muzik"]["group"], "metal_rock")

    def test_modules_absent(self):
        r = self.make()                              # no content_id / resolver / apply
        self.pulse.add_stream(1, "spotify")
        self.assertTrue(r.scan())
        self.step(2.0)
        self.assertEqual(self.r.status()["variants"]["muzik"],
                         {"group": "", "label": "Genel", "for": "", "key": "", "source": "", "mode": "auto",
                      "track": "", "ses": None})
        self.assertEqual(r.handle_command({"cmd": "set_variant", "profile": "muzik",
                                           "group": "metal_rock"}), {"ok": True})
        self.assertEqual(self.r.status()["variants"]["muzik"]["group"], "metal_rock")

    def test_errors_contained_and_resolver_stopped(self):
        def bad_cid(st, players, proc=None):
            raise RuntimeError("bozuk")

        def bad_apply(p, g, ramp_s=None):
            raise RuntimeError("pw-cli yok")
        r = self.mk(content_id=bad_cid)
        self.pulse.add_stream(1, "spotify")
        self.assertTrue(r.scan())
        r.handle_command({"cmd": "set_variant", "profile": "muzik", "group": "metal_rock"})
        r2 = self.mk(apply_variant=bad_apply)
        r2.handle_command({"cmd": "set_variant", "profile": "muzik", "group": "metal_rock"})
        self.assertTrue(r2.scan())
        r2.stop()
        self.assertTrue(self.resolver.stopped)


class FakeAnalyzer:
    """CHANGE 4: stands in for ses_analiz.AudioAnalyzer."""

    def __init__(self, group="elektronik", source="ses analizi: güçlü bas + 4/4 vuruş"):
        self.group, self.source = group, source
        self.ensured: list[bool] = []
        self.guesses: list[tuple] = []
        self.stopped = False

    def ensure(self, active):
        self.ensured.append(bool(active))

    def guess(self, since, until=None, min_s=3.0):
        self.guesses.append((since, until, min_s))
        if not self.group:
            return None
        return GenreResult(self.group, self.source, 0.7)

    def stop(self):
        self.stopped = True


class AudioResolver(FakeResolver):
    def __init__(self):
        super().__init__()
        self.remembered: list[tuple] = []

    def remember_audio(self, cid, group, confidence, detail):
        self.remembered.append((cid.key, group, confidence, detail))


class TestAudioVariants(TestVariants):
    def setUp(self):
        super().setUp()
        self.resolver = AudioResolver()
        self.an = FakeAnalyzer()

    def start(self, artist="SAFI", title="Bass Drop"):
        r = self.mk(audio_analyzer=self.an)
        self.pulse.add_stream(1, "spotify")
        self.song(1, artist, title)
        r.scan()
        self.step(1.0)                              # settle → request sent
        return r

    def test_unknown_answer_uses_audio_at_window_end(self):
        r = self.start()
        self.resolver.answer(0, GenreResult("", "bilinmiyor"))
        self.drain()
        self.assertEqual(self.applied, [])          # no change before the window ends
        self.step(R.VARIANT_LATE)
        self.assertEqual(self.applied, [("muzik", "elektronik", R.VARIANT_RAMP)])
        self.assertTrue(self.var()["source"].startswith("ses analizi"))
        since, until, _ = self.an.guesses[0]
        self.assertAlmostEqual(since, r.variants.state["muzik"].boundary_at + R.AUDIO_GUESS_FROM)

    def test_user_genel_assignment_is_not_overruled_by_audio(self):
        # tray/app "Genel (EQ farkı yok)" for this artist: GenreResult("", "kullanıcı", 1.0)
        self.resolver.cache["sanatci:safi"] = GenreResult("", "kullanıcı", 1.0)
        self.start()
        self.step(R.VARIANT_LATE)
        self.assertNotIn(("muzik", "elektronik", R.VARIANT_RAMP), self.applied)
        self.assertEqual(self.an.guesses, [])          # no audio guess for it at all

    def test_user_genel_answer_later_is_final_too(self):
        self.start()
        self.resolver.answer(0, GenreResult("", "kullanıcı", 1.0))
        self.drain()
        self.step(R.VARIANT_LATE)
        self.assertNotIn(("muzik", "elektronik", R.VARIANT_RAMP), self.applied)

    def test_no_answer_in_time_uses_audio(self):
        self.start()
        self.step(R.VARIANT_LATE)                   # resolver never answers
        self.assertEqual(self.applied, [("muzik", "elektronik", R.VARIANT_RAMP)])

    def test_offline_uses_audio(self):
        self.resolver.request = lambda cid, cb: False
        self.start()
        self.assertEqual(self.applied, [])
        self.step(R.VARIANT_LATE)
        self.assertEqual(self.applied, [("muzik", "elektronik", R.VARIANT_RAMP)])

    def test_name_result_wins_and_is_not_remembered(self):
        self.start()
        self.resolver.answer(0, GenreResult("metal_rock", "iTunes: Metal"))
        self.drain()
        self.assertEqual(self.applied, [("muzik", "metal_rock", R.VARIANT_RAMP)])
        self.step(R.AUDIO_REMEMBER_AT)
        self.assertEqual(self.an.guesses, [])
        self.assertEqual(self.resolver.remembered, [])

    def test_remembered_after_25s_without_changing_track(self):
        self.start()
        self.step(R.VARIANT_LATE)                   # audio applied at the window end
        n = len(self.applied)
        self.step(R.AUDIO_REMEMBER_AT - R.VARIANT_LATE)
        self.assertEqual(len(self.applied), n)      # no mid-track change
        self.assertEqual(len(self.resolver.remembered), 1)
        key, group, conf, detail = self.resolver.remembered[0]
        self.assertEqual((key, group, conf), ("sanatci:safi", "elektronik", 0.7))
        self.assertEqual(self.an.guesses[-1][2], R.AUDIO_REMEMBER_MIN)
        self.step(30.0)
        self.assertEqual(len(self.resolver.remembered), 1)   # once per track

    def test_audio_without_guess_goes_neutral_for_new_artist(self):
        self.an.group = ""                          # unclear audio → None
        self.resolver.cache["sanatci:a"] = GenreResult("metal_rock", "x")
        r = self.mk(audio_analyzer=self.an)
        self.pulse.add_stream(1, "spotify")
        self.song(1, "A", "t1")
        r.scan()
        self.step(1.0)
        self.song(1, "B", "t2")                     # new artist, nothing known
        r.scan()
        self.step(1.0)
        self.step(R.VARIANT_LATE)
        self.assertEqual(self.applied[-1], ("muzik", "", R.VARIANT_RAMP))

    def test_switch_off_disables_audio(self):
        r = self.mk(audio_analyzer=self.an)
        with mock.patch.object(R.config, "load_genre_settings",
                               return_value={"cevrimici": True, "ses_analizi": False}):
            r._reload()
        self.assertFalse(r.variants.audio_on)
        self.pulse.add_stream(1, "spotify")
        self.song(1, "SAFI", "x")
        r.scan()
        self.step(1.0)
        self.step(R.VARIANT_LATE)
        self.assertEqual(self.an.guesses, [])
        self.assertFalse(any(self.an.ensured))     # never captures while switched off

    def test_capture_only_for_music_and_stop(self):
        r = self.start()
        self.assertTrue(self.an.ensured and self.an.ensured[-1])
        r.stop()
        self.assertTrue(self.an.stopped)

    def test_game_profile_never_uses_audio(self):
        r = self.mk(audio_analyzer=self.an)
        self.pulse.add_stream(2, "game.exe")
        self.game(2, "oyun:x", "X")
        r.scan()
        self.step(1.0)
        self.step(R.VARIANT_LATE)
        self.assertEqual(self.an.guesses, [])


# TestAudioVariants reuses TestVariants' helpers only, not its tests
for _n in [n for n in dir(TestVariants) if n.startswith("test_")]:
    if _n not in TestAudioVariants.__dict__:
        setattr(TestAudioVariants, _n, None)


class FakeDucker:
    def __init__(self):
        self.calls: list[tuple] = []
        self.stopped = False
        self.settings = {"ek_uygulamalar": []}

    def ensure(self, voice, sinks):
        self.calls.append(([s.index for s in voice], dict(sinks)))

    def stop(self):
        self.stopped = True

    def status(self, wait=None):
        self.wait = wait
        return {"acik": True, "konusma": False}


class TestDucking(RouterTestBase):
    def voice_check(self, st, category, players, extra=None):
        return st.binary == "discord"

    def test_voice_streams_reach_ducker(self):
        d = FakeDucker()
        r = self.make(ducker=d, voice_check=self.voice_check)
        self.pulse.add_stream(1, "discord")
        self.pulse.add_stream(2, "spotify")
        r.scan()
        self.assertEqual(d.calls[-1][0], [1])
        self.assertTrue(any(n.startswith(R.SINK_PREFIX) for n in d.calls[-1][1]))
        self.assertEqual(r.status()["kisma"], {"acik": True, "konusma": False})

    def test_corked_and_off_mode_never_duck(self):
        d = FakeDucker()
        r = self.make(ducker=d, voice_check=self.voice_check, mode=("off", None))
        self.pulse.add_stream(1, "discord")
        r.scan()
        self.assertEqual(d.calls[-1][0], [])
        d2 = FakeDucker()
        r2 = self.make(ducker=d2, voice_check=self.voice_check)
        self.pulse.add_stream(3, "discord", corked=True)
        r2.scan()
        self.assertEqual(d2.calls[-1][0], [])

    def test_stop_releases_ducking(self):
        d = FakeDucker()
        r = self.make(ducker=d, voice_check=self.voice_check)
        r.stop()
        self.assertTrue(d.stopped)

    def test_status_never_waits_for_a_busy_ducker(self):
        # 6 Kas: a ducker tick holds its lock across pactl calls (3 s each while
        # pipewire-pulse hangs) → `kulak durum` / watch / the worker's publish hung on it
        import tempfile
        from oto_eq import kisma
        tmp = tempfile.mkdtemp(prefix="oto-eq-test-kisma-")
        self.addCleanup(shutil.rmtree, tmp, True)
        entered, release = threading.Event(), threading.Event()

        def slow_get(name):
            entered.set()
            release.wait(5)
            return [30000, 30000]
        duck = kisma.Ducker(set_volume=lambda n, v: True, get_volume=slow_get, threaded=False,
                            settings_path=f"{tmp}/k.toml", state_path=f"{tmp}/state.json")
        duck._recs["oto_eq_muzik"] = kisma._Rec([40000, 40000], [1, 1], stale=True)
        duck._sinks = {"muzik": 101}
        first = duck.status()
        r = self.make(ducker=duck)
        t = threading.Thread(target=duck.tick)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))              # the tick is inside "pactl", lock held
        t0 = time.monotonic()
        st = r.status()
        self.assertLess(time.monotonic() - t0, 0.5)
        self.assertEqual(st["kisma"], first)          # the last snapshot
        release.set()
        t.join(5)
        self.assertNotIn("oto_eq_muzik", r.status()["kisma"]["kisilan"])   # fresh again

    def test_ducker_status_wait_is_passed(self):
        d = FakeDucker()
        r = self.make(ducker=d, voice_check=self.voice_check)
        r.status()
        self.assertEqual(d.wait, R.KISMA_STATUS_WAIT)

    def test_voice_check_error_is_contained(self):
        d = FakeDucker()

        def bad(*a, **k):
            raise RuntimeError("x")
        r = self.make(ducker=d, voice_check=bad)
        self.pulse.add_stream(1, "discord")
        self.assertTrue(r.scan())
        self.assertEqual(d.calls[-1][0], [])


class TestAudioLog(TestAudioVariants):
    def test_audio_decisions_are_logged(self):
        logged = []
        self.an.last_detail = {"group": "elektronik"}
        r = self.mk(audio_analyzer=self.an, audio_log=lambda cid, d, a: logged.append((cid.key, d, a)))
        self.pulse.add_stream(1, "spotify")
        self.song(1, "SAFI", "x")
        r.scan()
        self.step(1.0)
        self.step(R.VARIANT_LATE)
        self.step(R.AUDIO_REMEMBER_AT)
        self.assertEqual([a for _, _, a in logged], ["pencere", "hatirla"])
        self.assertEqual(logged[0][1], {"group": "elektronik"})


for _n in [n for n in dir(TestAudioVariants) if n.startswith("test_")]:
    if _n not in TestAudioLog.__dict__:
        setattr(TestAudioLog, _n, None)
