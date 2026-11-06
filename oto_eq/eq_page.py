"""Kulak Kesil "Ekolayzır" page: interactive EQ editor for the 4 profiles (+ Düz).

``EqPage(parent=None)`` is a QWidget; ``set_status(status)`` takes the IPC
status dict (or None). Imported lazily by ``oto_eq.app``.

* profile selector (CS2 / Oyun / Müzik / Film / Düz) in the profile colors
* ``ResponseGraph``: QPainter response plot, draggable band handles
  (drag = frequency/gain, wheel = Q, right click = menu, double click on
  empty space = new band), HS80 correction dashed, "odak" band shaded
* band table kept in sync with the graph, dynamics panel with explanations
* "Canlı dinle": changes go to the running sinks via ``eqgen.set_live``
  (debounced, background thread); "Seviyeyi koru": recompute loudness
  matching live, otherwise the gain stages stay as saved (anti-clip still on)
* Kaydet / Geri al / Varsayılana dön, A/B (fixed:<profil> <-> off via IPC)

Uses the object names of app.py's global stylesheet (card, seg, segment,
primary, secondary, pageTitle, sectionTitle, muted, small) so it matches the
main window; colors of the plot come from the widget palette (Breeze
dark/light) and ``ui_common.PROFILE_COLORS``.
"""
from __future__ import annotations

import copy
import html
import logging
import math
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
from PyQt6.QtCore import (QEasingCurve, QObject, QPointF, QPropertyAnimation, QRectF, QSize, Qt,
                          QTimer, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import (QAction, QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath,
                         QPalette, QPen)
from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QButtonGroup, QComboBox,
                             QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                             QMenu, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider,
                             QStyle, QStyledItemDelegate, QTableWidget, QTableWidgetItem, QToolTip,
                             QVBoxLayout, QWidget, QDialog, QPlainTextEdit)

from . import config, eqgen, kulaklik
from . import ui_common as ui
from . import ui_text as tx
from .i18n import _
from .models import (ALL_SINK_PROFILES, EQ_PROFILES, FLAT_PROFILE, GROUP_LABELS, PROFILE_LABELS,
                     SINK_PREFIX, VARIANT_PROFILES, sink_name, variant_groups)

log = logging.getLogger("oto_eq.eq_page")

F_MIN, F_MAX = 20.0, 20000.0
GAIN_LIMIT = 15.0          # handles are clamped to ±15 dB (TOML allows ±24)
Q_MIN, Q_MAX = 0.1, 20.0
LIVE_DEBOUNCE_MS = 80
SINKS_SERVICE = config.SINKS_SERVICE

TYPE_NAMES = {"PK": _("Tepe (PK)"), "LSC": _("Alçak raf (LSC)"), "HSC": _("Yüksek raf (HSC)")}

# shaded "odak" region per profile: (low Hz, high Hz, label)
ODAK_BANDS: dict[str, tuple[float, float, str]] = {
    "cs2": (2000.0, 4500.0, _("Adım sesi bandı")),
    "oyun": (30.0, 120.0, _("Bas / etki")),
    "muzik": (150.0, 400.0, _("Sıcaklık")),
    "film": (1000.0, 4000.0, _("Konuşma bandı")),
}

DYN_PARAMS = [
    # key, label, min, max, step, unit, decimals
    ("esik_db", _("Eşik"), -60.0, 0.0, 0.5, "dB", 1),
    ("oran", _("Oran"), 1.0, 10.0, 0.1, ":1", 1),
    ("atak_ms", _("Atak"), 0.1, 50.0, 0.1, "ms", 1),
    ("birakma_ms", _("Bırakma"), 10.0, 1000.0, 5.0, "ms", 0),
    ("telafi_db", _("Telafi"), 0.0, 20.0, 0.5, "dB", 1),
]

DYN_HELP: dict[str, dict[str, str]] = {
    "cs2": {
        "_": _("Silah, flaş ve patlama gibi yüksek sesleri bastırır, telafi ile uzak/sessiz "
               "adımları onlara göre yukarı çeker. Gecikme eklemez (lookahead 0)."),
        "esik_db": _("Bunun üstündeki sesler (silah, flaş) kısılır. Düşürdükçe daha çok ses "
                     "sıkışır, adımlar görece öne çıkar."),
        "oran": _("Eşiği aşan ses ne kadar ezilsin. 3:1 → eşiğin 3 dB üstü 1 dB olur. "
                  "Yüksek oran = silahlar daha uysal."),
        "atak_ms": _("Ne kadar hızlı yakalasın. 1 ms silahın ilk vuruşunu da yakalar; "
                     "büyütürsen vuruş sertleşir."),
        "birakma_ms": _("Silahtan sonra ortam ne kadar çabuk geri gelsin. Kısa = adım hemen "
                        "duyulur; çok kısa = 'pompalama'."),
        "telafi_db": _("Sıkıştırmadan sonra hepsini yükseltir: sessiz adımlar yukarı gelir. "
                       "Toplam seviye eşitlemeyle sabit kalır."),
    },
    "film": {
        "_": _("Gece modu: patlamaları ve müziği bastırır, diyaloğu yükseltir. Sesi açıp "
               "kısmaya gerek kalmaz."),
        "esik_db": _("Bunun üstündeki sesler (patlama, müzik) kısılır. Düşürdükçe daha sıkı "
                     "gece modu."),
        "oran": _("Ne kadar sıkıştırılsın. 2–3:1 doğal, 4:1 üstü belirgin 'TV' sesi."),
        "atak_ms": _("Patlamayı ne kadar hızlı yakalasın. Çok kısa = diyalogda çıtırtı/"
                     "pompalama, 10 ms dengeli."),
        "birakma_ms": _("Patlamadan sonra diyalog ne kadar yumuşak geri gelsin. Uzun = daha "
                        "doğal, 200–300 ms önerilir."),
        "telafi_db": _("Sıkıştırmadan sonra hepsini yükseltir → fısıltı ve diyalog net "
                       "duyulur."),
    },
    "_": {
        "_": _("Hafif kompresör: yüksek anları yumuşatır, sessiz detayları yukarı çeker."),
        "esik_db": _("Bunun üstündeki sesler kısılır."),
        "oran": _("Eşiği aşan sesin ne kadar ezileceği (1:1 = hiç)."),
        "atak_ms": _("Sıkıştırmanın ne kadar hızlı başladığı."),
        "birakma_ms": _("Sıkıştırmanın ne kadar sürede bıraktığı."),
        "telafi_db": _("Sıkıştırma sonrası genel yükseltme (seviye eşitleme dengeler)."),
    },
}


# =========================================================================
# helpers
# =========================================================================
def profile_color(profile: str) -> str:
    return ui.PROFILE_COLORS.get(profile, ui.COLOR_INACTIVE)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def _alpha(c: QColor | str, a: float) -> QColor:
    c = QColor(c)
    c.setAlphaF(a)
    return c


def fmt_freq(f: float) -> str:
    if f >= 1000:
        return f"{f / 1000:.2f}".rstrip("0").rstrip(".") + " kHz"
    return f"{f:.0f} Hz"


def round_freq(f: float) -> float:
    if f < 100:
        return round(f, 1)
    if f < 1000:
        return float(round(f))
    return float(round(f / 10) * 10)


def restart_sinks_service() -> tuple[bool, str]:
    """systemctl --user restart oto-eq-sinks.service (mocked in tests)."""
    try:
        r = subprocess.run(["systemctl", "--user", "restart", SINKS_SERVICE], env=config.C_ENV,
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def ipc_request(obj: dict) -> dict:
    """IPC request to the router (mocked in tests)."""
    from . import ipc
    return ipc.request(obj, timeout=1.5)


def label(text: str, name: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(wrap)
    return lab


def button(text: str, kind: str = "secondary") -> QPushButton:
    b = QPushButton(text)
    b.setObjectName(kind)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def card() -> QFrame:
    f = QFrame()
    f.setObjectName("card")
    return f


class Switch(QAbstractButton):
    """Compact animated on/off switch."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setText(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self.on_color = QColor("#3DAEE9")
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(130)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def get_knob(self) -> float:
        return self._pos

    def set_knob(self, v: float) -> None:
        self._pos = v
        self.update()

    knob = pyqtProperty(float, get_knob, set_knob)

    def setChecked(self, on: bool) -> None:  # noqa: N802 - Qt API
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        tw = fm.horizontalAdvance(self.text()) + 10 if self.text() else 0
        return QSize(40 + tw, max(22, fm.height() + 4))

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        h = 20.0
        y = (self.height() - h) / 2
        track = QRectF(1, y, 36, h)
        off = _mix(pal.color(QPalette.ColorRole.Window), pal.color(QPalette.ColorRole.WindowText),
                   0.28)
        col = _mix(off, self.on_color, self._pos)
        if not self.isEnabled():
            col = _alpha(col, 0.45)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(track, h / 2, h / 2)
        r = h / 2 - 3
        cx = track.left() + 3 + r + self._pos * (track.width() - 6 - 2 * r)
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(cx, y + h / 2), r, r)
        if self.text():
            p.setPen(pal.color(QPalette.ColorRole.WindowText if self.isEnabled()
                               else QPalette.ColorRole.PlaceholderText))
            p.drawText(QRectF(46, 0, self.width() - 46, self.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())
        p.end()


# =========================================================================
# live apply worker (keeps the GUI responsive: pw-dump + pw-cli take ~100 ms)
# =========================================================================
class LiveApplier(QObject):
    """Applies only the newest submitted job on a background thread."""

    done = pyqtSignal(bool, str)

    def __init__(self, parent=None, prefix: str = SINK_PREFIX):
        super().__init__(parent)
        self.prefix = prefix
        self._cv = threading.Condition()
        self._job = None
        self._stop = False
        self._busy = False
        self._thread = threading.Thread(target=self._run, name="oto-eq-canli", daemon=True)
        self._thread.start()

    def submit(self, profile: str | list[tuple[str, dict[str, str] | None]], profiles: dict,
               staging: dict | None, variants: dict[str, str] | None = None) -> None:
        """``profile`` may be a list of (profile, variants) pairs applied in one job (the
        worker keeps only the newest job, so several submits in a row would drop all but
        the last)."""
        items = profile if isinstance(profile, list) else [(profile, variants)]
        with self._cv:
            self._job = ([(p, dict(v) if v is not None else None) for p, v in items],
                         copy.deepcopy(profiles), copy.deepcopy(staging))
            self._cv.notify()

    def idle(self) -> bool:
        with self._cv:
            return self._job is None and not self._busy

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._job is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                job, self._job = self._job, None
                self._busy = True
            items, profiles, staging = job
            ok, msg = True, ""
            try:
                for profile, variants in items:
                    ok = eqgen.set_live(profile, profiles, prefix=self.prefix, staging=staging,
                                        variants=variants) and ok
            except Exception as e:  # noqa: BLE001 - report, never kill the thread
                ok, msg = False, str(e)
            with self._cv:
                self._busy = False
            try:
                self.done.emit(bool(ok), msg)
            except RuntimeError:   # QObject deleted
                return


# =========================================================================
# "Kişisel ton" (C1: oto_eq.kisisel + eqgen.apply_tone). Both are optional: the
# group hides itself with a note while they are missing (imported lazily).
# =========================================================================
TONE_DEBOUNCE_MS = 150
TONE_RAMP_S = 0.3
TONE_STEP = 0.5
TONE_DEFAULT_LIMIT = 3.0
TONE_DEFAULT_KEYS = ("bas", "vokal", "parlaklik")
TONE_PROFILES = ("cs2", "oyun", "muzik", "film")          # never Düz
TONE_DEFAULT_BANDS = {"bas": ("LSC", 120.0, 0.7), "vokal": ("PK", 2500.0, 0.8),
                      "parlaklik": ("HSC", 8000.0, 0.7)}
TONE_LABELS = {"bas": _("Bas sıcaklığı"), "vokal": _("Vokal netliği"), "parlaklik": _("Parlaklık")}
TONE_HELP = {
    "bas": _("120 Hz altı: davul, bas gitar, patlamalar. Artı = daha dolgun ve sıcak, "
             "eksi = daha sıkı ve temiz."),
    "vokal": _("2,5 kHz civarı: şarkı sözü, konuşma, diyalog. Artı = sesler öne çıkar, "
               "eksi = daha yumuşak, uzun dinlemede yormaz."),
    "parlaklik": _("8 kHz üstü: zil, nefes, ince detay. Artı = daha parlak ve havadar, "
                   "eksi = daha yumuşak, cızırtı/sertlik azalır."),
}
TONE_INTRO = (_("Profillerin üstüne kendi zevkine göre küçük bir ayar (en çok ±3 dB). "
                "Genel ayar her profilde geçerli; \"bu tür için\" ayarı yalnız o tür çalarken "
                "genelin üstüne eklenir. CS2 varsayılan olarak hariç: adım sesi ayarı bozulmasın. "
                "Kulağın karar versin istersen \"Kör A/B dinle…\" ile neyi seçtiğini bilmeden dene."))
TERMINALS = (("ghostty", ("-e",)), ("konsole", ("-e",)), ("xterm", ("-e",)))


def load_kisisel():
    """C1's ``oto_eq.kisisel`` module, or None while missing/incomplete (patched in tests)."""
    try:
        from . import kisisel
    except Exception as e:  # noqa: BLE001 - missing or half-written module: hide the group
        log.info("kişisel ton modülü yok: %s", e)
        return None
    if not all(callable(getattr(kisisel, n, None)) for n in ("load", "save", "effective")):
        log.info("kişisel ton modülü eksik (load/save/effective)")
        return None
    return kisisel


def tone_apply_fn():
    """``eqgen.apply_tone`` once C1 added it, else None (no live preview)."""
    fn = getattr(eqgen, "apply_tone", None)
    return fn if callable(fn) else None


def fmt_tone(v: float) -> str:
    """'+1,5 dB' / '+1.5 dB' (decimal mark of the UI language) / '0 dB' (A/B may store 0.01 dB)."""
    v = round(float(v), 1)
    if abs(v) < 0.05:
        return "0 dB"
    return tx.ondalik(f"{v:+.1f}") + " dB"


def _band_spec(spec, default: tuple) -> tuple[str, float, float]:
    try:
        if isinstance(spec, dict):
            return str(spec["tip"]), float(spec["fc"]), float(spec["q"])
        tip, fc, q = list(spec)[:3]
        return str(tip), float(fc), float(q)
    except (KeyError, TypeError, ValueError):
        return default


def tone_filters(values: dict | None, kisisel=None) -> list[dict]:
    """k-band filter dicts (eqgen's helper when present) for the response plot; [] = all 0."""
    vals = {k: float(v) for k, v in (values or {}).items()}
    if not any(abs(v) >= 0.05 for v in vals.values()):
        return []
    fn = getattr(eqgen, "tone_filters", None)
    if callable(fn):
        try:
            out = [dict(b) for b in fn(vals)]
            if out and all({"tip", "fc", "kazanc", "q"} <= set(b) for b in out):
                return out
        except Exception:  # noqa: BLE001 - fall back to our own k-band table
            log.debug("eqgen.tone_filters başarısız", exc_info=True)
    specs = getattr(kisisel, "TON_BANDS", None) or TONE_DEFAULT_BANDS
    out = []
    for key, default in TONE_DEFAULT_BANDS.items():
        if key in vals:
            tip, fc, q = _band_spec(specs.get(key, default), default)
            out.append({"tip": tip, "fc": fc, "kazanc": vals[key], "q": q, "aktif": True})
    return out


def music_playing(status: dict | None) -> bool:
    """Something is routed to the Müzik sink (the A/B test needs it)."""
    return any(s.get("profile") == "muzik" for s in (status or {}).get("streams") or [])


def terminal_command(args: list[str], which=shutil.which) -> list[str] | None:
    """[terminal, -e, *args] for the first installed terminal (ghostty → konsole → xterm)."""
    for name, flags in TERMINALS:
        path = which(name)
        if path:
            return [path, *flags, *args]
    return None


def kulak_command(*args: str, which=shutil.which) -> list[str]:
    exe = which("kulak")
    if not exe:
        cand = Path.home() / ".local" / "bin" / "kulak"
        exe = str(cand) if cand.exists() else None
    return [exe, *args] if exe else [sys.executable, "-m", "oto_eq.cli", *args]


def launch_terminal(cmd: list[str]) -> bool:
    """Detached terminal window (mocked in tests)."""
    return ui.launch_detached(cmd)


class ToneApplier(QObject):
    """Runs the newest list of ``apply_tone`` jobs on a background thread."""

    done = pyqtSignal(bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cv = threading.Condition()
        self._jobs: list | None = None
        self._stop = False
        self._busy = False
        self._thread = threading.Thread(target=self._run, name="oto-eq-ton", daemon=True)
        self._thread.start()

    def submit(self, jobs: list[tuple[str, str, dict]]) -> None:
        """jobs: [(profile, group, values)]; replaces a not yet started list."""
        with self._cv:
            merged = {p: (p, g, v) for p, g, v in (self._jobs or [])}
            merged.update({p: (p, g, dict(v)) for p, g, v in jobs})
            self._jobs = list(merged.values())
            self._cv.notify()

    def idle(self) -> bool:
        with self._cv:
            return self._jobs is None and not self._busy

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify()

    @staticmethod
    def apply(jobs) -> tuple[bool, str]:
        fn = tone_apply_fn()
        if fn is None:
            return False, _("canlı önizleme yok (eqgen.apply_tone bulunamadı)")
        ok, msg = True, ""
        for profile, group, values in jobs:
            try:
                if not fn(profile, group, ramp_s=TONE_RAMP_S, values=dict(values)):
                    ok, msg = False, _("{profil} çıkışına uygulanamadı").format(
                        profil=PROFILE_LABELS.get(profile, profile))
            except Exception as e:  # noqa: BLE001 - report, never kill the thread
                ok, msg = False, str(e)
        return ok, msg

    def _run(self) -> None:
        while True:
            with self._cv:
                while self._jobs is None and not self._stop:
                    self._cv.wait()
                if self._stop:
                    return
                jobs, self._jobs = self._jobs, None
                self._busy = True
            ok, msg = self.apply(jobs)
            with self._cv:
                self._busy = False
            try:
                self.done.emit(ok, msg)
            except RuntimeError:   # QObject deleted
                return


# =========================================================================
# the graph
# =========================================================================
class ResponseGraph(QWidget):
    """Interactive log-frequency response plot of one profile."""

    bandChanged = pyqtSignal(int)          # band index (values changed by drag/wheel)
    bandSelected = pyqtSignal(int)         # -1 = none
    bandAddRequested = pyqtSignal(float, float)
    bandRemoveRequested = pyqtSignal(int)
    bandToggleRequested = pyqtSignal(int)
    bandTypeRequested = pyqtSignal(int, str)

    PAD_L, PAD_R, PAD_T, PAD_B = 46, 14, 14, 26
    HANDLE_R = 8.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumHeight(320)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.profiles: dict | None = None
        self.profile = "cs2"
        self.selected = -1
        self.hover = -1
        self._drag = -1
        self._drag_start = None
        self.editable = True
        self.flat_gain_db = 0.0
        self.variant_bands: list[dict] | None = None   # 4 slot dicts when editing a variant
        self.variant_name = ""
        self.tone_bands: list[dict] | None = None      # "Kişisel ton" k-bands (None = off/0 dB)
        self.device_correction = True      # the HS80 curve fits the active headphone (CHANGE 7)
        self._freqs = np.geomspace(F_MIN, F_MAX, 420)
        self.y_min, self.y_max = -15.0, 15.0

    # --- data
    def set_data(self, profiles: dict, profile: str, flat_gain_db: float = 0.0) -> None:
        self.profiles = profiles
        self.profile = profile
        self.flat_gain_db = flat_gain_db
        self.editable = profile != FLAT_PROFILE
        if profile not in VARIANT_PROFILES:
            self.variant_bands = None
        if self.selected >= len(self.bands()):
            self.selected = -1
        self.update()

    def set_variant(self, slots: list[dict] | None, name: str = "") -> None:
        self.variant_bands = slots
        self.variant_name = name
        self.selected = -1
        self.update()

    def bands(self) -> list[dict]:
        if not self.profiles or self.profile == FLAT_PROFILE:
            return []
        if self.variant_bands is not None:
            return self.variant_bands
        return self.profiles["profil"][self.profile]["filtreler"]

    def _rate(self) -> float:
        return self.profiles["genel"]["orneklem_hz"] if self.profiles else 48000

    def curves(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(correction, editable layer, total) in dB at self._freqs.

        In variant mode the editable layer is the variant's delta and the total
        includes correction + profile + variant (see base_total())."""
        f = self._freqs
        if not self.profiles or self.profile == FLAT_PROFILE:
            z = np.zeros_like(f)
            return z, z, z
        p = self.profiles["profil"][self.profile]
        on = p.get("aktif", True)
        corr_on = self.correction_audible() and on
        corr = eqgen.response_db(self.profiles["duzeltme"]["filtreler"] if corr_on else [], f,
                                 self._rate())
        layer = eqgen.response_db(p["filtreler"] if on else [], f, self._rate())
        tone = self.tone_curve()
        if self.variant_bands is not None:
            vl = eqgen.response_db(self.variant_bands if on else [], f, self._rate())
            return corr, vl, corr + layer + vl + tone
        return corr, layer, corr + layer + tone

    def correction_audible(self) -> bool:
        """[duzeltme] switched on AND it fits the active headphone (eqgen.correction_on)."""
        return bool(self.profiles["duzeltme"].get("aktif", True)) and self.device_correction

    def tone_curve(self) -> np.ndarray:
        """Response of the personal-tone k-bands (zeros when off)."""
        if not self.tone_bands or not self.profiles or self.profile == FLAT_PROFILE \
                or not self.profiles["profil"][self.profile].get("aktif", True):
            return np.zeros_like(self._freqs)
        return eqgen.response_db(self.tone_bands, self._freqs, self._rate())

    def base_total(self) -> np.ndarray:
        """Correction + profile without the variant (the "Genel" curve)."""
        f = self._freqs
        p = self.profiles["profil"][self.profile]
        on = p.get("aktif", True)
        corr_on = self.correction_audible() and on
        flt = (self.profiles["duzeltme"]["filtreler"] if corr_on else []) + \
            (p["filtreler"] if on else [])
        return eqgen.response_db(flt, f, self._rate()) + self.tone_curve()

    # --- geometry
    def plot_rect(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, max(10, self.width() - self.PAD_L - self.PAD_R),
                      max(10, self.height() - self.PAD_T - self.PAD_B))

    def x_of(self, f: float) -> float:
        r = self.plot_rect()
        t = (math.log10(max(f, 1e-3)) - math.log10(F_MIN)) / (math.log10(F_MAX) - math.log10(F_MIN))
        return r.left() + t * r.width()

    def f_of(self, x: float) -> float:
        r = self.plot_rect()
        t = (x - r.left()) / r.width()
        return 10 ** (math.log10(F_MIN) + t * (math.log10(F_MAX) - math.log10(F_MIN)))

    def y_of(self, db: float) -> float:
        r = self.plot_rect()
        return r.top() + (self.y_max - db) / (self.y_max - self.y_min) * r.height()

    def db_of(self, y: float) -> float:
        r = self.plot_rect()
        return self.y_max - (y - r.top()) / r.height() * (self.y_max - self.y_min)

    def handle_pos(self, i: int) -> QPointF:
        b = self.bands()[i]
        return QPointF(self.x_of(b["fc"]), self.y_of(b["kazanc"]))

    def handle_at(self, pos: QPointF) -> int:
        best, best_d = -1, self.HANDLE_R + 5
        for i in range(len(self.bands())):
            d = math.hypot(pos.x() - self.handle_pos(i).x(), pos.y() - self.handle_pos(i).y())
            if d <= best_d:
                best, best_d = i, d
        return best

    def _auto_range(self, curves) -> None:
        lo = min(-12.0, *(float(np.min(c)) for c in curves),
                 *(b["kazanc"] for b in self.bands()))
        hi = max(12.0, *(float(np.max(c)) for c in curves),
                 *(b["kazanc"] for b in self.bands()))
        step = 3.0
        lo, hi = math.floor((lo - 1) / step) * step, math.ceil((hi + 1) / step) * step
        if self._drag < 0:          # never rescale while dragging (handle would jump)
            self.y_min, self.y_max = max(lo, -30.0), min(hi, 30.0)

    # --- painting
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        win = pal.color(QPalette.ColorRole.Window)
        text = pal.color(QPalette.ColorRole.WindowText)
        dark = win.lightnessF() < 0.5
        base = _mix(win, QColor("black" if dark else "white"), 0.18 if dark else 0.55)
        color = QColor(profile_color(self.profile))
        r = self.plot_rect()
        corr, layer, total = self.curves()
        self._auto_range((corr, layer, total))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(base)
        p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 8, 8)
        p.save()
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)

        # odak band
        ob = ODAK_BANDS.get(self.profile)
        if ob:
            x0, x1 = self.x_of(ob[0]), self.x_of(ob[1])
            p.fillRect(QRectF(x0, r.top(), x1 - x0, r.height()), _alpha(color, 0.10 if dark else 0.08))

        # grid
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, self.font().pointSizeF() * 0.82))
        grid = _alpha(text, 0.10)
        grid_minor = _alpha(text, 0.05)
        for dec in (10, 100, 1000, 10000):
            for m in range(1, 10):
                f = dec * m
                if F_MIN <= f <= F_MAX:
                    p.setPen(QPen(grid if m == 1 else grid_minor, 1))
                    x = self.x_of(f)
                    p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        db = math.ceil(self.y_min / 3) * 3
        while db <= self.y_max:
            p.setPen(QPen(_alpha(text, 0.30) if db == 0 else grid, 1.2 if db == 0 else 1))
            y = self.y_of(db)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            db += 3

        xs = [self.x_of(f) for f in self._freqs]

        def path_of(vals) -> QPainterPath:
            path = QPainterPath()
            for i, (x, v) in enumerate(zip(xs, vals)):
                pt = QPointF(x, self.y_of(float(v)))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            return path

        if self.profile == FLAT_PROFILE:
            pen = QPen(color, 3)
            p.setPen(pen)
            p.drawLine(QPointF(r.left(), self.y_of(0)), QPointF(r.right(), self.y_of(0)))
        else:
            # profile layer, filled towards 0 dB
            lp = path_of(layer)
            fill = QPainterPath(lp)
            fill.lineTo(QPointF(xs[-1], self.y_of(0)))
            fill.lineTo(QPointF(xs[0], self.y_of(0)))
            fill.closeSubpath()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_alpha(color, 0.16))
            p.drawPath(fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(_alpha(color, 0.75), 1.4))
            p.drawPath(lp)
            # HS80 correction dashed
            pen = QPen(_alpha(text, 0.55), 1.5, Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPath(path_of(corr))
            if self.variant_bands is not None:   # Genel curve for comparison
                p.setPen(QPen(_alpha(color, 0.55), 1.8, Qt.PenStyle.DashDotLine))
                p.drawPath(path_of(self.base_total()))
            if self.tone_bands:                  # personal tone layer (k-bands)
                p.setPen(QPen(_alpha(text, 0.70), 1.6, Qt.PenStyle.DotLine))
                p.drawPath(path_of(self.tone_curve()))
            # total, thick with a soft glow
            tp = path_of(total)
            p.setPen(QPen(_alpha(color, 0.25), 7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawPath(tp)
            p.setPen(QPen(color, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            p.drawPath(tp)
        p.restore()

        # odak label
        p.setFont(small)
        if ob:
            x0, x1 = self.x_of(ob[0]), self.x_of(ob[1])
            p.setPen(_alpha(color, 0.95))
            p.drawText(QRectF(x0, r.top() + 4, x1 - x0, 16), Qt.AlignmentFlag.AlignCenter, ob[2])

        # axes labels
        p.setPen(_alpha(text, 0.60))
        for f, t in ((20, "20"), (50, "50"), (100, "100"), (200, "200"), (500, "500"),
                     (1000, "1k"), (2000, "2k"), (5000, "5k"), (10000, "10k"), (20000, "20k")):
            x = self.x_of(f)
            p.drawText(QRectF(x - 20, r.bottom() + 4, 40, 18), Qt.AlignmentFlag.AlignCenter, t)
        db = math.ceil(self.y_min / 6) * 6
        while db <= self.y_max:
            y = self.y_of(db)
            p.drawText(QRectF(0, y - 9, self.PAD_L - 8, 18),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{db:+.0f}"
                       if db else "0 dB")
            db += 6

        # handles
        if self.editable:
            bold = QFont(small)
            bold.setBold(True)
            p.setFont(bold)
            for i, b in enumerate(self.bands()):
                c = self.handle_pos(i)
                on = b.get("aktif", True)
                sel = i == self.selected
                rad = self.HANDLE_R + (2 if (sel or i == self.hover) else 0)
                if sel:
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_alpha(color, 0.28))
                    p.drawEllipse(c, rad + 6, rad + 6)
                p.setPen(QPen(QColor("white") if on else _alpha(text, 0.6), 2))
                p.setBrush(color if on else base)
                p.drawEllipse(c, rad, rad)
                p.setPen(QColor("white") if on else _alpha(text, 0.7))
                p.drawText(QRectF(c.x() - rad, c.y() - rad, 2 * rad, 2 * rad),
                           Qt.AlignmentFlag.AlignCenter, str(i + 1))

        # legend
        p.setFont(small)
        lx, ly = r.left() + 10, r.bottom() - 10
        corr_name = _("HS80 düzeltmesi") if self.device_correction \
            else _("HS80 düzeltmesi (bu kulaklıkta yok)")
        items = ([(corr_name, _alpha(text, 0.55), Qt.PenStyle.DashLine, 1.5),
                  (_("Genel (türsüz) toplam"), _alpha(color, 0.55), Qt.PenStyle.DashDotLine, 1.8),
                  (_("Tür farkı: {tur}").format(
                      tur=GROUP_LABELS.get(self.variant_name, self.variant_name)),
                   _alpha(color, 0.75), Qt.PenStyle.SolidLine, 1.4),
                  (_("Toplam (tür dahil)"), color, Qt.PenStyle.SolidLine, 3.0)]
                 if self.variant_bands is not None else
                 [(corr_name, _alpha(text, 0.55), Qt.PenStyle.DashLine, 1.5),
                  (_("Profil katmanı"), _alpha(color, 0.75), Qt.PenStyle.SolidLine, 1.4),
                  (_("Toplam"), color, Qt.PenStyle.SolidLine, 3.0)]
                 if self.profile != FLAT_PROFILE else
                 [(_("Düz geçiş (EQ yok) · seviye {db:+.1f} dB").format(db=self.flat_gain_db),
                   color, Qt.PenStyle.SolidLine, 3.0)])
        if self.tone_bands and self.profile != FLAT_PROFILE:
            items.insert(len(items) - 1, (_("Kişisel ton"), _alpha(text, 0.70),
                                          Qt.PenStyle.DotLine, 1.6))
        for name, col, style, w in reversed(items):
            p.setPen(QPen(col, w, style))
            p.drawLine(QPointF(lx, ly - 5), QPointF(lx + 22, ly - 5))
            p.setPen(_alpha(text, 0.75))
            p.drawText(QPointF(lx + 28, ly), name)
            ly -= 17
        p.end()

    # --- interaction
    def _tooltip(self, i: int, global_pos) -> None:
        b = self.bands()[i]
        lines = [_("Bant {n} · {tip}").format(n=i + 1, tip=TYPE_NAMES[b["tip"]]),
                 f"{fmt_freq(b['fc'])}   {b['kazanc']:+.1f} dB   Q {b['q']:.2f}"]
        if not b.get("aktif", True):
            lines.append(_("(kapalı)"))
        QToolTip.showText(global_pos, "\n".join(lines), self)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self.editable:
            return
        i = self.handle_at(e.position())
        if e.button() == Qt.MouseButton.LeftButton:
            self.selected = i
            self.bandSelected.emit(i)
            if i >= 0:
                self._drag = i
                self._drag_start = (e.position(), self.bands()[i]["fc"], self.bands()[i]["kazanc"])
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.update()
        elif e.button() == Qt.MouseButton.RightButton and i >= 0:
            self.selected = i
            self.bandSelected.emit(i)
            self.update()
            self._menu(i, e.globalPosition().toPoint())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if not self.editable:
            return
        pos = e.position()
        if self._drag >= 0:
            b = self.bands()[self._drag]
            start, f0, g0 = self._drag_start
            fine = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            k = 0.25 if fine else 1.0
            if not (e.modifiers() & Qt.KeyboardModifier.ShiftModifier):   # Shift = gain only
                x = self.x_of(f0) + (pos.x() - start.x()) * k
                b["fc"] = round_freq(min(max(self.f_of(x), F_MIN), F_MAX * 0.95))
            y = self.y_of(g0) + (pos.y() - start.y()) * k
            b["kazanc"] = round(min(max(self.db_of(y), -GAIN_LIMIT), GAIN_LIMIT), 1)
            self.bandChanged.emit(self._drag)
            self._tooltip(self._drag, e.globalPosition().toPoint())
            self.update()
            return
        i = self.handle_at(pos)
        if i != self.hover:
            self.hover = i
            self.setCursor(Qt.CursorShape.OpenHandCursor if i >= 0 else Qt.CursorShape.ArrowCursor)
            self.update()
        if i >= 0:
            self._tooltip(i, e.globalPosition().toPoint())

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        if self._drag >= 0:
            self._drag = -1
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self.update()

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        if not self.editable:
            return
        i = self.handle_at(e.position())
        if i >= 0:
            self.bandToggleRequested.emit(i)
        elif self.variant_bands is None and self.plot_rect().contains(e.position()):
            self.bandAddRequested.emit(round_freq(self.f_of(e.position().x())),
                                       round(min(max(self.db_of(e.position().y()), -GAIN_LIMIT),
                                                 GAIN_LIMIT), 1))

    def wheelEvent(self, e) -> None:  # noqa: N802
        i = self.handle_at(e.position()) if self.editable else -1
        if i < 0:
            i = self.selected if (self.editable and self.selected >= 0
                                  and self.plot_rect().contains(e.position())) else -1
        if i < 0:
            e.ignore()
            return
        steps = e.angleDelta().y() / 120.0
        b = self.bands()[i]
        b["q"] = round(min(max(b["q"] * (1.12 ** steps), Q_MIN), Q_MAX), 2)
        self.selected = i
        self.bandChanged.emit(i)
        self._tooltip(i, e.globalPosition().toPoint())
        self.update()
        e.accept()

    def leaveEvent(self, _e) -> None:  # noqa: N802
        self.hover = -1
        self.update()

    def _reset_slot(self, i: int) -> None:
        self.bands()[i]["kazanc"] = 0.0
        self.bandChanged.emit(i)
        self.update()

    def _menu(self, i: int, gpos) -> None:
        b = self.bands()[i]
        m = QMenu(self)
        t = m.addAction(_("Kapat") if b.get("aktif", True) else _("Aç"))
        t.triggered.connect(lambda: self.bandToggleRequested.emit(i))
        if self.variant_bands is not None:     # fixed slot types: only reset
            z = m.addAction(_("0 dB'e sıfırla"))
            z.triggered.connect(lambda: self._reset_slot(i))
            m.popup(gpos)
            return
        tm = m.addMenu(_("Tip"))
        for k, name in TYPE_NAMES.items():
            a = QAction(name, tm)
            a.setCheckable(True)
            a.setChecked(k == b["tip"])
            a.triggered.connect(lambda _c=False, k=k: self.bandTypeRequested.emit(i, k))
            tm.addAction(a)
        m.addSeparator()
        d = m.addAction(_("Bandı sil"))
        d.triggered.connect(lambda: self.bandRemoveRequested.emit(i))
        m.popup(gpos)


# =========================================================================
# band table delegate: clean text cells, spin/combo editors on edit
# =========================================================================
COL_NUM, COL_ON, COL_TYPE, COL_FREQ, COL_GAIN, COL_Q = range(6)
VALUE_ROLE = Qt.ItemDataRole.UserRole
EDIT_SPECS = {  # column: (key, min, max, decimals, step, suffix)
    COL_FREQ: ("fc", F_MIN, F_MAX * 0.95, 0, 10.0, " Hz"),
    COL_GAIN: ("kazanc", -24.0, 24.0, 1, 0.5, " dB"),
    COL_Q: ("q", 0.05, 50.0, 2, 0.05, ""),
}


def cell_text(col: int, v) -> str:
    if col == COL_TYPE:
        return TYPE_NAMES.get(v, str(v))
    if col == COL_FREQ:
        return fmt_freq(float(v))
    if col == COL_GAIN:
        return f"{float(v):+.1f} dB"
    if col == COL_Q:
        return f"{float(v):.2f}"
    return str(v)


class BandDelegate(QStyledItemDelegate):
    def __init__(self, page: "EqPage"):
        super().__init__(page)
        self.page = page

    def paint(self, p, option, index) -> None:
        if index.column() != COL_NUM:
            super().paint(p, option, index)
            return
        if option.state & QStyle.StateFlag.State_Selected:
            p.fillRect(option.rect, _alpha(option.palette.highlight().color(), 0.22))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(profile_color(self.page.profile)))
        p.drawEllipse(r, 10, 10)
        f = QFont(option.font)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("white"))
        p.drawText(QRectF(r.x() - 10, r.y() - 10, 20, 20), Qt.AlignmentFlag.AlignCenter,
                   str(index.row() + 1))
        p.restore()

    def createEditor(self, parent, option, index):  # noqa: N802
        col = index.column()
        if col == COL_TYPE:
            cb = QComboBox(parent)
            for k, name in TYPE_NAMES.items():
                cb.addItem(name, k)
            cb.activated.connect(lambda _i, cb=cb: self.commitData.emit(cb))
            return cb
        if col in EDIT_SPECS:
            _k, lo, hi, dec, step, suffix = EDIT_SPECS[col]
            sp = QDoubleSpinBox(parent)
            sp.setRange(lo, hi)
            sp.setDecimals(dec)
            sp.setSingleStep(step)
            sp.setSuffix(suffix)
            sp.setKeyboardTracking(False)
            sp.valueChanged.connect(lambda _v, sp=sp: self.commitData.emit(sp))
            return sp
        return None

    def setEditorData(self, editor, index) -> None:  # noqa: N802
        v = index.data(VALUE_ROLE)
        if isinstance(editor, QComboBox):
            editor.setCurrentIndex(max(0, editor.findData(v)))
        elif isinstance(editor, QDoubleSpinBox):
            editor.blockSignals(True)
            editor.setValue(float(v))
            editor.blockSignals(False)

    def setModelData(self, editor, model, index) -> None:  # noqa: N802
        if isinstance(editor, QComboBox):
            v = editor.currentData()
        elif isinstance(editor, QDoubleSpinBox):
            v = float(editor.value())
        else:
            return
        self.page._table_edit(index.row(), index.column(), v)


# =========================================================================
# blind ABX test
# =========================================================================
def binom_p(k: int, n: int) -> float:
    """One-sided p-value: P(at least k of n correct by guessing, p = 0.5)."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n


def abx_verdict(k: int, n: int) -> str:
    p = binom_p(k, n)
    yuzde = tx.percent_text(p * 100, 1)       # "%3,5" / "3.5%" / "3,5 %" by language
    if p < 0.01:
        text = _("{k}/{n} doğru = kesinlikle fark duyuyorsun. Bunun şans eseri olma ihtimali "
                 "{yuzde}.")
    elif p < 0.05:
        text = _("{k}/{n} doğru = gerçekten fark duyuyorsun. Bunun şans eseri olma ihtimali "
                 "{yuzde} (bilimsel eşik {esik}).")
    elif p < 0.2:
        text = _("{k}/{n} doğru: bir şeyler duyuyor olabilirsin ama kanıt zayıf (şans ihtimali "
                 "{yuzde}). Daha sessiz bir ortamda yeniden dene.")
    else:
        text = _("{k}/{n} doğru: tahminden ayırt edilemiyor (şans ihtimali {yuzde}). Fark ya çok "
                 "küçük ya da yok — bu da değerli: daha hafif bir ayar yetebilir.")
    return text.format(n=n, k=k, yuzde=yuzde, esik=tx.percent_text(5, 0))


class AbxSession:
    """Pure ABX bookkeeping. opt1/opt2 are the two compared sources."""

    def __init__(self, opt1: str, opt2: str, rounds: int = 8, rng: random.Random | None = None):
        self.rng = rng or random.SystemRandom()
        self.rounds = rounds
        self.opt1, self.opt2 = opt1, opt2
        self.mapping = {"A": opt1, "B": opt2} if self.rng.random() < 0.5 else {"A": opt2, "B": opt1}
        self.xs = [self.rng.choice("AB") for _i in range(rounds)]
        self.answers: list[str] = []

    @property
    def index(self) -> int:
        return len(self.answers)

    @property
    def done(self) -> bool:
        return self.index >= self.rounds

    def source_of(self, letter: str) -> str:
        """'A'/'B'/'X' -> the option to play (X = this round's hidden pick)."""
        if letter == "X":
            letter = self.xs[min(self.index, self.rounds - 1)]
        return self.mapping[letter]

    def answer(self, letter: str) -> bool:
        if self.done:
            raise RuntimeError("test bitti")
        ok = letter == self.xs[self.index]
        self.answers.append(letter)
        return ok

    @property
    def score(self) -> int:
        return sum(a == x for a, x in zip(self.answers, self.xs))

    def p_value(self) -> float:
        return binom_p(self.score, len(self.answers))


class AbxDialog(QDialog):
    """Kör test window. ``player(option)`` switches what the user hears."""

    finishedTest = pyqtSignal()

    def __init__(self, title: str, labels: dict[str, str], player, color: str,
                 rounds: int = 8, rng=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Kör test (ABX)"))
        self.setObjectName("abx")
        self.labels = labels            # option -> human name (revealed at the end)
        self.player = player
        self.color = color
        self.rounds = rounds
        self.rng = rng
        self.playing: str | None = None
        self.gecersiz = False           # a switch failed (router unreachable): no statistics
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(12)
        lay.addWidget(label(title, "sectionTitle"))
        self.info = label("", "muted", wrap=True)
        lay.addWidget(self.info)
        self.round_lbl = label("", "sectionTitle")
        lay.addWidget(self.round_lbl)
        self.dots = QLabel()
        lay.addWidget(self.dots)
        listen = QHBoxLayout()
        self.src_buttons: dict[str, QPushButton] = {}
        for letter in ("A", "X", "B"):
            b = QPushButton(_("{harf}\ndinle").format(harf=letter))
            b.setCheckable(True)
            b.setMinimumHeight(74)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ font-size: 15pt; font-weight: 700; border-radius: 12px;"
                f" border: 2px solid {_alpha(color, 0.55).name(QColor.NameFormat.HexArgb)}; }}"
                f"QPushButton:checked {{ background: {color}; color: white; border-color: {color}; }}")
            b.clicked.connect(lambda _c=False, L=letter: self.listen(L))
            self.src_buttons[letter] = b
            listen.addWidget(b)
        self.listen_row = QWidget()
        self.listen_row.setLayout(listen)
        listen.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.listen_row)
        self.now_lbl = label("", "small")
        lay.addWidget(self.now_lbl)
        ans = QHBoxLayout()
        ans.addWidget(label(_("X hangisi?"), "sectionTitle"))
        ans.addStretch(1)
        self.ans_a = button("X = A", "primary")
        self.ans_b = button("X = B", "primary")
        for b in (self.ans_a, self.ans_b):
            b.setMinimumSize(130, 42)
        self.ans_a.clicked.connect(lambda: self.choose("A"))
        self.ans_b.clicked.connect(lambda: self.choose("B"))
        ans.addWidget(self.ans_a)
        ans.addWidget(self.ans_b)
        self.ans_row = QWidget()
        self.ans_row.setLayout(ans)
        lay.addWidget(self.ans_row)
        self.result = label("", None, wrap=True)
        self.result.setVisible(False)
        lay.addWidget(self.result)
        foot = QHBoxLayout()
        foot.addStretch(1)
        self.again_btn = button(_("Yeniden başla"))
        self.again_btn.clicked.connect(self.restart)
        self.close_btn = button(_("Kapat"))
        self.close_btn.clicked.connect(self.close)
        foot.addWidget(self.again_btn)
        foot.addWidget(self.close_btn)
        lay.addLayout(foot)
        self.restart()

    def restart(self) -> None:
        opts = list(self.labels)
        self.session = AbxSession(opts[0], opts[1], self.rounds, self.rng)
        self.info.setText(_(
            "A ve B iki farklı ayar (hangisinin hangisi olduğu gizli). Her turda X bunlardan "
            "biri. A, B ve X'i istediğin kadar dinle, sonra X'in hangisi olduğunu seç. "
            "Seviyeler eşitlenmiştir; sadece tonu/dinamiği dinle. İpucu: oyunda aynı sahneyi "
            "(ör. adım sesi) tekrar tekrar dinle."))
        self.result.setVisible(False)
        self.ans_row.setVisible(True)
        self.listen_row.setVisible(True)
        for b in self.src_buttons.values():
            b.setEnabled(True)
        self._update()
        self.listen("X")

    def listen(self, letter: str) -> None:
        if self.session.done:
            return
        self.playing = letter
        for L, b in self.src_buttons.items():
            b.setChecked(L == letter)
        if self.player(self.session.source_of(letter)) is False:
            self.gecersiz = True
            self.now_lbl.setText(_("⚠ Ses değiştirilemedi: bu test geçersiz sayılacak"))
            return
        self.now_lbl.setText(_("Şu an {harf} çalıyor").format(harf=letter))

    def choose(self, letter: str) -> None:
        if self.session.done:
            return
        self.session.answer(letter)
        if self.session.done:
            self._show_result()
        else:
            self._update()
            self.listen("X")

    def _update(self) -> None:
        s = self.session
        self.round_lbl.setText(_("Tur {n} / {toplam}").format(n=min(s.index + 1, s.rounds),
                                                              toplam=s.rounds))
        self.dots.setTextFormat(Qt.TextFormat.PlainText)
        self.dots.setText("  ".join("●" if i < s.index else "○" for i in range(s.rounds)))

    def _show_result(self) -> None:
        s = self.session
        k, n = s.score, s.rounds
        self._update()
        self.dots.setTextFormat(Qt.TextFormat.RichText)
        self.dots.setText("&nbsp;&nbsp;".join(
            f'<span style="color:{"#22A559" if a == x else "#D64545"}; font-size:14pt">●</span>'
            for a, x in zip(s.answers, s.xs)))
        self.round_lbl.setText(_("Sonuç: {k}/{n} doğru").format(n=n, k=k))
        self.ans_row.setVisible(False)
        self.listen_row.setVisible(False)
        for b in self.src_buttons.values():
            b.setChecked(False)
        self.now_lbl.setText("")
        reveal = (f"A = {self.labels[s.mapping['A']]}, B = {self.labels[s.mapping['B']]}")
        marks = " ".join(f'<span style="color:{"#22A559" if a == x else "#D64545"}">'
                         f'{"✓" if a == x else "✗"}</span>' for a, x in zip(s.answers, s.xs))
        if self.gecersiz:            # what was heard is unknown: a p-value would be made up
            self.result.setText('<p style="font-size:13pt"><b>' + html.escape(_(
                "Test geçersiz: en az bir turda ses değiştirilemedi (servise ulaşılamadı).")) +
                f"</b></p><p>{reveal}</p>")
        else:
            self.result.setText(f'<p style="font-size:13pt"><b>{abx_verdict(k, n)}</b></p>'
                                "<p>" + _("p = {p:.3f} (tek yönlü binom) · turlar: {isaretler}")
                                .format(p=s.p_value(), isaretler=marks) + "</p>"
                                f"<p>{reveal}</p>")
        self.result.setVisible(True)

    def reject(self) -> None:
        # Esc / reject() goes through QDialog.done(), not closeEvent: without this the page
        # never restores the router mode (persisted in mod.json) and "Kör test" stays dead
        self.finishedTest.emit()
        super().reject()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.finishedTest.emit()
        super().closeEvent(e)


# =========================================================================
# listening test guide ("Test rehberi"); sources: docs/NOTES-test-rehberi.md
# =========================================================================
CS2_COMMANDS = """\
// Kulak Kesil - CS2 ses testi (cevrimdisi, botlu sunucu)
sv_cheats 1
mp_warmup_end
mp_limitteams 0
mp_autoteambalance 0
mp_freezetime 0
mp_roundtime 60
mp_roundtime_defuse 60
mp_ignore_round_win_conditions 1
mp_respawn_on_death_ct 1
mp_respawn_on_death_t 1
mp_buy_anywhere 1
mp_buytime 9999
mp_startmoney 16000
sv_infinite_ammo 1
bot_kick
bot_add_t
bot_dont_shoot 1
bot_stop 1
cl_showpos 1
mp_restartgame 1"""

# step = (title, what to do, what to listen for)
TEST_GUIDES: dict[str, dict] = {
    "cs2": {
        "baslik": _("CS2: adım sesi, yön ve silah sertliği"),
        "giris": _("Çevrimdışı botlu sunucuda, hep aynı sahneyi dinleyerek CS2 profilini "
                   "Düz ile karşılaştır. Bir test ~20 dk."),
        "komutlar": CS2_COMMANDS,
        "adimlar": [
            (_("Hazırlık"),
             _("Kulak Kesil → Durum: mod “Sabit: CS2”. CS2 ses ayarları: EQ Profili "
               "Natural, çıkış Kulaklık, sanal surround KAPALI, L/R Isolation %50, "
               "Perspective Correction açık, ana ses %80'de sabit."),
             _("Test boyunca kulaklık tekerine ve oyun sesine dokunma; seviye eşitleme "
               "ancak böyle adil olur. Kulaklığı her zamanki gibi oturt.")),
            (_("Sunucuyu kur"),
             _("Oyna → Antrenman (botlarla, çevrimdışı) → Mirage. Konsolu aç (Ayarlar → "
               "Oyun → Geliştirici konsolu) ve yukarıdaki komut bloğunu yapıştır."),
             _("Bir T botu donmuş halde bekler (bot_stop 1), ateş etmez. Sol üstteki "
               "cl_showpos koordinatları mesafe ölçmek için: 1 birim ≈ 2,5 cm.")),
            (_("Botu yerleştir ve yürüt"),
             _("Botu koymak istediğin yere nişan al: bot_place. Sonra noclip, bot_stop 0, "
               "bot_mimic 1 yaz ve olduğun yerde ileri-geri yürü: bot senin hareketini "
               "kopyalar. Bitince bot_mimic 0; bot_stop 1."),
             _("Önce kontrol et: bot yürüyor ve sen sessiz misin? Kendi adımını da "
               "duyuyorsan noclip'i kapat/aç ya da bot_stop 0 ile botu serbest bırak.")),
            (_("Yön (5 m ≈ 200 birim)"),
             _("Botu önüne, arkana, soluna, sağına ve çaprazlarına 5 m koy. Gözünü kapat, "
               "yürüt, eliyle yönünü göster."),
             _("Sol/sağ anında net mi? Ön–arka karışıyor mu? Yön Düz'e göre kötüleşmemeli; "
               "bağlı kompresör sol-sağ dengesini korur.")),
            (_("Mesafe (5 / 15 / 30 m ≈ 200 / 600 / 1200 birim)"),
             _("Mid gibi açık bir hatta botu 5, 15, 30 m'ye koy; her mesafede aynı "
               "yürüyüşü dinle."),
             _("30 m'de adım hâlâ seçiliyor mu? Mesafe hissi korunuyor mu (uzak ses "
               "uzak gelmeli; kompresör hepsini aynı seviyeye çekmemeli)?")),
            (_("Dikey (üst / alt)"),
             _("Nuke'u yükle (map de_nuke, komut bloğunu tekrar yapıştır): sen B'de "
               "(alt kat), bot tam üstünde A'da (üst kat); sonra yer değiştir."),
             _("Ses yukarıdan mı aşağıdan mı geliyor ayırt edebiliyor musun? 5–8 kHz "
               "(üst raf) yön/yükseklik ipuçlarını taşır.")),
            (_("Duvar arkası"),
             _("Botu bir duvarın/kapının arkasına koy (Mirage: connector, window), yürüt."),
             _("Boğuk ama yönü belli olmalı. 2–4 kHz yükseltmesi duvar arkası adımları "
               "öne çıkarır.")),
            (_("Silah sertliği ve pompalama"),
             _("Bot 10 m'de yürürken AK ile duvara tam şarjör tara (sonsuz mermi açık). "
               "give weapon_flashbang / give weapon_hegrenade ile flaş ve el bombası da at."),
             _("Silah kulağı acıtıyor mu? Ateş kesilince adım ne kadar hızlı geri geliyor "
               "(bırakma 120 ms)? Ortam “nefes alıyor” (pompalama) mu?")),
            (_("Karşılaştır: Sabit CS2 ↔ Kapalı (Düz)"),
             _("Aynı sahnede Durum'dan modu “Sabit: CS2” ile “Kapalı” arasında değiştir "
               "(Kapalı = seviye eşitli Düz çıkış)."),
             _("Seviye eşit; yalnız netlik, yön ve silah/adım dengesini karşılaştır.")),
            (_("Kör test (ABX)"),
             _("Ekolayzır → CS2 → Kör test. 8 tur; her turda X'in A mı B mi olduğunu seç."),
             _("8'de 7 ve üstü = farkı gerçekten duyuyorsun. 4–5 = fark yok/çok küçük: "
               "daha hafif bir ayar yetebilir.")),
            (_("Ayar (kulakla)"),
             _("Önce telafi ve eşik, sonra 3 kHz bandı, sonra 120 Hz rafı. Her değişiklikte "
               "aynı sahneyi tekrar dinle; Kör test ile doğrula."),
             _("Bir seferde tek şeyi değiştir. Yorulunca ara ver: kulak 30 dk sonra "
               "yanılır.")),
        ],
    },
    "film": {
        "baslik": _("Film: diyalog netliği ve gece modu"),
        "giris": _("Bir diyalog ağırlıklı sahne ve bir patlama sahnesiyle, ses düğmesine "
                   "dokunmadan dinle."),
        "komutlar": "",
        "adimlar": [
            (_("Hazırlık"),
             _("Durum: “Sabit: Film”. Altyazıyı kapat. Sesi normal diyalog seviyesine "
               "ayarla ve test boyunca dokunma."),
             _("Amaç: sesi açıp kısmadan hem fısıltıyı hem patlamayı rahat dinlemek.")),
            (_("Diyalog sahnesi"),
             _("Diyaloğu zor anlaşılan bir sahne seç (ör. Nolan filmleri, özellikle Tenet, "
               "bu yüzden eleştirildi) ya da fısıltılı bir dizi sahnesi."),
             _("Altyazısız anlıyor musun? “s/ş” sesleri sert/cızırtılı mı (sibilans)?")),
            (_("Patlama / aksiyon sahnesi"),
             _("Ardından yüksek bir aksiyon sahnesi aç (savaş, araba kovalamaca)."),
             _("Elin ses düğmesine gidiyor mu? Patlamadan hemen sonraki diyalog kayboluyor "
               "mu (bırakma 400 ms)? Müzik “nefes alıyor” mu?")),
            (_("Karşılaştır ve Kör test"),
             _("Durum'da Sabit: Film ↔ Kapalı; sonra Ekolayzır → Film → Kör test."),
             _("Gece modu doğal mı yoksa “TV sesi” gibi ezik mi? Ezikse oranı düşür.")),
        ],
    },
    "muzik": {
        "baslik": _("Müzik: ton sadakati"),
        "giris": _("İyi kaydedilmiş, iyi bildiğin parçalarla dinle; dinamiğe dokunulmaz."),
        "komutlar": "",
        "adimlar": [
            (_("Hazırlık"), _("Durum: “Sabit: Müzik”. Kayıpsız ya da yüksek bit hızlı kaynak seç."),
             _("Ses seviyesini bir kez ayarla; yüksek ses her zaman “daha iyi” gelir.")),
            (_("Vokal / akustik parça"), _("Çok iyi bildiğin vokal ağırlıklı bir parça çal."),
             _("Ses doğal mı, burundan mı? Nefes ve dudak sesleri doğal mı, cızırtılı mı?")),
            (_("Baslı parça"), _("Derin baslı bir parça (elektronik, metal, film müziği)."),
             _("Bas dolgun ama boğucu değil mi? Bas vokali örtüyor mu?")),
            (_("Uzun dinleme"), _("20 dk kesintisiz dinle."),
             _("Kulak yoruluyor ya da tiz batıyorsa tiz rafını 1 dB düşür.")),
            (_("Kör test"), _("Ekolayzır → Müzik → Kör test (Müzik ↔ Düz)."),
             _("HS80 düzeltmesinin farkını gerçekten duyuyor musun?")),
        ],
    },
    "oyun": {
        "baslik": _("Oyun: atmosfer ve etki"),
        "giris": _("Atmosferi güçlü bir oyunda, aynı sahneyi tekrar tekrar dinle."),
        "komutlar": "",
        "adimlar": [
            (_("Hazırlık"), _("Durum: “Sabit: Oyun”. Oyunun kendi ses ayarlarında “kulaklık”, "
                           "dinamik aralık “geniş/home theater” seç."),
             _("Oyun içi sesi sabitle.")),
            (_("Ortam"), _("Yağmur, rüzgâr, şehir gibi ortam seslerinin bol olduğu bir yerde dur."),
             _("Ortam geniş ve derin mi? Uzak sesler seçiliyor mu?")),
            (_("Etki"), _("Patlama, silah, motor sesleri olan bir sahne."),
             _("Bas “tok” mu, “bulanık” mı? Etki var ama kulak yorulmuyor mu?")),
            (_("Kör test"), _("Ekolayzır → Oyun → Kör test (Oyun ↔ Düz)."),
             _("Fark duyuluyor ve hoşuna gidiyor mu? Gerekirse dinamik bloğunu aç.")),
        ],
    },
}


class TestGuideDialog(QDialog):
    """Step-by-step listening test guide with a copyable CS2 command block."""

    def __init__(self, profile: str = "cs2", parent=None):
        super().__init__(parent)
        self.setWindowTitle(_("Test rehberi"))
        self.setMinimumSize(760, 640)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(label(_("Test rehberi"), "pageTitle"), 1)
        seg = QFrame()
        seg.setObjectName("segment")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(2)
        self.tabs: dict[str, QPushButton] = {}
        grp = QButtonGroup(self)
        for prof in ("cs2", "oyun", "muzik", "film"):
            b = QPushButton(PROFILE_LABELS[prof])
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"QPushButton#seg:checked {{ background: {profile_color(prof)}; "
                            f"color: white; }}")
            b.clicked.connect(lambda _c=False, p=prof: self.show_profile(p))
            grp.addButton(b)
            self.tabs[prof] = b
            sl.addWidget(b)
        head.addWidget(seg)
        lay.addLayout(head)
        self.title = label("", "sectionTitle")
        self.intro = label("", "muted", wrap=True)
        lay.addWidget(self.title)
        lay.addWidget(self.intro)

        self.cmd_card = card()
        cl = QVBoxLayout(self.cmd_card)
        cl.setContentsMargins(14, 10, 14, 10)
        ch = QHBoxLayout()
        ch.addWidget(label(_("Konsol komutları"), "sectionTitle"), 1)
        self.copy_btn = button(_("Kopyala"), "primary")
        self.copy_btn.clicked.connect(self.copy_commands)
        ch.addWidget(self.copy_btn)
        cl.addLayout(ch)
        self.cmd_text = QPlainTextEdit()
        self.cmd_text.setReadOnly(True)
        f = QFont("monospace")
        f.setStyleHint(QFont.StyleHint.Monospace)
        self.cmd_text.setFont(f)
        self.cmd_text.setMaximumHeight(150)
        cl.addWidget(self.cmd_text)
        lay.addWidget(self.cmd_card)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.steps_w = QWidget()
        self.steps_w.setObjectName("scrollBody")
        self.steps = QVBoxLayout(self.steps_w)
        self.steps.setContentsMargins(0, 0, 6, 0)
        self.steps.setSpacing(8)
        scroll.setWidget(self.steps_w)
        scroll.viewport().setAutoFillBackground(False)
        self.steps_w.setAutoFillBackground(False)
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        lay.addWidget(scroll, 1)
        foot = QHBoxLayout()
        self.note = label("", "small")
        foot.addWidget(self.note, 1)
        close = button(_("Kapat"))
        close.clicked.connect(self.close)
        foot.addWidget(close)
        lay.addLayout(foot)
        self.show_profile(profile if profile in TEST_GUIDES else "cs2")

    def show_profile(self, prof: str) -> None:
        self.profile = prof
        self.tabs[prof].setChecked(True)
        g = TEST_GUIDES[prof]
        color = profile_color(prof)
        self.title.setText(g["baslik"])
        self.intro.setText(g["giris"])
        self.cmd_card.setVisible(bool(g["komutlar"]))
        self.cmd_text.setPlainText(g["komutlar"])
        self.note.setText("")
        while self.steps.count():
            it = self.steps.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self.step_cards = []
        for i, (title, do, watch) in enumerate(g["adimlar"], 1):
            c = card()
            hl = QHBoxLayout(c)
            hl.setContentsMargins(14, 10, 14, 10)
            hl.setSpacing(12)
            num = QLabel(str(i))
            num.setFixedSize(28, 28)
            num.setAlignment(Qt.AlignmentFlag.AlignCenter)
            num.setStyleSheet(f"QLabel {{ background: {color}; color: white; border-radius: 14px;"
                              f" font-weight: 700; }}")
            hl.addWidget(num, 0, Qt.AlignmentFlag.AlignTop)
            col = QVBoxLayout()
            col.setSpacing(4)
            col.addWidget(label(title, "sectionTitle"))
            col.addWidget(label(do, None, wrap=True))
            w = label(f"<b>{_('Bu adımda neye dikkat et:')}</b> {watch}", None, wrap=True)
            w.setObjectName("watch")
            w.setStyleSheet(f"QLabel#watch {{ background: {_alpha(color, 0.13).name(QColor.NameFormat.HexArgb)};"
                            f" border-left: 3px solid {color}; border-radius: 6px;"
                            f" padding: 6px 10px; }}")
            col.addWidget(w)
            hl.addLayout(col, 1)
            self.steps.addWidget(c)
            self.step_cards.append(c)
        self.steps.addStretch(1)

    def copy_commands(self) -> None:
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(TEST_GUIDES[self.profile]["komutlar"])
        self.note.setText(_("✓ Komutlar panoya kopyalandı — CS2 konsoluna yapıştır (Ctrl+V)"))


# =========================================================================
# the page
# =========================================================================
class EqPage(QWidget):
    """Ekolayzır page (see module docstring)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        self.lsp = eqgen.lsp_available()
        self.saved = eqgen.load_profiles()
        self.work = copy.deepcopy(self.saved)
        self._disk = self._profiles_stamp()          # profiller.toml as loaded (outside edits)
        self._staging_dev = self._cihaz_duzeltme()   # device the gain labels were drawn for
        self.profile = "cs2"
        self.variant_group = ""          # genre variant being edited ("" = the profile itself)
        self._previewed: set[str] = set()  # sinks whose variant slots the editor overrode live
        self._live_pushed: set[str] = set()  # profiles whose unsaved edits reached the sinks
        self.status: dict | None = None
        self._loading = False
        self._ab_prev: dict | None = None
        self._live_error = ""
        self.applier = LiveApplier(self)
        self.applier.done.connect(self._live_done)
        self.live_timer = QTimer(self)
        self.live_timer.setSingleShot(True)
        self.live_timer.setInterval(LIVE_DEBOUNCE_MS)
        self.live_timer.timeout.connect(self._apply_live)
        self.destroyed.connect(self.applier.stop)
        # "Kişisel ton": saved = kisisel.json, draft = sliders; _tone_live = what the
        # preview pushed to each sink (profile -> (group, values)) and must be undone
        self.kisisel = load_kisisel()
        self.tone_saved = self._tone_norm(self._tone_load())
        self.tone_draft = copy.deepcopy(self.tone_saved)
        self.tone_scope = "genel"
        self.tone_target: tuple[str, str] | None = None
        self._tone_live: dict[str, tuple[str, dict]] = {}
        self._tone_error = ""
        self._tone_note = ""
        self.tone_applier = ToneApplier(self)
        self.tone_applier.done.connect(self._tone_done)
        self.destroyed.connect(self.tone_applier.stop)
        self.tone_timer = QTimer(self)
        self.tone_timer.setSingleShot(True)
        self.tone_timer.setInterval(TONE_DEBOUNCE_MS)
        self.tone_timer.timeout.connect(self._tone_preview)
        self._build()
        self.select_profile("cs2")

    # ------------------------------------------------------------- layout
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body_w = QWidget()
        body_w.setObjectName("scrollBody")
        scroll.setWidget(body_w)
        outer.addWidget(scroll)
        body = QVBoxLayout(body_w)
        body.setContentsMargins(32, 28, 32, 22)
        body.setSpacing(14)

        # title row + profile selector
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(_("Ekolayzır"), "pageTitle"))
        titles.addWidget(label(_("Her modun eğrisi: HS80 düzeltmesi (yalnız HS80'de) + profil "
                                 "bantları + dinamik."), "muted"))
        head.addLayout(titles, 1)
        seg = QFrame()
        seg.setObjectName("segment")
        sl = QHBoxLayout(seg)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(2)
        self.prof_group = QButtonGroup(self)
        self.prof_buttons: dict[str, QPushButton] = {}
        for prof in ALL_SINK_PROFILES:
            b = QPushButton(PROFILE_LABELS[prof])
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"QPushButton#seg:checked {{ background: {profile_color(prof)}; "
                            f"color: white; }}")
            b.clicked.connect(lambda _c=False, p=prof: self.select_profile(p))
            self.prof_group.addButton(b)
            self.prof_buttons[prof] = b
            sl.addWidget(b)
        head.addWidget(seg, 0, Qt.AlignmentFlag.AlignBottom)
        body.addLayout(head)

        # profile info card
        info = card()
        il = QHBoxLayout(info)
        il.setContentsMargins(18, 12, 18, 12)
        il.setSpacing(14)
        self.color_bar = QFrame()
        self.color_bar.setFixedWidth(5)
        il.addWidget(self.color_bar)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        self.title_lbl = label("", "sectionTitle")
        self.desc_lbl = label("", "muted", wrap=True)
        self.apps_lbl = label("", "small", wrap=True)
        texts.addWidget(self.title_lbl)
        texts.addWidget(self.desc_lbl)
        texts.addWidget(self.apps_lbl)
        il.addLayout(texts, 1)
        toggles = QVBoxLayout()
        toggles.setSpacing(4)
        self.profile_on = Switch(_("Bu profilde EQ"))
        self.profile_on.setToolTip(_("Kapalıysa bu çıkış düz geçer (düzeltme ve bantlar 0 dB)."))
        self.profile_on.toggled.connect(self._toggle_profile)
        self.corr_on = Switch(_("HS80 düzeltmesi"))
        self.corr_on.setToolTip(_("AutoEq düzeltmesi TÜM profillerin ortak tabanıdır.") + "\n"
                                + tx.DUZELTME_TIP)
        self.corr_on.toggled.connect(self._toggle_correction)
        # CHANGE 7: shown while the active headphone is not an HS80 (the curve stays flat there)
        self.corr_note = label("", "small", wrap=True)
        self.corr_note.setToolTip(tx.DUZELTME_TIP)
        self.corr_note.setMaximumWidth(260)
        self.corr_note.hide()
        toggles.addWidget(self.profile_on)
        toggles.addWidget(self.corr_on)
        toggles.addWidget(self.corr_note)
        self.guide_btn = button(_("Test rehberi"))
        self.guide_btn.setToolTip(_("Bu profili kulakla nasıl test edeceğini adım adım gösterir "
                                    "(CS2 için konsol komutlarıyla)."))
        self.guide_btn.clicked.connect(lambda: self.open_guide())
        il.addWidget(self.guide_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        il.addLayout(toggles)
        body.addWidget(info)

        # graph card
        gcard = card()
        gl = QVBoxLayout(gcard)
        gl.setContentsMargins(14, 12, 14, 10)
        gl.setSpacing(6)
        self.graph = ResponseGraph()
        self.graph.bandChanged.connect(self._graph_band_changed)
        self.graph.bandSelected.connect(self._graph_band_selected)
        self.graph.bandAddRequested.connect(self.add_band)
        self.graph.bandRemoveRequested.connect(self.remove_band)
        self.graph.bandToggleRequested.connect(self._toggle_band)
        self.graph.bandTypeRequested.connect(self._set_band_type)
        # genre variant selector (muzik / oyun): edit a variant's small delta on top
        self.variant_row = QWidget()
        vr = QHBoxLayout(self.variant_row)
        vr.setContentsMargins(0, 0, 0, 4)
        vr.setSpacing(10)
        vr.addWidget(label(_("Tür farkı"), "sectionTitle"))
        self.variant_box = QComboBox()
        self.variant_box.setToolTip(_("Genel = profilin kendisi. Bir tür seçersen o türün küçük "
                                      "farkını profilin üstünde düzenlersin (en çok 4 bant: 1 LSC, "
                                      "2 PK, 1 HSC; ±{limit:g} dB).")
                                    .format(limit=eqgen.VARIANT_GAIN_LIMIT))
        self.variant_box.currentIndexChanged.connect(self._variant_selected)
        vr.addWidget(self.variant_box)
        self.variant_lbl = label("", "small", wrap=True)
        self.variant_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        vr.addWidget(self.variant_lbl, 1)
        gl.addWidget(self.variant_row)
        gl.addWidget(self.graph, 1)
        foot = QHBoxLayout()
        self.gain_lbl = label("", "small")
        self.hint_lbl = hint = label(_("Sürükle: frekans/kazanç · Tekerlek: Q · Shift: yalnız "
                                       "kazanç · Ctrl: ince · Çift tık: bant ekle/aç-kapa · "
                                       "Sağ tık: menü"), "small")
        self._hint_bands = hint.text()
        hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        foot.addWidget(self.gain_lbl, 1)
        foot.addWidget(hint, 1)
        gl.addLayout(foot)
        body.addWidget(gcard, 3)
        body.addWidget(self._build_tone())

        # bands + dynamics side by side
        row = QHBoxLayout()
        row.setSpacing(14)
        self.bands_card = card()
        bl = QVBoxLayout(self.bands_card)
        bl.setContentsMargins(16, 12, 16, 12)
        bh = QHBoxLayout()
        bh.addWidget(label(_("Bantlar"), "sectionTitle"))
        bh.addStretch(1)
        self.add_btn = button(_("＋ Bant ekle"))
        self.add_btn.clicked.connect(lambda: self.add_band(1000.0, 0.0))
        self.del_btn = button(_("Seçiliyi sil"))
        self.del_btn.clicked.connect(lambda: self.remove_band(self.graph.selected))
        bh.addWidget(self.add_btn)
        bh.addWidget(self.del_btn)
        bl.addLayout(bh)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["", _("Açık"), _("Tip"), _("Frekans"), _("Kazanç"),
                                              "Q"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.SelectedClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setItemDelegate(BandDelegate(self))
        self.table.setShowGrid(False)
        self.table.setToolTip(_("Çift tıkla (veya seçip tıkla) ve değeri düzenle"))
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for c, w in ((0, 34), (1, 56)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.table.setColumnWidth(c, w)
        self.table.itemSelectionChanged.connect(self._table_selection)
        bl.addWidget(self.table)
        row.addWidget(self.bands_card, 3)

        self.dyn_card = card()
        dl = QVBoxLayout(self.dyn_card)
        dl.setContentsMargins(16, 12, 16, 12)
        dh = QHBoxLayout()
        dh.addWidget(label(_("Dinamik (kompresör)"), "sectionTitle"))
        dh.addStretch(1)
        self.dyn_on = Switch(_("Açık"))
        self.dyn_on.toggled.connect(self._toggle_dyn)
        dh.addWidget(self.dyn_on)
        dl.addLayout(dh)
        self.dyn_intro = label("", "small", wrap=True)
        dl.addWidget(self.dyn_intro)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        self.dyn_sliders: dict[str, tuple[QSlider, QLabel, QLabel, float, float]] = {}
        r = 0
        for key, name, lo, hi, step, unit, dec in DYN_PARAMS:
            nl = label(name)
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, round((hi - lo) / step))
            s.setSingleStep(1)
            s.setPageStep(max(1, round(s.maximum() / 20)))
            val = label("", "small")
            val.setMinimumWidth(64)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            help_ = label("", "small", wrap=True)
            s.valueChanged.connect(lambda v, k=key: self._dyn_slider(k, v))
            grid.addWidget(nl, r, 0)
            grid.addWidget(s, r, 1)
            grid.addWidget(val, r, 2)
            grid.addWidget(help_, r + 1, 1, 1, 2)
            self.dyn_sliders[key] = (s, val, help_, lo, step)
            r += 2
        grid.setColumnStretch(1, 1)
        dl.addLayout(grid)
        self.dyn_missing = label("", "small", wrap=True)
        dl.addWidget(self.dyn_missing)
        dl.addStretch(1)
        row.addWidget(self.dyn_card, 2)

        self.flat_card = card()
        fl = QVBoxLayout(self.flat_card)
        fl.setContentsMargins(18, 14, 18, 14)
        fl.addWidget(label(_("Düz çıkış"), "sectionTitle"))
        self.flat_lbl = label("", "muted", wrap=True)
        fl.addWidget(self.flat_lbl)
        self.flat_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        row.addWidget(self.flat_card, 3, Qt.AlignmentFlag.AlignTop)
        body.addLayout(row, 2)

        # action bar
        bar = card()
        al = QHBoxLayout(bar)
        al.setContentsMargins(16, 10, 16, 10)
        al.setSpacing(12)
        self.live_on = Switch(_("Canlı dinle"))
        self.live_on.setToolTip(_("Değişiklikler çalışan sanal çıkışlara anında uygulanır "
                                  "(kaydetmeden). Yeniden başlatma gerekmez."))
        self.live_on.toggled.connect(self._toggle_live)
        self.keep_level = Switch(_("Seviyeyi koru"))
        self.keep_level.setChecked(True)
        self.keep_level.setToolTip(_("Açık: düzenlerken ses seviyesi eşitlemesi canlı yeniden "
                                     "hesaplanır (mod değişince ses zıplamaz).\nKapalı: kazanç "
                                     "kademeleri kayıtlı haliyle kalır, boost'u gerçekten "
                                     "duyarsın (kırpılma koruması yine açık)."))
        self.keep_level.toggled.connect(lambda _on: self._changed(structural=False))
        al.addWidget(self.live_on)
        al.addWidget(self.keep_level)
        self.ab_btn = button(_("A/B karşılaştır"))
        self.ab_btn.setCheckable(True)
        self.ab_btn.setToolTip(_("Modu geçici olarak bu profile sabitler; ikinci düğme ile "
                                 "seviye eşitli Düz çıkışla (Kapalı mod) karşılaştır."))
        self.ab_btn.toggled.connect(self._toggle_ab)
        self.ab_swap = button(_("B: Düz'e geç"))
        self.ab_swap.setVisible(False)
        self.ab_swap.clicked.connect(self._ab_switch)
        al.addWidget(self.ab_btn)
        al.addWidget(self.ab_swap)
        self.abx_btn = button(_("Kör test"))
        self.abx_btn.setToolTip(_("Kör ABX testi: bu profil ile Düz'ü (ya da kayıtlı ile "
                                  "düzenlenmiş hali) gerçekten ayırt edebiliyor musun? 8 tur, "
                                  "sonunda puan ve istatistik."))
        self.abx_btn.clicked.connect(lambda: self.start_abx())
        al.addWidget(self.abx_btn)
        self.abx: AbxDialog | None = None
        self._abx_variants: dict[str, str] | None = None
        self.state_lbl = label("", "small")
        self.state_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        al.addWidget(self.state_lbl, 1)
        self.defaults_btn = button(_("Varsayılana dön"))
        self.defaults_btn.clicked.connect(self.reset_defaults)
        self.revert_btn = button(_("Geri al"))
        self.revert_btn.clicked.connect(self.revert)
        self.save_btn = button(_("Kaydet"), "primary")
        self.save_btn.clicked.connect(self.save)
        al.addWidget(self.defaults_btn)
        al.addWidget(self.revert_btn)
        al.addWidget(self.save_btn)
        body.addWidget(bar)

    # ------------------------------------------------------------- state
    @property
    def base_staging(self) -> dict:
        """Gain stages of the SAVED profiles, always fresh: they also depend on the device
        (eqgen.correction_on → kulaklik.duzeltme_acik, i.e. cihaz.json), which changes when
        the default headphone changes while the page is open (a cached copy went stale and
        live edits then wrote the old device's gains to every sink)."""
        return eqgen.gain_staging(self.saved, self.lsp)

    @staticmethod
    def _profiles_stamp() -> tuple[str, int] | None:
        """(path, mtime) of the profiller.toml the page reads (user copy or shipped)."""
        try:
            path = config.config_path(config.PROFILES_FILE)
            return str(path), path.stat().st_mtime_ns
        except OSError:
            return None

    def _reload_if_changed_on_disk(self) -> None:
        """`kulak profiller`, a text editor or `kulak yedek yukle` changed the file: show it
        (only while nothing is being edited here; save() asks otherwise)."""
        stamp = self._profiles_stamp()
        if stamp == self._disk or self.dirty() or self.abx is not None:
            return
        try:
            saved = eqgen.load_profiles()
        except Exception as e:  # noqa: BLE001 - a broken file: keep showing the last good one
            log.warning("profiller.toml yeniden okunamadı: %s", e)
            return
        self.saved, self.work, self._disk = saved, copy.deepcopy(saved), stamp
        self._refresh_all()

    @staticmethod
    def _cihaz_duzeltme() -> bool | None:
        try:
            return kulaklik.duzeltme_acik()
        except Exception:  # noqa: BLE001 - unreadable state must not break the page
            return None

    def staging(self) -> dict:
        return eqgen.gain_staging(self.work, self.lsp)

    def live_staging(self) -> dict | None:
        """None = recompute (Seviyeyi koru); else saved gain stages + anti-clip only."""
        if self.keep_level.isChecked():
            return None
        st = copy.deepcopy(self.base_staging)
        pay = self.work["genel"]["pay_db"]
        for prof in EQ_PROFILES:
            flt = eqgen.effective_filters(self.work, prof)
            # only the edited profile gets the static anti-clip guard: limiter-protected chains
            # (CHANGE 6) are staged above the static worst case on purpose, so recomputing an
            # untouched profile here would wrongly turn it down
            if flt == eqgen.effective_filters(self.saved, prof):
                continue
            if flt:
                # same rule as eqgen.gain_staging: a limiter-protected chain gives its
                # marj_db back (without it the first 0.1 dB nudge dropped the profile ~5 dB)
                need = min(0.0, -(max(eqgen.peak_db(flt, self.work["genel"]["orneklem_hz"]),
                                      0.0) + pay) + st[prof].get("limiter_margin_db", 0.0))
                st[prof]["preamp_db"] = min(st[prof]["preamp_db"], need)
        return st

    def dirty(self) -> bool:
        return self.work != self.saved

    def select_profile(self, prof: str) -> None:
        if self.ab_btn.isChecked():
            self.ab_btn.setChecked(False)
        if prof != self.profile:
            self.variant_group = ""
            self._end_preview()
        self.profile = prof
        self.prof_buttons[prof].setChecked(True)
        self.graph.selected = -1
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._loading = True
        try:
            prof = self.profile
            flat = prof == FLAT_PROFILE
            color = profile_color(prof)
            self.color_bar.setStyleSheet(f"background: {color}; border-radius: 2px;")
            self.live_on.on_color = QColor(color)
            self.keep_level.on_color = QColor(color)
            self.profile_on.on_color = QColor(color)
            self.corr_on.on_color = QColor(color)
            self.dyn_on.on_color = QColor(color)
            st = self.staging()
            if flat:
                d = self.work[FLAT_PROFILE]
                self.title_lbl.setText(_("Düz — seviye eşitli geçiş"))
                self.desc_lbl.setText(d.get("aciklama", ""))
            else:
                p = self.work["profil"][prof]
                odak = p.get("odak") or ui.PROFILE_ODAK.get(prof, "")
                self.title_lbl.setText(f"{PROFILE_LABELS[prof]} — {odak}")
                self.desc_lbl.setText(p.get("aciklama", ""))
                self.profile_on.setChecked(p.get("aktif", True))
                self.corr_on.setChecked(self.work["duzeltme"].get("aktif", True))
            self.profile_on.setVisible(not flat)
            self.corr_on.setVisible(not flat)
            self._sync_device_correction()
            self.bands_card.setVisible(not flat)
            self.hint_lbl.setVisible(not flat)
            self.flat_card.setVisible(flat)
            self.ab_btn.setEnabled(not flat)
            self.abx_btn.setEnabled(not flat)
            fs = st[FLAT_PROFILE]
            self.graph.set_data(self.work, prof, fs["match_db"])
            self._fill_variants()            # before the table: it shows the variant slots
            self._fill_table()
            self._fill_dyn()
            self.flat_lbl.setText(_(
                "HS80 düzeltmesi, EQ ve kompresör YOK. Sesli sohbet (Discord), sistem sesleri "
                "ve Kapalı mod buradan geçer.\n\n"
                "Kazanç: {kazanc:+.1f} dB — dört profilin ortak eşitlenmiş seviyesi"
                " (ek kazanç {ek:+.1f} dB). Böylece "
                "Discord ile oyun arasındaki denge bozulmaz ve A/B karşılaştırması adil olur.\n\n"
                "Bu çıkış salt okunur; ek kazancı profiller.toml'daki [duz] bölümünden "
                "değiştirebilirsin.").format(kazanc=fs["match_db"],
                                             ek=self.work[FLAT_PROFILE]["ek_kazanc_db"]))
            self._update_apps()
        finally:
            self._loading = False
        self._update_info()
        self._tone_refresh()

    # ------------------------------------------------------------- variants
    def in_variant(self) -> bool:
        return self.profile in VARIANT_PROFILES and bool(self.variant_group)

    def _bands(self) -> list[dict]:
        """Editable bands: the variant's 4 slots, or the profile's own bands."""
        if self.profile == FLAT_PROFILE:
            return []
        if self.in_variant() and self.graph.variant_bands is not None:
            return self.graph.variant_bands
        return self.work["profil"][self.profile]["filtreler"]

    def _fill_variants(self) -> None:
        """Variant combo + the graph's variant layer for the current profile."""
        prof = self.profile
        show = prof in VARIANT_PROFILES
        self.variant_row.setVisible(show)
        if not show:
            self.variant_group = ""
            self.hint_lbl.setText(self._hint_bands)
            return
        if self.variant_group not in variant_groups(prof):
            self.variant_group = ""
        self.variant_box.blockSignals(True)
        self.variant_box.clear()
        self.variant_box.addItem(_("Genel (türsüz)"), "")
        for g in variant_groups(prof):
            self.variant_box.addItem(GROUP_LABELS.get(g, g), g)
        self.variant_box.setCurrentIndex(max(0, self.variant_box.findData(self.variant_group)))
        self.variant_box.blockSignals(False)
        if self.variant_group:
            # set directly (not set_variant): a refill must keep the selected band
            self.graph.variant_bands = eqgen.variant_slots(self.work, prof, self.variant_group)
            self.graph.variant_name = self.variant_group
        else:
            self.graph.variant_bands = None
            self.graph.variant_name = ""
        self.add_btn.setVisible(not self.variant_group)
        self.del_btn.setVisible(not self.variant_group)
        self.hint_lbl.setText(_("Sürükle: frekans/kazanç · Tekerlek: Q · Çift tık: aç/kapa · "
                                "Sağ tık: 0 dB'e sıfırla") if self.variant_group
                              else self._hint_bands)
        self._update_variant_lbl()

    def _router_variant(self, prof: str) -> dict:
        return dict((((self.status or {}).get("variants") or {}).get(prof)) or {})

    def _update_variant_lbl(self) -> None:
        prof = self.profile
        if prof not in VARIANT_PROFILES:
            return
        rv = self._router_variant(prof)
        now = ""
        if rv:
            g = rv.get("group") or ""
            tur = GROUP_LABELS[g] if g in GROUP_LABELS else (rv.get("label") or _("Genel"))
            if rv.get("for"):
                tur += f" — {rv['for']}"
            now = (_("Şu an: {tur} (sabit)") if rv.get("mode") == "fixed"
                   else _("Şu an: {tur} (otomatik)")).format(tur=tur)
        if self.variant_group:
            t = (self.work["profil"][prof].get("tur") or {}).get(self.variant_group) or {}
            parts = [t.get("aciklama") or GROUP_LABELS.get(self.variant_group, self.variant_group),
                     _("yalnız tür farkı düzenlenir, kesik çizgi = Genel")]
            if self.live_on.isChecked():
                parts.append(_("canlı önizleme: şarkı/oyun değişince otomatik tür geri gelir"))
            text = " · ".join(parts)
            self.variant_lbl.setText(text + (f"\n{now}" if now else ""))
        else:
            self.variant_lbl.setText(now or _("Profilin kendisi düzenleniyor; türe özel küçük "
                                              "farklar için yandan bir tür seç."))

    def _variant_selected(self, idx: int) -> None:
        g = self.variant_box.itemData(idx) or ""
        if g == self.variant_group:
            return
        self.variant_group = g
        self.graph.selected = -1
        self._refresh_all()
        if self.live_on.isChecked():
            if g:
                self.live_timer.start()
            else:
                self._end_preview()

    def _store_variant(self) -> None:
        """Write the edited slots back into work[profil][p][tur][group] (non-zero ones)."""
        g, slots = self.variant_group, self.graph.variant_bands or []
        lim = eqgen.VARIANT_GAIN_LIMIT
        for b in slots:
            b["kazanc"] = round(min(max(float(b["kazanc"]), -lim), lim), 1)
        tur = self.work["profil"][self.profile].setdefault("tur", {})
        old = tur.get(g) or {}
        flt = [dict(b) for b in slots if abs(b["kazanc"]) >= 0.05]
        if flt or old:
            entry = {k: v for k, v in old.items() if k != "filtreler"}
            entry.setdefault("aciklama", GROUP_LABELS.get(g, g))
            entry.setdefault("odak", "")
            entry["filtreler"] = flt
            tur[g] = entry

    def _live_variants(self) -> dict[str, str] | None:
        """Variant slots for a live job: the edited variant, or (after a preview)
        the router's own group again; None = leave the running slots alone."""
        p = self.profile
        if self.in_variant():
            self._previewed.add(p)
            return {p: self.variant_group}
        if p in self._previewed:
            self._previewed.discard(p)
            return {p: self._router_variant(p).get("group") or ""}
        return None

    def _end_preview(self) -> None:
        """A variant preview ends: the running sink gets the router's group back."""
        p = self.profile
        if p not in self._previewed or self.in_variant():
            return
        live = self.live_on.isChecked()
        self.applier.submit(p, self.work if live else self.saved,
                            self.live_staging() if live else None, self._live_variants())

    def _update_info(self) -> None:
        st = self.staging()[self.profile]
        if self.profile == FLAT_PROFILE:
            self.gain_lbl.setText(_("Seviye {db:+.1f} dB · en kötü tepe {tepe:+.1f} dBFS")
                                  .format(db=st["match_db"], tepe=st["worst_peak_dbfs"]))
        else:
            parts = [_("Ön kazanç {db:+.1f} dB").format(db=st["preamp_db"]),
                     _("eşitleme {db:+.1f} dB").format(db=st["match_db"])]
            if st["makeup_db"]:
                parts.append(_("telafi {db:+.1f} dB").format(db=st["makeup_db"]))
            # CHANGE 6 (D1): with the limiter the real output peak is out_peak_dbfs
            peak = st.get("out_peak_dbfs", st["worst_peak_dbfs"])
            parts.append(_("en kötü tepe {tepe:+.1f} dBFS").format(tepe=peak))
            self.gain_lbl.setText(" · ".join(parts))
        d = self.dirty()
        self.save_btn.setEnabled(d)
        self.revert_btn.setEnabled(d)
        if self._live_error:
            self.state_lbl.setText("⚠ " + self._live_error)
        elif d:
            restart = eqgen.needs_restart(self.saved, self.work)
            self.state_lbl.setText(_("● Kaydedilmemiş değişiklik (kaydedince servis yeniden "
                                     "başlar)") if restart else _("● Kaydedilmemiş değişiklik"))
        else:
            self.state_lbl.setText(_("Kayıtlı"))

    def _fill_table(self) -> None:
        bands = self._bands()
        fixed_types = self.in_variant()
        color = profile_color(self.profile)
        self.table.blockSignals(True)
        for r in range(self.table.rowCount()):     # old switches: hide now, Qt deletes later
            w = self.table.cellWidget(r, COL_ON)
            if w is not None:
                w.hide()
                self.table.removeCellWidget(r, COL_ON)
        self.table.setRowCount(len(bands))
        for i, b in enumerate(bands):
            num = QTableWidgetItem("")
            num.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(i, COL_NUM, num)
            on = Switch()
            on.on_color = QColor(color)
            on.setChecked(b.get("aktif", True))
            on.setToolTip(_("Bandı aç/kapat (kapalı = 0 dB)"))
            on.toggled.connect(lambda v, i=i: self._table_edit(i, COL_ON, v))
            w = QWidget()
            wl = QHBoxLayout(w)
            wl.setContentsMargins(8, 0, 0, 0)
            wl.addWidget(on)
            self.table.setCellWidget(i, COL_ON, w)
            for col, v in ((COL_TYPE, b["tip"]), (COL_FREQ, b["fc"]), (COL_GAIN, b["kazanc"]),
                           (COL_Q, b["q"])):
                it = QTableWidgetItem(cell_text(col, v))
                it.setData(VALUE_ROLE, v)
                it.setTextAlignment(Qt.AlignmentFlag.AlignVCenter |
                                    (Qt.AlignmentFlag.AlignLeft if col == COL_TYPE
                                     else Qt.AlignmentFlag.AlignRight))
                if not b.get("aktif", True):
                    it.setForeground(self.palette().color(QPalette.ColorRole.PlaceholderText))
                if fixed_types and col == COL_TYPE:     # variant slot types are fixed
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    it.setToolTip(_("Tür yuvalarının tipi sabittir (v1 LSC, v2–v3 PK, v4 HSC)"))
                self.table.setItem(i, col, it)
            self.table.setRowHeight(i, 34)
        self.table.blockSignals(False)
        self.table.setMinimumHeight(min(34 * max(len(bands), 3) + 36, 340))
        sel = self.graph.selected
        self.del_btn.setEnabled(0 <= sel < len(bands))
        if 0 <= sel < len(bands):
            self.table.blockSignals(True)
            self.table.selectRow(sel)
            self.table.blockSignals(False)

    def _sync_row(self, i: int) -> None:
        """Push band i values into its table cells without re-emitting."""
        b = self._bands()[i]
        self.table.blockSignals(True)
        for col, key in ((COL_FREQ, "fc"), (COL_GAIN, "kazanc"), (COL_Q, "q")):
            it = self.table.item(i, col)
            if it is not None:
                it.setData(VALUE_ROLE, b[key])
                it.setText(cell_text(col, b[key]))
        self.table.blockSignals(False)

    def _fill_dyn(self) -> None:
        prof = self.profile
        d = None if prof == FLAT_PROFILE else self.work["profil"][prof].get("dinamik")
        self.dyn_card.setVisible(d is not None)
        if d is None:
            return
        helps = DYN_HELP.get(prof, DYN_HELP["_"])
        self.dyn_intro.setText(helps["_"])
        self.dyn_on.setChecked(d["aktif"])
        for key, name, lo, hi, step, unit, dec in DYN_PARAMS:
            s, val, help_, lo_, step_ = self.dyn_sliders[key]
            s.blockSignals(True)
            s.setValue(round((min(max(d[key], lo), hi) - lo) / step))
            s.blockSignals(False)
            val.setText(self._dyn_text(key, d[key]))
            help_.setText(helps.get(key, DYN_HELP["_"][key]))
            s.setEnabled(d["aktif"])
        lat = _("lookahead 0 ms — gecikme yok") if (self.work["profil"][prof].get("sifir_gecikme")
                                                    or d["ileri_bakis_ms"] == 0) else \
            _("lookahead {ms:g} ms (gecikme ekler!)").format(ms=d["ileri_bakis_ms"])
        self.dyn_missing.setText(
            ("" if self.lsp else _("⚠ lsp-plugins-lv2 kurulu değil: kompresör üretilmez.") + "\n")
            + lat)

    @staticmethod
    def _dyn_text(key: str, v: float) -> str:
        for k, _n, _lo, _hi, _st, unit, dec in DYN_PARAMS:
            if k == key:
                if unit == ":1":
                    return f"{v:.{dec}f}:1"
                sign = "+" if key == "telafi_db" and v > 0 else ""
                return f"{sign}{v:.{dec}f} {unit}"
        return str(v)

    # ------------------------------------------------------------- edits
    def _changed(self, structural: bool = False, refill: bool = False) -> None:
        if self._loading:
            return
        if self.in_variant():
            self._store_variant()
        if refill or structural:
            self._refresh_all()
        else:
            self.graph.update()
            self._update_info()
        if self.live_on.isChecked():
            self.live_timer.start()

    def _graph_band_changed(self, i: int) -> None:
        self._sync_row(i)
        self._changed()

    def _graph_band_selected(self, i: int) -> None:
        self.table.blockSignals(True)
        if i >= 0:
            self.table.selectRow(i)
        else:
            self.table.clearSelection()
        self.table.blockSignals(False)
        self.del_btn.setEnabled(i >= 0)

    def _table_selection(self) -> None:
        rows = {ix.row() for ix in self.table.selectedIndexes()}
        self.graph.selected = min(rows) if rows else -1
        self.del_btn.setEnabled(bool(rows))
        self.graph.update()

    def _table_edit(self, i: int, col: int, value) -> None:
        if self._loading or self.profile == FLAT_PROFILE:
            return
        bands = self._bands()
        if not 0 <= i < len(bands):
            return
        b = bands[i]
        if col == COL_TYPE:
            self._set_band_type(i, value)
            return
        if col == COL_ON:
            b["aktif"] = bool(value)
            self.graph.selected = i
            self._changed(refill=True)
            return
        key = EDIT_SPECS[col][0]
        if b[key] == float(value):
            return
        b[key] = float(value)
        self.graph.selected = i
        self._sync_row(i)
        self._changed()

    def _toggle_band(self, i: int) -> None:
        b = self._bands()[i]
        b["aktif"] = not b.get("aktif", True)
        self._changed(refill=True)

    def _set_band_type(self, i: int, tip: str) -> None:
        if self._loading or self.in_variant():
            return
        b = self.work["profil"][self.profile]["filtreler"][i]
        if b["tip"] != tip:
            b["tip"] = tip
            if tip != "PK":
                b["q"] = min(b["q"], 2.0)
            self._changed(structural=True)

    def add_band(self, fc: float = 1000.0, gain: float = 0.0) -> None:
        if self.profile == FLAT_PROFILE or self.in_variant():
            return
        bands = self.work["profil"][self.profile]["filtreler"]
        bands.append({"tip": "PK", "fc": float(fc), "kazanc": float(gain), "q": 1.0, "aktif": True})
        self.graph.selected = len(bands) - 1
        self._changed(structural=True)

    def remove_band(self, i: int) -> None:
        if self.profile == FLAT_PROFILE or self.in_variant():
            return
        bands = self.work["profil"][self.profile]["filtreler"]
        if 0 <= i < len(bands):
            del bands[i]
            self.graph.selected = -1
            self._changed(structural=True)

    def _toggle_profile(self, on: bool) -> None:
        if self._loading or self.profile == FLAT_PROFILE:
            return
        self.work["profil"][self.profile]["aktif"] = bool(on)
        self._changed()

    def _toggle_correction(self, on: bool) -> None:
        if self._loading:
            return
        self.work["duzeltme"]["aktif"] = bool(on)
        self._changed()

    def _toggle_dyn(self, on: bool) -> None:
        if self._loading or self.profile == FLAT_PROFILE:
            return
        d = self.work["profil"][self.profile].get("dinamik")
        if d is not None:
            d["aktif"] = bool(on)
            for key in self.dyn_sliders:
                self.dyn_sliders[key][0].setEnabled(on)
            self._changed()

    def _dyn_slider(self, key: str, v: int) -> None:
        if self._loading:
            return
        s, val, _h, lo, step = self.dyn_sliders[key]
        d = self.work["profil"][self.profile]["dinamik"]
        d[key] = round(lo + v * step, 3)
        val.setText(self._dyn_text(key, d[key]))
        self._changed()

    # ------------------------------------------------------------- live
    def _toggle_live(self, on: bool) -> None:
        self._live_error = ""
        if on:
            self.live_timer.start()
        else:
            # preview off: running sinks back to the saved curve (+ the router's group)
            self._back_to_saved()
        self._update_variant_lbl()
        self._update_info()

    def _apply_live(self) -> None:
        if not self.live_on.isChecked():
            return
        if eqgen.needs_restart(self.saved, self.work):
            self._live_error = _("Bant sayısı/tipi değişti: canlı uygulanamaz, Kaydet'e bas")
            self._update_info()
            return
        self.applier.submit(self.profile if self.profile != FLAT_PROFILE else "cs2",
                            self.work, self.live_staging(), self._live_variants())
        self._live_pushed.add(self.profile)

    def _back_to_saved(self) -> None:
        """Sinks that got unsaved live edits go back to the saved curve (live preview
        switched off, Geri al, page left): the UI says "Kayıtlı", the sound must match."""
        pushed, self._live_pushed = self._live_pushed, set()
        items = []
        for p in sorted(pushed | ({self.profile} & self._previewed)):
            prof = p if p != FLAT_PROFILE else "cs2"
            var = None
            if p in VARIANT_PROFILES:
                self._previewed.discard(p)
                var = {p: self._router_variant(p).get("group") or ""}
            items.append((prof, var))
        if items:
            self.applier.submit(items, self.saved, None)

    def _live_done(self, ok: bool, msg: str) -> None:
        if ok:
            self._live_error = ""
        elif msg:
            self._live_error = _("Canlı uygulanamadı: sanal çıkışlar çalışıyor mu? {hata}").format(
                hata=msg)
        else:
            self._live_error = _("Canlı uygulanamadı: sanal çıkışlar çalışıyor mu?")
        self._update_info()
        if self.tone_dirty() and self._tone_live:
            # set_live rewrote the sink with the saved tone: push the preview again
            self._tone_live.pop(self.profile, None)
            self.tone_timer.start()

    # ------------------------------------------------------------- save / revert
    def save(self) -> bool:
        try:
            eqgen.normalize_profiles(self.work)
        except eqgen.ProfileError as e:
            QMessageBox.warning(self, _("Kaydedilemedi"), str(e))
            return False
        if self._profiles_stamp() != self._disk:
            ans = QMessageBox.question(
                self, _("Dosya başka yerden değişti"),
                _("profiller.toml bu sayfa açıldıktan sonra başka bir yerden değiştirildi "
                  "(el ile, kulak komutu ya da yedekten geri yükleme). Kaydedersen o "
                  "değişiklikler kaybolur.\n\nYine de kaydedeyim mi?"))
            if ans != QMessageBox.StandardButton.Yes:
                return False
        restart = eqgen.needs_restart(self.saved, self.work)
        if restart:
            ans = QMessageBox.question(
                self, _("Servis yeniden başlatılsın mı?"),
                _("Bant sayısı, bant tipi veya kompresör bloğu değişti. Bunun uygulanması için "
                  "sanal çıkışlar (oto-eq-sinks) yeniden başlatılmalı; çalan sesler ~1 sn "
                  "kesilir.\n\nKaydedip yeniden başlatayım mı?"))
            if ans != QMessageBox.StandardButton.Yes:
                return False
        try:
            eqgen.save_profiles(self.work, config.CONFIG_DIR / config.PROFILES_FILE)
            eqgen.write_conf(config.SINKS_CONF, self.work)
        except (OSError, eqgen.ProfileError) as e:
            QMessageBox.warning(self, _("Kaydedilemedi"), str(e))
            return False
        before = self.saved
        self.saved = copy.deepcopy(self.work)
        self._disk = self._profiles_stamp()
        self._live_error = ""
        if restart:
            self._live_pushed.clear()
            ok, msg = restart_sinks_service()
            if not ok:
                self._live_error = _("Servis yeniden başlatılamadı: {hata}").format(hata=msg)
        else:
            # make the running sinks match what was saved (cheap, no restart): EVERY edited
            # profile (set_live writes bands/dynamics only for the profile it is given); a
            # variant sink gets the router's current group with the saved values
            prof_b, prof_a = before.get("profil", {}), self.saved.get("profil", {})
            others = {k: v for k, v in before.items() if k != "profil"} != \
                {k: v for k, v in self.saved.items() if k != "profil"}
            changed = {p for p in EQ_PROFILES if prof_b.get(p) != prof_a.get(p)}
            changed |= self._live_pushed - {FLAT_PROFILE}
            if others or not changed:           # genel / Düz: the gain stages of all sinks
                changed.add(self.profile if self.profile != FLAT_PROFILE else "cs2")
            self._live_pushed.clear()
            # the shared level-match target moves with ANY profile's edit, and set_live writes
            # a variant sink's cikis_ only when it is listed: list muzik/oyun always
            changed |= set(VARIANT_PROFILES)
            items = []
            for p in sorted(changed):
                var = None
                if p in VARIANT_PROFILES:
                    self._previewed.discard(p)
                    var = {p: self._router_variant(p).get("group") or ""}
                items.append((p, var))
            self.applier.submit(items, self.saved, None)
        self._refresh_all()
        return True

    def revert(self) -> None:
        self.work = copy.deepcopy(self.saved)
        self._live_error = ""
        if self.live_on.isChecked():
            self._live_pushed.add(self.profile)
        self._back_to_saved()
        self._refresh_all()

    def reset_defaults(self) -> None:
        ans = QMessageBox.question(
            self, _("Varsayılana dön"),
            _("Tüm profiller (CS2, Oyun, Müzik, Film, Düz) Kulak Kesil'in varsayılan eğrilerine "
              "döndürülsün mü? Kaydet'e basana kadar dosyaya yazılmaz."))
        if ans != QMessageBox.StandardButton.Yes:
            return
        if self.in_variant():               # else _changed() stores the old slots into it
            self.variant_group = ""
            self.graph.variant_bands = None
            self._end_preview()
        self.work = eqgen.load_profiles(config.DEFAULTS_DIR / config.PROFILES_FILE)
        self.graph.selected = -1
        self._changed(refill=True)

    # ------------------------------------------------------------- A/B
    def _request(self, obj: dict) -> dict | None:
        try:
            r = ipc_request(obj)
        except OSError as e:
            self.state_lbl.setText(_("⚠ Servise ulaşılamadı: {hata}").format(hata=e))
            return None
        if not r.get("ok", False):
            self.state_lbl.setText("⚠ " + str(r.get("error", _("hata"))))
            return None
        return r

    def _toggle_ab(self, on: bool) -> None:
        if on:
            st = self.status or self._request({"cmd": "status"})
            if st is None:
                self.ab_btn.blockSignals(True)
                self.ab_btn.setChecked(False)
                self.ab_btn.blockSignals(False)
                return
            prev = {"mode": st.get("mode", "auto"), "profile": st.get("fixed_profile")}
            if self._request({"cmd": "set_mode", "mode": "fixed", "profile": self.profile}) is None:
                self.ab_btn.blockSignals(True)       # state_lbl already shows the error
                self.ab_btn.setChecked(False)
                self.ab_btn.blockSignals(False)
                return
            self._ab_prev = prev
            self._ab_side = "A"
            self.ab_btn.setText(_("A/B bitir"))
            self.ab_swap.setText(_("B: Düz'e geç"))
            self.ab_swap.setVisible(True)
            self.state_lbl.setText(_("A: {profil} dinleniyor").format(
                profil=PROFILE_LABELS[self.profile]))
        else:
            prev = self._ab_prev
            self._ab_prev = None
            self.ab_swap.setVisible(False)
            self.ab_btn.setText(_("A/B karşılaştır"))
            if prev is not None:
                req = {"cmd": "set_mode", "mode": prev["mode"]}
                if prev["mode"] == "fixed" and prev.get("profile"):
                    req["profile"] = prev["profile"]
                self._request(req)
            self._update_info()

    def _ab_switch(self) -> None:
        if self._ab_prev is None:
            return
        if self._ab_side == "A":
            if self._request({"cmd": "set_mode", "mode": "off"}) is None:
                return                               # still A; state_lbl shows the error
            self._ab_side = "B"
            self.ab_swap.setText(_("A: {profil}'e geç").format(profil=PROFILE_LABELS[self.profile]))
            self.state_lbl.setText(_("B: Düz (seviye eşitli, EQ yok) dinleniyor"))
        else:
            if self._request({"cmd": "set_mode", "mode": "fixed", "profile": self.profile}) is None:
                return
            self._ab_side = "A"
            self.ab_swap.setText(_("B: Düz'e geç"))
            self.state_lbl.setText(_("A: {profil} dinleniyor").format(
                profil=PROFILE_LABELS[self.profile]))

    # ------------------------------------------------------------- test guide
    def open_guide(self) -> TestGuideDialog:
        dlg = TestGuideDialog(self.profile if self.profile in TEST_GUIDES else "cs2", self)
        dlg.show()
        self.guide = dlg
        return dlg

    # ------------------------------------------------------------- blind ABX
    def start_abx(self, kind: str | None = None, rng=None, rounds: int = 8) -> AbxDialog | None:
        """kind: 'duz' (profile vs Düz) or 'kayit' (saved vs edited; default when dirty)."""
        if self.profile == FLAT_PROFILE or self.abx is not None:
            return None
        ab_prev = self._ab_prev           # A/B running: self.status is its temporary fixed mode
        if self.ab_btn.isChecked():
            self.ab_btn.setChecked(False)
        kind = kind or ("kayit" if self.dirty() and not eqgen.needs_restart(self.saved, self.work)
                        else "duz")
        if ab_prev is not None:
            self._abx_prev = dict(ab_prev)
        else:
            st = self.status or self._request({"cmd": "status"})
            if st is None:
                return None
            self._abx_prev = {"mode": st.get("mode", "auto"), "profile": st.get("fixed_profile")}
        # a genre-variant edit: both options must load that variant's slots too
        self._abx_variants = {self.profile: self.variant_group} if self.in_variant() else None
        self._abx_kind = kind
        prof = self.profile
        name = PROFILE_LABELS[prof]
        if kind == "duz":
            labels = {"eq": _("{profil} (EQ)").format(profil=name), "duz": _("Düz (EQ yok)")}
            title = _("Kör test: {profil} ↔ Düz").format(profil=name)
        else:
            labels = {"yeni": _("Düzenlenmiş hal"), "kayitli": _("Kayıtlı hal")}
            title = _("Kör test: {profil} — kayıtlı ↔ düzenlenmiş").format(profil=name)
            self._request({"cmd": "set_mode", "mode": "fixed", "profile": prof})
        self._abx_snapshot = copy.deepcopy(self.work)
        dlg = AbxDialog(title, labels, self._abx_play, profile_color(prof), rounds, rng, self)
        dlg.finishedTest.connect(self._abx_finish)
        dlg.setModal(True)          # no tab switching mid-test
        self.abx = dlg
        dlg.show()
        return dlg

    def _abx_play(self, option: str) -> bool:
        """Switch what the user hears; False = it could not be switched (test invalid)."""
        prof = self.profile
        if option == "eq":
            return self._request({"cmd": "set_mode", "mode": "fixed", "profile": prof}) is not None
        if option == "duz":
            return self._request({"cmd": "set_mode", "mode": "off"}) is not None
        if option == "yeni":
            self.applier.submit(prof, self._abx_snapshot, None, variants=self._abx_variants)
        elif option == "kayitli":
            self.applier.submit(prof, self.saved, None, variants=self._abx_variants)
        return True

    def _abx_finish(self) -> None:
        if self.abx is None:
            return
        self.abx = None
        if self._abx_kind == "kayit":   # running sinks back to what the editor shows
            self.applier.submit(self.profile, self.work if self.live_on.isChecked()
                                else self.saved, None, variants=self._abx_variants)
        prev = self._abx_prev
        req = {"cmd": "set_mode", "mode": prev["mode"]}
        if prev["mode"] == "fixed" and prev.get("profile"):
            req["profile"] = prev["profile"]
        self._request(req)
        self._update_info()

    def hideEvent(self, e) -> None:  # noqa: N802 - leaving the page ends A/B
        if self.ab_btn.isChecked():
            self.ab_btn.setChecked(False)
        if self.in_variant() and self.profile in self._previewed:
            self.variant_group = ""          # leaving the page ends a variant preview
            self._end_preview()
            self._refresh_all()
        if not e.spontaneous():              # page switch / window closed (not minimized)
            self.discard_tone_preview()
            self._back_to_saved()            # unsaved live edits must not stay on the sinks
        super().hideEvent(e)

    def showEvent(self, e) -> None:  # noqa: N802 - pick up `kulak ton` / A/B results
        self.reload_tone()
        self._reload_if_changed_on_disk()
        super().showEvent(e)

    # ------------------------------------------------------------- kişisel ton
    def _build_tone(self) -> QFrame:
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label(_("Kişisel ton"), "sectionTitle"))
        self.tone_head = QWidget()
        hl = QHBoxLayout(self.tone_head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(10)
        self.tone_scope_box = QComboBox()
        self.tone_scope_box.addItem(_("Tüm profiller (genel)"), "genel")
        self.tone_scope_box.addItem(_("Bu tür için"), "tur")
        self.tone_scope_box.setToolTip(_("Genel: her profilde geçerli. Bu tür için: yalnız şu an "
                                         "çalan tür (ör. Müzik: Elektronik) çalarken genelin "
                                         "üstüne eklenir."))
        self.tone_scope_box.currentIndexChanged.connect(self._tone_scope_selected)
        hl.addWidget(self.tone_scope_box)
        hl.addStretch(1)
        self.tone_ab_btn = button(_("Kör A/B dinle…"))
        self.tone_ab_btn.setToolTip(_("Terminalde `kulak ab` açılır: çalan müziğin üstünde iki "
                                      "hali hangisi olduğunu bilmeden dinler, beğendiğini "
                                      "seçersin. Sonunda sonucu kaydedebilirsin."))
        self.tone_ab_btn.clicked.connect(lambda: self.start_tone_ab())
        self.tone_reset_btn = button(_("Sıfırla"))
        self.tone_reset_btn.setToolTip(_("Seçili kapsamın üç ayarını 0 dB yapar (Kaydet'e "
                                         "basana kadar yalnız önizleme)."))
        self.tone_reset_btn.clicked.connect(self.tone_reset)
        self.tone_revert_btn = button(_("Geri al"))
        self.tone_revert_btn.setToolTip(_("Kayıtlı değerlere döner."))
        self.tone_revert_btn.clicked.connect(self.tone_revert)
        self.tone_save_btn = button(_("Kaydet"), "primary")
        self.tone_save_btn.clicked.connect(self.tone_save)
        for b in (self.tone_ab_btn, self.tone_reset_btn, self.tone_revert_btn, self.tone_save_btn):
            hl.addWidget(b)
        head.addWidget(self.tone_head, 1)
        lay.addLayout(head)

        self.tone_body = QWidget()
        bl = QVBoxLayout(self.tone_body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(4)
        bl.addWidget(label(TONE_INTRO, "small", wrap=True))
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(2)
        steps = round(self._tone_limit() / TONE_STEP)
        self.tone_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for r, key in enumerate(self._tone_keys()):
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(-steps, steps)
            s.setSingleStep(1)
            s.setPageStep(1)
            s.setTickPosition(QSlider.TickPosition.TicksBelow)
            s.setTickInterval(2)
            s.setToolTip(TONE_HELP.get(key, ""))
            val = label("0 dB", "small")
            val.setMinimumWidth(64)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            s.valueChanged.connect(lambda v, k=key: self._tone_slider(k, v))
            grid.addWidget(label(TONE_LABELS.get(key, key)), 2 * r, 0)
            grid.addWidget(s, 2 * r, 1)
            grid.addWidget(val, 2 * r, 2)
            grid.addWidget(label(TONE_HELP.get(key, ""), "small", wrap=True), 2 * r + 1, 1, 1, 2)
            self.tone_sliders[key] = (s, val)
        grid.setColumnStretch(1, 1)
        bl.addLayout(grid)
        lay.addWidget(self.tone_body)
        self.tone_state = label("", "small", wrap=True)
        lay.addWidget(self.tone_state)
        available = self.tone_available()
        self.tone_head.setVisible(available)
        self.tone_body.setVisible(available)
        if not available:
            self.tone_state.setText(_("Kişisel ton ayarı bu kurulumda henüz yok (kisisel modülü "
                                      "bulunamadı). Güncelleme gelince burada üç kaydırıcı "
                                      "görünür: Bas sıcaklığı, Vokal netliği, Parlaklık."))
        return c

    def tone_available(self) -> bool:
        return self.kisisel is not None

    def _tone_keys(self) -> tuple[str, ...]:
        return tuple(getattr(self.kisisel, "TON_KEYS", None) or TONE_DEFAULT_KEYS)

    def _tone_limit(self) -> float:
        try:
            return float(getattr(self.kisisel, "TON_LIMIT", TONE_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            return TONE_DEFAULT_LIMIT

    def _tone_load(self) -> dict:
        if self.kisisel is None:
            return {}
        try:
            return self.kisisel.load() or {}
        except Exception as e:  # noqa: BLE001 - a broken file must not kill the page
            log.warning("kisisel.json okunamadı: %s", e)
            self._tone_error = _("Kişisel ton dosyası okunamadı: {hata}").format(hata=e)
            return {}

    def _tone_norm(self, data: dict | None) -> dict:
        """{"genel": {k: float}, "tur": {"p:g": {k: float}}} with every key present."""
        data = copy.deepcopy(data) if isinstance(data, dict) else {}
        keys = self._tone_keys()

        def vals(d) -> dict:
            d = d if isinstance(d, dict) else {}
            out = {}
            for k in keys:
                try:
                    out[k] = float(d.get(k, 0.0) or 0.0)
                except (TypeError, ValueError):
                    out[k] = 0.0
            return out
        data["genel"] = vals(data.get("genel"))
        tur = data.get("tur") if isinstance(data.get("tur"), dict) else {}
        data["tur"] = {str(k): vals(v) for k, v in tur.items()}
        return data

    @staticmethod
    def _tone_pruned(data: dict) -> dict:
        """Comparable form: tür entries that are all 0 dB do not count."""
        return {"genel": data["genel"],
                "tur": {k: v for k, v in data["tur"].items()
                        if any(abs(x) >= 0.05 for x in v.values())}}

    def tone_dirty(self) -> bool:
        return self._tone_pruned(self.tone_draft) != self._tone_pruned(self.tone_saved)

    def tone_enabled(self, prof: str) -> bool:
        """Profile takes part in the personal tone (profiller.toml `kisisel_ton`, CS2 off)."""
        if prof not in TONE_PROFILES:
            return False
        fn = getattr(eqgen, "tone_enabled", None)
        if callable(fn):
            try:
                return bool(fn(self.work, prof))
            except Exception:  # noqa: BLE001 - fall back to the contract defaults
                log.debug("eqgen.tone_enabled başarısız", exc_info=True)
        p = (self.work.get("profil") or {}).get(prof) or {}
        return bool(p.get("aktif", True)) and bool(p.get("kisisel_ton", prof != "cs2"))

    def _tone_group(self, prof: str) -> str:
        """The router's current genre group of a profile ("" = none / Genel)."""
        if prof not in VARIANT_PROFILES:
            return ""
        return self._router_variant(prof).get("group") or ""

    def _tone_effective(self, data: dict, prof: str, group: str = "") -> dict[str, float]:
        try:
            eff = self.kisisel.effective(data, prof, group)
            return {k: float(eff.get(k, 0.0)) for k in self._tone_keys()}
        except Exception:  # noqa: BLE001 - same rule as the contract: clamp(genel + tür)
            lim = self._tone_limit()
            t = data["tur"].get(f"{prof}:{group}", {}) if group else {}
            return {k: min(max(data["genel"].get(k, 0.0) + t.get(k, 0.0), -lim), lim)
                    for k in self._tone_keys()}

    def _tone_find_target(self) -> tuple[str, str] | None:
        """(profile, group) for the "Bu tür için" scope: the shown profile first, then the
        dominant one, then any variant profile with a current genre."""
        order = [self.profile, (self.status or {}).get("dominant"), *VARIANT_PROFILES]
        for prof in order:
            if prof in VARIANT_PROFILES and self.tone_enabled(prof) and self._tone_group(prof):
                return prof, self._tone_group(prof)
        return None

    def _tone_scope_values(self, create: bool = False) -> dict[str, float]:
        if self.tone_scope == "tur" and self.tone_target:
            key = "{}:{}".format(*self.tone_target)
            if create:
                return self.tone_draft["tur"].setdefault(key, {k: 0.0 for k in self._tone_keys()})
            return self.tone_draft["tur"].get(key) or {k: 0.0 for k in self._tone_keys()}
        return self.tone_draft["genel"]

    @staticmethod
    def _target_text(target: tuple[str, str] | None) -> str:
        if not target:
            return _("Bu tür için (şu an tür yok)")
        p, g = target
        return _("Bu tür için ({profil}: {tur})").format(profil=PROFILE_LABELS.get(p, p),
                                                         tur=GROUP_LABELS.get(g, g))

    def _tone_refresh(self) -> None:
        """Scope selector, sliders, state text and the graph's k-bands from the draft."""
        if not self.tone_available():
            self.graph.tone_bands = None
            return
        if self.tone_scope != "tur" or not self.tone_dirty():
            found = self._tone_find_target()
            if self.tone_scope == "tur" and found is None:
                self.tone_scope = "genel"       # the genre is gone and nothing is pending
            self.tone_target = found
        box = self.tone_scope_box
        box.blockSignals(True)
        box.setItemText(1, self._target_text(self.tone_target))
        item = box.model().item(1) if hasattr(box.model(), "item") else None
        if item is not None:
            item.setEnabled(self.tone_target is not None)
        box.setCurrentIndex(1 if self.tone_scope == "tur" else 0)
        box.blockSignals(False)
        vals = self._tone_scope_values()
        for key, (s, val) in self.tone_sliders.items():
            v = float(vals.get(key, 0.0))
            s.blockSignals(True)
            s.setValue(round(v / TONE_STEP))
            s.blockSignals(False)
            val.setText(fmt_tone(v))
        dirty = self.tone_dirty()
        self.tone_save_btn.setEnabled(dirty)
        self.tone_revert_btn.setEnabled(dirty)
        self.tone_reset_btn.setEnabled(any(abs(v) >= 0.05 for v in vals.values()))
        self._tone_update_state()
        self._tone_update_graph()

    def _tone_update_state(self) -> None:
        prof = self.profile
        lines = []
        if prof == FLAT_PROFILE:
            lines.append(_("Düz çıkışa kişisel ton uygulanmaz (EQ'suz geçiş)."))
        elif not self.tone_enabled(prof):
            lines.append((_("{profil} hariç: adım sesi ayarı bozulmasın "
                            "(profiller.toml'da kisisel_ton = true ile açılabilir).")
                          if prof == "cs2" else
                          _("{profil} profilinde kişisel ton kapalı "
                            "(profiller.toml: kisisel_ton)."))
                         .format(profil=PROFILE_LABELS.get(prof, prof)))
        else:
            g = self.variant_group if self.in_variant() else self._tone_group(prof)
            eff = self._tone_effective(self.tone_draft, prof, g)
            where = PROFILE_LABELS.get(prof, prof) + (f" · {GROUP_LABELS.get(g, g)}" if g else "")
            lines.append(_("{yer} için etkin: {degerler}").format(yer=where, degerler=" · ".join(
                f"{TONE_LABELS.get(k, k)} {fmt_tone(v)}" for k, v in eff.items())))
        if self.tone_scope == "tur" and self.tone_target:
            p, g = self.tone_target
            if self._tone_group(p) != g:
                lines.append(_("Not: şu an {tur} çalmıyor; bu ayar o tür çalınca duyulur.")
                             .format(tur=GROUP_LABELS.get(g, g)))
        if self._tone_error:
            lines.append("⚠ " + self._tone_error)
        elif self._tone_note:
            lines.append(self._tone_note)
        elif self.tone_dirty():
            lines.append(_("● Kaydedilmemiş — canlı önizleniyor; Kaydet'e basmadan sayfadan "
                           "çıkarsan kayıtlı değerlere döner."))
        self.tone_state.setText("\n".join(lines))

    def _tone_update_graph(self) -> None:
        prof = self.profile
        bands = None
        if self.tone_available() and self.tone_enabled(prof):
            g = self.variant_group if self.in_variant() else self._tone_group(prof)
            bands = tone_filters(self._tone_effective(self.tone_draft, prof, g),
                                 self.kisisel) or None
        if bands != self.graph.tone_bands:
            self.graph.tone_bands = bands
            self.graph.update()

    def _tone_scope_selected(self, idx: int) -> None:
        scope = self.tone_scope_box.itemData(idx) or "genel"
        if scope == "tur":
            target = self.tone_target or self._tone_find_target()
            if target is None:
                scope = "genel"
            self.tone_target = target
        self.tone_scope = scope
        self._tone_note = ""
        self._tone_refresh()

    def _tone_slider(self, key: str, v: int) -> None:
        lim = self._tone_limit()
        value = min(max(v * TONE_STEP, -lim), lim)
        self._tone_scope_values(create=True)[key] = value
        self.tone_sliders[key][1].setText(fmt_tone(value))
        self._tone_note = ""
        self._tone_changed()

    def set_tone(self, key: str, value: float) -> None:
        """Programmatic slider move (same path as the user)."""
        self.tone_sliders[key][0].setValue(round(value / TONE_STEP))

    def _tone_changed(self) -> None:
        dirty = self.tone_dirty()
        self.tone_save_btn.setEnabled(dirty)
        self.tone_revert_btn.setEnabled(dirty)
        self.tone_reset_btn.setEnabled(any(abs(v) >= 0.05
                                           for v in self._tone_scope_values().values()))
        self._tone_update_state()
        self._tone_update_graph()
        self.tone_timer.start()

    def _tone_jobs(self, data: dict) -> list[tuple[str, str, dict]]:
        """Sinks whose live tone differs from `data` (profile, current group, values)."""
        jobs = []
        for prof in TONE_PROFILES:
            if not self.tone_enabled(prof):
                continue
            g = self._tone_group(prof)
            eff = self._tone_effective(data, prof, g)
            live = self._tone_live.get(prof) or (g, self._tone_effective(self.tone_saved, prof, g))
            if (g, eff) != live:
                jobs.append((prof, g, eff))
        return jobs

    def _tone_preview(self) -> None:
        """Debounced: push the draft to the affected sinks via eqgen.apply_tone."""
        if not self.tone_available():
            return
        if tone_apply_fn() is None:
            self._tone_note = _("Canlı önizleme yok (eqgen güncel değil); Kaydet'e basınca "
                                "sonraki şarkı/oyunda uygulanır.")
            self._tone_update_state()
            return
        jobs = self._tone_jobs(self.tone_draft)
        for prof, g, eff in jobs:
            self._tone_live[prof] = (g, eff)
        if jobs:
            self.tone_applier.submit(jobs)

    def _tone_restore_jobs(self) -> list[tuple[str, str, dict]]:
        jobs = []
        for prof in list(self._tone_live):
            g = self._tone_group(prof)
            jobs.append((prof, g, self._tone_effective(self.tone_saved, prof, g)))
        self._tone_live.clear()
        return jobs

    def _tone_done(self, ok: bool, msg: str) -> None:
        self._tone_error = "" if ok else _("Ton canlı uygulanamadı: {hata}").format(hata=msg)
        self._tone_update_state()

    def tone_reset(self) -> None:
        vals = self._tone_scope_values(create=self.tone_scope == "tur")
        for k in list(vals):
            vals[k] = 0.0
        self._tone_note = ""
        self._tone_refresh()
        self.tone_timer.start()

    def tone_revert(self) -> None:
        """Back to the saved values (also live)."""
        self.tone_timer.stop()
        self.tone_draft = copy.deepcopy(self.tone_saved)
        jobs = self._tone_restore_jobs()
        if jobs and tone_apply_fn() is not None:
            self.tone_applier.submit(jobs)
        self._tone_note = ""
        self._tone_refresh()

    def discard_tone_preview(self, sync: bool = False) -> None:
        """Leaving without saving: drop the draft and restore the saved tone on the sinks.
        sync=True applies on the calling thread (window close / quit)."""
        if not self.tone_available():
            return
        self.tone_timer.stop()
        if not self.tone_dirty() and not self._tone_live:
            return
        self.tone_draft = copy.deepcopy(self.tone_saved)
        jobs = self._tone_restore_jobs()
        if jobs and tone_apply_fn() is not None:
            if sync:
                ToneApplier.apply(jobs)
            else:
                self.tone_applier.submit(jobs)
        self._tone_note = _("Kaydedilmemiş ton ayarı geri alındı.") if jobs else ""
        self._tone_refresh()

    def tone_save(self) -> bool:
        """Write the changed scopes into kisisel.json (merged with the file's current
        content, so `kulak ton` / A/B changes to other genres survive)."""
        if not self.tone_available():
            return False
        self.tone_timer.stop()
        fresh = self._tone_norm(self._tone_load())
        draft, saved = self.tone_draft, self.tone_saved
        if draft["genel"] != saved["genel"]:
            fresh["genel"] = dict(draft["genel"])
        for key in set(draft["tur"]) | set(saved["tur"]):
            d, s = draft["tur"].get(key), saved["tur"].get(key)
            if d == s:
                continue
            if d and any(abs(v) >= 0.05 for v in d.values()):
                fresh["tur"][key] = dict(d)
            else:
                fresh["tur"].pop(key, None)
        fresh["tur"] = {k: v for k, v in fresh["tur"].items()
                        if any(abs(x) >= 0.05 for x in v.values())}
        try:
            self.kisisel.save(fresh)
        except Exception as e:  # noqa: BLE001
            self._tone_error = _("Kaydedilemedi: {hata}").format(hata=e)
            self._tone_update_state()
            return False
        self.tone_saved = self._tone_norm(fresh)
        self.tone_draft = copy.deepcopy(self.tone_saved)
        self._tone_error = ""
        # the sinks already play the previewed (= now saved) values; anything else
        # (e.g. a sink whose genre changed meanwhile) gets the saved values now
        jobs = self._tone_jobs(self.tone_saved)
        self._tone_live.clear()
        if jobs and tone_apply_fn() is not None:
            self.tone_applier.submit(jobs)
        self._tone_note = (_("Kaydedildi.") if tone_apply_fn() is not None else
                           _("Kaydedildi; sonraki şarkı/oyunda uygulanır."))
        self._tone_refresh()
        return True

    def reload_tone(self) -> None:
        """Re-read kisisel.json (CLI / A/B may have changed it) unless edits are pending."""
        if not self.tone_available() or self.tone_dirty():
            return
        self._tone_error = ""
        data = self._tone_norm(self._tone_load())
        if data != self.tone_saved:
            self.tone_saved = data
            self.tone_draft = copy.deepcopy(data)
            self._tone_live.clear()
        self._tone_refresh()

    def _tone_status_changed(self) -> None:
        if not self.tone_available():
            return
        changed = False
        for prof, (g, _v) in list(self._tone_live.items()):
            if self._tone_group(prof) != g:   # router ramped in the saved tone with the new genre
                del self._tone_live[prof]
                changed = True
        if changed and self.tone_dirty():
            self.tone_timer.start()
        self._tone_refresh()

    def start_tone_ab(self, which=shutil.which) -> bool:
        """Open `kulak ab` in a terminal; only while music plays (it tests on the music)."""
        if not self.tone_available():
            return False
        if self.status is None:
            self._tone_note = _("Servis durumu bilinmiyor: Kulak Kesil çalışıyor mu?")
            self._tone_update_state()
            return False
        if not music_playing(self.status):
            self._tone_note = _("Kör A/B çalan müziğin üstünde yapılır: önce Müzik profilinde "
                                "bir şey çal (Spotify, YouTube…), sonra yeniden bas.")
            self._tone_update_state()
            return False
        cmd = terminal_command(kulak_command("ab", which=which), which=which)
        if cmd is None:
            self._tone_note = _("Terminal bulunamadı (ghostty/konsole/xterm). Elle: kulak ab")
            self._tone_update_state()
            return False
        self.discard_tone_preview()          # the test starts from the saved values
        ok = launch_terminal(cmd)
        self._tone_note = (_("Kör A/B terminalde açıldı. Bitince sonucu orada kaydet; "
                             "bu sayfaya dönünce kaydırıcılar güncellenir.") if ok else
                           _("Terminal açılamadı. Elle: kulak ab"))
        self._tone_update_state()
        return ok

    # ------------------------------------------------------------- status
    def set_status(self, status: dict | None) -> None:
        self.status = status
        self._sync_device_correction()
        dev = self._cihaz_duzeltme()
        if dev != self._staging_dev:          # another headphone: gain labels / Düz level
            self._staging_dev = dev
            self._refresh_all()
        self._update_apps()
        self._update_variant_lbl()
        self._tone_status_changed()

    def device_correction(self) -> bool:
        """Does the HS80 [duzeltme] curve apply on the current output? (CHANGE 7)

        From the router's status["kulaklik"] while a headphone is active; otherwise
        kulaklik.duzeltme_acik() (the state eqgen also reads; no file = True). Display only:
        the user's [duzeltme] aktif flag is never changed because of the device."""
        k = tx.kulaklik_of(self.status)
        if k is not None and k.get("kulaklik"):
            return bool(k.get("duzeltme"))
        if k is not None:
            return True           # speaker / HDMI: EQ is not running, show the curve as for HS80
        try:
            return kulaklik.duzeltme_acik()
        except Exception:  # noqa: BLE001 - unreadable state must not break the page
            return True

    def _sync_device_correction(self) -> None:
        on = self.device_correction()
        note = tx.duzeltme_yok_notu(self.status) if not on else ""
        if not on and not note:
            note = tx.DUZELTME_YOK_GENEL
        self.corr_note.setText(note)
        self.corr_note.setVisible(bool(note) and self.profile != FLAT_PROFILE)
        if self.graph.device_correction != on:
            self.graph.device_correction = on
            self.graph.update()

    def _update_apps(self) -> None:
        st = self.status
        if st is None:
            self.apps_lbl.setText(_("Servis durumu bilinmiyor"))
            return
        prof = self.profile
        if prof == FLAT_PROFILE:
            apps = [s for s in st.get("streams", []) if s.get("sink") == sink_name(FLAT_PROFILE)]
        else:
            apps = [s for s in st.get("streams", []) if s.get("profile") == prof]
        names = list(dict.fromkeys((s.get("app") or s.get("binary") or f"#{s.get('index')}")
                                   for s in apps))
        self.apps_lbl.setText(_("Şu an kullanan: {uygulamalar}").format(
            uygulamalar=", ".join(names)) if names
            else _("Şu an bu çıkışı kullanan uygulama yok"))


__all__ = ["EqPage", "ResponseGraph", "LiveApplier", "ToneApplier"]
