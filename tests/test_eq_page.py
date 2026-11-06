"""Offscreen tests for the Ekolayzır page (oto_eq.eq_page). No audio, no services."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import copy  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from PyQt6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PyQt6.QtCore import QEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from oto_eq import config, eqgen, kulaklik  # noqa: E402
from oto_eq import eq_page  # noqa: E402

APP = QApplication.instance() or QApplication([])
ORIG_LOAD_KISISEL = eq_page.load_kisisel
ORIG_IPC = eq_page.ipc_request

STATUS = {"ok": True, "active": True, "mode": "auto", "fixed_profile": None, "dominant": "cs2",
          "sinks_ready": True,
          "streams": [{"index": 1, "app": "Counter-Strike 2", "binary": "cs2", "profile": "cs2",
                       "sink": "oto_eq_cs2"},
                      {"index": 3, "app": "Discord", "binary": "discord", "profile": None,
                       "sink": "oto_eq_duz"}]}


def pump(cond=None, timeout=None) -> bool:
    """Process events until cond() or timeout (no cond: 50 ms or the given timeout)."""
    if timeout is None:
        timeout = 0.05 if cond is None else 2.0
    if cond is None:
        cond = lambda: False  # noqa: E731
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    APP.processEvents()
    return cond()


def mouse(kind, pos: QPointF, button=Qt.MouseButton.LeftButton, mods=Qt.KeyboardModifier.NoModifier):
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    return QMouseEvent(kind, pos, pos, button, buttons, mods)


class FakeKisisel:
    """Stand-in for C1's oto_eq.kisisel (contract API, in memory)."""

    TON_KEYS = ("bas", "vokal", "parlaklik")
    TON_LIMIT = 3.0
    TON_BANDS = {"bas": {"tip": "LSC", "fc": 120.0, "q": 0.7},
                 "vokal": {"tip": "PK", "fc": 2500.0, "q": 0.8},
                 "parlaklik": {"tip": "HSC", "fc": 8000.0, "q": 0.7}}

    def __init__(self, data=None):
        self.data = copy.deepcopy(data or {"genel": {"bas": 0, "vokal": 0, "parlaklik": 0},
                                           "tur": {}})
        self.saves = []

    def load(self):
        return copy.deepcopy(self.data)

    def save(self, data):
        self.data = copy.deepcopy(data)
        self.saves.append(copy.deepcopy(data))

    def effective(self, data, profile, group=""):
        t = (data.get("tur") or {}).get(f"{profile}:{group}", {}) if group else {}
        return {k: max(-3.0, min(3.0, float(data["genel"].get(k, 0)) + float(t.get(k, 0))))
                for k in self.TON_KEYS}

    def set_genel(self, **vals):
        self.data["genel"].update(vals)

    def set_tur(self, profile, group, **vals):
        self.data["tur"].setdefault(f"{profile}:{group}", {}).update(vals)


class PageTestBase(unittest.TestCase):
    kisisel = None          # FakeKisisel instance for the "Kişisel ton" tests
    device_fits = True      # kulaklik.duzeltme_acik() (no router status["kulaklik"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(config, "CONFIG_DIR", d),
            mock.patch.object(config, "SINKS_CONF", d / "oto-eq-sinks.conf"),
            mock.patch.object(eqgen, "lsp_available", return_value=True),
            mock.patch.object(eqgen, "set_live", return_value=True),
            mock.patch.object(eq_page, "restart_sinks_service", return_value=(True, "")),
            mock.patch.object(eq_page, "ipc_request", return_value={"ok": True}),
            # LIVE-SAFETY: never touch the real kisisel.json / sinks / terminal
            mock.patch.object(eq_page, "load_kisisel", lambda: self.kisisel),
            mock.patch.object(eqgen, "apply_tone", mock.Mock(return_value=True), create=True),
            mock.patch.object(eq_page, "launch_terminal", return_value=True),
            # LIVE-SAFETY: the active device (STATE_DIR/cihaz.json) is the router's, not ours
            mock.patch.object(kulaklik, "duzeltme_acik", lambda path=None: self.device_fits),
        ]
        for p in self.patches:
            p.start()
        self.set_live = eqgen.set_live
        self.restart = eq_page.restart_sinks_service
        self.ipc = eq_page.ipc_request
        self.apply_tone = eqgen.apply_tone
        self.launch = eq_page.launch_terminal
        self.page = eq_page.EqPage()
        self.page.resize(1200, 1100)
        self.page.show()
        pump()

    def tearDown(self):
        self.page.applier.stop()
        self.page.close()
        self.page.tone_applier.stop()
        self.page.deleteLater()
        pump()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def bands(self, prof=None):
        return self.page.work["profil"][prof or self.page.profile]["filtreler"]


class StructureTests(PageTestBase):
    def test_profiles_and_panels(self):
        p = self.page
        self.assertEqual(list(p.prof_buttons), ["cs2", "oyun", "muzik", "film", "duz"])
        self.assertTrue(p.prof_buttons["cs2"].isChecked())
        self.assertEqual(p.table.rowCount(), len(self.bands()))
        self.assertTrue(p.dyn_card.isVisible())
        p.select_profile("muzik")
        self.assertEqual(p.graph.profile, "muzik")
        self.assertFalse(p.dyn_card.isVisible())          # muzik has no [dinamik]
        p.select_profile("duz")
        self.assertFalse(p.bands_card.isVisible())
        self.assertTrue(p.flat_card.isVisible())
        self.assertFalse(p.graph.editable)
        self.assertIn("dB", p.flat_lbl.text())

    def test_status_apps(self):
        self.page.set_status(STATUS)
        self.assertIn("Counter-Strike 2", self.page.apps_lbl.text())
        self.page.select_profile("duz")
        self.assertIn("Discord", self.page.apps_lbl.text())
        self.page.select_profile("film")
        self.assertIn("yok", self.page.apps_lbl.text())
        self.page.set_status(None)
        self.assertIn("bilinmiyor", self.page.apps_lbl.text())


class GraphTests(PageTestBase):
    def test_drag_updates_band_and_table(self):
        g = self.page.graph
        i = 2
        start = g.handle_pos(i)
        fc0, g0 = self.bands()[i]["fc"], self.bands()[i]["kazanc"]
        g.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, start))
        self.assertEqual(g.selected, i)
        g.mouseMoveEvent(mouse(QEvent.Type.MouseMove, start + QPointF(60, -40)))
        g.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, start + QPointF(60, -40)))
        b = self.bands()[i]
        self.assertGreater(b["fc"], fc0)
        self.assertGreater(b["kazanc"], g0)
        self.assertEqual(self.page.table.item(i, eq_page.COL_GAIN).data(eq_page.VALUE_ROLE),
                         b["kazanc"])
        self.assertEqual(self.page.table.item(i, eq_page.COL_FREQ).text(), eq_page.fmt_freq(b["fc"]))
        self.assertTrue(self.page.dirty())
        self.assertTrue(self.page.save_btn.isEnabled())

    def test_shift_drag_keeps_frequency(self):
        g = self.page.graph
        start = g.handle_pos(0)
        fc0 = self.bands()[0]["fc"]
        g.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, start))
        g.mouseMoveEvent(mouse(QEvent.Type.MouseMove, start + QPointF(80, 30),
                               mods=Qt.KeyboardModifier.ShiftModifier))
        g.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, start))
        self.assertEqual(self.bands()[0]["fc"], fc0)

    def test_wheel_changes_q(self):
        g = self.page.graph
        pos = g.handle_pos(1)
        q0 = self.bands()[1]["q"]
        ev = QWheelEvent(pos, pos, QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        g.wheelEvent(ev)
        self.assertAlmostEqual(self.bands()[1]["q"], round(q0 * 1.12, 2))

    def test_double_click_adds_band_and_menu_actions(self):
        g = self.page.graph
        n = len(self.bands())
        r = g.plot_rect()
        pos = QPointF(r.left() + r.width() * 0.5, r.top() + r.height() * 0.3)
        g.mouseDoubleClickEvent(mouse(QEvent.Type.MouseButtonDblClick, pos))
        self.assertEqual(len(self.bands()), n + 1)
        self.assertEqual(self.page.table.rowCount(), n + 1)
        self.assertTrue(eqgen.needs_restart(self.page.saved, self.page.work))
        g.bandToggleRequested.emit(0)
        self.assertFalse(self.bands()[0]["aktif"])
        g.bandTypeRequested.emit(1, "HSC")
        self.assertEqual(self.bands()[1]["tip"], "HSC")
        g.bandRemoveRequested.emit(n)
        self.assertEqual(len(self.bands()), n)


class RobustnessTests(PageTestBase):
    def test_odd_router_fields(self):
        p = self.page
        p.select_profile("muzik")
        for st in ({**STATUS, "streams": None, "variants": None},
                   {**STATUS, "streams": [None, "x", {"profile": "muzik", "app": "Spotify"}],
                    "variants": {"muzik": "bozuk", "oyun": None}}):
            p.set_status(st)                       # no TypeError / ValueError
            p.reload_tone()
        self.assertEqual(p._router_variant("muzik"), {})
        self.assertEqual(p.apps_lbl.text(), "Şu an kullanan: Spotify")
        p.set_status({**STATUS, "streams": None})
        self.assertEqual(p.apps_lbl.text(), "Şu an bu çıkışı kullanan uygulama yok")

    def test_graph_menu_freed_after_use(self):
        from PyQt6.QtWidgets import QMenu
        g = self.page.graph
        before = len(g.findChildren(QMenu))
        g._menu(0, QPoint(10, 10))
        menus = [m for m in g.findChildren(QMenu) if m.parent() is g]
        self.assertGreater(len(g.findChildren(QMenu)), before)
        for m in menus:
            m.hide()
        pump(timeout=0.1)
        self.assertEqual(len([m for m in g.findChildren(QMenu) if m.parent() is g]), 0)

    def test_drag_does_not_recompute_staging_per_move(self):
        p = self.page
        real = eqgen.gain_staging
        with mock.patch.object(eqgen, "gain_staging", side_effect=real) as gs:
            for k in range(10):                    # 10 mouse moves of a drag
                self.bands()[0]["kazanc"] = -3.0 - k * 0.1
                p._graph_band_changed(0)
            self.assertEqual(gs.call_count, 0)     # gain label debounced
            self.assertTrue(p.save_btn.isEnabled())              # cheap state is immediate
            self.assertTrue(pump(lambda: gs.call_count >= 1, timeout=1.0))
            pump(timeout=0.2)
            self.assertLessEqual(gs.call_count, 1)
            n = gs.call_count
            p._update_info()                       # same data: memoised
            self.assertEqual(gs.call_count, n)
        self.assertIn("Ön kazanç", p.gain_lbl.text())

    def test_ipc_timeout_outlasts_router_wait(self):
        from oto_eq import router, ui_common as ui
        with mock.patch("oto_eq.ipc.request", return_value={"ok": True}) as req:
            ORIG_IPC({"cmd": "status"})          # the real helper, not the setUp mock
        self.assertEqual(req.call_args.kwargs["timeout"], ui.IPC_TIMEOUT)
        self.assertGreaterEqual(ui.IPC_TIMEOUT, router.COMMAND_WAIT + 1.0)


class LayoutTests(PageTestBase):
    """Long translations wrap instead of forcing a horizontal scroll bar (de/ru: 1050–1150 px
    before, at a 980 px window)."""

    LONG = {"Varsayılana dön": "Zurück zu den Standardeinstellungen der Anwendung",
            "A/B karşılaştır": "Mit der flachen Ausgabe vergleichen (A/B)",
            "Kör test": "Blindtest mit acht Durchgängen",
            "Canlı dinle": "Live anhören, ohne zu speichern",
            "Seviyeyi koru": "Lautstärkeangleichung beibehalten",
            "Kör A/B dinle…": "Blinden A/B-Hörtest im Terminal starten…",
            "Tüm profiller (genel)": "Alle Profile (allgemein, gilt überall ausser CS2)"}

    def test_no_horizontal_scroll_when_narrow(self):
        from oto_eq import i18n
        from PyQt6.QtWidgets import QScrollArea
        self.addCleanup(i18n.sifirla)
        i18n.sifirla("de", self.LONG)
        p = eq_page.EqPage()
        self.addCleanup(lambda: (p.applier.stop(), p.tone_applier.stop(), p.close(), p.deleteLater()))
        p.resize(740, 900)                         # a 980 px window minus the sidebar
        p.show()
        pump()
        sa = p.findChildren(QScrollArea)[0]
        self.assertLessEqual(sa.widget().minimumSizeHint().width(), sa.viewport().width())
        self.assertGreater(p.save_btn.y(), p.live_on.y())          # the button row wrapped
        self.assertTrue(p.save_btn.isVisible())


class TableAndDynTests(PageTestBase):
    def test_delegate_edit(self):
        t = self.page.table
        delegate = t.itemDelegate()
        idx = t.model().index(0, eq_page.COL_GAIN)
        ed = delegate.createEditor(t.viewport(), None, idx)
        delegate.setEditorData(ed, idx)
        ed.setValue(-7.5)
        delegate.setModelData(ed, t.model(), idx)
        self.assertEqual(self.bands()[0]["kazanc"], -7.5)
        self.assertEqual(t.item(0, eq_page.COL_GAIN).text(), "-7.5 dB")
        idx = t.model().index(1, eq_page.COL_TYPE)
        cb = delegate.createEditor(t.viewport(), None, idx)
        cb.setCurrentIndex(cb.findData("LSC"))
        delegate.setModelData(cb, t.model(), idx)
        self.assertEqual(self.bands()[1]["tip"], "LSC")

    def test_dyn_slider(self):
        s = self.page.dyn_sliders["telafi_db"][0]
        s.setValue(s.value() + 4)        # +2 dB (step 0.5)
        d = self.page.work["profil"]["cs2"]["dinamik"]
        self.assertAlmostEqual(d["telafi_db"], self.page.saved["profil"]["cs2"]["dinamik"]["telafi_db"] + 2)
        self.page.dyn_on.setChecked(False)
        self.assertFalse(d["aktif"])
        self.assertFalse(s.isEnabled())


class LiveTests(PageTestBase):
    def test_live_debounced(self):
        self.page.live_on.setChecked(True)
        pump(lambda: self.set_live.called)
        self.set_live.reset_mock()
        for v in (1.0, 2.0, 3.0):   # burst -> coalesced
            self.page._table_edit(0, eq_page.COL_GAIN, v)
        self.assertTrue(pump(lambda: self.set_live.called and self.page.applier.idle()))
        self.assertLessEqual(self.set_live.call_count, 2)
        args, kwargs = self.set_live.call_args
        self.assertEqual(args[0], "cs2")
        self.assertEqual(args[1]["profil"]["cs2"]["filtreler"][0]["kazanc"], 3.0)
        self.assertIsNone(kwargs["staging"])          # Seviyeyi koru on -> recompute

    def test_frozen_level(self):
        self.page.live_on.setChecked(True)
        self.page.keep_level.setChecked(False)
        pump(lambda: self.set_live.called)
        self.set_live.reset_mock()
        self.page._table_edit(2, eq_page.COL_GAIN, 9.0)    # big boost
        self.assertTrue(pump(lambda: self.set_live.called and self.page.applier.idle()))
        st = self.set_live.call_args.kwargs["staging"]
        base = self.page.base_staging
        self.assertEqual(st["muzik"], base["muzik"])               # other levels frozen
        self.assertEqual(st["cs2"]["match_db"], base["cs2"]["match_db"])
        self.assertLess(st["cs2"]["preamp_db"], base["cs2"]["preamp_db"])   # anti-clip

    def test_structural_change_not_live(self):
        self.page.live_on.setChecked(True)
        pump(lambda: self.set_live.called)
        self.set_live.reset_mock()
        self.page.add_band(800.0, 2.0)
        pump(timeout=0.4)
        self.assertFalse(self.set_live.called)
        self.assertIn("Kaydet", self.page.state_lbl.text())


class VariantEditTests(PageTestBase):
    """Genre variant ("Tür farkı") editing on the muzik / oyun pages (CHANGE 3)."""

    def pick(self, group):
        box = self.page.variant_box
        box.setCurrentIndex(box.findData(group))
        pump()

    def tur(self, prof="muzik"):
        return self.page.work["profil"][prof].get("tur") or {}

    def test_selector_only_on_variant_profiles(self):
        p = self.page
        self.assertFalse(p.variant_row.isVisible())          # cs2
        p.select_profile("muzik")
        self.assertTrue(p.variant_row.isVisible())
        self.assertEqual(p.variant_box.itemData(0), "")
        self.assertEqual([p.variant_box.itemData(i) for i in range(1, p.variant_box.count())],
                         list(eq_page.variant_groups("muzik")))
        p.select_profile("film")
        self.assertFalse(p.variant_row.isVisible())

    def test_edit_variant_slots(self):
        p = self.page
        p.select_profile("muzik")
        base = copy.deepcopy(self.bands())
        self.pick("metal_rock")
        self.assertTrue(p.in_variant())
        self.assertEqual(p.table.rowCount(), eqgen.VARIANT_SLOTS)
        self.assertFalse(p.add_btn.isVisible())
        types = [p.table.item(i, eq_page.COL_TYPE).data(eq_page.VALUE_ROLE) for i in range(4)]
        self.assertEqual(types, ["LSC", "PK", "PK", "HSC"])
        self.assertFalse(p.table.item(0, eq_page.COL_TYPE).flags() & Qt.ItemFlag.ItemIsEditable)
        p._table_edit(0, eq_page.COL_GAIN, 2.0)               # unused LSC slot -> used
        flt = self.tur()["metal_rock"]["filtreler"]
        self.assertEqual(flt[0]["tip"], "LSC")
        self.assertEqual(flt[0]["kazanc"], 2.0)
        p._table_edit(3, eq_page.COL_GAIN, 9.0)               # clamped to the variant limit
        self.assertEqual(self.tur()["metal_rock"]["filtreler"][-1]["kazanc"],
                         eqgen.VARIANT_GAIN_LIMIT)
        self.assertEqual(self.bands(), base)                  # profile's own bands untouched
        self.assertTrue(p.dirty())
        p.add_band(500.0, 1.0)                                # no new bands in a variant
        self.assertEqual(p.table.rowCount(), eqgen.VARIANT_SLOTS)
        eqgen.normalize_profiles(p.work)                       # still valid
        self.pick("")                                          # back to the profile
        self.assertFalse(p.in_variant())
        self.assertEqual(p.table.rowCount(), len(base))

    def test_live_preview_and_restore(self):
        p = self.page
        p.set_status({**STATUS, "variants": {"muzik": {"group": "pop_vokal", "label": "Pop / Vokal",
                                                       "for": "Tarkan", "mode": "auto"}}})
        p.select_profile("muzik")
        self.assertIn("Tarkan", p.variant_lbl.text())
        p.live_on.setChecked(True)
        pump(lambda: self.set_live.called and p.applier.idle())
        self.assertIsNone(self.set_live.call_args.kwargs["variants"])   # Genel: slots untouched
        self.set_live.reset_mock()
        self.pick("elektronik")
        self.assertTrue(pump(lambda: self.set_live.called and p.applier.idle()))
        self.assertEqual(self.set_live.call_args.kwargs["variants"], {"muzik": "elektronik"})
        self.set_live.reset_mock()
        self.pick("")                                          # preview over: router's group back
        self.assertTrue(pump(lambda: self.set_live.called and p.applier.idle()))
        self.assertEqual(self.set_live.call_args.kwargs["variants"], {"muzik": "pop_vokal"})

    def test_save_variant_restores_router_group(self):
        p = self.page
        p.set_status({**STATUS, "variants": {"muzik": {"group": "metal_rock", "mode": "auto"}}})
        p.select_profile("muzik")
        self.pick("metal_rock")
        p._table_edit(1, eq_page.COL_GAIN, 2.5)
        self.assertTrue(p.save())
        saved = eqgen.load_profiles(config.CONFIG_DIR / config.PROFILES_FILE)
        gains = [f["kazanc"] for f in saved["profil"]["muzik"]["tur"]["metal_rock"]["filtreler"]]
        self.assertIn(2.5, gains)
        self.restart.assert_not_called()                       # variant edits are live
        self.assertTrue(pump(lambda: self.set_live.called and p.applier.idle()))
        # save() refreshes every variant sink (the shared level match moves): the muzik
        # call carries the router's group
        self.assertIn({"muzik": "metal_rock"},
                      [c.kwargs.get("variants") for c in self.set_live.call_args_list])
        self.assertFalse(p.dirty())


class SaveTests(PageTestBase):
    def test_save_without_restart(self):
        self.page._table_edit(0, eq_page.COL_GAIN, -4.0)
        self.assertTrue(self.page.save())
        path = config.CONFIG_DIR / config.PROFILES_FILE
        self.assertEqual(eqgen.load_profiles(path)["profil"]["cs2"]["filtreler"][0]["kazanc"], -4.0)
        self.assertTrue(config.SINKS_CONF.exists())
        self.restart.assert_not_called()
        self.assertFalse(self.page.dirty())
        self.assertTrue(pump(lambda: self.set_live.called))

    def test_save_with_restart(self):
        self.page.add_band(900.0, 1.0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.assertFalse(self.page.save())
        self.restart.assert_not_called()
        self.assertFalse((config.CONFIG_DIR / config.PROFILES_FILE).exists())
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.assertTrue(self.page.save())
        # the restart runs on a thread (systemctl waits for the new process): the page answers
        self.assertTrue(pump(lambda: self.restart.called and not self.page._restarting))
        self.restart.assert_called_once()
        self.assertEqual(self.page.state_lbl.text(), "Kayıtlı")
        saved = eqgen.load_profiles(config.CONFIG_DIR / config.PROFILES_FILE)
        self.assertEqual(len(saved["profil"]["cs2"]["filtreler"]), len(self.bands()))

    def test_save_restart_async_and_failure(self):
        import threading
        gate = threading.Event()

        def slow_restart():
            gate.wait(5)
            return False, "Job for oto-eq-sinks.service failed"
        self.page.add_band(900.0, 1.0)
        with mock.patch.object(eq_page, "restart_sinks_service", slow_restart), \
                mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            t0 = time.monotonic()
            self.assertTrue(self.page.save())
            self.assertLess(time.monotonic() - t0, 2.0)            # save() did not wait for it
            self.assertIn("yeniden başlatılıyor", self.page.state_lbl.text())
            gate.set()
            self.assertTrue(pump(lambda: not self.page._restarting))
        self.assertIn("Servis yeniden başlatılamadı", self.page.state_lbl.text())
        self.assertIn("failed", self.page.state_lbl.text())

    def test_revert_and_defaults(self):
        orig = copy.deepcopy(self.page.saved)
        self.page._table_edit(0, eq_page.COL_GAIN, 5.0)
        self.page.revert()
        self.assertEqual(self.page.work, orig)
        self.page._table_edit(0, eq_page.COL_GAIN, 5.0)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.page.reset_defaults()
        self.assertNotEqual(self.page.work, orig)
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.page.reset_defaults()
        shipped = eqgen.load_profiles(config.DEFAULTS_DIR / config.PROFILES_FILE)
        self.assertEqual(self.page.work, shipped)


class ABTests(PageTestBase):
    def calls(self):
        return [c.args[0] for c in self.ipc.call_args_list]

    def test_ab_cycle_restores_previous_mode(self):
        self.page.set_status(STATUS)
        self.page.select_profile("film")
        self.page.ab_btn.setChecked(True)
        self.assertTrue(self.page.ab_swap.isVisible())
        self.page.ab_swap.click()
        self.page.ab_swap.click()
        self.page.ab_btn.setChecked(False)
        self.assertEqual(self.calls(), [
            {"cmd": "set_mode", "mode": "fixed", "profile": "film"},
            {"cmd": "set_mode", "mode": "off"},
            {"cmd": "set_mode", "mode": "fixed", "profile": "film"},
            {"cmd": "set_mode", "mode": "auto"},
        ])

    def test_ab_restores_fixed_and_ends_on_profile_switch(self):
        self.page.set_status(dict(STATUS, mode="fixed", fixed_profile="muzik"))
        self.page.ab_btn.setChecked(True)
        self.page.select_profile("oyun")           # switching tabs ends A/B
        self.assertFalse(self.page.ab_btn.isChecked())
        self.assertEqual(self.calls()[-1], {"cmd": "set_mode", "mode": "fixed", "profile": "muzik"})

    def test_ab_daemon_down(self):
        self.ipc.side_effect = OSError("yok")
        self.page.set_status(None)
        self.page.ab_btn.setChecked(True)
        self.assertFalse(self.page.ab_btn.isChecked())
        self.assertIn("ulaşılamadı", self.page.state_lbl.text())


class AbxTests(PageTestBase):
    def test_binomial(self):
        self.assertAlmostEqual(eq_page.binom_p(7, 8), 9 / 256)
        self.assertAlmostEqual(eq_page.binom_p(8, 8), 1 / 256)
        self.assertAlmostEqual(eq_page.binom_p(0, 8), 1.0)
        self.assertIn("7/8 doğru = gerçekten fark duyuyorsun", eq_page.abx_verdict(7, 8))
        self.assertIn("kesinlikle", eq_page.abx_verdict(8, 8))
        self.assertIn("tahminden ayırt edilemiyor", eq_page.abx_verdict(4, 8))

    def test_session(self):
        import random
        s = eq_page.AbxSession("eq", "duz", 8, random.Random(3))
        self.assertEqual(set(s.mapping.values()), {"eq", "duz"})
        for _ in range(8):
            self.assertIn(s.source_of("X"), ("eq", "duz"))
            self.assertTrue(s.answer(s.xs[s.index]))
        self.assertTrue(s.done)
        self.assertEqual(s.score, 8)
        with self.assertRaises(RuntimeError):
            s.answer("A")

    def calls(self):
        return [c.args[0] for c in self.ipc.call_args_list]

    def test_abx_profile_vs_flat(self):
        import random
        self.page.set_status(dict(STATUS, mode="fixed", fixed_profile="muzik"))
        dlg = self.page.start_abx("duz", rng=random.Random(1))
        self.assertIsNotNone(dlg)
        s = dlg.session
        modes = {"eq": {"cmd": "set_mode", "mode": "fixed", "profile": "cs2"},
                 "duz": {"cmd": "set_mode", "mode": "off"}}
        self.assertEqual(self.calls()[-1], modes[s.source_of("X")])   # X plays first
        dlg.listen("A")
        self.assertEqual(self.calls()[-1], modes[s.mapping["A"]])
        dlg.listen("B")
        self.assertEqual(self.calls()[-1], modes[s.mapping["B"]])
        for i in range(8):           # 7 right, 1 wrong
            x = s.xs[s.index]
            dlg.choose(x if i else ("B" if x == "A" else "A"))
        self.assertTrue(s.done)
        self.assertEqual(s.score, 7)
        self.assertIn("gerçekten fark duyuyorsun", dlg.result.text())
        self.assertIn("Düz (EQ yok)", dlg.result.text())
        dlg.close()
        self.assertIsNone(self.page.abx)
        self.assertEqual(self.calls()[-1], {"cmd": "set_mode", "mode": "fixed", "profile": "muzik"})

    def test_abx_saved_vs_edited(self):
        import random
        self.page.set_status(STATUS)
        self.page._table_edit(2, eq_page.COL_GAIN, 4.0)
        dlg = self.page.start_abx(rng=random.Random(2))          # dirty -> kayit mode
        self.assertEqual(self.page._abx_kind, "kayit")
        self.assertEqual(self.calls()[0], {"cmd": "set_mode", "mode": "fixed", "profile": "cs2"})
        for letter in ("A", "B"):
            self.set_live.reset_mock()
            dlg.listen(letter)
            self.assertTrue(pump(lambda: self.set_live.called and self.page.applier.idle()))
            gain = self.set_live.call_args.args[1]["profil"]["cs2"]["filtreler"][2]["kazanc"]
            want = 4.0 if dlg.session.mapping[letter] == "yeni" else 2.0
            self.assertEqual(gain, want)
        dlg.close()
        self.assertEqual(self.calls()[-1], {"cmd": "set_mode", "mode": "auto"})

    def test_abx_render(self):
        import random
        out = os.environ.get("OTO_EQ_RENDER_DIR")
        self.page.set_status(STATUS)
        dlg = self.page.start_abx("duz", rng=random.Random(5))
        dlg.resize(620, 420)
        dlg.choose("A")
        dlg.choose("B")
        pump()
        img = dlg.grab()
        self.assertFalse(img.isNull())
        if out:
            img.save(str(Path(out) / "abx_tur.png"))
        while not dlg.session.done:
            dlg.choose(dlg.session.xs[dlg.session.index])
        pump()
        if out:
            dlg.grab().save(str(Path(out) / "abx_sonuc.png"))
        dlg.close()


class GuideTests(PageTestBase):
    def test_guide_cs2_default_and_copy(self):
        self.page.select_profile("duz")
        dlg = self.page.open_guide()           # Düz -> CS2 guide
        self.assertEqual(dlg.profile, "cs2")
        self.assertTrue(dlg.cmd_card.isVisible())
        self.assertGreaterEqual(len(dlg.step_cards), 8)
        dlg.copy_commands()
        clip = APP.clipboard().text()
        self.assertEqual(clip, eq_page.CS2_COMMANDS)
        for cmd in ("sv_cheats 1", "bot_kick", "bot_add_t", "mp_ignore_round_win_conditions 1",
                    "sv_infinite_ammo 1", "mp_warmup_end"):
            self.assertIn(cmd, clip)
        self.assertIn("kopyalandı", dlg.note.text())
        dlg.close()

    def test_guide_profiles(self):
        self.page.select_profile("film")
        dlg = self.page.open_guide()
        self.assertEqual(dlg.profile, "film")
        self.assertFalse(dlg.cmd_card.isVisible())
        for prof in ("cs2", "oyun", "muzik", "film"):
            dlg.show_profile(prof)
            pump()
            self.assertEqual(len(dlg.step_cards), len(eq_page.TEST_GUIDES[prof]["adimlar"]))
            for _t, do, watch in eq_page.TEST_GUIDES[prof]["adimlar"]:
                self.assertTrue(do and watch)
        out = os.environ.get("OTO_EQ_RENDER_DIR")
        if out:
            dlg.show_profile("cs2")
            dlg.resize(820, 900)
            pump()
            dlg.grab().save(str(Path(out) / "rehber_test.png"))
        dlg.close()


TONE_STATUS = {**STATUS, "dominant": "muzik",
               "streams": STATUS["streams"] + [{"index": 7, "app": "Spotify", "binary": "spotify",
                                                "profile": "muzik", "sink": "oto_eq_muzik"}],
               "variants": {"muzik": {"group": "elektronik", "label": "Elektronik",
                                      "for": "SAFI", "mode": "auto"},
                            "oyun": {"group": "", "mode": "auto"}}}
ZERO = {"bas": 0.0, "vokal": 0.0, "parlaklik": 0.0}


def which_of(*names):
    paths = {"ghostty": "/usr/bin/ghostty", "konsole": "/usr/bin/konsole",
             "xterm": "/usr/bin/xterm", "kulak": "/home/u/.local/bin/kulak"}
    return lambda n: paths.get(n) if n in names else None


class ToneUnavailableTests(PageTestBase):
    def test_group_hidden_with_note(self):
        p = self.page
        self.assertFalse(p.tone_available())
        self.assertFalse(p.tone_head.isVisible())
        self.assertFalse(p.tone_body.isVisible())
        self.assertIn("henüz yok", p.tone_state.text())
        self.assertIsNone(p.graph.tone_bands)
        p.discard_tone_preview(sync=True)                 # harmless no-ops
        p.reload_tone()
        self.assertFalse(p.start_tone_ab())
        self.launch.assert_not_called()

    def test_load_kisisel_guards(self):
        import oto_eq
        with mock.patch.object(oto_eq, "kisisel", types.SimpleNamespace(TON_KEYS=()),
                               create=True):
            self.assertIsNone(ORIG_LOAD_KISISEL())             # incomplete module: hidden
        mod = ORIG_LOAD_KISISEL()                              # C1's module (if installed)
        if mod is not None:
            self.assertTrue(callable(mod.effective))


class DeviceCorrectionTests(PageTestBase):
    """CHANGE 7: the HS80 [duzeltme] curve only on the HS80; display only, never saved."""

    def st(self, k):
        return {**STATUS, "kulaklik": k}

    HS80 = {"ad": "alsa_output.usb-Corsair_HS80", "aciklama": "CORSAIR HS80 RGB Wireless Gaming "
            "Receiver", "tur": "usb", "etiket": "USB kulaklık", "kulaklik": True,
            "duzeltme": True, "neden": "", "baglanti": "usb", "bt_profil": ""}
    BT = {"ad": "bluez_output.X.1", "aciklama": "WH-1000XM4", "tur": "bluetooth",
          "etiket": "Bluetooth kulaklık", "kulaklik": True, "duzeltme": False, "neden": "",
          "baglanti": "bluetooth", "bt_profil": "a2dp-sink"}
    SPK = {"ad": "alsa_output.pci-x", "aciklama": "Ryzen HD Audio Controller", "tur": "hoparlor",
           "etiket": "Hoparlör", "kulaklik": False, "duzeltme": False, "neden": "",
           "baglanti": "pci", "bt_profil": ""}

    def corr(self):
        return self.page.graph.curves()[0]

    def test_other_headphone_flattens_curve_only_on_screen(self):
        p = self.page
        self.assertTrue(p.work["duzeltme"]["aktif"])
        p.set_status(self.st(self.HS80))
        pump()
        self.assertTrue(p.graph.device_correction)
        self.assertGreater(abs(self.corr()).max(), 0.1)
        self.assertTrue(p.corr_note.isHidden())
        total_hs80 = p.graph.curves()[2].copy()
        p.set_status(self.st(self.BT))
        pump()
        self.assertFalse(p.graph.device_correction)
        self.assertEqual(abs(self.corr()).max(), 0.0)
        self.assertFalse(p.corr_note.isHidden())
        self.assertEqual(p.corr_note.text(), "Bu kulaklıkta (WH-1000XM4) HS80 düzeltmesi "
                                             "uygulanmıyor; eğri HS80 için ölçüldü.")
        self.assertGreater(abs(total_hs80 - p.graph.curves()[2]).max(), 0.1)
        # the user's switch and profiller.toml stay as they were
        self.assertTrue(p.corr_on.isChecked())
        self.assertTrue(p.work["duzeltme"]["aktif"])
        self.assertFalse(p.dirty())
        self.set_live.assert_not_called()
        # base_total (variant mode "Genel" curve) agrees
        p.select_profile("muzik")
        self.assertEqual(p.graph.device_correction, False)
        self.assertLess(abs(p.graph.base_total() - p.graph.curves()[1]
                            - p.graph.tone_curve()).max(), 1e-9)

    def test_switch_off_still_wins(self):
        p = self.page
        p.set_status(self.st(self.HS80))
        p.corr_on.setChecked(False)
        pump()
        self.assertEqual(abs(self.corr()).max(), 0.0)
        self.assertFalse(p.work["duzeltme"]["aktif"])

    def test_speaker_and_flat_profile_show_no_note(self):
        p = self.page
        p.set_status(self.st(self.SPK))         # EQ not running: curve shown as for HS80
        self.assertTrue(p.graph.device_correction)
        self.assertTrue(p.corr_note.isHidden())
        p.set_status(self.st(self.BT))
        p.select_profile("duz")
        pump()
        self.assertTrue(p.corr_note.isHidden())
        p.select_profile("cs2")
        pump()
        self.assertFalse(p.corr_note.isHidden())

    def test_older_router_uses_device_state_file(self):
        p = self.page
        self.device_fits = False                 # cihaz.json: active headphone does not fit
        p.set_status(dict(STATUS))               # no "kulaklik" field
        pump()
        self.assertFalse(p.graph.device_correction)
        self.assertEqual(p.corr_note.text(), "Bu kulaklıkta HS80 düzeltmesi uygulanmıyor; eğri "
                                             "HS80 için ölçüldü.")
        self.device_fits = True
        p.set_status(None)
        self.assertTrue(p.graph.device_correction)
        self.assertTrue(p.corr_note.isHidden())


class ToneTests(PageTestBase):
    """"Kişisel ton" sliders (CHANGE 5, C3)."""

    def setUp(self):
        self.kisisel = FakeKisisel()
        super().setUp()
        self.page.set_status(TONE_STATUS)
        pump()

    def wait_tone(self):
        self.assertTrue(pump(lambda: self.apply_tone.called and self.page.tone_applier.idle()))

    def calls(self):
        return {c.args[0]: (c.args[1], c.kwargs) for c in self.apply_tone.call_args_list}

    def test_layout_and_labels(self):
        p = self.page
        self.assertTrue(p.tone_head.isVisible())
        self.assertEqual(list(p.tone_sliders), ["bas", "vokal", "parlaklik"])
        s, val = p.tone_sliders["bas"]
        self.assertEqual((s.minimum(), s.maximum()), (-6, 6))     # ±3 dB in 0.5 dB steps
        self.assertEqual(val.text(), "0 dB")
        p.set_tone("bas", 1.5)
        self.assertEqual(val.text(), "+1,5 dB")
        p.set_tone("bas", -0.5)
        self.assertEqual(val.text(), "-0,5 dB")
        p.set_tone("bas", 9)                                      # clamped by the slider
        self.assertEqual(val.text(), "+3,0 dB")
        self.assertEqual(eq_page.fmt_tone(0.0), "0 dB")
        self.assertEqual(eq_page.fmt_tone(1.27), "+1,3 dB")          # A/B estimates (0.01 dB)
        self.assertEqual(p.tone_scope_box.itemText(0), "Tüm profiller (genel)")
        self.assertEqual(p.tone_scope_box.itemText(1),
                         f"Bu tür için (Müzik: {eq_page.GROUP_LABELS['elektronik']})")
        self.assertIn("CS2", p.tone_state.text())                 # cs2 page: excluded note
        help_text = " ".join(l.text() for l in p.tone_body.findChildren(eq_page.QLabel))
        for word in ("Bas sıcaklığı", "Vokal netliği", "Parlaklık", "adım"):
            self.assertIn(word, help_text)

    def test_genel_preview_debounced_on_all_tone_sinks_but_cs2(self):
        p = self.page
        p.set_tone("bas", 0.5)
        p.set_tone("bas", 1.0)
        p.set_tone("bas", 1.5)
        self.apply_tone.assert_not_called()                       # debounced
        self.wait_tone()
        calls = self.calls()
        self.assertEqual(set(calls), {"oyun", "muzik", "film"})   # CS2 excluded by default
        self.assertEqual(self.apply_tone.call_count, 3)           # one batch
        self.assertEqual(calls["muzik"][0], "elektronik")         # router's current genre
        self.assertEqual(calls["oyun"][0], "")
        self.assertEqual(calls["muzik"][1], {"ramp_s": 0.3, "values": {**ZERO, "bas": 1.5}})
        self.assertTrue(p.tone_dirty())
        self.assertTrue(p.tone_save_btn.isEnabled())
        self.assertIn("Kaydedilmemiş", p.tone_state.text())
        self.assertEqual(self.kisisel.saves, [])                  # preview only

    def test_cs2_opt_in_from_profiles(self):
        p = self.page
        p.work["profil"]["cs2"]["kisisel_ton"] = True
        p.set_tone("vokal", 1.0)
        self.wait_tone()
        self.assertIn("cs2", self.calls())

    def test_tur_scope_preview_and_merge_save(self):
        p = self.page
        p.tone_scope_box.setCurrentIndex(1)
        self.assertEqual(p.tone_scope, "tur")
        self.assertEqual(p.tone_target, ("muzik", "elektronik"))
        self.kisisel.data["genel"]["bas"] = 1.0                   # saved before: genel bas +1
        p.reload_tone()
        p.tone_scope_box.setCurrentIndex(1)
        self.assertEqual(p.tone_sliders["bas"][1].text(), "0 dB")   # tür offsets shown
        p.set_tone("vokal", 1.0)
        p.set_tone("bas", 2.5)
        self.wait_tone()
        calls = self.calls()
        self.assertEqual(set(calls), {"muzik"})                   # only that genre's sink
        self.assertEqual(calls["muzik"][1]["values"],
                         {"bas": 3.0, "vokal": 1.0, "parlaklik": 0.0})   # clamped genel+tür
        # meanwhile `kulak ab` saved another genre: must survive our save
        self.kisisel.data["tur"]["oyun:rekabetci"] = {"bas": -1.0, "vokal": 0, "parlaklik": 0}
        self.assertTrue(p.tone_save())
        saved = self.kisisel.saves[-1]
        self.assertEqual(saved["tur"]["muzik:elektronik"], {"bas": 2.5, "vokal": 1.0,
                                                            "parlaklik": 0.0})
        self.assertEqual(saved["tur"]["oyun:rekabetci"]["bas"], -1.0)
        self.assertEqual(saved["genel"]["bas"], 1.0)              # genel untouched
        self.assertFalse(p.tone_dirty())
        self.assertFalse(p.tone_save_btn.isEnabled())
        self.assertIn("Kaydedildi", p.tone_state.text())

    def test_tur_scope_disabled_without_genre(self):
        p = self.page
        p.set_status({**STATUS, "variants": {"muzik": {"group": "", "mode": "auto"}}})
        self.assertEqual(p.tone_scope_box.itemText(1), "Bu tür için (şu an tür yok)")
        self.assertFalse(p.tone_scope_box.model().item(1).isEnabled())
        p.tone_scope_box.setCurrentIndex(1)
        self.assertEqual(p.tone_scope, "genel")
        # a genre appears, user picks it, then it goes away with nothing pending -> genel
        p.set_status(TONE_STATUS)
        p.tone_scope_box.setCurrentIndex(1)
        self.assertEqual(p.tone_scope, "tur")
        p.set_status({**STATUS, "variants": {}})
        self.assertEqual(p.tone_scope, "genel")

    def test_tur_target_kept_while_dirty(self):
        p = self.page
        p.tone_scope_box.setCurrentIndex(1)
        p.set_tone("parlaklik", -1.0)
        p.set_status({**TONE_STATUS, "variants": {"muzik": {"group": "pop_vokal", "mode": "auto"}}})
        self.assertEqual(p.tone_target, ("muzik", "elektronik"))
        self.assertIn("çalmıyor", p.tone_state.text())

    def test_revert_restores_saved_live(self):
        p = self.page
        p.set_tone("parlaklik", 2.0)
        self.wait_tone()
        self.apply_tone.reset_mock()
        p.tone_revert_btn.click()
        self.wait_tone()
        calls = self.calls()
        self.assertEqual(set(calls), {"oyun", "muzik", "film"})
        self.assertEqual(calls["film"][1]["values"], ZERO)
        self.assertFalse(p.tone_dirty())
        self.assertEqual(p.tone_sliders["parlaklik"][1].text(), "0 dB")

    def test_leaving_page_without_saving_restores(self):
        p = self.page
        p.set_tone("vokal", -1.5)
        self.wait_tone()
        self.apply_tone.reset_mock()
        p.hide()                                                  # page switch / window closed
        self.wait_tone()
        self.assertEqual(self.calls()["muzik"][1]["values"], ZERO)
        self.assertFalse(p.tone_dirty())
        self.assertEqual(self.kisisel.saves, [])
        p.show()
        pump()
        self.assertEqual(p.tone_sliders["vokal"][1].text(), "0 dB")

    def test_discard_sync_on_quit(self):
        p = self.page
        p.set_tone("bas", 1.0)
        self.wait_tone()
        self.apply_tone.reset_mock()
        p.tone_applier.stop()                                     # quitting: no worker
        p.discard_tone_preview(sync=True)
        self.assertEqual(self.apply_tone.call_count, 3)
        self.assertEqual(self.apply_tone.call_args.kwargs["values"], ZERO)

    def test_debounce_cancelled_by_leaving(self):
        p = self.page
        p.set_tone("bas", 1.0)                                    # preview not sent yet
        p.discard_tone_preview()
        pump(timeout=0.3)
        self.apply_tone.assert_not_called()                       # nothing to undo either
        self.assertFalse(p.tone_dirty())

    def test_reset_and_save(self):
        self.kisisel.data["genel"] = {"bas": 1.0, "vokal": -0.5, "parlaklik": 0.5}
        p = self.page
        p.reload_tone()
        self.assertEqual(p.tone_sliders["vokal"][1].text(), "-0,5 dB")
        self.assertTrue(p.tone_reset_btn.isEnabled())
        p.tone_reset_btn.click()
        self.assertEqual([v.text() for _s, v in p.tone_sliders.values()], ["0 dB"] * 3)
        self.wait_tone()
        self.assertEqual(self.calls()["muzik"][1]["values"], ZERO)
        self.assertTrue(p.tone_save())
        self.assertEqual(self.kisisel.data["genel"], ZERO)

    def test_reload_respects_pending_edits(self):
        p = self.page
        self.kisisel.data["genel"]["bas"] = 2.0
        p.reload_tone()
        self.assertEqual(p.tone_sliders["bas"][1].text(), "+2,0 dB")
        p.set_tone("vokal", 1.0)
        self.kisisel.data["genel"]["bas"] = -2.0
        p.reload_tone()                                           # dirty: keep the user's edit
        self.assertEqual(p.tone_sliders["bas"][1].text(), "+2,0 dB")

    def test_graph_includes_k_bands(self):
        p = self.page
        p.select_profile("muzik")
        g = p.graph
        self.assertIsNone(g.tone_bands)
        before = g.curves()[2].copy()
        p.set_tone("parlaklik", 3.0)
        self.assertEqual([b["fc"] for b in g.tone_bands], [120.0, 2500.0, 8000.0])
        after = g.curves()[2]
        hi = g._freqs > 15000
        lo = g._freqs < 40
        self.assertAlmostEqual(float((after - before)[hi].mean()), 3.0, delta=0.4)
        self.assertAlmostEqual(float((after - before)[lo].mean()), 0.0, delta=0.1)
        self.assertIn(f"Müzik · {eq_page.GROUP_LABELS['elektronik']} için etkin",
                      p.tone_state.text())
        img = p.grab()                                            # legend paints
        self.assertFalse(img.isNull())
        p.select_profile("cs2")                                   # excluded: no k-bands drawn
        self.assertIsNone(g.tone_bands)
        p.select_profile("duz")
        self.assertIn("Düz", p.tone_state.text())

    def test_tone_filters_helpers(self):
        self.assertEqual(eq_page.tone_filters(ZERO), [])
        if callable(getattr(eqgen, "tone_filters", None)):       # C1's helper is used
            self.assertEqual(eq_page.tone_filters({**ZERO, "bas": 1.0}),
                             eqgen.tone_filters({**ZERO, "bas": 1.0}))
        with mock.patch.object(eqgen, "tone_filters", None, create=True):   # own fallback
            flt = eq_page.tone_filters({"bas": 1.0, "vokal": 0, "parlaklik": 0}, FakeKisisel)
            self.assertEqual(flt[0], {"tip": "LSC", "fc": 120.0, "kazanc": 1.0, "q": 0.7,
                                      "aktif": True})
            tup = type("K", (), {"TON_BANDS": {"bas": ("LSC", 100.0, 0.5)}})
            self.assertEqual(eq_page.tone_filters({"bas": 1.0}, tup)[0]["fc"], 100.0)
        with mock.patch.object(eqgen, "tone_filters", create=True,
                               return_value=[{"tip": "PK", "fc": 1000.0, "kazanc": 1.0, "q": 1.0}]):
            self.assertEqual(eq_page.tone_filters({"bas": 1.0})[0]["fc"], 1000.0)

    def test_without_apply_tone(self):
        p = self.page
        with mock.patch.object(eq_page, "tone_apply_fn", return_value=None):
            p.set_tone("bas", 1.0)
            pump(timeout=0.3)
            self.assertIn("Canlı önizleme yok", p.tone_state.text())
            self.assertTrue(p.tone_save())
            self.assertIn("sonraki şarkı", p.tone_state.text())
        self.apply_tone.assert_not_called()

    def test_genre_change_reapplies_preview(self):
        p = self.page
        p.set_tone("bas", 1.0)
        self.wait_tone()
        self.apply_tone.reset_mock()
        p.set_status({**TONE_STATUS, "variants": {"muzik": {"group": "pop_vokal", "mode": "auto"}}})
        self.wait_tone()
        self.assertEqual(self.calls(), {"muzik": ("pop_vokal", {"ramp_s": 0.3,
                                                                "values": {**ZERO, "bas": 1.0}})})

    def test_apply_failure_reported(self):
        self.apply_tone.return_value = False
        self.page.set_tone("bas", 1.0)
        self.wait_tone()
        self.assertTrue(pump(lambda: "uygulanamadı" in self.page.tone_state.text()))

    def test_ab_needs_music(self):
        p = self.page
        p.set_status(STATUS)                                      # nothing on Müzik
        self.assertFalse(p.start_tone_ab(which=which_of("ghostty", "kulak")))
        self.launch.assert_not_called()
        self.assertIn("önce Müzik", p.tone_state.text())
        p.set_status(None)
        self.assertFalse(p.start_tone_ab(which=which_of("ghostty", "kulak")))
        self.assertIn("bilinmiyor", p.tone_state.text())

    def test_ab_launches_terminal(self):
        p = self.page
        p.set_tone("bas", 1.0)                                    # pending preview is dropped
        self.assertTrue(p.start_tone_ab(which=which_of("ghostty", "kulak", "konsole")))
        self.launch.assert_called_once_with(["/usr/bin/ghostty", "-e",
                                             "/home/u/.local/bin/kulak", "ab"])
        self.assertFalse(p.tone_dirty())
        self.assertIn("terminalde açıldı", p.tone_state.text())
        self.launch.reset_mock()
        p.start_tone_ab(which=which_of("konsole", "kulak"))       # fallback terminal
        self.assertEqual(self.launch.call_args.args[0][:2], ["/usr/bin/konsole", "-e"])
        self.launch.reset_mock()
        self.assertFalse(p.start_tone_ab(which=which_of("kulak")))
        self.launch.assert_not_called()
        self.assertIn("Terminal bulunamadı", p.tone_state.text())

    def test_ab_button_and_kulak_fallback(self):
        cmd = eq_page.kulak_command("ab", which=lambda _n: None)
        self.assertEqual(cmd[-1], "ab")
        self.assertTrue(cmd[0].endswith("kulak") or cmd[1:3] == ["-m", "oto_eq.cli"])
        with mock.patch.object(eq_page.shutil, "which",
                               side_effect=which_of("ghostty", "kulak")):
            self.page.tone_ab_btn.click()
        self.launch.assert_called_once()


class TranslationTests(PageTestBase):
    """CHANGE 8: a forced catalog reaches the page's texts (built after the switch)."""

    CATALOG = {
        "Bantlar": "Bänder",
        "Kaydet": "Speichern",
        "Kayıtlı": "Gespeichert",
        "Şu an kullanan: {uygulamalar}": "Wird gerade genutzt von: {uygulamalar}",
        "Ön kazanç {db:+.1f} dB": "Vorverstärkung {db:+.1f} dB",
        "{k}/{n} doğru = gerçekten fark duyuyorsun. Bunun şans eseri olma ihtimali "
        "{yuzde} (bilimsel eşik {esik}).":
            "{k} von {n} = du hörst wirklich einen Unterschied (Zufall: {yuzde} %).",
    }

    def setUp(self):
        from oto_eq import i18n
        self.addCleanup(i18n.sifirla)          # runs after tearDown: back to the default
        super().setUp()
        old = self.page                        # built in Turkish: replace it
        old.applier.stop()
        old.tone_applier.stop()
        old.close()
        old.deleteLater()
        i18n.sifirla("de", self.CATALOG)
        self.page = eq_page.EqPage()           # tearDown cleans this one up
        self.page.resize(1200, 1100)
        self.page.show()
        pump()

    def test_forced_catalog_reaches_widgets(self):
        p = self.page
        titles = [l.text() for l in p.bands_card.findChildren(eq_page.QLabel)]
        self.assertIn("Bänder", titles)
        self.assertNotIn("Bantlar", titles)
        self.assertEqual(p.save_btn.text(), "Speichern")
        self.assertEqual(p.state_lbl.text(), "Gespeichert")
        self.assertIn("Vorverstärkung", p.gain_lbl.text())
        self.assertIn(" dB", p.gain_lbl.text())                # number + unit still formatted
        p.set_status(STATUS)
        self.assertEqual(p.apps_lbl.text(), "Wird gerade genutzt von: Counter-Strike 2")
        # untranslated msgid -> Turkish fallback
        self.assertEqual(p.del_btn.text(), "Seçiliyi sil")
        self.assertTrue(eq_page.abx_verdict(7, 8).startswith("7 von 8 = du hörst"))


class RenderTests(PageTestBase):
    def test_grab(self):
        for prof in ("cs2", "duz"):
            self.page.select_profile(prof)
            pump()
            img = self.page.grab()
            self.assertFalse(img.isNull())
            self.assertGreater(img.width(), 800)


if __name__ == "__main__":
    unittest.main()
