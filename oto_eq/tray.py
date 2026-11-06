"""Kulak Kesil system tray (python3 -m oto_eq.tray).

- A QThread consumes ``ipc.watch()`` (reconnects every 3 s) and hands status
  dicts to the GUI thread via signals.
- Icon: colored round badge + letter of the dominant profile (see
  ``ui_common.badge_spec``), small white dot = "sabit" mode.
- Left click: open / raise the main window (``oto_eq.app``).
  Middle click: quick toggle Otomatik <-> Kapalı (also in the menu).
  Right click: menu (KDE draws it via SNI/dbusmenu).
- Notification when the dominant profile changes to a non-null value,
  debounced 2 s, replacing the previous one.
- Only the "Bildirimler" toggle is persisted (config.TRAY_SETTINGS_FILE).
- "Bu şarkı aslında…" submenu (while Müzik plays and the router knows the artist):
  one click = artist genre assignment (same as the GUI) + IPC reload + a feedback
  record (``geri_bildirim``) + notification "Kaydedildi: <sanatçı> → <tür>".
- Single instance via QLocalServer ``oto-eq-tray``. A client that sends the line
  ``restart`` makes the tray re-exec itself (the app sends it after a language change,
  CONTRACT CHANGE 8); any other / empty connection is ignored.
- UI texts are Turkish msgids translated with ``_()`` (CONTRACT CHANGE 8).
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer
from PyQt6.QtGui import QAction, QActionGroup, QFont
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import config, geri_bildirim
from .i18n import N_, _
from . import ui_common as ui
from . import ui_text as tx
from .models import EQ_PROFILES, MUSIC_GROUPS, VARIANT_PROFILES

log = logging.getLogger("oto_eq.tray")

RETRY_SECONDS = ui.RETRY_SECONDS
DEBOUNCE_MS = 2000
DOWN_TEXT = ui.DOWN_TEXT

# kept here for compatibility; the implementations live in ui_common
_default_watch = ui.default_watch
_default_request = ui.default_request
WatchThread = ui.WatchThread
load_settings = ui.load_tray_settings
save_settings = ui.save_tray_settings

# msgids; translated where they are shown (_(GENRE_MENU_TEXT))
GENRE_MENU_TEXT = N_("Bu şarkı aslında…")
GENRE_GENERAL_TEXT = N_("Genel (EQ farkı yok)")
GENRE_REMOVE_TEXT = N_("Atamayı kaldır")
RESTART_COMMAND = b"restart\n"
FEEDBACK_GROUPS: tuple[str, ...] = (*MUSIC_GROUPS, "")


def music_feedback_variant(st: dict | None) -> dict | None:
    """The music variant to correct: Müzik is playing and the router knows its key."""
    if not st or not st.get("active"):
        return None
    v = tx.variant_of(st, "muzik")
    if not v or not v.get("key"):
        return None
    playing = st.get("dominant") == "muzik" or any(
        isinstance(s, dict) and s.get("profile") == "muzik" for s in st.get("streams") or [])
    return v if playing else None


def feedback_header(v: dict) -> str:
    """'Virgin Black — Metal / Rock (iTunes — Rock)'."""
    who = v.get("for") or str(v.get("key") or "").split(":", 1)[-1] or "?"
    text = f"{who} — {tx._router_label(v, v.get('group'))}"
    src = tx.source_text(v.get("source"))
    if src:
        text += f" ({src})"
    return text if len(text) <= 90 else text[:89] + "…"


def tooltip_text(st: dict | None) -> str:
    head = f"{ui.APP_NAME} — {ui.APP_TAGLINE}"
    if st is None:
        return f"{head}\n{DOWN_TEXT}"
    dom = st.get("dominant")
    lines = [head, _("Mod: {mod} · Baskın profil: {profil}").format(
        mod=ui.mode_text(st), profil=ui.profile_label(dom) if dom else "—")]
    if st.get("mode") == "off":
        lines.append(ui.OFF_EXPLAIN)
    k = tx.kulaklik_of(st)
    if k is not None:
        lines.append(_("Çıkış: {aygit}").format(aygit=tx.kulaklik_text(st)))
    extra = []
    if not st.get("active"):
        extra.append(_("varsayılan çıkış kulaklık değil") if k is not None
                     and not k.get("kulaklik") else _("kulaklık aktif değil"))
    if not st.get("sinks_ready"):
        extra.append(_("EQ çıkışları hazır değil"))
    if extra:
        lines.append("⚠ " + ", ".join(extra))
    for prof in VARIANT_PROFILES:
        v = tx.variant_of(st, prof)
        if v:
            lines.append(f"{tx.VARIANT_KIND_LABELS[prof]}: {tx.variant_text(v)}")
    streams = st.get("streams") or []
    for s in streams[:8]:
        app = s.get("app") or s.get("binary") or f"#{s.get('index', '?')}"
        lines.append(f"• {app} → {ui.stream_profile_label(s)}")
    if len(streams) > 8:
        lines.append(_("… diğer akışlar: {n}").format(n=len(streams) - 8))
    if not streams:
        lines.append(_("Çalan akış yok"))
    return "\n".join(lines)



def _train_later() -> None:
    """Retrain the personal genre model in the background (cheap; it only activates itself
    when it has enough corrections AND beats the base). Router reloads it within ~60 s."""
    import threading

    def run():
        try:
            from . import kisisel_model
            kisisel_model.egit()
        except Exception as e:  # noqa: BLE001 — optional feature
            log.debug("kişisel model eğitilemedi: %s", e)
    threading.Thread(target=run, name="kisisel-model-egit", daemon=True).start()

class TrayController(QObject):
    """All tray logic; the QSystemTrayIcon is optional (tests run without one)."""

    def __init__(self, tray: QSystemTrayIcon | None = None, notifier=None, requester=None,
                 settings_path: Path | None = None, debounce_ms: int = DEBOUNCE_MS,
                 app_launcher=None, parent=None, genre_load=None, genre_save=None,
                 feedback=None):
        super().__init__(parent)
        self._genre_load = genre_load or config.load_genre_settings
        self._genre_save = genre_save or config.save_genre_settings
        self._feedback = feedback or geri_bildirim.kaydet
        self._genre_variant: dict | None = None
        self._genre_sig = None
        self._genre_group: QActionGroup | None = None
        self.genre_actions: dict[str, QAction] = {}
        self.act_genre_remove: QAction | None = None
        self.tray = tray
        self.notifier = notifier or ui.Notifier()
        self._request = requester or _default_request
        self._settings_path = settings_path
        self.settings = load_settings(settings_path)
        self._app_launcher = app_launcher or self._launch_app
        self.status: dict | None = None
        self.badge = ui.badge_spec(None)
        self._icons: dict = {}
        self._seen_first = False
        self._down_logged = False
        self._pending = None
        self._last_notified = None
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self._debounce_fired)
        self._game_timer = QTimer(self)
        self._game_timer.setSingleShot(True)
        self._game_timer.setInterval(debounce_ms)
        self._game_timer.timeout.connect(self._game_fired)
        self._build_menu()
        self._apply()
        self._watch_settings()

    def _watch_settings(self) -> None:
        """Follow changes made by the main window (same settings file)."""
        d = Path(self._settings_path or config.TRAY_SETTINGS_FILE).parent
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        self._fsw = QFileSystemWatcher([str(d)], self)
        self._fsw.directoryChanged.connect(self._settings_changed)

    def _settings_changed(self, *_a) -> None:
        new = load_settings(self._settings_path)
        if new != self.settings:
            self.settings = new
            self.act_notify.blockSignals(True)
            self.act_notify.setChecked(new["bildirimler"])
            self.act_notify.blockSignals(False)

    # --- menu -----------------------------------------------------------
    def _build_menu(self) -> None:
        m = self.menu = QMenu()
        self.act_title = m.addAction(f"{ui.APP_NAME} — {ui.APP_TAGLINE}")
        self.act_title.setEnabled(False)
        self.act_open = m.addAction(_("Kulak Kesil'i aç"))
        f = QFont(self.act_open.font())
        f.setBold(True)
        self.act_open.setFont(f)
        self.act_open.triggered.connect(self.open_app)
        self.act_toggle = m.addAction(_("EQ'yu kapat"))
        self.act_toggle.triggered.connect(self.quick_toggle)
        self.genre_menu = m.addMenu(_(GENRE_MENU_TEXT))
        self.act_genre = self.genre_menu.menuAction()
        self.act_genre.setVisible(False)
        m.aboutToShow.connect(lambda: self._sync_genre_menu(force=True))
        self.act_device = m.addAction("")          # "🎧 model (tür)": info only
        self.act_device.setEnabled(False)
        self.act_device.setVisible(False)
        m.addSeparator()
        self.mode_group = QActionGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_actions: dict[str, QAction] = {}
        for key, text in [("auto", _("Otomatik"))] + \
                [(f"fixed:{p}", _("Sabit: {profil}").format(profil=ui.profile_label(p)))
                 for p in EQ_PROFILES] + \
                [("off", _("Kapalı"))]:
            act = QAction(text, self.mode_group)
            act.setCheckable(True)
            act.triggered.connect(lambda _c=False, k=key: self.select_mode(k))
            m.addAction(act)
            self.mode_actions[key] = act
        m.addSeparator()
        self.act_notify = m.addAction(_("Bildirimler"))
        self.act_notify.setCheckable(True)
        self.act_notify.setChecked(self.settings["bildirimler"])
        self.act_notify.toggled.connect(self.set_notifications)
        self.act_reload = m.addAction(_("Kuralları yeniden yükle"))
        self.act_reload.triggered.connect(self.reload_rules)
        self.act_plot = m.addAction(_("EQ eğrilerini göster"))
        self.act_plot.triggered.connect(lambda: ui.launch_module("oto_eq.cli", "eq-ciz"))
        self.act_folder = m.addAction(_("Ayar klasörünü aç"))
        self.act_folder.triggered.connect(self.open_config_dir)
        m.addSeparator()
        self.act_quit = m.addAction(_("Çıkış"))
        self.act_quit.triggered.connect(QApplication.quit)
        if self.tray is not None:
            self.tray.setContextMenu(m)
            self.tray.activated.connect(self._activated)

    def current_mode_key(self) -> str | None:
        st = self.status
        if not st:
            return None
        if st.get("mode") == "fixed":
            return f"fixed:{st.get('fixed_profile')}"
        return st.get("mode")

    def _sync_menu(self) -> None:
        up = self.status is not None
        key = self.current_mode_key()
        for k, act in self.mode_actions.items():
            act.setEnabled(up)
            act.setChecked(k == key)
        if key not in self.mode_actions:
            # nothing checked (unknown/unreachable): exclusive groups need this
            self.mode_group.setExclusive(False)
            for act in self.mode_actions.values():
                act.setChecked(False)
            self.mode_group.setExclusive(True)
        self.act_toggle.setEnabled(up)
        self.act_toggle.setText(_("EQ'yu aç (Otomatik)") if key == "off" else _("EQ'yu kapat"))
        self.act_reload.setEnabled(up)
        self._sync_device()
        self._sync_genre_menu()

    def _sync_device(self) -> None:
        """Info line with the output device (model + type); hidden for an older router."""
        st = self.status
        k = tx.kulaklik_of(st)
        if k is None:
            self.act_device.setVisible(False)
            return
        text = f"🎧 {tx.kulaklik_kisa(st)}" if k.get("kulaklik") \
            else "🔈 " + _("{aygit} — EQ devre dışı").format(aygit=tx.kulaklik_kisa(st))
        if k.get("kulaklik") and tx.bt_arama_modu(k):
            text += f" · {tx.ARAMA_MODU}"
        self.act_device.setText(text)
        self.act_device.setToolTip(tx.kulaklik_text(st))
        self.act_device.setVisible(True)

    # --- "Bu şarkı aslında…" ------------------------------------------------
    def _sync_genre_menu(self, force: bool = False) -> None:
        v = music_feedback_variant(self.status)
        sig = None if v is None else tuple(str(v.get(k) or "") for k in
                                           ("key", "for", "group", "label", "source", "mode"))
        if not force and sig == self._genre_sig:
            return
        self._genre_sig = sig
        self._genre_variant = v
        self._build_genre_menu(v)

    def _build_genre_menu(self, v: dict | None) -> None:
        gm = self.genre_menu
        gm.clear()
        if self._genre_group is not None:
            self._genre_group.deleteLater()
            self._genre_group = None
        self.genre_actions = {}
        self.act_genre_remove = None
        self.act_genre.setVisible(v is not None)
        if v is None:
            return
        hdr = gm.addAction(feedback_header(v))
        hdr.setEnabled(False)
        gm.addSeparator()
        self._genre_group = QActionGroup(gm)
        self._genre_group.setExclusive(True)
        current = v.get("group") or ""
        for g in FEEDBACK_GROUPS:
            if g == "":
                gm.addSeparator()
            act = gm.addAction(_(GENRE_GENERAL_TEXT) if g == "" else tx.group_label(g))
            act.setCheckable(True)
            act.setChecked(g == current)
            self._genre_group.addAction(act)
            act.triggered.connect(lambda _c=False, gg=g: self.correct_genre(gg))
            self.genre_actions[g] = act
        try:
            assigned = geri_bildirim.sanatci_atamasi(str(v.get("key") or ""), v.get("for") or "",
                                                     load=self._genre_load)
        except Exception as e:  # noqa: BLE001 - a broken file must not break the menu
            log.debug("tür ataması okunamadı: %s", e)
            assigned = None
        if assigned is not None:
            gm.addSeparator()
            self.act_genre_remove = gm.addAction(_(GENRE_REMOVE_TEXT))
            self.act_genre_remove.triggered.connect(lambda: self.correct_genre(None))

    def _reload_quietly(self) -> bool:
        """IPC reload; False when not applied (``_reload_error``: None = router down, else
        its error text: it is running and rejected the reload)."""
        self._reload_error = None
        try:
            resp = self._request({"cmd": "reload"})
        except (ConnectionError, OSError, TimeoutError):
            return False
        if isinstance(resp, dict) and resp.get("ok"):
            return True
        err = resp.get("error") if isinstance(resp, dict) else resp
        self._reload_error = str(err or _("bilinmeyen hata"))
        return False

    def correct_genre(self, group: str | None) -> bool:
        """Assign the playing artist to ``group`` ('' = Genel, None = remove the
        assignment), reload the router and record the correction."""
        v = self._genre_variant
        if not v or not v.get("key"):
            return False
        key = geri_bildirim.sanatci_anahtari(v)
        if not key:
            return False
        display = v.get("for") or key
        try:
            geri_bildirim.sanatci_ata(key, group, display, load=self._genre_load,
                                      save=self._genre_save)
        except (OSError, ValueError) as e:
            self.notifier.notify(ui.APP_NAME, _("Kaydedilemedi: {hata}").format(hata=e))
            self._sync_genre_menu(force=True)
            return False
        if group is not None:
            try:
                self._feedback("muzik", str(v.get("key") or ""), display, v.get("track") or "",
                               v.get("group") or "", v.get("source") or "", group)
            except (OSError, ValueError) as e:
                log.warning("geri bildirim kaydedilemedi: %s", e)
            else:
                if self._feedback is geri_bildirim.kaydet:   # real log only (not in tests)
                    _train_later()            # CHANGE 6: personal model learns (if enough data)
        live = self._reload_quietly()
        later = "" if live else (
            _("Kaydedildi ama uygulanamadı: {hata}").format(hata=self._reload_error)
            if getattr(self, "_reload_error", None) else _("Servis çalışınca uygulanacak"))
        if group is None:
            self.notifier.notify(_("Atama kaldırıldı: {ad}").format(ad=display), later)
        else:
            self.notifier.notify(_("Kaydedildi: {ad} → {tur}").format(
                ad=display, tur=tx.group_label(group)), later)
        self._sync_genre_menu(force=True)
        return True

    # --- status ---------------------------------------------------------
    def icon_for(self, spec: ui.BadgeSpec):
        if spec not in self._icons:
            self._icons[spec] = ui.badge_icon(spec)
        return self._icons[spec]

    def _apply(self) -> None:
        self.badge = ui.badge_spec(self.status)
        self._sync_menu()
        if self.tray is not None:
            self.tray.setIcon(self.icon_for(self.badge))
            self.tray.setToolTip(tooltip_text(self.status))

    def on_status(self, st: dict) -> None:
        st = dict(st)
        st.pop("event", None)
        self.status = st
        dom = st.get("dominant") if st.get("active") and st.get("mode") != "off" else None
        if not self._seen_first:
            self._seen_first = True
            self._last_notified = dom  # don't announce what was already playing
            self._pending = dom
        elif dom != self._pending:
            self._pending = dom
            self._debounce.start()
        self._track_game(st)
        self._apply()

    @staticmethod
    def game_key(st: dict) -> tuple | None:
        """(game, group) of the playing game variant; None when no game is known."""
        v = tx.variant_of(st, "oyun")
        if not v or not v.get("for"):
            return None
        return (v.get("for"), v.get("group") or "")

    def _track_game(self, st: dict) -> None:
        """Announce a NEW game (game variant change) — never song changes."""
        key = self.game_key(st)
        if not hasattr(self, "_game_seen"):
            self._game_seen = True
            self._game_last = key            # already running at start: stay quiet
            self._game_pending = key
            return
        if key != self._game_pending:
            self._game_pending = key
            self._game_timer.start()

    def _game_fired(self) -> None:
        key = self._game_pending
        if key is None or key == self._game_last:
            if key is None:
                self._game_last = None
            return
        self._game_last = key
        if self.settings["bildirimler"]:
            game, group = key
            self.notifier.notify(f"🎮 {game}",
                                 _("Oyun türü: {tur}").format(tur=tx.group_label(group)))

    def on_down(self, _msg: str = "") -> None:
        if self.status is not None or not self._down_logged:
            log.info("servise ulaşılamıyor")
        self._down_logged = True
        self.status = None
        self._apply()

    def _debounce_fired(self) -> None:
        dom = self._pending
        if dom == self._last_notified:
            return
        self._last_notified = dom
        if dom and self.settings["bildirimler"]:
            self.notifier.notify(*ui.notify_texts(dom))

    # --- actions --------------------------------------------------------
    def _send(self, obj: dict) -> bool:
        try:
            resp = self._request(obj)
        except (ConnectionError, OSError, TimeoutError) as e:
            log.warning("istek başarısız %s: %s", obj, e)
            self.notifier.notify(ui.APP_NAME, DOWN_TEXT)
            self._sync_menu()
            return False
        if not isinstance(resp, dict) or not resp.get("ok"):
            err = resp.get("error") if isinstance(resp, dict) else resp
            self.notifier.notify(ui.APP_NAME, _("Hata: {hata}").format(hata=err))
            self._sync_menu()
            return False
        if resp.get("uyari"):
            self.notifier.notify(ui.APP_NAME, _("Uyarı: {uyari}").format(uyari=resp["uyari"]))
        return True

    def select_mode(self, key: str) -> bool:
        if key.startswith("fixed:"):
            req = {"cmd": "set_mode", "mode": "fixed", "profile": key.split(":", 1)[1]}
        else:
            req = {"cmd": "set_mode", "mode": key}
        return self._send(req)

    def quick_toggle(self) -> bool:
        turning_on = self.current_mode_key() == "off"
        ok = self.select_mode("auto" if turning_on else "off")
        if ok and self.settings["bildirimler"]:
            self.notifier.notify(ui.APP_NAME, _("Otomatik EQ açıldı") if turning_on
                                 else ui.OFF_EXPLAIN)
        return ok

    def reload_rules(self) -> bool:
        ok = self._send({"cmd": "reload"})
        if ok and self.settings["bildirimler"]:
            self.notifier.notify(ui.APP_NAME, _("Kurallar yeniden yüklendi"))
        return ok

    def set_notifications(self, on: bool) -> None:
        self.settings["bildirimler"] = bool(on)
        save_settings(self.settings, self._settings_path)

    def open_config_dir(self) -> None:
        config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ui.xdg_open(config.CONFIG_DIR)

    def _launch_app(self) -> str:
        if importlib.util.find_spec("oto_eq.app") is None:
            self.notifier.notify(ui.APP_NAME,
                                 _("Ana pencere henüz kurulu değil — menüyü kullan (sağ tık)"))
            return "failed"
        return ui.raise_or_launch_app()

    def open_app(self) -> str:
        return self._app_launcher()

    def _activated(self, reason) -> None:
        R = QSystemTrayIcon.ActivationReason
        if reason == R.Trigger:
            self.open_app()
        elif reason == R.MiddleClick:
            self.quick_toggle()


MAX_COMMAND_BYTES = 256


def restart_argv() -> list[str]:
    """argv for re-running the tray in place (same interpreter, args and environment)."""
    return [sys.executable, "-m", "oto_eq.tray", *sys.argv[1:]]


def restart_tray(server: QLocalServer | None, execv=os.execv) -> None:
    """Re-exec the tray (e.g. after a language change): free the single-instance socket
    first so the new process can claim it. ``execv`` does not return on success."""
    log.info("tepsi yeniden başlatılıyor")
    name = server.serverName() if server is not None else ""
    if server is not None:
        server.close()
    for h in logging.getLogger().handlers:
        h.flush()
    sys.stdout.flush()
    sys.stderr.flush()
    argv = restart_argv()
    try:
        execv(argv[0], argv)
    except OSError as e:           # keep running (an exception in a Qt slot would abort)
        log.warning("tepsi yeniden başlatılamadı: %s", e)
        if server is not None and name:
            QLocalServer.removeServer(name)
            server.listen(name)


def _serve_connection(server: QLocalServer, sock: QLocalSocket, execv) -> None:
    """Read one line; ``restart`` re-execs the tray, anything else is ignored."""
    buf = bytearray()
    done = {"v": False}

    def on_ready():
        if done["v"]:
            sock.readAll()
            return
        buf.extend(bytes(sock.readAll()))
        if b"\n" not in buf and len(buf) < MAX_COMMAND_BYTES:
            return                       # wait for the rest of the line
        done["v"] = True
        line = bytes(buf).split(b"\n", 1)[0] + b"\n"
        sock.disconnectFromServer()
        if line == RESTART_COMMAND:
            restart_tray(server, execv)

    sock.readyRead.connect(on_ready)
    sock.disconnected.connect(sock.deleteLater)
    if sock.bytesAvailable():
        on_ready()


def acquire_single_instance(name: str = ui.TRAY_SERVER_NAME,
                            execv=os.execv) -> QLocalServer | None:
    """Return a listening QLocalServer, or None if another instance owns it.

    Extra launches just connect & go away; a client sending ``restart\\n`` makes this
    tray re-exec itself (``execv`` is injectable for tests)."""
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.disconnectFromServer()
        return None
    QLocalServer.removeServer(name)  # stale socket from a crashed instance
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        log.warning("tekil örnek soketi açılamadı: %s", server.errorString())

    def on_new():
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is None:
                break
            _serve_connection(server, sock, execv)

    server.newConnection.connect(on_new)
    return server


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setApplicationName("oto-eq-tray")
    app.setApplicationDisplayName(ui.APP_NAME)
    app.setDesktopFileName("oto-eq-tray")
    app.setQuitOnLastWindowClosed(False)
    ui.install_excepthook(log)            # a stray exception in a slot must not abort the tray

    guard = acquire_single_instance()
    if guard is None:
        print(_("Kulak Kesil tepsisi zaten çalışıyor."), file=sys.stderr)
        return 0

    tray = QSystemTrayIcon()
    ctl = TrayController(tray)
    thread = WatchThread()
    thread.status.connect(ctl.on_status)
    thread.down.connect(ctl.on_down)
    thread.start()

    # At login the panel may not be ready yet: wait up to ~60 s for it.
    tries = {"n": 0}

    def show_when_ready():
        if QSystemTrayIcon.isSystemTrayAvailable() or tries["n"] >= 30:
            tray.show()
            return
        tries["n"] += 1
        QTimer.singleShot(2000, show_when_ready)

    show_when_ready()
    rc = app.exec()
    thread.stop()
    thread.wait(500)
    tray.hide()
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    # The watch thread may still block in a socket read inside ipc.watch();
    # skip Qt/Python teardown so "QThread destroyed while running" can't abort.
    os._exit(rc)


if __name__ == "__main__":
    sys.exit(main())
