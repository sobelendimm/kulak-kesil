"""Tests for oto_eq.kisma (voice ducking). No audio devices: fake meter/volume/clock."""
from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from oto_eq import kisma
from oto_eq.models import MediaPlayer, Stream

SINK_IDX = {"oyun": 101, "muzik": 102, "film": 103, "cs2": 104, "duz": 105}
ALL_SINKS = {f"oto_eq_{p}": i for p, i in SINK_IDX.items()}
FACTOR6 = 10 ** (-6 / 60)
_LOG = logging.getLogger("oto_eq.kisma")


def setUpModule():
    _LOG.disabled = True          # expected error logs (injected failures) stay quiet


def tearDownModule():
    _LOG.disabled = False


def stream(index=500, app="Discord", binary="Discord", sink=SINK_IDX["duz"], corked=False,
           media_name="", role="", pid=1234, props=None):
    return Stream(index=index, sink_index=sink, app_name=app, binary=binary, pid=pid,
                  media_name=media_name, media_role=role, node_name="", corked=corked,
                  props=props or {})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Volumes:
    """Fake pactl: per-sink channel lists; can fail on chosen set calls."""

    def __init__(self, base=None):
        self.v = {n: [65536, 65536] for n in ALL_SINKS}
        if base:
            self.v.update(base)
        self.calls: list[tuple[str, list[int]]] = []
        self.fail_on: set[int] = set()
        self.fail_always = False
        self.absent: set[str] = set()

    def get(self, name):
        if name in self.absent or name not in self.v:
            return None
        return list(self.v[name])

    def set(self, name, vols):
        n = len(self.calls)
        self.calls.append((name, list(vols)))
        if self.fail_always or n in self.fail_on:
            raise RuntimeError("pactl patladı")
        if name in self.absent:
            return False
        self.v[name] = list(vols)
        return True


class Meter:
    def __init__(self, src, target):
        self.src, self.target = src, target
        self.closed = False
        self.failed = False

    def level(self):
        if self.src.raise_level:
            raise RuntimeError("ölçer patladı")
        return self.src.level

    def close(self):
        self.closed = True


class MeterSource:
    def __init__(self):
        self.level = None
        self.raise_level = False
        self.created: list[Meter] = []

    def factory(self, target):
        m = Meter(self, target)
        self.created.append(m)
        return m

    def open(self):
        return [m for m in self.created if not m.closed]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.settings = self.dir / "kisma.json"
        self.state = self.dir / "kisma-durum.json"
        self.clock = Clock()
        self.vol = Volumes()
        self.src = MeterSource()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        return kisma.Ducker(meter_factory=self.src.factory, set_volume=self.vol.set,
                            get_volume=self.vol.get, clock=self.clock,
                            settings_path=self.settings, state_path=self.state,
                            threaded=False, **kw)

    def run_for(self, d, seconds, level=None):
        if level is not None or level is None:
            self.src.level = level
        steps = int(round(seconds / kisma.TICK_S))
        for _ in range(steps):
            self.clock.t += kisma.TICK_S
            d.tick()

    def ducked(self, name, base=65536, db=6.0):
        f = 10 ** (-db / 60)
        return self.vol.v[name] == [int(round(base * f))] * 2


class GateTest(unittest.TestCase):
    def test_attack_needs_sustained_level(self):
        g = kisma.Gate(-48, -56, 0.05, 0.3)
        self.assertFalse(g.update(-20, 0.00))
        self.assertFalse(g.update(-60, 0.02))     # blip broken → restart
        self.assertFalse(g.update(-20, 0.04))
        self.assertFalse(g.update(-20, 0.06))
        self.assertTrue(g.update(-20, 0.10))

    def test_hysteresis_and_hold(self):
        g = kisma.Gate(-48, -56, 0.05, 0.3)
        t = 0.0
        for _ in range(10):                        # between thresholds: never opens
            self.assertFalse(g.update(-52, t))
            t += 0.02
        for _ in range(5):
            g.update(-30, t)
            t += 0.02
        self.assertTrue(g.is_open)
        for _ in range(50):                        # above close threshold: stays open
            self.assertTrue(g.update(-52, t))
            t += 0.02
        start = t
        while g.update(-80, t):
            t += 0.02
        self.assertAlmostEqual(t - start, 0.30, delta=0.021)


class DuckingTest(Base):
    def test_idle_without_voice_streams(self):
        d = self.make()
        d.ensure([], ALL_SINKS)
        self.assertIsNone(d.tick())
        self.assertEqual(self.src.created, [])
        self.assertEqual(self.vol.calls, [])
        self.assertFalse(self.settings.exists(), "ayar dosyası yalnız kaydedince oluşur")
        # G2: idle status shows 0, not "-0" (GUI/CLI printed "kısma -0 dB")
        self.assertEqual(f"{d.status()['kisma_db']:g}", "0")

    def test_ducks_eq_sinks_but_not_cs2_or_duz(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        for p in ("oyun", "muzik", "film"):
            self.assertTrue(self.ducked(f"oto_eq_{p}"), (p, self.vol.v[f"oto_eq_{p}"]))
        self.assertEqual(self.vol.v["oto_eq_cs2"], [65536, 65536])
        self.assertEqual(self.vol.v["oto_eq_duz"], [65536, 65536])
        self.assertFalse(any(n in ("oto_eq_cs2", "oto_eq_duz") for n, _ in self.vol.calls))
        st = d.status()
        self.assertTrue(st["aktif"] and st["konusma"])
        self.assertEqual(st["kisma_db"], 6.0)
        self.assertEqual(st["sesli"], ["Discord"])
        self.assertEqual([m.target for m in self.src.open()], [500])   # per-stream meter
        self.assertTrue(self.state.exists())

    def test_short_blip_does_not_duck(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.2, -80)
        self.run_for(d, 0.02, -20)
        self.run_for(d, 1.0, -80)
        self.assertEqual(self.vol.calls, [])

    def test_ramp_down_is_smooth_and_fast(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.1, -80)
        self.src.level = -25
        t0 = self.clock.t
        values = []
        while not self.ducked("oto_eq_oyun"):
            self.clock.t += kisma.TICK_S
            d.tick()
            values.append(self.vol.v["oto_eq_oyun"][0])
            self.assertLess(self.clock.t - t0, 0.5)
        # attack (~60 ms) + ramp (~80 ms)
        self.assertLessEqual(self.clock.t - t0, 0.16 + 1e-9)
        steps = sorted(set(values), reverse=True)
        self.assertGreaterEqual(len(steps) - 1, 3, steps)     # several small steps, no jump

    def test_release_after_hold_and_exact_restore_of_user_volume(self):
        user = {"oto_eq_oyun": [52000, 47000], "oto_eq_muzik": [30000, 30000],
                "oto_eq_film": [65536, 60001]}
        self.vol.v.update({k: list(v) for k, v in user.items()})
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.assertLess(self.vol.v["oto_eq_oyun"][0], 52000)
        # quiet: hold 300 + release 600 ms → still lowered at 0.8 s
        self.run_for(d, 0.8, -90)
        self.assertLess(self.vol.v["oto_eq_oyun"][0], 52000)
        self.run_for(d, 0.2 + 0.45, -90)          # ramp up 400 ms
        for k, v in user.items():
            self.assertEqual(self.vol.v[k], v, k)
        self.assertFalse(self.state.exists())
        self.assertEqual(d.status()["kisma_db"], 0.0)
        self.assertIsNotNone(d.tick())            # still metering (voice stream exists)

    def test_pause_between_words_does_not_pump(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        n = len(self.vol.calls)
        for _ in range(5):                         # 500 ms pauses between sentences
            self.run_for(d, 0.5, -90)
            self.run_for(d, 0.4, -25)
        self.assertEqual(len(self.vol.calls), n)
        self.assertTrue(self.ducked("oto_eq_film"))

    def test_voice_stream_gone_restores(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        d.ensure([], ALL_SINKS)
        self.run_for(d, 0.6, -25)
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertEqual(self.src.open(), [])
        self.assertIsNone(d.tick())               # idle again: no ticking, no CPU

    def test_corked_voice_stream_ignored(self):
        d = self.make()
        d.ensure([stream(corked=True)], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.assertEqual(self.vol.calls, [])
        self.assertEqual(self.src.created, [])

    def test_user_changed_volume_while_ducked_is_kept(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.vol.v["oto_eq_muzik"] = [20000, 20000]     # user moved the slider
        n = len(self.vol.calls)
        self.run_for(d, 2.0, -90)
        self.assertFalse([c for c in self.vol.calls[n:] if c[0] == "oto_eq_muzik"])
        self.assertEqual(self.vol.v["oto_eq_muzik"], [20000, 20000])
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertFalse(self.state.exists())

    def test_cs2_only_when_configured(self):
        kisma.save_settings({"profiller": ["cs2", "oyun"], "db": 10}, self.settings)
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.assertTrue(self.ducked("oto_eq_cs2", db=10))
        self.assertTrue(self.ducked("oto_eq_oyun", db=10))
        self.assertEqual(self.vol.v["oto_eq_film"], [65536, 65536])

    def test_sink_carrying_the_call_is_not_lowered(self):
        d = self.make()
        meet = stream(index=600, app="Firefox", binary="firefox", sink=SINK_IDX["film"])
        d.ensure([meet], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.assertEqual(self.vol.v["oto_eq_film"], [65536, 65536])
        self.assertTrue(self.ducked("oto_eq_oyun"))

    def test_disable_in_settings_while_ducked_restores(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        kisma.save_settings({"acik": False}, self.settings)
        d.reload_settings()
        self.run_for(d, 0.6, -25)
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertFalse(d.status()["aktif"])
        self.assertEqual(self.src.open(), [])

    def test_profile_removed_while_ducked_restores_that_sink(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        kisma.save_settings({"profiller": ["oyun"]}, self.settings)
        d.reload_settings()
        self.run_for(d, 0.1, -25)
        self.assertEqual(self.vol.v["oto_eq_film"], [65536, 65536])
        self.assertTrue(self.ducked("oto_eq_oyun"))

    def test_meter_fallback_to_duz_monitor(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.04, -90)
        self.src.created[0].failed = True
        self.run_for(d, 0.04, -90)
        self.assertEqual([m.target for m in self.src.open()], [kisma.DUZ_MONITOR])
        many = [stream(index=700 + i) for i in range(kisma.MAX_METERS + 1)]
        d2 = self.make()
        d2.ensure(many, ALL_SINKS)
        self.run_for(d2, 0.04, -90)
        self.assertIn(kisma.DUZ_MONITOR, [m.target for m in self.src.open()])
        self.assertEqual(d2.status()["olcum"], "Düz monitörü")

    def test_stop_restores_immediately(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        d.stop()
        for p in ("oyun", "muzik", "film"):
            self.assertEqual(self.vol.v[f"oto_eq_{p}"], [65536, 65536])
        self.assertEqual(self.src.open(), [])
        self.assertFalse(self.state.exists())
        d.stop()                                   # idempotent


class ThreadedTest(Base):
    def test_worker_thread_ducks_and_stop_restores(self):
        d = kisma.Ducker(meter_factory=self.src.factory, set_volume=self.vol.set,
                         get_volume=self.vol.get, settings_path=self.settings,
                         state_path=self.state, threaded=True)
        try:
            self.src.level = -25
            d.ensure([stream()], ALL_SINKS)
            deadline = time.monotonic() + 3
            while not self.ducked("oto_eq_oyun") and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(self.ducked("oto_eq_oyun"))
        finally:
            d.stop()
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.src.open(), [])


class StatusSnapshotTest(Base):
    def test_status_wait_returns_snapshot_while_a_tick_holds_the_lock(self):
        # 6 Kas: the router's status() (IPC `kulak durum`, watch, publish) hung for the
        # whole tick while pactl was slow; status(wait=…) serves the last snapshot instead
        import threading
        entered, release = threading.Event(), threading.Event()
        d = self.make()
        d.ensure([], ALL_SINKS)
        d._recs["oto_eq_muzik"] = kisma._Rec([40000, 40000], [1, 1], stale=True)
        snap = d.status()
        self.assertEqual(snap["kisilan"], ["oto_eq_muzik"])

        def slow_get(name):                           # pactl hanging near its timeout
            entered.set()
            release.wait(5)
            return self.vol.get(name)
        d._get = slow_get
        t = threading.Thread(target=d.tick)
        t.start()
        try:
            self.assertTrue(entered.wait(5))
            t0 = time.monotonic()
            got = d.status(wait=0.05)
            self.assertLess(time.monotonic() - t0, 0.5)
            self.assertEqual(got, snap)
            got["kisilan"].append("x")                # a copy: the snapshot is not mutated
            self.assertNotIn("x", d.status(wait=0.05)["kisilan"])
        finally:
            release.set()
            t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(d.status(wait=0.05)["kisilan"], [])   # the tick refreshed it

    def test_snapshot_exists_before_the_first_tick(self):
        d = self.make()
        d._lock.acquire()
        try:
            import threading
            out = []
            t = threading.Thread(target=lambda: out.append(d.status(wait=0.01)))
            t.start()
            t.join(2)
        finally:
            d._lock.release()
        self.assertFalse(out[0]["aktif"])


class SafetyTest(Base):
    def test_no_stale_lowering_after_volume_exception(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.06, -25)                 # attack done
        self.vol.fail_on = {len(self.vol.calls) + 1}   # a mid-ramp pactl call blows up
        self.run_for(d, 0.1, -25)
        for p in ("oyun", "muzik", "film"):
            self.assertEqual(self.vol.v[f"oto_eq_{p}"], [65536, 65536], p)
        self.assertFalse(self.state.exists())
        # cool-down, then works again
        self.run_for(d, kisma.ERROR_COOLDOWN_S + 0.5, -25)
        self.assertTrue(self.ducked("oto_eq_oyun"))

    def test_no_stale_lowering_after_meter_exception(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.src.raise_level = True
        self.run_for(d, 0.04, -25)
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertFalse(self.state.exists())

    def test_persistent_failure_keeps_record_for_next_start(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.vol.fail_always = True
        d.stop()                                   # restore fails → record must survive
        self.assertTrue(self.state.exists())
        self.vol.fail_always = False
        d2 = self.make()
        self.assertEqual(d2.restore_on_start(), 3)
        for p in ("oyun", "muzik", "film"):
            self.assertEqual(self.vol.v[f"oto_eq_{p}"], [65536, 65536])
        self.assertFalse(self.state.exists())

    def test_crash_then_restore_on_start(self):
        self.vol.v["oto_eq_muzik"] = [40000, 41000]
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        self.assertNotEqual(self.vol.v["oto_eq_muzik"], [40000, 41000])
        del d                                      # "SIGKILL": no stop()
        d2 = self.make()
        d2.restore_on_start()
        self.assertEqual(self.vol.v["oto_eq_muzik"], [40000, 41000])
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertFalse(self.state.exists())

    def test_crash_mid_ramp_then_restore(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.1, -25)                  # partly lowered
        cur = self.vol.v["oto_eq_oyun"][0]
        self.assertTrue(int(65536 * FACTOR6) <= cur < 65536, cur)
        d2 = self.make()
        d2.restore_on_start()
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])

    def test_restore_on_start_respects_user_change_and_absent_sink(self):
        self.state.write_text(json.dumps({"kisilan": {
            "oto_eq_oyun": {"base": [65536, 65536], "floor": [52000, 52000]},
            "oto_eq_muzik": {"base": [65536, 65536], "floor": [52000, 52000]},
            "oto_eq_film": {"base": [65536, 65536], "floor": [52000, 52000]},
        }}))
        self.vol.v["oto_eq_oyun"] = [52000, 52000]    # still lowered → restore
        self.vol.v["oto_eq_muzik"] = [20000, 20000]   # user set it lower himself → keep
        self.vol.absent.add("oto_eq_film")            # sinks not up yet → later
        self.vol.v["oto_eq_film"] = [52000, 52000]
        d = self.make()
        self.assertEqual(d.restore_on_start(), 1)
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])
        self.assertEqual(self.vol.v["oto_eq_muzik"], [20000, 20000])
        self.assertTrue(self.state.exists())          # film still pending
        self.vol.absent.clear()
        d.ensure([], ALL_SINKS)
        d.tick()
        self.assertEqual(self.vol.v["oto_eq_film"], [65536, 65536])
        self.assertFalse(self.state.exists())

    def test_ensure_runs_restore_on_start_lazily(self):
        self.state.write_text(json.dumps({"kisilan": {
            "oto_eq_oyun": {"base": [65536, 65536], "floor": [52000, 52000]}}}))
        self.vol.v["oto_eq_oyun"] = [60000, 60000]
        d = self.make()
        d.ensure([], ALL_SINKS)
        self.assertEqual(self.vol.v["oto_eq_oyun"], [65536, 65536])

    def test_corrupt_state_file_is_ignored(self):
        self.state.write_text("{bozuk")
        d = self.make()
        self.assertEqual(d.restore_on_start(), 0)
        self.assertEqual(self.vol.calls, [])

    def test_sink_restart_while_ducked(self):
        d = self.make()
        d.ensure([stream()], ALL_SINKS)
        self.run_for(d, 0.5, -25)
        lowered = list(self.vol.v["oto_eq_film"])
        gone = {k: v for k, v in ALL_SINKS.items() if k != "oto_eq_film"}
        d.ensure([stream()], gone)
        self.run_for(d, 0.1, -25)
        self.assertTrue(self.state.exists())
        # sink comes back with the lowered volume restored by WirePlumber, call ended
        d.ensure([], ALL_SINKS)
        self.run_for(d, 0.6, -90)
        self.assertNotEqual(lowered, [65536, 65536])
        for p in ("oyun", "muzik", "film"):
            self.assertEqual(self.vol.v[f"oto_eq_{p}"], [65536, 65536], p)
        self.assertFalse(self.state.exists())


class SettingsTest(unittest.TestCase):
    def test_normalize(self):
        s = kisma.normalize_settings({"db": 99, "profiller": ["film", "duz", "x", "film"],
                                      "acik": "evet", "acik_esik_db": -60,
                                      "kapali_esik_db": -40, "atak_ms": -5,
                                      "ek_uygulamalar": ["ok.*", "(bozuk"]})
        self.assertEqual(s["db"], kisma.DB_RANGE[1])
        self.assertEqual(s["profiller"], ["film"])
        self.assertTrue(s["acik"])
        self.assertEqual(s["kapali_esik_db"], -60)
        self.assertEqual(s["atak_ms"], 0)
        self.assertEqual(s["ek_uygulamalar"], ["ok.*"])
        d = kisma.normalize_settings(None)
        self.assertEqual(d["profiller"], ["oyun", "muzik", "film"])
        self.assertEqual(d["db"], 6.0)
        self.assertNotIn("cs2", d["profiller"])


class DetectTest(unittest.TestCase):
    def test_classifier_voice(self):
        self.assertTrue(kisma.sesli_mi(stream(), "sesli"))
        from oto_eq.models import Category
        self.assertTrue(kisma.sesli_mi(stream(app="TeamSpeak"), Category.VOICE))
        self.assertFalse(kisma.sesli_mi(stream(app="Spotify", binary="spotify"), "muzik"))

    def test_browser_tabs(self):
        ff = dict(app="Firefox", binary="firefox")
        self.assertTrue(kisma.sesli_mi(stream(media_name="Meet - abc-defg-hij", **ff), "film"))
        self.assertTrue(kisma.sesli_mi(stream(media_name="Zoom Meeting", **ff), "film"))
        self.assertFalse(kisma.sesli_mi(stream(media_name="Lofi beats - YouTube", **ff), "muzik"))
        pl = MediaPlayer(bus_name="x", pid=1234, identity="Firefox", status="Playing",
                         title="", artist="", url="https://meet.google.com/abc-defg-hij")
        self.assertTrue(kisma.sesli_mi(stream(media_name="AudioStream", **ff), "film", [pl]))
        other = MediaPlayer(bus_name="y", pid=999, identity="Firefox", status="Playing",
                            title="Discord", artist="", url="https://discord.com/channels/1")
        self.assertFalse(kisma.sesli_mi(stream(media_name="AudioStream", **ff), "film", [other]))

    def test_messenger_calls_only(self):
        tg = dict(app="Telegram Desktop", binary="telegram-desktop")
        self.assertFalse(kisma.sesli_mi(stream(media_name="notification", **tg), "bilinmeyen"))
        self.assertTrue(kisma.sesli_mi(stream(role="phone", **tg), "bilinmeyen"))
        self.assertTrue(kisma.sesli_mi(stream(media_name="Call", app="WhatsApp",
                                              binary="whatsapp"), "bilinmeyen"))

    def test_messenger_hosts_need_call_evidence(self):
        # G2 (25 Eki): Ferdium/Signal are "sesli" in the classifier, but their streams are
        # mostly message notifications → no ducking without call evidence
        fd = dict(app="Chromium", binary="ferdium",
                  props={"pipewire.access.portal.app_id": "org.ferdium.Ferdium"})
        self.assertFalse(kisma.sesli_mi(stream(media_name="Playback", **fd), "sesli"))
        self.assertTrue(kisma.sesli_mi(stream(media_name="Playback", role="communication", **fd),
                                       "sesli"))
        sg = dict(app="Signal", binary="signal-desktop")
        self.assertFalse(kisma.sesli_mi(stream(media_name="Playback", **sg), "sesli"))
        self.assertTrue(kisma.sesli_mi(stream(media_name="Signal call", **sg), "sesli"))
        # real voice apps keep ducking on the classifier's word
        self.assertTrue(kisma.sesli_mi(stream(app="WEBRTC VoiceEngine", binary="Discord"), "sesli"))

    def test_messenger_web_tabs_need_call_evidence(self):
        ff = dict(app="Firefox", binary="firefox")
        self.assertFalse(kisma.sesli_mi(stream(media_name="(3) WhatsApp", **ff), "sesli"))
        self.assertFalse(kisma.sesli_mi(stream(media_name="Telegram Web", **ff), "sesli"))
        wa = MediaPlayer(bus_name="x", pid=1234, identity="Firefox", status="Playing",
                         title="WhatsApp", artist="", url="https://web.whatsapp.com/")
        self.assertFalse(kisma.sesli_mi(stream(media_name="AudioStream", **ff), "sesli", [wa]))
        self.assertTrue(kisma.sesli_mi(stream(media_name="WhatsApp - sesli arama", **ff), "sesli"))
        # a call site, and a user-defined voice site (classifier "sesli") still count
        self.assertTrue(kisma.sesli_mi(stream(media_name="Meet - abc-defg-hij", **ff), "sesli"))
        self.assertTrue(kisma.sesli_mi(stream(media_name="Benim telsiz sitem", **ff), "sesli"))
        self.assertFalse(kisma.sesli_mi(stream(media_name="Benim telsiz sitem", **ff), "film"))

    def test_extra_patterns(self):
        self.assertTrue(kisma.sesli_mi(stream(app="Element", binary="element-desktop"),
                                       "bilinmeyen", extra=["element.*"]))


class ParseTest(unittest.TestCase):
    def test_parse_channel_volumes(self):
        txt = ("Volume: front-left: 52000 /  79% / -6.13 dB,   front-right: 47000 /  75% / -7.4 dB\n"
               "        balance -0.10\n")
        self.assertEqual(kisma.parse_channel_volumes(txt), [52000, 47000])
        self.assertIsNone(kisma.parse_channel_volumes("garbage"))

    def test_meter_argv(self):
        self.assertIn("--monitor-stream=42", kisma.meter_argv(42))
        a = kisma.meter_argv(kisma.DUZ_MONITOR)
        self.assertEqual(a[-2:], ["-d", "oto_eq_duz.monitor"])
        # parec runs with LC_ALL=C: a non-ASCII client name makes it exit at once
        self.assertTrue(all(x.isascii() for x in a), a)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "kisma.json"
        self.out: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, live=lambda: None):
        self.out.clear()
        return kisma.run(list(argv), out=self.out.append, settings_path=self.path, live=live)

    def test_durum_default_does_not_create_file(self):
        self.assertEqual(self.run_cli(), 0)
        self.assertFalse(self.path.exists())
        self.assertIn("AÇIK", self.out[0])

    def test_ac_kapat_db_profiller(self):
        self.assertEqual(self.run_cli("kapat"), 0)
        self.assertFalse(json.loads(self.path.read_text())["acik"])
        self.assertEqual(self.run_cli("ac", "db=8,5", "--profiller", "oyun,film"), 0)
        s = json.loads(self.path.read_text())
        self.assertTrue(s["acik"])
        self.assertEqual(s["db"], 8.5)
        self.assertEqual(s["profiller"], ["oyun", "film"])
        self.assertEqual(self.run_cli("--profiller=cs2"), 0)
        self.assertEqual(kisma.load_settings(self.path)["profiller"], ["cs2"])

    def test_usage_errors(self):
        self.assertEqual(self.run_cli("db=100"), 2)
        self.assertEqual(self.run_cli("db=abc"), 2)
        self.assertEqual(self.run_cli("--profiller", "duz"), 2)
        self.assertEqual(self.run_cli("--profiller"), 2)
        self.assertEqual(self.run_cli("uc"), 2)
        self.assertEqual(self.run_cli("--help"), 0)
        self.assertFalse(self.path.exists())

    def test_durum_live(self):
        live = {"aktif": True, "konusma": True, "kisma_db": 6.0, "sesli": ["Discord"],
                "olcum": "akış #500"}
        self.assertEqual(self.run_cli("durum", live=lambda: live), 0)
        self.assertTrue(any("Discord" in l and "konuşma VAR" in l for l in self.out), self.out)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_cli(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"kulak kisma: geçersiz dB değeri: {deger}": "ungültiger dB-Wert: {deger}"})
        lines = []
        self.assertEqual(kisma.run(["db=abc"], out=lines.append), kisma.EXIT_USAGE)
        self.assertEqual(lines, ["ungültiger dB-Wert: 'abc'"])


if __name__ == "__main__":
    unittest.main()
