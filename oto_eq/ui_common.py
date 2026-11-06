"""Shared look & helpers for the tray and the main window (Kulak Kesil).

Colors, Turkish labels/"odak" texts per profile, the QPainter status badge,
the app icon generator, the notify-send wrapper and process launch helpers.

Qt is imported lazily inside the painting functions so that pure helpers
(``badge_spec``, ``Notifier``) work without PyQt6 / a display.

CLI: ``python3 -m oto_eq.ui_common icon [PATH]`` renders the app icon PNG
(used by install.sh; run with QT_QPA_PLATFORM=offscreen).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QSize, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import QLayout, QWidgetItem

from . import config
from . import ui_text as tx
from .i18n import _
from .models import EQ_PROFILES, PROFILE_LABELS
from .ui_text import (FLAT_LABEL, FLAT_TOOLTIP, OFF_EXPLAIN, assignment_profile_label,  # noqa: F401
                      stream_profile_label, stream_profile_tooltip)

log = logging.getLogger("oto_eq.ui_common")

APP_NAME = "Kulak Kesil"
APP_TAGLINE = _("Akıllı Ekolayzır")
APP_SERVER_NAME = "oto-eq-app"     # QLocalServer name of the main window
TRAY_SERVER_NAME = "oto-eq-tray"   # QLocalServer name of the tray (single instance)
APP_VERSION = "1.0"
APP_ICON_FILE = config.CONFIG_DIR / "kulak-kesil.png"
AUTOSTART_FILE = config.CONFIG_DIR.parent / "autostart" / "oto-eq-tray.desktop"
APP_SETTINGS_FILE = config.STATE_DIR / "pencere.json"
DESKTOP_MARK = "X-OtoEQ-Installed=true"
RETRY_SECONDS = 3.0
# IPC client wait: longer than the router's own COMMAND_WAIT, so a busy router's
# "bekliyor" answer arrives instead of a timeout ("service not running" would be wrong)
IPC_TIMEOUT = 2.5
DOWN_TEXT = _("Servis çalışmıyor — systemctl --user start oto-eq")
FALLBACK_ICON = "audio-headphones"

# --- colors ---------------------------------------------------------------
PROFILE_COLORS: dict[str, str] = {
    "cs2": "#F28C28",    # turuncu
    "oyun": "#8E5CD9",   # mor
    "muzik": "#22A559",  # yeşil
    "film": "#3B82F6",   # mavi
}
COLOR_OFF = "#D64545"        # kırmızı: mod kapalı
COLOR_INACTIVE = "#8A8F98"   # gri: EQ'lu akış yok / kulaklık aktif değil
COLOR_UNREACHABLE = "#6B7280"  # koyu gri: servis yok
COLOR_FIXED_MARK = "#FFFFFF"


def install_excepthook(logger: logging.Logger) -> None:
    """PyQt6 calls qFatal() (abort, exit 134) when an exception escapes a slot while
    sys.excepthook is the default; log it instead so one bad click cannot kill the app."""
    def hook(t, v, tb):
        if issubclass(t, KeyboardInterrupt):
            sys.__excepthook__(t, v, tb)
            return
        logger.error("beklenmeyen hata", exc_info=(t, v, tb))
    sys.excepthook = hook

# --- texts ----------------------------------------------------------------
PROFILE_LETTERS = {"cs2": "C", "oyun": "O", "muzik": "M", "film": "F"}
PROFILE_EMOJI = {"cs2": "🎯", "oyun": "🎮", "muzik": "🎵", "film": "🎬"}
PROFILE_ODAK: dict[str, str] = {
    "cs2": _("adım sesi / sert ses bastırma"),
    "oyun": _("atmosfer"),
    "muzik": _("tını"),
    "film": _("diyalog"),
}
PROFILE_NOTIFY_BODY: dict[str, str] = {
    "cs2": _("Adım sesleri öne çıkarıldı"),
    "oyun": _("Atmosfer ve derinlik"),
    "muzik": _("Doğal tını"),
    "film": _("Diyalog net"),
}
MODE_LABELS = tx.MODE_LABELS


profile_label = tx.profile_label


mode_text = tx.mode_text


def notify_texts(profile: str) -> tuple[str, str]:
    """(summary, body) for a dominant-profile notification."""
    return (f"{PROFILE_EMOJI.get(profile, '🎧')} "
            + _("{profil} modu").format(profil=profile_label(profile)),
            PROFILE_NOTIFY_BODY.get(profile, ""))


# --- badge ----------------------------------------------------------------
@dataclass(frozen=True)
class BadgeSpec:
    """What the status badge shows."""
    key: str          # cs2|oyun|muzik|film|none|off|down  (stable id, used by tests)
    color: str
    glyph: str        # a letter, or "dash" | "cross" | "bang"
    fixed: bool       # draw the small "sabit" marker


def badge_spec(status: dict | None) -> BadgeSpec:
    """Map an IPC status dict (None = daemon unreachable) to a badge."""
    if status is None:
        return BadgeSpec("down", COLOR_UNREACHABLE, "bang", False)
    mode = status.get("mode")
    if mode == "off":
        return BadgeSpec("off", COLOR_OFF, "cross", False)
    fixed = mode == "fixed"
    dom = status.get("dominant")
    if status.get("active") and dom in PROFILE_COLORS:
        return BadgeSpec(dom, PROFILE_COLORS[dom], PROFILE_LETTERS[dom], fixed)
    return BadgeSpec("none", COLOR_INACTIVE, "dash", fixed)


def paint_badge(painter, size: int, spec: BadgeSpec) -> None:
    """Draw the round badge into a size x size area with an active QPainter."""
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QFont, QPen

    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)
    s = float(size)
    m = max(0.5, s * 0.03)
    circle = QRectF(m, m, s - 2 * m, s - 2 * m)
    # thin dark ring keeps the badge visible on light panels
    ring = max(1.0, s * 0.045)
    painter.setPen(QPen(QColor(0, 0, 0, 110), ring))
    painter.setBrush(QColor(spec.color))
    painter.drawEllipse(circle.adjusted(ring / 2, ring / 2, -ring / 2, -ring / 2))

    white = QColor("#FFFFFF")
    c = QPointF(s / 2, s / 2)
    stroke = max(1.6, s * 0.11)
    pen = QPen(white, stroke, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    if spec.glyph == "dash":
        painter.setPen(pen)
        painter.drawLine(QPointF(s * 0.32, c.y()), QPointF(s * 0.68, c.y()))
    elif spec.glyph == "cross":
        painter.setPen(pen)
        a, b = s * 0.34, s * 0.66
        painter.drawLine(QPointF(a, a), QPointF(b, b))
        painter.drawLine(QPointF(b, a), QPointF(a, b))
    elif spec.glyph == "bang":
        painter.setPen(pen)
        painter.drawLine(QPointF(c.x(), s * 0.26), QPointF(c.x(), s * 0.56))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(white)
        r = stroke * 0.62
        painter.drawEllipse(QPointF(c.x(), s * 0.72), r, r)
    else:
        font = QFont("Noto Sans")
        font.setBold(True)
        font.setPixelSize(max(7, round(s * (0.66 if s <= 24 else 0.60))))
        painter.setFont(font)
        painter.setPen(white)
        painter.drawText(QRectF(0, 0, s, s * 1.02), Qt.AlignmentFlag.AlignCenter, spec.glyph)

    if spec.fixed:
        # "sabit" marker: white dot with dark outline, bottom-right
        r = max(2.2, s * 0.15)
        p = QPointF(s - r - m * 0.5, s - r - m * 0.5)
        painter.setPen(QPen(QColor(0, 0, 0, 170), max(1.0, s * 0.04)))
        painter.setBrush(QColor(COLOR_FIXED_MARK))
        painter.drawEllipse(p, r, r)


ICON_SIZES = (16, 22, 24, 32, 48, 64, 128)


def badge_pixmap(size: int, spec: BadgeSpec):
    """A transparent QPixmap with the badge (needs a QGuiApplication)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QPainter, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    try:
        paint_badge(p, size, spec)
    finally:
        p.end()
    return pm


def badge_icon(spec: BadgeSpec):
    """QIcon with crisp pixmaps for every common panel size."""
    from PyQt6.QtGui import QIcon

    icon = QIcon()
    for sz in ICON_SIZES:
        icon.addPixmap(badge_pixmap(sz, spec))
    return icon


# --- app icon -------------------------------------------------------------
def paint_app_icon(painter, size: int) -> None:
    """Headphone glyph over a dark rounded square, with 4 colored EQ bars."""
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import QColor, QLinearGradient, QPen

    painter.setRenderHint(painter.RenderHint.Antialiasing, True)
    s = float(size)
    bg = QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92)
    grad = QLinearGradient(bg.topLeft(), bg.bottomRight())
    grad.setColorAt(0.0, QColor("#2B3245"))
    grad.setColorAt(1.0, QColor("#151924"))
    painter.setPen(QPen(QColor(255, 255, 255, 40), max(1.0, s * 0.01)))
    painter.setBrush(grad)
    painter.drawRoundedRect(bg, s * 0.22, s * 0.22)

    # headband arc
    band = QPen(QColor("#E8ECF4"), s * 0.075, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    painter.setPen(band)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    arc = QRectF(s * 0.20, s * 0.17, s * 0.60, s * 0.62)
    painter.drawArc(arc, 0 * 16, 180 * 16)
    # side stems down to the cups
    painter.drawLine(QPointF(s * 0.20, s * 0.48), QPointF(s * 0.20, s * 0.58))
    painter.drawLine(QPointF(s * 0.80, s * 0.48), QPointF(s * 0.80, s * 0.58))
    # ear cups
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#E8ECF4"))
    painter.drawRoundedRect(QRectF(s * 0.13, s * 0.52, s * 0.15, s * 0.28), s * 0.06, s * 0.06)
    painter.drawRoundedRect(QRectF(s * 0.72, s * 0.52, s * 0.15, s * 0.28), s * 0.06, s * 0.06)

    # EQ bars between the cups, one per profile color
    heights = (0.20, 0.32, 0.14, 0.26)
    bw = s * 0.07
    gap = s * 0.035
    total = 4 * bw + 3 * gap
    x0 = s / 2 - total / 2
    base = s * 0.80
    for i, prof in enumerate(EQ_PROFILES):
        h = s * heights[i]
        painter.setBrush(QColor(PROFILE_COLORS[prof]))
        painter.drawRoundedRect(QRectF(x0 + i * (bw + gap), base - h, bw, h), bw * 0.4, bw * 0.4)


def render_app_icon(path: Path = APP_ICON_FILE, size: int = 256) -> Path:
    """Render the app icon PNG to ``path`` (needs a QGuiApplication)."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QPainter, QPixmap

    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    try:
        paint_app_icon(p, size)
    finally:
        p.end()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pm.save(str(path), "PNG"):
        raise OSError(f"simge kaydedilemedi: {path}")
    return path


def app_icon():
    """QIcon for windows: the generated PNG if present, else drawn in memory."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon, QPainter, QPixmap

    if APP_ICON_FILE.exists():
        return QIcon(str(APP_ICON_FILE))
    icon = QIcon()
    for sz in (32, 64, 128, 256):
        pm = QPixmap(sz, sz)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        paint_app_icon(p, sz)
        p.end()
        icon.addPixmap(pm)
    return icon


# --- notifications --------------------------------------------------------
# notify-send (GLib) converts argv from the locale charset; under LC_ALL=C
# the Turkish text/emoji would be rejected, so use the C.UTF-8 locale.
NOTIFY_ENV = {**config.C_ENV, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}


class Notifier:
    """notify-send wrapper that replaces its previous notification (-r).

    notify-send waits for the notification server (up to 3 s): with the real
    ``subprocess.run`` it runs on one worker thread so a slow server never freezes the
    tray/window; requests stay in order, so ``-r`` keeps replacing the last one. An
    injected runner (tests) runs inline and ``notify`` returns its result."""

    def __init__(self, runner=subprocess.run, app_name: str = APP_NAME,
                 background: bool | None = None):
        self._run = runner
        self.app_name = app_name
        self.last_id: int | None = None
        self.background = (runner is subprocess.run) if background is None else background
        self._queue: queue.Queue | None = None
        self._lock = threading.Lock()

    def icon(self) -> str:
        return str(APP_ICON_FILE) if APP_ICON_FILE.exists() else FALLBACK_ICON

    def notify(self, summary: str, body: str = "", timeout_ms: int = 4000) -> bool:
        """Show (or replace) the notification; True = sent (background: queued)."""
        if not self.background:
            return self._send(summary, body, timeout_ms)
        with self._lock:
            if self._queue is None:
                self._queue = queue.Queue()
                threading.Thread(target=self._worker, name="oto-eq-bildirim", daemon=True).start()
        self._queue.put((summary, body, timeout_ms))
        return True

    def _worker(self) -> None:
        while True:
            summary, body, timeout_ms = self._queue.get()
            try:
                self._send(summary, body, timeout_ms)
            except Exception:  # noqa: BLE001 - never kill the worker
                log.exception("bildirim gönderilemedi")

    def _send(self, summary: str, body: str, timeout_ms: int) -> bool:
        cmd = ["notify-send", "-a", self.app_name, "-i", self.icon(), "-e",
               "-t", str(timeout_ms), "-p"]
        if self.last_id:
            cmd += ["-r", str(self.last_id)]
        cmd += ["--", summary]
        if body:
            cmd.append(body)
        try:
            res = self._run(cmd, capture_output=True, text=True, timeout=3, env=NOTIFY_ENV)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("bildirim gönderilemedi: %s", e)
            return False
        if getattr(res, "returncode", 1) != 0:
            log.warning("notify-send hata kodu %s", getattr(res, "returncode", "?"))
            return False
        out = (getattr(res, "stdout", "") or "").strip().splitlines()
        if out and out[-1].strip().isdigit():
            self.last_id = int(out[-1].strip())
        return True


# --- process helpers ------------------------------------------------------
def gui_env() -> dict[str, str]:
    """Env for user-facing child programs: user's locale + our PYTHONPATH."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(config.SRC_DIR)
    return env


def launch_detached(args: list[str]) -> bool:
    """Start a program fully detached (own session, no stdio)."""
    try:
        subprocess.Popen(args, cwd=str(config.SRC_DIR), env=gui_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError as e:
        log.warning("başlatılamadı %s: %s", args, e)
        return False


def launch_module(module: str, *args: str) -> bool:
    """Detached ``python3 -m <module> args...``."""
    return launch_detached([sys.executable, "-m", module, *args])


def xdg_open(target: Path | str) -> bool:
    return launch_detached(["xdg-open", str(target)])


def raise_or_launch_app(server_name: str = APP_SERVER_NAME, launcher=None) -> str:
    """Raise the running main window (QLocalSocket "raise") or start it.

    Returns "raised" or "launched" (or "failed").
    """
    from PyQt6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(server_name)
    if sock.waitForConnected(300):
        sock.write(b"raise\n")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return "raised"
    launcher = launcher or (lambda: launch_module("oto_eq.app"))
    return "launched" if launcher() else "failed"


# --- IPC helpers -----------------------------------------------------------
def default_watch():
    from . import ipc
    return ipc.watch()


def default_request(obj: dict) -> dict:
    from . import ipc
    return ipc.request(obj, timeout=IPC_TIMEOUT)


class WatchThread(QThread):
    """Consumes ipc.watch() forever (reconnect every 3 s); emits status or 'down'."""

    status = pyqtSignal(object)
    down = pyqtSignal(str)

    def __init__(self, watch_fn=default_watch, retry_s: float = RETRY_SECONDS, parent=None):
        super().__init__(parent)
        self._watch_fn = watch_fn
        self._retry = retry_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                for ev in self._watch_fn():
                    if self._stop.is_set():
                        return
                    if isinstance(ev, dict):
                        self.status.emit(ev)
                raise ConnectionError("bağlantı kapandı")
            except Exception as e:  # noqa: BLE001 - any failure = reconnect later
                if self._stop.is_set():
                    return
                log.debug("izleme koptu: %s", e)
                self.down.emit(str(e))
            self._stop.wait(self._retry)


# --- wrapping row of buttons --------------------------------------------------
class FlowLayout(QLayout):
    """Lays its widgets out left to right and wraps to a new line when the width runs
    out (Qt's flow layout example). Button rows use it so long translations (de, ru …)
    wrap instead of forcing a horizontal scroll bar. ``right`` = right-align each line."""

    def __init__(self, parent=None, spacing: int = 8, right: bool = False):
        super().__init__(parent)
        self._items: list = []
        self._right = right
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def addWidget(self, w) -> None:  # noqa: N802 - Qt API
        self.addChildWidget(w)
        self.addItem(QWidgetItem(w))

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int):  # noqa: N802 - Qt API
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int):  # noqa: N802 - Qt API
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._do_layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802 - Qt API
        super().setGeometry(rect)
        self._do_layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        w = h = 0
        for it in self._visible():
            sh = it.sizeHint()
            w += sh.width() + (self.spacing() if w else 0)
            h = max(h, sh.height())
        m = self.contentsMargins()
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        size = QSize()
        for it in self._visible():
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _visible(self) -> list:
        return [it for it in self._items if not (it.widget() and it.widget().isHidden())]

    def _do_layout(self, rect, apply: bool) -> int:
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        sp = self.spacing()
        lines, line, line_w = [], [], 0
        for it in self._visible():
            w = it.sizeHint().width()
            if line and line_w + sp + w > r.width():
                lines.append(line)
                line, line_w = [], 0
            line_w += (sp if line else 0) + w
            line.append(it)
        if line:
            lines.append(line)
        y = r.y()
        for ln in lines:
            h = max(it.sizeHint().height() for it in ln)
            if apply:
                used = sum(it.sizeHint().width() for it in ln) + sp * (len(ln) - 1)
                x = r.x() + (max(0, r.width() - used) if self._right else 0)
                for it in ln:
                    sh = it.sizeHint()
                    w = min(sh.width(), r.width())
                    it.setGeometry(QRect(QPoint(x, y + (h - sh.height()) // 2), QSize(w, sh.height())))
                    x += w + sp
            y += h + sp
        return (y - sp - r.y() if lines else 0) + m.top() + m.bottom()


# --- small JSON settings files ----------------------------------------------
def read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: dict) -> None:
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("ayar kaydedilemedi %s: %s", path, e)


def load_tray_settings(path: Path | None = None) -> dict:
    """Shared by tray and main window: only the notification toggle."""
    data = read_json(path or config.TRAY_SETTINGS_FILE)
    return {"bildirimler": bool(data.get("bildirimler", True))}


def save_tray_settings(settings: dict, path: Path | None = None) -> None:
    write_json(path or config.TRAY_SETTINGS_FILE,
               {"bildirimler": bool(settings.get("bildirimler", True))})


# --- autostart ----------------------------------------------------------------
def tray_exec_line() -> str:
    wrapper = Path.home() / ".local/bin/kulak-tepsi"
    if wrapper.exists():
        return str(wrapper)
    return f"env PYTHONPATH={config.SRC_DIR} {sys.executable} -m oto_eq.tray"


def autostart_enabled(path: Path | None = None) -> bool:
    return Path(path or AUTOSTART_FILE).exists()


# English default keys + Turkish [tr] (the session's locale picks one). The marker key
# is unchanged, so entries written by older versions are still recognised as ours.
AUTOSTART_TEXTS = (
    "Name=Kulak Kesil Tray\nName[tr]=Kulak Kesil Tepsisi\n"
    "GenericName=Smart Equalizer\nGenericName[tr]=Akıllı Ekolayzır\n"
    "Comment=Headphone EQ status in the system tray\n"
    "Comment[tr]=Kulaklık EQ'sunu bağlama göre değiştiren Kulak Kesil'in sistem tepsisi simgesi\n")


def autostart_text() -> str:
    icon = str(APP_ICON_FILE) if APP_ICON_FILE.exists() else FALLBACK_ICON
    return ("[Desktop Entry]\nType=Application\n" + AUTOSTART_TEXTS
            + f"Exec={tray_exec_line()}\nIcon={icon}\nTerminal=false\n"
            "Categories=AudioVideo;Audio;\nNoDisplay=true\nX-KDE-autostart-after=panel\n"
            f"X-GNOME-Autostart-enabled=true\n{DESKTOP_MARK}\n")


def set_autostart(on: bool, path: Path | None = None) -> bool:
    """Create/remove the tray autostart entry. Never deletes a foreign file."""
    path = Path(path or AUTOSTART_FILE)
    try:
        if on:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(autostart_text(), encoding="utf-8")
        elif path.exists():
            if DESKTOP_MARK not in path.read_text(encoding="utf-8", errors="replace"):
                log.warning("otomatik başlatma dosyası bizim değil: %s", path)
                return False
            path.unlink()
        return True
    except OSError as e:
        log.warning("otomatik başlatma değiştirilemedi: %s", e)
        return False


def _main(argv: list[str]) -> int:
    if len(argv) >= 1 and argv[0] == "icon":
        from PyQt6.QtGui import QGuiApplication

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QGuiApplication(sys.argv[:1])
        out = render_app_icon(Path(argv[1]) if len(argv) > 1 else APP_ICON_FILE)
        print(out)
        return 0
    print(_("kullanım: python3 -m oto_eq.ui_common icon [YOL]"), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
