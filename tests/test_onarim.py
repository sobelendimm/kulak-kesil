"""onarim (self-repair) tests: pure decisions, fake observations, dry-run executor.

Nothing here touches the real system: every command goes to a fake runner,
notifications to a list, files to temp dirs.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from oto_eq import onarim as O

HS80 = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1c55-00.analog-stereo"
HS80_WIRED = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_USB_Gaming_Headset_1c54-00.analog-stereo"
SPK = "alsa_output.pci-0000_15_00.6.analog-stereo"
OURS = ["oto_eq_cs2", "oto_eq_oyun", "oto_eq_muzik", "oto_eq_film", "oto_eq_duz"]


def status(active=True, ready=True, default=HS80, streams=None, dominant=None):
    return {"active": active, "sinks_ready": ready, "default_sink": default, "mode": "auto",
            "streams": streams or [], "dominant": dominant}


def gozlem(sinks=None, default=HS80, unit=None, xrun=None, kayit=0, wp=0):
    return O.Gozlem(sinks=[HS80, SPK] + OURS if sinks is None else sinks, default_sink=default,
                    sinks_unit=unit or {"LoadState": "loaded", "ActiveState": "active"},
                    xrun=xrun, kayit_sayisi=kayit, wp_kalinti=wp)


def kinds(eylemler):
    return [e.tur for e in eylemler]


class TestParsePwTop(unittest.TestCase):
    OUT = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
C   42      0      0    ---     ---   ---   ---     0                  hs
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R   42    256  48000  24.7us   7.1us  0.00  0.00    5    S24LE 2 48000 alsa_output.usb-Corsair_HS80
R   93    256  48000   9.3us  10.9us  0.00  0.00    2    F32LE 2 48000  + game.exe
S   57      0      0    ---     ---   ---   ---     0                  oto_eq_cs2
I  140      0      0   0.0us   0.0us  ???   ???     1         F32P 2 0 oto_eq_oyun_out
S   99      0      0    ---     ---   ---   ---     0                  Kulak Kesil Mikrofon
"""

    def test_last_block_names_and_counts(self):
        r = O.parse_pwtop(self.OUT)
        self.assertEqual(r, {"42:alsa_output.usb-Corsair_HS80": 5, "93:game.exe": 2,
                             "57:oto_eq_cs2": 0, "140:oto_eq_oyun_out": 1,
                             "99:Kulak Kesil Mikrofon": 0})

    def test_garbage(self):
        self.assertEqual(O.parse_pwtop(""), {})
        self.assertEqual(O.parse_pwtop("hata\nS ID\nX y z"), {})


class TestCikislar(unittest.TestCase):
    def setUp(self):
        self.d = O.Durum()

    def k(self, now, st=None, g=None, ayar=None):
        return O.kontrol(st or status(ready=False), now,
                         g if g is not None else gozlem(sinks=[HS80, SPK] + OURS[:3]),
                         self.d, ayar)

    def test_grace_then_restart_then_notify_only(self):
        self.assertEqual(self.k(0), [])
        self.assertEqual(self.k(10), [])
        e = self.k(25)
        self.assertEqual(kinds(e), [O.YENIDEN_BASLAT])
        self.assertEqual(e[0].hedef, "oto-eq-sinks.service")
        self.assertIn("oto_eq_film, oto_eq_duz", e[0].neden)
        # still missing after the restart: grace again, then notification only (10 min rule)
        self.assertEqual(self.k(30), [])
        e = self.k(50)
        self.assertEqual(kinds(e), [O.BILDIRIM])
        self.assertIn("10 dakikada en çok bir", e[0].govde)
        self.assertIn("systemctl --user restart oto-eq-sinks", e[0].govde)
        self.assertEqual(self.k(120), [])               # notification rate-limited too
        self.assertEqual(kinds(self.k(25 + 601)), [O.YENIDEN_BASLAT])   # window passed

    def test_consecutive_cap(self):
        t = 0.0
        restarts = 0
        for _ in range(60):
            restarts += kinds(self.k(t)).count(O.YENIDEN_BASLAT)
            t += 60
        self.assertEqual(restarts, O.RESTART_UST)
        self.assertEqual(self.d.ardisik_restart, O.RESTART_UST)
        # recovery resets the counter
        self.assertEqual(self.k(t, st=status(), g=gozlem()), [])
        self.assertEqual(self.d.ardisik_restart, 0)

    def test_router_inactive_does_nothing(self):
        g = gozlem(sinks=[SPK] + OURS[:3], default=SPK)
        for t in (0, 30, 60):
            self.assertEqual(self.k(t, st=status(active=False, ready=False, default=SPK), g=g), [])

    def test_not_installed_notifies(self):
        g = gozlem(sinks=[HS80], unit={"LoadState": "not-found", "ActiveState": "inactive"})
        self.k(0, g=g)
        e = self.k(30, g=g)
        self.assertEqual(kinds(e), [O.BILDIRIM])
        self.assertIn("install.sh", e[0].govde)

    def test_restart_in_progress_waits(self):
        g = gozlem(sinks=[HS80], unit={"LoadState": "loaded", "ActiveState": "activating"})
        self.k(0, g=g)
        self.assertEqual(self.k(30, g=g), [])

    def test_auto_restart_disabled(self):
        a = O.Ayar(otomatik_yeniden_baslat=False)
        self.k(0, ayar=a)
        e = self.k(30, ayar=a)
        self.assertEqual(kinds(e), [O.BILDIRIM])
        self.assertIn("sınırına", e[0].govde)

    def test_status_only_without_observation(self):
        st = status(ready=False)
        self.assertEqual(O.kontrol(st, 0, None, self.d), [])
        self.assertEqual(kinds(O.kontrol(st, 30, None, self.d)), [O.YENIDEN_BASLAT])

    def test_no_status_no_action(self):
        self.assertEqual(O.kontrol(None, 0, gozlem(), self.d), [])
        self.assertEqual(O.kontrol({}, 0, gozlem(), self.d), [])


class TestXrun(unittest.TestCase):
    def setUp(self):
        self.d = O.Durum()

    def k(self, now, xr):
        return O.kontrol(status(), now, gozlem(xrun=xr), self.d)

    def test_burst(self):
        self.assertEqual(self.k(0, {"1:hs80": 3, "2:oto_eq_cs2": 0}), [])     # baseline
        self.assertEqual(self.k(60, {"1:hs80": 5, "2:oto_eq_cs2": 1}), [])    # +3 < 8
        e = self.k(120, {"1:HS80 x": 0, "1:hs80": 9, "2:oto_eq_cs2": 6})     # +4 +5
        self.assertEqual(kinds(e), [O.BILDIRIM])
        self.assertIn("9 ses kesintisi", e[0].govde)
        self.assertIn("oto_eq_cs2 +5", e[0].govde)
        e = self.k(180, {"1:hs80": 30, "2:oto_eq_cs2": 6})
        self.assertEqual(kinds(e), [O.KAYIT])                                 # rate-limited
        e = self.k(180 + O.XRUN_BILDIRIM_ARALIK, {"1:hs80": 60, "2:oto_eq_cs2": 6})
        self.assertEqual(kinds(e), [], "baseline older than XRUN_MAX_ARALIK is not compared")

    def test_counter_reset_and_new_nodes(self):
        self.k(0, {"1:a": 100})
        self.assertEqual(kinds(self.k(60, {"1:a": 9, "7:new": 50})), [O.BILDIRIM])  # reset → 9

    def test_unobserved(self):
        self.assertEqual(O.kontrol(status(), 0, gozlem(xrun=None), self.d), [])
        self.assertIsNone(self.d.xrun_onceki)


class TestHs80(unittest.TestCase):
    def setUp(self):
        self.d = O.Durum()

    def k(self, now, default, sinks=None, st_default=None):
        st = status(default=st_default if st_default is not None else default,
                    active=default == HS80)
        return O.kontrol(st, now, gozlem(default=default, sinks=sinks), self.d)

    def test_our_sink_default_notifies(self):
        self.assertEqual(self.k(0, "oto_eq_film"), [])
        e = self.k(10, "oto_eq_film")
        self.assertEqual(kinds(e), [O.BILDIRIM])
        self.assertIn("HS80", e[0].govde)
        self.assertEqual(self.k(20, "oto_eq_film"), [])

    def test_hs80_present_not_default_nudges_once_per_episode(self):
        self.assertEqual(self.k(0, SPK), [])
        self.assertEqual(self.k(30, SPK), [])          # router's own 30 s window first
        e = self.k(50, SPK)
        self.assertEqual(kinds(e), [O.TARA])
        self.assertIn("HS80 kablosuz", e[0].neden)
        self.assertEqual(self.k(50 + 700, SPK), [])    # same episode: no repeat
        self.assertEqual(self.k(800, HS80), [])        # episode ends
        self.k(900, SPK)
        self.assertEqual(kinds(self.k(1500, SPK)), [O.TARA])   # new episode, 10 min passed

    def test_wired_hs80_is_headset(self):
        for t in (0, 60, 120):
            self.assertEqual(self.k(t, HS80_WIRED, sinks=[HS80_WIRED, SPK] + OURS), [])

    def test_no_hs80_no_nudge(self):
        for t in (0, 60, 120):
            self.assertEqual(self.k(t, SPK, sinks=[SPK] + OURS), [])

    def test_stale_router_status(self):
        self.k(0, HS80, st_default=SPK)
        e = self.k(40, HS80, st_default=SPK)
        self.assertEqual(kinds(e), [O.TARA])
        self.assertIn("eski", e[0].neden)

    def test_custom_headset_pattern(self):
        a = O.Ayar(kulaklik_desen="(?i)hdmi")
        st = status(default=SPK, active=False)
        g = gozlem(default=SPK, sinks=["alsa_output.hdmi", SPK] + OURS)
        O.kontrol(st, 0, g, self.d, a)
        self.assertEqual(kinds(O.kontrol(st, 60, g, self.d, a)), [O.TARA])


class TestWp(unittest.TestCase):
    def setUp(self):
        self.d = O.Durum()

    def test_cleanup_only_when_quiet(self):
        busy = status(streams=[{"index": 1}], dominant="muzik")
        self.assertEqual(O.kontrol(busy, 0, gozlem(wp=3), self.d), [])
        self.assertEqual(O.kontrol(status(), 0, gozlem(wp=3, kayit=1), self.d), [])
        self.assertEqual(O.kontrol(status(), 0, gozlem(wp=3, kayit=None), self.d), [])
        e = O.kontrol(status(), 0, gozlem(wp=3), self.d)
        self.assertEqual(kinds(e), [O.WP_TEMIZLE])
        self.assertIn("3 test", e[0].neden)
        self.assertEqual(O.kontrol(status(), 3600, gozlem(wp=3), self.d), [])     # once a day
        self.assertEqual(kinds(O.kontrol(status(), 90000, gozlem(wp=3), self.d)), [O.WP_TEMIZLE])

    def test_disabled_or_clean(self):
        self.assertEqual(O.kontrol(status(), 0, gozlem(wp=0), self.d), [])
        self.assertEqual(O.kontrol(status(), 0, gozlem(wp=5), self.d,
                                   O.Ayar(wp_otomatik_temizle=False)), [])


class TestContained(unittest.TestCase):
    def test_crashing_check(self):
        def boom(*a):
            raise RuntimeError("x")
        old = O.KONTROLLER
        O.KONTROLLER = (boom, O._kontrol_wp)
        try:
            with self.assertLogs("oto_eq.onarim", "ERROR"):
                e = O.kontrol(status(), 0, gozlem(wp=1), O.Durum())
        finally:
            O.KONTROLLER = old
        self.assertEqual(kinds(e), [O.WP_TEMIZLE])


class FakeRun:
    """subprocess.run stand-in: records calls; scripted results per argv prefix."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def __call__(self, cmd, **kw):
        self.calls.append((list(cmd), kw))
        for prefix, res in self.results.items():
            if tuple(cmd[:len(prefix)]) == prefix:
                return res
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class TestGozle(unittest.TestCase):
    def test_parses_and_read_only(self):
        seen = []
        outs = {
            ("pactl", "list", "short", "sinks"): "1\t" + HS80 + "\tPipeWire\tx\tIDLE\n2\toto_eq_cs2\tP\n",
            ("pactl", "get-default-sink"): HS80 + "\n",
            ("systemctl", "--user", "show"): "LoadState=loaded\nActiveState=active\n",
            ("pactl", "list", "short", "source-outputs"):
                "52\t88\t-\tPipeWire\tf\n60\t88\t1234\tPipeWire\tf\n",
            ("pw-top",): TestParsePwTop.OUT,
        }

        def run(cmd, timeout=5.0):
            seen.append(cmd)
            for p, o in outs.items():
                if tuple(cmd[:len(p)]) == p:
                    return 0, o
            return 1, ""
        g = O.gozle(run=run, wp=False)
        self.assertEqual(g.sinks, [HS80, "oto_eq_cs2"])
        self.assertEqual(g.default_sink, HS80)
        self.assertEqual(g.sinks_unit["ActiveState"], "active")
        self.assertEqual(g.kayit_sayisi, 1)
        self.assertEqual(g.xrun["42:alsa_output.usb-Corsair_HS80"], 5)
        self.assertIsNone(g.wp_kalinti)
        for cmd in seen:
            self.assertTrue(cmd[:2] in (["pactl", "list"], ["pactl", "get-default-sink"],
                                        ["systemctl", "--user"], ["pw-top", "-b"]), cmd)
            if cmd[0] == "systemctl":
                self.assertEqual(cmd[2], "show")

    def test_failures_are_none(self):
        g = O.gozle(run=lambda cmd, timeout=5.0: (1, ""), wp=False)
        self.assertEqual(g, O.Gozlem())

    def test_wp_leftovers_counted_with_real_tool(self):
        wp = O.wp_modul()
        self.assertIsNotNone(wp, "tools/wp_temizle.py yüklenemedi")
        with tempfile.TemporaryDirectory() as td:
            Path(td, "stream-properties").write_text(
                "[stream-properties]\n"
                'Output/Audio:application.name:oto-eq-test-cs2={"volume":1.0}\n'
                'Output/Audio:application.name:Firefox={"volume":1.0}\n')
            self.assertEqual(O.wp_kalinti_say(Path(td), wp), 1)
            self.assertEqual(O.wp_kalinti_say(Path(td) / "yok", wp), 0)


class TestUygula(unittest.TestCase):
    def setUp(self):
        self.notes = []
        self.notify = lambda b, g: self.notes.append((b, g))

    def restart(self):
        return O.Eylem(O.YENIDEN_BASLAT, "eksik", "restart", hedef="oto-eq-sinks.service",
                       baslik="B", govde="G")

    def test_dry_run_runs_nothing(self):
        run = FakeRun()
        rescans = []
        for e in (self.restart(), O.Eylem(O.BILDIRIM, "n", baslik="b"),
                  O.Eylem(O.TARA, "t"), O.Eylem(O.WP_TEMIZLE, "w")):
            r = O.uygula(e, dry_run=True, run=run, notify=self.notify,
                         rescan=lambda: rescans.append(1))
            self.assertTrue(r.startswith("[deneme]"), r)
        self.assertEqual(run.calls, [])
        self.assertEqual(self.notes, [])
        self.assertEqual(rescans, [])

    def test_restart_verified_sinks_missing_is_a_failure(self):
        run = FakeRun()
        with self.assertLogs("oto_eq.onarim", "ERROR"):
            r = O.uygula(self.restart(), run=run, notify=self.notify, dogrula=lambda: False)
        self.assertFalse(r.basarili)
        self.assertIn("geri gelmedi", r)
        self.assertEqual(self.notes[-1][0], "Kulak Kesil: onarım başarısız")
        r = O.uygula(self.restart(), run=FakeRun(), notify=self.notify, dogrula=lambda: True)
        self.assertTrue(r.basarili)

    def test_sinks_back_check(self):
        lines = "\n".join(f"{i}\t{n}\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE"
                          for i, n in enumerate(O.sink_name(p) for p in O.ALL_SINK_PROFILES))
        ok = FakeRun({("pactl",): SimpleNamespace(returncode=0, stdout=lines, stderr="")})
        self.assertTrue(O.cikislar_dondu(ok, sure=0))
        self.assertFalse(O.cikislar_dondu(FakeRun(), sure=0))

    def test_restart_ok_and_fail(self):
        run = FakeRun()
        with self.assertLogs("oto_eq.onarim", "WARNING"):
            r = O.uygula(self.restart(), run=run, notify=self.notify)
        self.assertEqual(run.calls[0][0], ["systemctl", "--user", "restart", "oto-eq-sinks.service"])
        self.assertIn("yeniden başlatıldı", r)
        self.assertEqual(self.notes, [("B", "G")])
        run = FakeRun({("systemctl",): SimpleNamespace(returncode=1, stdout="", stderr="yok")})
        with self.assertLogs("oto_eq.onarim", "ERROR"):
            r = O.uygula(self.restart(), run=run, notify=self.notify)
        self.assertIn("yeniden başlatılamadı: yok", r)
        self.assertEqual(self.notes[-1][0], "Kulak Kesil: onarım başarısız")

    def test_notification_switch(self):
        with self.assertLogs("oto_eq.onarim", "WARNING"):
            O.uygula(O.Eylem(O.BILDIRIM, "n", baslik="b", govde="g"), notify=self.notify,
                     bildirim=False)
        self.assertEqual(self.notes, [])
        with self.assertLogs("oto_eq.onarim", "WARNING"):
            O.uygula(O.Eylem(O.BILDIRIM, "n", baslik="b", govde="g"), notify=self.notify)
        self.assertEqual(self.notes, [("b", "g")])

    def test_rescan_and_log(self):
        calls = []
        with self.assertLogs("oto_eq.onarim", "INFO"):
            self.assertIn("istendi", O.uygula(O.Eylem(O.TARA, "t"), rescan=lambda: calls.append(1)))
            self.assertIn("yapılamadı", O.uygula(O.Eylem(O.TARA, "t")))
            self.assertEqual(O.uygula(O.Eylem(O.KAYIT, "sadece kayıt")), "sadece kayıt")
        self.assertEqual(calls, [1])

    def test_wp_cleanup_uses_tool_apply(self):
        got = []
        fake = SimpleNamespace(apply=lambda d, say: got.append(d) or True)
        with tempfile.TemporaryDirectory() as td, self.assertLogs("oto_eq.onarim", "INFO"):
            r = O.uygula(O.Eylem(O.WP_TEMIZLE, "w"), wp=fake, state_dir=Path(td))
            self.assertEqual(got, [Path(td)])
            self.assertIn("temizlendi", r)
            fake.apply = lambda d, say: False
            self.assertIn("başarısız", O.uygula(O.Eylem(O.WP_TEMIZLE, "w"), wp=fake,
                                               state_dir=Path(td)))

    def test_bildir_utf8_env(self):
        run = FakeRun()
        self.assertTrue(O.bildir("Başlık ş", "gövde", run=run))
        cmd, kw = run.calls[0]
        self.assertEqual(cmd[0], "notify-send")
        self.assertEqual(cmd[-2:], ["Başlık ş", "gövde"])
        self.assertEqual(kw["env"]["LC_ALL"], "C.UTF-8")

        def boom(*a, **k):
            raise OSError("yok")
        with self.assertLogs("oto_eq.onarim", "WARNING"):
            self.assertFalse(O.bildir("x", run=boom))


class TestOnarici(unittest.TestCase):
    def make(self, td, dry=False, st=None):
        self.t = 0.0
        self.notes = []
        self.rescans = []
        self.run = FakeRun()
        g = gozlem(sinks=[HS80, SPK])
        return O.Onarici(lambda: st if st is not None else status(ready=False),
                         rescan=lambda: self.rescans.append(1), dry_run=dry,
                         gozle_fn=lambda: g, run=self.run,
                         notify=lambda b, gv: self.notes.append(b), clock=lambda: self.t,
                         wall=lambda: 1000.0, gecmis_dosya=Path(td) / "onarim.json")

    def test_adim_executes_and_records(self):
        with tempfile.TemporaryDirectory() as td:
            o = self.make(td)
            self.assertEqual(o.adim(), [])
            self.t = 30
            with self.assertLogs("oto_eq.onarim", "WARNING"):
                res = o.adim()
            self.assertEqual([e.tur for e, _ in res], [O.YENIDEN_BASLAT])
            self.assertEqual(self.run.calls[0][0][:3], ["systemctl", "--user", "restart"])
            self.assertEqual(len(self.notes), 1)
            hist = O.gecmis_oku(Path(td) / "onarim.json")
            self.assertEqual(hist[0]["tur"], O.YENIDEN_BASLAT)
            self.assertEqual(hist[0]["zaman"], 1000)
            self.assertEqual(o.durum_ozet()["ardisik_restart"], 1)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            o = self.make(td, dry=True)
            o.adim()
            self.t = 30
            res = o.adim()
            self.assertTrue(res[0][1].startswith("[deneme]"))
            self.assertEqual(self.run.calls, [])
            self.assertFalse((Path(td) / "onarim.json").exists())

    def test_status_errors(self):
        with tempfile.TemporaryDirectory() as td:
            o = self.make(td)
            o.status_fn = lambda: (_ for _ in ()).throw(ConnectionError("yok"))
            self.assertEqual(o.adim(), [])
            o.status_fn = lambda: None
            self.assertEqual(o.adim(), [])

    def test_thread_start_stop(self):
        with tempfile.TemporaryDirectory() as td:
            o = self.make(td, st=status())
            calls = []
            o.adim = lambda: calls.append(1) or []
            o.baslat(aralik=0.01, ilk_gecikme=0)
            deadline = time.monotonic() + 3
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            o.durdur()
            self.assertGreaterEqual(len(calls), 2)
            self.assertIsNone(o._thread)

    def test_gecmis_oku_broken(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.json"
            self.assertEqual(O.gecmis_oku(p), [])
            p.write_text("{bozuk")
            self.assertEqual(O.gecmis_oku(p), [])
            O.gecmis_yaz([{"tur": "a"}] * 30, p)
            self.assertEqual(len(O.gecmis_oku(p)), O.GECMIS_UZUNLUK)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_and_success_flag(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"yeniden tarama yapılamadı (yönlendirici bağlı değil): {neden}":
                            "Neuscan nicht möglich: {neden}"})
        r = O.uygula(O.Eylem(O.TARA, "t"))
        self.assertEqual(r, "Neuscan nicht möglich: t")
        self.assertFalse(r.basarili)              # success is a flag, not the (translated) text
        ok = O.uygula(O.Eylem(O.TARA, "t"), rescan=lambda: None)
        self.assertTrue(ok.basarili)

    def test_history_records_basarili(self):
        with tempfile.TemporaryDirectory() as td:
            run = FakeRun()
            o = O.Onarici(lambda: status(ready=False), rescan=None, gozle_fn=lambda: gozlem(sinks=[HS80, SPK]),
                          run=run, notify=lambda b, g: None, clock=lambda: 30.0, wall=lambda: 1000.0,
                          gecmis_dosya=Path(td) / "onarim.json")
            o.durum.since["eksik"] = 0.0
            with self.assertLogs("oto_eq.onarim", "WARNING"):
                o.adim()
            hist = O.gecmis_oku(Path(td) / "onarim.json")
            self.assertIs(hist[0]["basarili"], True)


if __name__ == "__main__":
    unittest.main()
