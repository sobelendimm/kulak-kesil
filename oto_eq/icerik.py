"""CHANGE 9 — İçerik denetimi: per-stream content check, independent of the browser.

Metadata (tab title / MPRIS / URL) cannot always say what a browser stream carries:

* Chromium-family browsers (Vivaldi, Chrome, Brave, Edge, Opera…) name every tab's
  stream just "Playback" and expose ONE media session over MPRIS → with a film tab and a
  song tab playing at once, both streams get the same decision;
* a YouTube video without "Official / ft. / - Topic" hints is guessed as Film even when
  it is a live singing performance.

So for such streams (``Decision.zayif`` or ≥2 nameless streams of one browser playing at
once, see ``Decision.grup``) the router asks this module: the stream itself is tapped for
a few seconds and YAMNet (already installed for the genre analysis) says music or speech.

The tap is a pw-record capture node linked by hand to the stream's output ports, with a
media.class that pipewire-pulse does not list → it is NOT a pulse source-output, so KDE
does not show its "microphone in use" icon (verified 2 Kas 2026), and WirePlumber never
links it anywhere else. It only copies the stream: routing and sound are untouched.

Decisions only switch Müzik ↔ Film for browser streams; calls, games, user rules and sure
site decisions (Netflix, Spotify…) are never touched. Hysteresis: a flip needs two
agreeing probes (one when very clear); "karışık" (speech over music, film scenes) keeps the
metadata decision. A stream that resumes after a pause is probed again at once (Chromium
hands an idle stream to the next tab), but its verdict stays until a new probe says
otherwise; a metadata change only triggers an early re-check (the shared media session
moves between tabs on its own). A verdict is only forgotten with its stream: a decision
that is briefly not a candidate (MPRIS flipping between tabs) keeps it (6 Kas live:
dropping it made the stream fall back to Film until the next probe → Film ↔ Müzik every
~5 s until the router parked it).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .i18n import N_, _
from .models import Category, Decision, Stream

log = logging.getLogger("oto_eq.icerik")

PROBE_S = 4.0            # seconds of audio per probe
REPROBE_S = 30.0         # re-check a playing candidate this often
QUICK_REPROBE_S = 8.0    # …but sooner until two verdicts exist (first decision in ~12 s)
SILENT_RETRY_S = 5.0     # paused Chromium tabs keep a running stream of zeros
FAIL_RETRY_S = 20.0
SILENT_DB = -65.0        # RMS below this = nothing to judge
TAP_RATE = 16000         # YAMNet's rate: PipeWire resamples for us
TAP_CLASS = "Stream/Input/Audio/Internal"   # not listed by pipewire-pulse → no KDE mic icon
TAP_PREFIX = "oto_eq_dinle_"
HISTORY = 3

# verdict kinds
MUZIK, KONUSMA, KARISIK, SESSIZ, HATA = "muzik", "konusma", "karisik", "sessiz", "hata"
KIND_CATEGORY = {MUZIK: Category.MUSIC, KONUSMA: Category.MOVIE}
KIND_TEXT = {MUZIK: N_("müzik"), KONUSMA: N_("konuşma"), KARISIK: N_("karışık"),
             SESSIZ: N_("sessiz"), HATA: N_("ölçülemedi")}
# the only categories this module ever switches between
SWITCHABLE = frozenset({Category.MUSIC, Category.MOVIE})


def classify_scores(music: float, speech: float) -> tuple[str, bool]:
    """(kind, strong) from YAMNet's "Music" and max speech-class activations (0..1).

    Thresholds checked on live content 2 Kas 2026 (O Ses Türkiye performance: music 0.93,
    speech 0.00; Evrim Ağacı talk: speech-dominant). Speech over background music, film
    scenes and effects stay "karışık" → the metadata decision is kept.
    """
    music, speech = max(0.0, float(music)), max(0.0, float(speech))
    if max(music, speech) < 0.15:
        return KARISIK, False              # neither clearly present (effects, noise, ambience)
    ratio = speech / (speech + music)
    if music >= 0.4 and ratio <= 0.25:
        return MUZIK, music >= 0.75 and speech <= 0.05
    if speech >= 0.3 and ratio >= 0.6:
        return KONUSMA, speech >= 0.6 and music <= 0.1
    return KARISIK, False


@dataclass(frozen=True)
class Verdict:
    kind: str
    music: float = 0.0
    speech: float = 0.0
    strong: bool = False
    rms_db: float = -240.0


class ContentJudge:
    """YAMNet activations of a mono float32 window → ``Verdict``."""

    def __init__(self, yamnet: Any) -> None:
        self.model = yamnet
        labels = list(yamnet.labels)
        from .ses_model import YAMNET_MUSIC, _YAM_SPEECH
        self._speech_idx = [labels.index(t) for t in _YAM_SPEECH if t in labels]
        self._music_idx = labels.index(YAMNET_MUSIC) if YAMNET_MUSIC in labels else None
        if self._music_idx is None or not self._speech_idx:
            raise ValueError("YAMNet etiketleri eksik (Music / Speech)")

    def judge(self, x, rate: int) -> Verdict:
        import numpy as np
        x = np.asarray(x, dtype=np.float32)
        if x.size == 0:
            return Verdict(SESSIZ)
        rms = float(20 * np.log10(np.sqrt(np.mean(np.square(x, dtype=np.float64))) + 1e-12))
        if rms < SILENT_DB:
            return Verdict(SESSIZ, rms_db=rms)
        act = self.model.activations(x, rate)
        if act is None:
            return Verdict(SESSIZ, rms_db=rms)
        music = float(act[self._music_idx])
        speech = max(float(act[i]) for i in self._speech_idx)
        kind, strong = classify_scores(music, speech)
        return Verdict(kind, round(music, 3), round(speech, 3), strong, round(rms, 1))


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _pw_dump() -> list[dict]:
    return json.loads(_run(["pw-dump"], timeout=10).stdout or "[]")


def _tap_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "oto-eq-dinle"
    d.mkdir(mode=0o700, exist_ok=True)
    return d


class StreamTap:
    """Copy one playback stream (by pulse index = PipeWire object.serial) for N seconds."""

    def __init__(self, dump: Callable[[], list[dict]] = _pw_dump,
                 run: Callable[..., subprocess.CompletedProcess] = _run,
                 popen: Callable[..., subprocess.Popen] = subprocess.Popen,
                 sleep: Callable[[float], Any] | None = None) -> None:
        self._dump, self._run, self._popen = dump, run, popen
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self._proc: subprocess.Popen | None = None

    @staticmethod
    def _node_of(dump: list[dict], serial: int) -> dict | None:
        for o in dump:
            if o.get("type") != "PipeWire:Interface:Node":
                continue
            props = (o.get("info") or {}).get("props") or {}
            if str(props.get("object.serial")) == str(serial):
                return o
        return None

    @staticmethod
    def _ports(dump: list[dict], node_id: int, direction: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in dump:
            if o.get("type") != "PipeWire:Interface:Port":
                continue
            info = o.get("info") or {}
            props = info.get("props") or {}
            if props.get("node.id") == node_id and info.get("direction") == direction:
                ch = str(props.get("audio.channel") or props.get("port.name") or o["id"])
                out[ch] = o["id"]
        return out

    def capture(self, serial: int, seconds: float = PROBE_S):
        """Mono float32 samples at TAP_RATE, or None (stream gone / pw tools failed)."""
        dump = self._dump()
        node = self._node_of(dump, serial)
        if node is None:
            return None
        props = (node.get("info") or {}).get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            return None
        src = self._ports(dump, node["id"], "output")
        if not src:
            return None
        name = f"{TAP_PREFIX}{serial}_{os.getpid()}"
        raw = _tap_dir() / f"{name}.raw"
        raw.unlink(missing_ok=True)
        try:
            return self._record(serial, seconds, name, raw, src)
        finally:
            raw.unlink(missing_ok=True)   # also on every early return (6 Kas: 0-byte leftovers)

    def _record(self, serial: int, seconds: float, name: str, raw: Path, src: dict[str, int]):
        import numpy as np
        tap_props = {"node.name": name, "node.description": "Kulak Kesil · içerik denetimi",
                     "media.class": TAP_CLASS, "node.autoconnect": "false",
                     "node.dont-reconnect": "true", "media.role": "Production",
                     "application.name": "oto-eq", "state.restore-props": "false"}
        proc = self._popen(["pw-record", "-a", "--format", "f32", "--rate", str(TAP_RATE),
                            "--channels", "2", "--target", "0", "-P", json.dumps(tap_props),
                            str(raw)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._proc = proc
        try:
            dst: dict[str, int] = {}
            for _i in range(30):
                if self._stop.is_set():
                    return None
                d2 = self._dump()
                me = next((o for o in d2 if o.get("type") == "PipeWire:Interface:Node"
                           and ((o.get("info") or {}).get("props") or {}).get("node.name") == name),
                          None)
                if me is not None:
                    dst = self._ports(d2, me["id"], "input")
                    if len(dst) >= 2:
                        break
                self._sleep(0.1)
            if len(dst) < 2:
                log.debug("içerik denetimi: dinleme düğümü açılmadı (#%s)", serial)
                return None
            pairs = self._pairs(src, dst)
            for a, b in pairs:
                r = self._run(["pw-link", str(a), str(b)])
                if r.returncode != 0:
                    log.debug("içerik denetimi: pw-link %s→%s: %s", a, b, (r.stderr or "").strip())
                    return None
            self._sleep(seconds)
            if self._stop.is_set():
                return None
        finally:
            self._proc = None
            proc.terminate()
            try:
                proc.wait(3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        try:
            x = np.fromfile(raw, dtype=np.float32)
        except OSError:
            return None
        x = x[: x.size // 2 * 2].reshape(-1, 2).mean(axis=1)
        return x

    @staticmethod
    def _pairs(src: dict[str, int], dst: dict[str, int]) -> list[tuple[int, int]]:
        """Stream output ports → our input_FL/FR (mono feeds both; extra channels skipped)."""
        dl = dst.get("FL") or dst.get("input_FL")
        dr = dst.get("FR") or dst.get("input_FR")
        if dl is None or dr is None:
            vals = list(dst.values())
            dl, dr = vals[0], vals[1]
        if "FL" in src and "FR" in src:
            return [(src["FL"], dl), (src["FR"], dr)]
        if "MONO" in src:
            return [(src["MONO"], dl), (src["MONO"], dr)]
        vals = list(src.values())
        return [(vals[0], dl), (vals[1] if len(vals) > 1 else vals[0], dr)]

    def stop(self) -> None:
        self._stop.set()
        p = self._proc
        if p is not None:
            try:
                p.terminate()
            except OSError:
                pass


class _State:
    __slots__ = ("key", "verdicts", "applied", "last", "next_at", "busy", "corked", "probed_at")

    def __init__(self, key: str, now: float) -> None:
        self.key = key
        self.verdicts: deque[str] = deque(maxlen=HISTORY)
        self.applied: str | None = None     # MUZIK / KONUSMA once decided, else None
        self.last: Verdict | None = None
        self.next_at = now                  # probe as soon as it plays
        self.busy = False
        self.corked = False
        self.probed_at = float("-inf")


class ContentProber:
    """Router side: ``adjust()`` per stream per scan; probes run in one worker thread."""

    def __init__(self, judge: Any, tap: Any = None, on_change: Callable[[], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, threaded: bool = True) -> None:
        self.judge = judge
        self.tap = tap if tap is not None else StreamTap()
        self.on_change = on_change
        self.clock = clock
        self.enabled = True
        self._threaded = threaded
        self._lock = threading.Lock()
        self._states: dict[int, _State] = {}
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()

    # ---------------------------------------------------------------- router API
    def adjust(self, st: Stream, d: Decision, ambiguous: bool, key: str,
               make: Callable[[Category, str], Decision]) -> Decision:
        """The decision to route ``st`` with (``d`` or a content-based override)."""
        eligible = d.zayif or ambiguous
        if not self.enabled:              # "Ses analizi" off: forget every verdict
            with self._lock:
                self._states.pop(st.index, None)
            return d
        # nameless (Chromium) streams are measured even while their metadata looks sure:
        # the shared media session can jump to another tab any moment, and then a verdict
        # must already be there (no 4 s detour through the wrong EQ)
        if d.kaynak != "tarayici" or d.category not in SWITCHABLE or not (eligible or d.grup):
            return d                      # not a candidate NOW: keep its state (prune drops it)
        now = self.clock()
        probe = False
        with self._lock:
            s = self._states.get(st.index)
            if s is None:
                s = self._states[st.index] = _State(key, now)
            elif s.key != key:
                # the metadata moved — in Chromium ONE media session serves every tab and a
                # muted autoplay video (x.com) takes it over: no proof that THIS stream's
                # audio changed. Keep the verdict, check again soon (2 Kas: flapping fix).
                s.key = key
                s.next_at = min(s.next_at, max(now, s.probed_at + QUICK_REPROBE_S))
            if st.corked:
                s.corked = True
            else:
                if s.corked:                  # resumed: maybe another tab now (Chromium reuse)
                    s.corked = False
                    s.verdicts.clear()        # measure again at once; the verdict (applied)
                    s.next_at = now           # stays until a new probe disagrees (_probe)
                if now >= s.next_at and not s.busy:
                    s.busy = True
                    s.probed_at = now
                    probe = True
            applied, last = s.applied, s.last
        if probe:
            self._enqueue(st.index)
        if not eligible or applied is None or last is None:
            return d
        cat = KIND_CATEGORY[applied]
        if cat == d.category:
            return replace(d, reason=_("{neden} · ses içeriği doğruladı: {icerik}").format(
                neden=d.reason, icerik=_(KIND_TEXT[applied])))
        nd = make(cat, _("ses içeriği: {icerik} (müzik {muzik}, konuşma {konusma}) · önce: {neden}")
                  .format(icerik=_(KIND_TEXT[applied]), muzik=_pct(last.music),
                          konusma=_pct(last.speech), neden=d.reason))
        return replace(nd, kaynak="icerik", grup=d.grup)

    def prune(self, present: set[int]) -> None:
        with self._lock:
            for idx in [i for i in self._states if i not in present]:
                del self._states[idx]

    def status(self) -> dict:
        with self._lock:
            return {str(i): {"karar": s.applied, "son": s.last.kind if s.last else None,
                             "muzik": s.last.music if s.last else None,
                             "konusma": s.last.speech if s.last else None,
                             "gecmis": list(s.verdicts)}
                    for i, s in self._states.items()}

    def stop(self) -> None:
        self._stopping.set()
        try:
            self.tap.stop()
        except Exception:
            pass
        self._q.put(None)

    # ---------------------------------------------------------------- worker
    def _enqueue(self, idx: int) -> None:
        if not self._threaded:
            self._probe(idx)
            return
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, name="icerik", daemon=True)
            self._worker.start()
        self._q.put(idx)

    def _loop(self) -> None:
        while not self._stopping.is_set():
            idx = self._q.get()
            if idx is None:
                return
            try:
                self._probe(idx)
            except Exception:
                log.exception("içerik denetimi hatası (#%s)", idx)

    def _probe(self, idx: int) -> None:
        with self._lock:
            mine = self._states.get(idx)
        if mine is None:
            return
        try:
            x = self.tap.capture(idx, PROBE_S)
            v = self.judge.judge(x, TAP_RATE) if x is not None else Verdict(HATA)
        except Exception as e:  # noqa: BLE001 — the router must never see this
            log.debug("içerik denetimi ölçülemedi (#%s): %s", idx, e)
            v = Verdict(HATA)
        changed = False
        now = self.clock()
        with self._lock:
            s = self._states.get(idx)
            if s is not mine:
                return                        # stream gone meanwhile
            s.busy = False
            if v.kind == HATA:
                s.next_at = now + FAIL_RETRY_S
                return
            if v.kind == SESSIZ:
                s.next_at = now + SILENT_RETRY_S
                return
            s.last = v
            s.verdicts.append(v.kind)
            s.next_at = now + (QUICK_REPROBE_S if len(s.verdicts) < 2 else REPROBE_S)
            two = len(s.verdicts) >= 2 and s.verdicts[-1] == s.verdicts[-2]
            new = s.applied
            if v.kind in KIND_CATEGORY and (v.strong or two):
                new = v.kind
            elif v.kind == KARISIK and two:
                new = None                    # sustained mix: back to the metadata decision
            if new != s.applied:
                s.applied = new
                changed = True
        log.info("içerik denetimi #%s: %s (müzik %.2f, konuşma %.2f%s)%s", idx, v.kind, v.music,
                 v.speech, ", net" if v.strong else "", " → karar değişti" if changed else "")
        if changed and self.on_change is not None:
            self.on_change()


def _pct(x: float) -> str:
    from .ui_text import percent
    return percent(x)


def build(model: Any, on_change: Callable[[], None] | None = None) -> ContentProber | None:
    """A prober using the genre model's YAMNet helper; None without it (feature off)."""
    if os.environ.get("OTO_EQ_ICERIK", "1") == "0":
        log.info("içerik denetimi kapalı (OTO_EQ_ICERIK=0)")
        return None
    yam = getattr(model, "helper", None) if model is not None else None
    if yam is None:
        log.info("içerik denetimi kapalı: YAMNet modeli yok")
        return None
    try:
        return ContentProber(ContentJudge(yam), on_change=on_change)
    except Exception as e:  # noqa: BLE001
        log.warning("içerik denetimi açılamadı: %s", e)
        return None
