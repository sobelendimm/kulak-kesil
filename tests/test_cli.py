"""CLI tests against a fake JSON-lines Unix-socket server (no real daemon)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import cli, config
from oto_eq import ui_text as tx

STATUS = {
    "ok": True, "active": True, "default_sink": "alsa_output.usb-Corsair_HS80",
    "mode": "auto", "fixed_profile": None, "dominant": "cs2", "sinks_ready": True,
    "streams": [
        {"index": 41, "app": "Counter-Strike 2", "binary": "cs2", "category": "cs2",
         "profile": "cs2", "reason": "ikili 'cs2' kuralı", "sink": "oto_eq_cs2"},
        {"index": 52, "app": "Firefox", "binary": "firefox", "category": "muzik",
         "profile": "muzik", "reason": "MPRIS: müzik", "sink": "oto_eq_muzik"},
        {"index": 60, "app": "Discord", "binary": "Discord", "category": "sesli",
         "profile": None, "reason": "sesli sohbet", "sink": "alsa_output.usb-Corsair_HS80"},
    ],
}

# status["kulaklik"] samples (CONTRACT CHANGE 7: kulaklik.Aygit.durum())
KULAKLIK_HS80 = {"ad": "alsa_output.usb-Corsair_HS80", "tur": "usb", "etiket": "USB kulaklık",
                 "aciklama": "CORSAIR HS80 RGB Wireless Gaming Receiver", "kulaklik": True,
                 "duzeltme": True, "neden": "USB, adından kulaklık", "baglanti": "usb",
                 "bt_profil": ""}
KULAKLIK_SPK = {"ad": "alsa_output.pci-0000_15_00.6.analog-stereo", "tur": "hoparlor",
                "etiket": "Hoparlör", "aciklama": "Ryzen HD Audio Controller", "kulaklik": False,
                "duzeltme": False, "neden": "hat çıkışı portu etkin", "baglanti": "pci",
                "bt_profil": ""}
KULAKLIK_BT = {"ad": "bluez_output.AC_80_0A_11_22_33.1", "tur": "bluetooth",
               "etiket": "Bluetooth kulaklık", "aciklama": "WH-1000XM4", "kulaklik": True,
               "duzeltme": False, "neden": "Bluetooth aygıt biçimi: headphone",
               "baglanti": "bluetooth", "bt_profil": "a2dp-sink"}


class FakeServer:
    """Minimal oto-eq IPC server: records requests, answers canned replies."""

    def __init__(self, path: Path, status=None, watch_events=(), reply=None):
        self.path = path
        self.status = status or STATUS
        self.watch_events = list(watch_events)
        self.reply = reply or {"ok": True}
        self.requests: list[dict] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(path))
        self.sock.listen(8)
        self._stop = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        with conn, conn.makefile("rwb") as f:
            line = f.readline()
            if not line:
                return
            req = json.loads(line)
            self.requests.append(req)
            if req.get("cmd") == "status":
                out = [self.status]
            elif req.get("cmd") == "watch":
                out = [{"event": "status", **{k: v for k, v in ev.items() if k != "ok"}}
                       for ev in self.watch_events]
            else:
                out = [self.reply]
            for o in out:
                f.write((json.dumps(o, ensure_ascii=False) + "\n").encode())
                f.flush()

    def close(self):
        self._stop = True
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oto-eq-cli-")
        self.dir = Path(self.tmp.name)
        self.sock_path = self.dir / "oto-eq.sock"
        for p in (mock.patch.object(config, "SOCKET_PATH", self.sock_path),
                  # LIVE-SAFETY: never read/write the user's real genre settings
                  mock.patch.object(config, "CONFIG_DIR", self.dir / "cfg"),
                  mock.patch.object(config, "GENRE_FILE", self.dir / "cfg" / "tur-atamalari.json")):
            p.start()
            self.addCleanup(p.stop)
        self.server = None

    def tearDown(self):
        if self.server:
            self.server.close()
        self.tmp.cleanup()

    def serve(self, **kw):
        self.server = FakeServer(self.sock_path, **kw)
        return self.server

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:  # argparse
                rc = e.code
        return rc, out.getvalue(), err.getvalue()


class TestDurum(CliTestBase):
    def test_table(self):
        self.serve()
        rc, out, _ = self.run_cli("durum")
        self.assertEqual(rc, 0)
        self.assertIn("Kulak Kesil — durum", out)
        self.assertIn("Kulaklık", out)
        self.assertIn(": aktif", out)
        self.assertIn("Mod              : Otomatik", out)
        self.assertIn("Baskın profil    : CS2", out)
        self.assertIn("EQ çıkışları     : hazır", out)
        self.assertIn("Akışlar (3):", out)
        lines = [l for l in out.splitlines() if "→" in l]
        self.assertEqual(len(lines), 3)
        # aligned arrows
        self.assertEqual(len({l.index("→") for l in lines}), 1)
        self.assertIn("Counter-Strike 2 → CS2", lines[0])
        self.assertIn("(ikili 'cs2' kuralı)", lines[0])
        self.assertIn("EQ yok", lines[2])
        # all key/value separators aligned
        kv = [l for l in out.splitlines()[1:6]]
        self.assertEqual(len({l.index(" : ") for l in kv}), 1)
        self.assertEqual(self.server.requests, [{"cmd": "status"}])

    def test_fixed_inactive_no_streams(self):
        st = {**STATUS, "active": False, "mode": "fixed", "fixed_profile": "film",
              "dominant": None, "sinks_ready": False, "streams": []}
        self.serve(status=st)
        rc, out, _ = self.run_cli("durum")
        self.assertEqual(rc, 0)
        self.assertIn("Sabit: Film", out)
        self.assertIn("aktif değil", out)
        self.assertIn("HAZIR DEĞİL", out)
        self.assertIn("Çalan akış yok.", out)

    def test_kulaklik_type_and_model(self):
        self.serve(status={**STATUS, "kulaklik": KULAKLIK_HS80})
        rc, out, _ = self.run_cli("durum")
        self.assertEqual(rc, 0)
        self.assertIn("Kulaklık         : aktif — CORSAIR HS80 RGB Wireless Gaming Receiver · "
                      "USB kulaklık · HS80 düzeltmesi açık", out)

    def test_kulaklik_speaker_default(self):
        self.serve(status={**STATUS, "active": False, "kulaklik": KULAKLIK_SPK,
                           "default_sink": KULAKLIK_SPK["ad"]})
        rc, out, _ = self.run_cli("durum")
        self.assertIn("aktif değil — varsayılan çıkış kulaklık değil: Hoparlör "
                      "(Ryzen HD Audio Controller; hat çıkışı portu etkin)", out)
        self.assertNotIn("HS80 değil", out)

    def test_kulaklik_other_headphone_and_old_router(self):
        self.serve(status={**STATUS, "kulaklik": KULAKLIK_BT})
        rc, out, _ = self.run_cli("durum")
        self.assertIn("WH-1000XM4 · Bluetooth kulaklık · HS80 düzeltmesi nötr", out)
        self.assertNotIn("arama modu", out)
        # an older router sends no "kulaklik": the old wording, generalized
        self.assertEqual(cli.format_status({**STATUS, "active": False}).splitlines()[1],
                         "  Kulaklık         : aktif değil (varsayılan çıkış kulaklık değil)")
        self.assertIn("Kulaklık         : aktif\n", cli.format_status(STATUS))

    def test_kulaklik_bluetooth_call_mode(self):
        bt = {**KULAKLIK_BT, "bt_profil": "headset-head-unit"}
        out = cli.format_status({**STATUS, "kulaklik": bt})
        self.assertIn("Bluetooth arama modu (dar bant)", out)

    def test_watch_line_device_type(self):
        line = cli.format_watch_line({**STATUS, "active": False, "kulaklik": KULAKLIK_SPK})
        self.assertIn("pasif | çıkış: Hoparlör | mod:", line)
        self.assertNotIn("çıkış:", cli.format_watch_line(STATUS))

    def test_flat_and_off(self):
        streams = [{"index": 5, "app": "Discord", "binary": "Discord", "category": "sesli",
                    "profile": None, "reason": "sesli sohbet", "sink": "oto_eq_duz"}]
        self.serve(status={**STATUS, "mode": "off", "dominant": None, "streams": streams})
        rc, out, _ = self.run_cli("durum")
        self.assertIn("Discord → Düz", out)
        self.assertIn("Kapalı — EQ kapalı (seviye eşitli düz geçiş)", out)
        self.assertNotIn("EQ yok", out)

    def test_json(self):
        self.serve()
        rc, out, _ = self.run_cli("durum", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["dominant"], "cs2")
        self.assertNotIn("ok", data)


class TestKulaklikText(unittest.TestCase):
    """ui_text helpers for status["kulaklik"] (CHANGE 7)."""

    def test_texts(self):
        self.assertEqual(tx.kulaklik_text({"kulaklik": KULAKLIK_HS80}),
                         "CORSAIR HS80 RGB Wireless Gaming Receiver · USB kulaklık · "
                         "HS80 düzeltmesi açık")
        self.assertEqual(tx.kulaklik_text({"kulaklik": KULAKLIK_SPK}),
                         "Ryzen HD Audio Controller · Hoparlör — EQ devre dışı "
                         "(hat çıkışı portu etkin)")
        self.assertEqual(tx.kulaklik_kisa({"kulaklik": KULAKLIK_BT}),
                         "WH-1000XM4 (Bluetooth kulaklık)")
        self.assertEqual(tx.kulaklik_degil_text({"kulaklik": KULAKLIK_SPK}),
                         "Varsayılan çıkış bir kulaklık değil: Hoparlör (Ryzen HD Audio Controller).")
        self.assertEqual(tx.kulaklik_degil_text({"kulaklik": KULAKLIK_HS80}), "")
        self.assertEqual(tx.duzeltme_yok_notu({"kulaklik": KULAKLIK_BT}),
                         "Bu kulaklıkta (WH-1000XM4) HS80 düzeltmesi uygulanmıyor; eğri HS80 "
                         "için ölçüldü.")
        self.assertEqual(tx.duzeltme_yok_notu({"kulaklik": KULAKLIK_HS80}), "")
        self.assertEqual(tx.duzeltme_yok_notu({"kulaklik": KULAKLIK_SPK}), "")

    def test_game_keys_saved_as_the_resolver_compares_them(self):
        # typed as shown in the dialog hint, these were saved but never matched (1 Kas 2026)
        from oto_eq import tur
        self.assertEqual(tx.normalize_game_key("exe:PathOfExileSteam.exe"), "exe:pathofexilesteam")
        self.assertEqual(tx.normalize_game_key("Steam:2694490"), "steam:2694490")
        self.assertEqual(tx.normalize_game_key("oyun:Path of Exile 2"),
                         "oyun:" + tur.norm("Path of Exile 2"))
        self.assertEqual(tx.normalize_game_key("Path of Exile 2"), "Path of Exile 2")

    def test_router_texts_follow_this_process_language(self):
        # the router writes "etiket"/"neden" in its own language (Turkish); a GUI/CLI in
        # another language must translate them, not show "USB kulaklık" (1 Kas 2026)
        from oto_eq import i18n
        kat = {"USB kulaklık": "USB headphones", "Hoparlör": "Speaker",
               "hat çıkışı portu etkin": "line-out port active"}
        with mock.patch.dict(i18n._state, {"dil": "en", "katalog": kat}):
            self.assertIn("· USB headphones ·", tx.kulaklik_text({"kulaklik": KULAKLIK_HS80}))
            spk = tx.kulaklik_text({"kulaklik": KULAKLIK_SPK})
            self.assertIn("Speaker", spk)
            self.assertIn("line-out port active", spk)

    def test_missing_or_odd_field(self):
        for st in (None, {}, {"kulaklik": None}, {"kulaklik": "bozuk"}):
            with self.subTest(st=st):
                self.assertEqual(tx.kulaklik_text(st), "")
                self.assertEqual(tx.kulaklik_kisa(st), "")
                self.assertIsNone(tx.kulaklik_tile(st))
        # partial dict: no crash, readable fallbacks
        self.assertEqual(tx.kulaklik_text({"kulaklik": {"kulaklik": True}}),
                         "— · Bilinmeyen çıkış · HS80 düzeltmesi nötr")

    def test_bluetooth_call_profiles(self):
        for prof, call in (("a2dp-sink", False), ("", False), ("headset-head-unit", True),
                           ("headset-head-unit-msbc", True), ("handsfree_head_unit", True)):
            with self.subTest(prof=prof):
                self.assertEqual(tx.bt_arama_modu({**KULAKLIK_BT, "bt_profil": prof}), call)
        self.assertFalse(tx.bt_arama_modu(None))

    def test_tile(self):
        t = tx.kulaklik_tile({"kulaklik": KULAKLIK_HS80})
        self.assertEqual((t["title"], t["value"], t["state"]), ("Kulaklık", "USB kulaklık", "on"))
        self.assertEqual(t["lines"], ["CORSAIR HS80 RGB Wireless Gaming Receiver",
                                      "HS80 düzeltmesi açık"])
        self.assertIn("yalnız Corsair HS80 için", t["tip"])
        t = tx.kulaklik_tile({"kulaklik": KULAKLIK_SPK})
        self.assertEqual((t["value"], t["state"]), ("Hoparlör", "off"))
        self.assertIn("EQ devre dışı", t["lines"])
        self.assertIn("hat çıkışı portu etkin", t["tip"])
        t = tx.kulaklik_tile({"kulaklik": {**KULAKLIK_BT, "bt_profil": "headset-head-unit"}})
        self.assertEqual(t["state"], "wait")
        self.assertIn("HS80 düzeltmesi nötr", t["lines"])
        self.assertIn(tx.ARAMA_MODU, t["lines"])
        self.assertIn("A2DP", t["tip"])


class TestModes(CliTestBase):
    def test_set_mode_requests(self):
        self.serve()
        cases = [
            (("oto",), {"cmd": "set_mode", "mode": "auto"}, "Otomatik"),
            (("kapat",), {"cmd": "set_mode", "mode": "off"}, "Kapalı"),
            (("sabitle", "cs2"), {"cmd": "set_mode", "mode": "fixed", "profile": "cs2"}, "Sabit: CS2"),
            (("sabitle", "muzik"), {"cmd": "set_mode", "mode": "fixed", "profile": "muzik"}, "Sabit: Müzik"),
            (("yenile",), {"cmd": "reload"}, "yeniden yüklendi"),
        ]
        for argv, expected, text in cases:
            with self.subTest(argv=argv):
                self.server.requests.clear()
                rc, out, _ = self.run_cli(*argv)
                self.assertEqual(rc, 0)
                self.assertEqual(self.server.requests, [expected])
                self.assertIn(text, out)

    def test_bad_profile_is_usage_error(self):
        rc, _, err = self.run_cli("sabitle", "bas")
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("hata", err)

    def test_daemon_error(self):
        self.serve(reply={"ok": False, "error": "geçersiz profil"})
        rc, _, err = self.run_cli("oto")
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("geçersiz profil", err)

    def test_no_command(self):
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("durum", out)


class TestDaemonDown(CliTestBase):
    def test_down_message_and_exit_code(self):
        for argv in (("durum",), ("oto",), ("sabitle", "film"), ("kapat",), ("yenile",), ("izle",)):
            with self.subTest(argv=argv):
                rc, out, err = self.run_cli(*argv)
                self.assertEqual(rc, cli.EXIT_DOWN)
                self.assertIn("servisi (oto-eq) çalışmıyor", err)
                self.assertIn("systemctl --user start oto-eq", err)
                self.assertEqual(out, "")

    def test_stale_socket_file(self):
        # a leftover socket file with nobody listening
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.sock_path))
        s.close()
        rc, _, err = self.run_cli("durum")
        self.assertEqual(rc, cli.EXIT_DOWN)


class TestIzle(CliTestBase):
    def test_lines_then_drop(self):
        ev2 = {**STATUS, "mode": "off", "dominant": None, "streams": []}
        self.serve(watch_events=[STATUS, ev2])
        rc, out, err = self.run_cli("izle")
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("mod: Otomatik", lines[0])
        self.assertIn("baskın profil: CS2", lines[0])
        self.assertIn("Counter-Strike 2→CS2", lines[0])
        self.assertIn("mod: Kapalı", lines[1])
        self.assertIn("akış yok", lines[1])
        self.assertEqual(self.server.requests, [{"cmd": "watch"}])
        self.assertEqual(rc, cli.EXIT_DOWN)  # connection closed by server

    def test_ctrl_c(self):
        def boom():
            raise KeyboardInterrupt
            yield  # pragma: no cover
        with mock.patch("oto_eq.ipc.watch", return_value=boom()):
            rc, _, _ = self.run_cli("izle")
        self.assertEqual(rc, 0)


class TestEqUret(CliTestBase):
    def _fake_eqgen(self, path):
        """Install a fake oto_eq.eqgen for this test, whether or not the real one is imported.

        `from . import eqgen` prefers the package attribute over sys.modules, so
        both must be patched.
        """
        import types
        import oto_eq
        mod = types.ModuleType("oto_eq.eqgen")
        mod.write_conf = mock.Mock(return_value=path)
        for p in (mock.patch.dict("sys.modules", {"oto_eq.eqgen": mod}),
                  mock.patch.object(oto_eq, "eqgen", mod, create=True)):
            p.start()
            self.addCleanup(p.stop)
        return mod

    def test_generate_restart_rescan(self):
        self.serve()
        conf = self.dir / "oto-eq-sinks.conf"
        fake = self._fake_eqgen(conf)
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("oto_eq.cli.systemctl", return_value=ok) as sc, \
             mock.patch("oto_eq.cli.time.sleep"):
            rc, out, _ = self.run_cli("eq-uret")
        self.assertEqual(rc, 0)
        fake.write_conf.assert_called_once_with()
        sc.assert_called_once_with("restart", "oto-eq-sinks.service")
        self.assertEqual(self.server.requests, [{"cmd": "rescan"}])
        self.assertIn(str(conf), out)

    def test_restart_failure(self):
        fake = self._fake_eqgen(self.dir / "x.conf")
        bad = subprocess.CompletedProcess([], 1, "", "Unit not found")
        with mock.patch("oto_eq.cli.systemctl", return_value=bad):
            rc, _, err = self.run_cli("eq-uret")
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("Unit not found", err)

    def test_daemon_down_after_restart(self):
        fake = self._fake_eqgen(self.dir / "x.conf")
        ok = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("oto_eq.cli.systemctl", return_value=ok), \
             mock.patch("oto_eq.cli.time.sleep"):
            rc, _, err = self.run_cli("eq-uret")
        self.assertEqual(rc, cli.EXIT_DOWN)
        self.assertIn("çalışmıyor", err)


class TestFilesAndLaunchers(CliTestBase):
    def test_kurallar_copies_default_and_opens(self):
        cfg = self.dir / "cfg"
        defaults = self.dir / "defaults"
        defaults.mkdir()
        (defaults / "kurallar.toml").write_text("[genel]\n", encoding="utf-8")
        with mock.patch.object(config, "CONFIG_DIR", cfg), \
             mock.patch.object(config, "DEFAULTS_DIR", defaults), \
             mock.patch("oto_eq.cli.subprocess.Popen") as popen:
            rc, out, _ = self.run_cli("kurallar")
            self.assertEqual(rc, 0)
            self.assertEqual((cfg / "kurallar.toml").read_text(encoding="utf-8"), "[genel]\n")
            self.assertEqual(popen.call_args.args[0], ["xdg-open", str(cfg / "kurallar.toml")])
            # existing user file is not overwritten
            (cfg / "kurallar.toml").write_text("# benim\n", encoding="utf-8")
            rc, _, _ = self.run_cli("kurallar")
            self.assertEqual((cfg / "kurallar.toml").read_text(encoding="utf-8"), "# benim\n")

    def test_gunluk_execs_journalctl(self):
        with mock.patch("oto_eq.cli.os.execvpe") as ex:
            self.run_cli("gunluk")
        args = ex.call_args.args
        self.assertEqual(args[1], ["journalctl", "--user", "-u", "oto-eq", "-u", "oto-eq-sinks", "-f"])

    def test_eq_ciz(self):
        plot = self.dir / "egriler.png"
        def fake_run(cmd, **kw):
            plot.write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(config, "PLOT_FILE", plot), \
             mock.patch("oto_eq.cli.subprocess.run", side_effect=fake_run) as run, \
             mock.patch("oto_eq.cli.subprocess.Popen") as popen:
            rc, _, _ = self.run_cli("eq-ciz")
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_args.args[0][-2:], ["oto_eq.eqgen", "plot"])
        self.assertEqual(popen.call_args.args[0], ["xdg-open", str(plot)])


class TestMikrofon(CliTestBase):
    SOURCES = ["alsa_output.x.monitor", "alsa_input.pci-0000.analog-stereo",
               "alsa_input.usb-Corsair_CORSAIR_HS80-00.mono-fallback", "oto_eq_mikrofon"]

    def test_pick_real_source(self):
        self.assertEqual(cli.pick_real_source(self.SOURCES), "alsa_input.usb-Corsair_CORSAIR_HS80-00.mono-fallback")
        self.assertEqual(cli.pick_real_source(self.SOURCES[:2] + ["oto_eq_mikrofon"]),
                         "alsa_input.pci-0000.analog-stereo")
        self.assertIsNone(cli.pick_real_source(["a.monitor", "oto_eq_mikrofon"]))

    def run_mic(self, *argv, default="alsa_input.usb-Corsair_CORSAIR_HS80-00.mono-fallback", sources=None):
        with mock.patch("oto_eq.cli.list_sources", return_value=sources or self.SOURCES), \
             mock.patch("oto_eq.cli.default_source", return_value=default), \
             mock.patch("oto_eq.cli.set_default_source", return_value=True) as sds, \
             mock.patch("oto_eq.cli.systemctl",
                        return_value=subprocess.CompletedProcess([], 0, "active\n", "")), \
             mock.patch("oto_eq.cli.os.execvpe") as ex:
            rc, out, err = self.run_cli("mikrofon", *argv)
        return rc, out, err, sds, ex

    def test_status(self):
        rc, out, _e, sds, _x = self.run_mic()
        self.assertEqual(rc, 0)
        self.assertIn("Sanal mikrofon : çalışıyor", out)
        self.assertIn("kulak mikrofon varsayilan", out)
        sds.assert_not_called()

    def test_varsayilan_and_hs80(self):
        rc, out, _e, sds, _x = self.run_mic("varsayilan")
        self.assertEqual(rc, 0)
        sds.assert_called_once_with("oto_eq_mikrofon")
        rc, out, _e, sds, _x = self.run_mic("hs80", default="oto_eq_mikrofon")
        sds.assert_called_once_with("alsa_input.usb-Corsair_CORSAIR_HS80-00.mono-fallback")

    def test_missing_virtual_mic(self):
        rc, _o, err, sds, ex = self.run_mic("varsayilan", sources=self.SOURCES[:3])
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("oto-eq-mic", err)
        sds.assert_not_called()

    def test_test_execs_module(self):
        rc, _o, _e, _s, ex = self.run_mic("test", "3")
        self.assertEqual(ex.call_args.args[1][-3:], ["-m", "oto_eq.mikrofon_test", "3.0"])


ROOT = Path(__file__).resolve().parent.parent


VARIANTS = {"muzik": {"group": "metal_rock", "label": "Metal / Rock", "for": "Virgin Black",
                      "source": "MusicBrainz: gothic metal", "mode": "auto"},
            "oyun": {"group": "aksiyon_rpg", "label": "Aksiyon / RPG", "for": "Path of Exile 2",
                     "source": "Steam: Action RPG", "mode": "fixed", "key": "steam:2694490"}}


class TestTur(CliTestBase):
    def test_show(self):
        self.serve(status={**STATUS, "variants": VARIANTS})
        rc, out, _ = self.run_cli("tur")
        self.assertEqual(rc, 0)
        self.assertIn("Müzik türü : Metal / Rock — Virgin Black  (otomatik; kaynak: MusicBrainz — gothic metal)", out)
        self.assertIn("Oyun türü  : Aksiyon / RPG — Path of Exile 2  (sabit; kaynak: Steam — Action RPG)", out)
        self.assertIn("Ses analizi: açık", out)
        self.assertIn("kulak tur dinle", out)
        rc, out, _ = self.run_cli("durum")
        self.assertIn("Metal / Rock — Virgin Black", out)
        self.assertIn("Aksiyon / RPG — Path of Exile 2 · sabit", out)

    def test_set(self):
        self.serve()
        for argv, group, text in ((("muzik", "metal_rock"), "metal_rock", "Metal / Rock"),
                                  (("oyun", "oto"), "auto", "Otomatik"),
                                  (("muzik", "genel"), "", "Genel")):
            self.server.requests.clear()
            rc, out, _ = self.run_cli("tur", *argv)
            self.assertEqual(rc, 0)
            self.assertEqual(self.server.requests, [{"cmd": "set_variant", "profile": argv[0], "group": group}])
            self.assertIn(text, out)

    def test_bad_group(self):
        rc, _o, err = self.run_cli("tur", "muzik", "rekabetci")   # a game group for music
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("bilinmeyen grup", err)
        rc, _o, err = self.run_cli("tur", "oyun")
        self.assertEqual(rc, cli.EXIT_USAGE)


class TestTon(CliTestBase):
    """`kulak ton` (kişisel ton): kisisel.json under the patched CONFIG_DIR, apply_tone mocked."""

    def setUp(self):
        super().setUp()
        from oto_eq import eqgen, kisisel
        self.kisisel, self.eqgen = kisisel, eqgen
        self.applied = []
        for p in (mock.patch.object(kisisel, "KISISEL_FILE", None),
                  mock.patch.object(config, "SINKS_CONF", self.dir / "cfg" / "yok.conf"),
                  mock.patch.object(eqgen, "apply_tone", side_effect=self._apply),
                  mock.patch.object(eqgen, "wait_ramps")):
            p.start()
            self.addCleanup(p.stop)
        self.ok = True

    def _apply(self, prof, group="", ramp_s=0.3, values=None, profiles=None, **kw):
        self.applied.append((prof, group, ramp_s, values))
        return self.ok

    def test_file_is_isolated(self):
        self.assertEqual(self.kisisel.path(), self.dir / "cfg" / "kisisel.json")

    def test_show(self):
        self.serve(status={**STATUS, "variants": VARIANTS})
        self.kisisel.set_genel(bas=1.5, parlaklik=-1)
        self.kisisel.set_tur("muzik", "metal_rock", vokal=-1)
        rc, out, _ = self.run_cli("ton")
        self.assertEqual(rc, 0)
        self.assertIn("Genel        : bas +1.5  vokal 0  parlaklık -1", out)
        self.assertIn("Müzik · Metal / Rock: bas 0  vokal -1  parlaklık 0", out)
        self.assertIn("CS2    : kapalı (profiller.toml kisisel_ton = false)", out)
        self.assertRegex(out, r"Müzik\s+· Metal / Rock\s+: bas \+1\.5  vokal -1  parlaklık -1")
        self.assertRegex(out, r"Film\s+: bas \+1\.5  vokal 0  parlaklık -1")
        self.assertEqual(self.applied, [])

    def test_show_without_daemon(self):
        rc, out, _ = self.run_cli("ton")
        self.assertEqual(rc, 0)
        self.assertIn("yönlendirici yanıt vermiyor", out)
        self.assertIn("Tür farkları : yok", out)

    def test_set_genel_applies_live(self):
        self.serve(status={**STATUS, "variants": VARIANTS})
        rc, out, err = self.run_cli("ton", "bas=+1.3", "vokal=0", "parlaklık=-1")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.kisisel.load()["genel"], {"bas": 1.5, "vokal": 0.0,
                                                        "parlaklik": -1.0})
        self.assertIn("0.5 dB adımına yuvarlandı", out)
        self.assertIn("Genel kişisel ton: bas +1.5  vokal 0  parlaklık -1", out)
        self.assertEqual(sorted(self.applied), sorted([
            ("film", "", 0.3, None), ("muzik", "metal_rock", 0.3, None),
            ("oyun", "aksiyon_rpg", 0.3, None)]))          # cs2 opted out
        self.assertIn("CS2 kişisel tonu kullanmıyor", out)
        self.assertIn("Canlı uygulandı: Oyun, Müzik, Film", out)

    def test_set_genel_daemon_down_and_sinks_down(self):
        self.ok = False
        rc, out, _ = self.run_cli("ton", "vokal=2")
        self.assertEqual(rc, 0)
        self.assertEqual(self.kisisel.load()["genel"]["vokal"], 2.0)
        self.assertEqual([a[0] for a in self.applied], ["film"])   # variant groups unknown
        self.assertIn("Canlı uygulanamadı: Film", out)
        self.assertIn("Oyun, Müzik atlandı", out)

    def test_set_tur(self):
        self.serve(status={**STATUS, "variants": VARIANTS})
        self.kisisel.set_genel(bas=3)
        rc, out, err = self.run_cli("ton", "bas=-6", "--tur", "muzik:metal_rock")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.kisisel.load()["tur"]["muzik:metal_rock"]["bas"], -6.0)
        self.assertIn("Etkin (Müzik · Metal / Rock): bas -3", out)   # 3 + (-6): reachable
        self.assertNotIn("sınırlandı", out)
        self.assertEqual(self.applied, [("muzik", "metal_rock", 0.3, None)])
        rc, out, _ = self.run_cli("ton", "bas=+1", "--tur", "oyun:aksiyon_rpg")
        self.assertIn("Etkin (Oyun · Aksiyon / RPG): bas +3", out)
        self.assertIn("etkin değer sınırlandı", out)
        self.applied.clear()
        rc, out, _ = self.run_cli("ton", "vokal=1", "--tur", "muzik:elektronik")
        self.assertEqual(rc, 0)
        self.assertEqual(self.applied, [])                       # not the playing genre
        self.assertIn("Bu tür şu an çalmıyor", out)

    def test_reset(self):
        self.serve(status={**STATUS, "variants": VARIANTS})
        self.kisisel.set_genel(bas=1)
        self.kisisel.set_tur("muzik", "metal_rock", vokal=1)
        rc, out, _ = self.run_cli("ton", "sifirla", "--tur", "muzik:metal_rock")
        self.assertEqual(rc, 0)
        self.assertEqual(self.kisisel.load()["tur"], {})
        self.assertEqual(self.kisisel.load()["genel"]["bas"], 1.0)
        rc, out, _ = self.run_cli("ton", "sifirla")
        self.assertEqual(self.kisisel.load()["genel"], self.kisisel.zero())
        self.assertIn("Genel kişisel ton sıfırlandı", out)
        self.kisisel.set_tur("oyun", "rekabetci", bas=1)
        rc, out, _ = self.run_cli("ton", "sifirla", "--hepsi")
        self.assertEqual(rc, 0)
        self.assertEqual(self.kisisel.load(), {"genel": self.kisisel.zero(), "tur": {}})

    def test_usage_errors(self):
        for argv, msg in ((("ton", "bas=4"), "±3 dB"),
                          (("ton", "bas=7", "--tur", "muzik:elektronik"), "±6 dB"),
                          (("ton", "tiz2=1"), "anlaşılmadı"),
                          (("ton", "bas=çok"), "sayı bekleniyordu"),
                          (("ton", "bas"), "anlaşılmadı"),
                          (("ton", "bas=1", "--tur", "muzik:rekabetci"), "bilinmeyen tür"),
                          (("ton", "bas=1", "--tur", "film:x"), "tür yok"),
                          (("ton", "--tur", "muzik:elektronik"), "en az bir değer"),
                          (("ton", "sifirla", "bas=1"), "ek değer almaz"),
                          (("ton", "bas=1", "--hepsi"), "--hepsi")):
            rc, _o, err = self.run_cli(*argv)
            self.assertEqual(rc, cli.EXIT_USAGE, argv)
            self.assertIn(msg, err, argv)
        self.assertFalse((self.dir / "cfg" / "kisisel.json").exists())
        self.assertEqual(self.applied, [])

    def test_parse_ton_args(self):
        vals, notes = cli.parse_ton_args(["bas=+1,5", "tiz=-0.7"], 3.0, 0.5)
        self.assertEqual(vals, {"bas": 1.5, "parlaklik": -0.5})
        self.assertEqual(len(notes), 1)


class TestSourceText(unittest.TestCase):
    def test_names(self):
        from oto_eq import ui_text as tx
        cases = {"MusicBrainz: gothic metal": "MusicBrainz — gothic metal",
                 "itunes: Dance": "iTunes — Dance",
                 "Deezer: Electro, Dance": "Deezer — Electro, Dance",
                 "YouTube: bass house, tech house": "YouTube etiketleri — bass house, tech house",
                 "ses analizi: bas ağırlıklı, 124 BPM": "Ses analizi — bas ağırlıklı, 124 BPM",
                 "kullanıcı": "Kullanıcı ataması", "yerleşik liste": "Yerleşik liste",
                 "iTunes: tür bulunamadı": "iTunes — tür bulunamadı",
                 "şarkı adı: “Original Mix”": "Şarkı adı — “Original Mix”",
                 "": "", None: ""}
        for src, want in cases.items():
            self.assertEqual(tx.source_text(src), want)
        tip = tx.variant_tooltip({"label": "Elektronik", "for": "SAFI", "source": "ses analizi: bas ağır",
                                  "mode": "auto"})
        self.assertIn("Kaynak: Ses analizi — bas ağır", tip)
        for name in ("iTunes", "Deezer", "YouTube etiketleri", "MusicBrainz", "ses analizi"):
            self.assertIn(name, tx.GENRE_SOURCES_TEXT)
        self.assertIn("YouTube video kimliği", tx.GENRE_PRIVACY_TEXT)


class TestUiTextI18n(unittest.TestCase):
    """CHANGE 8: ui_text helpers translate through the active catalog."""

    def test_forced_catalog(self):
        from oto_eq import i18n, ui_text as tx
        i18n.sifirla("de", {"Etkin": "Aktiv", "{n} düzeltme toplandı": "{n} Korrekturen",
                            "Kişisel model": "Persönliches Modell",
                            "Kaynak: {kaynak}": "Quelle: {kaynak}"})
        try:
            t = tx.model_tile({"aktif": True})
            self.assertEqual((t["title"], t["value"]), ("Persönliches Modell", "Aktiv"))
            self.assertIn("12 Korrekturen", tx.model_tile({"duzeltme": 12, "gerek": 10})["lines"])
            self.assertIn("Quelle: iTunes — Dance", tx.variant_tooltip(
                {"label": "Elektronik", "source": "itunes: Dance"}))
        finally:
            i18n.sifirla()
        self.assertEqual(tx.model_tile({"aktif": True})["value"], "Etkin")

    def test_source_checks_use_keys(self):
        from oto_eq import ui_text as tx
        self.assertEqual(tx.source_short_key("Ses Analizi: bas ağır"), "ses analizi")
        self.assertEqual(tx.source_short_key("kullanıcı"), "kullanıcı")
        self.assertEqual(tx.source_short_key(""), "")
        t = tx.genre_tile({"streams": [{"profile": "muzik"}], "variants": {"muzik": {
            "label": "Rock", "group": "rock", "for": "X", "source": "kullanıcı"}}})
        self.assertIn("Kullanıcı", t["value"])


class FakeTap:
    instances: list = []

    def __init__(self, source="", rate=22050, seconds=45.0, runner=None, data=None):
        self.source, self.rate, self.seconds = source, rate, seconds
        self.data = data
        self.started = self.stopped = False
        FakeTap.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def running(self):
        return self.started and not self.stopped

    def window(self, t0, t1=None):
        return self.data


class FakeGuess:
    def __init__(self, group, confidence, scores=None, reason=""):
        self.group, self.confidence, self.scores, self.reason = group, confidence, scores or {}, reason


class FakeModel:
    def __init__(self, probs=None, exc=None):
        self.probs, self.exc, self.calls = probs, exc, []

    def predict(self, x, rate):
        self.calls.append((len(x), rate))
        if self.exc:
            raise self.exc
        return self.probs


class TestTurDinle(CliTestBase):
    """`kulak tur dinle`: ses_analiz / ses_model are faked (no audio devices)."""

    def setUp(self):
        super().setUp()
        import numpy as np
        self.np = np
        FakeTap.instances = []
        t = np.arange(int(22050 * 8), dtype=np.float32) / 22050
        self.music = (0.2 * np.sin(2 * np.pi * 55 * t)).astype(np.float32)
        self.waits = []
        p = mock.patch.object(cli, "_wait", lambda s: self.waits.append(s))
        p.start()
        self.addCleanup(p.stop)

    def fake_sa(self, data=None, feats=None, guess=None, analyzer=True, combined=None):
        import types
        data = self.music if data is None else data
        mod = types.SimpleNamespace(
            AudioTap=lambda **kw: FakeTap(data=data, **kw),
            features=lambda x, rate: feats if feats is not None else {"bas_orani": 0.41, "tempo": 124.0},
            classify=lambda x, rate: guess)
        if analyzer:
            class Analyzer:
                def __init__(self, tap=None, model=None):
                    self.tap, self.model = tap, model

                def guess(self, since, until=None, min_s=3.0):
                    return combined
            mod.AudioAnalyzer = Analyzer
        return mod

    def run_listen(self, *argv, sa=None, model=None, model_missing=False, sa_missing=False):
        import types

        def imp_sa():
            if sa_missing:
                raise ImportError("no ses_analiz")
            return sa or self.fake_sa()

        def imp_sm():
            if model_missing:
                raise ImportError("no ses_model")
            return types.SimpleNamespace(load_model=lambda path=None: model)
        with mock.patch.object(cli, "_import_ses_analiz", imp_sa), \
             mock.patch.object(cli, "_import_ses_model", imp_sm):
            return self.run_cli("tur", "dinle", *argv)

    def test_full_report(self):
        from oto_eq.models import GenreResult
        guess = FakeGuess("elektronik", 0.72, {"elektronik": 0.72, "pop_vokal": 0.2, "metal_rock": 0.05},
                          "bas ağırlıklı, düzenli vuruş 124 BPM")
        model = FakeModel({"elektronik": 0.81, "pop_vokal": 0.12, "klasik_akustik": 0.07})
        sa = self.fake_sa(guess=guess, combined=GenreResult("elektronik", "ses analizi: bas ağır, 124 BPM", 0.78))
        rc, out, err = self.run_listen(sa=sa, model=model)
        self.assertEqual(rc, 0, err)
        tap = FakeTap.instances[0]
        self.assertEqual((tap.source, tap.rate), ("oto_eq_muzik.monitor", 22050))
        self.assertTrue(tap.started and tap.stopped)
        self.assertEqual(self.waits, [8.0])                      # default ~8 s
        self.assertIn("8 saniye boyunca Müzik çıkışı dinleniyor", out)
        self.assertIn("kaydedilmez", out)
        self.assertIn("bas_orani : 0.41", out)
        self.assertIn("tempo     : 124", out)
        self.assertIn("Sinyal işleme tahmini : Elektronik / Hip-hop (%72) — bas ağırlıklı, düzenli vuruş 124 BPM", out)
        self.assertIn("puanlar: Elektronik / Hip-hop %72, Pop / Vokal %20, Metal / Rock %5", out)
        self.assertIn("Model tahmini         : Elektronik / Hip-hop %81, Pop / Vokal %12", out)
        self.assertIn("Birleşik sonuç        : Elektronik / Hip-hop (%78; kaynak: Ses analizi — bas ağır, 124 BPM)", out)
        self.assertEqual(model.calls, [(len(self.music), 22050)])

    def test_custom_seconds_and_unclear(self):
        rc, out, _ = self.run_listen("12,5", model=None)
        self.assertEqual(rc, 0)
        self.assertEqual(self.waits, [12.5])
        self.assertEqual(FakeTap.instances[0].seconds, 17.5)    # ring buffer > capture
        self.assertIn("Sinyal işleme tahmini : belirsiz → Genel", out)
        self.assertIn("Model tahmini         : yok (onnxruntime ya da model dosyası eksik", out)
        self.assertNotIn("Birleşik", out)

    def test_silence(self):
        sa = self.fake_sa(data=self.np.zeros(22050 * 8, dtype=self.np.float32))
        rc, out, err = self.run_listen(sa=sa)
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("sessizlik", err)
        self.assertNotIn("Özellikler", out)
        self.assertTrue(FakeTap.instances[0].stopped)

    def test_nothing_captured(self):
        sa = self.fake_sa(data=self.np.zeros(0, dtype=self.np.float32))
        rc, _o, err = self.run_listen(sa=sa)
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("ses yakalanamadı", err)
        self.assertIn("kulak saglik", err)

    def test_capture_error_stops_tap(self):
        sa = self.fake_sa()
        orig = sa.AudioTap

        def broken(**kw):
            tap = orig(**kw)
            tap.window = lambda *a: (_ for _ in ()).throw(OSError("parec yok"))
            return tap
        sa.AudioTap = broken
        rc, _o, err = self.run_listen(sa=sa)
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("ses yakalanamadı: parec yok", err)
        self.assertTrue(FakeTap.instances[0].stopped)

    def test_modules_missing(self):
        rc, _o, err = self.run_listen(sa_missing=True)
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("oto_eq.ses_analiz", err)
        self.assertEqual(FakeTap.instances, [])
        rc, out, _ = self.run_listen(model_missing=True)
        self.assertEqual(rc, 0)
        self.assertIn("Model tahmini         : yok (oto_eq.ses_model modülü henüz kurulu değil)", out)
        rc, out, _ = self.run_listen(model=FakeModel(exc=RuntimeError("bozuk")))
        self.assertEqual(rc, 0)
        self.assertIn("Model tahmini         : hata (bozuk)", out)

    def test_bad_seconds(self):
        for arg in ("abc", "0.5", "500"):
            rc, _o, err = self.run_listen(arg)
            self.assertEqual(rc, cli.EXIT_USAGE, arg)
        self.assertEqual(FakeTap.instances, [])

    def test_note_when_disabled(self):
        config.save_genre_settings({"cevrimici": True, "ses_analizi": False})
        rc, out, _ = self.run_listen()
        self.assertEqual(rc, 0)
        self.assertIn("Ses analizi Ayarlar'da kapalı", out)

    def test_report_pure(self):
        text = cli.format_listen_report(5, {}, None, "", None)
        self.assertIn("(5 sn, Müzik çıkışı)", text)
        self.assertIn("Model tahmini         : belirsiz", text)


class TestSaglikAndKalibre(CliTestBase):
    def fake_mod(self, name, **attrs):
        import types
        import oto_eq
        mod = types.ModuleType(f"oto_eq.{name}")
        for k, v in attrs.items():
            setattr(mod, k, v)
        for p in (mock.patch.dict("sys.modules", {f"oto_eq.{name}": mod}),
                  mock.patch.object(oto_eq, name, mod, create=True)):
            p.start()
            self.addCleanup(p.stop)
        return mod

    def test_saglik(self):
        res = [{"ad": "HS80", "durum": "tamam", "ayrinti": "takılı", "oneri": ""},
               {"ad": "servis", "durum": "uyari", "ayrinti": "kapalı", "oneri": "systemctl ..."}]
        self.fake_mod("saglik", run_checks=lambda: res)       # no format_report -> own printer
        rc, out, _ = self.run_cli("saglik")
        self.assertEqual(rc, 0)
        self.assertIn("✔ HS80: takılı", out)
        self.assertIn("⚠ servis: kapalı", out)
        self.assertIn("→ systemctl ...", out)
        rc, out, _ = self.run_cli("saglik", "--json")
        self.assertEqual(json.loads(out), res)
        res.append({"ad": "x", "durum": "hata", "ayrinti": "y", "oneri": ""})
        self.assertEqual(self.run_cli("saglik")[0], cli.EXIT_ERR)

    def test_kalibre(self):
        calls = []
        result = {"konusma_dbfs": -24.3, "gurultu_dbfs": -62.0, "tepe_dbfs": -4.1,
                  "oneri": {"kompresor.esik": -27.0, "kompresor.telafi": 7.5},
                  "aciklama": "telafi artırıldı", "uygulandi": False}

        def calibrate(seconds=5.0, apply=False):
            calls.append((seconds, apply))
            return {**result, "uygulandi": apply}
        self.fake_mod("micgen", calibrate=calibrate,
                      load_mic=lambda: {"kompresor": {"esik": -30.0, "telafi": 6.0}})
        rc, out, _ = self.run_cli("mikrofon", "kalibre")
        self.assertEqual(rc, 0)
        self.assertIn("Konuşma : -24.3 dBFS", out)
        self.assertIn("Kompresör eşiği", out)
        self.assertIn("-30 dB → -27 dB", out)
        self.assertIn("kulak mikrofon kalibre --uygula", out)
        rc, out, _ = self.run_cli("mikrofon", "kalibre", "--uygula")
        self.assertIn("uygulandı ✓", out)
        self.assertEqual(calls, [(5.0, False), (5.0, True)])

    def test_kalibre_error(self):
        self.fake_mod("micgen", calibrate=lambda seconds=5.0, apply=False: {"hata": "mikrofon yok"},
                      load_mic=lambda: {})
        rc, _o, err = self.run_cli("mikrofon", "kalibre")
        self.assertEqual(rc, cli.EXIT_ERR)
        self.assertIn("mikrofon yok", err)


class TestUnitsAndScripts(unittest.TestCase):
    """systemd unit relations + install/uninstall --dry-run in a fake HOME with stub tools."""

    def unit(self, name):
        lines = (ROOT / "systemd" / name).read_text(encoding="utf-8").splitlines()
        out = {}
        for ln in lines:
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                out.setdefault(k.strip(), []).extend(v.split())
        return out

    def test_unit_relations(self):
        sinks, router, mic = (self.unit(n) for n in
                              ("oto-eq-sinks.service", "oto-eq.service", "oto-eq-mic.service"))
        self.assertIn("oto-eq.service", sinks["BindsTo"])
        self.assertIn("pipewire.service", sinks["BindsTo"])
        self.assertNotIn("oto-eq.service", sinks.get("After", []))       # would be a cycle
        self.assertIn("oto-eq-sinks.service", router["After"])           # router stops first
        self.assertIn("oto-eq-sinks.service", router["Wants"])
        self.assertEqual(router["Restart"], ["on-failure"])
        self.assertIn("pipewire.service", mic["BindsTo"])
        self.assertNotIn("oto-eq.service", mic.get("BindsTo", []))

    def make_home(self, default_source="alsa_input.usb-Corsair_HS80-00.mono-fallback", installed=False):
        tmp = tempfile.TemporaryDirectory(prefix="oto-eq-sh-")
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        home, stubs = d / "home", d / "stubs"
        (home / "Masaüstü").mkdir(parents=True)
        (home / ".config/oto-eq").mkdir(parents=True)
        (home / ".config/oto-eq/mikrofon.conf").write_text("# stub\n")
        stubs.mkdir()
        scripts = {
            "systemctl": "exit 0",
            "gio": "exit 0", "kbuildsycoca6": "exit 0", "setsid": "exit 0",
            "xdg-user-dir": f'echo "{home}/Masaüstü"',
            "pactl": ('case "$1" in get-default-source) echo "%s" ;; list) printf '
                      "'1\\talsa_input.pci-0000.analog-stereo\\tx\\n2\\talsa_input.usb-Corsair_HS80-00.mono-fallback"
                      "\\tx\\n3\\toto_eq_mikrofon\\tx\\n' ;; *) exit 0 ;; esac") % default_source,
        }
        for name, body in scripts.items():
            f = stubs / name
            f.write_text(f"#!/bin/sh\n{body}\n")
            f.chmod(0o755)
        if installed:
            u = home / ".config/systemd/user"
            u.mkdir(parents=True)
            for n in ("oto-eq.service", "oto-eq-sinks.service", "oto-eq-mic.service"):
                (u / n).write_text("[Unit]\n")
        return home, stubs

    def run_script(self, script, home, stubs, *args, lang="tr_TR.UTF-8"):
        env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "LANG": lang,
               "XDG_RUNTIME_DIR": str(home)}
        r = subprocess.run(["bash", str(ROOT / script), "--dry-run", *args], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [ln for ln in r.stdout.splitlines() if ln.startswith("[deneme]")], r.stdout

    def test_scripts_speak_english_outside_turkish_locale(self):
        for script in ("install.sh", "uninstall.sh"):
            home, stubs = self.make_home()
            _, out = self.run_script(script, home, stubs, lang="C.UTF-8")
            self.assertIn("[dry-run]", out, script)
            self.assertNotIn("[deneme]", out, script)

    def idx(self, lines, needle):
        return next(i for i, ln in enumerate(lines) if needle in ln)

    def test_install_dry_run(self):
        home, stubs = self.make_home()
        lines, out = self.run_script("install.sh", home, stubs)
        self.assertIn("DENEME MODU", out)
        self.assertFalse(any("set-default-source" in ln for ln in lines))   # never touches the mic default
        self.assertLess(self.idx(lines, "restart oto-eq.service"), self.idx(lines, "start oto-eq-sinks.service"))
        self.assertFalse(any("restart oto-eq-sinks.service" in ln for ln in lines))  # one audio gap only
        self.assertTrue(any("enable oto-eq-mic.service" in ln for ln in lines))
        for name in ("kulak", "kulak-tepsi", "kulak-app"):
            self.assertTrue(any(f"yaz {home}/.local/bin/{name} " in ln for ln in lines))
        self.assertTrue(any("Kulak\\ Kesil.desktop metadata::trusted true" in ln for ln in lines))
        self.assertFalse((home / ".local/bin").exists())   # dry-run wrote nothing

    def test_uninstall_order(self):
        home, stubs = self.make_home(installed=True)
        lines, _ = self.run_script("uninstall.sh", home, stubs)
        order = [self.idx(lines, f"disable --now {u}") for u in
                 ("oto-eq.service", "oto-eq-sinks.service", "oto-eq-mic.service")]
        self.assertEqual(order, sorted(order))   # router first
        self.assertFalse(any("set-default-source" in ln for ln in lines))
        self.assertTrue((home / ".config/systemd/user/oto-eq.service").exists())  # nothing removed

    def test_uninstall_restores_default_mic(self):
        home, stubs = self.make_home(default_source="oto_eq_mikrofon", installed=True)
        lines, _ = self.run_script("uninstall.sh", home, stubs, "--purge")
        i = self.idx(lines, "set-default-source")
        self.assertIn("alsa_input.usb-Corsair_HS80-00.mono-fallback", lines[i])  # HS80 preferred
        self.assertLess(i, self.idx(lines, "disable --now oto-eq-mic.service"))
        self.assertTrue(any("rm -rf" in ln and ".config/oto-eq" in ln for ln in lines))


class TestI18n(unittest.TestCase):
    """CHANGE 8: a forced catalog reaches `kulak durum` / --help; widths follow the text."""
    DE = {"Kulak Kesil — durum": "Kulak Kesil — Status",
          "Varsayılan çıkış": "Standard-Audioausgabe",     # longer than every Turkish label
          "Mod": "Modus", "hazır": "bereit",
          "Akışlar ({n}):": "Streams ({n}):",
          "durumu ve akışları göster": "Status und Streams anzeigen",
          "{prog}: hata: {mesaj}": "{prog}: Fehler: {mesaj}"}

    def setUp(self):
        from oto_eq import i18n
        i18n.sifirla("de", self.DE)
        self.addCleanup(i18n.sifirla)

    def test_status_translated_and_aligned(self):
        out = cli.format_status(STATUS)
        lines = out.splitlines()
        self.assertEqual(lines[0], "Kulak Kesil — Status")
        w = len("Standard-Audioausgabe")
        self.assertIn(f"  {'Modus'.ljust(w)} : ", out)
        self.assertIn(f"  {'Standard-Audioausgabe'} : alsa_output.usb-Corsair_HS80", out)
        self.assertIn(f"  {'EQ çıkışları'.ljust(w)} : bereit", out)   # untranslated → Turkish
        self.assertIn("Streams (3):", out)
        self.assertEqual(cli.text_width("日本語"), 6)                 # CJK = 2 terminal cells
        self.assertEqual(cli.pad("日本", 5), "日本 ")

    def test_help_and_argparse_error(self):
        sub = next(a for a in cli.build_parser()._actions
                   if isinstance(a, cli.argparse._SubParsersAction))
        self.assertIn("Status und Streams anzeigen", sub.choices["durum"].format_help())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["sabitle", "yok"])
        self.assertIn("kulak sabitle: Fehler: argument PROFIL: invalid choice", err.getvalue())


class TestModuleEntry(unittest.TestCase):
    def test_help_runs_without_daemon(self):
        root = Path(__file__).resolve().parent.parent
        r = subprocess.run([os.sys.executable, "-m", "oto_eq.cli", "--help"], cwd=root,
                           env={**config.C_ENV, "PYTHONPATH": str(root)},
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertIn("sabitle", r.stdout)
        self.assertIn("eq-uret", r.stdout)


if __name__ == "__main__":
    unittest.main()
