"""Tray / ui_common smoke tests (offscreen Qt, mocked notifier, no real tray)."""
from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Qt's GLib event dispatcher permanently acquires GLib's default main context
# for the main thread; dbus-python loops in other threads (oto_eq.mpris) then
# never run. Use Qt's own dispatcher so these tests don't break test_mpris.
os.environ.setdefault("QT_NO_GLIB", "1")

import json
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from oto_eq import i18n, tray, ui_common as ui
from tests.test_cli import KULAKLIK_BT, KULAKLIK_HS80, KULAKLIK_SPK

APP = QApplication.instance() or QApplication(["test"])

BASE = {"active": True, "default_sink": "hs80", "mode": "auto", "fixed_profile": None,
        "dominant": None, "sinks_ready": True, "streams": []}


def st(**kw):
    return {**BASE, **kw}


def spin(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        time.sleep(0.005)


class FakeNotifier:
    def __init__(self):
        self.calls = []

    def notify(self, summary, body="", timeout_ms=4000):
        self.calls.append((summary, body))
        return True


class TestBadge(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(ui.badge_spec(None).key, "down")
        self.assertEqual(ui.badge_spec(None).glyph, "bang")
        self.assertEqual(ui.badge_spec(st(mode="off", dominant="cs2")).key, "off")
        self.assertEqual(ui.badge_spec(st(mode="off")).glyph, "cross")
        for p, letter in (("cs2", "C"), ("oyun", "O"), ("muzik", "M"), ("film", "F")):
            s = ui.badge_spec(st(dominant=p))
            self.assertEqual((s.key, s.glyph, s.fixed), (p, letter, False))
            self.assertEqual(s.color, ui.PROFILE_COLORS[p])
        self.assertEqual(ui.badge_spec(st()).key, "none")
        self.assertEqual(ui.badge_spec(st(active=False, dominant="cs2")).key, "none")
        f = ui.badge_spec(st(mode="fixed", fixed_profile="film", dominant="film"))
        self.assertTrue(f.fixed)
        self.assertEqual(f.key, "film")

    def test_render_every_state_and_size(self):
        states = [None, st(mode="off"), st(), st(mode="fixed", fixed_profile="cs2", dominant="cs2")]
        states += [st(dominant=p) for p in ("cs2", "oyun", "muzik", "film")]
        for status in states:
            spec = ui.badge_spec(status)
            for size in ui.ICON_SIZES:
                with self.subTest(key=spec.key, size=size):
                    img = ui.badge_pixmap(size, spec).toImage()
                    self.assertEqual((img.width(), img.height()), (size, size))
                    self.assertEqual(img.pixelColor(0, 0).alpha(), 0)  # transparent corner
                    self.assertGreater(img.pixelColor(size // 2, size // 2).alpha(), 200)
            # body color near the top inside the circle (64 px)
            img = ui.badge_pixmap(64, spec).toImage()
            c = img.pixelColor(32, 9)
            want = QColor(spec.color)
            self.assertLess(abs(c.red() - want.red()) + abs(c.green() - want.green())
                            + abs(c.blue() - want.blue()), 30, spec.key)
            # the glyph is white somewhere in the middle area
            whites = sum(1 for x in range(16, 48) for y in range(16, 48)
                         if img.pixelColor(x, y).lightness() > 240)
            self.assertGreater(whites, 20, spec.key)

    def test_fixed_marker(self):
        on = ui.badge_pixmap(64, ui.badge_spec(st(mode="fixed", fixed_profile="cs2", dominant="cs2"))).toImage()
        off = ui.badge_pixmap(64, ui.badge_spec(st(dominant="cs2"))).toImage()
        self.assertGreater(on.pixelColor(54, 54).lightness(), 240)
        self.assertLess(off.pixelColor(54, 54).lightness(), 200)

    def test_icon_has_sizes(self):
        icon = ui.badge_icon(ui.badge_spec(st(dominant="muzik")))
        self.assertFalse(icon.isNull())
        self.assertGreaterEqual(len(icon.availableSizes()), len(ui.ICON_SIZES))

    def test_app_icon_png(self):
        with tempfile.TemporaryDirectory() as d:
            out = ui.render_app_icon(Path(d) / "x" / "kulak-kesil.png")
            img = QImage(str(out))
            self.assertEqual((img.width(), img.height()), (256, 256))
            self.assertEqual(img.pixelColor(0, 0).alpha(), 0)


class TestNotifier(unittest.TestCase):
    def test_replace_id_and_env(self):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "17\n", "")

        n = ui.Notifier(runner=runner)
        self.assertTrue(n.notify("🎯 CS2 modu", "Adım sesleri öne çıkarıldı"))
        self.assertTrue(n.notify("🎬 Film modu", "Diyalog net"))
        first, second = calls[0][0], calls[1][0]
        self.assertEqual(first[:3], ["notify-send", "-a", "Kulak Kesil"])
        self.assertIn("-p", first)
        self.assertIn("-e", first)
        self.assertNotIn("-r", first)
        self.assertEqual(second[second.index("-r") + 1], "17")
        self.assertEqual(second[-2:], ["🎬 Film modu", "Diyalog net"])
        self.assertEqual(calls[0][1]["env"]["LC_ALL"], "C.UTF-8")

    def test_failure_is_soft(self):
        def runner(cmd, **kw):
            raise FileNotFoundError("notify-send")
        self.assertFalse(ui.Notifier(runner=runner).notify("x"))


class ControllerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / "tepsi.json"
        self.notifier = FakeNotifier()
        self.requests = []
        self.reply = {"ok": True}
        self.launches = []

        def requester(obj):
            self.requests.append(obj)
            if isinstance(self.reply, Exception):
                raise self.reply
            return self.reply

        self.ctl = tray.TrayController(None, notifier=self.notifier, requester=requester,
                                       settings_path=self.settings, debounce_ms=60,
                                       app_launcher=lambda: self.launches.append(1) or "launched")

    def tearDown(self):
        self.tmp.cleanup()


class TestMenu(ControllerBase):
    def checked(self):
        return [k for k, a in self.ctl.mode_actions.items() if a.isChecked()]

    def test_initial_unreachable(self):
        self.assertEqual(self.ctl.badge.key, "down")
        self.assertEqual(self.checked(), [])
        self.assertFalse(self.ctl.mode_actions["auto"].isEnabled())
        self.assertIn("Servis çalışmıyor", tray.tooltip_text(None))

    def test_menu_items(self):
        texts = [a.text() for a in self.ctl.menu.actions() if not a.isSeparator()]
        self.assertEqual(texts[1], "Kulak Kesil'i aç")
        for t in ("Otomatik", "Sabit: CS2", "Sabit: Oyun", "Sabit: Müzik", "Sabit: Film",
                  "Kapalı", "Bildirimler", "Kuralları yeniden yükle", "EQ eğrilerini göster",
                  "Ayar klasörünü aç", "Çıkış"):
            self.assertIn(t, texts)

    def test_sync_modes(self):
        self.ctl.on_status(st())
        self.assertEqual(self.checked(), ["auto"])
        self.ctl.on_status(st(mode="fixed", fixed_profile="oyun"))
        self.assertEqual(self.checked(), ["fixed:oyun"])
        self.assertEqual(self.ctl.act_toggle.text(), "EQ'yu kapat")
        self.ctl.on_status(st(mode="off"))
        self.assertEqual(self.checked(), ["off"])
        self.assertEqual(self.ctl.act_toggle.text(), "EQ'yu aç (Otomatik)")
        self.assertEqual(self.ctl.badge.key, "off")
        self.ctl.on_down("x")
        self.assertEqual(self.checked(), [])
        self.assertEqual(self.ctl.badge.key, "down")

    def test_select_mode_requests(self):
        self.ctl.on_status(st())
        self.ctl.mode_actions["fixed:cs2"].trigger()
        self.ctl.mode_actions["off"].trigger()
        self.ctl.mode_actions["auto"].trigger()
        self.assertEqual(self.requests, [
            {"cmd": "set_mode", "mode": "fixed", "profile": "cs2"},
            {"cmd": "set_mode", "mode": "off"},
            {"cmd": "set_mode", "mode": "auto"},
        ])

    def test_request_failure_resyncs_and_notifies(self):
        self.ctl.on_status(st())
        self.reply = ConnectionRefusedError("yok")
        self.ctl.mode_actions["off"].trigger()
        self.assertEqual(self.checked(), ["auto"])  # reverted to real state
        self.assertIn(tray.DOWN_TEXT, self.notifier.calls[-1][1])

    def test_quick_toggle(self):
        self.ctl.on_status(st(mode="fixed", fixed_profile="film"))
        self.ctl.quick_toggle()
        self.ctl.on_status(st(mode="off"))
        self.ctl.quick_toggle()
        self.assertEqual([r["mode"] for r in self.requests], ["off", "auto"])

    def test_left_click_opens_app(self):
        from PyQt6.QtWidgets import QSystemTrayIcon
        self.ctl._activated(QSystemTrayIcon.ActivationReason.Trigger)
        self.assertEqual(self.launches, [1])
        self.ctl.on_status(st())
        self.ctl._activated(QSystemTrayIcon.ActivationReason.MiddleClick)
        self.assertEqual(self.requests, [{"cmd": "set_mode", "mode": "off"}])

    def test_tooltip(self):
        t = tray.tooltip_text(st(dominant="cs2", sinks_ready=False, streams=[
            {"index": 1, "app": "Counter-Strike 2", "profile": "cs2"},
            {"index": 2, "app": "", "binary": "discord", "profile": None}]))
        self.assertIn("Kulak Kesil", t)
        self.assertIn("Baskın profil: CS2", t)
        self.assertIn("EQ çıkışları hazır değil", t)
        self.assertIn("• Counter-Strike 2 → CS2", t)
        self.assertIn("• discord → EQ yok", t)
        t = tray.tooltip_text(st(mode="off", streams=[
            {"index": 3, "app": "Firefox", "profile": None, "sink": "oto_eq_duz"}]))
        self.assertIn("• Firefox → Düz", t)
        self.assertIn("seviye eşitli düz geçiş", t)


class TestI18n(unittest.TestCase):
    """CHANGE 8: a forced catalog reaches the tray menu, tooltip and notifications."""

    def setUp(self):
        i18n.sifirla("de", {
            "Kulak Kesil'i aç": "Kulak Kesil öffnen",
            "Sabit: {profil}": "Fest: {profil}",
            "Çıkış": "Beenden",
            "Bu şarkı aslında…": "Dieses Lied ist eigentlich…",
            "Mod: {mod} · Baskın profil: {profil}": "Modus: {mod} · Hauptprofil: {profil}",
            "Çalan akış yok": "Keine Wiedergabe",
            "Kurallar yeniden yüklendi": "Regeln neu geladen",
        })
        self.addCleanup(i18n.sifirla)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.notifier = FakeNotifier()
        self.ctl = tray.TrayController(None, notifier=self.notifier,
                                       requester=lambda obj: {"ok": True},
                                       settings_path=Path(self.tmp.name) / "tepsi.json",
                                       debounce_ms=60, app_launcher=lambda: "launched")

    def test_menu_tooltip_notification(self):
        texts = [a.text() for a in self.ctl.menu.actions() if not a.isSeparator()]
        self.assertEqual(texts[1], "Kulak Kesil öffnen")
        self.assertIn("Fest: CS2", texts)
        self.assertIn("Beenden", texts)
        self.assertIn("Kuralları yeniden yükle", texts)       # missing entry → Turkish
        self.assertEqual(self.ctl.act_genre.text(), "Dieses Lied ist eigentlich…")
        t = tray.tooltip_text(st(dominant="cs2"))
        self.assertIn("Modus: Otomatik · Hauptprofil: CS2", t)
        self.assertIn("Keine Wiedergabe", t)
        self.ctl.on_status(st())
        self.ctl.on_status(st(mode="fixed", fixed_profile="cs2"))
        self.assertEqual([k for k, a in self.ctl.mode_actions.items() if a.isChecked()],
                         ["fixed:cs2"])                       # state by key, not by text
        self.ctl.reload_rules()
        self.assertEqual(self.notifier.calls[-1], (ui.APP_NAME, "Regeln neu geladen"))


class TestKulaklik(ControllerBase):
    """CHANGE 7: output model + type in the tooltip and the menu."""

    def test_tooltip_device(self):
        t = tray.tooltip_text(st(kulaklik=KULAKLIK_HS80))
        self.assertIn("Çıkış: CORSAIR HS80 RGB Wireless Gaming Receiver · USB kulaklık · "
                      "HS80 düzeltmesi açık", t)
        self.assertNotIn("⚠", t)
        t = tray.tooltip_text(st(active=False, kulaklik=KULAKLIK_SPK))
        self.assertIn("Çıkış: Ryzen HD Audio Controller · Hoparlör — EQ devre dışı", t)
        self.assertIn("⚠ varsayılan çıkış kulaklık değil", t)
        t = tray.tooltip_text(st(kulaklik={**KULAKLIK_BT, "bt_profil": "headset-head-unit"}))
        self.assertIn("HS80 düzeltmesi nötr · Bluetooth arama modu (dar bant)", t)
        # older router: no device line, the old warning
        t = tray.tooltip_text(st(active=False))
        self.assertNotIn("Çıkış:", t)
        self.assertIn("⚠ kulaklık aktif değil", t)

    def test_menu_device_line(self):
        act = self.ctl.act_device
        self.assertFalse(act.isVisible())
        self.ctl.on_status(st(kulaklik=KULAKLIK_BT))
        self.assertTrue(act.isVisible())
        self.assertFalse(act.isEnabled())
        self.assertEqual(act.text(), "🎧 WH-1000XM4 (Bluetooth kulaklık)")
        self.ctl.on_status(st(active=False, kulaklik=KULAKLIK_SPK))
        self.assertEqual(act.text(), "🔈 Ryzen HD Audio Controller (Hoparlör) — EQ devre dışı")
        self.ctl.on_status(st(kulaklik={**KULAKLIK_BT, "bt_profil": "headset-head-unit"}))
        self.assertIn("arama modu", act.text())
        self.ctl.on_status(st())
        self.assertFalse(act.isVisible())
        self.ctl.on_down("x")
        self.assertFalse(act.isVisible())
        texts = [a.text() for a in self.ctl.menu.actions() if not a.isSeparator()]
        self.assertEqual(texts[1], "Kulak Kesil'i aç")


class TestNotifications(ControllerBase):
    def test_first_status_silent_then_debounced(self):
        self.ctl.on_status(st(dominant="cs2"))
        spin(150)
        self.assertEqual(self.notifier.calls, [])
        self.ctl.on_status(st(dominant="film"))
        self.assertEqual(self.notifier.calls, [])  # not before debounce
        spin(150)
        self.assertEqual(self.notifier.calls, [("🎬 Film modu", "Diyalog net")])

    def test_flapping_suppressed(self):
        self.ctl.on_status(st(dominant="cs2"))
        self.ctl.on_status(st(dominant="muzik"))
        spin(20)
        self.ctl.on_status(st(dominant="cs2"))
        spin(150)
        self.assertEqual(self.notifier.calls, [])

    def test_none_is_silent_and_resets(self):
        self.ctl.on_status(st(dominant="cs2"))
        self.ctl.on_status(st(dominant=None))
        spin(150)
        self.assertEqual(self.notifier.calls, [])
        self.ctl.on_status(st(dominant="cs2"))
        spin(150)
        self.assertEqual(self.notifier.calls, [("🎯 CS2 modu", "Adım sesleri öne çıkarıldı")])

    def test_toggle_off_and_persist(self):
        self.ctl.act_notify.setChecked(False)
        self.assertEqual(json.loads(self.settings.read_text()), {"bildirimler": False})
        self.ctl.on_status(st(dominant="cs2"))
        self.ctl.on_status(st(dominant="oyun"))
        spin(150)
        self.assertEqual(self.notifier.calls, [])
        again = tray.TrayController(None, notifier=FakeNotifier(), settings_path=self.settings)
        self.assertFalse(again.act_notify.isChecked())
        self.assertEqual(tray.load_settings(Path(self.tmp.name) / "yok.json"), {"bildirimler": True})


class TestVariants(ControllerBase):
    MUS = {"group": "metal_rock", "label": "Metal / Rock", "for": "Virgin Black", "mode": "auto"}
    GAME = {"group": "aksiyon_rpg", "label": "Aksiyon / RPG", "for": "Path of Exile 2", "mode": "auto"}

    def test_tooltip_variants(self):
        t = tray.tooltip_text(st(variants={"muzik": self.MUS, "oyun": self.GAME}))
        self.assertIn("Müzik türü: Metal / Rock — Virgin Black", t)
        self.assertIn("Oyun türü: Aksiyon / RPG — Path of Exile 2", t)

    def test_notify_only_on_new_game(self):
        self.ctl.on_status(st(dominant="oyun", variants={"oyun": self.GAME, "muzik": self.MUS}))
        spin(150)
        self.assertEqual(self.notifier.calls, [])                 # already running at start
        song2 = {**self.MUS, "for": "Sisters of Mercy", "group": "metal_rock"}
        self.ctl.on_status(st(dominant="oyun", variants={"oyun": self.GAME, "muzik": song2}))
        song3 = {**self.MUS, "for": "Daft Punk", "group": "elektronik", "label": "Elektronik / Hip-hop"}
        self.ctl.on_status(st(dominant="oyun", variants={"oyun": self.GAME, "muzik": song3}))
        spin(150)
        self.assertEqual(self.notifier.calls, [])                 # songs never notify
        game2 = {"group": "korku_hayatta_kalma", "label": "Korku / Hayatta kalma", "for": "Dead Space"}
        self.ctl.on_status(st(dominant="oyun", variants={"oyun": game2}))
        spin(150)
        self.assertEqual(self.notifier.calls, [("🎮 Dead Space", "Oyun türü: Korku / Hayatta kalma")])

    def test_game_flap_and_off(self):
        self.ctl.on_status(st(variants={}))
        self.ctl.on_status(st(variants={"oyun": self.GAME}))
        self.ctl.on_status(st(variants={}))                        # gone again within the debounce
        spin(150)
        self.assertEqual(self.notifier.calls, [])
        self.ctl.act_notify.setChecked(False)
        self.ctl.on_status(st(variants={"oyun": self.GAME}))
        spin(150)
        self.assertEqual(self.notifier.calls, [])



class TestGenreFeedback(ControllerBase):
    """'Bu şarkı aslında…': temp genre file + temp feedback file, fake IPC."""
    V = {"group": "pop_vokal", "label": "Pop / Vokal", "for": "Virgin Black",
         "key": "sanatci:virgin black", "source": "iTunes: Pop", "mode": "auto"}
    PLAYING = [{"index": 7, "app": "Firefox", "profile": "muzik"}]

    def setUp(self):
        super().setUp()
        from oto_eq import config, geri_bildirim
        d = Path(self.tmp.name)
        self.genre_file = d / "cfg" / "tur-atamalari.json"
        self.fb = d / "data" / "geri_bildirim.jsonl"
        for target, attr, val in ((config, "CONFIG_DIR", d / "cfg"),
                                  (config, "GENRE_FILE", self.genre_file),
                                  (geri_bildirim, "FEEDBACK_FILE", self.fb)):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)
        # the real geri_bildirim.kaydet is kept, so the tray's "real log only" guard
        # passes: without this the REAL ~/.local/share/oto-eq/kisisel_model.json is rewritten
        p = mock.patch.object(tray, "_train_later")
        self.train = p.start()
        self.addCleanup(p.stop)
        self.gb = geri_bildirim
        self.ctl.on_status(st(dominant="muzik", streams=self.PLAYING, variants={"muzik": self.V}))
        self.requests.clear()

    def texts(self):
        return [a.text() for a in self.ctl.genre_menu.actions() if not a.isSeparator()]

    def genre_data(self):
        return json.loads(self.genre_file.read_text("utf-8"))

    def test_visibility(self):
        self.assertTrue(self.ctl.act_genre.isVisible())
        self.assertEqual(self.ctl.act_genre.text(), tray.GENRE_MENU_TEXT)
        for status in (None, st(variants={"muzik": self.V}),                       # nothing plays
                       st(dominant="muzik", streams=self.PLAYING,
                          variants={"muzik": {**self.V, "key": ""}}),              # unknown artist
                       st(dominant="oyun", streams=[{"index": 1, "profile": "oyun"}],
                          variants={"muzik": self.V}),
                       st(active=False, dominant="muzik", streams=self.PLAYING,
                          variants={"muzik": self.V})):
            with self.subTest(status=status):
                if status is None:
                    self.ctl.on_down()
                else:
                    self.ctl.on_status(status)
                self.assertFalse(self.ctl.act_genre.isVisible())
        # music on another sink while a game dominates still counts
        self.ctl.on_status(st(dominant="oyun", streams=[{"index": 1, "profile": "oyun"}, *self.PLAYING],
                              variants={"muzik": self.V}))
        self.assertTrue(self.ctl.act_genre.isVisible())

    def test_items_header_checked(self):
        t = self.texts()
        self.assertEqual(t[0], "Virgin Black — Pop / Vokal (iTunes — Pop)")
        self.assertFalse(self.ctl.genre_menu.actions()[0].isEnabled())
        self.assertEqual(t[1:], ["Metal / Rock", "Elektronik / Hip-hop", "Klasik / Caz / Akustik",
                                 "Pop / Vokal", "Konuşma / Podcast", "Genel (EQ farkı yok)"])
        self.assertEqual([g for g, a in self.ctl.genre_actions.items() if a.isChecked()], ["pop_vokal"])
        self.assertNotIn(tray.GENRE_REMOVE_TEXT, t)            # no assignment yet
        self.assertEqual(self.requests, [])                     # building never talks to IPC
        self.ctl.on_status(st(dominant="muzik", streams=self.PLAYING,
                              variants={"muzik": {**self.V, "group": "", "label": "Genel",
                                                  "for": "Daft Punk", "key": "sanatci:daft punk",
                                                  "source": ""}}))
        self.assertEqual(self.texts()[0], "Daft Punk — Genel")
        self.assertEqual([g for g, a in self.ctl.genre_actions.items() if a.isChecked()], [""])

    def test_correct_genre_click(self):
        self.ctl.genre_actions["metal_rock"].trigger()
        self.assertEqual(self.genre_data()["sanatci"], {"virgin black": "metal_rock"})
        self.assertEqual(self.requests, [{"cmd": "reload"}])
        recs = self.gb.oku()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["profile"], r["key"], r["display"], r["predicted_group"], r["source"],
                          r["corrected_group"]),
                         ("muzik", "sanatci:virgin black", "Virgin Black", "pop_vokal",
                          "iTunes: Pop", "metal_rock"))
        self.assertEqual(self.notifier.calls, [("Kaydedildi: Virgin Black → Metal / Rock", "")])
        self.assertIn(tray.GENRE_REMOVE_TEXT, self.texts())    # now removable
        self.train.assert_called_once_with()                    # a correction retrains

    def test_general_and_remove(self):
        self.ctl.genre_actions[""].trigger()
        self.assertEqual(self.genre_data()["sanatci"], {"virgin black": ""})
        self.assertEqual(self.notifier.calls[-1], ("Kaydedildi: Virgin Black → Genel", ""))
        self.assertEqual(self.gb.oku()[-1]["corrected_group"], "")
        self.ctl.act_genre_remove.trigger()
        self.assertEqual(self.genre_data()["sanatci"], {})
        self.assertEqual(self.notifier.calls[-1], ("Atama kaldırıldı: Virgin Black", ""))
        self.assertEqual(len(self.gb.oku()), 1)                 # removing is not a correction
        self.assertEqual(self.requests, [{"cmd": "reload"}, {"cmd": "reload"}])
        self.assertNotIn(tray.GENRE_REMOVE_TEXT, self.texts())

    def test_existing_assignment_shown_and_replaced(self):
        self.genre_file.parent.mkdir(parents=True)
        self.genre_file.write_text(json.dumps({"cevrimici": True, "ses_analizi": False,
                                               "sanatci": {"Virgin Black": "konusma"},
                                               "oyun": {"steam:1": "aksiyon_rpg"}}), "utf-8")
        self.ctl.menu.aboutToShow.emit()                        # re-reads the file on open
        self.assertIn(tray.GENRE_REMOVE_TEXT, self.texts())
        self.assertTrue(self.ctl.correct_genre("klasik_akustik"))
        d = self.genre_data()
        self.assertEqual(d["sanatci"], {"virgin black": "klasik_akustik"})
        self.assertEqual((d["oyun"], d["ses_analizi"]), ({"steam:1": "aksiyon_rpg"}, False))

    def test_service_down_still_saves(self):
        self.reply = ConnectionRefusedError("yok")
        self.assertTrue(self.ctl.correct_genre("elektronik"))
        self.assertEqual(self.genre_data()["sanatci"], {"virgin black": "elektronik"})
        self.assertEqual(self.notifier.calls,
                         [("Kaydedildi: Virgin Black → Elektronik / Hip-hop",
                           "Servis çalışınca uygulanacak")])

    def test_write_failure_and_feedback_failure(self):
        def boom(_d):
            raise OSError("salt okunur")
        ctl = tray.TrayController(None, notifier=self.notifier, requester=lambda o: {"ok": True},
                                  settings_path=self.settings, genre_save=boom)
        ctl.on_status(st(dominant="muzik", streams=self.PLAYING, variants={"muzik": self.V}))
        self.assertFalse(ctl.correct_genre("metal_rock"))
        self.assertIn("Kaydedilemedi", self.notifier.calls[-1][1])
        self.assertEqual(self.gb.oku(), [])

        def fb_boom(*a):
            raise OSError("disk dolu")
        ctl2 = tray.TrayController(None, notifier=self.notifier, requester=lambda o: {"ok": True},
                                   settings_path=self.settings, feedback=fb_boom)
        ctl2.on_status(st(dominant="muzik", streams=self.PLAYING, variants={"muzik": self.V}))
        self.assertTrue(ctl2.correct_genre("metal_rock"))      # assignment still made
        self.assertEqual(self.genre_data()["sanatci"], {"virgin black": "metal_rock"})

    def test_no_variant_no_action(self):
        self.ctl.on_status(st())
        self.assertFalse(self.ctl.correct_genre("metal_rock"))
        self.assertFalse(self.genre_file.exists())
        self.assertEqual(self.requests, [])

class TestWatchThread(unittest.TestCase):
    def test_emits_and_reconnects(self):
        calls = {"n": 0}

        def watch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionRefusedError("yok")
            yield {"event": "status", **st(dominant="cs2")}
            yield {"event": "status", **st(dominant="film")}

        got, downs = [], []
        th = tray.WatchThread(watch_fn=watch, retry_s=0.05)
        th.status.connect(lambda d: got.append(d["dominant"]))
        th.down.connect(downs.append)
        th.start()
        end = time.monotonic() + 3
        while len(got) < 4 and time.monotonic() < end:
            spin(20)
        th.stop()
        self.assertTrue(th.wait(2000))
        self.assertGreaterEqual(calls["n"], 3)
        self.assertEqual(got[:4], ["cs2", "film", "cs2", "film"])
        self.assertGreaterEqual(len(downs), 2)


class TestSingleInstance(unittest.TestCase):
    def test_guard(self):
        name = f"oto-eq-test-{uuid.uuid4().hex[:8]}"
        first = tray.acquire_single_instance(name)
        try:
            self.assertIsNotNone(first)
            self.assertTrue(first.isListening())
            self.assertIsNone(tray.acquire_single_instance(name))
        finally:
            first.close()

    def _server(self, execv):
        name = f"oto-eq-test-{uuid.uuid4().hex[:8]}"
        server = tray.acquire_single_instance(name, execv=execv)
        self.addCleanup(server.close)
        return name, server

    def _send(self, name, data: bytes) -> None:
        from PyQt6.QtNetwork import QLocalSocket
        sock = QLocalSocket()
        sock.connectToServer(name)
        self.assertTrue(sock.waitForConnected(1000))
        if data:
            sock.write(data)
            sock.flush()
            sock.waitForBytesWritten(500)
        spin(150)
        sock.disconnectFromServer()
        spin(50)

    def test_restart_command_reexecs(self):
        calls = []
        name, server = self._server(lambda path, argv: calls.append((path, argv)))
        self._send(name, b"restart\n")
        self.assertEqual(calls, [(sys.executable,
                                  [sys.executable, "-m", "oto_eq.tray", *sys.argv[1:]])])
        self.assertFalse(server.isListening())          # socket freed for the new process

    def test_other_connections_ignored(self):
        calls = []
        name, server = self._server(lambda path, argv: calls.append((path, argv)))
        self._send(name, b"")                          # an extra launch: connect & go
        self._send(name, b"raise\n")
        self._send(name, b"restartx\n")
        self._send(name, b"x" * 400)                    # no newline, too long
        self.assertEqual(calls, [])
        self.assertTrue(server.isListening())
        self.assertIsNone(tray.acquire_single_instance(name))   # still the single instance

    def test_restart_failure_keeps_listening(self):
        def boom(path, argv):
            raise OSError("yok")
        name, server = self._server(boom)
        with self.assertLogs("oto_eq.tray", "WARNING"):
            self._send(name, b"restart\n")
        self.assertTrue(server.isListening())

    def test_raise_or_launch(self):
        launched = []
        name = f"oto-eq-test-{uuid.uuid4().hex[:8]}"
        r = ui.raise_or_launch_app(name, launcher=lambda: launched.append(1) or True)
        self.assertEqual((r, launched), ("launched", [1]))


if __name__ == "__main__":
    unittest.main()
