"""Main window tests (offscreen Qt, fake IPC server, fake command runner)."""
from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Qt's GLib event dispatcher permanently acquires GLib's default main context
# for the main thread; dbus-python loops in other threads (oto_eq.mpris) then
# never run. Use Qt's own dispatcher so these tests don't break test_mpris.
os.environ.setdefault("QT_NO_GLIB", "1")

import json
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QDialog

from oto_eq import app as appmod
from oto_eq import config, ui_common as ui
from tests.test_cli import KULAKLIK_BT, KULAKLIK_HS80, KULAKLIK_SPK, STATUS, FakeServer

QAPP = QApplication.instance() or QApplication(["test"])
ORIG_MAKE_EQ_PAGE = appmod.make_eq_page


def spin(ms: int = 30) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


def st(**kw):
    base = {k: v for k, v in STATUS.items() if k != "ok"}
    return {**base, **kw}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.results = {}

    def run(self, args, on_done=None, env=None, timeout_ms=0):
        self.calls.append(list(args))
        rc, out, err = 0, "", ""
        if "is-active" in args:
            out = self.results.get("is-active", "active\ninactive\n")
        elif args[:3] == ["pactl", "list", "short"]:
            out = self.results.get("sources", "1\talsa_input.usb-Corsair_HS80-00.mono-fallback\tx\n"
                                              "2\talsa_input.pci-0000.analog-stereo\tx\n"
                                              "3\toto_eq_mikrofon\tx\n")
        elif args[:2] == ["pactl", "get-default-source"]:
            out = self.results.get("default_source", "alsa_input.usb-Corsair_HS80-00.mono-fallback\n")
        elif args[0] == "pactl" and "sinks" in args:
            out = json.dumps([{"name": STATUS["default_sink"], "description": "CORSAIR HS80 Kulaklık"}])
        elif args[-1] == "eq-uret":
            rc = self.results.get("eq-uret", 0)
        if on_done:
            on_done(rc, out, err)


class AppTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oto-eq-app-")
        d = Path(self.tmp.name)
        self.dir = d
        patches = [
            mock.patch.object(config, "SOCKET_PATH", d / "oto-eq.sock"),
            mock.patch.object(config, "CONFIG_DIR", d / "cfg"),
            mock.patch.object(config, "ASSIGNMENTS_FILE", d / "cfg" / "atamalar.json"),
            mock.patch.object(config, "TRAY_SETTINGS_FILE", d / "state" / "tepsi.json"),
            mock.patch.object(config, "STATE_DIR", d / "state"),
            # LIVE-SAFETY: the program is installed; never touch the real config/state
            mock.patch.object(config, "GENRE_FILE", d / "cfg" / "tur-atamalari.json"),
            mock.patch.object(config, "GENRE_CACHE", d / "state" / "tur-onbellek.json"),
            mock.patch.object(ui, "APP_SETTINGS_FILE", d / "state" / "pencere.json"),
            mock.patch.object(appmod, "load_micgen", lambda: None),
            mock.patch.object(appmod, "load_saglik", lambda: None),
            mock.patch.object(appmod, "make_eq_page", lambda: appmod.EqPlaceholderPage()),
            mock.patch.object(ui, "AUTOSTART_FILE", d / "autostart" / "oto-eq-tray.desktop"),
            mock.patch.object(ui, "xdg_open", lambda *_a: True),
            # never record from a real microphone in tests
            mock.patch.object(appmod.MicPage, "start_meter", lambda self: setattr(self, "_meter_started", True)),
            mock.patch.object(appmod.CalibrateDialog, "start_meter", lambda self: None),
            # Durum tiles: never read the real kisisel.json / model / onarim / profiles
            mock.patch.object(appmod, "load_tone", lambda: None),
            mock.patch.object(appmod, "load_level_info", lambda: None),
            mock.patch.object(appmod, "load_model_info", lambda: None),
            mock.patch.object(appmod, "load_onarim", lambda: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.server = FakeServer(d / "oto-eq.sock")
        self.runner = FakeRunner()
        self.win = appmod.MainWindow(runner=self.runner, settings_path=d / "pencere.json", restore=False)
        self.win.resize(1080, 720)

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        spin(5)
        self.server.close()
        self.tmp.cleanup()

    @property
    def sp(self):
        return self.win.page_status


class TestAssignmentHelpers(unittest.TestCase):
    def test_key(self):
        self.assertEqual(appmod.assignment_key({"binary": "firefox", "app": "Firefox"}), ("ikili", "firefox"))
        self.assertEqual(appmod.assignment_key({"binary": "wine64-preloader", "app": "Witcher 3"}),
                         ("uygulama", "Witcher 3"))
        self.assertEqual(appmod.assignment_key({"binary": "", "app": "mpv"}), ("uygulama", "mpv"))
        self.assertEqual(appmod.assignment_key({"binary": "python3", "app": ""}), ("ikili", "python3"))
        self.assertIsNone(appmod.assignment_key({}))

    def test_upsert_remove(self):
        items = [{"alan": "ikili", "deger": "Firefox", "profil": "muzik"}]
        out = appmod.upsert_assignment(items, "ikili", "firefox", "film")
        self.assertEqual(out, [{"alan": "ikili", "deger": "firefox", "profil": "film"}])
        out = appmod.upsert_assignment(out, "uygulama", "mpv", "")
        self.assertEqual(len(out), 2)
        self.assertEqual(appmod.find_assignment(out, "uygulama", "MPV")["profil"], "")
        self.assertEqual(appmod.remove_assignment(out, "ikili", "FIREFOX"),
                         [{"alan": "uygulama", "deger": "mpv", "profil": ""}])
        self.assertEqual(items[0]["profil"], "muzik")  # input not mutated

    def test_sink_helpers(self):
        self.assertEqual(appmod.prettify_sink("alsa_output.usb-Corsair_CORSAIR_HS80"), "Corsair HS80")
        js = json.dumps([{"name": "a", "description": "A çıkışı"}])
        self.assertEqual(appmod.parse_sink_description(js, "a"), "A çıkışı")
        self.assertIsNone(appmod.parse_sink_description("bozuk", "a"))

    def test_theme_dark_light(self):
        p = QPalette()
        p.setColor(QPalette.ColorRole.Window, QColor("#202326"))
        p.setColor(QPalette.ColorRole.WindowText, QColor("#FCFCFC"))
        t = appmod.Theme.from_palette(p, "#F28C28", 10)
        self.assertTrue(t.dark)
        self.assertGreater(t.card.lightness(), t.window.lightness())
        self.assertEqual(t.accent.name(), "#f28c28")
        p.setColor(QPalette.ColorRole.Window, QColor("#EFF0F1"))
        self.assertFalse(appmod.Theme.from_palette(p, None, 10).dark)
        self.assertIn("QFrame#card", appmod.stylesheet(t))


class TestStatusPage(AppTestBase):
    def test_render_each_status(self):
        cases = [
            (st(), "CS2 modu", "Adım sesleri öne çıkarıldı"),
            (st(dominant="film"), "Film modu", "Diyalog net"),
            (st(dominant="muzik", mode="fixed", fixed_profile="muzik"), "Müzik modu", "sabit mod"),
            (st(mode="off", dominant=None), "EQ kapalı", "Seviye eşitli düz geçiş"),
            (st(active=False, dominant=None), "Kulaklık aktif değil", "USB, Bluetooth ya da 3,5 mm"),
            (st(mode="fixed", fixed_profile="oyun", dominant=None, streams=[]), "Sabit: Oyun", "sesli sohbet"),
            (st(dominant=None, streams=[]), "Hazır, dinliyor", "EQ'lu"),
        ]
        for status, title, odak in cases:
            with self.subTest(title=title):
                self.win.on_status({"event": "status", **status})
                self.assertEqual(self.sp.hero_title.text(), title)
                self.assertIn(odak, self.sp.hero_odak.text())
                self.assertTrue(self.sp.banner.isHidden())
                self.assertFalse(self.sp.streams_card.isHidden())
                self.assertEqual(len(self.sp.rows), len(status["streams"]))
                self.assertEqual(self.sp.empty.isHidden(), bool(status["streams"]))

    def test_rows_and_subtitle(self):
        self.win.on_status(st())
        names = [r.stream["app"] for r in self.sp.rows]
        self.assertEqual(names, ["Counter-Strike 2", "Firefox", "Discord"])
        self.assertIn("Kulaklık: CORSAIR HS80 Kulaklık", self.sp.hero_sub.text())  # resolved via pactl
        self.assertIn("EQ çıkışları hazır", self.sp.hero_sub.text())
        self.win.on_status(st(sinks_ready=False))
        self.assertIn("hazır değil", self.sp.hero_sub.text())
        self.assertEqual(self.win.pill_text.text(), "Otomatik · CS2")

    def test_hero_headphone_type(self):
        # CHANGE 7: model + type from status["kulaklik"] instead of the sink name
        self.win.on_status(st(kulaklik=KULAKLIK_HS80))
        self.assertEqual(self.sp.hero_sub.text(), "Çıkış: CORSAIR HS80 RGB Wireless Gaming Receiver "
                                                  "(USB kulaklık)  ·  EQ çıkışları hazır")
        self.win.on_status(st(active=False, dominant=None, kulaklik=KULAKLIK_SPK))
        self.assertEqual(self.sp.hero_title.text(), "Kulaklık aktif değil")
        self.assertEqual(self.sp.hero_odak.text(),
                         "Varsayılan çıkış bir kulaklık değil: Hoparlör (Ryzen HD Audio Controller). "
                         "Varsayılan çıkış bir kulaklık olunca (USB, Bluetooth ya da 3,5 mm) EQ "
                         "kendiliğinden devreye girer.")
        self.assertIn("Çıkış: Ryzen HD Audio Controller (Hoparlör)", self.sp.hero_sub.text())
        self.win.on_status(st(kulaklik={**KULAKLIK_BT, "bt_profil": "headset-head-unit"}))
        self.assertIn("WH-1000XM4 (Bluetooth kulaklık) · Bluetooth arama modu (dar bant)",
                      self.sp.hero_sub.text())
        self.assertEqual(self.sp.hero_title.text(), "CS2 modu")

    def test_flat_chip(self):
        streams = [{"index": 1, "app": "Discord", "binary": "Discord", "profile": None,
                    "reason": "sesli sohbet", "sink": "oto_eq_duz"},
                   {"index": 2, "app": "Firefox", "binary": "firefox", "profile": None,
                    "reason": "elle taşınmış", "sink": "alsa_output.hdmi"}]
        self.win.on_status(st(streams=streams, dominant=None))
        chips = [r.findChild(appmod.QLabel, "chip") for r in self.sp.rows]
        self.assertEqual([c.text() for c in chips], ["Düz", "EQ yok"])
        self.assertEqual(chips[0].toolTip(), "EQ'suz, seviye eşitli")
        self.assertEqual(self.sp.mode_buttons["off"].toolTip(), "EQ kapalı (seviye eşitli düz geçiş)")

    def test_mode_segment_sync(self):
        self.win.on_status(st(mode="fixed", fixed_profile="film", dominant="film"))
        checked = [k for k, b in self.sp.mode_buttons.items() if b.isChecked()]
        self.assertEqual(checked, ["fixed:film"])
        self.assertEqual(self.win.pill_text.text(), "Sabit: Film")

    def test_mode_buttons_send_json(self):
        self.win.on_status(st())
        for key in ("fixed:cs2", "fixed:muzik", "off", "auto"):
            self.sp.mode_buttons[key].click()
        self.assertEqual(self.server.requests, [
            {"cmd": "set_mode", "mode": "fixed", "profile": "cs2"},
            {"cmd": "set_mode", "mode": "fixed", "profile": "muzik"},
            {"cmd": "set_mode", "mode": "off"},
            {"cmd": "set_mode", "mode": "auto"},
        ])

    def test_mode_failure_reverts(self):
        self.win.on_status(st())
        self.server.close()
        os.unlink(config.SOCKET_PATH)
        self.sp.mode_buttons["off"].click()
        checked = [k for k, b in self.sp.mode_buttons.items() if b.isChecked()]
        self.assertEqual(checked, ["auto"])
        self.assertIn("Servis çalışmıyor", self.win.toast.text())

    def test_daemon_down_banner(self):
        self.win.on_status(st())
        self.win.on_down("yok")
        self.assertFalse(self.sp.banner.isHidden())
        self.assertTrue(self.sp.streams_card.isHidden())
        self.assertEqual(self.sp.hero_title.text(), "Servis çalışmıyor")
        self.assertTrue(all(not b.isEnabled() for b in self.sp.mode_buttons.values()))
        self.assertEqual(self.win.pill_text.text(), "Servis çalışmıyor")
        self.sp.btn_start.click()
        self.assertIn(["systemctl", "--user", "start", "oto-eq.service", "oto-eq-sinks.service"],
                      self.runner.calls)
        self.win.on_status(st())
        self.assertTrue(self.sp.banner.isHidden())

    def test_assign_via_row_menu_writes_and_reloads(self):
        self.win.on_status(st())
        row = self.sp.rows[1]  # Firefox
        sub = row.menu.actions()[0].menu()
        film = next(a for a in sub.actions() if a.text() == "Film")
        film.trigger()
        self.assertEqual(config.load_assignments(), [{"alan": "ikili", "deger": "firefox", "profil": "film"}])
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        self.assertIn("Firefox artık hep Film", self.win.toast.text())
        # menu now reflects the assignment, removal works
        row = self.sp.rows[1]
        sub = row.menu.actions()[0].menu()
        self.assertTrue(next(a for a in sub.actions() if a.text() == "Film").isChecked())
        rm = next(a for a in row.menu.actions() if a.text() == "Atamayı kaldır")
        self.assertTrue(rm.isEnabled())
        rm.trigger()
        self.assertEqual(config.load_assignments(), [])
        self.assertEqual(self.server.requests[-1], {"cmd": "reload"})

    def test_assign_eqsuz_and_daemon_down(self):
        self.win.on_status(st())
        self.server.close()
        os.unlink(config.SOCKET_PATH)
        self.win.assign(st()["streams"][0], "")
        self.assertEqual(config.load_assignments(), [{"alan": "ikili", "deger": "cs2", "profil": ""}])
        self.assertIn("servis çalışınca", self.win.toast.text())


class FakeDialog:
    value_to_return = None

    def __init__(self, parent, item=None, title=""):
        self.item = item

    def exec(self):
        return QDialog.DialogCode.Accepted

    def value(self):
        return self.value_to_return or self.item


class TestRulesPage(AppTestBase):
    def test_add_edit_delete(self):
        rp = self.win.page_rules
        self.win.show_page(3)
        FakeDialog.value_to_return = {"alan": "uygulama", "deger": "VLC media player", "profil": "film"}
        self.assertTrue(rp.edit_item(None, dialog_cls=FakeDialog))
        self.assertEqual(rp.table.rowCount(), 1)
        self.assertEqual(rp.table.item(0, 2).text(), "Film")
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        # edit: change value + profile
        FakeDialog.value_to_return = {"alan": "ikili", "deger": "vlc", "profil": ""}
        rp.edit_item(config.load_assignments()[0], new=False, dialog_cls=FakeDialog)
        self.assertEqual(config.load_assignments(), [{"alan": "ikili", "deger": "vlc", "profil": ""}])
        self.assertEqual(rp.table.item(0, 2).text(), "Düz")
        # delete
        rp.table.selectRow(0)
        self.assertTrue(rp.btn_del.isEnabled())
        rp.delete_selected()
        self.assertEqual(config.load_assignments(), [])
        self.assertFalse(rp.empty.isHidden())
        self.assertEqual(len(self.server.requests), 3)

    def test_from_playing_menu(self):
        rp = self.win.page_rules
        rp._fill_from_menu()
        self.assertEqual(rp.from_menu.actions()[0].text(), "Şu an çalan uygulama yok")
        self.win.on_status(st())
        rp._fill_from_menu()
        texts = [a.text() for a in rp.from_menu.actions()]
        self.assertEqual(len(texts), 3)
        self.assertIn("Firefox  (firefox) — şu an Müzik", texts)

    def test_open_rules_copies_default(self):
        self.win.page_rules.open_rules_file()
        self.assertTrue((config.CONFIG_DIR / config.RULES_FILE).exists())

    def test_reload_button(self):
        self.win.reload_rules(announce=True)
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        self.assertIn("yeniden yüklendi", self.win.toast.text())

    def test_dialog_value(self):
        dlg = appmod.AssignmentDialog(self.win, {"alan": "flatpak", "deger": "com.spotify.Client",
                                                 "profil": "muzik"})
        self.assertEqual(dlg.value(), {"alan": "flatpak", "deger": "com.spotify.Client", "profil": "muzik"})
        dlg.deger.setText("  ")
        self.assertFalse(dlg.ok_button.isEnabled())
        dlg.deleteLater()


class TestRecentApps(AppTestBase):
    def write_history(self, entries):
        f = config.STATE_DIR / "gorulen.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"surum": 1, "uygulamalar": entries}), encoding="utf-8")

    def entry(self, **kw):
        e = {"ikili": "spotify", "uygulama": "Spotify", "flatpak": "", "ilk": time.time() - 900,
             "son": time.time() - 300, "sayi": 4, "kategori": "muzik", "profil": "muzik",
             "neden": "bilinen müzik uygulaması"}
        e.update(kw)
        return e

    def test_rows_and_assign(self):
        self.write_history([self.entry(),
                            self.entry(ikili="wine64-preloader", uygulama="Witcher 3", kategori="oyun",
                                       profil="oyun", sayi=2, son=time.time() - 7200)])
        rp = self.win.page_rules
        self.win.show_page(3)
        self.assertFalse(rp.recent_card.isHidden())
        self.assertEqual(len(rp.recent_rows), 2)
        self.assertEqual(rp.recent_count.text(), "uygulama: 2")      # no plural forms needed
        r0 = rp.recent_rows[0]
        self.assertEqual(r0.stream["app"], "Spotify")
        texts = [w.text() for w in r0.findChildren(appmod.QLabel)]
        self.assertIn("5 dk önce · çalma: 4", texts)
        self.assertIn("spotify · Müzik", texts)
        # Ata ▾ -> Film on the wine game assigns by app name, not wine64-preloader
        r1 = rp.recent_rows[1]
        sub = r1.menu.actions()[0].menu()
        next(a for a in sub.actions() if a.text() == "Film").trigger()
        self.assertEqual(config.load_assignments(),
                         [{"alan": "uygulama", "deger": "Witcher 3", "profil": "film"}])
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        # rows rebuilt and marked as assigned
        self.assertIsNotNone(rp.recent_rows[1].assigned)

    def test_show_all_toggle(self):
        self.write_history([self.entry(ikili=f"app{i}", uygulama=f"App {i}", son=time.time() - i)
                            for i in range(12)])
        rp = self.win.page_rules
        rp.reload_recent()
        self.assertEqual(len(rp.recent_rows), rp.RECENT_SHORT)
        self.assertFalse(rp.recent_more.isHidden())
        rp.recent_more.click()
        self.assertEqual(len(rp.recent_rows), 12)
        self.assertEqual(rp.recent_more.text(), "Daha az göster")

    def test_empty_and_fresh_reads(self):
        rp = self.win.page_rules
        rp.reload_recent()
        self.assertFalse(rp.recent_card.isHidden())
        self.assertFalse(rp.recent_empty.isHidden())
        self.write_history([self.entry()])   # written later by "the router"
        rp.reload_recent()
        self.assertEqual(len(rp.recent_rows), 1)

    def test_hidden_without_history_module(self):
        import oto_eq
        with mock.patch.dict(sys.modules, {"oto_eq.history": None}), \
             mock.patch.object(oto_eq, "history", None, create=True):
            delattr(oto_eq, "history")
            self.win.page_rules.reload_recent()
            self.assertTrue(self.win.page_rules.recent_card.isHidden())

    def test_ago(self):
        now = 1_000_000.0
        self.assertEqual(appmod.ago(now - 10, now), "az önce")
        self.assertEqual(appmod.ago(now - 600, now), "10 dk önce")
        self.assertEqual(appmod.ago(now - 3 * 3600, now), "3 sa önce")
        self.assertEqual(appmod.ago(now - 30 * 3600, now), "dün")
        self.assertEqual(appmod.ago(now - 5 * 86400, now), "5 gün önce")

    def test_flatpak_key(self):
        self.assertEqual(appmod.assignment_key({"binary": "", "app": "", "flatpak": "com.spotify.Client"}),
                         ("flatpak", "com.spotify.Client"))


class FakeMicgen:
    def __init__(self, restart=False):
        self.cfg = {"gurultu": {"aktif": True, "esik": 50}, "netlik": {"kazanc": 3.0},
                    "kompresor": {"esik": -30, "telafi": 6.0}}
        self.saved, self.live, self.restart = [], [], restart

    def load_mic(self):
        import copy
        return copy.deepcopy(self.cfg)

    def save_mic(self, cfg):
        self.saved.append(cfg)
        self.cfg = cfg

    def needs_restart(self, old, new):
        return self.restart

    def set_live(self, cfg):
        self.live.append(cfg)
        return getattr(self, "live_ok", True)        # the real micgen.set_live returns bool


class TestMicPage(AppTestBase):
    @property
    def mp(self):
        return self.win.page_mic

    def test_nav_position(self):
        self.assertEqual([k for k, _ in appmod.PAGES], ["durum", "eq", "mikrofon", "kurallar", "ayarlar"])
        self.win.show_page(2)
        self.assertIs(self.win.stack.currentWidget(), self.mp)

    def test_status_and_default_toggle(self):
        self.mp.refresh()
        self.assertTrue(self.mp.state["present"])
        self.assertIn("varsayılan değil", self.mp.status_line.text())
        self.assertEqual(self.mp.btn_default.text(), "Varsayılan mikrofon yap")
        self.assertTrue(self.mp.btn_start.isHidden())
        self.mp.btn_default.click()
        self.assertIn(["pactl", "set-default-source", "oto_eq_mikrofon"], self.runner.calls)
        # now it is the default -> button goes back to the HS80 mic (not the onboard one)
        self.runner.results["default_source"] = "oto_eq_mikrofon\n"
        self.mp.refresh()
        self.assertEqual(self.mp.btn_default.text(), "HS80'e geri dön")
        self.assertIn("varsayılan mikrofon", self.mp.status_line.text())
        self.mp.btn_default.click()
        self.assertEqual(self.runner.calls[-4], ["pactl", "set-default-source",
                                                 "alsa_input.usb-Corsair_HS80-00.mono-fallback"])

    def test_not_running(self):
        self.runner.results["sources"] = "1\talsa_input.usb-Corsair_HS80-00.mono-fallback\tx\n"
        self.runner.results["is-active"] = "failed\n"
        self.mp.refresh()
        self.assertFalse(self.mp.state["present"])
        self.assertIn("Çalışmıyor", self.mp.status_line.text())
        self.assertFalse(self.mp.btn_start.isHidden())
        self.assertFalse(self.mp.btn_test.isEnabled())
        self.mp.btn_start.click()
        self.assertIn(["systemctl", "--user", "start", "oto-eq-mic.service"], self.runner.calls)

    def test_level_math_and_meter(self):
        import math
        from array import array
        silence = bytes(3200)
        self.assertEqual(appmod.pcm_level_db(silence), (appmod.LEVEL_FLOOR_DB, appmod.LEVEL_FLOOR_DB))
        a = array("h", [int(16384 * math.sin(i / 5)) for i in range(1600)])
        rms, peak = appmod.pcm_level_db(a.tobytes())
        self.assertAlmostEqual(peak, -6.0, delta=0.2)
        self.assertAlmostEqual(rms, -9.0, delta=0.5)
        self.mp.feed_audio(a.tobytes())
        self.assertEqual(self.mp.level_text.text(), "-6 dBFS")
        self.assertGreater(self.mp.meter.frac(self.mp.meter.rms), 0.8)
        self.mp.feed_audio(silence)
        self.assertEqual(self.mp.level_text.text(), "sessiz")

    def test_meter_only_while_visible(self):
        self.mp._meter_started = False
        self.mp.refresh()   # hidden page: no recording
        self.assertFalse(self.mp._meter_started)
        self.assertNotIn("parec", [c[0] for c in self.runner.calls])

    def test_params_disabled_without_micgen(self):
        with mock.patch.object(appmod, "load_micgen", return_value=None):
            page = appmod.MicPage(self.win)
        self.assertTrue(all(not c[0].isEnabled() for c in page.controls.values()))
        self.assertIn("micgen", page.param_note.text())
        self.assertFalse(page.param_changed(("netlik", "kazanc"), 5.0))

    def test_params_live_and_restart(self):
        gen = FakeMicgen()
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        ctl, val, _k, step = page.controls[("netlik", "kazanc")]
        self.assertTrue(ctl.isEnabled())
        self.assertEqual(val.text(), "3 dB")
        self.assertTrue(page.param_changed(("netlik", "kazanc"), 5.0))
        self.assertEqual(gen.saved[-1]["netlik"]["kazanc"], 5.0)
        self.assertEqual(len(gen.live), 1)
        self.assertNotIn(["systemctl", "--user", "restart", "oto-eq-mic.service"], self.runner.calls)
        gen.restart = True
        page.controls[("gurultu", "aktif")][0].click()   # toggle off -> restart path
        self.assertFalse(gen.saved[-1]["gurultu"]["aktif"])
        self.assertIn(["systemctl", "--user", "restart", "oto-eq-mic.service"], self.runner.calls)
        self.assertFalse(page.param_changed(("netlik", "kazanc"), 5.0))  # unchanged -> no-op

    def test_params_keep_outside_changes(self):
        gen = FakeMicgen()
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        self.assertEqual(page.cfg["kompresor"]["esik"], -30)
        # meanwhile `kulak mikrofon kalibre --uygula` writes a new threshold
        gen.cfg = {**gen.load_mic(), "kompresor": {"esik": -31.0, "telafi": 6.0}}
        self.assertTrue(page.param_changed(("netlik", "kazanc"), 5.0))
        self.assertEqual(gen.saved[-1]["kompresor"]["esik"], -31.0)    # not reverted to -30
        self.assertEqual(gen.saved[-1]["netlik"]["kazanc"], 5.0)
        ctl, val, _k, step = page.controls[("kompresor", "esik")]
        self.assertEqual(ctl.value() * step, -31.0)                     # the page shows it too
        # the same value already in the file: nothing to write, but the controls catch up
        gen.cfg = {**gen.load_mic(), "netlik": {"kazanc": 6.0}}
        n = len(gen.saved)
        self.assertFalse(page.param_changed(("netlik", "kazanc"), 6.0))
        self.assertEqual(len(gen.saved), n)
        self.assertEqual(page.cfg["netlik"]["kazanc"], 6.0)

    def test_stop_process_does_not_block(self):
        # a child that ignores SIGTERM: SIGKILL follows after the grace time, the GUI never waits
        proc = appmod.QProcess(self.win)
        proc.start(sys.executable, ["-c", "import signal, time; signal.signal(signal.SIGTERM, "
                                          "signal.SIG_IGN); print('ok', flush=True); time.sleep(30)"])
        self.assertTrue(proc.waitForStarted(5000))
        self.assertTrue(proc.waitForReadyRead(5000))
        t0 = time.monotonic()
        appmod.stop_process(proc, 200)
        self.assertLess(time.monotonic() - t0, 0.1)
        self.assertTrue(wait_until(lambda: proc.state() == appmod.QProcess.ProcessState.NotRunning, 5.0))
        appmod.stop_process(None)                    # no process: nothing to do

    def test_failed_live_set_falls_back_to_restart(self):
        gen = FakeMicgen()
        gen.live_ok = False                        # running graph did not take the value
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        self.assertTrue(page.param_changed(("netlik", "kazanc"), 5.0))
        self.assertIn(["systemctl", "--user", "restart", "oto-eq-mic.service"], self.runner.calls)

    def test_mode_override_shown_and_locked(self):
        gen = FakeMicgen()
        gen.cfg["genel"] = {"mod": "discord", "aktif": True}
        gen.cfg["mod"] = {"discord": {"gurultu": {"aktif": False}}}
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        ctl = page.controls[("gurultu", "aktif")][0]
        self.assertFalse(ctl.isChecked())          # what runs, not the base table's True
        self.assertFalse(ctl.isEnabled())
        self.assertIn("discord", ctl.toolTip())
        self.assertTrue(page.controls[("netlik", "kazanc")][0].isEnabled())

    def test_partial_config_disables_missing(self):
        gen = FakeMicgen()
        del gen.cfg["kompresor"]
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        self.assertFalse(page.controls[("kompresor", "esik")][0].isEnabled())
        self.assertTrue(page.controls[("netlik", "kazanc")][0].isEnabled())
        self.assertIn("Bazı ayarlar", page.param_note.text())

    def test_test_dialog_parsing(self):
        out = ("🎙  5 saniye boyunca normal sesinle konuş…\n\n"
               "             gürültü   konuşma     tepe  (dBFS)\n"
               "Ham            -62.0     -28.0     -6.0\n"
               "İşlenmiş       -80.0     -22.0     -3.0\n"
               "🔊 ham ses çalınıyor…\n")
        self.assertEqual(appmod.parse_mic_test(out), [("Ham", -62.0, -28.0, -6.0),
                                                       ("İşlenmiş", -80.0, -22.0, -3.0)])
        dlg = appmod.MicTestDialog(self.win, 5, start=False)
        dlg.feed(out)
        self.assertFalse(dlg.table.isHidden())
        self.assertEqual(dlg.table.item(1, 2).text(), "-22.0 dB")
        self.assertIn("ham 34 dB → işlenmiş 58 dB", dlg.verdict.text())
        self.assertIn("Ham ses çalınıyor", dlg.title.text())
        dlg.finished_with(0)
        self.assertIn("Test bitti", dlg.title.text())
        dlg.deleteLater()

    def test_test_dialog_rows_in_separate_chunks(self):
        # PYTHONUNBUFFERED=1: every print() of mikrofon_test reaches feed() on its own
        out = ("🎙  5 saniye boyunca normal sesinle konuş…\n\n"
               "             gürültü   konuşma     tepe  (dBFS)\n"
               "Ham            -62.0     -28.0     -6.0\n"
               "İşlenmiş       -80.0     -22.0     -3.0\n"
               "🔊 ham ses çalınıyor…\n")
        dlg = appmod.MicTestDialog(self.win, 5, start=False)
        for _ in range(2):               # 2nd pass = "Tekrarla" (start()'s resets, filled table)
            dlg.out = ""
            dlg.table.hide()
            dlg.verdict.hide()
            for line in out.splitlines(keepends=True):
                dlg.feed(line)
            self.assertEqual(dlg.table.rowCount(), 2)
            self.assertEqual(dlg.table.item(1, 0).text(), "İşlenmiş")
            self.assertEqual(dlg.table.item(1, 2).text(), "-22.0 dB")
            self.assertFalse(dlg.verdict.isHidden())
            self.assertIn("ham 34 dB → işlenmiş 58 dB", dlg.verdict.text())
        dlg.deleteLater()


class TestGuideAndCs2Card(AppTestBase):
    def test_welcome_fits_screen_and_scrolls(self):
        dlg = self.win.show_welcome()
        self.addCleanup(dlg.deleteLater)
        self.assertIs(dlg.checks["ses"].window(), dlg)
        self.assertIsNotNone(dlg.scroll.widget())
        self.assertTrue(dlg.scroll.widget().isAncestorOf(dlg.checks["mikrofon"]))
        self.assertFalse(dlg.scroll.isAncestorOf(dlg.btn_close))          # "Tamam" never scrolls away
        avail = dlg.screen().availableGeometry().height()
        self.assertLessEqual(dlg.height(), max(avail - 60, dlg.minimumHeight()))
        self.assertGreater(dlg.content_height, 300)
        self.assertLessEqual(dlg.minimumHeight(), 360)

    def test_welcome_once_and_persist(self):
        self.assertTrue(self.win.maybe_show_welcome())
        dlg = self.win.findChild(appmod.WelcomeDialog)
        self.assertIsNotNone(dlg)
        self.assertEqual(list(dlg.checks), ["ses", "cs2", "discord", "mikrofon"])
        self.assertIn("0/4", dlg.progress.text())
        dlg.checks["ses"].click()
        dlg.checks["cs2"].click()
        self.assertEqual(self.win.app_settings()["rehber"],
                         {"ses": True, "cs2": True, "discord": False, "mikrofon": False})
        self.assertIn("2/4", dlg.progress.text())
        dlg.accept()
        self.assertTrue(self.win.app_settings()["rehber_goruldu"])
        self.assertFalse(self.win.maybe_show_welcome())
        # re-open from Ayarlar keeps the ticks
        self.win.page_settings.btn_guide.click()
        dlg2 = [d for d in self.win.findChildren(appmod.WelcomeDialog) if d is not dlg][0]
        self.assertTrue(dlg2.checks["cs2"].isChecked())
        dlg2.reject()
        # geometry save must not wipe the guide state
        self.win.save_state()
        self.assertTrue(self.win.app_settings()["rehber_goruldu"])

    def texts(self, w):
        return " ".join(l.text() for l in w.findChildren(appmod.QLabel))

    def test_discord_mic_line_only_when_default(self):
        dlg = appmod.WelcomeDialog(self.win)
        self.assertNotIn("Giriş cihazı", self.texts(dlg))
        self.assertIn("Natural", self.texts(dlg))
        self.assertIn("10 dB", self.texts(dlg))
        dlg.deleteLater()
        self.win.page_mic.state["default"] = "oto_eq_mikrofon"
        dlg = appmod.WelcomeDialog(self.win)
        self.assertIn("Giriş cihazı: Kulak Kesil · Mikrofon", self.texts(dlg))
        dlg.deleteLater()

    def test_test_button_needs_mic(self):
        dlg = appmod.WelcomeDialog(self.win)
        with mock.patch.object(self.win.page_mic, "run_test") as rt:
            dlg.btn_test.click()
            rt.assert_not_called()
            self.assertIn("çalışmıyor", self.win.toast.text())
            self.win.page_mic.state["present"] = True
            dlg.btn_test.click()
            rt.assert_called_once()
        dlg.deleteLater()

    def test_cs2_card(self):
        sp = self.win.page_status
        self.win.on_status(st(streams=[s for s in st()["streams"] if s["profile"] != "cs2"], dominant="muzik"))
        self.assertTrue(sp.cs2_card.isHidden())
        self.win.on_status(st())
        self.assertFalse(sp.cs2_card.isHidden())
        self.assertIn("Natural", self.texts(sp.cs2_card))
        sp.btn_cs2_hide.click()                     # this session only
        self.win.on_status(st())
        self.assertTrue(sp.cs2_card.isHidden())
        self.assertNotIn("cs2_kart_gizli", self.win.app_settings())
        w2 = appmod.MainWindow(runner=FakeRunner(), settings_path=self.dir / "pencere.json", restore=False)
        w2.on_status(st())
        self.assertFalse(w2.page_status.cs2_card.isHidden())
        w2.page_status.btn_cs2_never.click()        # forever
        self.assertTrue(self.win.app_settings()["cs2_kart_gizli"])
        w3 = appmod.MainWindow(runner=FakeRunner(), settings_path=self.dir / "pencere.json", restore=False)
        w3.on_status(st())
        self.assertTrue(w3.page_status.cs2_card.isHidden())
        w2.deleteLater()
        w3.deleteLater()


def wait_until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        spin(20)
    return cond()


class FakeCalibMicgen(FakeMicgen):
    """calibrate() exists (the dialog runs it in a subprocess; never called here)."""

    def __init__(self, applied=None):
        super().__init__()
        self.calls = []
        self.applied_with = []
        self.conf_written = []
        self.apply_result = applied or {"degisti": True, "kaydedildi": True, "conf": True,
                                        "canli": True, "yeniden_baslat": False}

    def calibrate(self, seconds=5.0, apply=False):
        self.calls.append((seconds, apply))
        raise AssertionError("the GUI must not record in-process")

    def apply_suggestion(self, oneri):
        self.applied_with.append(dict(oneri))
        if not self.apply_result.get("hata"):
            for key, val in oneri.items():
                sec, k = key.split(".")
                self.cfg.setdefault(sec, {})[k] = val
        return dict(self.apply_result)

    def write_conf(self, data=None):
        self.conf_written.append(data)


CALIB_OK = {"konusma_dbfs": -24.3, "gurultu_dbfs": -62.0, "tepe_dbfs": -4.1,
            "oneri": {"kompresor.esik": -27.0, "kompresor.telafi": 7.5, "gurultu.esik": 50.0},
            "tahmini_cikis_dbfs": -18.2, "aciklama": "x", "uygulandi": False}


class FakeProc:
    def __init__(self):
        self.killed = False

    def state(self):
        return appmod.QProcess.ProcessState.NotRunning if self.killed else \
            appmod.QProcess.ProcessState.Running

    def kill(self):
        self.killed = True

    def waitForFinished(self, _ms):
        return True


class TestCalibration(AppTestBase):
    def page_with(self, gen, **runner_results):
        self.runner.results.update(runner_results)
        with mock.patch.object(appmod, "load_micgen", return_value=gen):
            page = appmod.MicPage(self.win)
        page.refresh()
        return page

    def dialog(self, gen=None, **runner_results):
        gen = gen or FakeCalibMicgen()
        page = self.page_with(gen, **runner_results)
        launched = []

        def fake_launch(dlg, program, args):
            launched.append([program, *args])
            return FakeProc()
        p = mock.patch.object(appmod.CalibrateDialog, "_launch", fake_launch)
        p.start()
        self.addCleanup(p.stop)
        dlg = page.run_calibration()
        self.addCleanup(dlg.deleteLater)
        return gen, page, dlg, launched

    def measured(self, res=None, **kw):
        gen, page, dlg, launched = self.dialog(**kw)
        dlg.btn_start.click()
        dlg.measure_done(0, "INFO bir şey\n" + json.dumps(res or CALIB_OK) + "\n", "")
        return gen, page, dlg, launched

    def visible(self, dlg):
        return {b.text() for b in (dlg.btn_cancel, dlg.btn_again, dlg.btn_start, dlg.btn_apply,
                                   dlg.btn_apply_default, dlg.btn_test) if not b.isHidden()}

    # --- page + intro
    def test_button_and_intro(self):
        gen, page, dlg, launched = self.dialog()
        self.assertEqual(page.btn_calib.text(), "Mikrofonu kalibre et…")
        self.assertTrue(page.btn_calib.isEnabled())
        self.assertEqual(dlg.state, "intro")
        self.assertIn("10 saniye boyunca normal sesinle konuş; arada kısa sessizlik bırak",
                      dlg.hint.text())
        self.assertIn("kaydedilmez", dlg.hint.text())
        self.assertEqual(self.visible(dlg), {"Vazgeç", "Başla"})
        self.assertTrue(dlg.warn.isHidden())
        self.assertEqual(launched, [])                       # nothing recorded before Başla

    def test_intro_warns_service_down_and_no_hs80(self):
        _g, page, dlg, _l = self.dialog(sources="2\talsa_input.pci-0000.analog-stereo\tx\n",
                                        **{"is-active": "failed\n"})
        self.assertTrue(page.btn_calib.isEnabled())         # raw mic: service not required
        self.assertFalse(dlg.warn.isHidden())
        self.assertIn("HS80 mikrofonu şu an görünmüyor", dlg.warn.text())
        self.assertIn("çalışmıyor", dlg.warn.text())

    def test_missing_api_disables_button(self):
        page = self.page_with(FakeMicgen())                  # no calibrate()
        self.assertFalse(page.btn_calib.isEnabled())
        with mock.patch.object(appmod, "load_micgen", return_value=None):
            page2 = appmod.MicPage(self.win)
        self.assertFalse(page2.btn_calib.isEnabled())

    # --- measuring
    def test_measure_runs_subprocess_with_countdown(self):
        gen, page, dlg, launched = self.dialog()
        dlg.btn_start.click()
        self.assertEqual(launched, [[sys.executable, "-m", "oto_eq.micgen", "kalibre", "--json",
                                     "--saniye", "10"]])
        self.assertEqual(gen.calls, [])                       # not in-process
        self.assertEqual(dlg.state, "measuring")
        self.assertEqual(dlg.count.text(), "10")
        self.assertFalse(dlg.meter.isHidden())
        self.assertEqual(self.visible(dlg), {"Vazgeç"})
        dlg._countdown()
        self.assertEqual(dlg.count.text(), "9")
        dlg.start()                                           # double start ignored
        self.assertEqual(len(launched), 1)
        for _ in range(9):
            dlg._countdown()
        self.assertIn("Değerlendiriliyor", dlg.title.text())
        dlg.meter.set_level(-20.0, -10.0)                     # meter widget works
        self.assertGreater(dlg.meter.frac(dlg.meter.rms), 0.5)

    def test_result_before_after_table(self):
        gen, page, dlg, _l = self.measured()
        self.assertEqual(dlg.state, "result")
        self.assertIn("Ölçüm tamam", dlg.title.text())
        self.assertIn("Konuşman -24.3 dBFS, ortam gürültüsü -62.0 dBFS (aradaki fark 38 dB)",
                      dlg.summary.text())
        self.assertIn("≈ -18.2 dBFS", dlg.summary.text())
        rows = {r[0]: r for r in dlg.rows}
        self.assertEqual(rows["Kompresör eşiği"][1:3], ("-30 dBFS", "-27 dBFS"))
        self.assertEqual(rows["Ses kazancı (telafi)"][1:3], ("+6 dB", "+7.5 dB"))
        self.assertEqual(rows["Gürültü kapısı (VAD eşiği)"][1:3], ("%50", "%50"))
        self.assertFalse(rows["Gürültü kapısı (VAD eşiği)"][4])          # unchanged
        self.assertTrue(all(r[3] for r in dlg.rows))                     # one-line explanations
        self.assertFalse(dlg.rows_box.isHidden())
        texts = [dlg.grid.itemAt(i).widget().text() for i in range(dlg.grid.count())
                 if isinstance(dlg.grid.itemAt(i).widget(), appmod.QLabel)]
        self.assertIn("%50 (aynı)", texts)
        self.assertEqual(self.visible(dlg), {"Vazgeç", "Yeniden ölç", "Uygula",
                                             "Uygula ve varsayılan mikrofon yap"})

    def test_result_uses_effective_values_and_findings(self):
        gen = FakeCalibMicgen()
        gen.effective = lambda cfg: {**cfg, "kompresor": {"esik": -20.0, "telafi": 3.0}}
        res = {**CALIB_OK, "konusma_dbfs": -50.0, "gurultu_dbfs": -58.0, "tepe_dbfs": -2.0}
        _g, _p, dlg, _l = self.measured(res, gen=gen)
        rows = {r[0]: r for r in dlg.rows}
        self.assertEqual(rows["Kompresör eşiği"][1], "-20 dBFS")         # mode override shown
        note = dlg.note.text()
        self.assertIn("kırpılabilir", note)
        self.assertIn("çok kısık", note)
        self.assertIn("Ortam gürültülü", note)

    def test_already_default_hides_default_button(self):
        _g, _p, dlg, _l = self.measured(default_source="oto_eq_mikrofon\n")
        self.assertTrue(dlg.btn_apply_default.isHidden())
        self.assertFalse(dlg.btn_apply.isHidden())

    # --- errors
    def test_error_kinds(self):
        cases = [({"hata": "HS80 mikrofonu bulunamadı (kulaklık açık ve bağlı mı?)",
                   "kod": "mikrofon_yok"}, "Mikrofon bulunamadı", "Kulaklığı ya da mikrofonu"),
                 ({"hata": "Konuşma algılanmadı. Mikrofon açık mı?", "kod": "sessiz",
                   "konusma_dbfs": -240.0, "gurultu_dbfs": -70.0, "tepe_dbfs": -60.0},
                  "Konuşma algılanmadı", "sessize alma"),
                 ({"hata": "Mikrofon kırpılıyor (tepe +0.0 dBFS).", "konusma_dbfs": -10.0,
                   "gurultu_dbfs": -60.0, "tepe_dbfs": 0.0}, "Ses çok yüksek (kırpılıyor)", "uzaklaştır"),
                 ({"hata": "mikrofon.toml okunamadı: bozuk", "kod": "ayar"},
                  "Mikrofon ayarları okunamadı", "bozuk")]
        for res, title, todo in cases:
            with self.subTest(title=title):
                _g, _p, dlg, _l = self.measured(res)
                self.assertEqual(dlg.state, "error")
                self.assertEqual(dlg.title.text(), title)
                self.assertIn(todo, dlg.hint.text())
                self.assertEqual(self.visible(dlg), {"Kapat", "Yeniden ölç"})
                self.assertTrue(dlg.rows_box.isHidden())
                if title.startswith("Ses çok yüksek"):
                    self.assertIn("tepe 0.0 dBFS", dlg.note.text())
                    self.assertIn("kırpılıyor", dlg.note.text())

    def test_bad_output_timeout_and_failed_start(self):
        _g, _p, dlg, launched = self.dialog()
        dlg.btn_start.click()
        dlg.measure_done(2, "", "INFO x\nmikrofon.toml sözdizimi hatası: satır 3\n")
        self.assertEqual(dlg.title.text(), "Ölçüm yapılamadı")
        self.assertIn("sözdizimi hatası: satır 3", dlg.hint.text())
        dlg.btn_again.click()                                  # Yeniden ölç
        self.assertEqual(len(launched), 2)
        proc = dlg.proc
        dlg._timed_out()
        self.assertTrue(proc.killed)
        self.assertIn("zaman aşımı", dlg.hint.text())
        dlg.btn_again.click()
        dlg._proc_error(dlg.proc, appmod.QProcess.ProcessError.FailedToStart)
        self.assertIn("başlatılamadı", dlg.hint.text())

    def test_real_qprocess_plumbing_without_recording(self):
        page = self.page_with(FakeCalibMicgen())
        dlg = appmod.CalibrateDialog(page)
        self.addCleanup(dlg.deleteLater)
        line = json.dumps(CALIB_OK)
        dlg.command = lambda: (sys.executable, ["-c", f"import sys; print('log', file=sys.stderr); "
                                                      f"print({line!r})"])
        dlg.start()
        self.assertTrue(wait_until(lambda: dlg.state == "result", 10.0))
        self.assertEqual(len(dlg.rows), 3)
        self.assertIsNone(dlg.proc)

    def test_cancel_kills_measurement(self):
        _g, _p, dlg, _l = self.dialog()
        dlg.btn_start.click()
        proc = dlg.proc
        codes = []
        dlg.finished.connect(codes.append)
        dlg.btn_cancel.click()
        self.assertTrue(proc.killed)
        self.assertEqual(codes, [QDialog.DialogCode.Rejected.value])

    # --- applying
    def test_apply_live_no_restart(self):
        gen, page, dlg, launched = self.measured()
        dlg.btn_apply.click()
        self.assertTrue(wait_until(lambda: dlg.state == "done"))
        self.assertEqual(gen.applied_with, [CALIB_OK["oneri"]])
        self.assertEqual(len(launched), 1)                     # no second recording
        self.assertNotIn(["systemctl", "--user", "restart", "oto-eq-mic.service"], self.runner.calls)
        self.assertNotIn(["pactl", "set-default-source", "oto_eq_mikrofon"], self.runner.calls)
        self.assertIn("hemen uygulandı", dlg.hint.text())
        self.assertEqual(dlg.title.text(), "✓  Uygulandı")
        self.assertEqual(self.visible(dlg), {"Kapat", "Mikrofon testi"})
        self.assertEqual(page.controls[("kompresor", "telafi")][1].text(), "7.5 dB")  # reloaded
        self.assertIn("kalibre edildi", self.win.toast.text())

    def test_apply_restart_then_default(self):
        gen = FakeCalibMicgen({"degisti": True, "canli": False, "yeniden_baslat": True})
        _g, _p, dlg, _l = self.measured(gen=gen)
        dlg.btn_apply_default.click()
        self.assertTrue(wait_until(lambda: dlg.state == "done", 4.0))
        restart = ["systemctl", "--user", "restart", "oto-eq-mic.service"]
        default = ["pactl", "set-default-source", "oto_eq_mikrofon"]
        self.assertIn(restart, self.runner.calls)
        self.assertGreater(self.runner.calls.index(default), self.runner.calls.index(restart))
        self.assertIn("yeniden başlatıldı", dlg.hint.text())
        self.assertIn("Varsayılan mikrofon artık", dlg.hint.text())

    def test_apply_restart_failure(self):
        gen = FakeCalibMicgen({"degisti": True, "canli": False, "yeniden_baslat": True})
        _g, _p, dlg, _l = self.measured(gen=gen)
        orig = self.runner.run

        def run(args, on_done=None, env=None, timeout_ms=0):
            if "restart" in args:
                self.runner.calls.append(list(args))
                return on_done(1, "", "Job failed")
            return orig(args, on_done, env, timeout_ms)
        self.runner.run = run
        dlg.btn_apply_default.click()
        self.assertTrue(wait_until(lambda: dlg.state == "done"))
        self.assertEqual(dlg.title.text(), "Kısmen uygulandı")
        self.assertIn("Job failed", dlg.hint.text())
        self.assertNotIn(["pactl", "set-default-source", "oto_eq_mikrofon"], self.runner.calls)
        self.assertFalse(dlg.btn_test.isEnabled())

    def test_apply_error_keeps_result(self):
        gen = FakeCalibMicgen({"hata": "kaydedilemedi: salt okunur"})
        _g, _p, dlg, _l = self.measured(gen=gen)
        dlg.btn_apply.click()
        self.assertTrue(wait_until(lambda: dlg.title.text() == "Uygulanamadı"))
        self.assertEqual(dlg.state, "result")
        self.assertIn("salt okunur", dlg.hint.text())
        self.assertTrue(dlg.btn_apply.isEnabled())
        self.assertNotIn(["systemctl", "--user", "restart", "oto-eq-mic.service"], self.runner.calls)

    def test_cancel_ignored_while_applying(self):
        _g, _p, dlg, _l = self.measured()
        dlg._set_state("applying")
        self.assertFalse(dlg.btn_cancel.isEnabled())
        dlg.reject()
        self.assertEqual(dlg.state, "applying")

    def test_open_test_after_apply(self):
        _g, page, dlg, _l = self.measured()
        dlg.btn_apply.click()
        self.assertTrue(wait_until(lambda: dlg.state == "done"))
        opened, codes = [], []
        dlg.finished.connect(codes.append)
        with mock.patch.object(page, "run_test", lambda: opened.append(1)):
            dlg.btn_test.click()
            self.assertTrue(wait_until(lambda: opened == [1]))
        self.assertEqual(codes, [QDialog.DialogCode.Accepted.value])

    def test_legacy_micgen_uses_page_apply(self):
        class Legacy(FakeMicgen):                   # older micgen: no apply_suggestion
            def __init__(self):
                super().__init__()
                self.conf_written = []

            def calibrate(self, seconds=5.0, apply=False):
                raise AssertionError("no in-process recording")

            def write_conf(self, data=None):
                self.conf_written.append(data)
        gen = Legacy()
        _g, page, dlg, _l = self.measured(gen=gen)
        dlg.btn_apply.click()
        self.assertEqual(dlg.state, "done")
        self.assertEqual(gen.saved[-1]["kompresor"], {"esik": -27.0, "telafi": 7.5})
        self.assertEqual(len(gen.saved), 1)
        self.assertEqual(gen.conf_written, [gen.saved[-1]])    # conf regenerated too

    def test_apply_values_regenerates_conf_before_restart(self):
        gen = FakeCalibMicgen()
        gen.restart = True
        page = self.page_with(gen)
        order = []
        gen.write_conf = lambda data=None: order.append("conf")
        orig = self.runner.run
        self.runner.run = lambda args, on_done=None, env=None, timeout_ms=0: (
            order.append("restart") if "restart" in args else None, orig(args, on_done, env, timeout_ms))
        self.assertTrue(page.param_changed(("netlik", "kazanc"), 5.0))
        self.assertEqual(order, ["conf", "restart"])

    # --- pure helpers
    def test_helpers(self):
        self.assertIsNone(appmod.parse_calib_output("bozuk\n{yarım"))
        self.assertEqual(appmod.parse_calib_output('x\n{"a": 1}\n'), {"a": 1})
        self.assertEqual(appmod.calib_error_kind({"hata": "Mikrofon bulunamadı"}), "mikrofon_yok")
        self.assertEqual(appmod.calib_error_kind({"hata": "garip"}), "kayit")
        rows = appmod.calib_rows({"oneri": {"kompresor.oran": 4.0, "yeni.anahtar": 1}},
                                 {"kompresor": {"oran": 3.0}})
        self.assertEqual(rows[0][:3], ("Kompresör oranı", "3:1", "4:1"))
        self.assertEqual(rows[1][:3], ("yeni.anahtar", "—", "1"))
        self.assertEqual(appmod.calib_findings({"konusma_dbfs": -24.0, "gurultu_dbfs": -62.0,
                                                "tepe_dbfs": -6.0}), [])


class FakeSaglik:
    def __init__(self, results):
        self.results = results

    def run_checks(self):
        time.sleep(0.05)
        return self.results


class TestHealth(AppTestBase):
    RESULTS = [{"ad": "HS80", "durum": "tamam", "ayrinti": "takılı", "oneri": ""},
               {"ad": "servis oto-eq", "durum": "hata", "ayrinti": "kurulu değil", "oneri": "./install.sh"},
               {"ad": "WP test kalıntıları", "durum": "uyari", "ayrinti": "3 kayıt", "oneri": "zararsız"}]

    def page_with(self, mod):
        with mock.patch.object(appmod, "load_saglik", return_value=mod):
            return appmod.SettingsPage(self.win)

    def test_run_in_thread_and_render(self):
        page = self.page_with(FakeSaglik(self.RESULTS))
        self.assertFalse(page.health_card.isHidden())
        page.btn_health.click()
        self.assertFalse(page.btn_health.isEnabled())         # GUI not blocked, button busy
        self.assertTrue(wait_until(lambda: page.btn_health.isEnabled()))
        self.assertEqual(len(page.health_rows), 3)
        first = " ".join(l.text() for l in page.health_rows[0].findChildren(appmod.QLabel))
        self.assertIn("servis oto-eq", first)                 # errors sorted first
        self.assertIn("→ ./install.sh", first)
        last = " ".join(l.text() for l in page.health_rows[2].findChildren(appmod.QLabel))
        self.assertNotIn("→", last)                            # no öneri for "tamam"
        self.assertEqual(page.health_summary.text(), "Özet: 1 tamam, 1 uyarı, 1 hata")
        self.assertEqual(page.btn_health.text(), "Yeniden kontrol et")

    def test_all_ok_and_hidden_without_module(self):
        page = self.page_with(FakeSaglik(self.RESULTS[:1]))
        page.run_health()
        self.assertTrue(wait_until(lambda: page.health_summary.isVisibleTo(page)))
        self.assertIn("her şey yolunda", page.health_summary.text())
        self.assertTrue(self.page_with(None).health_card.isHidden())


VARIANTS = {"muzik": {"group": "metal_rock", "label": "Metal / Rock", "for": "Virgin Black",
                      "source": "MusicBrainz: gothic metal", "mode": "auto"},
            "oyun": {"group": "aksiyon_rpg", "label": "Aksiyon / RPG", "for": "Path of Exile 2",
                     "source": "Steam: Action RPG", "mode": "fixed", "key": "steam:2694490"}}


class FakeGenreDialog:
    value_to_return = None

    def __init__(self, parent, item=None, title=""):
        self.item = item

    def exec(self):
        return QDialog.DialogCode.Accepted

    def value(self):
        return self.value_to_return or self.item


class TestVariantsUi(AppTestBase):
    def status_v(self, **kw):
        streams = [{"index": 52, "app": "Spotify", "binary": "spotify", "category": "muzik",
                    "profile": "muzik", "reason": "bilinen müzik uygulaması", "sink": "oto_eq_muzik"},
                   {"index": 77, "app": "Path of Exile 2", "binary": "PathOfExileSteam.exe",
                    "category": "oyun", "profile": "oyun", "reason": "Proton", "sink": "oto_eq_oyun"}]
        return st(streams=streams, dominant="muzik", variants=VARIANTS, **kw)

    def test_chip_and_menu(self):
        self.win.on_status(self.status_v())
        rows = self.win.page_status.rows
        chip = rows[0].findChild(appmod.QLabel, "chip")
        self.assertEqual(chip.text(), "Müzik · Metal / Rock — Virgin Black")
        self.assertIn("Kaynak: MusicBrainz — gothic metal", chip.toolTip())
        self.assertIsNotNone(rows[0].variant_button)
        acts = [x for x in rows[0].variant_menu.actions() if not x.isSeparator()]
        self.assertEqual(acts[0].text(), "Otomatik (içeriğe göre)")
        self.assertTrue(acts[0].isChecked())                    # music variant is auto
        next(x for x in acts if x.text() == "Elektronik / Hip-hop").trigger()
        self.assertEqual(self.server.requests[-1], {"cmd": "set_variant", "profile": "muzik", "group": "elektronik"})
        game_acts = [x for x in rows[1].variant_menu.actions() if not x.isSeparator()]
        self.assertTrue(next(x for x in game_acts if x.text() == "Aksiyon / RPG").isChecked())  # fixed
        game_acts[0].trigger()
        self.assertEqual(self.server.requests[-1], {"cmd": "set_variant", "profile": "oyun", "group": "auto"})
        game_acts[1].trigger()
        self.assertEqual(self.server.requests[-1], {"cmd": "set_variant", "profile": "oyun", "group": ""})

    def test_rows_rebuilt_on_variant_change(self):
        self.win.on_status(self.status_v())
        v2 = {**VARIANTS, "muzik": {**VARIANTS["muzik"], "for": "Sisters of Mercy"}}
        self.win.on_status(self.status_v() | {"variants": v2})
        chip = self.win.page_status.rows[0].findChild(appmod.QLabel, "chip")
        self.assertIn("Sisters of Mercy", chip.text())

    def test_hero_variant_odak(self):
        cfg = config.CONFIG_DIR
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / config.PROFILES_FILE).write_text(
            '[profil.muzik.tur.metal_rock]\nodak = "Gitar duvarı net, bas çamurlaşmaz"\n', encoding="utf-8")
        self.win.on_status(self.status_v())
        hv = self.win.page_status.hero_variant
        self.assertFalse(hv.isHidden())
        self.assertEqual(hv.text(), "Tür: Metal / Rock — Virgin Black — Gitar duvarı net, bas çamurlaşmaz")
        self.win.on_status(self.status_v() | {"dominant": "cs2"})
        self.assertTrue(hv.isHidden())

    def test_no_variants_no_button(self):
        self.win.on_status(st())
        self.assertIsNone(self.win.page_status.rows[0].variant_button)   # cs2 row
        chip = self.win.page_status.rows[1].findChild(appmod.QLabel, "chip")
        self.assertEqual(chip.text(), "Müzik")


class TestGenreSection(AppTestBase):
    @property
    def gs(self):
        return self.win.page_rules.genre

    def test_add_edit_delete(self):
        FakeGenreDialog.value_to_return = {"kind": "sanatci", "key": "virgin black", "group": "metal_rock"}
        self.assertTrue(self.gs.edit_item(None, dialog_cls=FakeGenreDialog))
        self.assertEqual(config.load_genre_settings()["sanatci"], {"virgin black": "metal_rock"})
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        self.assertEqual(self.gs.table.rowCount(), 1)
        self.assertEqual(self.gs.table.item(0, 2).text(), "Metal / Rock")
        FakeGenreDialog.value_to_return = {"kind": "oyun", "key": "steam:2694490", "group": "aksiyon_rpg"}
        self.gs.edit_item(None, dialog_cls=FakeGenreDialog)
        # edit the artist into a different group
        FakeGenreDialog.value_to_return = {"kind": "sanatci", "key": "virgin black", "group": "klasik_akustik"}
        self.gs.edit_item(self.gs.items()[0] if self.gs.items()[0]["kind"] == "sanatci" else self.gs.items()[1],
                          new=False, dialog_cls=FakeGenreDialog)
        data = config.load_genre_settings()
        self.assertEqual(data["sanatci"], {"virgin black": "klasik_akustik"})
        self.assertEqual(data["oyun"], {"steam:2694490": "aksiyon_rpg"})
        self.assertTrue(data["cevrimici"])                       # untouched
        self.gs.table.selectRow(1)
        self.gs.delete_selected()
        self.assertEqual(config.load_genre_settings()["oyun"], {})

    def test_dialog_normalizes_and_switches_groups(self):
        dlg = appmod.GenreDialog(self.win)
        dlg.key.setText("  Virgin   BLACK ")
        self.assertEqual(dlg.value()["key"], "virgin black")
        groups = [dlg.group.itemData(i) for i in range(dlg.group.count())]
        self.assertIn("metal_rock", groups)
        dlg.kind.setCurrentIndex(dlg.kind.findData("oyun"))
        groups = [dlg.group.itemData(i) for i in range(dlg.group.count())]
        self.assertIn("rekabetci", groups)
        self.assertNotIn("metal_rock", groups)
        dlg.key.setText("steam:2694490")
        self.assertEqual(dlg.value(), {"kind": "oyun", "key": "steam:2694490", "group": "rekabetci"})
        dlg.deleteLater()

    def test_from_playing(self):
        self.gs.fill_from_menu()
        self.assertFalse(self.gs.from_menu.actions()[0].isEnabled())
        self.win.on_status(st(variants=VARIANTS))
        self.gs.fill_from_menu()
        texts = [x.text() for x in self.gs.from_menu.actions()]
        self.assertEqual(texts, ["Sanatçı: Virgin Black — şu an Metal / Rock",
                                 "Oyun: Path of Exile 2 — şu an Aksiyon / RPG"])
        seen = []
        with mock.patch.object(self.gs, "edit_item", side_effect=lambda item, **kw: seen.append(item)):
            for x in self.gs.from_menu.actions():
                x.trigger()
        self.assertEqual(seen, [{"kind": "sanatci", "key": "virgin black", "group": "metal_rock"},
                                {"kind": "oyun", "key": "steam:2694490", "group": "aksiyon_rpg"}])

    def test_clear_cache(self):
        config.GENRE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        config.GENRE_CACHE.write_text("{}", encoding="utf-8")
        self.gs.confirm = lambda text: False
        self.assertFalse(self.gs.clear_cache())
        self.assertTrue(config.GENRE_CACHE.exists())
        self.gs.confirm = lambda text: True
        self.assertTrue(self.gs.clear_cache())
        self.assertFalse(config.GENRE_CACHE.exists())
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        self.assertTrue(self.gs.clear_cache())                    # missing file is fine

    def test_online_toggle(self):
        sw = self.win.page_settings.sw_online
        self.assertTrue(sw.isChecked())
        sw.click()
        self.assertFalse(config.load_genre_settings()["cevrimici"])
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])
        sw.click()
        self.assertTrue(config.load_genre_settings()["cevrimici"])

    def test_audio_analysis_toggle_live(self):
        sp = self.win.page_settings
        sw = sp.sw_audio
        self.assertTrue(sw.isChecked())                            # default on
        sw.click()
        data = config.load_genre_settings()
        self.assertFalse(data["ses_analizi"])
        self.assertTrue(data["cevrimici"])                         # other flag untouched
        self.assertEqual(self.server.requests, [{"cmd": "reload"}])  # pushed live, no restart
        self.assertEqual(self.win.toast.text(), "Ses analizi kapalı")
        self.assertFalse(any("restart" in c for c in self.runner.calls))
        sw.click()
        self.assertTrue(config.load_genre_settings()["ses_analizi"])
        self.assertEqual(self.win.toast.text(), "Ses analizi açık")

    def test_audio_toggle_daemon_down_and_sync(self):
        sp = self.win.page_settings
        self.server.close()
        sp.sw_audio.click()
        self.assertFalse(config.load_genre_settings()["ses_analizi"])   # still saved
        self.assertIn("servis çalışınca uygulanacak", self.win.toast.text())
        config.save_genre_settings({**config.load_genre_settings(), "ses_analizi": True, "cevrimici": False})
        sp.sync_from_disk()
        self.assertTrue(sp.sw_audio.isChecked())
        self.assertFalse(sp.sw_online.isChecked())

    def test_genre_sources_text(self):
        text = self.win.page_settings.genre_sources.text()
        for name in ("iTunes", "Deezer", "YouTube etiketleri", "MusicBrainz", "yerleşik liste",
                     "ses analizi", "YouTube video kimliği", "Çevrimiçi açıkken"):
            self.assertIn(name, text)


class TestSettingsPage(AppTestBase):
    def test_toggles_persist(self):
        sp = self.win.page_settings
        self.assertTrue(sp.sw_notify.isChecked())
        sp.sw_notify.click()
        self.assertEqual(ui.load_tray_settings(), {"bildirimler": False})
        self.assertFalse(sp.sw_autostart.isChecked())
        sp.sw_autostart.click()
        text = ui.AUTOSTART_FILE.read_text(encoding="utf-8")
        self.assertIn(ui.DESKTOP_MARK, text)
        self.assertTrue("oto_eq.tray" in text or "kulak-tepsi" in text)
        sp.sw_autostart.click()
        self.assertFalse(ui.AUTOSTART_FILE.exists())

    def test_autostart_entry_localized_and_old_entry_still_ours(self):
        self.assertTrue(ui.set_autostart(True))
        text = ui.AUTOSTART_FILE.read_text(encoding="utf-8")
        for line in ("Name=Kulak Kesil Tray", "Name[tr]=Kulak Kesil Tepsisi",
                     "GenericName=Smart Equalizer", "GenericName[tr]=Akıllı Ekolayzır",
                     "Comment=Headphone EQ status in the system tray", "Comment[tr]=Kulaklık EQ'sunu"):
            self.assertIn(line, text)
        # an entry written by an older version (Turkish only, same marker) is removed too
        ui.AUTOSTART_FILE.write_text("[Desktop Entry]\nType=Application\nName=Kulak Kesil Tepsisi\n"
                                     "Exec=kulak-tepsi\nX-OtoEQ-Installed=true\n", encoding="utf-8")
        self.assertTrue(ui.set_autostart(False))
        self.assertFalse(ui.AUTOSTART_FILE.exists())

    def test_regenerate_child_speaks_window_language(self):
        from oto_eq import i18n
        envs = []
        self.runner.run = lambda args, on_done=None, env=None, timeout_ms=0: envs.append((args, env))
        i18n.sifirla("de", {})
        self.addCleanup(i18n.sifirla)
        self.win.page_settings.regenerate()
        args, env = envs[-1]
        self.assertEqual(args[-1], "eq-uret")
        self.assertEqual(env["OTO_EQ_DIL"], "de")       # not English via LC_ALL=C + "otomatik"
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["PYTHONPATH"], str(config.SRC_DIR))

    def test_foreign_autostart_not_deleted(self):
        ui.AUTOSTART_FILE.parent.mkdir(parents=True)
        ui.AUTOSTART_FILE.write_text("[Desktop Entry]\n", encoding="utf-8")
        self.assertFalse(ui.set_autostart(False))
        self.assertTrue(ui.AUTOSTART_FILE.exists())

    def test_services_and_regen(self):
        sp = self.win.page_settings
        sp.refresh_services()
        dot, lab, b_start, _ = sp.svc_widgets[config.DAEMON_SERVICE]
        self.assertEqual(lab.text(), "Çalışıyor")
        self.assertFalse(b_start.isEnabled())
        _, lab2, b_start2, _ = sp.svc_widgets[config.SINKS_SERVICE]
        self.assertEqual(lab2.text(), "Durdu")
        self.assertTrue(b_start2.isEnabled())
        b_start2.click()
        self.assertIn(["systemctl", "--user", "start", "oto-eq-sinks.service"], self.runner.calls)
        sp.btn_regen.click()
        self.assertEqual(self.runner.calls[-2][-3:], ["-m", "oto_eq.cli", "eq-uret"])
        self.assertIn("EQ yeniden üretildi", self.win.toast.text())
        self.assertTrue(sp.btn_regen.isEnabled())
        self.runner.results["eq-uret"] = 3
        sp.regenerate()
        self.assertIn("servis çalışmıyor", self.win.toast.text())

    def test_copy_uninstall(self):
        self.win.page_settings.copy_uninstall()
        self.assertTrue(QApplication.clipboard().text().endswith("uninstall.sh"))


class TestWindowMisc(AppTestBase):
    def test_eq_page_placeholder_and_set_status(self):
        self.assertIsInstance(self.win.page_eq, (appmod.Page,) + ((object,) if "oto_eq.eq_page" in sys.modules else ()))
        with mock.patch.dict(sys.modules, {"oto_eq.eq_page": None}):
            self.assertIsInstance(ORIG_MAKE_EQ_PAGE(), appmod.EqPlaceholderPage)
        got = []

        class FakeEq(appmod.QWidget):
            def __init__(self, parent=None):
                super().__init__(parent)

            def set_status(self, s):
                got.append(s)

        mod = types.ModuleType("oto_eq.eq_page")
        mod.EqPage = FakeEq
        with mock.patch.dict(sys.modules, {"oto_eq.eq_page": mod}):
            page = ORIG_MAKE_EQ_PAGE()
        self.assertIsInstance(page, FakeEq)
        self.win.page_eq = page
        self.win.on_status(st())
        self.assertEqual(got[-1]["dominant"], "cs2")

        class Broken(appmod.QWidget):
            def __init__(self, parent=None):
                raise RuntimeError("bozuk")
        mod.EqPage = Broken
        with mock.patch.dict(sys.modules, {"oto_eq.eq_page": mod}), \
             mock.patch.object(appmod.log, "exception"):
            self.assertIsInstance(ORIG_MAKE_EQ_PAGE(), appmod.EqPlaceholderPage)

    def test_eq_tone_hooks(self):
        """Kişisel ton: closing discards an unsaved preview, re-activation reloads."""
        from PyQt6.QtCore import QEvent
        calls = []

        class FakeEq(appmod.QWidget):
            def discard_tone_preview(self, sync=False):
                calls.append(("discard", sync))

            def reload_tone(self):
                calls.append(("reload",))

        self.win.page_eq = FakeEq()
        self.win.close()
        self.assertIn(("discard", True), calls)
        calls.clear()
        with mock.patch.object(self.win, "isActiveWindow", return_value=True):
            QApplication.sendEvent(self.win, QEvent(QEvent.Type.ActivationChange))
        self.assertEqual(calls, [("reload",)])
        calls.clear()
        with mock.patch.object(self.win, "isActiveWindow", return_value=False):
            QApplication.sendEvent(self.win, QEvent(QEvent.Type.ActivationChange))
        self.assertEqual(calls, [])

        class Broken(appmod.QWidget):
            def discard_tone_preview(self, sync=False):
                raise RuntimeError("bozuk")
        self.win.page_eq = Broken()
        with mock.patch.object(appmod.log, "exception") as logged:
            self.win.leave_eq_preview()                   # never raises
        logged.assert_called_once()
        self.win.page_eq = appmod.EqPlaceholderPage()     # no hooks: no-op
        self.win.leave_eq_preview()

    def test_navigation_and_state_persist(self):
        self.win.show_page(4)
        self.assertIs(self.win.stack.currentWidget(), self.win.page_settings)
        self.assertTrue(self.win.nav_buttons[4].isChecked())
        self.win.save_state()
        data = json.loads((self.dir / "pencere.json").read_text())
        self.assertEqual(data["sayfa"], 4)
        w2 = appmod.MainWindow(runner=FakeRunner(), settings_path=self.dir / "pencere.json")
        self.assertEqual(w2.stack.currentIndex(), 4)
        w2.deleteLater()

    def test_accent_follows_dominant(self):
        self.win.on_status(st(dominant="film"))
        self.assertEqual(self.win.theme.accent.name(), QColor(ui.PROFILE_COLORS["film"]).name())
        self.win.on_status(st(dominant=None))
        self.assertEqual(self.win.theme.accent, self.win.theme.highlight)

    def test_single_instance_raise(self):
        name = f"oto-eq-app-test-{uuid.uuid4().hex[:8]}"
        self.assertFalse(appmod.notify_running_instance(name))
        server = appmod.serve_single_instance(self.win, name)
        try:
            with mock.patch.object(self.win, "bring_to_front") as btf:
                self.assertTrue(appmod.notify_running_instance(name))
                end = time.monotonic() + 2
                while not btf.called and time.monotonic() < end:
                    spin(20)
                btf.assert_called()
        finally:
            server.close()

    def test_tray_raise_helper_matches(self):
        name = f"oto-eq-app-test-{uuid.uuid4().hex[:8]}"
        server = appmod.serve_single_instance(self.win, name)
        try:
            with mock.patch.object(self.win, "bring_to_front") as btf:
                self.assertEqual(ui.raise_or_launch_app(name, launcher=lambda: True), "raised")
                end = time.monotonic() + 2
                while not btf.called and time.monotonic() < end:
                    spin(20)
                btf.assert_called()
        finally:
            server.close()


# =========================================================================
# Durum: "Akıllı özellikler" tiles + "Yenilikler" card (F1)
# =========================================================================
from oto_eq import ui_text as tx  # noqa: E402

F1_KISMA = {"acik": True, "aktif": False, "konusma": False, "kisma_db": -0.0, "hedef_db": 6.0,
         "profiller": ["oyun", "muzik", "film"], "kisilan": [], "sesli": [], "olcum": "",
         "seviye_db": None}
F1_VARIANTS = {"muzik": {"group": "metal_rock", "label": "Metal / Rock", "for": "Virgin Black",
                      "key": "sanatci:virgin black", "source": "iTunes: Rock", "mode": "auto",
                      "track": "Lamenting Kiss", "ses": None},
            "oyun": {"group": "", "label": "Genel", "for": "", "key": "", "source": "", "mode": "auto",
                     "track": ""}}
F1_TONE = {"genel": {"bas": 1.5, "vokal": 0.0, "parlaklik": -1.0}, "tur": {}}
F1_LEVEL = {"sinirlayici": ["oyun", "muzik", "film"], "seviye": ["muzik", "film"], "crossfeed": [],
         "seviye_db": -8.42}


def smart_st(**kw):
    return st(dominant="muzik", variants=F1_VARIANTS, kisma=dict(F1_KISMA), **kw)


class TestTileTexts(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(tx.fmt_db(1.5), "+1,5")
        self.assertEqual(tx.fmt_db(-1), "−1")
        self.assertEqual(tx.fmt_db(0.04), "0")
        self.assertEqual(tx.fmt_db(-6, unit=True), "−6 dB")
        self.assertEqual(tx.percent(0.78), "%78")
        self.assertEqual(tx.percent(None), "")
        self.assertEqual(tx.tone_text({"bas": 1.5, "vokal": 0, "parlaklik": -1}),
                         "Bas +1,5 · Vokal 0 · Parlaklık −1")
        self.assertEqual(tx.source_short("iTunes: Rock"), "iTunes")
        self.assertEqual(tx.source_short("ses analizi: bas ağır, 124 BPM"), "Ses analizi")
        self.assertEqual(tx.source_short(""), "")

    def test_genre_tile(self):
        t = tx.genre_tile(smart_st())
        self.assertEqual(t["title"], "Müzik türü")
        self.assertEqual(t["value"], "Metal / Rock · iTunes")
        self.assertEqual(t["lines"], ["Virgin Black — Lamenting Kiss"])
        v = {**F1_VARIANTS["muzik"], "ses": {"group": "elektronik", "label": "Elektronik", "confidence": 0.78,
                                         "source": "ses analizi", "for": "Virgin Black"}}
        t = tx.genre_tile(smart_st() | {"variants": {"muzik": v}})
        self.assertIn("ses analizi: Elektronik %78", t["lines"])
        # the audio analysis itself decided: no repeated line, confidence in the value
        v2 = {**v, "group": "elektronik", "label": "Elektronik", "source": "ses analizi: bas ağır"}
        t = tx.genre_tile(smart_st() | {"variants": {"muzik": v2}})
        # the group's label comes from OUR catalog (the router's "label" may be in its own
        # language); the real router sends the same GROUP_LABELS text
        self.assertEqual(t["value"].replace("\u00a0", " "), "Elektronik / Hip-hop · Ses analizi %78")
        self.assertEqual(t["lines"], ["Virgin Black — Lamenting Kiss"])
        # nothing playing yet
        idle = st(dominant=None, streams=[], variants={"muzik": {**F1_VARIANTS["oyun"]}})
        self.assertEqual(tx.genre_tile(idle)["value"], "Bekliyor")
        # older router without variants: no tile
        self.assertIsNone(tx.genre_tile(st()))
        self.assertIsNone(tx.genre_tile(None))

    def test_genre_tile_follows_game(self):
        s = st(dominant="oyun", streams=[{"index": 1, "app": "Game", "profile": "oyun"}],
               variants={**F1_VARIANTS, "oyun": {"group": "rekabetci", "label": "Rekabetçi / FPS",
                                              "for": "Valorant", "key": "oyun:1", "source": "Steam: FPS",
                                              "mode": "fixed", "track": ""}})
        t = tx.genre_tile(s)
        self.assertEqual(t["title"], "Oyun türü")
        self.assertEqual(t["value"], "Rekabetçi / FPS · Steam · sabit")

    def test_kisma_tile_states(self):
        self.assertIsNone(tx.kisma_tile(st()))
        self.assertIsNone(tx.kisma_tile(st(kisma={"hata": "x"})))
        off = tx.kisma_tile(st(kisma={**F1_KISMA, "acik": False}))
        self.assertEqual((off["value"], off["state"]), ("Kapalı", "off"))
        ready = tx.kisma_tile(st(kisma=F1_KISMA))
        self.assertEqual((ready["value"], ready["state"]), ("Hazır", "on"))
        listening = tx.kisma_tile(st(kisma={**F1_KISMA, "aktif": True, "sesli": ["Discord"]}))
        self.assertEqual(listening["value"], "Hazır")
        self.assertIn("Discord dinleniyor", listening["lines"])
        hot = tx.kisma_tile(st(kisma={**F1_KISMA, "aktif": True, "konusma": True, "kisma_db": 6.0,
                                      "sesli": ["Discord"]}))
        self.assertEqual((hot["value"], hot["state"]), ("Konuşma var · −6 dB", "hot"))

    def test_tone_level_model_onarim_tiles(self):
        t = tx.tone_tile({"genel": {"bas": 0, "vokal": 0, "parlaklik": 0}, "tur": {}})
        self.assertEqual(t["value"], "Ayarlanmadı")
        self.assertTrue(t["link"])
        self.assertIn("Ekolayzır", t["lines"][0])
        t = tx.tone_tile(F1_TONE)
        self.assertEqual(t["value"], "Bas +1,5 · Vokal 0 · Parlaklık −1")
        t = tx.tone_tile({**F1_TONE, "tur": {"muzik:metal_rock": {"bas": 1.0}}}, smart_st())
        self.assertIn("bu tür için ayrıca: Bas +1", t["lines"])
        self.assertIsNone(tx.tone_tile(None))

        lv = tx.level_tile(F1_LEVEL)
        self.assertEqual(lv["value"], "Sınırlayıcı açık")
        self.assertEqual(lv["lines"], ["seviye dengeleme: Müzik, Film"])
        self.assertIn("−8,4 dB", lv["tip"])
        self.assertEqual(tx.level_tile({"sinirlayici": [], "seviye": []})["state"], "off")

        m = tx.model_tile({"aktif": False, "gerek": 20, "duzeltme": 3})
        self.assertEqual((m["value"], m["state"]), ("Öğreniyor 3/20 düzeltme", "wait"))
        self.assertEqual(tx.model_tile({"aktif": True, "gerek": 20, "duzeltme": 30})["value"], "Etkin")
        self.assertEqual(tx.model_tile({"aktif": False, "gerek": 20, "duzeltme": 25})["value"], "Eğitime hazır")

        now = 1_761_400_000.0
        self.assertIsNone(tx.onarim_tile([]))
        self.assertIsNone(tx.onarim_tile([{"zaman": now, "neden": "x", "deneme": True}]))
        o = tx.onarim_tile([{"zaman": now - 60, "neden": "EQ çıkışları yeniden başlatıldı", "sonuc": "tamam"}], now)
        self.assertTrue(o["value"].startswith("Son: "))
        self.assertEqual(o["state"], "wait")
        self.assertIn("EQ çıkışları yeniden başlatıldı", o["lines"])

    def test_news_entries(self):
        self.assertEqual(len(tx.YENILIKLER), 5)
        targets = {(e[2], e[3]) for e in tx.YENILIKLER if e[2]}
        self.assertEqual(targets, {("eq", "ton"), ("ayarlar", "ses_analizi"), ("mikrofon", "kalibrasyon")})
        self.assertIn("Bu şarkı aslında", tx.YENILIKLER_TEPSI)


class TestSmartTiles(AppTestBase):
    def load_local(self, tone=F1_TONE, level=F1_LEVEL, model=None, onarim=None):
        model = model or {"aktif": False, "gerek": 20, "duzeltme": 3, "neden": ""}
        with mock.patch.object(appmod, "load_tone", lambda: tone), \
                mock.patch.object(appmod, "load_level_info", lambda: level), \
                mock.patch.object(appmod, "load_model_info", lambda: model), \
                mock.patch.object(appmod, "load_onarim", lambda: onarim):
            self.sp.refresh_local()

    def tile(self, key):
        return self.sp.smart.tiles[key]

    def test_tiles_shown_and_updated(self):
        self.load_local()
        self.win.on_status(smart_st())
        self.assertFalse(self.sp.smart.isHidden())
        self.assertEqual(self.sp.smart.visible_keys(), ["tur", "kisma", "ton", "seviye", "model"])
        self.assertEqual(self.tile("tur").value.text(), "Metal / Rock · iTunes")
        self.assertEqual(self.tile("tur").lines[0].full(), "Virgin Black — Lamenting Kiss")
        self.assertEqual(self.tile("kisma").value.text(), "Hazır")
        self.assertFalse(self.tile("kisma").property("hot"))
        self.assertEqual(self.tile("ton").value.text(), "Bas +1,5 · Vokal 0 · Parlaklık −1")
        self.assertEqual(self.tile("seviye").value.text(), "Sınırlayıcı açık")
        self.assertEqual(self.tile("model").value.text(), "Öğreniyor 3/20 düzeltme")
        self.assertTrue(self.tile("onarim").isHidden())
        # someone talks: highlighted, live
        talk = smart_st()
        talk["kisma"] = {**F1_KISMA, "aktif": True, "konusma": True, "kisma_db": 6.0, "sesli": ["Discord"]}
        self.win.on_status(talk)
        self.assertEqual(self.tile("kisma").value.text(), "Konuşma var · −6 dB")
        self.assertTrue(self.tile("kisma").property("hot"))

    def test_kulaklik_tile(self):
        self.load_local()
        self.win.on_status(smart_st(kulaklik=KULAKLIK_BT))
        self.assertEqual(self.sp.smart.visible_keys()[0], "kulaklik")
        t = self.tile("kulaklik")
        self.assertEqual(t.title.text(), "Kulaklık")
        self.assertEqual(t.value.text(), "Bluetooth kulaklık")
        self.assertEqual([ln.full() for ln in t.lines if not ln.isHidden()],
                         ["WH-1000XM4", "HS80 düzeltmesi nötr"])
        self.assertIn("yalnız Corsair HS80 için", t.toolTip())
        self.win.on_status(smart_st(active=False, kulaklik=KULAKLIK_SPK))
        self.assertEqual(self.tile("kulaklik").value.text(), "Hoparlör")
        self.assertIn("USB, Bluetooth ya da 3,5 mm", self.tile("kulaklik").toolTip())
        self.win.on_status(smart_st())              # older router: no device → no tile
        self.assertNotIn("kulaklik", self.sp.smart.visible_keys())

    def test_older_router_hides_tiles(self):
        self.win.on_status(st())          # no variants, no kisma; local loaders → None
        self.assertTrue(self.sp.smart.isHidden())
        self.load_local()
        self.win.on_status(st())
        self.assertEqual(self.sp.smart.visible_keys(), ["ton", "seviye", "model"])
        self.win.on_down()
        self.assertTrue(self.sp.smart.isHidden())

    def test_broken_local_data_never_breaks_page(self):
        self.load_local(tone={"genel": "bozuk"}, level={"x": 1}, model={"duzeltme": "?"},
                        onarim=["bozuk", {"zaman": "?", "neden": "x"}])
        self.win.on_status(smart_st())
        self.assertEqual(self.sp.hero_title.text(), "Müzik modu")
        # odd data hides only its own tile; the others still show
        self.assertIn("tur", self.sp.smart.visible_keys())
        self.assertIn("kisma", self.sp.smart.visible_keys())
        self.assertNotIn("model", self.sp.smart.visible_keys())

    def test_tone_tile_click_opens_eq(self):
        self.load_local(tone={"genel": {"bas": 0, "vokal": 0, "parlaklik": 0}, "tur": {}})
        self.win.on_status(smart_st())
        self.assertEqual(self.tile("ton").value.text(), "Ayarlanmadı")
        with mock.patch.object(self.win, "jump_to") as jt:
            self.sp.smart.tiles["ton"].clicked.disconnect()
            self.sp.smart.tiles["ton"].clicked.connect(lambda: self.win.jump_to("eq", "ton"))
            from PyQt6.QtCore import QPointF, QEvent
            from PyQt6.QtGui import QMouseEvent
            from PyQt6.QtCore import Qt
            t = self.tile("ton")
            ev = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(5, 5), QPointF(5, 5),
                             Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
            t.mouseReleaseEvent(ev)
            jt.assert_called_once_with("eq", "ton")

    def test_columns_wrap(self):
        r = self.sp.smart
        self.assertEqual(r.columns(6, 780), 3)
        self.assertEqual(r.columns(5, 780), 3)
        self.assertEqual(r.columns(4, 780), 2)
        self.assertEqual(r.columns(6, 450), 2)
        self.assertEqual(r.columns(3, 200), 1)

    def test_live_poll_updates_ducking(self):
        self.load_local()
        self.win.on_status(smart_st())
        fresh = {"ok": True, **smart_st()}
        fresh["kisma"] = {**F1_KISMA, "aktif": True, "konusma": True, "kisma_db": 4.5}
        self.sp._live_busy = True
        self.sp._live_done(fresh)
        self.assertFalse(self.sp._live_busy)
        self.assertEqual(self.tile("kisma").value.text(), "Konuşma var · −4,5 dB")
        self.sp._live_done(None)              # router busy: nothing breaks
        self.assertEqual(self.tile("kisma").value.text(), "Konuşma var · −4,5 dB")

    def test_live_tick_runs_request_off_thread(self):
        self.load_local()
        self.win.on_status(smart_st())
        calls = []
        fresh = {"ok": True, **smart_st()}
        fresh["kisma"] = {**F1_KISMA, "acik": False}

        def req(obj):
            calls.append(obj)
            return fresh
        self.win._request = req
        with mock.patch.object(self.sp, "isVisible", lambda: True):
            self.sp._live_tick()
        end = time.monotonic() + 2
        while self.sp._live_busy and time.monotonic() < end:
            spin(10)
        self.assertEqual(calls, [{"cmd": "status"}])
        self.assertEqual(self.tile("kisma").value.text(), "Kapalı")

    def test_news_card_once(self):
        self.assertFalse(self.sp.news.isHidden())
        self.assertEqual(set(self.sp.news.jump_buttons), {"ton", "ses_analizi", "kalibrasyon"})
        self.sp.news.btn_close.click()
        self.assertTrue(self.sp.news.isHidden())
        self.assertEqual(self.win.app_settings().get("yenilikler_kapandi"), tx.YENILIKLER_SURUM)
        w2 = appmod.MainWindow(runner=FakeRunner(), settings_path=self.dir / "pencere.json", restore=False)
        self.assertTrue(w2.page_status.news.isHidden())
        w2.deleteLater()

    def test_news_buttons_jump(self):
        nb = self.sp.news.jump_buttons
        nb["kalibrasyon"].click()
        self.assertIs(self.win.stack.currentWidget(), self.win.page_mic)
        nb["ses_analizi"].click()
        self.assertIs(self.win.stack.currentWidget(), self.win.page_settings)
        nb["ton"].click()
        self.assertIs(self.win.stack.currentWidget(), self.win.page_eq)
        self.assertFalse(self.win.jump_to("yok"))

    def test_scroll_into_view(self):
        self.win.resize(1080, 500)
        self.win.show()
        spin(50)
        self.assertTrue(self.win.jump_to("ayarlar", "ses_analizi"))
        spin(50)
        self.assertTrue(appmod.scroll_into_view(self.win.page_settings.sw_audio))
        self.assertFalse(appmod.scroll_into_view(QWidgetNoScroll()))


def QWidgetNoScroll():  # noqa: N802 - a widget outside any scroll area
    from PyQt6.QtWidgets import QWidget
    return QWidget()


class TestLanguage(AppTestBase):
    """CHANGE 8: translated texts reach the widgets; Ayarlar → Dil saves + restarts."""

    def test_forced_catalog_reaches_window(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"Ayarlar": "Einstellungen", "Kulaklık aktif değil": "Kopfhörer nicht aktiv",
                            "Kaydet": "Speichern",
                            "{zaman} önce": "vor {zaman}", "{n} dk": "{n} Min.",
                            "Özet: {tamam} tamam, {uyari} uyarı, {hata} hata":
                                "Übersicht: {tamam} ok, {uyari} Warnung, {hata} Fehler"})
        self.addCleanup(i18n.sifirla)
        w = appmod.MainWindow(runner=self.runner, settings_path=self.dir / "p2.json", restore=False)
        self.addCleanup(w.deleteLater)
        self.assertEqual(w.nav_buttons[4].text().strip(), "Einstellungen")
        w.set_status(st(active=False))
        self.assertEqual(w.page_status.hero_title.text(), "Kopfhörer nicht aktiv")
        dlg = appmod.AssignmentDialog(w)
        self.assertEqual(dlg.ok_button.text(), "Speichern")
        dlg.deleteLater()
        self.assertEqual(appmod.ago(1000.0 - 600, 1000.0), "vor 10 Min.")
        self.assertEqual(appmod.health_summary([{"durum": "hata"}]), "Übersicht: 0 ok, 0 Warnung, 1 Fehler")
        # the mic test table: the parse keys stay Turkish, the shown label is translated
        i18n.sifirla("de", {"İşlenmiş": "Verarbeitet"})
        td = appmod.MicTestDialog(w, 5, start=False)
        td.feed("Ham            -62.0     -28.0     -6.0\nİşlenmiş       -80.0     -22.0     -3.0\n")
        self.assertEqual(td.table.item(1, 0).text(), "Verarbeitet")
        self.assertEqual(td.table.item(0, 0).text(), "Ham")
        self.assertFalse(td.verdict.isHidden())          # needs both rows: parsing unchanged
        td.deleteLater()

    def test_turkish_by_default(self):
        self.assertEqual(self.win.nav_buttons[4].text().strip(), "Ayarlar")

    def test_selector_items(self):
        from oto_eq import i18n
        combo = self.win.page_settings.lang_combo
        self.assertEqual(combo.count(), 1 + len(i18n.DILLER))
        self.assertEqual(combo.itemData(0), i18n.OTOMATIK)
        self.assertTrue(combo.itemText(0).startswith("Otomatik (sistem dili: "))
        self.assertEqual([combo.itemData(i) for i in range(1, combo.count())], list(i18n.DILLER))
        # each language in its own name; an unfinished catalog shows its coverage
        beklenen = [ad if (o := i18n.kapsama(k)) is None or o >= 0.95 else f"{ad} ({o:.0%})"
                    for k, ad in i18n.DILLER.items()]
        self.assertEqual([combo.itemText(i) for i in range(1, combo.count())], beklenen)
        self.assertEqual(beklenen[0], "Türkçe")
        self.assertEqual(combo.currentData(), i18n.OTOMATIK)        # nothing saved yet

    def test_change_without_restart(self):
        from oto_eq import i18n
        page = self.win.page_settings
        page.confirm_restart = lambda: False
        self.win.restart_app = mock.Mock()
        self.win.language_changed_quietly = mock.Mock(return_value=True)
        combo = page.lang_combo
        combo.setCurrentIndex(combo.findData("de"))
        combo.activated.emit(combo.findData("de"))
        saved = json.loads((config.CONFIG_DIR / i18n.SETTINGS_FILE).read_text("utf-8"))
        self.assertEqual(saved["dil"], "de")
        self.assertEqual(i18n.ayar_oku(), "de")
        self.assertEqual(self.win.toast.text(), "Yeni dil bir sonraki açılışta geçerli olur.")
        self.win.restart_app.assert_not_called()
        # restart declined: GUI stays in the old language, so does the router (the next
        # GUI start syncs it: MainWindow.set_status → st["dil"])
        self.win.language_changed_quietly.assert_not_called()
        self.assertFalse(page.set_language("de"))                  # same choice: nothing to do
        page.sync_from_disk()
        self.assertEqual(combo.currentData(), "de")

    def test_change_with_restart(self):
        page = self.win.page_settings
        page.confirm_restart = lambda: True
        self.win.restart_app = mock.Mock()
        self.win.language_changed_quietly = mock.Mock(return_value=True)
        self.assertTrue(page.set_language("en"))
        self.win.restart_app.assert_called_once_with()
        self.win.language_changed_quietly.assert_called_once_with()   # router follows (IPC "dil")

    def test_router_language_synced_once_at_startup(self):
        from oto_eq import i18n
        self.win.language_changed_quietly = mock.Mock(return_value=True)
        self.win._dil_esitlendi = False
        other = "en" if i18n.dil() != "en" else "de"
        self.win.set_status({"active": False, "mode": "auto", "streams": [], "dil": other})
        self.win.set_status({"active": False, "mode": "auto", "streams": [], "dil": other})
        self.win.language_changed_quietly.assert_called_once_with()

    def test_restart_app_execs_itself(self):
        server = appmod.serve_single_instance(self.win, f"oto-eq-test-{uuid.uuid4().hex[:8]}")
        self.assertTrue(server.isListening())
        calls, tray = [], mock.Mock(return_value=False)
        self.win.restart_app(execv=lambda prog, args: calls.append((prog, args)), tray_restart=tray)
        tray.assert_called_once_with()
        self.assertEqual(calls, [(sys.executable, [sys.executable, "-m", "oto_eq.app"])])
        self.assertFalse(server.isListening())
        self.assertIn("geometry", self.win.app_settings())         # window state saved first

    def test_tray_restart_request(self):
        from PyQt6.QtNetwork import QLocalServer
        name = f"oto-eq-test-tray-{uuid.uuid4().hex[:8]}"
        self.assertFalse(appmod.request_tray_restart(name))         # no tray: silently False
        srv = QLocalServer()
        self.assertTrue(srv.listen(name))
        self.addCleanup(srv.close)
        got = []

        def on_conn():
            c = srv.nextPendingConnection()
            c.readyRead.connect(lambda: got.append(bytes(c.readAll())))
        srv.newConnection.connect(on_conn)
        self.assertTrue(appmod.request_tray_restart(name))
        self.assertTrue(wait_until(lambda: b"".join(got) == b"restart\n"))


if __name__ == "__main__":
    unittest.main()
