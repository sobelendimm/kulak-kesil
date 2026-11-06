"""Voice ducking ("kısma", CONTRACT CHANGE 6 → D2).

While someone talks in a voice app (Discord, TeamSpeak, Mumble, calls, Meet/Zoom
tabs …) the EQ sinks oto_eq_oyun / _muzik / _film are lowered by ``db`` (default
6 dB; cs2 is NOT touched by default) with short ramps, and restored when the
talk stops.

    Ducker(meter_factory=None, set_volume=None, get_volume=None, clock=None)
        ensure(voice_streams, sinks)   router: on every update (cheap, no subprocess)
        stop()                         restore immediately, close meters
        status() -> dict               aktif / konusma / kisma_db …
        restore_on_start() -> int      undo a stale lowering left by a crash
    sesli_mi(stream, category, players=())   helper for the router (voice detection)
    run(argv) -> int                   ``kulak kisma [durum|ac|kapat] [db=6] [--profiller …]``

Level meter: ``parec --monitor-stream=<sink-input>`` per voice stream (8 kHz mono,
20 ms blocks); the Düz monitor is the fallback. RMS gate with hysteresis
(open −48 dBFS / close −56 dBFS), attack 50 ms, hold 300 ms; after the gate closes
the ducking waits ``birakma_ms`` (600 ms) before ramping up. Ramps: 80 ms down,
400 ms up. Volumes are pactl raw units (cubic): raw = base · 10^(gain_dB/60).

Safety: the user's volume is read at the start of every ducking episode and
written back *exactly*; a record of the lowered sinks is kept in
``~/.local/state/oto-eq/kisma-durum.json`` before the first change, so a crash
(SIGKILL, power loss) is undone by ``restore_on_start()``. If the user moved a
lowered sink's volume himself, we leave his new value alone.
See docs/NOTES-kisma.md.
"""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import re
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable

from . import config
from .i18n import _
from .models import EQ_PROFILES, FLAT_PROFILE, PROFILE_LABELS, SINK_PREFIX, sink_name

log = logging.getLogger("oto_eq.kisma")

SETTINGS_FILE = config.CONFIG_DIR / "kisma.json"
STATE_FILE = config.STATE_DIR / "kisma-durum.json"
DUZ_MONITOR = sink_name(FLAT_PROFILE) + ".monitor"

DEFAULTS: dict[str, Any] = {
    "acik": True,
    "db": 6.0,                       # attenuation while someone talks
    "profiller": ["oyun", "muzik", "film"],   # cs2 excluded on purpose
    "acik_esik_db": -48.0,           # gate opens above this RMS (dBFS)
    "kapali_esik_db": -56.0,         # … and closes below this one (hysteresis)
    "atak_ms": 50,                   # level must stay above the open threshold this long
    "tutma_ms": 300,                 # gate stays open this long after the level drops
    "birakma_ms": 600,               # then the ducking waits this long before ramping up
    "inis_ms": 80,                   # ramp down duration
    "cikis_ms": 400,                 # ramp up duration
    "ek_uygulamalar": [],            # extra app/binary regexes always treated as voice
}
DB_RANGE = (1.0, 24.0)
MS_RANGE = (0, 5000)

TICK_S = 0.02                    # worker period while a voice stream exists
STEP_DOWN_S = 0.02               # min interval between volume writes (ramp down)
STEP_UP_S = 0.04                 # … (ramp up): fewer pactl calls
METER_STALE_S = 0.25             # no block for this long = silence
MAX_METERS = 3                   # more voice streams → one Düz monitor meter
SETTINGS_CHECK_S = 1.0
ERROR_COOLDOWN_S = 5.0
VOL_TOL = 2                      # raw units: pactl/pipewire float round trip
SILENCE_DB = -120.0

EXIT_OK, EXIT_ERR, EXIT_USAGE = 0, 1, 2


# ============================================================================
#  SETTINGS
# ============================================================================

def _num(v: Any, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return min(hi, max(lo, x))


def normalize_settings(data: Any) -> dict:
    """Validated copy of ``data`` merged over DEFAULTS (never raises)."""
    out = json.loads(json.dumps(DEFAULTS))
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("acik"), bool):
        out["acik"] = data["acik"]
    out["db"] = _num(data.get("db"), DEFAULTS["db"], *DB_RANGE)
    prof = data.get("profiller")
    if isinstance(prof, list):
        out["profiller"] = [p for p in dict.fromkeys(str(x) for x in prof) if p in EQ_PROFILES]
    out["acik_esik_db"] = _num(data.get("acik_esik_db"), DEFAULTS["acik_esik_db"], -100.0, 0.0)
    out["kapali_esik_db"] = _num(data.get("kapali_esik_db"), DEFAULTS["kapali_esik_db"],
                                 -100.0, 0.0)
    if out["kapali_esik_db"] > out["acik_esik_db"]:
        out["kapali_esik_db"] = out["acik_esik_db"]
    for k in ("atak_ms", "tutma_ms", "birakma_ms", "inis_ms", "cikis_ms"):
        out[k] = int(_num(data.get(k), DEFAULTS[k], *MS_RANGE))
    extra = data.get("ek_uygulamalar")
    if isinstance(extra, list):
        good = []
        for x in extra:
            try:
                re.compile(str(x))
                good.append(str(x))
            except re.error:
                log.warning("kisma.json: geçersiz desen atlandı: %r", x)
        out["ek_uygulamalar"] = good
    return out


def load_settings(path: Path | str | None = None) -> dict:
    p = Path(path) if path else SETTINGS_FILE
    try:
        with open(p, encoding="utf-8") as f:
            return normalize_settings(json.load(f))
    except (OSError, ValueError):
        return normalize_settings({})


def save_settings(data: dict, path: Path | str | None = None) -> dict:
    """Validate and write atomically (creates the file on the first save)."""
    p = Path(path) if path else SETTINGS_FILE
    clean = normalize_settings(data)
    p.parent.mkdir(parents=True, exist_ok=True)
    from .yedek import atomik_yaz      # unique temp file + fsync (GUI and CLI may both save)
    atomik_yaz(p, json.dumps(clean, ensure_ascii=False, indent=2) + "\n")
    return clean


# ============================================================================
#  VOICE DETECTION (helper for the router)
# ============================================================================

BROWSERS = re.compile(
    r"^(firefox|firefox-bin|librewolf|floorp|zen|zen-bin|waterfox|chromium|chrome|"
    r"google-chrome.*|brave|brave-browser.*|vivaldi.*|opera.*|microsoft-edge.*|msedge|"
    r"epiphany|thorium.*|org\.mozilla\.firefox|com\.google\.chrome|com\.brave\.browser|"
    r"org\.chromium\.chromium|io\.gitlab\.librewolf-community)$", re.I)
CALL_TITLE = re.compile(
    r"(^|\W)(google meet|meet\s*[-–:]|discord|zoom( meeting| workplace)?\b|microsoft teams|"
    r"jitsi|whereby|whatsapp)", re.I)
CALL_URL = re.compile(
    r"(meet\.google\.com|discord\.com/(channels|app)|app\.zoom\.us|zoom\.us/(j|wc)/|"
    r"teams\.(microsoft|live)\.com|meet\.jit\.si|whereby\.com|web\.whatsapp\.com)", re.I)
CALL_APPS = re.compile(
    r"^(telegram.*|org\.telegram\.desktop|ayugram.*|kotatogram.*|whatsapp.*|zapzap|"
    r"com\.rtosta\.zapzap|whatsie|altus)$", re.I)
CALL_WORDS = re.compile(r"(call|arama|voip|webrtc|voice|sesli)", re.I)
VOICE_ROLES = ("phone", "communication")
# G2 (25 Eki): messenger hosts the classifier lists as voice apps (EQ'suz Düz) but whose
# streams are mostly message notification sounds — Ferdium (WhatsApp/Telegram inside),
# Signal. Like Telegram/WhatsApp desktop they duck only with call evidence; otherwise
# every incoming message dipped music/game/film by 6 dB for ~1.5 s.
MESSENGER_HOSTS = re.compile(
    r"^(ferdium|org\.ferdium\.ferdium|signal.*|org\.signal\.signal)$", re.I)
# Browser tabs of messengers (WhatsApp Web, Telegram Web): same rule (notification ≠ talk)
MESSENGER_WEB = re.compile(
    r"(web\.whatsapp\.com|web\.telegram\.org|(^|\W)whatsapp(\W|$)|(^|\W)telegram(\W|$))", re.I)
# a call TAB names the service where a site name goes (start / end of the title), not
# anywhere: "Zoom H6 review - YouTube", "Discord nasıl kullanılır? - YouTube" or
# "Best Google Meet tips" are videos (they ducked the other outputs by 6 dB) — 1 Kas
_CALL_TITLE_NO_MESSENGER = re.compile(
    r"(^Meet\s*[-–—|:]|^Meet$|^Google Meet\b|[-–—|]\s*Google Meet\s*$|"
    r"\bZoom (Meeting|Workplace|Toplantı)\b|^Zoom\s*[-–—|]|"
    r"[-–—|]\s*(Microsoft Teams|Discord|Jitsi Meet|Whereby)\s*$|"
    r"^(\(\d+\)\s*)?(Microsoft Teams|Discord|Jitsi Meet)\s*([-–—|]|$))", re.I)
_CALL_URL_NO_MESSENGER = re.compile(
    r"(meet\.google\.com|discord\.com/(channels|app)|app\.zoom\.us|zoom\.us/(j|wc)/|"
    r"teams\.(microsoft|live)\.com|meet\.jit\.si|whereby\.com)", re.I)


def _cat_value(category: Any) -> str:
    return str(getattr(category, "value", category) or "")


def sesli_mi(stream: Any, category: Any = None, players: Iterable[Any] = (),
             extra: Iterable[str] = ()) -> bool:
    """Is this playback stream a voice/call stream that should trigger ducking?

    * ``category`` "sesli" (the classifier's VOICE: Discord, Vesktop, WebCord,
      TeamSpeak, Mumble, Zoom … and media.role phone/communication) → True
    * Telegram / WhatsApp desktop: only call streams (role or media name)
    * browsers: tab title (media.name, or the MPRIS title of the same PID) or
      URL says Meet / Discord / Zoom / Teams / Jitsi / Whereby / WhatsApp
    * ``extra``: user regexes (kisma.json "ek_uygulamalar") on binary/app name
    * messenger hosts (Ferdium, Signal) and messenger tabs (WhatsApp/Telegram Web) need
      call evidence even when the classifier says "sesli": their streams are mostly
      notification sounds (G2, 25 Eki)
    """
    voice_cat = _cat_value(category) == "sesli"
    binary = getattr(stream, "binary", "") or ""
    app = getattr(stream, "app_name", "") or ""
    props = getattr(stream, "props", {}) or {}
    names = [n for n in (binary, app, props.get("application.process.binary", ""),
                         props.get("pipewire.access.portal.app_id", ""),
                         props.get("application.id", "")) if n]
    for pat in extra or ():
        try:
            if any(re.fullmatch(pat, n, re.I) for n in names):
                return True
        except re.error:
            continue
    role = (getattr(stream, "media_role", "") or "").lower()
    media = getattr(stream, "media_name", "") or ""
    if any(CALL_APPS.match(n) or MESSENGER_HOSTS.match(n) for n in names):
        return role in VOICE_ROLES or bool(CALL_WORDS.search(media))
    if any(BROWSERS.match(n) for n in names):
        if role in VOICE_ROLES or _CALL_TITLE_NO_MESSENGER.search(media):
            return True
        pid = getattr(stream, "pid", None)
        texts = [media]
        for pl in players or ():
            if pid is not None and getattr(pl, "pid", None) not in (None, pid):
                continue
            url = getattr(pl, "url", "") or ""
            title = getattr(pl, "title", "") or ""
            if _CALL_URL_NO_MESSENGER.search(url):
                return True
            if pid is not None and _CALL_TITLE_NO_MESSENGER.search(title):
                return True
            texts.append(url)
            if pid is not None:
                texts.append(title)
        if any(MESSENGER_WEB.search(t) for t in texts if t):
            return bool(CALL_WORDS.search(media))     # WhatsApp/Telegram Web: calls only
    return voice_cat


# ============================================================================
#  VOLUME BACKEND (pactl, per channel, exact raw values)
# ============================================================================

_VOL_RE = re.compile(r"(\d+)\s*/\s*\d+%")


def parse_channel_volumes(text: str) -> list[int] | None:
    """``pactl get-sink-volume`` → raw value of every channel (None if unparsable)."""
    first = (text or "").splitlines()[0] if text else ""
    vals = [int(v) for v in _VOL_RE.findall(first)]
    return vals or None


def pactl_get_volume(name: str) -> list[int] | None:
    try:
        p = subprocess.run(["pactl", "get-sink-volume", name], capture_output=True, text=True,
                           env=config.C_ENV, timeout=3, errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("ses seviyesi okunamadı (%s): %s", name, e)
        return None
    if p.returncode != 0:
        return None
    return parse_channel_volumes(p.stdout)


def pactl_set_volumes(changes: dict[str, list[int]]) -> dict[str, bool]:
    """Set several sinks at once (parallel pactl processes: one ramp step ≈ one spawn time)."""
    procs = {}
    for name, vols in changes.items():
        try:
            procs[name] = subprocess.Popen(
                ["pactl", "set-sink-volume", name, *(str(max(0, int(v))) for v in vols)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                env=config.C_ENV)
        except OSError as e:
            log.warning("ses seviyesi ayarlanamadı (%s): %s", name, e)
    out = {name: False for name in changes}
    for name, p in procs.items():
        try:
            out[name] = p.wait(timeout=3) == 0
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    return out


# ============================================================================
#  LEVEL METER (parec ring of 20 ms blocks → latest RMS in dBFS)
# ============================================================================

def meter_argv(target: int | str, rate: int = 8000, block_ms: int = 20) -> list[str]:
    base = ["parec", "--raw", "--format=float32le", "--channels=1", f"--rate={rate}",
            f"--latency-msec={block_ms}", "--client-name=Kulak Kesil kisma",
            "--stream-name=Kulak Kesil kisma olcer"]  # ASCII: parec runs with LC_ALL=C
    if isinstance(target, int):
        return base + [f"--monitor-stream={target}"]
    return base + ["-d", str(target)]


class ParecMeter:
    """Background ``parec`` reader; ``level()`` = RMS dBFS of the newest block.

    ``failed`` becomes True when the process exits without ever delivering
    audio (e.g. --monitor-stream unsupported) → the Ducker falls back to Düz.
    """

    RATE = 8000
    BLOCK_MS = 20

    def __init__(self, target: int | str, runner: Callable[[list[str]], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.target = target
        self._clock = clock
        self._level: float | None = None
        self._t = -math.inf
        self._got_data = False
        self._done = False
        self._stop = threading.Event()
        argv = meter_argv(target, self.RATE, self.BLOCK_MS)
        run = runner or (lambda a: subprocess.Popen(
            a, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            bufsize=0, env=config.C_ENV))
        self._proc = run(argv)
        self._thread = threading.Thread(target=self._pump, name="oto_eq.kisma.olcer",
                                        daemon=True)
        self._thread.start()

    @property
    def failed(self) -> bool:
        return self._done and not self._got_data

    def level(self) -> float | None:
        if self._clock() - self._t > METER_STALE_S:
            return None
        return self._level

    def _pump(self) -> None:
        import numpy as np
        n = int(self.RATE * self.BLOCK_MS / 1000) * 4
        out = self._proc.stdout
        rest = b""
        try:
            while not self._stop.is_set():
                data = out.read(n)
                if not data:
                    break
                data = rest + data
                usable = len(data) - len(data) % 4
                rest = data[usable:]
                if not usable:
                    continue
                x = np.frombuffer(data[:usable], dtype="<f4")
                rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
                self._level = 20.0 * math.log10(rms) if rms > 1e-6 else SILENCE_DB
                self._t = self._clock()
                self._got_data = True
        except (OSError, ValueError):
            pass
        finally:
            self._done = True

    def close(self) -> None:
        self._stop.set()
        p = self._proc
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=1.0)
        except Exception:  # noqa: BLE001
            log.debug("ölçer kapatılırken hata", exc_info=True)
        try:
            if p.stdout:
                p.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)


# ============================================================================
#  GATE (pure logic)
# ============================================================================

class Gate:
    """RMS gate with hysteresis, attack and hold (times in seconds)."""

    def __init__(self, open_db: float, close_db: float, attack: float, hold: float):
        self.open_db, self.close_db = open_db, min(close_db, open_db)
        self.attack, self.hold = attack, hold
        self.is_open = False
        self._above_since: float | None = None
        self._last_loud = -math.inf

    def reset(self) -> None:
        self.is_open = False
        self._above_since = None
        self._last_loud = -math.inf

    def update(self, level_db: float | None, now: float) -> bool:
        lv = SILENCE_DB if level_db is None else level_db
        if not self.is_open:
            if lv >= self.open_db:
                if self._above_since is None:
                    self._above_since = now
                if now - self._above_since >= self.attack - 1e-9:
                    self.is_open = True
                    self._last_loud = now
            else:
                self._above_since = None
        else:
            if lv >= self.close_db:
                self._last_loud = now
            elif now - self._last_loud >= self.hold - 1e-9:
                self.is_open = False
                self._above_since = None
        return self.is_open


# ============================================================================
#  DUCKER
# ============================================================================

def _copy_status(st: dict) -> dict:
    """A caller may edit what status() returns: never hand out the snapshot's own lists."""
    return {k: list(v) if isinstance(v, list) else v for k, v in st.items()}


class _Rec:
    """One lowered sink: the user's volume (base) and what we wrote last."""
    __slots__ = ("base", "floor", "set", "stale")

    def __init__(self, base: list[int], floor: list[int], stale: bool = False):
        self.base = list(base)
        self.floor = list(floor)
        self.set = list(base)
        self.stale = stale

    def to_json(self) -> dict:
        return {"base": self.base, "floor": self.floor}


def _scaled(base: list[int], gain_db: float) -> list[int]:
    if gain_db >= 0:
        return list(base)
    f = 10.0 ** (gain_db / 60.0)       # pactl raw is cubic in amplitude
    return [int(round(b * f)) for b in base]


def _close(a: list[int] | None, b: list[int] | None, tol: int = VOL_TOL) -> bool:
    return a is not None and b is not None and len(a) == len(b) \
        and all(abs(x - y) <= tol for x, y in zip(a, b))


def _in_range(cur: list[int], floor: list[int], base: list[int]) -> bool:
    return len(cur) == len(base) == len(floor) and all(
        f - VOL_TOL <= c <= b + VOL_TOL for c, f, b in zip(cur, floor, base))


def _profile_key(k: str) -> str:
    return k[len(SINK_PREFIX):] if k.startswith(SINK_PREFIX) else k


class Ducker:
    """See module docstring. All volume work happens on one worker thread
    (``threaded=True``) or in ``tick()`` called by the tests."""

    def __init__(self, meter_factory: Callable[[int | str], Any] | None = None,
                 set_volume: Callable[[str, list[int]], bool] | None = None,
                 get_volume: Callable[[str], list[int] | None] | None = None,
                 clock: Callable[[], float] | None = None,
                 settings_path: Path | str | None = None,
                 state_path: Path | str | None = None,
                 threaded: bool | None = None,
                 on_status: Callable[[dict], None] | None = None):
        real = set_volume is None and get_volume is None
        self._meter_factory = meter_factory or (lambda t: ParecMeter(t))
        if set_volume is None:
            self._set_many: Callable[[dict[str, list[int]]], dict[str, bool]] = pactl_set_volumes
        else:
            self._set_many = lambda ch: {n: bool(set_volume(n, v)) for n, v in ch.items()}
        self._get = get_volume or pactl_get_volume
        self._clock = clock or time.monotonic
        self._settings_path = Path(settings_path) if settings_path else SETTINGS_FILE
        self._state_path = Path(state_path) if state_path else STATE_FILE
        self._threaded = real if threaded is None else threaded
        self._on_status = on_status

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

        self.settings = load_settings(self._settings_path)
        self._settings_mtime = self._mtime()
        self._settings_checked = -math.inf
        self._gate = self._new_gate()

        # inputs from ensure()
        self._voice: dict[int, str] = {}          # stream index → app label
        self._voice_sinks: set[Any] = set()        # sink indices/names hosting voice streams
        self._sinks: dict[str, Any] = {}           # profile → index/name (present sinks)
        # worker state
        self._meters: dict[int | str, Any] = {}
        self._fallback = False                     # a per-stream meter failed → Düz monitor
        self._recs: dict[str, _Rec] = {}           # sink name → record
        self._gain = 0.0                           # current gain (≤ 0 dB)
        self._target = 0.0
        self._applied = 0.0
        self._last_apply = -math.inf
        self._closed_at: float | None = None       # gate closed (release timer)
        self._up_checked = False
        self._t_prev: float | None = None
        self._skip: set[str] = set()
        self._cooldown_until = -math.inf
        self._restored_on_start = False
        self._stopped = False
        self._last_level: float | None = None
        self._status_key: tuple | None = None
        # last status() result: served when the worker holds the lock in a slow pactl call
        self._snapshot: dict = self._status_dict()
        if self._threaded:
            ref = weakref.ref(self)
            atexit.register(lambda: (lambda d: d and d.stop())(ref()))

    # ------------------------------------------------------------------ public API

    def ensure(self, voice_streams: list[Any], sinks: dict[str, Any]) -> None:
        """Router hook: current voice streams + present sinks. Cheap; never raises."""
        try:
            voice: dict[int, str] = {}
            vsinks: set[Any] = set()
            for st in voice_streams or ():
                if getattr(st, "corked", False):
                    continue
                if (getattr(st, "node_name", "") or "").startswith(SINK_PREFIX):
                    continue
                idx = int(getattr(st, "index"))
                voice[idx] = (getattr(st, "app_name", "") or getattr(st, "binary", "")
                              or f"#{idx}")
                vsinks.add(getattr(st, "sink_index", None))
            norm: dict[str, Any] = {}
            for k, v in (sinks or {}).items():
                p = _profile_key(str(k))
                if p in EQ_PROFILES or p == FLAT_PROFILE:
                    norm[p] = v
            with self._lock:
                changed = (voice != self._voice or norm != self._sinks
                           or vsinks != self._voice_sinks)
                self._voice, self._voice_sinks, self._sinks = voice, vsinks, norm
                self._stopped = False
            if self._threaded:
                self._ensure_thread()
                if changed or self._recs or self._pending_stale():
                    self._wake.set()
            elif not self._restored_on_start:
                self.restore_on_start()
        except Exception:  # noqa: BLE001
            log.exception("kısma: ensure hatası")

    def tick(self) -> float | None:
        """One worker step. Returns seconds until the next step, None = idle."""
        with self._lock:
            now = self._clock()
            try:
                return self._tick(now)
            except Exception:  # noqa: BLE001
                log.exception("kısma hatası; sesler geri açılıyor")
                self._safe_restore_all()
                self._cooldown_until = now + ERROR_COOLDOWN_S
                return TICK_S
            finally:
                try:
                    self._snapshot = self._status_dict()   # for status(wait=…) during the next tick
                except Exception:  # noqa: BLE001
                    log.debug("kısma durum özeti alınamadı", exc_info=True)

    def stop(self) -> None:
        """Restore every lowered sink immediately and close the meters (idempotent)."""
        self._stopping.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        with self._lock:
            self._stopped = True
            self._close_meters()
            self._safe_restore_all()
            self._gate.reset()
            self._closed_at = None
        self._stopping.clear()

    def status(self, wait: float | None = None) -> dict:
        """Current state for the GUI/CLI. ``wait``: at most this many seconds for the
        worker lock; on timeout the last snapshot (≤ one tick old) is returned. The
        router passes a short wait: a tick holds the lock across pactl calls (3 s each
        while pipewire-pulse hangs), and `kulak durum` / IPC watch must not hang on it."""
        if wait is None:
            self._lock.acquire()
        elif not self._lock.acquire(timeout=max(0.0, wait)):
            return _copy_status(self._snapshot)
        try:
            self._snapshot = self._status_dict()
            return _copy_status(self._snapshot)
        finally:
            self._lock.release()

    def _status_dict(self) -> dict:
        with self._lock:
            s = self.settings
            meters = list(self._meters)
            return {
                "acik": s["acik"],
                "aktif": bool(s["acik"] and self._voice and not self._stopped),
                "konusma": bool(self._gate.is_open),
                "kisma_db": round(-self._gain, 1) + 0.0,    # + 0.0: never "-0" in GUI/CLI
                "hedef_db": s["db"],
                "profiller": [p for p in s["profiller"]],
                "kisilan": sorted(self._recs),
                "sesli": sorted(set(self._voice.values())),
                "olcum": (_("Düz monitörü") if DUZ_MONITOR in meters else
                          ", ".join(_("akış #{no}").format(no=m) for m in meters) if meters else ""),
                "seviye_db": None if self._last_level is None else round(self._last_level, 1),
            }

    def restore_on_start(self) -> int:
        """Undo a lowering recorded by a previous run that did not exit cleanly.

        Sinks that are absent now stay pending and are fixed by a later
        ``ensure()`` when they reappear. Returns the number of sinks restored."""
        with self._lock:
            self._restored_on_start = True
            data = self._read_state()
            for name, rec in data.items():
                if name not in self._recs:
                    self._recs[name] = rec
            return self._fix_stale(only_present=False)

    # ------------------------------------------------------------------ worker

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._stopping.is_set():
            return
        self._thread = threading.Thread(target=self._loop, name="oto_eq.kisma", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        if not self._restored_on_start:
            self.restore_on_start()
        while not self._stopping.is_set():
            delay = self.tick()
            if delay is None:
                # idle: 30 s, but while a voice stream exists check the settings every second
                # (`kulak kisma ac` during a call promises "applied within 1 s")
                with self._lock:
                    talking = bool(self._voice)
                delay = SETTINGS_CHECK_S if talking else SETTINGS_CHECK_S * 30
            self._wake.wait(delay)
            self._wake.clear()

    def _tick(self, now: float) -> float | None:
        self._maybe_reload(now)
        self._fix_stale(only_present=True)
        s = self.settings
        ducking_sinks = self._target_sinks()
        want_meter = bool(s["acik"] and self._voice and not self._stopped
                          and now >= self._cooldown_until)
        self._sync_meters(want_meter)

        level = self._read_level() if want_meter else None
        self._last_level = level
        was_open = self._gate.is_open
        is_open = self._gate.update(level, now) if want_meter else False
        if not want_meter:
            self._gate.reset()
        if is_open:
            self._closed_at = None
            self._target = -float(s["db"])
        else:
            if was_open or (self._closed_at is None and self._target < 0):
                self._closed_at = now
            if self._target < 0 and (not want_meter or self._closed_at is None
                                     or now - self._closed_at >= s["birakma_ms"] / 1000.0):
                self._target = 0.0
        # sinks that left the ducking set (profile removed, voice moved onto it) → restore now
        for name in [n for n, r in self._recs.items() if not r.stale]:
            if not self._sink_present(name):
                # sink vanished (oto-eq-sinks restart): fix when it comes back
                self._recs[name].stale = True
                self._write_state()
            elif name not in ducking_sinks:
                self._restore_one(name)
        self._ramp(now, ducking_sinks)
        self._emit_status()
        if want_meter or self._gain != self._target or self._gain < 0 \
                or any(not r.stale for r in self._recs.values()):
            return TICK_S
        return None

    # ------------------------------------------------------------------ ramps

    def _ramp(self, now: float, sinks: list[str]) -> None:
        s = self.settings
        if self._gain == self._target:
            self._t_prev = None                 # steady: next ramp starts with one tick
            dt = 0.0
        else:
            dt = TICK_S if self._t_prev is None else min(max(now - self._t_prev, 0.0), 0.1)
            self._t_prev = now
        db = max(float(s["db"]), 1e-6)
        step = STEP_DOWN_S
        if self._gain > self._target:          # going down
            rate = db / max(s["inis_ms"] / 1000.0, 1e-3)
            self._gain = max(self._target, self._gain - rate * dt)
            self._up_checked = False
        elif self._gain < self._target:        # going up
            if not self._up_checked:
                self._check_user_changes()
                self._up_checked = True
            rate = db / max(s["cikis_ms"] / 1000.0, 1e-3)
            self._gain = min(self._target, self._gain + rate * dt)
            step = STEP_UP_S
        due = (now - self._last_apply >= step - 1e-9) or self._gain == self._target
        if self._gain < 0:
            missing = [n for n in sinks if n not in self._recs and n not in self._skip]
            if missing:
                self._begin(missing)
                due = True
        else:
            self._skip.clear()
            if self._target == 0.0 and self._applied == 0.0:
                self._finish_restored(list(self._recs))
        if due and (self._gain != self._applied or self._live_recs()):
            self._apply(self._gain)
            self._applied = self._gain
            self._last_apply = now

    def _live_recs(self) -> list[str]:
        """Records we are actively lowering (not stale, sink present)."""
        return [n for n, r in self._recs.items() if not r.stale and self._sink_present(n)
                and (r.set != _scaled(r.base, self._gain))]

    def _begin(self, names: list[str]) -> None:
        """Start lowering ``names``: read the user's volume and record it first."""
        floor_db = -float(self.settings["db"])
        added = False
        for name in names:
            base = self._get(name)
            if not base or max(base) <= 0:
                self._skip.add(name)            # unreadable / muted: leave it alone
                continue
            self._recs[name] = _Rec(base, _scaled(base, min(floor_db, self._gain)))
            added = True
        if added:
            self._write_state()                 # record BEFORE the first change

    def _apply(self, gain: float) -> None:
        changes: dict[str, list[int]] = {}
        persist = False
        for name, rec in self._recs.items():
            if rec.stale or not self._sink_present(name):
                continue
            vols = _scaled(rec.base, gain)
            if any(v < f for v, f in zip(vols, rec.floor)):
                rec.floor = [min(a, b) for a, b in zip(rec.floor, vols)]
                persist = True
            if vols != rec.set:
                changes[name] = vols
        if persist:
            self._write_state()
        res = self._set_many(changes) if changes else {}
        for name, ok in res.items():
            if ok:
                self._recs[name].set = changes[name]
            else:
                log.warning("kısma: %s ses seviyesi ayarlanamadı", name)
        if gain == 0.0:
            self._finish_restored(list(self._recs))
            failed = [n for n, ok in res.items() if not ok]
            if failed:
                raise RuntimeError(_("geri yükleme başarısız: {cikislar}").format(cikislar=", ".join(failed)))

    def _finish_restored(self, names: list[str]) -> None:
        changed = False
        for n in names:
            rec = self._recs.get(n)
            if rec is not None and not rec.stale and rec.set == rec.base:
                del self._recs[n]
                changed = True
        if changed:
            self._write_state()

    def _check_user_changes(self) -> None:
        """Before ramping up: if the user moved a lowered sink, keep his value."""
        for name in list(self._recs):
            rec = self._recs[name]
            if rec.stale or not self._sink_present(name):
                continue
            cur = self._get(name)
            if cur is None:
                continue
            if not _close(cur, rec.set):
                log.info("kısma: %s ses seviyesi elle değiştirilmiş, dokunulmuyor", name)
                del self._recs[name]
                self._skip.add(name)            # not again in this episode
                self._write_state()

    def _restore_one(self, name: str) -> bool:
        rec = self._recs.get(name)
        if rec is None:
            return True
        cur = self._get(name)
        if cur is None:
            return False                        # absent → stays recorded
        if _close(cur, rec.set) or _in_range(cur, rec.floor, rec.base):
            if not _close(cur, rec.base, 0):
                ok = self._set_many({name: rec.base}).get(name, False)
                if not ok:
                    return False
        else:
            log.info("kısma: %s elle değiştirilmiş (%s), geri yüklenmiyor", name, cur)
        del self._recs[name]
        self._write_state()
        return True

    def _safe_restore_all(self) -> None:
        for name in list(self._recs):
            try:
                self._restore_one(name)
            except Exception:  # noqa: BLE001
                log.exception("kısma: %s geri yüklenemedi (kayıt duruyor)", name)
        if not self._recs:
            self._gain = self._target = self._applied = 0.0
        else:
            # what is left is unreachable now; treat as stale (fixed on reappearance)
            for r in self._recs.values():
                r.stale = True
            self._gain = self._target = self._applied = 0.0
            self._write_state()

    def _fix_stale(self, only_present: bool) -> int:
        n = 0
        for name in [k for k, r in self._recs.items() if r.stale]:
            if only_present and not self._sink_present(name):
                continue
            rec = self._recs[name]
            cur = self._get(name)
            if cur is None:
                continue
            if _in_range(cur, rec.floor, rec.base) and not _close(cur, rec.base, 0):
                if not self._set_many({name: rec.base}).get(name, False):
                    continue
                log.info("kısma: önceki çalışmadan kalan kısma geri alındı: %s", name)
                n += 1
            del self._recs[name]
            self._write_state()
        return n

    def _pending_stale(self) -> bool:
        return any(r.stale for r in self._recs.values())

    # ------------------------------------------------------------------ helpers

    def _target_sinks(self) -> list[str]:
        out = []
        for p in self.settings["profiller"]:
            if p not in self._sinks or p not in EQ_PROFILES:
                continue
            v = self._sinks[p]
            if v in self._voice_sinks or sink_name(p) in self._voice_sinks:
                continue                         # never lower the sink carrying the call
            out.append(sink_name(p))
        return out

    def _sink_present(self, name: str) -> bool:
        return _profile_key(name) in self._sinks

    def _sync_meters(self, want: bool) -> None:
        if not want:
            self._close_meters()
            return
        if any(getattr(m, "failed", False) for k, m in self._meters.items() if k != DUZ_MONITOR):
            if not self._fallback:
                log.info("kısma: akış ölçümü çalışmadı, Düz monitörüne geçiliyor")
            self._fallback = True
        idx = sorted(self._voice)
        wanted: list[int | str] = ([DUZ_MONITOR] if self._fallback or len(idx) > MAX_METERS
                                   else list(idx))
        for k in [k for k in self._meters if k not in wanted]:
            self._close_meter(k)
        for k in wanted:
            if k not in self._meters:
                try:
                    self._meters[k] = self._meter_factory(k)
                except Exception as e:  # noqa: BLE001
                    log.warning("kısma: ölçer başlatılamadı (%s): %s", k, e)
                    if k != DUZ_MONITOR:
                        self._fallback = True

    def _read_level(self) -> float | None:
        best: float | None = None
        for m in self._meters.values():
            lv = m.level()
            if lv is not None and (best is None or lv > best):
                best = lv
        return best

    def _close_meter(self, k: int | str) -> None:
        m = self._meters.pop(k, None)
        if m is not None:
            try:
                m.close()
            except Exception:  # noqa: BLE001
                log.debug("ölçer kapatılamadı", exc_info=True)

    def _close_meters(self) -> None:
        for k in list(self._meters):
            self._close_meter(k)
        if not self._voice:
            self._fallback = False

    def _new_gate(self) -> Gate:
        s = self.settings
        return Gate(s["acik_esik_db"], s["kapali_esik_db"], s["atak_ms"] / 1000.0,
                    s["tutma_ms"] / 1000.0)

    def _mtime(self) -> float | None:
        try:
            return self._settings_path.stat().st_mtime_ns
        except OSError:
            return None

    def _maybe_reload(self, now: float) -> None:
        if now - self._settings_checked < SETTINGS_CHECK_S:
            return
        self._settings_checked = now
        m = self._mtime()
        if m == self._settings_mtime:
            return
        self._settings_mtime = m
        new = load_settings(self._settings_path)
        if new != self.settings:
            log.info("kısma ayarları yeniden yüklendi: %s, %.1f dB, %s",
                     "açık" if new["acik"] else "kapalı", new["db"], ",".join(new["profiller"]))
            gate_changed = any(new[k] != self.settings[k] for k in
                               ("acik_esik_db", "kapali_esik_db", "atak_ms", "tutma_ms"))
            self.settings = new
            if gate_changed:
                was = self._gate.is_open
                self._gate = self._new_gate()
                self._gate.is_open = was

    def reload_settings(self) -> None:
        """Force a settings reload on the next tick (e.g. after the GUI saved)."""
        with self._lock:
            self._settings_checked = -math.inf
            self._settings_mtime = object()     # type: ignore[assignment]
        self._wake.set()

    # state file ---------------------------------------------------------

    def _read_state(self) -> dict[str, _Rec]:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        out = {}
        for name, r in (data.get("kisilan") or {}).items() if isinstance(data, dict) else ():
            try:
                base = [int(x) for x in r["base"]]
                floor = [int(x) for x in r["floor"]]
            except (KeyError, TypeError, ValueError):
                continue
            if name.startswith(SINK_PREFIX) and base and len(base) == len(floor):
                out[name] = _Rec(base, floor, stale=True)
        return out

    def _write_state(self) -> None:
        try:
            if not self._recs:
                self._state_path.unlink(missing_ok=True)
                return
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"surum": 1, "pid": os.getpid(), "kisilan": {
                n: r.to_json() for n, r in self._recs.items()}}, indent=1), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as e:
            log.warning("kısma durum dosyası yazılamadı: %s", e)

    def _emit_status(self) -> None:
        if self._on_status is None:
            return
        key = (self._gate.is_open, round(self._gain), bool(self._voice), self.settings["acik"])
        if key == self._status_key:
            return
        self._status_key = key
        try:
            self._on_status(self.status())
        except Exception:  # noqa: BLE001
            log.debug("kısma durum dinleyicisi hatası", exc_info=True)


# ============================================================================
#  CLI: kulak kisma [durum|ac|kapat] [db=6] [--profiller oyun,muzik,film]
# ============================================================================

USAGE = _("""kullanım: kulak kisma [durum|ac|kapat] [db=6] [--profiller oyun,muzik,film]

Sesli sohbette (Discord, TeamSpeak, Mumble, görüşmeler, Meet/Zoom sekmesi) biri konuşurken Oyun / Müzik / Film çıkışlarını kısar, susunca geri açar.
  durum               ayarlar + şu anki durum (varsayılan)
  ac | kapat          özelliği aç / kapat
  db=N                kısma miktarı (dB, {lo:g}–{hi:g}; varsayılan 6)
  --profiller a,b     kısılacak çıkışlar: oyun, muzik, film, cs2 (varsayılan: oyun,muzik,film)
""")


def _fmt_profiles(ps: list[str]) -> str:
    return ", ".join(PROFILE_LABELS.get(p, p) for p in ps) or _("(hiçbiri)")


def _live_status() -> dict | None:
    try:
        from . import ipc
        resp = ipc.request({"cmd": "status"}, timeout=1.5)
    except (OSError, ImportError, ValueError):
        return None
    k = resp.get("kisma") if isinstance(resp, dict) else None
    return k if isinstance(k, dict) else None


def run(argv: list[str] | None = None, out: Callable[[str], None] = print,
        settings_path: Path | str | None = None,
        live: Callable[[], dict | None] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = "durum"
    db: float | None = None
    profiles: list[str] | None = None
    usage = USAGE.format(lo=DB_RANGE[0], hi=DB_RANGE[1])
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low in ("-h", "--help", "yardim", "yardım"):
            out(usage)
            return EXIT_OK
        if low in ("durum", "ac", "aç", "kapat"):
            action = "ac" if low == "aç" else low
        elif low.startswith("db="):
            try:
                db = float(a[3:].replace(",", "."))
            except ValueError:
                out(_("kulak kisma: geçersiz dB değeri: {deger}").format(deger=repr(a[3:])))
                return EXIT_USAGE
            if not DB_RANGE[0] <= db <= DB_RANGE[1]:
                out(_("kulak kisma: dB {en_az:g}–{en_cok:g} arasında olmalı").format(
                    en_az=DB_RANGE[0], en_cok=DB_RANGE[1]))
                return EXIT_USAGE
        elif low == "--profiller" or low.startswith("--profiller="):
            if "=" in a:
                val = a.split("=", 1)[1]
            else:
                i += 1
                if i >= len(args):
                    out(_("kulak kisma: {secenek} için değer gerekli (ör. {ornek})").format(
                        secenek="--profiller", ornek="oyun,muzik,film"))
                    return EXIT_USAGE
                val = args[i]
            profiles = [p.strip().lower() for p in val.split(",") if p.strip()]
            bad = [p for p in profiles if p not in EQ_PROFILES]
            if bad:
                out(_("kulak kisma: bilinmeyen profil: {profil} (geçerli: {olanlar})").format(
                    profil=", ".join(bad), olanlar=", ".join(EQ_PROFILES)))
                return EXIT_USAGE
        else:
            out(_("kulak kisma: bilinmeyen argüman: {arguman}").format(arguman=repr(a)) + "\n")
            out(usage)
            return EXIT_USAGE
        i += 1

    path = Path(settings_path) if settings_path else SETTINGS_FILE
    s = load_settings(path)
    if action in ("ac", "kapat") or db is not None or profiles is not None:
        if action == "ac":
            s["acik"] = True
        elif action == "kapat":
            s["acik"] = False
        if db is not None:
            s["db"] = db
        if profiles is not None:
            s["profiller"] = profiles
        try:
            s = save_settings(s, path)
        except OSError as e:
            out(_("kulak kisma: ayar kaydedilemedi: {hata}").format(hata=e))
            return EXIT_ERR
        out((_("Sesli sohbette kısma AÇIK · {db:g} dB · {cikislar}  (servis 1 sn içinde uygular)")
             if s["acik"] else
             _("Sesli sohbette kısma KAPALI · {db:g} dB · {cikislar}  (servis 1 sn içinde uygular)"))
            .format(db=s["db"], cikislar=_fmt_profiles(s["profiller"])))
        return EXIT_OK

    out(_("Sesli sohbette kısma: AÇIK") if s["acik"] else _("Sesli sohbette kısma: KAPALI"))
    out("  " + _("miktar: {db:g} dB · çıkışlar: {cikislar}").format(
        db=s["db"], cikislar=_fmt_profiles(s["profiller"])))
    out("  " + _("eşik: aç {ac:g} / kapat {kapat:g} dBFS · atak {atak} ms · tutma {tutma} ms · "
                 "bırakma {birakma} ms · rampa {inis}/{cikis} ms").format(
        ac=s["acik_esik_db"], kapat=s["kapali_esik_db"], atak=s["atak_ms"], tutma=s["tutma_ms"],
        birakma=s["birakma_ms"], inis=s["inis_ms"], cikis=s["cikis_ms"]))
    st = (live or _live_status)()
    if st is None:
        out("  " + _("canlı durum: alınamadı (servis çalışmıyor ya da henüz bağlanmadı)"))
        return EXIT_OK
    if not st.get("aktif"):
        out("  " + _("şu an: sesli sohbet akışı yok"))
    else:
        apps = ", ".join(st.get("sesli") or []) or "?"
        kw = {"uygulamalar": apps, "db": st.get("kisma_db", 0), "olcum": st.get("olcum")}
        if st.get("olcum"):
            line = (_("şu an: {uygulamalar} · konuşma VAR · kısma {db:g} dB · ölçüm: {olcum}")
                    if st.get("konusma") else
                    _("şu an: {uygulamalar} · sessiz · kısma {db:g} dB · ölçüm: {olcum}"))
        else:
            line = (_("şu an: {uygulamalar} · konuşma VAR · kısma {db:g} dB") if st.get("konusma")
                    else _("şu an: {uygulamalar} · sessiz · kısma {db:g} dB"))
        out("  " + line.format(**kw))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run())
