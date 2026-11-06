"""Tests for oto_eq.ses_analiz (no audio devices: fake runner + synthetic signals).

Run: QT_NO_GLIB=1 OMP_NUM_THREADS=1 python3 -m unittest tests.test_ses_analiz -v
"""
from __future__ import annotations

import os

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import math
import queue
import threading
import time
import unittest

import numpy as np

from oto_eq import ses_analiz as sa
from oto_eq.models import MUSIC_GROUPS, GenreResult

R = 22050


# ---------------------------------------------------------------- fakes
class FakeStdout:
    """Blocking read(n) fed from a queue; b'' (EOF) once closed."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.closed = False

    def feed(self, data: bytes) -> None:
        self.q.put(data)

    def read(self, n: int) -> bytes:
        if self.closed:
            return b""
        data = self.q.get()
        return data if data is not None else b""

    def close(self) -> None:
        self.closed = True
        self.q.put(None)


class FakeProc:
    def __init__(self):
        self.stdout = FakeStdout()
        self.returncode = None
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.stdout.close()

    def kill(self):
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode

    def die(self):
        """Process exits on its own (EOF on stdout)."""
        self.returncode = 1
        self.stdout.close()


class FakeRunner:
    def __init__(self):
        self.procs: list[FakeProc] = []
        self.argvs: list[list[str]] = []
        self.spawned = threading.Event()

    def __call__(self, argv):
        p = FakeProc()
        self.procs.append(p)
        self.argvs.append(argv)
        self.spawned.set()
        return p

    def wait_spawn(self, count: int, timeout: float = 3.0) -> FakeProc:
        end = time.monotonic() + timeout
        while len(self.procs) < count and time.monotonic() < end:
            time.sleep(0.005)
        assert len(self.procs) >= count, f"{count}. süreç başlatılmadı"
        return self.procs[count - 1]


class Clock:
    def __init__(self, t: float = 100.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


# ---------------------------------------------------------------- signals
def kick_track(seconds=8.0, bpm=125.0, seed=0, level=0.3):
    """4-on-the-floor kick with sub-bass + off-beat hats + a bass line."""
    rng = np.random.default_rng(seed)
    n = int(seconds * R)
    t = np.arange(n) / R
    x = np.zeros(n)
    beat = 60.0 / bpm
    for b in np.arange(0, seconds - 0.3, beat):
        i = int(b * R)
        m = min(n - i, int(0.3 * R))
        tt = np.arange(m) / R
        x[i:i + m] += np.sin(2 * np.pi * (45 + 90 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 8)
        j = int((b + beat / 2) * R)
        h = min(n - j, int(0.05 * R))
        if h > 0:
            x[j:j + h] += 0.15 * rng.standard_normal(h) * np.exp(-np.arange(h) / R * 60)
    x += 0.35 * np.sin(2 * np.pi * 55 * t) * (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * t / beat)))
    return (level * x / np.max(np.abs(x))).astype(np.float32)


def speech_like(seconds=8.0, seed=1, level=0.2):
    """Syllables (~4/s) of a voiced harmonic source with formants, pauses between phrases."""
    rng = np.random.default_rng(seed)
    n = int(seconds * R)
    x = np.zeros(n)
    t = 0.05
    while t < seconds - 0.4:
        phrase_end = t + rng.uniform(1.0, 2.2)
        while t < min(phrase_end, seconds - 0.3):
            d = rng.uniform(0.12, 0.26)
            i, m = int(t * R), int(d * R)
            tt = np.arange(m) / R
            f0 = rng.uniform(100, 150) * (1 + 0.1 * np.sin(2 * np.pi * 3 * tt))
            ph = 2 * np.pi * np.cumsum(f0) / R
            src = sum(np.sin(k * ph) / k for k in range(1, 30))
            # crude formant emphasis: boost harmonics near 500/1500/2500 Hz
            f1 = rng.uniform(400, 800)
            src = src + 0.6 * np.sin(ph * max(1, round(f1 / f0.mean())))
            src += 0.5 * np.sin(ph * max(1, round(1500 / f0.mean())))
            env = np.sin(np.pi * np.arange(m) / m) ** 1.5
            x[i:i + m] += src * env
            if rng.random() < 0.35:   # fricative
                k = int(0.06 * R)
                if i + m + k < n:
                    nz = rng.standard_normal(k)
                    nz = np.diff(nz, prepend=0)          # high-passed noise
                    x[i + m:i + m + k] += 0.4 * nz * np.hanning(k)
            t += d + rng.uniform(0.03, 0.1)
        t = phrase_end + rng.uniform(0.25, 0.6)
    x += 0.0005 * rng.standard_normal(n)
    return (level * x / np.max(np.abs(x))).astype(np.float32)


def strings_like(seconds=8.0, seed=2, level=0.15):
    """Slow sustained harmonic chords with swells (orchestral-ish), no percussion."""
    rng = np.random.default_rng(seed)
    n = int(seconds * R)
    t = np.arange(n) / R
    x = np.zeros(n)
    for f in (196.0, 246.9, 293.7, 392.0, 493.9):
        vib = 1 + 0.004 * np.sin(2 * np.pi * 5.5 * t + rng.uniform(0, 6))
        ph = 2 * np.pi * np.cumsum(f * vib) / R
        x += sum(np.sin(k * ph) * 0.6 ** k for k in range(1, 8))
    swell = 0.35 + 0.65 * np.sin(np.pi * t / seconds) ** 2
    x *= swell
    return (level * x / np.max(np.abs(x))).astype(np.float32)


# ================================================================ tap
class TestAudioTap(unittest.TestCase):
    def make(self, seconds=2.0):
        runner, clock = FakeRunner(), Clock()
        tap = sa.AudioTap(source="test.monitor", rate=1000, seconds=seconds,
                          runner=runner, clock=clock)
        tap.BACKOFF_MIN = 0.01
        tap.BACKOFF_MAX = 0.02
        return tap, runner, clock

    def feed(self, tap, proc, clock, values, t_end):
        before = tap._total
        clock.t = t_end
        proc.stdout.feed(np.asarray(values, dtype="<f4").tobytes())
        self.assertTrue(wait_until(lambda: tap._total >= before + len(values)))

    def test_argv_parec_monitor(self):
        tap, runner, clock = self.make()
        tap.start()
        try:
            runner.wait_spawn(1)
            argv = runner.argvs[0]
            self.assertIn(argv[0], ("parec", "pw-record"))
            self.assertIn("test.monitor", argv)
            if argv[0] == "parec":
                self.assertIn("--format=float32le", argv)
                self.assertIn("--channels=1", argv)
                self.assertIn("--rate=1000", argv)
        finally:
            tap.stop()

    def test_window_by_timestamps(self):
        tap, runner, clock = self.make()
        tap.start()
        try:
            p = runner.wait_spawn(1)
            self.feed(tap, p, clock, np.arange(100), 10.1)        # samples at 10.001..10.1
            self.feed(tap, p, clock, np.arange(100, 200), 10.2)
            w = tap.window(10.05, 10.15)
            self.assertEqual(w.dtype, np.float32)
            self.assertEqual(w[0], 50.0)
            self.assertEqual(w[-1], 149.0)
            self.assertEqual(len(tap.window(0.0)), 200)
            self.assertEqual(len(tap.window(10.3)), 0)
        finally:
            tap.stop()

    def test_window_t1_none_means_now(self):
        """Contract: t1=None = up to now (was a floor(inf) error)."""
        tap, runner, clock = self.make()
        tap.start()
        try:
            p = runner.wait_spawn(1)
            self.feed(tap, p, clock, np.ones(300), 5.3)      # samples at 5.001 .. 5.300
            self.assertEqual(len(tap.window(5.1)), 200)
            self.assertEqual(len(tap.window(5.1, None)), 200)
            self.assertEqual(len(tap.window(float("-inf"))), 300)
            self.assertEqual(len(tap.window(-math.inf, math.inf)), 300)
        finally:
            tap.stop()

    def test_gap_between_chunks_is_respected(self):
        """Sink suspended: no samples for a while; timestamps stay per chunk."""
        tap, runner, clock = self.make()
        tap.start()
        try:
            p = runner.wait_spawn(1)
            self.feed(tap, p, clock, np.zeros(100), 1.0)
            self.feed(tap, p, clock, np.ones(100), 9.0)
            w = tap.window(5.0)
            self.assertEqual(len(w), 100)
            self.assertTrue(np.all(w == 1.0))
        finally:
            tap.stop()

    def test_ring_wraps_and_drops_old(self):
        tap, runner, clock = self.make(seconds=0.25)   # 250 samples
        tap.start()
        try:
            p = runner.wait_spawn(1)
            for k in range(4):
                self.feed(tap, p, clock, np.full(100, k), 1.0 + 0.1 * (k + 1))
            w = tap.window(0.0)
            self.assertEqual(len(w), 250)
            self.assertTrue(np.all(w[:50] == 1.0))
            self.assertTrue(np.all(w[-100:] == 3.0))
            np.testing.assert_array_equal(tap.latest(0.12),
                                          np.r_[np.full(20, 2.0), np.full(100, 3.0)])
        finally:
            tap.stop()

    def test_partial_sample_bytes_across_reads(self):
        tap, runner, clock = self.make()
        tap.start()
        try:
            p = runner.wait_spawn(1)
            raw = np.array([1.5, -2.25, 3.0], dtype="<f4").tobytes()
            clock.t = 2.0
            p.stdout.feed(raw[:5])
            p.stdout.feed(raw[5:])
            self.assertTrue(wait_until(lambda: tap._total == 3))
            np.testing.assert_array_equal(tap.window(0.0), [1.5, -2.25, 3.0])
        finally:
            tap.stop()

    def test_restart_after_process_dies(self):
        tap, runner, clock = self.make()
        tap.start()
        try:
            p1 = runner.wait_spawn(1)
            p1.die()
            p2 = runner.wait_spawn(2)
            self.assertIsNot(p1, p2)
            self.assertTrue(p1.waited, "ölen süreç toplanmadı (zombi)")
            self.assertTrue(tap.running)
            self.assertGreaterEqual(tap.restarts, 1)
        finally:
            tap.stop()

    def test_runner_error_backoff_no_crash(self):
        calls = []

        def bad(argv):
            calls.append(argv)
            raise OSError("parec yok")

        tap = sa.AudioTap(rate=1000, runner=bad)
        tap.BACKOFF_MIN = 0.01
        tap.BACKOFF_MAX = 0.02
        with self.assertLogs("oto_eq.ses_analiz", level="WARNING"):
            tap.start()
            try:
                self.assertTrue(wait_until(lambda: len(calls) >= 3))
                self.assertTrue(tap.running)
            finally:
                tap.stop()
        self.assertFalse(tap.running)

    def test_stop_kills_and_reaps(self):
        tap, runner, clock = self.make()
        tap.start()
        p = runner.wait_spawn(1)
        tap.stop()
        self.assertFalse(tap.running)
        self.assertTrue(p.terminated)
        self.assertTrue(p.waited)
        n = len(runner.procs)
        time.sleep(0.05)
        self.assertEqual(len(runner.procs), n, "durduktan sonra yeni süreç açıldı")

    def test_start_is_idempotent(self):
        tap, runner, clock = self.make()
        tap.start()
        tap.start()
        try:
            runner.wait_spawn(1)
            time.sleep(0.05)
            self.assertEqual(len(runner.procs), 1)
        finally:
            tap.stop()

    def test_window_does_not_block_while_reader_waits(self):
        tap, runner, clock = self.make()
        tap.start()
        try:
            runner.wait_spawn(1)       # reader thread now blocked in read()
            t0 = time.perf_counter()
            tap.window(0.0)
            self.assertLess(time.perf_counter() - t0, 0.05)
        finally:
            tap.stop()


# ================================================================ features
class TestFeatures(unittest.TestCase):
    def test_band_ratios_sines(self):
        t = np.arange(4 * R) / R
        for f, band in ((40, "r_sub"), (120, "r_bass"), (350, "r_lowmid"), (1000, "r_mid"),
                        (3500, "r_highmid"), (8000, "r_high")):
            x = (0.3 * np.sin(2 * np.pi * f * t)).astype(np.float32)
            ft = sa.features(x, R)
            self.assertGreater(ft[band], 0.9, f"{f} Hz → {band}")
            total = sum(ft["r_" + b[0]] for b in sa.BANDS)
            self.assertAlmostEqual(total, 1.0, places=3)

    def test_sine_crest_and_noise_flatness(self):
        t = np.arange(4 * R) / R
        sine = sa.features((0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), R)
        self.assertAlmostEqual(sine["crest_db"], 3.0, delta=0.3)
        noise = sa.features(np.random.default_rng(0).standard_normal(4 * R).astype(np.float32) * 0.1, R)
        self.assertGreater(noise["flatness"], 0.4)
        self.assertLess(sine["flatness"], 0.05)

    def test_tempo_of_kick_track(self):
        for bpm in (100.0, 125.0, 140.0):
            ft = sa.features(kick_track(8.0, bpm), R)
            self.assertAlmostEqual(ft["tempo"], bpm, delta=bpm * 0.04, msg=f"{bpm}")
            self.assertGreater(ft["beat_low"], 0.4)

    def test_values_are_python_floats(self):
        ft = sa.features(kick_track(4.0), R)
        self.assertTrue(all(type(v) is float for v in ft.values()), ft)

    def test_degenerate_inputs_finite(self):
        for x in (np.zeros(3 * R, np.float32), np.zeros(100, np.float32),
                  np.full(2 * R, np.nan, np.float32), np.ones(2 * R, np.float32)):
            ft = sa.features(x, R)
            self.assertTrue(all(math.isfinite(v) for v in ft.values()))

    def test_stereo_input_accepted(self):
        x = kick_track(4.0)
        ft = sa.features(np.stack([x, x], axis=1), R)
        self.assertGreater(ft["r_sub"] + ft["r_bass"], 0.5)

    def test_speed_6s(self):
        x = kick_track(6.0)
        sa.features(x, R)
        t0 = time.perf_counter()
        for _ in range(5):
            sa.classify(x, R)
        ms = (time.perf_counter() - t0) / 5 * 1000
        self.assertLess(ms, 100.0, f"6 sn analiz {ms:.1f} ms")


# ================================================================ classify
class TestClassify(unittest.TestCase):
    def test_silence_and_short_none(self):
        self.assertIsNone(sa.classify(np.zeros(6 * R, np.float32), R))
        self.assertIsNone(sa.classify(np.full(6 * R, 1e-5, np.float32), R))
        self.assertIsNone(sa.classify(kick_track(1.0), R))

    def test_kick_track_is_electronic(self):
        g = sa.classify(kick_track(6.0), R)
        self.assertIsNotNone(g)
        self.assertEqual(g.group, "elektronik")
        self.assertIn("BPM", g.reason)
        self.assertGreater(g.confidence, 0.4)

    def test_speech_like(self):
        g = sa.classify(speech_like(8.0), R)
        self.assertIsNotNone(g)
        self.assertEqual(g.group, "konusma")

    def test_strings_not_electronic(self):
        g = sa.classify(strings_like(8.0), R)
        self.assertTrue(g is None or g.group == "klasik_akustik", g)

    def test_scores_cover_groups(self):
        g = sa.classify(kick_track(6.0), R)
        self.assertEqual(set(g.scores), set(MUSIC_GROUPS))
        self.assertTrue(all(0.0 <= v <= 1.0 for v in g.scores.values()))

    def test_level_invariant(self):
        a = sa.classify(kick_track(6.0, level=0.5), R)
        b = sa.classify(kick_track(6.0, level=0.05), R)
        self.assertEqual(a.group, b.group)

    def test_long_window(self):
        x = np.concatenate([kick_track(12.0), kick_track(12.0, seed=3)])
        g = sa.classify(x, R)
        self.assertEqual(g.group, "elektronik")


class FakeModel:
    def __init__(self, probs=None, exc=None):
        self.probs, self.exc, self.calls = probs, exc, 0

    def predict(self, x, rate):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.probs


class TestFusion(unittest.TestCase):
    def test_model_none_or_failing_falls_back_to_dsp(self):
        x = kick_track(6.0)
        base = sa.classify(x, R)
        for m in (FakeModel(None), FakeModel(exc=RuntimeError("onnx")), FakeModel({})):
            with self.assertLogs("oto_eq.ses_analiz", level="DEBUG"):
                sa.log.debug("x")
                g = sa.classify(x, R, model=m)
            self.assertEqual(g.group, base.group)
            self.assertEqual(m.calls, 1)

    def test_model_agreement(self):
        x = kick_track(6.0)
        g = sa.classify(x, R, model=FakeModel({"elektronik": 0.9, "pop_vokal": 0.1}))
        self.assertEqual(g.group, "elektronik")
        self.assertGreater(g.confidence, sa.FUSED_MIN)
        self.assertIn("model %90", g.reason)

    def test_model_disagreement_is_unclear(self):
        """Confident model for another group vs confident DSP → fused < 0.6 → None."""
        g = sa.classify(kick_track(6.0), R, model=FakeModel({"klasik_akustik": 0.9,
                                                              "elektronik": 0.1}))
        self.assertIsNone(g)

    def test_weak_model_must_agree_with_dsp(self):
        """Model top < 0.6 cannot decide against the DSP top group."""
        x = kick_track(6.0)
        dsp = sa.dsp_probs(sa.features(x, R))
        self.assertEqual(max(dsp, key=dsp.get), "elektronik")
        g = sa.classify(x, R, model=FakeModel({"elektronik": 0.55, "metal_rock": 0.45}))
        self.assertEqual(g.group, "elektronik")

    def test_dsp_only_never_says_pop(self):
        self.assertNotIn("pop_vokal", sa.DSP_ONLY_GROUPS)

    def test_model_can_decide_when_dsp_unclear(self):
        x = strings_like(8.0)
        g = sa.classify(x, R, model=FakeModel({"klasik_akustik": 0.92, "pop_vokal": 0.05}))
        self.assertIsNotNone(g)
        self.assertEqual(g.group, "klasik_akustik")

    def test_dsp_veto_blocks_impossible_model_answer(self):
        """Model says electronic, but the clip has no low end at all → not electronic."""
        g = sa.classify(speech_like(8.0), R, model=FakeModel({"elektronik": 0.95}))
        self.assertTrue(g is None or g.group != "elektronik", g)

    def test_unknown_labels_ignored(self):
        x = kick_track(6.0)
        g = sa.classify(x, R, model=FakeModel({"bogus": 1.0, "elektronik": float("nan")}))
        self.assertEqual(g.group, "elektronik")

    def test_detail_filled_without_audio(self):
        d = {}
        g = sa.classify(kick_track(6.0), R, model=FakeModel({"elektronik": 0.9}), detail=d)
        self.assertEqual(d["group"], g.group)
        self.assertAlmostEqual(d["confidence"], g.confidence)
        self.assertEqual(set(d["dsp_scores"]), set(MUSIC_GROUPS))
        self.assertAlmostEqual(d["model_probs"]["elektronik"], 1.0)
        self.assertEqual(d["fused"], d["scores"])
        self.assertIsNone(d["personal"])
        self.assertIn("harm_ratio", d["features"])
        for v in d.values():                          # only numbers/dicts, never samples
            self.assertNotIsInstance(v, np.ndarray)
        d2 = {}
        self.assertIsNone(sa.classify(kick_track(6.0), R, model=FakeModel(
            {"klasik_akustik": 0.9, "elektronik": 0.1}), detail=d2))
        self.assertEqual(d2["group"], "")              # unclear windows are described too
        d3 = {}
        sa.classify(np.zeros(6 * R, np.float32), R, detail=d3)
        self.assertEqual(d3, {})                       # silence: nothing analysed

    def test_decide_matches_classify(self):
        x = kick_track(6.0)
        f = sa.features(x, R)
        pd_ = sa.dsp_probs(f)
        for probs in ({"elektronik": 0.9}, {"klasik_akustik": 0.9, "elektronik": 0.1}, None):
            pm = sa.clean_model_probs(probs) if probs else None
            top, ok, _ = sa.decide(pd_, pm, f)
            g = sa.classify(x, R, model=FakeModel(probs) if probs else None)
            self.assertEqual(g.group if g else None, top if ok else None)

    def test_personal_hybrid_rule(self):
        x = kick_track(6.0)
        f = sa.features(x, R)
        pd_ = sa.dsp_probs(f)
        pm = sa.clean_model_probs({"elektronik": 0.9, "pop_vokal": 0.1})
        base_top, base_ok, _ = sa.decide(pd_, pm, f)
        self.assertEqual((base_top, base_ok), ("elektronik", True))

        def adj(**kw):
            d = {g: 0.0 for g in MUSIC_GROUPS}
            d.update(kw)
            return d
        # confident personal override
        self.assertEqual(sa.decide(pd_, pm, f, adj(pop_vokal=0.9, elektronik=0.1))[:2],
                         ("pop_vokal", True))
        # not confident enough to override, base group kept while personal ≥ KEEP_MIN
        self.assertEqual(sa.decide(pd_, pm, f, adj(pop_vokal=0.45, elektronik=0.55))[:2],
                         ("elektronik", True))
        # personal disagrees with the base group → unclear (no wrong EQ)
        top, ok, _ = sa.decide(pd_, pm, f, adj(pop_vokal=0.7, elektronik=0.3))
        self.assertFalse(ok)
        # the physical vetoes still bind the personal model
        fv = dict(f, r_sub=0.0)
        self.assertFalse(sa.decide(pd_, pm, fv, adj(elektronik=0.99, pop_vokal=0.01))[1])
        # without a model the personal adjustment is ignored (DSP-only rule)
        self.assertEqual(sa.decide(pd_, None, f, adj(pop_vokal=1.0)), sa.decide(pd_, None, f))

    def test_classify_with_personal(self):
        class P:
            def __init__(self, out=None, exc=None):
                self.out, self.exc, self.calls = out, exc, 0

            def adjust(self, pd_, pm, f):
                self.calls += 1
                if self.exc:
                    raise self.exc
                return self.out
        x = kick_track(6.0)
        m = FakeModel({"elektronik": 0.9, "pop_vokal": 0.1})
        d = {}
        g = sa.classify(x, R, model=m, personal=P({"pop_vokal": 0.95, "elektronik": 0.05}),
                        detail=d)
        self.assertEqual(g.group, "pop_vokal")
        self.assertIn("kişisel model", g.reason)
        self.assertAlmostEqual(d["personal"]["pop_vokal"], 0.95)
        self.assertEqual(d["fused"]["elektronik"], sa.fuse(d["dsp_scores"],
                                                           d["model_probs"])["elektronik"])
        for p in (P(None), P(exc=RuntimeError("bozuk"))):      # None/failing → base fusion
            with self.assertLogs("oto_eq.ses_analiz", level="DEBUG"):
                sa.log.debug("x")
                g = sa.classify(x, R, model=m, personal=p)
            self.assertEqual(g.group, "elektronik")
            self.assertNotIn("kişisel", g.reason)
            self.assertEqual(p.calls, 1)
        p = P({"pop_vokal": 1.0})
        self.assertEqual(sa.classify(x, R, personal=p).group, "elektronik")   # no model
        self.assertEqual(p.calls, 0)

    def test_fuse_rule(self):
        dsp = {g: 0.0 for g in MUSIC_GROUPS}
        dsp["elektronik"] = 0.8
        f = sa.fuse(dsp, {"elektronik": 0.2, "metal_rock": 0.8})
        self.assertAlmostEqual(f["elektronik"], sa.DSP_WEIGHT * 0.8 + sa.MODEL_WEIGHT * 0.2)
        self.assertAlmostEqual(f["metal_rock"], sa.MODEL_WEIGHT * 0.8)


# ================================================================ analyzer
class FakeTap:
    def __init__(self, x=None, rate=R):
        self.rate = rate
        self.running = False
        self.starts = self.stops = 0
        self.x = x if x is not None else np.zeros(0, np.float32)
        self.calls = []

    def start(self):
        self.running = True
        self.starts += 1

    def stop(self):
        self.running = False
        self.stops += 1

    def window(self, t0, t1=None):
        self.calls.append((t0, t1))
        return self.x


class TestAnalyzer(unittest.TestCase):
    def test_ensure_starts_and_idles_out(self):
        clock = Clock(0.0)
        tap = FakeTap()
        an = sa.AudioAnalyzer(tap=tap, idle_s=15.0, clock=clock)
        an.ensure(True)
        an.ensure(True)
        self.assertEqual(tap.starts, 1)
        clock.t = 10.0
        an.ensure(False)
        self.assertTrue(tap.running)
        clock.t = 15.5
        an.ensure(False)
        self.assertFalse(tap.running)
        an.ensure(True)
        self.assertEqual(tap.starts, 2)
        an.stop()
        self.assertFalse(tap.running)

    def test_idle_timer_stops_without_more_calls(self):
        tap = FakeTap()
        an = sa.AudioAnalyzer(tap=tap, idle_s=0.05)
        an.ensure(True)
        an.ensure(False)
        self.assertTrue(wait_until(lambda: not tap.running, 2.0))
        an.stop()

    def test_active_again_cancels_idle_timer(self):
        tap = FakeTap()
        an = sa.AudioAnalyzer(tap=tap, idle_s=0.1)
        an.ensure(True)
        an.ensure(False)
        an.ensure(True)
        time.sleep(0.3)
        self.assertTrue(tap.running)
        an.stop()

    def test_guess_too_short_none(self):
        tap = FakeTap(kick_track(2.0))
        an = sa.AudioAnalyzer(tap=tap)
        self.assertIsNone(an.guess(1.0, 3.0))
        self.assertEqual(tap.calls, [(1.0, 3.0)])
        tap.x = kick_track(6.0)
        self.assertIsNone(an.guess(1.0, None, min_s=10.0))

    def test_guess_result(self):
        an = sa.AudioAnalyzer(tap=FakeTap(kick_track(6.0)))
        g = an.guess(5.0)
        self.assertIsInstance(g, GenreResult)
        self.assertEqual(g.group, "elektronik")
        self.assertTrue(g.source.startswith("ses analizi: "))
        self.assertTrue(0.0 < g.confidence <= 1.0)
        self.assertIsNotNone(an.last)

    def test_guess_caps_window_length(self):
        an = sa.AudioAnalyzer(tap=FakeTap(np.concatenate([np.zeros(40 * R, np.float32),
                                                           kick_track(30.0)])))
        g = an.guess(0.0)
        self.assertEqual(g.group, "elektronik")      # the silent first 40 s were not analysed

    def test_guess_never_raises(self):
        class Broken(FakeTap):
            def window(self, t0, t1=None):
                raise RuntimeError("boom")

        an = sa.AudioAnalyzer(tap=Broken())
        with self.assertLogs("oto_eq.ses_analiz", level="ERROR"):
            self.assertIsNone(an.guess(0.0))

    def test_guess_with_model(self):
        m = FakeModel({"elektronik": 0.8})
        an = sa.AudioAnalyzer(tap=FakeTap(kick_track(6.0)), model=m)
        g = an.guess(0.0)
        self.assertEqual(g.group, "elektronik")
        self.assertEqual(m.calls, 1)

    def test_last_detail(self):
        tap = FakeTap(kick_track(6.0))
        an = sa.AudioAnalyzer(tap=tap, model=FakeModel({"elektronik": 0.8}))
        self.assertIsNone(an.last_detail)
        an.guess(5.0, 11.0)
        d = an.last_detail
        self.assertEqual((d["since"], d["until"]), (5.0, 11.0))
        self.assertAlmostEqual(d["window_s"], 6.0, places=1)
        self.assertEqual(d["group"], "elektronik")
        tap.x = kick_track(2.0)                         # too short → cleared
        self.assertIsNone(an.guess(5.0))
        self.assertIsNone(an.last_detail)

    def test_analyzer_passes_personal(self):
        class P:
            def adjust(self, pd_, pm, f):
                return {"pop_vokal": 0.9, "elektronik": 0.1}
        an = sa.AudioAnalyzer(tap=FakeTap(kick_track(6.0)), model=FakeModel({"elektronik": 0.8}),
                              personal=P())
        g = an.guess(0.0)
        self.assertEqual(g.group, "pop_vokal")
        self.assertIsNotNone(an.last_detail["personal"])
        self.assertIsNone(sa.AudioAnalyzer(tap=FakeTap()).personal)

    def test_default_tap(self):
        an = sa.AudioAnalyzer()
        self.assertEqual(an.tap.source, "oto_eq_muzik.monitor")
        self.assertEqual(an.tap.rate, 22050)
        self.assertFalse(an.tap.running)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_evidence_source_key_fixed(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"vokal ağırlıklı orta bölge": "gesangsbetonte Mitten"})
        self.assertEqual(sa._evidence("pop_vokal", {}), "gesangsbetonte Mitten")


if __name__ == "__main__":
    unittest.main()
