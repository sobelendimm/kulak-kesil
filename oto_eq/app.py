"""Kulak Kesil main window (python3 -m oto_eq.app, wrapper `kulak-app`).

Sidebar (Durum / Ekolayzır / Kurallar / Ayarlar) + QStackedWidget.
Status arrives from ``ipc.watch()`` on ``ui_common.WatchThread``.
Single instance: QLocalServer "oto-eq-app" accepts ``raise\\n`` (tray left click).
The look follows the system palette (Breeze light/dark); the accent color is
the dominant profile's color.
"""
from __future__ import annotations

import base64
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (QByteArray, QEasingCurve, QEvent, QObject, QProcess, QThread,
                          QPoint, QPointF, QProcessEnvironment, QPropertyAnimation, QRectF, QSize, Qt,
                          QTimer, QVariantAnimation, pyqtProperty, pyqtSignal)
from PyQt6.QtGui import (QActionGroup, QColor, QFont, QGuiApplication, QIcon, QPainter,
                         QPainterPath, QPalette, QPen, QPixmap)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (QAbstractButton, QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                             QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QGridLayout,
                             QGraphicsOpacityEffect, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                             QSlider,
                             QSizePolicy, QStackedWidget, QTableWidget, QTableWidgetItem,
                             QToolButton, QVBoxLayout, QWidget)

from . import config
from . import i18n
from .i18n import N_, _, veri_metni
from . import ui_common as ui
from . import ui_text as tx
from .models import EQ_PROFILES, GROUP_LABELS, VARIANT_PROFILES, variant_groups

log = logging.getLogger("oto_eq.app")

PAGES = (("durum", N_("Durum")), ("eq", N_("Ekolayzır")), ("mikrofon", N_("Mikrofon")),
         ("kurallar", N_("Kurallar")), ("ayarlar", N_("Ayarlar")))     # titles: _(text) at use
ALAN_LABELS = {"ikili": N_("Program (ikili)"), "uygulama": N_("Uygulama adı"),
               "flatpak": N_("Flatpak kimliği")}


def profile_choices() -> list[tuple[str, str]]:
    """(profile, label) for the assignment menus/dialog ('' = EQ'suz)."""
    return [(p, ui.profile_label(p)) for p in EQ_PROFILES] + \
        [("", _("{duz} (EQ'suz)").format(duz=_(ui.FLAT_LABEL)))]


# binaries that say nothing about the app: assign by application name instead
GENERIC_BINARIES = {"wine", "wine64", "wine-preloader", "wine64-preloader", "wineserver",
                    "python", "python3", "java", "electron", "pw-play", "paplay", "pw-cat",
                    "aplay", "bash", "sh", "steam", "reaper"}
SERVICES = (config.DAEMON_SERVICE, config.SINKS_SERVICE)
SERVICE_LABELS = {config.DAEMON_SERVICE: N_("Yönlendirici (oto-eq)"),
                  config.SINKS_SERVICE: N_("EQ çıkışları (oto-eq-sinks)")}
TILE_HOT = "#22A559"          # "acting right now" tile (ducking while someone talks)
TILE_DOTS = {"on": "#22A559", "hot": "#22A559", "wait": "#F2A428"}   # else COLOR_INACTIVE
STATE_TEXT = {"active": (N_("Çalışıyor"), "#22A559"), "inactive": (N_("Durdu"), ui.COLOR_INACTIVE),
              "failed": (N_("Hata"), ui.COLOR_OFF), "activating": (N_("Başlıyor"), "#F2A428"),
              "deactivating": (N_("Duruyor"), "#F2A428")}


# =========================================================================
# assignments (pure helpers, tested directly)
# =========================================================================
def assignment_key(stream: dict) -> tuple[str, str] | None:
    """('ikili', binary) or ('uygulama', app) for a status stream."""
    binary = (stream.get("binary") or "").strip()
    app = (stream.get("app") or "").strip()
    if binary and binary.lower() not in GENERIC_BINARIES:
        return ("ikili", binary)
    if app:
        return ("uygulama", app)
    flatpak = (stream.get("flatpak") or "").strip()
    if flatpak:
        return ("flatpak", flatpak)
    if binary:
        return ("ikili", binary)
    return None


def _same(a: dict, alan: str, deger: str) -> bool:
    return a.get("alan") == alan and str(a.get("deger", "")).lower() == deger.lower()


def find_assignment(items: list[dict], alan: str, deger: str) -> dict | None:
    return next((a for a in items if _same(a, alan, deger)), None)


def upsert_assignment(items: list[dict], alan: str, deger: str, profil: str) -> list[dict]:
    out, done = [], False
    for a in items:
        if _same(a, alan, deger):
            if not done:
                out.append({"alan": alan, "deger": deger, "profil": profil})
                done = True
        else:
            out.append(dict(a))
    if not done:
        out.append({"alan": alan, "deger": deger, "profil": profil})
    return out


def remove_assignment(items: list[dict], alan: str, deger: str) -> list[dict]:
    return [dict(a) for a in items if not _same(a, alan, deger)]


_ODAK_CACHE: dict = {"mtime": None, "data": {}}


def variant_odak(profile: str, group: str) -> str:
    """'odak' of [profil.<p>.tur.<group>] in profiller.toml ('' if none). Cached by mtime."""
    if not group:
        return ""
    try:
        path = config.config_path(config.PROFILES_FILE)
        mtime = path.stat().st_mtime
        if _ODAK_CACHE["mtime"] != (path, mtime):
            _ODAK_CACHE["data"] = config.load_toml(config.PROFILES_FILE)
            _ODAK_CACHE["mtime"] = (path, mtime)
        tur = ((_ODAK_CACHE["data"].get("profil") or {}).get(profile) or {}).get("tur") or {}
        return veri_metni((tur.get(group) or {}).get("odak"))
    except (OSError, ValueError, AttributeError):
        return ""


CATEGORY_LABELS = {"cs2": "CS2", "oyun": N_("Oyun"), "muzik": N_("Müzik"), "film": N_("Film"),
                   "sesli": N_("Sesli sohbet"), "sistem": N_("Sistem"), "bilinmeyen": N_("Bilinmeyen")}


def ago(ts: float, now: float | None = None) -> str:
    """Relative time: 'az önce', '5 dk önce', '3 sa önce', 'dün', '4 gün önce'. The amount is
    a unit abbreviation inside "{zaman} önce" (no plural forms needed in any language)."""
    import time as _t
    now = _t.time() if now is None else now
    d = max(0.0, now - (ts or 0))
    if d < 60:
        return _("az önce")
    if d < 3600:
        return _("{zaman} önce").format(zaman=_("{n} dk").format(n=int(d // 60)))
    if d < 86400:
        return _("{zaman} önce").format(zaman=_("{n} sa").format(n=int(d // 3600)))
    if d < 2 * 86400:
        return _("dün")
    if d < 30 * 86400:
        return _("{zaman} önce").format(zaman=_("{n} gün").format(n=int(d // 86400)))
    return _t.strftime("%d.%m.%Y", _t.localtime(ts))


def load_recent(limit: int = 50) -> list[dict] | None:
    """Recently seen apps from oto_eq.history, or None if unavailable.

    A fresh History() per call: the module default caches the file after the
    first load and would never show what the router recorded later.
    """
    try:
        from . import history
    except ImportError:
        return None
    try:
        cls = getattr(history, "History", None)
        return cls().recent(limit) if cls else history.recent(limit=limit)
    except Exception:  # noqa: BLE001 - a broken log must not break the page
        log.exception("son görülen uygulamalar okunamadı")
        return None


def history_as_stream(e: dict) -> dict:
    """History entry -> the stream-like dict used by assignment helpers/rows."""
    return {"app": e.get("uygulama") or "", "binary": e.get("ikili") or "",
            "flatpak": e.get("flatpak") or "", "profile": e.get("profil"),
            "reason": e.get("neden") or "", "category": e.get("kategori") or ""}


# =========================================================================
# theme
# =========================================================================
def mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(round(a.red() + (b.red() - a.red()) * t),
                  round(a.green() + (b.green() - a.green()) * t),
                  round(a.blue() + (b.blue() - a.blue()) * t))


def rgba(c: QColor | str, alpha: float) -> str:
    c = QColor(c)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha:.3f})"


@dataclass
class Theme:
    dark: bool
    window: QColor
    card: QColor
    sidebar: QColor
    border: QColor
    hover: QColor
    text: QColor
    muted: QColor
    highlight: QColor
    accent: QColor
    base_pt: float

    @classmethod
    def from_palette(cls, pal: QPalette, accent: str | None, base_pt: float) -> "Theme":
        win = pal.color(QPalette.ColorRole.Window)
        text = pal.color(QPalette.ColorRole.WindowText)
        dark = win.lightnessF() < 0.5
        white, black = QColor("#FFFFFF"), QColor("#000000")
        if dark:
            card, sidebar = mix(win, white, 0.045), mix(win, black, 0.22)
            border, hover = mix(win, white, 0.11), mix(win, white, 0.075)
        else:
            card, sidebar = pal.color(QPalette.ColorRole.Base), mix(win, black, 0.035)
            border, hover = mix(win, black, 0.11), mix(win, black, 0.05)
        hl = pal.color(QPalette.ColorRole.Highlight)
        return cls(dark, win, card, sidebar, border, hover, text, mix(text, win, 0.42), hl,
                   QColor(accent) if accent else hl, base_pt)


def stylesheet(t: Theme) -> str:
    b = t.base_pt
    acc = t.accent
    acc_hover = acc.lighter(112) if t.dark else acc.darker(108)
    return f"""
    QWidget#root {{ background: {t.window.name()}; }}
    QWidget#page, QWidget#scrollBody {{ background: transparent; }}
    QScrollArea {{ background: transparent; border: none; }}
    QFrame#sidebar {{ background: {t.sidebar.name()}; border: none;
                      border-right: 1px solid {t.border.name()}; }}
    QLabel {{ color: {t.text.name()}; background: transparent; }}
    QLabel#appName {{ font-size: {b * 1.4:.1f}pt; font-weight: 700; }}
    QLabel#muted, QLabel#small {{ color: {t.muted.name()}; }}
    QLabel#small {{ font-size: {b * 0.9:.1f}pt; }}
    QLabel#pageTitle {{ font-size: {b * 1.75:.1f}pt; font-weight: 650; }}
    QLabel#sectionTitle {{ font-size: {b * 1.15:.1f}pt; font-weight: 650; }}
    QLabel#heroTitle {{ font-size: {b * 2.1:.1f}pt; font-weight: 700; }}
    QLabel#heroOdak {{ font-size: {b * 1.2:.1f}pt; }}
    QLabel#mono {{ font-family: monospace; padding: 6px 10px; border-radius: 6px;
                   background: {t.hover.name()}; }}
    QPushButton#nav {{ text-align: left; padding: 9px 12px; border: none; border-radius: 9px;
                       color: {t.text.name()}; background: transparent; font-size: {b * 1.05:.1f}pt; }}
    QPushButton#nav:hover {{ background: {t.hover.name()}; }}
    QPushButton#nav:checked {{ background: {rgba(acc, 0.20 if t.dark else 0.14)}; font-weight: 650; }}
    QFrame#card {{ background: {t.card.name()}; border: 1px solid {t.border.name()};
                   border-radius: 14px; }}
    QFrame#row {{ background: transparent; border: none; border-radius: 10px; }}
    QFrame#row:hover {{ background: {t.hover.name()}; }}
    QFrame#separator {{ background: {t.border.name()}; border: none; }}
    QFrame#segment {{ background: {t.hover.name()}; border: 1px solid {t.border.name()};
                      border-radius: 11px; }}
    QPushButton#seg {{ border: none; border-radius: 8px; padding: 7px 16px; background: transparent;
                       color: {t.muted.name()}; font-weight: 600; }}
    QPushButton#seg:hover {{ color: {t.text.name()}; background: {rgba(t.text, 0.06)}; }}
    QPushButton#seg:disabled {{ color: {rgba(t.muted, 0.5)}; }}
    QPushButton#primary {{ background: {acc.name()}; color: white; border: none; border-radius: 8px;
                           padding: 7px 16px; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {acc_hover.name()}; }}
    QPushButton#primary:disabled {{ background: {rgba(acc, 0.45)}; }}
    QPushButton#secondary, QToolButton#secondary {{ background: {t.hover.name()}; color: {t.text.name()};
                           border: 1px solid {t.border.name()}; border-radius: 8px; padding: 6px 14px; }}
    QPushButton#secondary:hover, QToolButton#secondary:hover {{ border-color: {rgba(acc, 0.8)}; }}
    QPushButton#secondary:disabled {{ color: {t.muted.name()}; }}
    QToolButton#secondary::menu-indicator {{ image: none; width: 0; }}
    QToolButton#more {{ border: none; border-radius: 8px; padding: 2px 8px; color: {t.muted.name()};
                        font-size: {b * 1.4:.1f}pt; font-weight: 700; background: transparent; }}
    QToolButton#more:hover {{ background: {rgba(t.text, 0.08)}; color: {t.text.name()}; }}
    QToolButton#more::menu-indicator {{ image: none; width: 0; }}
    QFrame#banner {{ background: {rgba(ui.COLOR_OFF, 0.13)}; border: 1px solid {rgba(ui.COLOR_OFF, 0.5)};
                     border-radius: 12px; }}
    QPushButton#danger {{ background: {ui.COLOR_OFF}; color: white; border: none; border-radius: 8px;
                          padding: 7px 16px; font-weight: 600; }}
    QPushButton#danger:hover {{ background: {QColor(ui.COLOR_OFF).lighter(110).name()}; }}
    QLabel#toast {{ background: {rgba('#1B1E24', 0.94)}; color: white; border-radius: 10px;
                    padding: 9px 16px; }}
    QTableWidget {{ background: {t.card.name()}; border: none; gridline-color: transparent;
                    color: {t.text.name()}; selection-background-color: {rgba(acc, 0.22)};
                    selection-color: {t.text.name()}; outline: none; }}
    QTableWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {rgba(t.border, 0.7)}; }}
    QHeaderView::section {{ background: {t.card.name()}; color: {t.muted.name()}; border: none;
                            border-bottom: 1px solid {t.border.name()}; padding: 6px 8px;
                            font-weight: 600; }}
    QFrame#tile {{ background: {t.card.name()}; border: 1px solid {t.border.name()};
                   border-radius: 12px; }}
    QFrame#tile[hot="true"] {{ background: {rgba(TILE_HOT, 0.13)}; border-color: {rgba(TILE_HOT, 0.65)}; }}
    QFrame#tile[link="true"]:hover {{ border-color: {rgba(acc, 0.8)}; background: {t.hover.name()}; }}
    QLabel#tileTitle {{ color: {t.muted.name()}; font-size: {b * 0.88:.1f}pt; font-weight: 600; }}
    QLabel#tileValue {{ font-weight: 650; }}
    QLabel#tileLine {{ color: {t.muted.name()}; font-size: {b * 0.9:.1f}pt; }}
    QFrame#news {{ background: {t.card.name()}; border: 1px solid {rgba(t.highlight, 0.45)};
                   border-radius: 14px; }}
    QLabel#newsDot {{ background: {t.highlight.name()}; border-radius: 3px; }}
    QPushButton#link {{ background: transparent; border: none; padding: 2px 4px;
                        color: {(t.highlight.lighter(125) if t.dark else t.highlight.darker(110)).name()};
                        font-weight: 600; }}
    QPushButton#link:hover {{ text-decoration: underline; }}
    """


# =========================================================================
# small painted pieces
# =========================================================================
def _pm(size: int, dpr: float) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    return pm, p


def nav_icon(kind: str, color: QColor, size: int = 20, dpr: float = 2.0) -> QIcon:
    """Crisp monochrome sidebar icons drawn with QPainter."""
    pm, p = _pm(size, dpr)
    s = float(size)
    pen = QPen(color, s * 0.09, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
               Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    if kind == "durum":  # headphones
        p.drawArc(QRectF(s * 0.18, s * 0.16, s * 0.64, s * 0.66), 0, 180 * 16)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(s * 0.14, s * 0.50, s * 0.17, s * 0.30), s * 0.06, s * 0.06)
        p.drawRoundedRect(QRectF(s * 0.69, s * 0.50, s * 0.17, s * 0.30), s * 0.06, s * 0.06)
    elif kind == "eq":  # sliders
        for x, k in ((0.25, 0.62), (0.5, 0.35), (0.75, 0.55)):
            p.setPen(pen)
            p.drawLine(QPointF(s * x, s * 0.16), QPointF(s * x, s * 0.84))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(QRectF(s * x - s * 0.11, s * k - s * 0.11, s * 0.22, s * 0.22))
    elif kind == "kurallar":  # list
        p.setBrush(color)
        for y in (0.27, 0.5, 0.73):
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QRectF(s * 0.14, s * y - s * 0.06, s * 0.12, s * 0.12))
            p.setPen(pen)
            p.drawLine(QPointF(s * 0.38, s * y), QPointF(s * 0.86, s * y))
    elif kind == "mikrofon":
        paint_mic_glyph(p, s, color)
    else:  # ayarlar: gear
        import math
        c = s / 2
        path = QPainterPath()
        teeth = 8
        for i in range(teeth * 2):
            a = math.pi * 2 * i / (teeth * 2)
            r = s * (0.42 if i % 2 == 0 else 0.33)
            for da in (-0.17, 0.17):
                pt = (c + r * math.cos(a + da), c + r * math.sin(a + da))
                if path.elementCount() == 0:
                    path.moveTo(*pt)
                else:
                    path.lineTo(*pt)
        path.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(QRectF(c - s * 0.13, c - s * 0.13, s * 0.26, s * 0.26))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(path.subtracted(hole))
    p.end()
    return QIcon(pm)


def paint_mic_glyph(p: QPainter, s: float, color: QColor, ox: float = 0.0, oy: float = 0.0) -> None:
    """Microphone: capsule + holder arc + stem + base, in an s x s box."""
    pen = QPen(color, s * 0.09, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(ox + s * 0.36, oy + s * 0.10, s * 0.28, s * 0.46), s * 0.14, s * 0.14)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(QRectF(ox + s * 0.24, oy + s * 0.22, s * 0.52, s * 0.48), 180 * 16, 180 * 16)
    p.drawLine(QPointF(ox + s * 0.5, oy + s * 0.70), QPointF(ox + s * 0.5, oy + s * 0.84))
    p.drawLine(QPointF(ox + s * 0.36, oy + s * 0.86), QPointF(ox + s * 0.64, oy + s * 0.86))


def mic_badge_pixmap(color: str, size: int, dpr: float) -> QPixmap:
    pm, p = _pm(size, dpr)
    ring = max(1.0, size * 0.045)
    p.setPen(QPen(QColor(0, 0, 0, 110), ring))
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(ring, ring, size - 2 * ring, size - 2 * ring))
    inner = size * 0.62
    paint_mic_glyph(p, inner, QColor("white"), (size - inner) / 2, (size - inner) / 2)
    p.end()
    return pm


def badge_label_pixmap(spec: ui.BadgeSpec, size: int, dpr: float) -> QPixmap:
    pm = ui.badge_pixmap(round(size * dpr), spec)
    pm.setDevicePixelRatio(dpr)
    return pm


def app_avatar(stream: dict, color: str, size: int, dpr: float) -> QPixmap:
    """Theme icon of the app if there is one, else a colored letter tile."""
    names = []
    for raw in (stream.get("binary"), stream.get("app")):
        if raw:
            n = raw.lower().removesuffix(".exe")
            names += [n, n.replace(" ", "-"), n.split()[0] if " " in n else n]
    for n in dict.fromkeys(names):
        if n and QIcon.hasThemeIcon(n):
            pm = QIcon.fromTheme(n).pixmap(QSize(size, size), dpr)
            if not pm.isNull():
                return pm
    pm, p = _pm(size, dpr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.28, size * 0.28)
    f = QFont()
    f.setBold(True)
    f.setPixelSize(round(size * 0.5))
    p.setFont(f)
    p.setPen(QColor("white"))
    name = stream.get("app") or stream.get("binary") or "?"
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, name[:1].upper())
    p.end()
    return pm


def dot_icon(color: str, size: int = 12, dpr: float = 2.0) -> QIcon:
    pm, p = _pm(size, dpr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    p.end()
    return QIcon(pm)


def chip(text: str, color: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("chip")
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet(f"QLabel#chip {{ background: {color}; color: white; border-radius: 10px;"
                      f" padding: 3px 12px; font-weight: 600; }}")
    return lab


def card(parent=None) -> QFrame:
    f = QFrame(parent)
    f.setObjectName("card")
    return f


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


class ToggleSwitch(QAbstractButton):
    """Small animated on/off switch (Plasma-like)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)
        self.on_color = QColor("#3DAEE9")

    def sizeHint(self):
        return QSize(44, 24)

    def _get(self):
        return self._pos

    def _set(self, v):
        self._pos = v
        self.update()

    knob = pyqtProperty(float, _get, _set)

    def _animate(self, on: bool):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on: bool):  # noqa: N802 - Qt API
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        off = self.palette().color(QPalette.ColorRole.Mid)
        track = mix(off, self.on_color, self._pos)
        if not self.isEnabled():
            track.setAlphaF(0.4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * self._pos
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))
        p.end()


class Toast(QLabel):
    """Short message bubble at the bottom of the window."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toast")
        self.setWordWrap(False)
        self._fx = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._fx)
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(180)
        self._timer = QTimer(self, singleShot=True, timeout=self._fade_out)
        self.hide()

    def show_text(self, text: str, ms: int = 2800):
        self.setText(text)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity() if self.isVisible() else 0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(ms)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self._hide_once)
        self._anim.start()

    def _hide_once(self):
        try:
            self._anim.finished.disconnect(self._hide_once)
        except TypeError:
            pass
        if self._fx.opacity() < 0.05:
            self.hide()

    def reposition(self):
        par = self.parentWidget()
        if par:
            self.move((par.width() - self.width()) // 2, par.height() - self.height() - 24)


# =========================================================================
# command runner (QProcess; tests inject a fake with the same signature)
# =========================================================================
class CommandRunner(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._procs: set[QProcess] = set()

    def run(self, args: list[str], on_done=None, env: dict | None = None,
            timeout_ms: int = 120000) -> None:
        proc = QProcess(self)
        penv = QProcessEnvironment()
        for k, v in (env or config.C_ENV).items():
            penv.insert(k, v)
        proc.setProcessEnvironment(penv)
        proc.setWorkingDirectory(str(config.SRC_DIR))
        self._procs.add(proc)
        timer = QTimer(proc, singleShot=True, timeout=proc.kill)

        def finished(code=0, _status=None):
            timer.stop()
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            err = bytes(proc.readAllStandardError()).decode("utf-8", "replace")
            self._procs.discard(proc)
            proc.deleteLater()
            if on_done:
                on_done(code, out, err)

        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                finished(127)

        proc.finished.connect(finished)
        proc.errorOccurred.connect(failed)
        timer.start(timeout_ms)
        proc.start(args[0], args[1:])


def systemctl_args(*args: str) -> list[str]:
    return ["systemctl", "--user", *args]


def py_child_env(base: dict | None = None) -> dict[str, str]:
    """Env for our own `python3 -m oto_eq.…` children: importable source + THIS window's
    language (a child started with config.C_ENV / LC_ALL=C would pick English for
    "otomatik" and show its errors in another language than the window)."""
    env = dict(config.C_ENV if base is None else base)
    env["PYTHONPATH"] = str(config.SRC_DIR)
    env["OTO_EQ_DIL"] = i18n.dil()
    return env


def stop_process(proc: QProcess | None, grace_ms: int = 500) -> None:
    """SIGTERM now, SIGKILL after ``grace_ms`` if it is still running — without blocking
    the GUI thread (waitForFinished froze the window for up to 4 s)."""
    if proc is None or proc.state() == QProcess.ProcessState.NotRunning:
        return
    proc.terminate()

    def kill():
        if proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()
    QTimer(proc, singleShot=True, interval=grace_ms, timeout=kill).start()


class FuncThread(QThread):
    """Runs fn() off the GUI thread; emits done(result) or failed(exception text)."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            log.exception("arka plan işi başarısız")
            self.failed.emit(f"{type(e).__name__}: {e}")


HEALTH_COLORS = {"tamam": "#22A559", "uyari": "#F2A428", "hata": ui.COLOR_OFF}
HEALTH_GLYPHS = {"tamam": "✔", "uyari": "!", "hata": "✕"}


def load_saglik():
    try:
        from . import saglik
    except ImportError:
        return None
    return saglik if hasattr(saglik, "run_checks") else None


def health_summary(results: list[dict]) -> str:
    n = {k: sum(r.get("durum") == k for r in results) for k in ("tamam", "uyari", "hata")}
    if not n["uyari"] and not n["hata"]:
        return _("Özet: {tamam} tamam, {uyari} uyarı, {hata} hata — her şey yolunda ✓").format(**n)
    return _("Özet: {tamam} tamam, {uyari} uyarı, {hata} hata").format(**n)


def status_glyph_pixmap(durum: str, size: int, dpr: float) -> QPixmap:
    pm, p = _pm(size, dpr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(HEALTH_COLORS.get(durum, ui.COLOR_INACTIVE)))
    p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    f = QFont()
    f.setBold(True)
    f.setPixelSize(round(size * 0.62))
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, HEALTH_GLYPHS.get(durum, "?"))
    p.end()
    return pm


# =========================================================================
# pages
# =========================================================================
class Page(QWidget):
    """Scrollable page with a title."""

    def __init__(self, title: str, subtitle: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        body.setObjectName("scrollBody")
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.body = QVBoxLayout(body)
        self.body.setContentsMargins(32, 28, 32, 28)
        self.body.setSpacing(18)
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label(title, "pageTitle"))
        if subtitle:
            head.addWidget(label(subtitle, "muted", wrap=True))
        self.body.addLayout(head)

    def set_status(self, status: dict | None) -> None:
        pass


class StreamRow(QFrame):
    def __init__(self, win: "MainWindow", stream: dict, assigned: dict | None):
        super().__init__()
        self.setObjectName("row")
        self.win, self.stream = win, stream
        prof = stream.get("profile")
        color = ui.PROFILE_COLORS.get(prof, ui.COLOR_INACTIVE)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 6, 8)
        lay.setSpacing(12)
        dpr = win.devicePixelRatioF() or 1.0
        icon = QLabel()
        icon.setPixmap(app_avatar(stream, color, 34, dpr))
        icon.setFixedSize(34, 34)
        lay.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(0)
        app = stream.get("app") or stream.get("binary") or f"#{stream.get('index')}"
        nl = label(app)
        f = nl.font()
        f.setWeight(QFont.Weight.DemiBold)
        nl.setFont(f)
        names.addWidget(nl)
        sub = stream.get("binary") or ""
        if assigned is not None:
            sub = (sub + " · " if sub else "") + _("📌 atandı")
        names.addWidget(label(sub, "small"))
        lay.addLayout(names, 2)
        reason = label(stream.get("reason") or "", "small")
        reason.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        reason.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        reason.setToolTip(stream.get("reason") or "")
        lay.addWidget(reason, 3)
        v = tx.variant_of(win.status, prof) if prof in VARIANT_PROFILES else None
        text = ui.stream_profile_label(stream)
        if v:
            text = f"{text} · {tx.variant_text(v)}"
        c = chip(text, color)
        c.setToolTip(tx.variant_tooltip(v) if v else ui.stream_profile_tooltip(stream))
        c.setMinimumWidth(78)
        c.setMaximumWidth(330)
        fm = c.fontMetrics()
        c.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 300))
        lay.addWidget(c)
        self.variant_button = None
        if prof in VARIANT_PROFILES:
            vb = QToolButton()
            vb.setObjectName("more")
            vb.setText("▾")
            vb.setToolTip(_("{tur} seç").format(tur=_(tx.VARIANT_KIND_LABELS[prof])))
            vb.setCursor(Qt.CursorShape.PointingHandCursor)
            vb.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            self.variant_menu = win.variant_menu(prof, self)
            vb.setMenu(self.variant_menu)
            lay.addWidget(vb)
            self.variant_button = vb
        more = QToolButton()
        more.setObjectName("more")
        more.setText("⋯")
        more.setToolTip(_("Bu uygulama için profil seç"))
        more.setCursor(Qt.CursorShape.PointingHandCursor)
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = win.assignment_menu(stream, self)
        more.setMenu(self.menu)
        lay.addWidget(more)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda pos: self.menu.popup(self.mapToGlobal(pos)))


# =========================================================================
# Durum: "Akıllı özellikler" tiles + one-time "Yenilikler" card
# =========================================================================
LIVE_POLL_MS = 2000           # ducking/genre are not pushed by the router: poll while visible
LOCAL_EVERY = 15              # live ticks between re-reads of the local files (~30 s)
TILE_KEYS = ("kulaklik", "tur", "kisma", "ton", "seviye", "model", "onarim")
_LEVEL_CACHE: dict = {}


def load_tone() -> dict | None:
    """kisisel.json (never raises; None = module missing)."""
    try:
        from . import kisisel
        return kisisel.load()
    except Exception as e:  # noqa: BLE001
        log.debug("kişisel ton okunamadı: %s", e)
        return None


def load_level_info() -> dict | None:
    """Which EQ profiles have the limiter / loudness leveller / crossfeed (profiller.toml).
    Cached by the file's mtime (gain staging takes ~30 ms)."""
    try:
        from . import eqgen
        lim, ag, xf = (getattr(eqgen, n, None) for n in ("_lim_active", "_ag_active", "_xf_active"))
        if lim is None or ag is None:
            return None
        path = config.config_path(config.PROFILES_FILE)
        try:
            mt = Path(path).stat().st_mtime_ns
        except OSError:
            mt = None
        key = (str(path), mt)
        if _LEVEL_CACHE.get("key") == key:
            return _LEVEL_CACHE["info"]
        profiles = eqgen.load_profiles()
        lsp = eqgen.lsp_available()
        info = {"sinirlayici": [p for p in EQ_PROFILES if lim(profiles, p, lsp)],
                "seviye": [p for p in EQ_PROFILES if ag(profiles, p, lsp)],
                "crossfeed": [p for p in EQ_PROFILES if xf is not None and xf(profiles, p)],
                "seviye_db": None}
        try:
            stg = eqgen.gain_staging(profiles, lsp)
            info["seviye_db"] = (stg.get("duz") or {}).get("final_loudness_db")
        except Exception as e:  # noqa: BLE001
            log.debug("kazanç dağılımı hesaplanamadı: %s", e)
        _LEVEL_CACHE.update(key=key, info=info)
        return info
    except Exception as e:  # noqa: BLE001
        log.debug("seviye bilgisi okunamadı: %s", e)
        return None


def load_model_info() -> dict | None:
    """Kişisel model: active? + how many tray corrections so far."""
    try:
        from . import kisisel_model as km
    except Exception as e:  # noqa: BLE001
        log.debug("kişisel model yok: %s", e)
        return None
    m = km.oku_model()
    try:
        aktif = km._gecerli(m) is not None
    except Exception:  # noqa: BLE001
        aktif = False
    try:
        from . import geri_bildirim
        n = len(geri_bildirim.oku())
    except Exception:  # noqa: BLE001
        n = 0
    return {"aktif": aktif, "gerek": getattr(km, "MIN_STRONG", 20), "duzeltme": n,
            "neden": (m or {}).get("neden", "")}


def load_onarim() -> list[dict] | None:
    """Self-heal history (STATE_DIR/onarim.json, written by oto_eq.onarim)."""
    data = ui.read_json(config.STATE_DIR / "onarim.json")
    son = data.get("son") if isinstance(data, dict) else None
    return son if isinstance(son, list) else None


def scroll_into_view(w: QWidget, margin: int = 16) -> bool:
    """Scroll the page that holds ``w`` so its card (or ``w``) is at the top."""
    target, area, p = w, None, w.parentWidget()
    while p is not None:
        if target is w and p.objectName() in ("card", "news"):
            target = p
        if isinstance(p, QScrollArea):
            area = p
            break
        p = p.parentWidget()
    if area is None or area.widget() is None:
        return False
    y = target.mapTo(area.widget(), QPoint(0, 0)).y()
    area.verticalScrollBar().setValue(max(0, y - margin))
    return True


class ElideLabel(QLabel):
    """One line, elided on the right; the full text goes into the tooltip."""

    def __init__(self, text: str = "", name: str | None = None):
        super().__init__()
        if name:
            self.setObjectName(name)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(30)
        self.set_full(text)

    def full(self) -> str:
        return self._full

    def set_full(self, text: str) -> None:
        self._full = text or ""
        self._elide()

    def _elide(self) -> None:
        el = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(20, self.width()))
        if el != self.text():
            self.setText(el)
        self.setToolTip(self._full if el != self._full else "")

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e):  # noqa: N802
        super().changeEvent(e)
        if e.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()


class SmartTile(QFrame):
    """Small status tile: dot + title, value, up to 3 muted lines (tx.*_tile dicts)."""

    clicked = pyqtSignal()
    MAX_LINES = 3

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.data: dict | None = None
        self.setObjectName("tile")
        self.setProperty("hot", False)
        self.setProperty("link", False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 11)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(7)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        head.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.title = label("", "tileTitle")
        head.addWidget(self.title, 1)
        lay.addLayout(head)
        self.value = label("", "tileValue", wrap=True)
        lay.addWidget(self.value)
        self.lines = [ElideLabel("", "tileLine") for _i in range(self.MAX_LINES)]
        for ln in self.lines:
            lay.addWidget(ln)
        lay.addStretch(1)
        self._dot_color = ""

    def set_data(self, d: dict | None) -> None:
        self.data = d
        if d is None:
            return
        self.title.setText(d.get("title", ""))
        self.value.setText(d.get("value", ""))
        lines = [str(x) for x in d.get("lines") or [] if x][:self.MAX_LINES]
        for i, ln in enumerate(self.lines):
            ln.set_full(lines[i] if i < len(lines) else "")
            ln.setVisible(i < len(lines))
        self.setToolTip(d.get("tip", ""))
        state = d.get("state", "off")
        color = TILE_DOTS.get(state, ui.COLOR_INACTIVE)
        if color != self._dot_color:
            self._dot_color = color
            dpr = self.devicePixelRatioF() or 1.0
            self.dot.setPixmap(dot_icon(color, 8, max(2.0, dpr)).pixmap(QSize(8, 8), max(2.0, dpr)))
        hot, link = state == "hot", bool(d.get("link"))
        if self.property("hot") != hot or self.property("link") != link:
            self.setProperty("hot", hot)
            self.setProperty("link", link)
            self.setCursor(Qt.CursorShape.PointingHandCursor if link else Qt.CursorShape.ArrowCursor)
            self.style().unpolish(self)
            self.style().polish(self)

    def mouseReleaseEvent(self, e):  # noqa: N802
        if self.data and self.data.get("link") and e.button() == Qt.MouseButton.LeftButton \
                and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(e)


class SmartRow(QWidget):
    """'Akıllı özellikler': tiles in a grid that wraps with the page width."""

    MIN_TILE = 210
    SPACING = 10

    def __init__(self, win: "MainWindow"):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(label(_("Akıllı özellikler"), "sectionTitle"))
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(self.SPACING)
        self.grid.setVerticalSpacing(self.SPACING)
        v.addLayout(self.grid)
        self.tiles = {k: SmartTile(k) for k in TILE_KEYS}
        for t in self.tiles.values():
            t.setParent(self)
            t.hide()
        self.tiles["ton"].clicked.connect(lambda: win.jump_to("eq", "ton"))
        self._order: tuple = ()

    def visible_keys(self) -> list[str]:
        return [k for k, t in self.tiles.items() if t.data is not None]

    def columns(self, n: int, width: int | None = None) -> int:
        """Most tiles per row that fit, then balanced (4 tiles → 2+2, 5 → 3+2)."""
        if n <= 0:
            return 1
        w = self.width() if width is None else width
        fit = max(1, min(n, (w + self.SPACING) // (self.MIN_TILE + self.SPACING)))
        rows = -(-n // fit)
        return -(-n // rows)

    def set_tiles(self, datas: dict) -> None:
        for k, t in self.tiles.items():
            t.set_data(datas.get(k))
        self._relayout()

    def _relayout(self) -> None:
        keys = self.visible_keys()
        cols = self.columns(len(keys))
        order = (tuple(keys), cols)
        if order == self._order:
            return
        self._order = order
        while self.grid.count():
            self.grid.takeAt(0)
        for k, t in self.tiles.items():
            t.setVisible(k in keys)
        for i, k in enumerate(keys):
            self.grid.addWidget(self.tiles[k], i // cols, i % cols)
        for c in range(len(TILE_KEYS)):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self._relayout()


class NewsCard(QFrame):
    """One-time 'Yenilikler' card: today's features + jump buttons (closed = remembered)."""

    closed = pyqtSignal()

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("news")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 14, 12, 14)
        lay.setSpacing(7)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label(_("Yenilikler"), "sectionTitle"))
        head.addWidget(label(_("bu güncellemede"), "muted"))
        head.addStretch(1)
        self.btn_close = QToolButton()
        self.btn_close.setObjectName("more")
        self.btn_close.setText("✕")
        self.btn_close.setToolTip(_("Kapat (bir daha gösterilmez)"))
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.clicked.connect(self.closed.emit)
        head.addWidget(self.btn_close)
        lay.addLayout(head)
        self.jump_buttons: dict[str, QPushButton] = {}
        for title, text, page, anchor, btn_text in tx.YENILIKLER:
            row = QHBoxLayout()
            row.setSpacing(10)
            dot = QLabel()
            dot.setObjectName("newsDot")
            dot.setFixedSize(6, 6)
            row.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            lab = label(f"<b>{_(title)}</b> — {_(text)}", wrap=True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(lab, 1)
            if page:
                b = button(_(btn_text) + " →", "link")
                b.clicked.connect(lambda _c=False, p=page, a=anchor: win.jump_to(p, a))
                row.addWidget(b, 0, Qt.AlignmentFlag.AlignVCenter)
                self.jump_buttons[anchor] = b
            lay.addLayout(row)
        foot = QHBoxLayout()
        foot.setContentsMargins(16, 4, 8, 0)
        foot.setSpacing(12)
        self.tray_hint = label(_(tx.YENILIKLER_TEPSI), "small", wrap=True)
        foot.addWidget(self.tray_hint, 1)
        self.btn_ok = button(_("Anladım"))
        self.btn_ok.clicked.connect(self.closed.emit)
        foot.addWidget(self.btn_ok, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(foot)


class _LiveBridge(QObject):
    got = pyqtSignal(object)


class StatusPage(Page):
    def __init__(self, win: "MainWindow"):
        super().__init__(_("Durum"))
        self.win = win
        self._streams_key = None
        self._hero_color = QColor(ui.COLOR_INACTIVE)

        # --- daemon-down banner
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(16, 12, 12, 12)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        t = label(_("Kulak Kesil servisi çalışmıyor"))
        f = t.font()
        f.setBold(True)
        t.setFont(f)
        txt.addWidget(t)
        txt.addWidget(label(_("Başlatınca otomatik EQ geri gelir. Terminalden: {komut}").format(
            komut="systemctl --user start oto-eq"), "muted", wrap=True))
        bl.addLayout(txt, 1)
        self.btn_start = button(_("Servisi başlat"), "danger")
        self.btn_start.clicked.connect(win.start_services)
        bl.addWidget(self.btn_start)
        self.banner.hide()
        self.body.addWidget(self.banner)

        # --- hero
        self.hero = QFrame()
        self.hero.setObjectName("hero")
        hl = QHBoxLayout(self.hero)
        hl.setContentsMargins(26, 24, 26, 24)
        hl.setSpacing(22)
        self.badge = QLabel()
        self.badge.setFixedSize(92, 92)
        hl.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        hv = QVBoxLayout()
        hv.setSpacing(4)
        self.hero_title = label(_("Bağlanıyor…"), "heroTitle")
        self.hero_odak = label("", "heroOdak", wrap=True)
        self.hero_sub = label("", "muted", wrap=True)
        hv.addWidget(self.hero_title)
        hv.addWidget(self.hero_odak)
        self.hero_variant = label("", "muted", wrap=True)
        self.hero_variant.hide()
        hv.addWidget(self.hero_variant)
        hv.addSpacing(6)
        hv.addWidget(self.hero_sub)
        hv.addStretch(1)
        hl.addLayout(hv, 1)
        self.body.addWidget(self.hero)
        self._hero_anim = QVariantAnimation(self)
        self._hero_anim.setDuration(320)
        self._hero_anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._hero_anim.valueChanged.connect(self._paint_hero)

        # --- "Akıllı özellikler" tiles + one-time "Yenilikler" card
        self.smart = SmartRow(win)
        self.smart.hide()
        self.body.addWidget(self.smart)
        self.news = NewsCard(win)
        self.news.closed.connect(self.dismiss_news)
        self.news.setVisible(win.app_settings().get("yenilikler_kapandi") != tx.YENILIKLER_SURUM)
        self.body.addWidget(self.news)
        self._local: dict = {}
        self._live_busy = False
        self._live_ticks = 0
        self._bridge = _LiveBridge(self)
        self._bridge.got.connect(self._live_done)
        self._live_timer = QTimer(self, interval=LIVE_POLL_MS, timeout=self._live_tick)

        # --- CS2 checklist card (only while a CS2 stream is active)
        self.cs2_card = QFrame()
        self.cs2_card.setObjectName("card")
        self.cs2_card.setStyleSheet(f"QFrame#card {{ border-left: 4px solid {ui.PROFILE_COLORS['cs2']}; }}")
        ccl = QVBoxLayout(self.cs2_card)
        ccl.setContentsMargins(18, 12, 12, 12)
        ccl.setSpacing(3)
        ch = QHBoxLayout()
        ch.addWidget(label(_("🎯 CS2 oyun içi ses ayarları"), "sectionTitle"))
        ch.addStretch(1)
        self.btn_cs2_never = QPushButton(_("Bir daha gösterme"))
        self.btn_cs2_never.setObjectName("secondary")
        self.btn_cs2_never.clicked.connect(lambda: self.hide_cs2_card(forever=True))
        self.btn_cs2_hide = QToolButton()
        self.btn_cs2_hide.setObjectName("more")
        self.btn_cs2_hide.setText("✕")
        self.btn_cs2_hide.setToolTip(_("Şimdilik gizle"))
        self.btn_cs2_hide.clicked.connect(lambda: self.hide_cs2_card(forever=False))
        ch.addWidget(self.btn_cs2_never)
        ch.addWidget(self.btn_cs2_hide)
        ccl.addLayout(ch)
        for t, d in CS2_CHECKLIST:
            lab = label(f"✓  <b>{_(t)}</b> — {_(d)}", "small", wrap=True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            ccl.addWidget(lab)
        self.cs2_card.hide()
        self._cs2_session_hidden = False
        self.body.addWidget(self.cs2_card)

        # --- mode selector
        mode_row = QHBoxLayout()
        mode_row.setSpacing(14)
        ml = label(_("Mod"), "sectionTitle")
        mode_row.addWidget(ml)
        self.segment = QFrame()
        self.segment.setObjectName("segment")
        sl = QHBoxLayout(self.segment)
        sl.setContentsMargins(4, 4, 4, 4)
        sl.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: dict[str, QPushButton] = {}
        keys = [("auto", _("Otomatik"))] + [(f"fixed:{p}", ui.profile_label(p)) for p in EQ_PROFILES] \
            + [("off", _("Kapalı"))]
        for key, text in keys:
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            tip = {"auto": _("Her uygulama kendi profilini alır"), "off": _(ui.OFF_EXPLAIN)}
            b.setToolTip(tip.get(key) or _("Tüm sesler {profil} profiliyle (sesli sohbet hariç)").format(profil=text))
            b.clicked.connect(lambda _c=False, k=key: win.select_mode(k))
            self.mode_group.addButton(b)
            sl.addWidget(b)
            self.mode_buttons[key] = b
        mode_row.addWidget(self.segment)
        mode_row.addStretch(1)
        self.body.addLayout(mode_row)

        # --- streams
        self.streams_card = card()
        cl = QVBoxLayout(self.streams_card)
        cl.setContentsMargins(18, 16, 18, 14)
        cl.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(label(_("Çalan uygulamalar"), "sectionTitle"))
        head.addStretch(1)
        self.count_label = label("", "muted")
        head.addWidget(self.count_label)
        cl.addLayout(head)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(2)
        cl.addLayout(self.rows_box)
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 26, 0, 26)
        el.setSpacing(4)
        self.empty_icon = QLabel()
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(self.empty_icon)
        e1 = label(_("Şu an çalan bir şey yok"))
        e1.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = e1.font()
        f.setWeight(QFont.Weight.DemiBold)
        e1.setFont(f)
        el.addWidget(e1)
        e2 = label(_("Bir uygulama ses çalmaya başlayınca burada görünür ve hangi EQ ile çaldığını görürsün."),
                   "muted", wrap=True)
        e2.setAlignment(Qt.AlignmentFlag.AlignCenter)
        el.addWidget(e2)
        cl.addWidget(self.empty)
        self.body.addWidget(self.streams_card)
        self.body.addStretch(1)

    # --- hero colors
    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self._paint_hero(self._hero_color)
        dpr = self.devicePixelRatioF() or 1.0
        pm, p = _pm(56, dpr)
        ui.paint_app_icon(p, 56)
        p.end()
        self.empty_icon.setPixmap(pm)
        fx = QGraphicsOpacityEffect(self.empty_icon)
        fx.setOpacity(0.55)
        self.empty_icon.setGraphicsEffect(fx)
        self._style_segment()

    def _paint_hero(self, color) -> None:
        if not hasattr(self, "theme"):
            return
        c = QColor(color)
        t = self.theme
        gray = abs(c.red() - c.green()) < 24 and abs(c.green() - c.blue()) < 24
        strength = (0.14 if t.dark else 0.08) if gray else (0.30 if t.dark else 0.20)
        self.hero.setStyleSheet(
            f"QFrame#hero {{ border-radius: 18px; border: 1px solid {rgba(c, 0.25 if gray else 0.45)};"
            f" background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {rgba(c, strength)},"
            f" stop:0.65 {t.card.name()}, stop:1 {t.card.name()}); }}")

    def _set_hero_color(self, color: str) -> None:
        new = QColor(color)
        if new == self._hero_color:
            return
        self._hero_anim.stop()
        self._hero_anim.setStartValue(self._hero_color)
        self._hero_anim.setEndValue(new)
        self._hero_color = new
        if self.isVisible():
            self._hero_anim.start()
        else:
            self._paint_hero(new)

    def _style_segment(self) -> None:
        hl = self.theme.highlight.name() if hasattr(self, "theme") else "#3DAEE9"
        colors = {"auto": hl, "off": ui.COLOR_OFF}
        colors.update({f"fixed:{p}": ui.PROFILE_COLORS[p] for p in EQ_PROFILES})
        for key, b in self.mode_buttons.items():
            b.setStyleSheet(f"QPushButton#seg:checked {{ background: {colors[key]}; color: white; }}")

    # --- status
    def set_status(self, st: dict | None) -> None:
        win = self.win
        spec = ui.badge_spec(st)
        dpr = self.devicePixelRatioF() or 1.0
        self.badge.setPixmap(badge_label_pixmap(spec, 92, dpr))
        self.banner.setVisible(st is None and win.seen_down)
        up = st is not None
        for b in self.mode_buttons.values():
            b.setEnabled(up)
        key = win.current_mode_key()
        self.mode_group.setExclusive(False)
        for k, b in self.mode_buttons.items():
            b.setChecked(k == key)
        self.mode_group.setExclusive(True)

        if st is None:
            title = _("Servis çalışmıyor") if win.seen_down else _("Bağlanıyor…")
            odak = _("Sesler şu an EQ'suz çalıyor.") if win.seen_down else ""
        elif st.get("mode") == "off":
            title, odak = _("EQ kapalı"), _("Seviye eşitli düz geçiş: sesler EQ'suz ama dengeli.")
        elif not st.get("active"):
            title = _("Kulaklık aktif değil")
            odak = " ".join(x for x in (tx.kulaklik_degil_text(st), _(tx.KULAKLIK_BEKLE)) if x)
        elif spec.key in EQ_PROFILES:
            title = _("{profil} modu").format(profil=ui.profile_label(spec.key))
            odak = _(ui.PROFILE_NOTIFY_BODY.get(spec.key, ""))
            if st.get("mode") == "fixed":
                odak += " · " + _("sabit mod")
        elif st.get("mode") == "fixed":
            p = st.get("fixed_profile")
            title = _("Sabit: {profil}").format(profil=ui.profile_label(p))
            odak = _("Çalan her şey bu profille çalacak (sesli sohbet ve sistem sesleri hariç).")
        else:
            title, odak = _("Hazır, dinliyor"), _("EQ'lu bir ses çalınca profil burada görünür.")
        self.hero_title.setText(title)
        self.hero_odak.setText(odak)
        self.hero_odak.setVisible(bool(odak))
        vtext = ""
        if st is not None and spec.key in VARIANT_PROFILES:
            v = tx.variant_of(st, spec.key)
            if v:
                vtext = _("Tür: {tur}").format(tur=tx.variant_text(v))
                vodak = variant_odak(spec.key, v.get("group") or "")
                if vodak:
                    vtext += f" — {vodak}"
                self.hero_variant.setToolTip(tx.variant_tooltip(v))
        self.hero_variant.setText(vtext)
        self.hero_variant.setVisible(bool(vtext))
        if st is None:
            self.hero_sub.setText("")
        else:
            ready = _("EQ çıkışları hazır") if st.get("sinks_ready") else _("EQ çıkışları hazır değil ⚠")
            device = tx.kulaklik_kisa(st)
            if device:
                if tx.bt_arama_modu(tx.kulaklik_of(st)):
                    device += f" · {_(tx.ARAMA_MODU)}"
                self.hero_sub.setText(_("Çıkış: {cihaz}  ·  {durum}").format(cihaz=device, durum=ready))
            else:               # older router: no device type, the sink's name only
                sink = win.sink_description(st.get("default_sink") or "")
                self.hero_sub.setText(_("Kulaklık: {cihaz}  ·  {durum}").format(cihaz=sink or "—", durum=ready))
        self.hero_sub.setVisible(bool(self.hero_sub.text()))
        self._set_hero_color(spec.color)
        self._update_smart(st)
        self._update_streams(st)
        self._update_cs2_card(st)

    # --- "Akıllı özellikler"
    def refresh_local(self) -> None:
        """Re-read the local data behind the tiles (kişisel ton, seviye, model, onarım)."""
        self._local = {"ton": load_tone(), "seviye": load_level_info(),
                       "model": load_model_info(), "onarim": load_onarim()}

    def smart_tiles(self, st: dict | None) -> dict:
        if st is None:
            return {}
        if not self._local:
            self.refresh_local()
        loc = self._local
        builders = {"kulaklik": lambda: tx.kulaklik_tile(st),
                    "tur": lambda: tx.genre_tile(st), "kisma": lambda: tx.kisma_tile(st),
                    "ton": lambda: tx.tone_tile(loc.get("ton"), st),
                    "seviye": lambda: tx.level_tile(loc.get("seviye")),
                    "model": lambda: tx.model_tile(loc.get("model")),
                    "onarim": lambda: tx.onarim_tile(loc.get("onarim"))}
        out = {}
        for key, make in builders.items():
            try:
                out[key] = make()
            except Exception as e:  # noqa: BLE001 - odd data hides only that tile
                log.debug("kutucuk %s gösterilemedi: %s", key, e)
                out[key] = None
        return out

    def _update_smart(self, st: dict | None) -> None:
        try:
            datas = self.smart_tiles(st)
            self.smart.set_tiles(datas)
            self.smart.setVisible(any(d is not None for d in datas.values()))
        except Exception:  # noqa: BLE001 - the tiles must never break the status page
            log.exception("akıllı özellik kutucukları güncellenemedi")

    def dismiss_news(self) -> None:
        self.news.hide()
        self.win.update_app_settings({"yenilikler_kapandi": tx.YENILIKLER_SURUM})

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.refresh_local()
        if self.win.status is not None:
            self._update_smart(self.win.status)
        self._live_timer.start()

    def hideEvent(self, e):  # noqa: N802
        super().hideEvent(e)
        self._live_timer.stop()

    def _live_tick(self) -> None:
        """Ducking state and the audio guess change without a status push: ask for them."""
        if self._live_busy or self.win.status is None or not self.isVisible() \
                or self.window().isMinimized():
            return
        self._live_ticks += 1
        if self._live_ticks % LOCAL_EVERY == 0:
            self.refresh_local()
        self._live_busy = True
        req, bridge = self.win._request, self._bridge

        def work():
            try:
                r = req({"cmd": "status"})
            except Exception:  # noqa: BLE001 - router down/busy: the watch thread reports it
                r = None
            try:
                bridge.got.emit(r)
            except RuntimeError:  # page already gone
                pass
        threading.Thread(target=work, name="oto-eq-durum", daemon=True).start()

    def _live_done(self, r) -> None:
        self._live_busy = False
        if self.win.status is None or not isinstance(r, dict) or not r.get("ok"):
            return
        fresh = {k: v for k, v in r.items() if k not in ("ok", "event")}
        self._update_smart(fresh)

    def _update_cs2_card(self, st: dict | None) -> None:
        playing = bool(st) and any(s.get("profile") == "cs2" or s.get("category") == "cs2"
                                   for s in st.get("streams") or [])
        hidden = self._cs2_session_hidden or bool(self.win.app_settings().get("cs2_kart_gizli"))
        self.cs2_card.setVisible(playing and not hidden)

    def hide_cs2_card(self, forever: bool) -> None:
        self._cs2_session_hidden = True
        if forever:
            self.win.update_app_settings({"cs2_kart_gizli": True})
        self.cs2_card.hide()

    def _update_streams(self, st: dict | None) -> None:
        streams = (st or {}).get("streams") or []
        self.streams_card.setVisible(st is not None)
        assigns = config.load_assignments()
        variants = (st or {}).get("variants") or {}
        key = (tuple((s.get("index"), s.get("app"), s.get("binary"), s.get("profile"), s.get("reason"))
                     for s in streams), tuple(sorted((a["alan"], a["deger"].lower()) for a in assigns)),
               repr(sorted((k, sorted(v.items())) for k, v in variants.items() if isinstance(v, dict))))
        if key == self._streams_key:
            return
        self._streams_key = key
        while self.rows_box.count():
            w = self.rows_box.takeAt(0).widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.rows = []
        for s in streams:
            k = assignment_key(s)
            row = StreamRow(self.win, s, find_assignment(assigns, *k) if k else None)
            self.rows.append(row)
            self.rows_box.addWidget(row)
        self.empty.setVisible(not streams)
        self.count_label.setText(_("akış: {n}").format(n=len(streams)) if streams else "")

    def refresh_rows(self) -> None:
        self._streams_key = None
        self._update_streams(self.win.status)


class EqPlaceholderPage(Page):
    def __init__(self, reason: str = ""):
        super().__init__(_("Ekolayzır"), _("Profillerin EQ eğrileri."))
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(28, 40, 28, 40)
        lay.setSpacing(6)
        t = label(_("Ekolayzır sayfası hazırlanıyor"))
        t.setObjectName("sectionTitle")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(t)
        m = label(_("Şimdilik eğrileri görmek için “EQ eğrilerini göster”, ayarlamak için "
                    "profiller.toml dosyasını kullanabilirsin."), "muted", wrap=True)
        m.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(m)
        row = QHBoxLayout()
        row.addStretch(1)
        b1 = button(_("EQ eğrilerini göster"))
        b1.clicked.connect(lambda: ui.launch_module("oto_eq.cli", "eq-ciz"))
        b2 = button(_("profiller.toml'u aç"))
        b2.clicked.connect(lambda: ui.launch_module("oto_eq.cli", "profiller"))
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        lay.addSpacing(8)
        lay.addLayout(row)
        if reason:
            r = label(reason, "small", wrap=True)
            r.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(r)
        self.body.addWidget(c)
        self.body.addStretch(1)


class AssignmentDialog(QDialog):
    def __init__(self, parent, item: dict | None = None, title: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title or _("Atama ekle"))
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(12)
        lay.addWidget(label(_("Bu uygulama her zaman seçtiğin profille çalar; diğer kurallardan önce gelir."),
                            "muted", wrap=True))
        form = QFormLayout()
        form.setSpacing(10)
        self.alan = QComboBox()
        for k, v in ALAN_LABELS.items():
            self.alan.addItem(_(v), k)
        self.deger = QLineEdit()
        self.deger.setPlaceholderText(_("ör. firefox, spotify, cs2"))
        self.profil = QComboBox()
        for k, v in profile_choices():
            self.profil.addItem(dot_icon(ui.PROFILE_COLORS.get(k, ui.COLOR_INACTIVE)), v, k)
        form.addRow(_("Neye göre"), self.alan)
        form.addRow(_("Değer"), self.deger)
        form.addRow(_("Profil"), self.profil)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(_("Kaydet"))
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText(_("Vazgeç"))
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self.reject)
        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        lay.addWidget(bb)
        if item:
            self.alan.setCurrentIndex(max(0, self.alan.findData(item.get("alan"))))
            self.deger.setText(item.get("deger", ""))
            self.profil.setCurrentIndex(max(0, self.profil.findData(item.get("profil", ""))))
        self.deger.textChanged.connect(lambda t: self.ok_button.setEnabled(bool(t.strip())))
        self.ok_button.setEnabled(bool(self.deger.text().strip()))

    def _accept(self):
        if self.deger.text().strip():
            self.accept()

    def value(self) -> dict:
        return {"alan": self.alan.currentData(), "deger": self.deger.text().strip(),
                "profil": self.profil.currentData()}


class GenreDialog(QDialog):
    """Add/edit one artist → tür or game → tür override."""

    def __init__(self, parent, item: dict | None = None, title: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title or _("Tür ataması ekle"))
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 14)
        lay.setSpacing(12)
        lay.addWidget(label(_("Bu sanatçı / oyun her zaman seçtiğin türün ayarıyla çalar; çevrimiçi "
                              "algılamanın önüne geçer."), "muted", wrap=True))
        form = QFormLayout()
        form.setSpacing(10)
        self.kind = QComboBox()
        for k, (_p, text) in tx.GENRE_KINDS.items():
            self.kind.addItem(_(text), k)
        self.key = QLineEdit()
        self.group = QComboBox()
        self.hint = label("", "small", wrap=True)
        form.addRow(_("Tür"), self.kind)
        form.addRow(_("Ad"), self.key)
        form.addRow("", self.hint)
        form.addRow(_("Grup"), self.group)
        lay.addLayout(form)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(_("Kaydet"))
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText(_("Vazgeç"))
        bb.accepted.connect(lambda: self.key.text().strip() and self.accept())
        bb.rejected.connect(self.reject)
        self.ok_button = bb.button(QDialogButtonBox.StandardButton.Ok)
        lay.addWidget(bb)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.key.textChanged.connect(lambda t: self.ok_button.setEnabled(bool(t.strip())))
        item = item or {}
        self.kind.setCurrentIndex(max(0, self.kind.findData(item.get("kind", "sanatci"))))
        self._kind_changed()
        self.key.setText(item.get("key", ""))
        if "group" in item:
            self.group.setCurrentIndex(max(0, self.group.findData(item["group"])))
        self.ok_button.setEnabled(bool(self.key.text().strip()))

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        prof = tx.GENRE_KINDS[kind][0]
        cur = self.group.currentData()
        self.group.clear()
        for g in variant_groups(prof):
            self.group.addItem(_(GROUP_LABELS.get(g, g)), g)
        self.group.addItem(_("Genel"), "")
        if cur is not None:
            self.group.setCurrentIndex(max(0, self.group.findData(cur)))
        if kind == "sanatci":
            self.key.setPlaceholderText(_("ör. {ornek}").format(ornek="Virgin Black"))
            self.hint.setText(_("Sanatçı adı; büyük/küçük harf fark etmez."))
        else:
            self.key.setPlaceholderText(_("ör. {ornek}").format(ornek="steam:2694490"))
            self.hint.setText(_("Oyun kimliği: steam:<uygulama no> ya da exe:<program adı>."))

    def value(self) -> dict:
        kind = self.kind.currentData()
        key = self.key.text().strip()
        if kind == "sanatci":
            key = tx.normalize_artist(key)
        else:
            key = tx.normalize_game_key(key)
        return {"kind": kind, "key": key, "group": self.group.currentData()}


class GenreSection(QFrame):
    """Kurallar → "Tür atamaları": artist/game → tür overrides + cache reset."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("card")
        self.win = win
        self.confirm = lambda text: QMessageBox.question(
            self, _("Onay"), text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(10)
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label(_("Tür atamaları"), "sectionTitle"))
        head.addWidget(label(_("Müzik sanatçıya, oyun türüne göre küçük ayar farkı alır (şarkı/oyun "
                               "değişince). Yanlış algılanırsa buradan düzelt."), "muted", wrap=True))
        lay.addLayout(head)
        bar = ui.FlowLayout(spacing=8)      # long translations wrap (no horizontal scroll)
        self.btn_add = button(_("Ekle"), "primary")
        self.btn_add.clicked.connect(lambda: self.edit_item(None))
        self.btn_from = QToolButton()
        self.btn_from.setObjectName("secondary")
        self.btn_from.setText(_("Şu an çalandan ekle ▾"))
        self.btn_from.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_from.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.from_menu = QMenu(self.btn_from)
        self.from_menu.aboutToShow.connect(self.fill_from_menu)
        self.btn_from.setMenu(self.from_menu)
        self.btn_edit = button(_("Düzenle"))
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_del = button(_("Sil"))
        self.btn_del.clicked.connect(self.delete_selected)
        self.btn_cache = button(_("Tür önbelleğini temizle"))
        self.btn_cache.setToolTip(_("Çevrimiçi bulunan ve ses analiziyle öğrenilen türleri unutur; "
                                    "bir dahaki çalışta yeniden bulunur"))
        self.btn_cache.clicked.connect(self.clear_cache)
        for b in (self.btn_add, self.btn_from, self.btn_edit, self.btn_del, self.btn_cache):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([_("Tür"), _("Ad"), _("Grup")])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.doubleClicked.connect(lambda _i: self.edit_selected())
        self.table.itemSelectionChanged.connect(self._sel_changed)
        lay.addWidget(self.table)
        self.empty = label(_("Henüz tür ataması yok."), "muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.empty)
        self.reload()

    def items(self) -> list[dict]:
        data = config.load_genre_settings()
        out = []
        for kind in ("sanatci", "oyun"):
            for k, g in sorted((data.get(kind) or {}).items()):
                out.append({"kind": kind, "key": k, "group": g})
        return out

    def reload(self) -> None:
        items = self.items()
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            prof = tx.GENRE_KINDS[it["kind"]][0]
            cell = QTableWidgetItem(_(tx.GENRE_KINDS[it["kind"]][1]))
            cell.setData(Qt.ItemDataRole.UserRole, dict(it))   # what this row shows
            self.table.setItem(r, 0, cell)
            self.table.setItem(r, 1, QTableWidgetItem(it["key"]))
            self.table.setItem(r, 2, QTableWidgetItem(dot_icon(ui.PROFILE_COLORS[prof]),
                                                      tx.group_label(it["group"])))
        hdr = self.table.horizontalHeader().sizeHint().height()
        self.table.setFixedHeight(hdr + 40 * min(max(len(items), 1), 8) + 4)
        self.table.setVisible(bool(items))
        self.empty.setVisible(not items)
        self._sel_changed()

    def _sel_changed(self) -> None:
        has = bool(self.table.selectionModel().selectedRows())
        self.btn_edit.setEnabled(has)
        self.btn_del.setEnabled(has)

    def _selected(self) -> dict | None:
        # the item the row SHOWS, not items()[row]: the tray may have changed the file since
        # (index → wrong entry deleted/edited, or IndexError)
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        cell = self.table.item(rows[0].row(), 0)
        it = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        return dict(it) if isinstance(it, dict) else None

    def write(self, remove: dict | None, add: dict | None, what: str) -> None:
        data = config.load_genre_settings()
        if remove:
            (data.get(remove["kind"]) or {}).pop(remove["key"], None)
        if add:
            data.setdefault(add["kind"], {})[add["key"]] = add["group"]
        try:
            config.save_genre_settings(data)
        except OSError as e:
            self.win.toast.show_text(_("Kaydedilemedi: {hata}").format(hata=e))
            return
        self.reload()
        ok = self.win.reload_quietly()
        self.win.toast.show_text(f"{what} ✓" if ok else self.win.not_applied_text(what))

    def edit_item(self, item: dict | None, new: bool = True, dialog_cls=GenreDialog) -> bool:
        dlg = dialog_cls(self, item, _("Tür ataması ekle") if new else _("Tür atamasını düzenle"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        v = dlg.value()
        if not v["key"]:
            return False
        self.write(None if new else item, v, f"{v['key']} → {tx.group_label(v['group'])}")
        return True

    def edit_selected(self) -> None:
        it = self._selected()
        if it:
            self.edit_item(it, new=False)

    def delete_selected(self) -> None:
        it = self._selected()
        if it:
            self.write(it, None, _("“{ad}” tür ataması silindi").format(ad=it["key"]))

    def fill_from_menu(self) -> None:
        self.from_menu.clear()
        found = False
        for prof, kind in (("muzik", "sanatci"), ("oyun", "oyun")):
            v = tx.variant_of(self.win.status, prof)
            if not v or not v.get("for"):
                continue
            key = tx.variant_override_key(v, kind)
            act = self.from_menu.addAction(dot_icon(ui.PROFILE_COLORS[prof]),
                                           _("{tur}: {ad} — şu an {grup}").format(
                                               tur=_(tx.GENRE_KINDS[kind][1]), ad=v["for"],
                                               grup=tx.group_label(v.get("group"))))
            act.triggered.connect(lambda _c=False, k=kind, kk=key, g=v.get("group") or "":
                                  self.edit_item({"kind": k, "key": kk, "group": g}))
            found = True
        if not found:
            a = self.from_menu.addAction(_("Şu an tanınan şarkı ya da oyun yok"))
            a.setEnabled(False)

    def clear_cache(self) -> bool:
        path = config.GENRE_CACHE
        if not self.confirm(_("Tür önbelleği silinsin mi? Çevrimiçi bulunan türler bir dahaki çalışta "
                              "yeniden sorulur. Elle yaptığın atamalar silinmez.")):
            return False
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            self.win.toast.show_text(_("Silinemedi: {hata}").format(hata=e))
            return False
        ok = self.win.reload_quietly()
        self.win.toast.show_text(_("Tür önbelleği temizlendi ✓") if ok
                                 else self.win.not_applied_text(_("Tür önbelleği temizlendi")))
        return True


class RecentRow(QFrame):
    """One recently seen app: avatar, name, last category/profile, count, when, Ata ▾."""

    def __init__(self, win: "MainWindow", entry: dict, assigns: list[dict]):
        super().__init__()
        self.setObjectName("row")
        self.entry = entry
        self.stream = history_as_stream(entry)
        prof = entry.get("profil")
        color = ui.PROFILE_COLORS.get(prof, ui.COLOR_INACTIVE)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 6, 6)
        lay.setSpacing(12)
        dpr = win.devicePixelRatioF() or 1.0
        icon = QLabel()
        icon.setPixmap(app_avatar(self.stream, color, 30, dpr))
        icon.setFixedSize(30, 30)
        lay.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = self.stream["app"] or self.stream["binary"] or self.stream["flatpak"] or "?"
        nl = label(name)
        f = nl.font()
        f.setWeight(QFont.Weight.DemiBold)
        nl.setFont(f)
        names.addWidget(nl)
        bits = [b for b in (self.stream["binary"], self.stream["flatpak"]) if b and b != name]
        cat = entry.get("kategori") or ""
        if cat:
            bits.append(_(CATEGORY_LABELS[cat]) if cat in CATEGORY_LABELS else cat)
        k = assignment_key(self.stream)
        self.assigned = find_assignment(assigns, *k) if k else None
        if self.assigned is not None:
            bits.append(_("📌 atandı"))
        names.addWidget(label(" · ".join(bits), "small"))
        lay.addLayout(names, 2)
        n = int(entry.get("sayi") or 0)
        when = label(_("{zaman} · çalma: {n}").format(n=n, zaman=ago(entry.get("son") or 0)), "small")
        when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        when.setToolTip(entry.get("neden") or "")
        lay.addWidget(when, 1)
        c = chip(ui.assignment_profile_label(prof), color)
        c.setToolTip(_("{profil} profili").format(profil=ui.profile_label(prof)) if prof else _(ui.FLAT_TOOLTIP))
        c.setMinimumWidth(78)
        lay.addWidget(c)
        self.btn = QToolButton()
        self.btn.setObjectName("secondary")
        self.btn.setText(_("Ata ▾"))
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = win.assignment_menu(self.stream, self)
        self.btn.setMenu(self.menu)
        lay.addWidget(self.btn)


class RulesPage(Page):
    def __init__(self, win: "MainWindow"):
        super().__init__(_("Kurallar"), _("Uygulama atamaların. Buradaki atamalar diğer tüm otomatik "
                                          "kurallardan önce uygulanır."))
        self.win = win
        c = card()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(16, 14, 16, 14)
        cl.setSpacing(10)
        bar = ui.FlowLayout(spacing=8)
        self.btn_add = button(_("Ekle"), "primary")
        self.btn_add.clicked.connect(lambda: self.edit_item(None))
        self.btn_from = QToolButton()
        self.btn_from.setObjectName("secondary")
        self.btn_from.setText(_("Çalan uygulamadan ekle ▾"))
        self.btn_from.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_from.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.from_menu = QMenu(self.btn_from)
        self.from_menu.aboutToShow.connect(self._fill_from_menu)
        self.btn_from.setMenu(self.from_menu)
        self.btn_edit = button(_("Düzenle"))
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_del = button(_("Sil"))
        self.btn_del.clicked.connect(self.delete_selected)
        for b in (self.btn_add, self.btn_from, self.btn_edit, self.btn_del):
            bar.addWidget(b)
        cl.addLayout(bar)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels([_("Neye göre"), _("Değer"), _("Profil")])
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.doubleClicked.connect(lambda _i: self.edit_selected())
        self.table.itemSelectionChanged.connect(self._sel_changed)
        cl.addWidget(self.table)
        self.empty = label(_("Henüz atama yok. “Çalan uygulamadan ekle” ile başlayabilirsin."), "muted",
                           wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(self.empty)
        self.body.addWidget(c)

        adv = card()
        al = QHBoxLayout(adv)
        al.setContentsMargins(18, 14, 18, 14)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        txt.addWidget(label(_("Gelişmiş kurallar"), "sectionTitle"))
        txt.addWidget(label(_("Oyun / müzik / film tanıma kuralları kurallar.toml dosyasındadır. "
                              "Düzenledikten sonra “Yeniden yükle”ye bas."), "muted", wrap=True))
        al.addLayout(txt, 1)
        b_open = button(_("Gelişmiş kural dosyasını aç"))
        b_open.clicked.connect(self.open_rules_file)
        b_reload = button(_("Yeniden yükle"))
        b_reload.clicked.connect(lambda: win.reload_rules(announce=True))
        al.addWidget(b_open)
        al.addWidget(b_reload)

        # --- recently seen apps (oto_eq.history)
        self.recent_card = card()
        rl = QVBoxLayout(self.recent_card)
        rl.setContentsMargins(18, 14, 18, 12)
        rl.setSpacing(4)
        rh = QHBoxLayout()
        rt = QVBoxLayout()
        rt.setSpacing(2)
        rt.addWidget(label(_("Son görülen uygulamalar"), "sectionTitle"))
        rt.addWidget(label(_("Ses çalmış uygulamalar ve en son hangi profille çaldıkları. "
                             "“Ata” ile kalıcı profil ver."), "muted", wrap=True))
        rh.addLayout(rt, 1)
        self.recent_count = label("", "muted")
        rh.addWidget(self.recent_count, 0, Qt.AlignmentFlag.AlignTop)
        rl.addLayout(rh)
        self.recent_box = QVBoxLayout()
        self.recent_box.setSpacing(2)
        rl.addLayout(self.recent_box)
        self.recent_empty = label(_("Henüz kayıt yok — bir şey çalınca burada görünür."), "muted",
                                  wrap=True)
        self.recent_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rl.addWidget(self.recent_empty)
        self.recent_more = button(_("Tümünü göster"))
        self.recent_more.clicked.connect(self._toggle_recent_all)
        rl.addWidget(self.recent_more, 0, Qt.AlignmentFlag.AlignHCenter)
        self._recent_all = False
        self.recent_rows: list[QWidget] = []

        self.genre = GenreSection(win)
        self.body.addWidget(self.genre)
        self.body.addWidget(self.recent_card)
        self.body.addWidget(adv)
        self.body.addStretch(1)
        self.reload_table()

    def items(self) -> list[dict]:
        return config.load_assignments()

    RECENT_SHORT = 8

    def _toggle_recent_all(self) -> None:
        self._recent_all = not self._recent_all
        self.reload_recent()

    def reload_recent(self) -> None:
        entries = load_recent(50)
        self.recent_card.setVisible(entries is not None)
        if entries is None:
            return
        for w in self.recent_rows:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self.recent_rows = []
        assigns = self.items()
        shown = entries if self._recent_all else entries[: self.RECENT_SHORT]
        for e in shown:
            row = RecentRow(self.win, e, assigns)
            self.recent_rows.append(row)
            self.recent_box.addWidget(row)
        self.recent_empty.setVisible(not entries)
        self.recent_count.setText(_("uygulama: {n}").format(n=len(entries)) if entries else "")
        extra = len(entries) - self.RECENT_SHORT
        self.recent_more.setVisible(extra > 0)
        self.recent_more.setText(_("Daha az göster") if self._recent_all
                                  else _("Tümünü göster (+{n})").format(n=extra))

    def reload_table(self) -> None:
        items = self.items()
        self.table.setRowCount(len(items))
        for r, a in enumerate(items):
            cell = QTableWidgetItem(_(ALAN_LABELS[a["alan"]]) if a["alan"] in ALAN_LABELS
                                    else a["alan"])
            cell.setData(Qt.ItemDataRole.UserRole, dict(a))     # what this row shows
            self.table.setItem(r, 0, cell)
            self.table.setItem(r, 1, QTableWidgetItem(a["deger"]))
            p = a["profil"]
            it = QTableWidgetItem(dot_icon(ui.PROFILE_COLORS.get(p, ui.COLOR_INACTIVE)),
                                  ui.assignment_profile_label(p))
            self.table.setItem(r, 2, it)
        hdr = self.table.horizontalHeader().sizeHint().height()
        self.table.setFixedHeight(hdr + 40 * min(max(len(items), 1), 10) + 4)
        self.table.setVisible(bool(items))
        self.empty.setVisible(not items)
        self._sel_changed()
        self.reload_recent()
        if hasattr(self, "genre"):
            self.genre.reload()

    def _sel_changed(self) -> None:
        has = bool(self.table.selectionModel().selectedRows())
        self.btn_edit.setEnabled(has)
        self.btn_del.setEnabled(has)

    def _selected(self) -> dict | None:
        """The assignment the selected row shows (not items()[row]: the file may have
        changed since the table was filled)."""
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        cell = self.table.item(rows[0].row(), 0)
        a = cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None
        return dict(a) if isinstance(a, dict) else None

    def _fill_from_menu(self) -> None:
        self.from_menu.clear()
        streams = (self.win.status or {}).get("streams") or []
        if not streams:
            a = self.from_menu.addAction(_("Şu an çalan uygulama yok"))
            a.setEnabled(False)
            return
        for s in streams:
            k = assignment_key(s)
            if not k:
                continue
            name = s.get("app") or k[1]
            act = self.from_menu.addAction(
                dot_icon(ui.PROFILE_COLORS.get(s.get("profile"), ui.COLOR_INACTIVE)),
                _("{ad}  ({deger}) — şu an {profil}").format(ad=name, deger=k[1],
                                                              profil=ui.profile_label(s.get("profile"))))
            act.triggered.connect(lambda _c=False, kk=k, pp=s.get("profile") or "":
                                  self.edit_item({"alan": kk[0], "deger": kk[1], "profil": pp}, new=True))

    def edit_item(self, item: dict | None, new: bool = True, dialog_cls=AssignmentDialog) -> bool:
        dlg = dialog_cls(self, item, _("Atama ekle") if new else _("Atamayı düzenle"))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        v = dlg.value()
        items = self.items()
        if not new and item:
            items = remove_assignment(items, item["alan"], item["deger"])
        plabel = ui.assignment_profile_label(v["profil"])
        self.win.write_assignments(upsert_assignment(items, v["alan"], v["deger"], v["profil"]),
                                   f"“{v['deger']}” → {plabel}")
        return True

    def edit_selected(self) -> None:
        a = self._selected()
        if a is not None:
            self.edit_item(a, new=False)

    def delete_selected(self) -> None:
        a = self._selected()
        if a is None:
            return
        self.win.write_assignments(remove_assignment(self.items(), a["alan"], a["deger"]),
                                   _("“{ad}” ataması silindi").format(ad=a["deger"]))

    def open_rules_file(self) -> None:
        dst = config.CONFIG_DIR / config.RULES_FILE
        try:
            if not dst.exists():
                import shutil
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(config.DEFAULTS_DIR / config.RULES_FILE, dst)
        except OSError as e:
            self.win.toast.show_text(_("Kural dosyası hazırlanamadı: {hata}").format(hata=e))
            return
        ui.xdg_open(dst)
        self.win.toast.show_text(_("Kaydettikten sonra “Yeniden yükle”ye bas"))


class SettingsPage(Page):
    def __init__(self, win: "MainWindow"):
        super().__init__(_("Ayarlar"))
        self.win = win
        # restart prompt after a language change (tests replace it)
        self.confirm_restart = lambda: QMessageBox.question(
            self, _("Dil"), _("Dil değiştirildi. Uygulama ve tepsi simgesi şimdi yeniden başlatılsın mı? "
                              "(Arka plan servisinin metinleri bir sonraki oturum açılışında ya da "
                              "servis yeniden başlayınca değişir.)"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes) == QMessageBox.StandardButton.Yes

        # --- general toggles
        g = card()
        gl = QVBoxLayout(g)
        gl.setContentsMargins(18, 14, 18, 14)
        gl.setSpacing(4)
        gl.addWidget(label(_("Genel"), "sectionTitle"))
        self.lang_combo = QComboBox()
        self.lang_combo.addItem(_("Otomatik (sistem dili: {dil})").format(
            dil=i18n.DILLER.get(i18n.sistem_dili(), i18n.sistem_dili())), i18n.OTOMATIK)
        for code, name in i18n.DILLER.items():
            oran = i18n.kapsama(code)                # unfinished catalog: rest stays Turkish
            if oran is not None and oran < 0.95:
                name = f"{name} ({oran:.0%})"
            self.lang_combo.addItem(name, code)      # each language in its own name (not translated)
        self._select_language(i18n.ayar_oku())
        self.lang_combo.activated.connect(self._language_activated)
        gl.addWidget(self._setting_row(_("Dil"), _("Arayüzün dili. Değişiklik uygulama yeniden "
                                                   "başlayınca geçerli olur."), self.lang_combo))
        sep0 = QFrame()
        sep0.setObjectName("separator")
        sep0.setFixedHeight(1)
        gl.addWidget(sep0)
        self.sw_notify = ToggleSwitch()
        self.sw_notify.setChecked(ui.load_tray_settings()["bildirimler"])
        self.sw_notify.toggled.connect(self.set_notifications)
        gl.addWidget(self._setting_row(_("Profil değişince bildirim göster"),
                                       _("Örn. “🎯 CS2 modu — adım sesleri öne çıkarıldı”. Tepsiyle ortak ayar."),
                                       self.sw_notify))
        sep = QFrame()
        sep.setObjectName("separator")
        sep.setFixedHeight(1)
        gl.addWidget(sep)
        self.sw_autostart = ToggleSwitch()
        self.sw_autostart.setChecked(ui.autostart_enabled())
        self.sw_autostart.toggled.connect(self.set_autostart)
        gl.addWidget(self._setting_row(_("Tepsi simgesi oturum açılışında başlasın"),
                                       _("Panelde Kulak Kesil simgesi (sol tık bu pencereyi açar)."),
                                       self.sw_autostart))
        sep2 = QFrame()
        sep2.setObjectName("separator")
        sep2.setFixedHeight(1)
        gl.addWidget(sep2)
        genre = config.load_genre_settings()
        self.sw_online = ToggleSwitch()
        self.sw_online.setChecked(bool(genre.get("cevrimici", True)))
        self.sw_online.toggled.connect(self.set_online)
        gl.addWidget(self._setting_row(_("Çevrimiçi tür algılama"),
                                       _("Müzik: iTunes, Deezer, YouTube etiketleri, MusicBrainz · "
                                         "Oyun: Steam. Sadece sanatçı + şarkı adı / YouTube video "
                                         "kimliği / oyun kimliği gönderilir."),
                                       self.sw_online))
        sep3 = QFrame()
        sep3.setObjectName("separator")
        sep3.setFixedHeight(1)
        gl.addWidget(sep3)
        self.sw_audio = ToggleSwitch()
        self.sw_audio.setChecked(bool(genre.get("ses_analizi", True)))
        self.sw_audio.toggled.connect(self.set_audio_analysis)
        gl.addWidget(self._setting_row(_("Ses analizi"),
                                       _("Adı hiçbir kaynakta bulunamayan şarkıların türünü Müzik "
                                         "çıkışındaki sesi bu bilgisayarda dinleyerek tahmin eder "
                                         "(ör. küçük YouTube yapımcıları). Ses kaydedilmez, gönderilmez."),
                                       self.sw_audio))
        self.genre_sources = label(f"{_(tx.GENRE_SOURCES_TEXT)}\n{_(tx.GENRE_PRIVACY_TEXT)}", "small", wrap=True)
        self.genre_sources.setContentsMargins(0, 4, 0, 4)
        gl.addWidget(self.genre_sources)
        self.body.addWidget(g)

        # --- services
        s = card()
        sl = QVBoxLayout(s)
        sl.setContentsMargins(18, 14, 18, 14)
        sl.setSpacing(6)
        sh = QHBoxLayout()
        sh.addWidget(label(_("Servisler"), "sectionTitle"))
        sh.addStretch(1)
        sl.addLayout(sh)
        self.svc_widgets: dict[str, tuple[QLabel, QLabel, QPushButton, QPushButton]] = {}
        for unit in SERVICES:
            row = QHBoxLayout()
            row.setSpacing(10)
            dot = QLabel()
            dot.setFixedSize(12, 12)
            name = label(_(SERVICE_LABELS[unit]))
            state = label("…", "muted")
            b_start = button(_("Başlat"))
            b_start.clicked.connect(lambda _c=False, u=unit: self.service_action("start", u))
            b_restart = button(_("Yeniden başlat"))
            b_restart.clicked.connect(lambda _c=False, u=unit: self.service_action("restart", u))
            row.addWidget(dot)
            row.addWidget(name)
            row.addWidget(state)
            row.addStretch(1)
            row.addWidget(b_start)
            row.addWidget(b_restart)
            sl.addLayout(row)
            self.svc_widgets[unit] = (dot, state, b_start, b_restart)
        self.body.addWidget(s)
        self._svc_timer = QTimer(self, interval=4000, timeout=self.refresh_services)

        # --- EQ regenerate
        e = card()
        el = QHBoxLayout(e)
        el.setContentsMargins(18, 14, 18, 14)
        et = QVBoxLayout()
        et.setSpacing(2)
        et.addWidget(label(_("EQ yapılandırması"), "sectionTitle"))
        et.addWidget(label(_("profiller.toml değiştiyse EQ çıkışlarını yeniden üretir "
                             "({komut} ile aynı). Ses bir anlığına kesilebilir.").format(komut="kulak eq-uret"),
                           "muted", wrap=True))
        el.addLayout(et, 1)
        self.btn_regen = button(_("Yeniden üret"))
        self.btn_regen.clicked.connect(self.regenerate)
        el.addWidget(self.btn_regen)
        self.body.addWidget(e)

        # --- health check
        self.saglik = load_saglik()
        hc = card()
        hl = QVBoxLayout(hc)
        hl.setContentsMargins(18, 14, 18, 14)
        hl.setSpacing(6)
        hh = QHBoxLayout()
        ht = QVBoxLayout()
        ht.setSpacing(2)
        ht.addWidget(label(_("Sağlık kontrolü"), "sectionTitle"))
        ht.addWidget(label(_("Servisleri, EQ çıkışlarını, eklentileri ve ses kesintilerini denetler. "
                             "Hiçbir şeyi değiştirmez."), "muted", wrap=True))
        hh.addLayout(ht, 1)
        self.btn_health = button(_("Kontrol et"))
        self.btn_health.clicked.connect(self.run_health)
        hh.addWidget(self.btn_health, 0, Qt.AlignmentFlag.AlignTop)
        hl.addLayout(hh)
        self.health_box = QVBoxLayout()
        self.health_box.setSpacing(0)
        hl.addLayout(self.health_box)
        self.health_summary = label("", "muted", wrap=True)
        self.health_summary.hide()
        hl.addWidget(self.health_summary)
        self.health_rows: list[QWidget] = []
        self._health_thread: FuncThread | None = None
        hc.setVisible(self.saglik is not None)
        self.health_card = hc
        self.body.addWidget(hc)

        # --- setup guide
        gcard = card()
        gl2 = QHBoxLayout(gcard)
        gl2.setContentsMargins(18, 14, 18, 14)
        gt = QVBoxLayout()
        gt.setSpacing(2)
        gt.addWidget(label(_("Kurulum rehberi"), "sectionTitle"))
        gt.addWidget(label(_("Ses seviyesi, CS2 ve Discord ayarları, mikrofon testi: dört kısa adım."),
                           "muted", wrap=True))
        gl2.addLayout(gt, 1)
        self.btn_guide = button(_("Rehberi aç"))
        self.btn_guide.clicked.connect(lambda: win.show_welcome())
        gl2.addWidget(self.btn_guide)
        self.body.addWidget(gcard)

        # --- uninstall info (only for install.sh installs; a distro package (AUR) has no uninstall.sh:
        # the package manager removes it, so the card would point to a missing file)
        uninstall_sh = config.SRC_DIR / "uninstall.sh"
        self.uninstall_cmd = str(uninstall_sh).replace(str(Path.home()), "~", 1)
        if uninstall_sh.is_file():
            u = card()
            ul = QVBoxLayout(u)
            ul.setContentsMargins(18, 14, 18, 14)
            ul.setSpacing(6)
            ul.addWidget(label(_("Kulak Kesil'i kaldır"), "sectionTitle"))
            ul.addWidget(label(_("Servisleri, komutları ve kısayolları kaldırmak için terminalde çalıştır. "
                                 "Ayarların korunur; tamamen silmek için sonuna {secenek} ekle.").format(
                                     secenek="--purge"), "muted", wrap=True))
            ur = QHBoxLayout()
            mono = label(self.uninstall_cmd, "mono")
            mono.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            ur.addWidget(mono)
            b_copy = button(_("Kopyala"))
            b_copy.clicked.connect(self.copy_uninstall)
            ur.addWidget(b_copy)
            ur.addStretch(1)
            ul.addLayout(ur)
            self.body.addWidget(u)

        # --- about
        a = card()
        al = QHBoxLayout(a)
        al.setContentsMargins(18, 16, 18, 16)
        al.setSpacing(16)
        self.about_icon = QLabel()
        self.about_icon.setFixedSize(64, 64)
        al.addWidget(self.about_icon, 0, Qt.AlignmentFlag.AlignTop)
        at = QVBoxLayout()
        at.setSpacing(2)
        at.addWidget(label(f"{ui.APP_NAME}", "sectionTitle"))
        at.addWidget(label(_("{slogan} · sürüm {surum}").format(slogan=_(ui.APP_TAGLINE), surum=ui.APP_VERSION),
                           "muted"))
        at.addWidget(label(_("Kulaklık için bağlama göre otomatik ekolayzır: CS2'de adım sesi, "
                             "filmde diyalog, müzikte tını, oyunda atmosfer. USB, Bluetooth ve 3,5 mm "
                             "kulaklıklarda çalışır; HS80 düzeltme eğrisi yalnız Corsair HS80'de "
                             "uygulanır. Discord ve sistem sesleri EQ'suz kalır."), "small", wrap=True))
        al.addLayout(at, 1)
        self.body.addWidget(a)
        self.body.addStretch(1)

    @staticmethod
    def _setting_row(title: str, desc: str, control: QWidget) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 8, 0, 8)
        t = QVBoxLayout()
        t.setSpacing(1)
        t.addWidget(label(title))
        t.addWidget(label(desc, "small", wrap=True))
        lay.addLayout(t, 1)
        lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return w

    def apply_theme(self, theme: Theme) -> None:
        for sw in (self.sw_notify, self.sw_autostart, self.sw_online, self.sw_audio):
            sw.on_color = theme.highlight
            sw.update()
        dpr = self.devicePixelRatioF() or 1.0
        pm, p = _pm(64, dpr)
        ui.paint_app_icon(p, 64)
        p.end()
        self.about_icon.setPixmap(pm)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.sync_from_disk()
        self.refresh_services()
        self._svc_timer.start()

    def hideEvent(self, e):  # noqa: N802
        super().hideEvent(e)
        self._svc_timer.stop()

    def sync_from_disk(self) -> None:
        genre = config.load_genre_settings()
        for sw, val in ((self.sw_notify, ui.load_tray_settings()["bildirimler"]),
                        (self.sw_autostart, ui.autostart_enabled()),
                        (self.sw_online, bool(genre.get("cevrimici", True))),
                        (self.sw_audio, bool(genre.get("ses_analizi", True)))):
            if sw.isChecked() != val:
                sw.blockSignals(True)
                sw.setChecked(val)
                sw.blockSignals(False)
        self._select_language(i18n.ayar_oku())

    # --- interface language (CHANGE 8)
    def _select_language(self, code: str) -> None:
        i = self.lang_combo.findData(code)
        self.lang_combo.blockSignals(True)
        self.lang_combo.setCurrentIndex(max(0, i))
        self.lang_combo.blockSignals(False)

    def _language_activated(self, index: int) -> None:
        self.set_language(self.lang_combo.itemData(index))

    def set_language(self, code: str) -> bool:
        """Save the choice; offer to restart the app + tray (True = restart requested)."""
        if code == i18n.ayar_oku():
            return False
        try:
            i18n.ayar_yaz(code)
        except (OSError, ValueError) as e:
            self.win.toast.show_text(_("Kaydedilemedi: {hata}").format(hata=e))
            self._select_language(i18n.ayar_oku())
            return False
        if not self.confirm_restart():
            self.win.toast.show_text(_("Yeni dil bir sonraki açılışta geçerli olur."))
            return False
        self.win.language_changed_quietly()     # router texts follow (GUI + tray restart now)
        self.win.restart_app()
        return True

    def _save_genre_flag(self, key: str, on: bool, name: str) -> bool:
        """Persist one tur-atamalari.json flag and push it to the router LIVE
        (IPC ``reload`` → router re-reads genre settings; no service restart)."""
        data = config.load_genre_settings()
        data[key] = bool(on)
        try:
            config.save_genre_settings(data)
        except OSError as e:
            self.win.toast.show_text(_("Kaydedilemedi: {hata}").format(hata=e))
            self.sync_from_disk()
            return False
        live = self.win.reload_quietly()
        text = (_("{ozellik} açık") if on else _("{ozellik} kapalı")).format(ozellik=name)
        self.win.toast.show_text(text if live else self.win.not_applied_text(text))
        return live

    def set_online(self, on: bool) -> None:
        self._save_genre_flag("cevrimici", on, _("Çevrimiçi tür algılama"))

    def set_audio_analysis(self, on: bool) -> None:
        self._save_genre_flag("ses_analizi", on, _("Ses analizi"))

    def set_notifications(self, on: bool) -> None:
        ui.save_tray_settings({"bildirimler": on})

    def set_autostart(self, on: bool) -> None:
        if not ui.set_autostart(on):
            self.win.toast.show_text(_("Otomatik başlatma değiştirilemedi"))
            self.sync_from_disk()

    def refresh_services(self) -> None:
        self.win.runner.run(systemctl_args("is-active", *SERVICES), self._services_done, timeout_ms=5000)

    def _services_done(self, _rc: int, out: str, _err: str) -> None:
        states = out.split()
        for i, unit in enumerate(SERVICES):
            st = states[i] if i < len(states) else "unknown"
            self.set_service_state(unit, st)

    def set_service_state(self, unit: str, st: str) -> None:
        dot, lab, b_start, _b_restart = self.svc_widgets[unit]
        text, color = STATE_TEXT.get(st, (N_("Bilinmiyor"), ui.COLOR_INACTIVE))
        dpr = self.devicePixelRatioF() or 1.0
        dot.setPixmap(dot_icon(color, 12, dpr).pixmap(QSize(12, 12), dpr))
        lab.setText(_(text))
        b_start.setEnabled(st != "active")

    def service_action(self, action: str, unit: str) -> None:
        msg = _("{servis}: başlatılıyor…") if action == "start" else _("{servis}: yeniden başlatılıyor…")
        self.win.toast.show_text(msg.format(servis=_(SERVICE_LABELS[unit])))

        def done(rc, _o, err):
            self.win.toast.show_text(_("Tamam") if rc == 0 else _("Olmadı: {hata}").format(hata=err.strip()[:120]))
            self.refresh_services()
        self.win.runner.run(systemctl_args(action, unit), done, timeout_ms=30000)

    def regenerate(self) -> None:
        self.btn_regen.setEnabled(False)
        self.btn_regen.setText(_("Üretiliyor…"))

        def done(rc, out, err):
            self.btn_regen.setEnabled(True)
            self.btn_regen.setText(_("Yeniden üret"))
            if rc == 0:
                self.win.toast.show_text(_("EQ yeniden üretildi ✓"))
            elif rc == 3:
                self.win.toast.show_text(_("EQ üretildi, ama servis çalışmıyor"))
            else:
                last = (err.strip().splitlines() or [_("bilinmeyen hata")])[-1]
                self.win.toast.show_text(_("EQ üretilemedi: {hata}").format(hata=last[:140]))
            self.refresh_services()
        self.win.runner.run([sys.executable, "-m", "oto_eq.cli", "eq-uret"], done,
                            env=py_child_env(), timeout_ms=120000)

    def run_health(self) -> None:
        if self.saglik is None or self._health_thread is not None:
            return
        self.btn_health.setEnabled(False)
        self.btn_health.setText(_("Kontrol ediliyor…"))
        th = FuncThread(self.saglik.run_checks, self)
        th.done.connect(self.show_health)
        th.failed.connect(self._health_failed)
        th.finished.connect(self._health_finished)
        self._health_thread = th
        th.start()

    def _health_finished(self) -> None:
        th, self._health_thread = self._health_thread, None
        self.btn_health.setEnabled(True)
        self.btn_health.setText(_("Yeniden kontrol et"))
        if th is not None:
            th.deleteLater()

    def _health_failed(self, msg: str) -> None:
        self.show_health([{"ad": _("sağlık kontrolü"), "durum": "hata", "ayrinti": msg, "oneri": ""}])

    def show_health(self, results: list[dict]) -> None:
        for w in self.health_rows:
            w.hide()
            w.setParent(None)
            w.deleteLater()
        self.health_rows = []
        dpr = self.devicePixelRatioF() or 1.0
        order = {"hata": 0, "uyari": 1, "tamam": 2}
        for r in sorted(results, key=lambda r: order.get(r.get("durum"), 3)):
            row = QFrame()
            row.setObjectName("row")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(6, 6, 6, 6)
            rl.setSpacing(10)
            ic = QLabel()
            ic.setPixmap(status_glyph_pixmap(r.get("durum", ""), 18, dpr))
            ic.setFixedSize(18, 18)
            rl.addWidget(ic, 0, Qt.AlignmentFlag.AlignTop)
            tv = QVBoxLayout()
            tv.setSpacing(0)
            top = QHBoxLayout()
            name = label(r.get("ad", ""))
            f = name.font()
            f.setWeight(QFont.Weight.DemiBold)
            name.setFont(f)
            name.setMinimumWidth(170)
            top.addWidget(name)
            det = label(r.get("ayrinti", ""), "muted", wrap=True)
            top.addWidget(det, 1)
            tv.addLayout(top)
            if r.get("durum") != "tamam" and r.get("oneri"):
                o = label(f"→ {r['oneri']}", "small", wrap=True)
                o.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                tv.addWidget(o)
            rl.addLayout(tv, 1)
            self.health_rows.append(row)
            self.health_box.addWidget(row)
        self.health_summary.setText(health_summary(results))
        self.health_summary.show()

    def copy_uninstall(self) -> None:
        QGuiApplication.clipboard().setText(self.uninstall_cmd)
        self.win.toast.show_text(_("Komut kopyalandı"))



# =========================================================================
# microphone page
# =========================================================================
MIC_SOURCE = "oto_eq_mikrofon"
MIC_SERVICE = "oto-eq-mic.service"
MIC_LABEL = "Kulak Kesil · Mikrofon"
MIC_COLOR = "#22A559"
# (path in micgen's config dict, label, kind, min, max, step, unit, help)
MIC_PARAMS = (
    (("gurultu", "aktif"), N_("Gürültü bastırma"), "bool", 0, 1, 1, "",
     N_("Fan, klavye ve oda uğultusunu bastırır.")),
    (("gurultu", "esik"), N_("Bastırma eşiği"), "float", 0, 100, 1, "%",
     N_("Yüksek = daha sıkı; çok yüksekte kelime sonları kesilebilir.")),
    (("netlik", "kazanc"), N_("Netlik"), "float", 0, 8, 0.5, "dB",
     N_("Konuşma bölgesini (2–4 kHz) öne çıkarır; sesin daha anlaşılır olur.")),
    (("kompresor", "esik"), N_("Kompresör eşiği"), "float", -50, -10, 1, "dB",
     N_("Bunun üstündeki sesler yumuşatılır; bağırınca patlamaz.")),
    (("kompresor", "telafi"), N_("Kompresör telafisi"), "float", 0, 15, 0.5, "dB",
     N_("Yumuşatmadan sonra genel seviyeyi geri yükseltir.")),
)
LEVEL_FLOOR_DB = -60.0


def get_path(d: dict, path: tuple):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def set_path(d: dict, path: tuple, value) -> dict:
    import copy
    out = copy.deepcopy(d)
    cur = out
    for k in path[:-1]:
        cur = cur.setdefault(k, {})
    cur[path[-1]] = value
    return out


def load_micgen():
    try:
        from . import micgen
    except ImportError:
        return None
    return micgen


def pcm_level_db(data: bytes) -> tuple[float, float]:
    """(rms dBFS, peak dBFS) of s16le mono PCM."""
    import math
    from array import array
    n = len(data) // 2
    if n == 0:
        return (LEVEL_FLOOR_DB, LEVEL_FLOOR_DB)
    a = array("h")
    a.frombytes(data[: n * 2])
    if sys.byteorder != "little":
        a.byteswap()
    peak = max(abs(min(a)), abs(max(a))) / 32768.0
    rms = math.sqrt(sum(x * x for x in a) / n) / 32768.0
    db = lambda v: max(LEVEL_FLOOR_DB, 20 * math.log10(v)) if v > 0 else LEVEL_FLOOR_DB  # noqa: E731
    return (db(rms), db(peak))


def parse_mic_test(text: str) -> list[tuple[str, float, float, float]]:
    """Rows of the mikrofon_test level table: (label, gürültü, konuşma, tepe)."""
    rows = []
    for ln in text.splitlines():
        parts = ln.split()
        if len(parts) == 4 and parts[0] in ("Ham", "İşlenmiş"):
            try:
                rows.append((parts[0], float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                pass
    return rows


class LevelMeter(QWidget):
    """Horizontal input level bar (dBFS) with peak hold."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(18)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.rms = LEVEL_FLOOR_DB
        self.peak = LEVEL_FLOOR_DB
        self.hold = LEVEL_FLOOR_DB
        self._decay = QTimer(self, interval=50, timeout=self._tick)
        self._decay.start()

    def sizeHint(self):
        return QSize(300, 18)

    def set_level(self, rms_db: float, peak_db: float) -> None:
        self.rms = max(rms_db, self.rms - 3.0)    # fast attack, smooth release
        self.peak = peak_db
        self.hold = max(self.hold, peak_db)
        self.update()

    def _tick(self) -> None:
        if self.hold > LEVEL_FLOOR_DB:
            self.hold = max(LEVEL_FLOOR_DB, self.hold - 1.2)
            self.rms = max(LEVEL_FLOOR_DB, self.rms - 1.5)
            self.update()

    @staticmethod
    def frac(db: float) -> float:
        return min(1.0, max(0.0, (db - LEVEL_FLOOR_DB) / -LEVEL_FLOOR_DB))

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rad = r.height() / 2
        track = self.palette().color(QPalette.ColorRole.Window)
        track = mix(track, self.palette().color(QPalette.ColorRole.WindowText), 0.12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, rad, rad)
        from PyQt6.QtGui import QLinearGradient
        g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0.0, QColor("#22A559"))
        g.setColorAt(0.70, QColor("#22A559"))
        g.setColorAt(0.85, QColor("#F2C230"))
        g.setColorAt(1.0, QColor("#D64545"))
        w = r.width() * self.frac(self.rms)
        if w > 1:
            p.setBrush(g)
            p.drawRoundedRect(QRectF(r.left(), r.top(), max(w, r.height()), r.height()), rad, rad)
        x = r.left() + r.width() * self.frac(self.hold)
        if self.hold > LEVEL_FLOOR_DB:
            p.setBrush(QColor("#FFFFFF") if track.lightness() < 128 else QColor("#232629"))
            p.drawRoundedRect(QRectF(min(x, r.right() - 3), r.top() + 2, 3, r.height() - 4), 1.5, 1.5)
        p.end()


class MicTestDialog(QDialog):
    """Runs `python3 -m oto_eq.mikrofon_test N`: record → level table → playback."""

    def __init__(self, parent, seconds: int = 5, start: bool = True):
        super().__init__(parent)
        self.setWindowTitle(_("Mikrofon testi"))
        self.setMinimumWidth(460)
        self.seconds = seconds
        self.out = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 16)
        lay.setSpacing(12)
        self.title = label(_("Hazırlanıyor…"), "sectionTitle")
        lay.addWidget(self.title)
        self.hint = label(_("Ham HS80 mikrofonu ve Kulak Kesil · Mikrofon aynı anda kaydedilir, "
                            "sonra önce ham, ardından işlenmiş ses kulaklığından çalınır."), "muted", wrap=True)
        lay.addWidget(self.hint)
        self.bar = QProgressBar()
        self.bar.setRange(0, seconds * 10)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", _("Gürültü"), _("Konuşma"), _("Tepe")])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.setFixedHeight(110)
        self.table.hide()
        lay.addWidget(self.table)
        self.verdict = label("", "small", wrap=True)
        self.verdict.hide()
        lay.addWidget(self.verdict)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_again = button(_("Tekrarla"))
        self.btn_again.clicked.connect(self.start)
        self.btn_again.setEnabled(False)
        self.btn_close = button(_("Kapat"), "primary")
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_again)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.proc: QProcess | None = None
        self._t = QTimer(self, interval=100, timeout=self._progress)
        if start:
            QTimer.singleShot(0, self.start)

    def start(self) -> None:
        self.out = ""
        self.table.hide()
        self.verdict.hide()
        self.btn_again.setEnabled(False)
        self.bar.setValue(0)
        self.title.setText(_("🎙  Konuş…"))
        self.hint.setText(_("{n} saniye boyunca normal sesinle konuş.").format(n=self.seconds))
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(config.SRC_DIR))
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("OTO_EQ_DIL", i18n.dil())
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(str(config.SRC_DIR))
        proc.readyReadStandardOutput.connect(lambda: self.feed(
            bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")))
        proc.finished.connect(lambda code, _s: self.finished_with(code))
        self.proc = proc
        self._t.start()
        proc.start(sys.executable, ["-m", "oto_eq.mikrofon_test", str(self.seconds)])

    def _progress(self) -> None:
        if self.bar.value() < self.bar.maximum():
            self.bar.setValue(self.bar.value() + 1)

    def feed(self, text: str) -> None:
        self.out += text
        if "çalınıyor" in text:
            self._t.stop()
            self.bar.setValue(self.bar.maximum())
            what = "işlenmiş" if "işlenmiş" in text else "ham"
            self.title.setText(_("🔊  İşlenmiş ses çalınıyor…") if what == "işlenmiş"
                               else _("🔊  Ham ses çalınıyor…"))
            self.hint.setText(_("Kulaklığından dinle: önce ham HS80, sonra Kulak Kesil · Mikrofon."))
        rows = parse_mic_test(self.out)
        # unbuffered child: "Ham" and "İşlenmiş" usually arrive in separate chunks
        if rows and (self.table.isHidden() or len(rows) != self.table.rowCount()):
            self.show_rows(rows)

    def show_rows(self, rows) -> None:
        self.table.setRowCount(len(rows))
        for r, (name, n, sp, pk) in enumerate(rows):
            shown = {"Ham": _("Ham"), "İşlenmiş": _("İşlenmiş")}.get(name, name)   # name stays the parse key
            vals = [shown, f"{n:.1f} dB", f"{sp:.1f} dB", f"{pk:.1f} dB"]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, it)
        self.table.show()
        d = {name: (n, sp, pk) for name, n, sp, pk in rows}
        if "Ham" in d and "İşlenmiş" in d:
            (hn, hs, _hp), (pn, ps, pp) = d["Ham"], d["İşlenmiş"]
            gap_h, gap_p = hs - hn, ps - pn
            msg = _("Konuşma/gürültü farkı: ham {ham:.0f} dB → işlenmiş {islenmis:.0f} dB.").format(
                ham=gap_h, islenmis=gap_p)
            if pp > -1.0:
                msg += " " + _("Tepe çok yüksek; kompresör eşiğini düşür.")
            elif ps < -30:
                msg += " " + _("İşlenmiş ses kısık; telafiyi artır.")
            self.verdict.setText(msg)
            self.verdict.show()

    def finished_with(self, code: int) -> None:
        self._t.stop()
        self.btn_again.setEnabled(True)
        if code == 0:
            self.title.setText(_("✓  Test bitti"))
            self.hint.setText(_("Değerler dBFS: gürültü düşük, konuşma ile arası geniş olmalı."))
        else:
            last = (self.out.strip().splitlines() or [_("test çalıştırılamadı")])[-1]
            self.title.setText(_("Test yapılamadı"))
            self.hint.setText(last)

    def reject(self) -> None:
        # SIGTERM first: mikrofon_test stops its pw-record children and deletes the
        # recording; SIGKILL only if it does not end in 3 s (its recorders run under
        # `timeout`). The window closes at once.
        stop_process(self.proc, 3000)
        super().reject()


CALIB_SECONDS = 10
CALIB_GRACE_S = 25           # subprocess start-up + analysis on top of the recording
# suggestion key -> (name, value format, one-line explanation)
def _calib_value(fmt: str, v: float) -> str:
    """A calibration value in its row format; percentages in the UI language's order."""
    if fmt == "%{:g}":
        return tx.percent_text(v, 0 if float(v).is_integer() else 1)
    return fmt.format(v)


CALIB_ROWS = {
    "gurultu.esik": (N_("Gürültü kapısı (VAD eşiği)"), "%{:g}",
                     N_("Konuşma sayılmayan sesler (fan, klavye) susturulur. Kelime sonların "
                        "kesilirse düşür.")),
    "kompresor.esik": (N_("Kompresör eşiği"), "{:g} dBFS",
                       N_("Bu seviyeyi aşan ses yumuşatılır; bağırınca karşı tarafın kulağı patlamaz.")),
    "kompresor.oran": (N_("Kompresör oranı"), "{:g}:1",
                       N_("Eşiği aşan sesin ne kadar bastırılacağı (3:1 → 3 dB fazlalık 1 dB olur).")),
    "kompresor.telafi": (N_("Ses kazancı (telafi)"), "{:+g} dB",
                         N_("Yumuşatmadan sonra genel seviyeyi geri yükseltir; seni rahat duyarlar.")),
    "netlik.kazanc": (N_("Netlik"), "{:+g} dB", N_("Konuşma bölgesini (2–4 kHz) öne çıkarır.")),
}
# micgen.calibrate() error code -> (title, what to do; None = micgen's own text)
# (marked only: translated at use, "{saniye}" filled in then)
CALIB_ERRORS = {
    "mikrofon_yok": (N_("Mikrofon bulunamadı"),
                     N_("Kulaklığı ya da mikrofonu aç ve bağla, sonra “Yeniden ölç”e bas.")),
    "sessiz": (N_("Konuşma algılanmadı"),
               N_("Mikrofon kolu aşağıda ve sessize alma kapalı mı? {saniye} saniye boyunca "
                  "normal sesinle konuş, sonra yeniden ölç.")),
    "kirpma": (N_("Ses çok yüksek (kırpılıyor)"),
               N_("Mikrofonu ağzından biraz uzaklaştır ya da KDE ses ayarlarında HS80 mikrofon "
                  "seviyesini düşür, sonra yeniden ölç.")),
    "ayar": (N_("Mikrofon ayarları okunamadı"), None),
    "kayit": (N_("Ölçüm yapılamadı"), None),
}


def calib_error_kind(res: dict) -> str:
    """Error code of a calibrate() result (older micgen: guessed from the text)."""
    kod = res.get("kod")
    if kod in CALIB_ERRORS:
        return kod
    h = str(res.get("hata") or "").lower()
    if "bulunamadı" in h and "mikrofon" in h:
        return "mikrofon_yok"
    if "kırpılıyor" in h:
        return "kirpma"
    if "algılanmadı" in h:
        return "sessiz"
    return "kayit"


def parse_calib_output(out: str) -> dict | None:
    """The JSON line printed by `python3 -m oto_eq.micgen kalibre --json`."""
    import json
    for ln in reversed(out.strip().splitlines()):
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                res = json.loads(ln)
            except ValueError:
                continue
            if isinstance(res, dict):
                return res
    return None


def calib_rows(res: dict, current: dict | None) -> list[tuple[str, str, str, str, bool]]:
    """(name, current, suggested, explanation, changed) for each suggested value."""
    rows = []
    for key, val in (res.get("oneri") or {}).items():
        name, fmt, expl = CALIB_ROWS.get(key, (key, "{:g}", ""))
        if key in CALIB_ROWS:
            name, expl = _(name), _(expl)
        cur = get_path(current, tuple(key.split("."))) if isinstance(current, dict) else None
        num = isinstance(val, (int, float))
        cur_s = _calib_value(fmt, cur) if isinstance(cur, (int, float)) else "—"
        new_s = _calib_value(fmt, val) if num else str(val)
        changed = not (isinstance(cur, (int, float)) and num and abs(cur - val) < 1e-9)
        rows.append((name, cur_s, new_s, expl, changed))
    return rows


def calib_findings(res: dict) -> list[str]:
    """Plain-Turkish remarks about the measurement itself (not errors)."""
    sp, nz, pk = (res.get(k) for k in ("konusma_dbfs", "gurultu_dbfs", "tepe_dbfs"))
    out = []
    if not all(isinstance(v, (int, float)) for v in (sp, nz, pk)):
        return out
    if pk > -3.0:
        out.append(_("Tepe {tepe:.1f} dBFS ile sınıra çok yakın: bağırınca kırpılabilir. Mikrofonu "
                     "biraz uzaklaştırmak ya da KDE'de seviyesini azıcık düşürmek iyi olur.").format(tepe=pk))
    if sp < -45.0:
        out.append(_("Sesin çok kısık geliyor ({konusma:.0f} dBFS). Kazanç yükseltildi; daha temiz sonuç "
                     "için KDE'de HS80 mikrofon seviyesini biraz artırıp yeniden ölçebilirsin.").format(
                         konusma=sp))
    if sp - nz < 12.0:
        out.append(_("Ortam gürültülü (konuşmayla arası yalnız {fark:.0f} dB): gürültü bastırmayı "
                     "açık tut, mikrofonu ağzına yaklaştır.").format(fark=sp - nz))
    return out


class CalibrateDialog(QDialog):
    """Mic calibration: speak 10 s → before/after table → Uygula (+ varsayılan yap).

    The recording runs in a subprocess (`python3 -m oto_eq.micgen kalibre --json`)
    so the window never blocks; a parec level meter shows the RAW HS80 mic
    meanwhile. Applying uses ``micgen.apply_suggestion`` (no second recording):
    save → regenerate mikrofon.conf → live update; only if the running chain
    cannot take it live, ``oto-eq-mic`` is restarted (systemctl, async).
    """

    SECONDS = CALIB_SECONDS

    def __init__(self, page: "MicPage"):
        super().__init__(page)
        self.page = page
        self.result: dict | None = None
        self.applied: dict | None = None
        self.state = ""
        self.proc: QProcess | None = None
        self._out = self._err = ""
        self._make_default = False
        self._restart_err: str | None = None
        self._default_err: str | None = None
        self._restarted = False
        self._defaulted = False
        self._thread: FuncThread | None = None
        self._meter_proc: QProcess | None = None
        self.setWindowTitle(_("Mikrofonu kalibre et"))
        self.setMinimumWidth(640)
        page.win.style_dialog(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(12)
        self.title = label(_("Mikrofonu kalibre et"), "pageTitle")
        lay.addWidget(self.title)
        self.hint = label("", "muted", wrap=True)
        lay.addWidget(self.hint)
        self.warn = label("", "small", wrap=True)
        self.warn.hide()
        lay.addWidget(self.warn)
        self.count = label("", "heroTitle")
        self.count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.count)
        self.bar = QProgressBar()
        self.bar.setRange(0, self.SECONDS * 10)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        lay.addWidget(self.bar)
        self.meter = LevelMeter()
        lay.addWidget(self.meter)
        self.meter_note = label(_("Ham HS80 mikrofonunun seviyesi: konuşurken yeşilde, ara ara sarıda "
                                  "olmalı; kırmızı = çok yüksek."), "small", wrap=True)
        lay.addWidget(self.meter_note)
        self.summary = label("", wrap=True)
        lay.addWidget(self.summary)
        self.rows_box = QFrame()
        self.rows_box.setObjectName("card")
        self.grid = QGridLayout(self.rows_box)
        self.grid.setContentsMargins(14, 10, 14, 10)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(6)
        lay.addWidget(self.rows_box)
        self.rows: list[tuple[str, str, str, str, bool]] = []
        self.note = label("", "small", wrap=True)
        lay.addWidget(self.note)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = button(_("Vazgeç"))
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_again = button(_("Yeniden ölç"))
        self.btn_again.clicked.connect(self.start)
        self.btn_start = button(_("Başla"), "primary")
        self.btn_start.clicked.connect(self.start)
        self.btn_apply = button(_("Uygula"))
        self.btn_apply.clicked.connect(lambda: self.apply(False))
        self.btn_apply_default = button(_("Uygula ve varsayılan mikrofon yap"), "primary")
        self.btn_apply_default.clicked.connect(lambda: self.apply(True))
        self.btn_test = button(_("Mikrofon testi"), "primary")
        self.btn_test.setToolTip(_("Ham ve işlenmiş sesi art arda dinle"))
        self.btn_test.clicked.connect(self.open_test)
        for b in (self.btn_cancel, self.btn_again, self.btn_start, self.btn_apply,
                  self.btn_apply_default, self.btn_test):
            row.addWidget(b)
        lay.addLayout(row)
        self._left = 0
        self._tick = QTimer(self, interval=1000, timeout=self._countdown)
        self._prog = QTimer(self, interval=100, timeout=self._progress)
        self._watchdog = QTimer(self, singleShot=True, timeout=self._timed_out)
        self._watchdog.setInterval((self.SECONDS + CALIB_GRACE_S) * 1000)
        self.show_intro()

    # --- states
    def _set_state(self, state: str) -> None:
        self.state = state
        measuring = state == "measuring"
        for w in (self.count, self.bar, self.meter, self.meter_note):
            w.setVisible(measuring)
        self.summary.setVisible(state in ("result", "applying", "done"))
        self.rows_box.setVisible(state in ("result", "applying") and bool(self.rows))
        self.btn_start.setVisible(state == "intro")
        self.btn_again.setVisible(state in ("result", "error", "applying"))
        is_def = self.page.state.get("default") == MIC_SOURCE
        self.btn_apply.setVisible(state in ("result", "applying"))
        self.btn_apply_default.setVisible(state in ("result", "applying") and not is_def)
        if state in ("result", "applying") and is_def:   # already default: plain Uygula is primary
            self.btn_apply.setObjectName("primary")
            self.btn_apply.style().unpolish(self.btn_apply)
            self.btn_apply.style().polish(self.btn_apply)
        self.btn_test.setVisible(state == "done")
        busy = state == "applying"
        for b in (self.btn_again, self.btn_apply, self.btn_apply_default, self.btn_cancel):
            b.setEnabled(not busy)
        self.btn_cancel.setText(_("Kapat") if state in ("error", "done") else _("Vazgeç"))
        self.adjustSize()

    def show_intro(self) -> None:
        self.title.setText(_("Mikrofonu kalibre et"))
        self.hint.setText(_(
            "1. Kulaklığı tak, mikrofon kolunu aşağı indir (sessize alma kapalı olsun).\n"
            "2. “Başla”ya bas ve {saniye} saniye boyunca normal sesinle konuş; arada kısa "
            "sessizlik bırak (oyunda/Discord'da konuştuğun gibi).\n"
            "3. Sonra şu anki ve önerilen ayarları yan yana görürsün; istersen uygularsın.\n"
            "Ses yalnızca bellekte ölçülür, hiçbir yere kaydedilmez.").format(saniye=self.SECONDS))
        warns = []
        st = self.page.state
        names = st.get("sources") or []
        if names and not any("hs80" in n.lower() and n.startswith("alsa_input") for n in names):
            warns.append(_("⚠ HS80 mikrofonu şu an görünmüyor: kulaklık açık ve bağlı mı?"))
        if st.get("present") is False:
            warns.append(_("⚠ {mikrofon} şu an çalışmıyor; “Uygula” dediğinde yeni ayarlarla "
                           "başlatılır.").format(mikrofon=MIC_LABEL))
        self.warn.setText("\n".join(warns))
        self.warn.setVisible(bool(warns))
        self.note.hide()
        self._set_state("intro")

    # --- measuring (subprocess)
    def command(self) -> tuple[str, list[str]]:
        return sys.executable, ["-m", "oto_eq.micgen", "kalibre", "--json",
                                "--saniye", str(self.SECONDS)]

    def start(self) -> None:
        gen = self.page.micgen
        if gen is None or not hasattr(gen, "calibrate"):
            self.show_error({"hata": _("Bu sürümde kalibrasyon yok (micgen.calibrate)."), "kod": "kayit"})
            return
        if self.state == "measuring":
            return
        self.result = None
        self.rows = []
        self._out = self._err = ""
        self.warn.hide()
        self.note.hide()
        self.title.setText(_("🎙  Konuş…"))
        self.hint.setText(_("Normal sesinle konuş; arada kısa sessizlik bırak. Ölçüm bitince sonuç "
                            "kendiliğinden gelir."))
        self._left = self.SECONDS
        self.count.setText(str(self._left))
        self.bar.setValue(0)
        self._set_state("measuring")
        self._tick.start()
        self._prog.start()
        self._watchdog.start()
        self.start_meter()
        prog, args = self.command()
        self.proc = self._launch(prog, args)

    def _launch(self, program: str, args: list[str]) -> QProcess:
        proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(config.SRC_DIR))
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("OTO_EQ_DIL", i18n.dil())
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(str(config.SRC_DIR))
        proc.readyReadStandardOutput.connect(lambda p=proc: self._read(p, False))
        proc.readyReadStandardError.connect(lambda p=proc: self._read(p, True))
        proc.finished.connect(lambda code, _s, p=proc: self._proc_finished(p, code))
        proc.errorOccurred.connect(lambda err, p=proc: self._proc_error(p, err))
        proc.start(program, args)
        return proc

    def _read(self, proc: QProcess, err: bool) -> None:
        if proc is not self.proc:
            return
        if err:
            self._err += bytes(proc.readAllStandardError()).decode("utf-8", "replace")
        else:
            self._out += bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")

    def _proc_finished(self, proc: QProcess, code: int) -> None:
        if proc is not self.proc:      # killed / replaced run
            if isinstance(proc, QProcess):
                proc.deleteLater()
            return
        self._read(proc, False)
        self._read(proc, True)
        self.proc = None
        proc.deleteLater()
        self.measure_done(code, self._out, self._err)

    def _proc_error(self, proc: QProcess, error) -> None:
        if proc is self.proc and error == QProcess.ProcessError.FailedToStart:
            self.proc = None
            self.measure_done(-1, "", _("ölçüm süreci başlatılamadı"))

    def _countdown(self) -> None:
        self._left = max(0, self._left - 1)
        self.count.setText(str(self._left) if self._left else "…")
        if not self._left:
            self._tick.stop()
            self.title.setText(_("Değerlendiriliyor…"))
            self.hint.setText(_("Sesin inceleniyor, birkaç saniye sürer."))

    def _progress(self) -> None:
        if self.bar.value() < self.bar.maximum():
            self.bar.setValue(self.bar.value() + 1)

    def _timed_out(self) -> None:
        self._kill_proc()
        self.measure_done(-1, "", _("ölçüm zaman aşımına uğradı (ses sistemi yanıt vermiyor)"))

    def _kill_proc(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()                # finished arrives later (_proc_finished frees it)

    def _stop_measuring(self) -> None:
        self._tick.stop()
        self._prog.stop()
        self._watchdog.stop()
        self.stop_meter()

    def measure_done(self, code: int, out: str, err: str) -> None:
        self._stop_measuring()
        res = parse_calib_output(out)
        if res is None:
            lines = [ln for ln in err.strip().splitlines()
                     if ln.strip() and not ln.startswith(("INFO ", "DEBUG "))]
            why = lines[-1] if lines else _("çıkış kodu {kod}").format(kod=code)
            self.show_error({"hata": _("Ölçüm yapılamadı: {neden}").format(neden=why), "kod": "kayit"})
            return
        self.show_result(res)

    # --- live meter on the raw HS80 microphone (only while measuring)
    def start_meter(self) -> None:
        from .cli import pick_real_source
        src = pick_real_source(self.page.state.get("sources") or [])
        if not src or self._meter_proc is not None:
            return
        proc = QProcess(self)
        env = QProcessEnvironment()
        for k, v in config.C_ENV.items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(
            lambda: self.meter.set_level(*pcm_level_db(bytes(proc.readAllStandardOutput()))))
        proc.finished.connect(proc.deleteLater)
        self._meter_proc = proc
        args = list(MicPage.METER_ARGS)
        args[1] = f"--device={src}"
        proc.start(args[0], args[1:])

    def stop_meter(self) -> None:
        proc, self._meter_proc = self._meter_proc, None
        stop_process(proc, 500)

    # --- results
    def show_error(self, res) -> None:
        self._stop_measuring()
        res = res if isinstance(res, dict) else {"hata": str(res)}
        title, todo = CALIB_ERRORS[calib_error_kind(res)]
        todo = _(todo).format(saniye=CALIB_SECONDS) if todo else None
        self.title.setText(_(title))
        self.hint.setText(todo or str(res.get("hata") or _("Bilinmeyen hata")))
        pk = res.get("tepe_dbfs")
        detail = []
        if todo and res.get("hata"):
            detail.append(str(res["hata"]))
        if isinstance(pk, (int, float)) and pk > -200:
            detail.append(_("Ölçülen: konuşma {konusma:.1f} dBFS, gürültü {gurultu:.1f} dBFS, "
                            "tepe {tepe:.1f} dBFS.").format(konusma=res.get("konusma_dbfs", 0),
                                                           gurultu=res.get("gurultu_dbfs", 0), tepe=pk))
        self.note.setText("\n".join(detail))
        self.note.setVisible(bool(detail))
        self.warn.hide()
        self.rows = []
        self._set_state("error")

    def current_values(self) -> dict | None:
        cfg = self.page.cfg if isinstance(self.page.cfg, dict) else None
        gen = self.page.micgen
        if cfg is not None and gen is not None and hasattr(gen, "effective"):
            try:
                return gen.effective(cfg)     # the values actually running (mode applied)
            except Exception:  # noqa: BLE001
                pass
        return cfg

    def show_result(self, res) -> None:
        if not isinstance(res, dict) or res.get("hata"):
            self.show_error(res)
            return
        self._stop_measuring()
        self.result = res
        self.rows = calib_rows(res, self.current_values())
        sp, nz, pk = (res.get(k) for k in ("konusma_dbfs", "gurultu_dbfs", "tepe_dbfs"))
        self.title.setText(_("✓  Ölçüm tamam"))
        self.hint.setText(_("Şu anki ve önerilen ayarlar aşağıda. Uygun görünüyorsa “Uygula”ya bas."))
        text = _("Konuşman {konusma:.1f} dBFS, ortam gürültüsü {gurultu:.1f} dBFS (aradaki fark "
                 "{fark:.0f} dB), en yüksek tepe {tepe:.1f} dBFS.").format(konusma=sp, gurultu=nz,
                                                                           fark=sp - nz, tepe=pk)
        if isinstance(res.get("tahmini_cikis_dbfs"), (int, float)):
            text += " " + _("Önerilen ayarlarla işlenmiş konuşman ≈ {cikis:.1f} dBFS olur (hedef −18).").format(
                cikis=res["tahmini_cikis_dbfs"])
        self.summary.setText(text)
        self._fill_grid()
        notes = calib_findings(res)
        if self.rows and not any(r[4] for r in self.rows):
            notes.insert(0, _("Ayarların zaten önerilen değerlerde; uygulamak yalnızca ayar dosyasını "
                              "tazeler."))
        self.note.setText("\n".join(notes))
        self.note.setVisible(bool(notes))
        self.warn.hide()
        self._set_state("result")
        has = bool(res.get("oneri")) and self.page.micgen is not None
        self.btn_apply.setEnabled(has)
        self.btn_apply_default.setEnabled(has)

    def _fill_grid(self) -> None:
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not self.rows:
            return
        for col, h in enumerate((_("Ayar"), _("Şu an"), "", _("Önerilen"))):
            self.grid.addWidget(label(h, "small"), 0, col)
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        for i, (name, cur, new, expl, changed) in enumerate(self.rows, 1):
            box = QVBoxLayout()
            box.setSpacing(1)
            box.addWidget(label(name))
            if expl:
                box.addWidget(label(expl, "small", wrap=True))
            holder = QWidget()
            holder.setLayout(box)
            box.setContentsMargins(0, 4, 0, 4)
            self.grid.addWidget(holder, i, 0)
            self.grid.addWidget(label(cur, "muted"), i, 1)
            self.grid.addWidget(label("→" if changed else "=", "muted"), i, 2)
            nl = label(new if changed else _("{deger} (aynı)").format(deger=new))
            if changed:
                nl.setFont(bold)
            self.grid.addWidget(nl, i, 3)
        self.grid.setColumnStretch(0, 1)

    def changes(self) -> dict:
        return {tuple(k.split(".")): v for k, v in ((self.result or {}).get("oneri") or {}).items()}

    # --- applying
    def run_bg(self, fn, on_done, on_fail) -> None:
        th = FuncThread(fn, self)
        th.done.connect(on_done)
        th.failed.connect(on_fail)
        th.finished.connect(lambda t=th: (setattr(self, "_thread", None), t.deleteLater()))
        self._thread = th
        th.start()

    def apply(self, make_default: bool = False) -> bool:
        oneri = dict((self.result or {}).get("oneri") or {})
        gen = self.page.micgen
        if not oneri or gen is None or self.state != "result":
            return False
        self._make_default = make_default
        self._restart_err = self._default_err = None
        self._restarted = self._defaulted = False
        self.title.setText(_("Uygulanıyor…"))
        self.hint.setText(_("Yeni ayarlar kaydediliyor ve mikrofona uygulanıyor."))
        self._set_state("applying")
        if hasattr(gen, "apply_suggestion"):
            self.run_bg(lambda: gen.apply_suggestion(oneri), self._applied, self._apply_failed)
        else:   # older micgen: the page's own path (save + live, restart if needed)
            ok = self.page.apply_values(self.changes())
            self._applied({"degisti": ok, "canli": ok, "yeniden_baslat": False})
        return True

    def _apply_failed(self, msg: str) -> None:
        self._applied({"hata": msg})

    def _applied(self, r) -> None:
        r = r if isinstance(r, dict) else {"hata": str(r)}
        self.applied = r
        self.page.load_params()
        if r.get("hata"):
            self.title.setText(_("Uygulanamadı"))
            self.hint.setText(_("{hata}\nAyarların değişmedi; “Yeniden ölç” ya da “Vazgeç”.").format(hata=r["hata"]))
            self._set_state("result")
            return
        if r.get("yeniden_baslat"):
            self.hint.setText(_("Mikrofon servisi yeni ayarlarla yeniden başlatılıyor…"))
            self.page.win.runner.run(systemctl_args("restart", MIC_SERVICE), self._after_restart,
                                     timeout_ms=20000)
        else:
            self._after_restart(None, "", "")

    def _after_restart(self, rc, _out, err) -> None:
        if rc is not None:
            self._restarted = rc == 0
            if rc != 0:
                self._restart_err = (err or "").strip()[:160] or _("çıkış kodu {kod}").format(kod=rc)
        if self._make_default and self._restart_err is None:
            delay = 1200 if self._restarted else 0      # let the new source appear first
            QTimer.singleShot(delay, self._set_default)
        else:
            self._finish()

    def _set_default(self) -> None:
        def done(rc, _o, err):
            self._defaulted = rc == 0
            if rc != 0:
                self._default_err = (err or "").strip()[:160] or _("çıkış kodu {kod}").format(kod=rc)
            self._finish()
        self.page.win.runner.run(["pactl", "set-default-source", MIC_SOURCE], done, timeout_ms=5000)

    def _finish(self) -> None:
        r = self.applied or {}
        lines = []
        if not r.get("degisti", True):
            lines.append(_("Ayarların zaten önerilen değerlerdeydi; değişiklik gerekmedi."))
        elif r.get("canli"):
            lines.append(_("Yeni ayarlar çalışan mikrofona hemen uygulandı; bağlı uygulamalar "
                           "(Discord, oyun) kesilmedi."))
        if self._restarted:
            lines.append(_("Mikrofon servisi yeni ayarlarla yeniden başlatıldı."))
        if self._restart_err:
            lines.append(_("Ayarlar kaydedildi ama mikrofon servisi başlatılamadı: {hata}. "
                           "Ayarlar sayfasındaki Servisler bölümünden yeniden başlatmayı dene.").format(
                               hata=self._restart_err))
        if self._defaulted:
            lines.append(_("Varsayılan mikrofon artık “{mikrofon}”.").format(mikrofon=MIC_LABEL))
        if self._default_err:
            lines.append(_("Varsayılan mikrofon değiştirilemedi: {hata}").format(hata=self._default_err))
        elif self._make_default and self._restart_err:
            lines.append(_("Servis çalışmadığı için varsayılan mikrofon değiştirilmedi."))
        ok = not (self._restart_err or self._default_err)
        lines.append(_("Sonucu duymak için “Mikrofon testi”ne bas: önce ham, sonra işlenmiş sesin çalınır."))
        self.title.setText(_("✓  Uygulandı") if ok else _("Kısmen uygulandı"))
        self.hint.setText("\n".join(lines))
        self.note.hide()
        self._set_state("done")
        self.btn_test.setEnabled(not self._restart_err)
        self.page.win.toast.show_text(_("Mikrofon kalibre edildi ✓") if ok else _("Mikrofon: kısmen uygulandı"))
        self.page.refresh()

    def open_test(self) -> None:
        self.accept()
        QTimer.singleShot(0, self.page.run_test)

    def reject(self) -> None:
        if self.state == "applying":       # a background save is running; let it finish
            return
        self._stop_measuring()
        self._kill_proc()
        super().reject()


class MicPage(Page):
    def __init__(self, win: "MainWindow"):
        super().__init__(_("Mikrofon"), _("HS80 mikrofonunun işlenmiş hâli: gürültü bastırma, netlik ve "
                                          "dengeli seviye. Discord ve oyunlarda bunu seç."))
        self.win = win
        self.state = {"present": None, "default": "", "service": "", "sources": []}
        self.micgen = load_micgen()
        self.cfg: dict | None = None
        self._meter_proc: QProcess | None = None

        # --- status card
        self.hero = card()
        hl = QHBoxLayout(self.hero)
        hl.setContentsMargins(22, 18, 22, 18)
        hl.setSpacing(18)
        self.badge = QLabel()
        self.badge.setFixedSize(64, 64)
        hl.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        hv = QVBoxLayout()
        hv.setSpacing(3)
        t = label(MIC_LABEL, "pageTitle", wrap=True)
        t.setStyleSheet("font-size: 15pt;")
        hv.addWidget(t)
        self.status_line = label(_("Durum alınıyor…"), "muted", wrap=True)
        hv.addWidget(self.status_line)
        hl.addLayout(hv, 1)
        bx = QVBoxLayout()
        bx.setSpacing(6)
        self.btn_default = button(_("Varsayılan mikrofon yap"), "primary")
        self.btn_default.clicked.connect(self.toggle_default)
        self.btn_start = button(_("Başlat"))
        self.btn_start.clicked.connect(self.start_service)
        bx.addWidget(self.btn_default)
        bx.addWidget(self.btn_start)
        bx.addStretch(1)
        hl.addLayout(bx)
        self.body.addWidget(self.hero)

        # --- level meter
        lm = card()
        ll = QVBoxLayout(lm)
        ll.setContentsMargins(18, 14, 18, 16)
        ll.setSpacing(8)
        lh = QHBoxLayout()
        lh.addWidget(label(_("Giriş seviyesi"), "sectionTitle"))
        lh.addStretch(1)
        self.level_text = label("—", "muted")
        lh.addWidget(self.level_text)
        ll.addLayout(lh)
        self.meter = LevelMeter()
        ll.addWidget(self.meter)
        ll.addWidget(label(_("İşlenmiş mikrofonun canlı seviyesi (sadece bu sayfa açıkken ölçülür, "
                             "hiçbir şey kaydedilmez). Konuşurken çoğunlukla yeşilde kalmalı."), "small", wrap=True))
        self.body.addWidget(lm)

        # --- calibration
        cc = card()
        cl = QHBoxLayout(cc)
        cl.setContentsMargins(18, 14, 18, 14)
        ct = QVBoxLayout()
        ct.setSpacing(2)
        ct.addWidget(label(_("Kalibrasyon"), "sectionTitle"))
        ct.addWidget(label(_("{saniye} saniye konuş; gürültü kapısı, kompresör ve ses kazancı "
                             "sesine göre ayarlansın. Önce önerileri görürsün, sonra uygularsın.").format(
                                 saniye=CALIB_SECONDS), "muted", wrap=True))
        cl.addLayout(ct, 1)
        self.btn_calib = button(_("Mikrofonu kalibre et…"), "primary")
        self.btn_calib.setToolTip(_("{saniye} saniye konuş; sesine göre gürültü kapısı, "
                                    "kompresör ve kazanç önerilir").format(saniye=CALIB_SECONDS))
        self.btn_calib.clicked.connect(self.run_calibration)
        cl.addWidget(self.btn_calib, 0, Qt.AlignmentFlag.AlignVCenter)
        self.body.addWidget(cc)

        # --- test
        tc = card()
        tl = QHBoxLayout(tc)
        tl.setContentsMargins(18, 14, 18, 14)
        tt = QVBoxLayout()
        tt.setSpacing(2)
        tt.addWidget(label(_("Mikrofon testi"), "sectionTitle"))
        tt.addWidget(label(_("5 saniye konuş; ham ve işlenmiş sesin seviyelerini gör, sonra ikisini "
                             "art arda dinle."), "muted", wrap=True))
        tl.addLayout(tt, 1)
        self.btn_test = button(_("Testi başlat"))
        self.btn_test.clicked.connect(self.run_test)
        tl.addWidget(self.btn_test)
        self.body.addWidget(tc)

        # --- parameters
        pc = card()
        pl = QVBoxLayout(pc)
        pl.setContentsMargins(18, 14, 18, 14)
        pl.setSpacing(4)
        pl.addWidget(label(_("Ses işleme"), "sectionTitle"))
        self.param_note = label("", "small", wrap=True)
        pl.addWidget(self.param_note)
        self.controls: dict[tuple, tuple] = {}
        for path, title, kind, lo, hi, step, unit, help_ in MIC_PARAMS:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 8, 0, 8)
            rl.setSpacing(12)
            txt = QVBoxLayout()
            txt.setSpacing(1)
            txt.addWidget(label(_(title)))
            txt.addWidget(label(_(help_), "small", wrap=True))
            rl.addLayout(txt, 3)
            if kind == "bool":
                ctl = ToggleSwitch()
                ctl.toggled.connect(lambda on, pth=path: self.param_changed(pth, bool(on)))
                rl.addWidget(ctl, 0, Qt.AlignmentFlag.AlignVCenter)
                val = None
            else:
                ctl = QSlider(Qt.Orientation.Horizontal)
                ctl.setRange(round(lo / step), round(hi / step))
                ctl.setMinimumWidth(220)
                val = label("—", "muted")
                val.setMinimumWidth(64)
                val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                ctl.valueChanged.connect(lambda v, lab=val, st=step, u=unit:
                                         lab.setText(f"{v * st:g} {u}".strip()))
                ctl.sliderReleased.connect(lambda c=ctl, pth=path, st=step: self.param_changed(pth, c.value() * st))
                ctl.valueChanged.connect(lambda v, c=ctl, pth=path, st=step:
                                         None if c.isSliderDown() else self._debounced(pth, v * st))
                rl.addWidget(ctl, 2)
                rl.addWidget(val)
            pl.addWidget(row)
            self.controls[path] = (ctl, val, kind, step)
        self._pending: tuple | None = None
        self._apply_timer = QTimer(self, singleShot=True, interval=400, timeout=self._apply_pending)
        self._loading = False
        self.body.addWidget(pc)

        # --- Discord tip
        dc = card()
        dl = QHBoxLayout(dc)
        dl.setContentsMargins(18, 14, 18, 14)
        dl.setSpacing(14)
        tip = QLabel("💡")
        tip.setStyleSheet("font-size: 20pt;")
        dl.addWidget(tip, 0, Qt.AlignmentFlag.AlignTop)
        dt = QVBoxLayout()
        dt.setSpacing(2)
        dt.addWidget(label(_("Discord için ipucu"), "sectionTitle"))
        dt.addWidget(label(_("Discord'da Ses ve Görüntü ayarlarından giriş cihazı olarak “Kulak Kesil · "
                             "Mikrofon”u seç; gürültü azaltma (Krisp), otomatik kazanç ve yankı giderme "
                             "kapalı olsun. İşlemeyi zaten Kulak Kesil yapıyor; ikisi üst üste binince ses "
                             "boğuklaşır."), "muted", wrap=True))
        dl.addLayout(dt, 1)
        self.body.addWidget(dc)
        self.body.addStretch(1)

        self._poll = QTimer(self, interval=3000, timeout=self.refresh)
        self.load_params()
        self.render()

    # --- state
    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.refresh()
        self._poll.start()
        self.load_params()

    def hideEvent(self, e):  # noqa: N802
        super().hideEvent(e)
        self._poll.stop()
        self.stop_meter()

    def refresh(self) -> None:
        r = self.win.runner
        r.run(["pactl", "list", "short", "sources"], self._got_sources, timeout_ms=4000)
        r.run(["pactl", "get-default-source"], self._got_default, timeout_ms=4000)
        r.run(systemctl_args("is-active", MIC_SERVICE), self._got_service, timeout_ms=4000)

    def _got_sources(self, rc, out, _e):
        names = [ln.split("\t")[1] for ln in out.splitlines() if ln.count("\t") >= 1] if rc == 0 else []
        self.state["sources"] = names
        self.state["present"] = MIC_SOURCE in names
        self.render()

    def _got_default(self, rc, out, _e):
        self.state["default"] = out.strip() if rc == 0 else ""
        self.render()

    def _got_service(self, _rc, out, _e):
        self.state["service"] = out.strip()
        self.render()

    def render(self) -> None:
        present, is_def = self.state["present"], self.state["default"] == MIC_SOURCE
        dpr = self.devicePixelRatioF() or 1.0
        color = MIC_COLOR if present and is_def else (ui.COLOR_INACTIVE if present else ui.COLOR_OFF)
        if present is None:
            color = ui.COLOR_INACTIVE
        self.badge.setPixmap(mic_badge_pixmap(color, 64, dpr))
        if present is None:
            text = _("Durum alınıyor…")
        elif not present:
            text = _({"failed": N_("Çalışmıyor — servis hata verdi. “Başlat” ile aç."),
                      "inactive": N_("Çalışmıyor — servis durdu. “Başlat” ile aç."),
                      "activating": N_("Çalışmıyor — servis başlıyor. “Başlat” ile aç.")}.get(
                self.state["service"], N_("Çalışmıyor — servis çalışmıyor. “Başlat” ile aç.")))
        elif is_def:
            text = _("Çalışıyor · varsayılan mikrofon. Uygulamalar işlenmiş sesi alıyor.")
        else:
            text = _("Çalışıyor · varsayılan değil. Varsayılan yap ya da Discord'da elle seç.")
        self.status_line.setText(text)
        self.btn_start.setVisible(present is False)
        self.btn_default.setEnabled(bool(present) or is_def)
        self.btn_default.setText(_("HS80'e geri dön") if is_def else _("Varsayılan mikrofon yap"))
        self.btn_default.setObjectName("secondary" if is_def else "primary")
        self.btn_default.style().unpolish(self.btn_default)
        self.btn_default.style().polish(self.btn_default)
        self.btn_test.setEnabled(bool(present))
        # calibration measures the RAW HS80 mic; applying (re)starts the service if needed
        self.btn_calib.setEnabled(self.micgen is not None and hasattr(self.micgen, "calibrate"))
        if present and self.isVisible():
            self.start_meter()
        elif not present:
            self.stop_meter()

    # --- actions
    def toggle_default(self) -> None:
        from .cli import pick_real_source
        if self.state["default"] == MIC_SOURCE:
            target = pick_real_source(self.state["sources"])
            if not target:
                self.win.toast.show_text(_("Gerçek bir mikrofon bulunamadı"))
                return
            msg = _("Varsayılan mikrofon: {mikrofon}").format(mikrofon="HS80")
        else:
            target, msg = MIC_SOURCE, _("Varsayılan mikrofon: {mikrofon}").format(mikrofon=MIC_LABEL) + " ✓"

        def done(rc, _o, err):
            self.win.toast.show_text(msg if rc == 0 else _("Değiştirilemedi: {hata}").format(hata=err.strip()[:120]))
            self.refresh()
        self.win.runner.run(["pactl", "set-default-source", target], done, timeout_ms=5000)

    def start_service(self) -> None:
        def done(rc, _o, err):
            self.win.toast.show_text(_("Mikrofon başlatıldı ✓") if rc == 0
                                     else _("Başlatılamadı: {hata}").format(hata=err.strip()[:120]))
            QTimer.singleShot(800, self.refresh)
        self.win.runner.run(systemctl_args("start", MIC_SERVICE), done, timeout_ms=20000)

    def run_calibration(self) -> "CalibrateDialog":
        self.stop_meter()
        dlg = CalibrateDialog(self)
        dlg.finished.connect(lambda _r: self.isVisible() and self.state["present"] and self.start_meter())
        dlg.open()
        return dlg

    def run_test(self) -> MicTestDialog:
        self.stop_meter()  # don't hold the source while testing
        dlg = MicTestDialog(self, 5)
        dlg.finished.connect(lambda _r: self.isVisible() and self.state["present"] and self.start_meter())
        dlg.open()
        return dlg

    # --- level meter (parec only while visible)
    METER_ARGS = ["parec", f"--device={MIC_SOURCE}", "--format=s16le", "--rate=16000", "--channels=1",
                  "--latency-msec=40", "--property=application.name=" + _("Kulak Kesil seviye ölçer"),
                  "--property=media.role=production"]

    def start_meter(self) -> None:
        if self._meter_proc is not None:
            return
        proc = QProcess(self)
        env = QProcessEnvironment()
        for k, v in config.C_ENV.items():
            env.insert(k, v)
        proc.setProcessEnvironment(env)
        proc.readyReadStandardOutput.connect(lambda: self.feed_audio(bytes(proc.readAllStandardOutput())))
        proc.finished.connect(lambda *_a: self._meter_gone(proc))
        self._meter_proc = proc
        proc.start(self.METER_ARGS[0], self.METER_ARGS[1:])

    def _meter_gone(self, proc) -> None:
        if self._meter_proc is proc:
            self._meter_proc = None
        proc.deleteLater()

    def stop_meter(self) -> None:
        proc, self._meter_proc = self._meter_proc, None
        stop_process(proc, 500)
        self.meter.set_level(LEVEL_FLOOR_DB, LEVEL_FLOOR_DB)
        self.level_text.setText("—")

    def feed_audio(self, data: bytes) -> None:
        rms, peak = pcm_level_db(data)
        self.meter.set_level(rms, peak)
        self.level_text.setText(_("sessiz") if peak <= LEVEL_FLOOR_DB + 0.5 else f"{peak:.0f} dBFS")

    # --- parameters via micgen
    def load_params(self) -> None:
        gen = self.micgen
        self.cfg = None
        if gen is not None:
            try:
                self.cfg = gen.load_mic()
            except Exception as e:  # noqa: BLE001
                log.warning("mikrofon ayarı okunamadı: %s", e)
        # a selected [mod.X] overrides some values: show what RUNS and lock those controls
        # (editing the base table would change nothing audible under that mode)
        mode, over = "tam", {}
        if isinstance(self.cfg, dict):
            mode = str(((self.cfg.get("genel") or {}).get("mod")) or "tam")
            over = ((self.cfg.get("mod") or {}).get(mode) or {}) if mode != "tam" else {}
        helps = {p[0]: p[7] for p in MIC_PARAMS}
        self._loading = True
        try:
            missing = 0
            for path, (ctl, val, kind, step) in self.controls.items():
                v = get_path(self.cfg, path) if isinstance(self.cfg, dict) else None
                sec_over = over.get(path[0]) if isinstance(over, dict) else None
                locked = isinstance(sec_over, dict) and path[-1] in sec_over
                if locked:
                    v = sec_over[path[-1]]
                ok = v is not None
                missing += not ok
                ctl.setEnabled(ok and not locked)
                ctl.setToolTip(_("Bu değeri “{mod}” modu belirliyor (mikrofon.toml → [mod.{mod}]); "
                                 "değiştirmek için oradaki satırı düzenle.").format(mod=mode)
                               if locked else _(helps.get(path, "")))
                if not ok:
                    continue
                if kind == "bool":
                    ctl.setChecked(bool(v))
                else:
                    ctl.setValue(round(float(v) / step))
                    val.setText(f"{ctl.value() * step:g} {dict((p[0], p[6]) for p in MIC_PARAMS)[path]}".strip())
        finally:
            self._loading = False
        if gen is None:
            self.param_note.setText(_("Ayar modülü (micgen) henüz kurulu değil; ayarlar şimdilik kapalı."))
        elif self.cfg is None:
            self.param_note.setText(_("Mikrofon ayarları okunamadı."))
        elif missing:
            self.param_note.setText(_("Bazı ayarlar bu sürümde yok ve kapalı gösteriliyor."))
        else:
            self.param_note.setText(_("Değişiklikler hemen uygulanır."))

    def _debounced(self, path, value) -> None:
        if self._loading:
            return
        self._pending = (path, value)
        self._apply_timer.start()

    def _apply_pending(self) -> None:
        if self._pending:
            path, value = self._pending
            self._pending = None
            self.param_changed(path, value)

    def param_changed(self, path: tuple, value) -> bool:
        if self._loading:
            return False
        return self.apply_values({path: value})

    def apply_values(self, changes: dict) -> bool:
        """Set several config paths at once: save_mic + set_live (or service restart)."""
        if self.micgen is None or not isinstance(self.cfg, dict):
            return False
        gen = self.micgen
        # mikrofon.toml may have changed since the page read it (`kulak mikrofon kalibre
        # --uygula`, a hand edit): change the file's CURRENT content, never write the page's
        # older copy back over it
        try:
            fresh = gen.load_mic()
        except Exception as e:  # noqa: BLE001 - unreadable now: keep the page's copy
            log.warning("mikrofon ayarı yeniden okunamadı: %s", e)
            fresh = None
        old = fresh if isinstance(fresh, dict) else self.cfg
        outside = old != self.cfg
        new = old
        for path, value in changes.items():
            if get_path(new, path) != value:
                new = set_path(new, path, value)
        if new is old:
            if outside:
                self.load_params()         # show what the file says now
            return False
        try:
            gen.save_mic(new)
            restart = bool(gen.needs_restart(old, new)) if hasattr(gen, "needs_restart") else True
            self._write_conf(new)          # a (re)start must use the new values
            if not restart and not gen.set_live(new):
                restart = True             # the running graph did not take it: restart applies
        except Exception as e:  # noqa: BLE001
            self.win.toast.show_text(_("Mikrofon ayarı uygulanamadı: {hata}").format(hata=e))
            return False
        self.cfg = new
        if outside:
            self.load_params()             # the other controls show the outside change too
        if restart:
            def done(rc, _o, err):
                self.win.toast.show_text(_("Mikrofon yeniden başlatıldı ✓") if rc == 0
                                         else _("Yeniden başlatılamadı: {hata}").format(hata=err.strip()[:120]))
                QTimer.singleShot(800, self.refresh)
            self.win.runner.run(systemctl_args("restart", MIC_SERVICE), done, timeout_ms=20000)
        return True

    def _write_conf(self, data: dict) -> None:
        """Regenerate mikrofon.conf (micgen.write_conf) so it never lags mikrofon.toml."""
        gen = self.micgen
        if gen is None or not hasattr(gen, "write_conf"):
            return
        try:
            gen.write_conf(data=data)
        except Exception as e:  # noqa: BLE001 - live values are applied anyway
            log.warning("mikrofon.conf yeniden üretilemedi: %s", e)



CS2_CHECKLIST = (
    (N_("EQ profili: Natural"), N_("Crisp tizi ikinci kez yükseltir; Smooth'u kullanma.")),
    (N_("Ses çıkışı: Kulaklık — sanal surround / 7.1 kapalı"), N_("CS2 kendi HRTF'ini uygular.")),
    (N_("L/R Isolation: %50 ile başla"), N_("Yön karışırsa %75–100 dene.")),
    (N_("Perspective Correction: Açık"), N_("Kenardaki sesler tek kulağa kayarsa kapatıp karşılaştır.")),
    (N_("Oyun içi ana ses sabit (ör. %80)"), N_("Sesi kulaklık tekerinden ya da KDE'den ayarla; kompresör "
                                                "oyunun dijital seviyesine göre çalışır.")),
)
GUIDE_STEPS = ("ses", "cs2", "discord", "mikrofon")


class WelcomeDialog(QDialog):
    """First-run guide: 4 short steps the user ticks off (state kept in pencere.json)."""

    WIDTH = 640

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle(_("{uygulama} — Kurulum rehberi").format(uygulama=ui.APP_NAME))
        self.setFixedWidth(self.WIDTH)
        win.style_dialog(self)
        saved = win.app_settings().get("rehber") or {}
        mic_default = getattr(win, "page_mic", None) is not None and \
            win.page_mic.state.get("default") == MIC_SOURCE
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 22, 26, 18)
        outer.setSpacing(12)
        head = QHBoxLayout()
        head.setSpacing(14)
        icon = QLabel()
        dpr = self.devicePixelRatioF() or 1.0
        pm, p = _pm(52, dpr)
        ui.paint_app_icon(p, 52)
        p.end()
        icon.setPixmap(pm)
        head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        ht = QVBoxLayout()
        ht.setSpacing(2)
        ht.addWidget(label(_("{uygulama}'e hoş geldin").format(uygulama=ui.APP_NAME), "pageTitle"))
        ht.addWidget(label(_("Dört kısa adım. Yaptıkça işaretle; bu rehbere Ayarlar → Kurulum rehberi'nden "
                             "yeniden ulaşabilirsin."), "muted", wrap=True))
        head.addLayout(ht, 1)
        outer.addLayout(head)

        self.checks: dict[str, QCheckBox] = {}
        # the four steps scroll: in de/ru the guide is ~730 px tall, more than a 768 px
        # (or 1080 px @150 %) screen leaves, and its "Tamam" button must stay reachable
        steps_w = QWidget()
        steps_w.setObjectName("scrollBody")
        steps = QVBoxLayout(steps_w)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(12)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(steps_w)

        def step(key: str, title: str, lines: list[str], extra: QWidget | None = None):
            c = card()
            lay = QVBoxLayout(c)
            lay.setContentsMargins(16, 12, 16, 12)
            lay.setSpacing(4)
            cb = QCheckBox(title)
            f = cb.font()
            f.setWeight(QFont.Weight.DemiBold)
            cb.setFont(f)
            cb.setChecked(bool(saved.get(key)))
            cb.setCursor(Qt.CursorShape.PointingHandCursor)
            cb.toggled.connect(self._save)
            lay.addWidget(cb)
            for ln in lines:
                lab = label(ln, "small", wrap=len(ln) > 80)
                lab.setContentsMargins(26, 0, 0, 0)
                lay.addWidget(lab)
            if extra is not None:
                row = QHBoxLayout()
                row.setContentsMargins(26, 4, 0, 0)
                row.addWidget(extra)
                row.addStretch(1)
                lay.addLayout(row)
            self.checks[key] = cb
            steps.addWidget(c)

        step("ses", _("1. Ses biraz kısılır: kulaklık sesini bir kez artır"),
             [_("EQ'nun yükselttiği bölgeler patlamasın diye yaklaşık 10 dB pay bırakılır. Kulaklık "
                "tekerinden ya da KDE ses seviyesinden bir kez artırman yeterli.")])
        step("cs2", _("2. CS2 oyun içi ses ayarları"),
             [f"• {_(t)}" for t, _d in CS2_CHECKLIST])
        discord = [_("• Ses ve Görüntü: gürültü azaltma (Krisp), otomatik kazanç ve yankı giderme kapalı.")]
        if mic_default:
            discord.append(_("• Giriş cihazı: {mikrofon}.").format(mikrofon=MIC_LABEL))
        step("discord", _("3. Discord ayarları"), discord)
        self.btn_test = button(_("Mikrofon testini başlat"))
        self.btn_test.clicked.connect(self._run_test)
        step("mikrofon", _("4. Mikrofon testi"),
             [_("5 saniye konuş; ham ve işlenmiş sesi art arda dinle, farkı duy.")], self.btn_test)

        steps.addStretch(1)
        outer.addWidget(self.scroll, 1)
        row = QHBoxLayout()
        self.progress = label("", "muted")
        row.addWidget(self.progress)
        row.addStretch(1)
        self.btn_close = button(_("Tamam"), "primary")
        self.btn_close.clicked.connect(self.accept)
        self.btn_close.setDefault(True)
        row.addWidget(self.btn_close)
        outer.addLayout(row)
        self._update_progress()
        self.btn_close.setFocus()
        # wrapped labels: the height the steps need at this width; the whole guide when the
        # screen has room, else what it allows (the steps scroll, header and buttons stay)
        m = outer.contentsMargins()
        inner = self.WIDTH - m.left() - m.right()
        steps.activate()
        need = steps.heightForWidth(inner) if steps.hasHeightForWidth() else -1
        need = max(need, steps.sizeHint().height())
        chrome = (m.top() + m.bottom() + 2 * outer.spacing() + head.sizeHint().height()
                  + row.sizeHint().height())
        self.content_height = need + chrome
        scr = self.screen() or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry().height() - 60 if scr is not None else self.content_height
        self.setMinimumHeight(min(self.content_height, 360))
        self.resize(self.WIDTH, max(self.minimumHeight(), min(self.content_height, avail)))

    def _save(self) -> None:
        self.win.update_app_settings({"rehber": {k: cb.isChecked() for k, cb in self.checks.items()}})
        self._update_progress()

    def _update_progress(self) -> None:
        done = sum(cb.isChecked() for cb in self.checks.values())
        self.progress.setText(_("Hepsi tamam ✓") if done == len(self.checks)
                              else _("{bitti}/{toplam} adım tamam").format(bitti=done, toplam=len(self.checks)))

    def _run_test(self) -> None:
        page = getattr(self.win, "page_mic", None)
        if page is None or not page.state.get("present"):
            self.win.toast.show_text(_("{mikrofon} çalışmıyor — önce Mikrofon sayfasından başlat").format(
                mikrofon=MIC_LABEL))
            return
        page.run_test()


def make_eq_page():
    """A4's EqPage if importable, else the placeholder."""
    try:
        from .eq_page import EqPage
    except ImportError:
        return EqPlaceholderPage()
    try:
        return EqPage()
    except Exception as e:  # noqa: BLE001 - never let the EQ page kill the window
        log.exception("Ekolayzır sayfası açılamadı")
        return EqPlaceholderPage(_("Hata: {hata}").format(hata=e))


# =========================================================================
# main window
# =========================================================================
class MainWindow(QMainWindow):
    def __init__(self, requester=None, runner=None, settings_path: Path | None = None,
                 restore: bool = True):
        super().__init__()
        self._request = requester or ui.default_request
        self.runner = runner or CommandRunner(self)
        self._settings_path = Path(settings_path or ui.APP_SETTINGS_FILE)
        self.status: dict | None = None
        self.seen_down = False
        self._sink_desc: dict[str, str] = {}
        self._dil_esitlendi = False          # router language checked once (set_status)
        self.last_reload_error: str | None = None   # reload_quietly: why it was not applied
        self._theming = False
        self.setWindowTitle(ui.APP_NAME)
        self.setWindowIcon(ui.app_icon())
        # fits a 1366×768 @125 % / 1920×1080 @150 % screen (~1093×614 logical, minus the panel);
        # pages scroll vertically, button rows wrap
        self.setMinimumSize(900, 540)
        self.resize(1080, 720)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        # sidebar
        self.sidebar = QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setFixedWidth(232)
        sl = QVBoxLayout(self.sidebar)
        sl.setContentsMargins(14, 20, 14, 16)
        sl.setSpacing(4)
        brand = QHBoxLayout()
        brand.setSpacing(10)
        self.brand_icon = QLabel()
        self.brand_icon.setFixedSize(40, 40)
        brand.addWidget(self.brand_icon)
        bt = QVBoxLayout()
        bt.setSpacing(0)
        bt.addWidget(label(ui.APP_NAME, "appName"))
        bt.addWidget(label(_(ui.APP_TAGLINE), "small"))
        brand.addLayout(bt, 1)
        sl.addLayout(brand)
        sl.addSpacing(22)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for i, (key, text) in enumerate(PAGES):
            b = QPushButton("  " + _(text))
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setIconSize(QSize(20, 20))
            b.setProperty("navKey", key)
            b.clicked.connect(lambda _c=False, idx=i: self.show_page(idx))
            self.nav_group.addButton(b, i)
            self.nav_buttons.append(b)
            sl.addWidget(b)
        sl.addStretch(1)
        self.pill_dot = QLabel()
        self.pill_dot.setFixedSize(10, 10)
        self.pill_text = label(_("Bağlanıyor…"), "small")
        self.pill_text.setWordWrap(True)
        pill = QHBoxLayout()
        pill.setSpacing(8)
        pill.addWidget(self.pill_dot)
        pill.addWidget(self.pill_text, 1)
        sl.addLayout(pill)
        h.addWidget(self.sidebar)

        # pages
        self.stack = QStackedWidget()
        self.page_status = StatusPage(self)
        self.page_eq = make_eq_page()
        self.page_mic = MicPage(self)
        self.page_rules = RulesPage(self)
        self.page_settings = SettingsPage(self)
        for p in (self.page_status, self.page_eq, self.page_mic, self.page_rules, self.page_settings):
            self.stack.addWidget(p)
        h.addWidget(self.stack, 1)

        self.toast = Toast(root)
        self.apply_theme()
        self.show_page(0)
        if restore:
            self.restore_state()
        self.set_status(None)

    # --- theme
    def accent(self) -> str | None:
        spec = ui.badge_spec(self.status)
        return spec.color if spec.key in EQ_PROFILES else None

    def apply_theme(self) -> None:
        if self._theming:
            return
        self._theming = True
        try:
            base = QApplication.font().pointSizeF()
            self.theme = Theme.from_palette(self.palette(), self.accent(), base if base > 0 else 10.0)
            self.centralWidget().setStyleSheet(stylesheet(self.theme))
            dpr = self.devicePixelRatioF() or 1.0
            for b in self.nav_buttons:
                b.setIcon(nav_icon(b.property("navKey"), self.theme.text, 20, max(2.0, dpr)))
            pm, p = _pm(40, dpr)
            ui.paint_app_icon(p, 40)
            p.end()
            self.brand_icon.setPixmap(pm)
            self.page_status.apply_theme(self.theme)
            self.page_settings.apply_theme(self.theme)
        finally:
            self._theming = False

    def changeEvent(self, e):  # noqa: N802
        if e.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            QTimer.singleShot(0, self.apply_theme)
        elif e.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            # back from the `kulak ab` terminal / `kulak ton`: show the saved tone
            self._eq_call("reload_tone")
        super().changeEvent(e)

    def _eq_call(self, name: str, *args, **kw) -> None:
        """Optional EqPage hook (absent on the placeholder / older pages)."""
        fn = getattr(self.page_eq, name, None)
        if callable(fn):
            try:
                fn(*args, **kw)
            except Exception:  # noqa: BLE001 - never let the EQ page break the window
                log.exception("EqPage.%s hatası", name)

    def leave_eq_preview(self) -> None:
        """Window closing / app quitting: unsaved "Kişisel ton" preview back to saved."""
        self._eq_call("discard_tone_preview", sync=True)

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        self.toast.reposition()

    # --- navigation
    def show_page(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        self.nav_buttons[idx].setChecked(True)
        if self.stack.currentWidget() is self.page_rules:
            self.page_rules.reload_table()

    JUMP_ANCHORS = {"ton": ("page_eq", "tone_scope_box"), "kalibrasyon": ("page_mic", "btn_calib"),
                    "ses_analizi": ("page_settings", "sw_audio")}

    def jump_to(self, page_key: str, anchor: str = "") -> bool:
        """Open a page (PAGES key) and scroll to a section ("ton", "kalibrasyon", "ses_analizi")."""
        keys = [k for k, _t in PAGES]
        if page_key not in keys:
            return False
        self.show_page(keys.index(page_key))
        page_attr, widget_attr = self.JUMP_ANCHORS.get(anchor, ("", ""))
        target = getattr(getattr(self, page_attr, None), widget_attr, None) if page_attr else None
        if isinstance(target, QWidget):
            QTimer.singleShot(0, lambda: scroll_into_view(target))
        return True

    # --- status
    def current_mode_key(self) -> str | None:
        st = self.status
        if not st:
            return None
        if st.get("mode") == "fixed":
            return f"fixed:{st.get('fixed_profile')}"
        return st.get("mode")

    def on_status(self, st: dict) -> None:
        st = dict(st)
        st.pop("event", None)
        st.pop("ok", None)
        self.set_status(st)

    def on_down(self, _msg: str = "") -> None:
        self.seen_down = True
        self.set_status(None)

    def set_status(self, st: dict | None) -> None:
        old_accent = self.accent()
        self.status = st
        if st and not self._dil_esitlendi and isinstance(st.get("dil"), str):
            # once per GUI run: the router keeps the language it started with; if the user
            # changed it since (or declined the restart then), bring the router along
            self._dil_esitlendi = True
            if st["dil"] != i18n.dil():
                self.language_changed_quietly()
        if self.accent() != old_accent:
            self.apply_theme()
        if st and st.get("default_sink") and st["default_sink"] not in self._sink_desc:
            self._resolve_sink(st["default_sink"])
        self.page_status.set_status(st)
        if hasattr(self.page_eq, "set_status"):
            try:
                self.page_eq.set_status(st)
            except Exception:  # noqa: BLE001
                log.exception("EqPage.set_status hatası")
        self._update_pill(st)

    def _update_pill(self, st: dict | None) -> None:
        spec = ui.badge_spec(st)
        dpr = self.devicePixelRatioF() or 1.0
        self.pill_dot.setPixmap(dot_icon(spec.color, 10, dpr).pixmap(QSize(10, 10), dpr))
        if st is None:
            self.pill_text.setText(_("Servis çalışmıyor") if self.seen_down else _("Bağlanıyor…"))
        else:
            if st.get("mode") in ("fixed", "off"):
                self.pill_text.setText(ui.mode_text(st))
            else:
                dom = ui.profile_label(spec.key) if spec.key in EQ_PROFILES else _("boşta")
                self.pill_text.setText(f"{ui.mode_text(st)} · {dom}")

    # --- default sink description
    def sink_description(self, name: str) -> str:
        if name in self._sink_desc:
            return self._sink_desc[name]
        return prettify_sink(name)

    def _resolve_sink(self, name: str) -> None:
        self._sink_desc[name] = prettify_sink(name)

        def done(rc, out, _err):
            desc = parse_sink_description(out, name) if rc == 0 else None
            if desc and desc != self._sink_desc.get(name):
                self._sink_desc[name] = desc
                if self.status and self.status.get("default_sink") == name:
                    self.page_status.set_status(self.status)
        self.runner.run(["pactl", "-f", "json", "list", "sinks"], done, timeout_ms=4000)

    # --- actions
    def send(self, obj: dict) -> bool:
        try:
            resp = self._request(obj)
        except (ConnectionError, OSError, TimeoutError):
            self.toast.show_text(_("Servis çalışmıyor — önce servisi başlat"))
            return False
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = resp.get("error") if isinstance(resp, dict) else resp
            self.toast.show_text(_("Hata: {hata}").format(hata=err))
            return False
        if resp.get("uyari"):
            self.toast.show_text(_("Uyarı: {uyari}").format(uyari=resp["uyari"]))
        return True

    def select_mode(self, key: str) -> bool:
        if key.startswith("fixed:"):
            req = {"cmd": "set_mode", "mode": "fixed", "profile": key.split(":", 1)[1]}
        else:
            req = {"cmd": "set_mode", "mode": key}
        ok = self.send(req)
        if not ok:
            self.page_status.set_status(self.status)  # revert the segmented control
        return ok

    def reload_rules(self, announce: bool = False) -> bool:
        ok = self.send({"cmd": "reload"})
        if ok and announce:
            self.toast.show_text(_("Kurallar yeniden yüklendi ✓"))
        return ok

    def write_assignments(self, items: list[dict], what: str) -> None:
        try:
            config.save_assignments(items)
        except OSError as e:
            self.toast.show_text(_("Kaydedilemedi: {hata}").format(hata=e))
            return
        self.page_rules.reload_table()
        self.page_status.refresh_rows()
        ok = self.reload_quietly()
        self.toast.show_text(f"{what} ✓" if ok else self.not_applied_text(what))

    def assign(self, stream: dict, profil: str | None) -> None:
        """profil: '' = EQ'suz, None = remove the assignment."""
        k = assignment_key(stream)
        if not k:
            return
        items = config.load_assignments()
        name = stream.get("app") or k[1]
        if profil is None:
            self.write_assignments(remove_assignment(items, *k), _("{ad} ataması kaldırıldı").format(ad=name))
        else:
            label_ = ui.assignment_profile_label(profil)
            self.write_assignments(upsert_assignment(items, k[0], k[1], profil),
                                   _("{ad} artık hep {profil} çalacak").format(ad=name, profil=label_))

    def assignment_menu(self, stream: dict, parent=None) -> QMenu:
        menu = QMenu(parent)
        sub = menu.addMenu(_("Bu uygulamayı hep şununla çal"))
        k = assignment_key(stream)
        current = find_assignment(config.load_assignments(), *k) if k else None
        for p, text in profile_choices():
            act = sub.addAction(dot_icon(ui.PROFILE_COLORS.get(p, ui.COLOR_INACTIVE)), text)
            act.setCheckable(True)
            act.setChecked(current is not None and current.get("profil") == p)
            act.triggered.connect(lambda _c=False, pp=p: self.assign(stream, pp))
        menu.addSeparator()
        rm = menu.addAction(_("Atamayı kaldır"))
        rm.setEnabled(current is not None)
        rm.triggered.connect(lambda: self.assign(stream, None))
        menu.setEnabled(k is not None)
        return menu

    def variant_menu(self, profile: str, parent=None) -> QMenu:
        menu = QMenu(parent)
        v = tx.variant_of(self.status, profile) or {}
        current = "auto" if v.get("mode", "auto") != "fixed" else (v.get("group") or "")
        group = QActionGroup(menu)
        group.setExclusive(True)
        items = [("auto", _("Otomatik (içeriğe göre)")), ("", _("Genel"))] + \
            [(g, _(GROUP_LABELS.get(g, g))) for g in variant_groups(profile)]
        for i, (g, text) in enumerate(items):
            if i == 2:
                menu.addSeparator()
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(g == current)
            group.addAction(act)
            act.triggered.connect(lambda _c=False, gg=g: self.set_variant(profile, gg))
        return menu

    def set_variant(self, profile: str, group: str) -> bool:
        ok = self.send({"cmd": "set_variant", "profile": profile, "group": group})
        if ok:
            shown = _("otomatik") if group == "auto" else _(GROUP_LABELS.get(group, group))
            self.toast.show_text(f"{_(tx.VARIANT_KIND_LABELS.get(profile, profile))}: {shown}")
        return ok

    def reload_quietly(self) -> bool:
        """IPC reload; False when not applied. ``last_reload_error`` tells WHY: None = the
        router is not reachable (applied when it starts), else its error text (it IS running
        and rejected the reload, e.g. a broken kurallar.toml: say so, do not blame the
        service)."""
        self.last_reload_error = None
        try:
            resp = self._request({"cmd": "reload"})
        except (ConnectionError, OSError, TimeoutError):
            return False
        if isinstance(resp, dict) and resp.get("ok"):
            return True
        err = resp.get("error") if isinstance(resp, dict) else resp
        if isinstance(resp, dict) and resp.get("bekliyor"):
            err = err or _("servis meşgul")
        self.last_reload_error = str(err or _("bilinmeyen hata"))
        return False

    def not_applied_text(self, what: str) -> str:
        """Toast after a saved change that the router did not apply (see reload_quietly)."""
        if self.last_reload_error:
            return _("{ne} — kaydedildi ama uygulanamadı: {hata}").format(
                ne=what, hata=self.last_reload_error)
        return _("{ne} — kaydedildi, servis çalışınca uygulanacak").format(ne=what)

    def language_changed_quietly(self) -> bool:
        """Tell the router the UI language changed (IPC "dil": it reloads its catalog, so its
        texts, e.g. classifier reasons, follow without a service restart). Best effort: an
        old router answers "unknown command" → False."""
        try:
            resp = self._request({"cmd": "dil"})
            return isinstance(resp, dict) and bool(resp.get("ok"))
        except (ConnectionError, OSError, TimeoutError, ValueError, RuntimeError):
            return False

    def start_services(self) -> None:
        self.page_status.btn_start.setEnabled(False)
        self.page_status.btn_start.setText(_("Başlatılıyor…"))

        def done(rc, _o, err):
            self.page_status.btn_start.setEnabled(True)
            self.page_status.btn_start.setText(_("Servisi başlat"))
            self.toast.show_text(_("Servis başlatıldı ✓") if rc == 0
                                else _("Başlatılamadı: {hata}").format(hata=err.strip()[:140]))
        self.runner.run(systemctl_args("start", *SERVICES), done, timeout_ms=30000)

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()
        if self.windowHandle():
            self.windowHandle().requestActivate()

    # --- persistence
    def restore_state(self) -> None:
        data = ui.read_json(self._settings_path)
        g = data.get("geometry")
        if isinstance(g, str):
            try:
                self.restoreGeometry(QByteArray(base64.b64decode(g)))
            except ValueError:
                pass
        page = data.get("sayfa")
        if isinstance(page, int) and 0 <= page < len(PAGES):
            self.show_page(page)

    def app_settings(self) -> dict:
        return ui.read_json(self._settings_path)

    def update_app_settings(self, patch: dict) -> None:
        data = self.app_settings()
        data.update(patch)
        ui.write_json(self._settings_path, data)

    def save_state(self) -> None:
        self.update_app_settings({
            "geometry": base64.b64encode(bytes(self.saveGeometry())).decode("ascii"),
            "sayfa": self.stack.currentIndex()})

    # --- welcome guide
    def style_dialog(self, dlg: QDialog) -> None:
        dlg.setObjectName("root")
        dlg.setStyleSheet(stylesheet(self.theme))

    def show_welcome(self, dialog_cls=None) -> "WelcomeDialog":
        dlg = (dialog_cls or WelcomeDialog)(self)
        dlg.finished.connect(lambda _r: self.update_app_settings({"rehber_goruldu": True}))
        dlg.open()
        return dlg

    def maybe_show_welcome(self) -> bool:
        if self.app_settings().get("rehber_goruldu"):
            return False
        self.show_welcome()
        return True

    def closeEvent(self, e):  # noqa: N802
        self.save_state()
        self.leave_eq_preview()
        super().closeEvent(e)

    # --- restart after a language change (CHANGE 8)
    def restart_app(self, execv=None, tray_restart=None) -> None:
        """Ask the tray to restart itself, then re-exec this app (new language).

        ``execv``/``tray_restart`` are injectable for tests (no real exec there)."""
        (tray_restart or request_tray_restart)()
        self.save_state()
        self.leave_eq_preview()
        server = getattr(self, "single_server", None)
        if server is not None:
            server.close()           # the new process removeServer()s the name anyway
        (execv or exec_self)(sys.executable, [sys.executable, "-m", "oto_eq.app"])


def prettify_sink(name: str) -> str:
    """Readable fallback for a sink node name."""
    if not name:
        return ""
    if "hs80" in name.lower():
        return "Corsair HS80"
    core = name.split(".", 1)[-1]
    return core.replace("_", " ").replace("-", " ").strip() or name


def parse_sink_description(pactl_json: str, name: str) -> str | None:
    import json
    try:
        for s in json.loads(pactl_json):
            if s.get("name") == name:
                return s.get("description") or None
    except (ValueError, AttributeError, TypeError):
        return None
    return None


# =========================================================================
# entry point
# =========================================================================
def notify_running_instance(name: str = ui.APP_SERVER_NAME) -> bool:
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"raise\n")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def request_tray_restart(name: str = ui.TRAY_SERVER_NAME) -> bool:
    """Tell a running tray icon to restart itself (no tray → False, silently)."""
    sock = QLocalSocket()
    sock.connectToServer(name)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"restart\n")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def exec_self(program: str, args: list[str]) -> None:
    """os.execv, keeping the source tree importable for ``-m oto_eq.app``."""
    src = str(config.SRC_DIR)
    paths = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    if src not in paths:
        os.environ["PYTHONPATH"] = os.pathsep.join([src, *paths])
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(program, args)


def serve_single_instance(win, name: str = ui.APP_SERVER_NAME) -> QLocalServer:
    """``win``: the MainWindow, or a callable returning it (None while it is still being
    built: main() claims the name FIRST, so a double launch cannot open two windows)."""
    get = win if callable(win) and not isinstance(win, QWidget) else (lambda: win)
    server = QLocalServer(None if get() is None else get())
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        # only a stale socket file (crashed instance; the caller's probe found nobody)
        QLocalServer.removeServer(name)
        if not server.listen(name):
            log.warning("tekil örnek soketi açılamadı: %s", server.errorString())

    def on_conn():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read():
            if b"raise" in bytes(conn.readAll()):
                w = get()
                if w is not None:
                    w.bring_to_front()
        conn.readyRead.connect(read)
        conn.disconnected.connect(conn.deleteLater)
        if conn.bytesAvailable():
            read()
    server.newConnection.connect(on_conn)
    w = get()
    if w is not None:
        w.single_server = server
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setApplicationName("oto-eq-app")
    app.setApplicationDisplayName(ui.APP_NAME)
    app.setDesktopFileName("oto-eq")
    ui.install_excepthook(log)            # a stray exception in a slot must not abort the app
    if notify_running_instance():
        return 0
    holder: list[MainWindow] = []
    _server = serve_single_instance(lambda: holder[0] if holder else None)   # before the window
    win = MainWindow()
    holder.append(win)
    win.single_server = _server
    app.aboutToQuit.connect(win.save_state)
    app.aboutToQuit.connect(win.leave_eq_preview)
    # logout sends SIGTERM: quit cleanly so the geometry is saved
    import signal
    signal.signal(signal.SIGTERM, lambda *_a: QApplication.quit())
    _tick = QTimer(interval=500, timeout=lambda: None)  # lets Python see signals
    _tick.start()
    thread = ui.WatchThread()
    thread.status.connect(win.on_status)
    thread.down.connect(win.on_down)
    thread.start()
    win.show()
    QTimer.singleShot(700, win.maybe_show_welcome)
    rc = app.exec()
    thread.stop()
    thread.wait(500)
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)  # watch thread may block in a socket read; skip teardown


if __name__ == "__main__":
    sys.exit(main())
