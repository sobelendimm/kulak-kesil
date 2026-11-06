"""Audio analysis for the music genre variants (CONTRACT CHANGE 4, B2).

Name-based sources (tur.py) fail for small producers; this module listens to
what actually plays on the Müzik sink and makes a conservative DSP guess.

    AudioTap(source="oto_eq_muzik.monitor")  background parec → ring buffer
    features(x, rate) -> dict[str, float]     numpy-only descriptors
    classify(x, rate, model=None, personal=None, detail=None) -> AudioGuess | None
    decide(dsp, model, features, adjusted=None)  the pure decision rule (base / personal)
    AudioAnalyzer(tap=None, model=None, personal=None)  ensure / guess / stop; last_detail

The monitor of the filter-chain sink carries the PRE-EQ signal (the sink's
input), so our own variant EQ never feeds back into the decision.
Nothing is written to disk; audio lives only in the ring buffer (≤ 45 s).
See docs/NOTES-ses-analiz.md for the rules, the calibration and CPU cost.
"""
from __future__ import annotations

import collections
import logging
import math
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from . import config
from .i18n import _
from .models import MUSIC_GROUPS, GenreResult

log = logging.getLogger("oto_eq.ses_analiz")

DEFAULT_SOURCE = "oto_eq_muzik.monitor"
DEFAULT_RATE = 22050

# ============================================================================
#  CAPTURE
# ============================================================================


def capture_argv(source: str, rate: int) -> list[str]:
    """parec (pipewire-pulse) if present, else pw-record; raw mono float32 to stdout."""
    if shutil.which("parec") or not shutil.which("pw-record"):
        return ["parec", "--raw", "--format=float32le", "--channels=1", f"--rate={rate}",
                "--latency-msec=100", "--client-name=Kulak Kesil ses analizi",
                "--stream-name=Kulak Kesil ses analizi", "-d", source]
    return ["pw-record", "--target", source, "-P", "stream.capture.sink=true",
            "--rate", str(rate), "--channels", "1", "--format", "f32", "--raw", "-"]


def _default_runner(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL, bufsize=0, env=config.C_ENV)


def _kill(proc: Any) -> None:
    """Terminate → SIGKILL → reap; never raises (no zombies)."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.5)
        else:
            proc.wait(timeout=0.1)
    except Exception:  # noqa: BLE001
        log.debug("kayıt süreci kapatılırken hata", exc_info=True)
    try:
        if getattr(proc, "stdout", None):
            proc.stdout.close()
    except Exception:  # noqa: BLE001
        pass


class AudioTap:
    """Background capture of ``source`` into a ring buffer with monotonic timestamps.

    ``runner(argv)`` returns a Popen-like object (``stdout.read(n)``, ``poll()``,
    ``terminate()``, ``kill()``, ``wait(timeout)``); tests inject a fake one.
    The capture process is restarted with backoff (0.5 → 10 s) if it dies.
    """

    CHUNK_BYTES = 4410 * 4          # 0.2 s at 22050 Hz: few wake-ups, low CPU
    BACKOFF_MIN = 0.5
    BACKOFF_MAX = 10.0
    STABLE_SECS = 10.0

    def __init__(self, source: str = DEFAULT_SOURCE, rate: int = DEFAULT_RATE,
                 seconds: float = 45.0, runner: Callable[[list[str]], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.source = source
        self.rate = int(rate)
        self.seconds = float(seconds)
        self._runner = runner or _default_runner
        self._clock = clock
        self._cap = max(1, int(self.rate * self.seconds))
        self._buf = np.zeros(self._cap, dtype=np.float32)
        self._total = 0                       # samples ever written
        # (first sample index, sample count, monotonic time of the chunk's LAST sample)
        self._chunks: collections.deque[tuple[int, int, float]] = collections.deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: Any = None
        self._thread: threading.Thread | None = None
        self.restarts = 0

    # -- public
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oto_eq.ses_analiz.tap",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _kill(proc)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None

    def window(self, t0: float, t1: float | None = None) -> np.ndarray:
        """Copy of the samples captured between monotonic times t0..t1 (default: now)."""
        if t1 is None:
            t1 = math.inf
        parts: list[np.ndarray] = []
        with self._lock:
            oldest = self._total - self._cap
            for start, n, t_end in self._chunks:
                t_start = t_end - n / self.rate
                if t_end < t0 or t_start > t1:
                    continue
                a = 0 if t0 <= t_start else int(math.ceil((t0 - t_start) * self.rate - 1e-6))
                b = n if t1 >= t_end else min(n, int(math.floor((t1 - t_start) * self.rate + 1e-6)))
                a = max(a, oldest - start)
                if b <= a:
                    continue
                parts.append(self._read(start + a, b - a))
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    def latest(self, seconds: float) -> np.ndarray:
        """The last ``seconds`` of audio (ignores timestamps)."""
        with self._lock:
            n = min(int(seconds * self.rate), self._total, self._cap)
            return self._read(self._total - n, n) if n > 0 else np.zeros(0, np.float32)

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()

    # -- internals
    def _read(self, idx: int, n: int) -> np.ndarray:
        i = idx % self._cap
        if i + n <= self._cap:
            return self._buf[i:i + n].copy()
        k = self._cap - i
        return np.concatenate((self._buf[i:], self._buf[:n - k]))

    def _append(self, samples: np.ndarray, t_end: float) -> None:
        n = len(samples)
        if n == 0:
            return
        with self._lock:
            if n > self._cap:
                samples, n = samples[-self._cap:], self._cap
            i = self._total % self._cap
            k = min(n, self._cap - i)
            self._buf[i:i + k] = samples[:k]
            if k < n:
                self._buf[:n - k] = samples[k:]
            self._chunks.append((self._total, n, t_end))
            self._total += n
            oldest = self._total - self._cap
            while self._chunks and self._chunks[0][0] + self._chunks[0][1] <= oldest:
                self._chunks.popleft()

    def _run(self) -> None:
        backoff = self.BACKOFF_MIN
        argv = capture_argv(self.source, self.rate)
        while not self._stop.is_set():
            started = self._clock()
            try:
                proc = self._runner(argv)
            except Exception as e:  # noqa: BLE001
                log.warning("ses analizi kaydı başlatılamadı: %s", e)
                proc = None
            if proc is not None:
                with self._lock:
                    self._proc = proc
                if not self._stop.is_set():
                    self._pump(proc)
                _kill(proc)
                with self._lock:
                    self._proc = None
                if self._stop.is_set():
                    break
                self.restarts += 1
                log.info("ses analizi kaydı sonlandı (kod %s), yeniden denenecek",
                         getattr(proc, "returncode", None))
            if self._clock() - started >= self.STABLE_SECS:
                backoff = self.BACKOFF_MIN
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def _pump(self, proc: Any) -> None:
        out = proc.stdout
        rest = b""
        while not self._stop.is_set():
            try:
                data = out.read(self.CHUNK_BYTES)
            except (OSError, ValueError):
                return
            if not data:
                return
            data = rest + data
            usable = len(data) - len(data) % 4
            rest = data[usable:]
            if usable:
                self._append(np.frombuffer(data[:usable], dtype="<f4").astype(np.float32),
                             self._clock())


# ============================================================================
#  FEATURES
# ============================================================================

BANDS: tuple[tuple[str, float, float], ...] = (
    ("sub", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("lowmid", 250.0, 500.0),
    ("mid", 500.0, 2000.0),
    ("highmid", 2000.0, 6000.0),
    ("high", 6000.0, 11025.0),
)
_EPS = 1e-12


def _db(v):
    return 10.0 * np.log10(np.maximum(v, _EPS))


def _ac_norm(o: np.ndarray) -> np.ndarray:
    """Normalized autocorrelation (lag 0 = 1) of a mean-removed series via FFT."""
    o = o - o.mean()
    n = len(o)
    if n < 4 or not np.any(o):
        return np.zeros(max(n, 1))
    f = np.fft.rfft(o, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    # unbiased-ish: compensate the shrinking overlap so long lags are not penalised
    ac = ac / np.maximum(n - np.arange(n), 1) * n
    return ac / ac[0] if ac[0] > 0 else np.zeros(n)


def _tempo(ac: np.ndarray, fps: float) -> tuple[float, float]:
    """(bpm, strength) from an onset autocorrelation; tempo prior centred on 120 BPM."""
    lo = int(fps * 60 / 200)
    hi = min(len(ac) - 2, int(fps * 60 / 60) + 1)
    if hi <= lo + 2:
        return 0.0, 0.0
    lags = np.arange(lo, hi + 1)
    vals = ac[lo:hi + 1]
    bpm = 60.0 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)
    k = int(np.argmax(vals * (0.6 + 0.4 * prior)))
    strength = float(max(0.0, vals[k]))
    lag = float(lags[k])
    if 0 < k < len(vals) - 1:      # parabolic refinement
        a, b, c = vals[k - 1], vals[k], vals[k + 1]
        den = a - 2 * b + c
        if den < 0:
            lag += float(0.5 * (a - c) / den)
    return float(60.0 * fps / lag), strength


def features(x: np.ndarray, rate: int) -> dict[str, float]:
    """Numpy-only descriptors of a mono signal (any length ≥ ~1 s).

    Keys (all finite floats):
      rms_db, active_s          level (dBFS) and non-silent duration
      r_sub … r_high            band energy ratios (sum 1 over 20 Hz–Nyquist)
      sub_share                 sub / (sub + bass)
      centroid, rolloff, flatness, centroid_cv, flux
      crest_db, lra_db, pause_ratio, env_std_db
      beat_low, beat_full, tempo, onset_rate, low_pump_db
      mod_speech, mod_peak      4 Hz speech modulation share / modulation peakiness
      stability, harm_ratio     frame-to-frame spectral similarity (~90 ms apart) /
                                harmonic share of a median-filter H/P split
    """
    x = np.asarray(x, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1 if x.shape[1] <= 8 else 0).astype(np.float32)
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    n_fft = 2048 if rate <= 24000 else 4096
    hop = n_fft // 8
    out: dict[str, float] = {}
    if len(x) < n_fft * 4:
        x = np.pad(x, (0, n_fft * 4 - len(x)))
    x = x - float(x.mean())
    fps = rate / hop

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop]
    win = np.hanning(n_fft).astype(np.float32)
    spec = np.fft.rfft(frames * win, axis=1)
    p = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)          # (T, F)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / rate)
    nyq = rate / 2.0

    e = p[:, (freqs >= 20.0)].sum(axis=1) + _EPS                        # frame energy
    e_db = _db(e)
    ref = float(np.percentile(e_db, 95))
    active = e_db > ref - 30.0
    # absolute gate: −65 dBFS-ish (window energy scale: sum(win²)·n_fft/2 per unit-amplitude sine)
    abs_gate = float(_db(np.array((win ** 2).sum() * n_fft / 4 * 10 ** (-65 / 10))))
    active &= e_db > abs_gate
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    out["rms_db"] = 20.0 * math.log10(max(rms, 1e-9))
    out["active_s"] = float(active.sum() / fps)

    psum = p[active].sum(axis=0) if active.any() else p.sum(axis=0)
    total = float(psum[(freqs >= 20.0)].sum()) + _EPS
    for name, lo, hi in BANDS:
        sel = (freqs >= lo) & (freqs < min(hi, nyq + 1))
        out["r_" + name] = float(psum[sel].sum() / total)
    out["sub_share"] = out["r_sub"] / (out["r_sub"] + out["r_bass"] + _EPS)

    sel = freqs >= 20.0
    fs, ps = freqs[sel], psum[sel]
    out["centroid"] = float((fs * ps).sum() / (ps.sum() + _EPS))
    cum = np.cumsum(ps)
    out["rolloff"] = float(fs[min(len(fs) - 1, int(np.searchsorted(cum, 0.85 * cum[-1])))])

    pa = p[active] if active.sum() >= 8 else p
    fb = (freqs >= 250.0) & (freqs <= min(8000.0, nyq))
    pb = pa[:, fb].astype(np.float64) + 1e-10
    flat = np.exp(np.mean(np.log(pb), axis=1)) / np.mean(pb, axis=1)
    out["flatness"] = float(np.median(flat))
    fc = (pa[:, sel] * fs).sum(axis=1) / (pa[:, sel].sum(axis=1) + _EPS)
    out["centroid_cv"] = float(np.std(fc) / (np.mean(fc) + _EPS))

    # positive log-spectral flux (full band) and low-band onset envelope
    lp = np.log1p(p[:, (freqs >= 30) & (freqs <= 8000)] / (np.median(e) / n_fft + _EPS))
    flux = np.maximum(np.diff(lp, axis=0), 0.0).mean(axis=1)
    out["flux"] = float(np.mean(flux))
    lowb = (freqs >= 30.0) & (freqs <= 150.0)
    low_db = _db(p[:, lowb].sum(axis=1))
    o_low = np.maximum(np.diff(low_db), 0.0)
    out["low_pump_db"] = float(np.std(np.clip(low_db, ref - 60, None)))
    t_low, s_low = _tempo(_ac_norm(o_low), fps)
    t_full, s_full = _tempo(_ac_norm(flux), fps)
    out["beat_low"] = s_low
    out["beat_full"] = s_full
    out["tempo"] = t_low if s_low >= s_full else t_full
    # onset density: peaks of the flux above its median + 1.5 MAD, ≥ 70 ms apart
    thr = np.median(flux) + 1.5 * (np.median(np.abs(flux - np.median(flux))) + 1e-9)
    pk = (flux[1:-1] > thr) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] > flux[2:])
    idx = np.flatnonzero(pk)
    if len(idx):
        keep = [idx[0]]
        for i in idx[1:]:
            if i - keep[-1] >= 0.07 * fps:
                keep.append(i)
        n_on = len(keep)
    else:
        n_on = 0
    out["onset_rate"] = float(n_on / (len(flux) / fps))

    # tonal stability + harmonic/percussive split (median filters on a coarse
    # spectrogram: every 3rd frame ≈ 35 ms, bin pairs ≈ 21 Hz, 60 Hz–5 kHz)
    hb = np.flatnonzero((freqs >= 60.0) & (freqs <= min(5000.0, nyq)))
    hb = hb[: len(hb) // 2 * 2]
    mag = np.sqrt(pa[::3][:, hb].reshape(-1, len(hb) // 2, 2).sum(axis=2))
    if len(mag) >= 12:
        a, b = mag[:-3], mag[3:]
        num = (a * b).sum(axis=1)
        den = np.sqrt((a * a).sum(axis=1) * (b * b).sum(axis=1)) + _EPS
        out["stability"] = float(np.median(num / den))
        h = np.median(np.lib.stride_tricks.sliding_window_view(
            np.pad(mag, ((4, 4), (0, 0)), mode="edge"), 9, axis=0), axis=-1)
        pc = np.median(np.lib.stride_tricks.sliding_window_view(
            np.pad(mag, ((0, 0), (4, 4)), mode="edge"), 9, axis=1), axis=-1)
        hp = (h * h).sum() / ((h * h).sum() + (pc * pc).sum() + _EPS)
        out["harm_ratio"] = float(hp)
    else:
        out["stability"] = 0.0
        out["harm_ratio"] = 0.5

    # dynamics: crest, short-term loudness range, pauses
    peak = float(np.percentile(np.abs(x), 99.99))
    out["crest_db"] = 20.0 * math.log10(max(peak, 1e-9) / max(rms, 1e-9))
    k = max(1, int(round(0.4 * fps)))
    st = np.convolve(e, np.ones(k) / k, mode="valid")
    st_db = _db(st)
    g = st_db[st_db > max(abs_gate, float(np.max(st_db)) - 40.0)]
    out["lra_db"] = float(np.percentile(g, 95) - np.percentile(g, 10)) if len(g) > 2 else 0.0
    k2 = max(1, int(round(0.05 * fps)))
    blk = e[: len(e) // k2 * k2].reshape(-1, k2).mean(axis=1)
    blk_db = _db(blk)
    out["pause_ratio"] = float(np.mean(blk_db < np.percentile(blk_db, 90) - 18.0))

    # speech modulation: 300–3400 Hz log envelope, modulation spectrum 0.5–16 Hz
    sb = (freqs >= 300.0) & (freqs <= 3400.0)
    env = _db(p[:, sb].sum(axis=1))
    env = np.clip(env, np.percentile(env, 95) - 45.0, None)
    out["env_std_db"] = float(np.std(env))
    envc = (env - env.mean()) * np.hanning(len(env))
    m = np.abs(np.fft.rfft(envc)) ** 2
    mf = np.fft.rfftfreq(len(envc), 1.0 / fps)
    band = (mf >= 0.5) & (mf <= 16.0)
    sp = (mf >= 2.5) & (mf <= 6.5)
    tot = float(m[band].sum()) + _EPS
    out["mod_speech"] = float(m[sp].sum() / tot)
    mb = m[(mf >= 0.7) & (mf <= 10.0)]
    out["mod_peak"] = float(mb.max() / (mb.mean() + _EPS)) if len(mb) else 0.0

    for key, v in out.items():
        v = float(v)
        out[key] = v if math.isfinite(v) else 0.0
    return out


# ============================================================================
#  CLASSIFIER (rules; thresholds calibrated in docs/NOTES-ses-analiz.md)
# ============================================================================


@dataclass(frozen=True)
class AudioGuess:
    group: str                       # one of MUSIC_GROUPS
    confidence: float                # 0..1
    scores: dict[str, float]         # per group evidence 0..1 (fused when a model is used)
    reason: str                      # short Turkish text, e.g. "güçlü alt bas, düzenli vuruş ~124 BPM"
    features: dict[str, float] = field(default_factory=dict, compare=False, repr=False)


def _ramp(v: float, lo: float, hi: float) -> float:
    """0 at lo, 1 at hi (linear, clipped; lo > hi = falling ramp)."""
    if hi == lo:
        return float(v >= hi)
    return float(min(1.0, max(0.0, (v - lo) / (hi - lo))))


def _vector(f: dict[str, float]) -> np.ndarray:
    """Level-independent, roughly Gaussian transform of ``features()`` for the linear model."""
    lg = math.log10
    return np.array([
        lg(f["r_sub"] + 1e-4), lg(f["r_bass"] + 1e-4), lg(f["r_lowmid"] + 1e-4),
        lg(f["r_mid"] + 1e-4), lg(f["r_highmid"] + 1e-4), lg(f["r_high"] + 1e-5),
        lg(f["sub_share"] + 1e-3), lg(max(f["centroid"], 20.0)), lg(max(f["rolloff"], 20.0)),
        lg(f["flatness"] + 1e-4), f["centroid_cv"], f["flux"], f["low_pump_db"],
        f["beat_low"], f["beat_full"], f["onset_rate"], f["crest_db"], f["lra_db"],
        f["pause_ratio"], f["env_std_db"], f["mod_speech"], lg(f["mod_peak"] + 1.0),
        f["stability"], f["harm_ratio"],
    ], dtype=np.float64)


# Softmax-regression weights over _vector() (standardised), classes = MUSIC_GROUPS order
# (metal_rock, elektronik, klasik_akustik, pop_vokal, konusma). Trained on 966 windows of
# 139 CC-licensed archive.org tracks (6 s mid-track, 5.5 s and 24.5 s from the track
# start), class-balanced, L2 λ=1. Regenerate with the calibration recipe in
# docs/NOTES-ses-analiz.md whenever features() changes.
_MU = (-2.073, -0.4838, -0.8318, -0.8521, -1.603, -2.608, -1.593, 2.717, 2.865, -1.927, 0.7664,
       0.04317, 8.057, 0.2581, 0.4065, 4.63, 13.68, 9.146, 0.08687, 6.938, 0.3157, 1.044, 0.7714,
       0.6277)
_SD = (1.16, 0.4401, 0.439, 0.4726, 0.6406, 1.034, 0.8995, 0.2611, 0.3679, 0.9312, 0.4646,
       0.02332, 4.879, 0.1686, 0.2241, 1.113, 2.962, 7.545, 0.1329, 4.631, 0.1393, 0.2078, 0.1355,
       0.1625)
_W = (
    (-0.1026, 0.6603, -0.7629, -0.1679, 0.3731),
    (-0.2444, -0.7711, 0.38, -0.05603, 0.6914),
    (0.1843, -0.2077, -0.3057, -0.3257, 0.6548),
    (-0.1252, -0.02936, 0.03943, -0.1168, 0.2319),
    (0.6092, -0.4763, 0.239, -0.1248, -0.247),
    (-0.179, 0.09727, -0.04876, -0.2098, 0.3403),
    (0.2271, -0.03578, 0.03624, 0.4063, -0.6338),
    (-0.9982, 0.3338, 1.353, -0.2147, -0.4737),
    (0.8065, 0.278, -1.395, 0.6769, -0.3667),
    (1.274, -0.0502, -0.5043, -0.008139, -0.7113),
    (-0.8705, 0.09082, -0.3528, 0.4566, 0.6758),
    (0.5511, -0.2314, -0.7847, 0.13, 0.3349),
    (0.04493, 0.2028, -0.07398, -0.2987, 0.125),
    (0.1466, -0.4076, 0.01835, 0.411, -0.1684),
    (0.07145, 0.6904, 0.004244, -0.203, -0.5632),
    (-0.3151, 0.5132, -0.03975, -0.3107, 0.1523),
    (-0.7463, -0.2043, 0.4765, 0.02758, 0.4466),
    (-0.4709, -0.2297, 0.554, -0.2492, 0.3958),
    (-0.1965, -0.01373, -0.1799, -0.1112, 0.5013),
    (-0.01381, -0.09821, -0.1677, 0.07606, 0.2037),
    (-0.1971, 0.2615, 0.05392, -0.1607, 0.04235),
    (0.07573, 0.07849, 0.1027, 0.1811, -0.4379),
    (0.9599, 0.91, -0.04901, 0.1557, -1.977),
    (-0.06808, -0.9617, -0.0566, 0.642, 0.4444),
)
_B = (-0.6992, -0.007064, 0.7541, 1.214, -1.262)
_GROUPS = ("metal_rock", "elektronik", "klasik_akustik", "pop_vokal", "konusma")
assert set(_GROUPS) == set(MUSIC_GROUPS)

# Decision rule (calibrated, see NOTES): fused = DSP_WEIGHT·p_dsp + MODEL_WEIGHT·p_model,
# decide when fused ≥ FUSED_MIN; a model under MODEL_ALONE_MIN must agree with the DSP top
# group. Without a model the DSP alone must reach DSP_ONLY_MIN and never says pop_vokal.
DSP_WEIGHT = 0.4
MODEL_WEIGHT = 0.6
FUSED_MIN = 0.6
MODEL_ALONE_MIN = 0.6
DSP_ONLY_MIN = 0.9
DSP_ONLY_GROUPS = ("metal_rock", "elektronik", "klasik_akustik", "konusma")
# Personal model (CHANGE 6, D3 kisisel_model; active only after cross-validated gain):
PERSONAL_OVERRIDE_MIN = 0.85    # personal top decides alone from this probability
PERSONAL_KEEP_MIN = 0.5         # a base decision survives if the personal prob. stays ≥ this
MIN_ACTIVE_S = 2.0          # non-silent audio needed
MIN_LEVEL_DB = -55.0        # quieter than this = silence / paused


def dsp_probs(f: dict[str, float]) -> dict[str, float]:
    """Softmax probabilities over MUSIC_GROUPS from ``features()``."""
    z = (_vector(f) - np.asarray(_MU)) / np.asarray(_SD)
    z = np.clip(z, -6.0, 6.0)                 # out-of-range input must not explode
    logits = z @ np.asarray(_W) + np.asarray(_B)
    e = np.exp(logits - logits.max())
    p = e / e.sum()
    return {g: float(v) for g, v in zip(_GROUPS, p)}


def vetoes(f: dict[str, float]) -> set[str]:
    """Groups the physics rules out, whatever a model says."""
    out = set()
    if f["r_sub"] + f["r_bass"] < 0.15 or f["r_sub"] < 0.005:
        out.add("elektronik")                 # electronic/hip-hop without any low end
    if f["beat_low"] > 0.6 and f["pause_ratio"] < 0.01:
        out.add("konusma")                    # steady kick, never a pause
    if f["flatness"] > 0.2:
        out.add("klasik_akustik")             # noise-like distorted spectrum
    return out


def clean_model_probs(probs: Any) -> dict[str, float] | None:
    """Keep MUSIC_GROUPS with finite non-negative values; renormalise to sum 1."""
    if not isinstance(probs, dict):
        return None
    out = {}
    for g in MUSIC_GROUPS:
        try:
            v = float(probs.get(g, 0.0))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            out[g] = v
    s = sum(out.values())
    if s <= 0:
        return None
    return {g: out.get(g, 0.0) / s for g in MUSIC_GROUPS}


def fuse(dsp: dict[str, float], model: dict[str, float]) -> dict[str, float]:
    """Linear opinion pool: DSP_WEIGHT·dsp + MODEL_WEIGHT·model (both sum to 1)."""
    return {g: DSP_WEIGHT * dsp.get(g, 0.0) + MODEL_WEIGHT * model.get(g, 0.0) for g in MUSIC_GROUPS}


def _evidence(group: str, f: dict[str, float]) -> str:
    if group == "elektronik":
        if max(f["beat_low"], f["beat_full"]) >= 0.3 and f["tempo"] > 0:
            return _("alt bas %{yuzde:.0f}, düzenli vuruş ~{bpm:.0f} BPM").format(
                yuzde=100 * f["r_sub"], bpm=f["tempo"])
        return _("alt bas %{yuzde:.0f}").format(yuzde=100 * f["r_sub"])
    if group == "metal_rock":
        return _("yoğun distorsiyonlu orta-tiz, dar dinamik ({db:.0f} dB tepe)").format(
            db=f["crest_db"])
    if group == "klasik_akustik":
        return _("tonal/akustik, geniş dinamik ({db:.0f} dB tepe), alt bas zayıf").format(
            db=f["crest_db"])
    if group == "pop_vokal":
        return _("vokal ağırlıklı orta bölge")
    if group == "konusma":
        return _("konuşma ritmi, duraklama %{yuzde:.0f}").format(yuzde=100 * f["pause_ratio"])
    return ""


def decide(pd_: dict[str, float], pm: dict[str, float] | None, f: dict[str, float],
           adjusted: dict[str, float] | None = None) -> tuple[str, bool, dict[str, float]]:
    """The decision rule as a pure function → (top group, decided?, scores).

    ``adjusted`` (CHANGE 6, D3 kisisel_model): personal probabilities (a correction of
    the base fusion). Hybrid, conservative rule: the personal top group decides alone
    only when ≥ PERSONAL_OVERRIDE_MIN (and not vetoed); otherwise the BASE decision
    stands if the personal model still gives its group ≥ PERSONAL_KEEP_MIN; else unclear.
    """
    if adjusted is not None and pm is not None:
        btop, bok, _kanit = decide(pd_, pm, f)
        scores = dict(adjusted)
        top = max(scores, key=scores.get)
        if scores[top] >= PERSONAL_OVERRIDE_MIN and top not in vetoes(f):
            return top, True, scores
        if bok and scores.get(btop, 0.0) >= PERSONAL_KEEP_MIN:
            return btop, True, scores
        return top, False, scores
    if pm is not None:
        scores = fuse(pd_, pm)
        top = max(scores, key=scores.get)
        ok = scores[top] >= FUSED_MIN
        if ok and max(pm.values()) < MODEL_ALONE_MIN:
            ok = max(pd_, key=pd_.get) == top
    else:
        scores = dict(pd_)
        top = max(scores, key=scores.get)
        ok = scores[top] >= DSP_ONLY_MIN and top in DSP_ONLY_GROUPS
    return top, bool(ok and top not in vetoes(f)), scores


def classify(x: np.ndarray, rate: int, model: Any = None, personal: Any = None,
             detail: dict | None = None) -> AudioGuess | None:
    """Conservative genre guess for one window; None = unclear (→ Genel).

    ``personal`` (optional, CHANGE 6 D3): object with ``adjust(dsp, model, features) ->
    dict[group, prob] | None`` (kisisel_model); None/failing = base fusion.
    ``detail`` (optional dict) is filled with the decision's numbers (no audio):
    features, dsp_scores, model_probs, fused (base), personal (or None), scores, group
    ('' = unclear), confidence — when the window passed the level gate.
    """
    x = np.asarray(x, dtype=np.float32)
    if x.ndim > 1:
        x = x.mean(axis=1 if x.shape[1] <= 8 else 0).astype(np.float32)
    if len(x) < MIN_ACTIVE_S * rate:
        return None
    f = features(x, rate)
    if f["active_s"] < MIN_ACTIVE_S or f["rms_db"] < MIN_LEVEL_DB:
        return None
    pd_ = dsp_probs(f)
    pm = None
    if model is not None:
        try:
            pm = clean_model_probs(model.predict(x, rate))
        except Exception as e:  # noqa: BLE001 - a broken model must not break the DSP
            log.warning("ses modeli tahmini başarısız: %s", e)
    adj = None
    if personal is not None and pm is not None:
        try:
            adj = clean_model_probs(personal.adjust(pd_, pm, f))
        except Exception as e:  # noqa: BLE001 - a broken personal model = base fusion
            log.warning("kişisel model uygulanamadı: %s", e)
    top, ok, scores = decide(pd_, pm, f, adj)
    if detail is not None:
        detail.update({
            "features": dict(f), "dsp_scores": dict(pd_), "model_probs": pm,
            "fused": fuse(pd_, pm) if pm is not None else None, "personal": adj,
            "scores": dict(scores), "group": top if ok else "",
            "confidence": float(scores[top]),
        })
    if not ok:
        log.debug("ses analizi kararsız: %s", {g: round(v, 2) for g, v in scores.items()})
        return None
    reason = _evidence(top, f)
    if pm is not None:
        reason += "; " + _("model %{yuzde:.0f}").format(yuzde=100 * pm[top])
    if adj is not None:
        reason += "; " + _("kişisel model")
    return AudioGuess(top, float(scores[top]), {g: round(float(v), 3) for g, v in scores.items()},
                      reason, f)


# ============================================================================
#  ANALYZER (router-facing)
# ============================================================================


class AudioAnalyzer:
    """Capture only while music plays; conservative genre guess for a time window.

    ``ensure(active)`` is called by the router on every update; the capture stops
    ``idle_s`` after the last active call (a timer also fires if no further call
    comes). ``guess()`` never raises and never blocks on the capture.
    """

    MAX_S = 30.0       # longest window analysed per guess (CPU bound)

    def __init__(self, tap: AudioTap | None = None, model: Any = None, idle_s: float = 15.0,
                 clock: Callable[[], float] = time.monotonic, personal: Any = None):
        self.tap = tap if tap is not None else AudioTap()
        self.model = model
        self.personal = personal                     # kisisel_model.yukle() (CHANGE 6 D3)
        self.idle_s = float(idle_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_active: float | None = None
        self._timer: threading.Timer | None = None
        self.last: AudioGuess | None = None          # last guess (for status / CLI)
        # numbers of the last analysed window (classify detail + since/until/window_s),
        # None when nothing was analysed (too short, silent, error); for kisisel_model
        self.last_detail: dict | None = None

    @property
    def rate(self) -> int:
        return int(getattr(self.tap, "rate", DEFAULT_RATE))

    def ensure(self, active: bool) -> None:
        now = self._clock()
        with self._lock:
            if active:
                self._last_active = now
                self._cancel_timer()
                if not self.tap.running:
                    log.info("ses analizi: dinleme başladı (%s)", getattr(self.tap, "source", "?"))
                    self.tap.start()
                return
            if not self.tap.running:
                return
            if self._last_active is None or now - self._last_active >= self.idle_s:
                self._stop_tap("müzik yok")
            elif self._timer is None:
                delay = self.idle_s - (now - self._last_active) + 0.2
                self._timer = threading.Timer(delay, self._idle_check)
                self._timer.daemon = True
                self._timer.start()

    def guess(self, since: float, until: float | None = None,
              min_s: float = 3.0) -> GenreResult | None:
        self.last_detail = None
        try:
            x = self.tap.window(since, until)
            rate = self.rate
            if len(x) < min_s * rate:
                return None
            if len(x) > self.MAX_S * rate:          # bound CPU: the latest MAX_S seconds
                x = x[-int(self.MAX_S * rate):]
            detail: dict = {}
            g = classify(x, rate, model=self.model, personal=self.personal, detail=detail)
            if detail:
                detail.update(since=since, until=until, window_s=round(len(x) / rate, 2))
                self.last_detail = detail
        except Exception:  # noqa: BLE001 - analysis must never break the router
            log.exception("ses analizi hatası")
            return None
        self.last = g
        if g is None:
            return None
        # "ses analizi" = source KEY (ui_text.SOURCE_NAMES, kisisel_model, tur cache): untranslated
        return GenreResult(g.group, f"ses analizi: {g.reason}", round(g.confidence, 2))

    def stop(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._stop_tap()

    # -- internals
    def _idle_check(self) -> None:
        with self._lock:
            self._timer = None
            if (self.tap.running and self._last_active is not None
                    and self._clock() - self._last_active >= self.idle_s):
                self._stop_tap("müzik yok")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _stop_tap(self, why: str = "") -> None:
        if self.tap.running:
            log.info("ses analizi: dinleme durdu%s", f" ({why})" if why else "")
        self.tap.stop()
