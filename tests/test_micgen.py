"""Tests for oto_eq.micgen (pure + optional live check with test instances).

    python3 -m unittest discover -s tests -t . -p 'test_micgen.py'

The live test starts TEST instances only (``oto_eq_mikrofon_test*``), plays a
synthetic tone into a temporary null sink (inaudible) and never captures the
real microphone (``real_mic=False``).
"""
from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, micgen

DEFAULT_TOML = config.DEFAULTS_DIR / micgen.MIC_FILE
W = "alsa_input.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.mono-fallback"
WD = "alsa_input.usb-Corsair_CORSAIR_HS80_RGB_USB_Gaming_Headset_1a2b3c4d00000002-00.mono-fallback"
SINK_W = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo"
SINK_WD = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_USB_Gaming_Headset_1a2b3c4d00000002-00.analog-stereo"
SPK = "alsa_output.pci-0000_15_00.6.analog-stereo"
OTHERS = ["alsa_input.pci-0000_15_00.6.analog-stereo", SINK_W + ".monitor", "oto_eq_mikrofon"]


def shipped() -> dict:
    return micgen.load_mic(DEFAULT_TOML)


class LoadTest(unittest.TestCase):
    def test_shipped_matches_handbuilt_chain(self):
        d = shipped()
        self.assertEqual(d["genel"]["mod"], "tam")
        self.assertEqual((d["bas_filtre"]["fc"], d["bas_filtre"]["q"]), (90.0, 0.707))
        self.assertEqual(d["gurultu"]["esik"], 60.0)
        self.assertEqual(d["gurultu"]["bekleme_ms"], 250.0)
        self.assertEqual([(f["tip"], f["fc"], f["kazanc"]) for f in d["eq"]["filtreler"]],
                         [("PK", 250.0, -2.0), ("HSC", 8000.0, 1.0)])
        self.assertEqual((d["netlik"]["fc"], d["netlik"]["kazanc"], d["netlik"]["q"]),
                         (3500.0, 2.5, 0.9))
        s, t = d["kompresor"], d["tepe"]
        self.assertEqual((s["esik"], s["oran"], s["atak_ms"], s["birakma_ms"], s["telafi"]),
                         (-26.0, 3.0, 10.0, 150.0, 6.0))
        self.assertEqual((t["esik"], t["oran"]), (-3.0, 20.0))

    def test_gui_schema_keys(self):
        # keys A5's GUI page reads/writes
        d = shipped()
        self.assertIsInstance(d["gurultu"]["aktif"], bool)
        for sec, key in (("gurultu", "esik"), ("netlik", "kazanc"), ("kompresor", "esik"),
                         ("kompresor", "telafi")):
            self.assertIsInstance(d[sec][key], float)

    def test_errors_are_turkish_mic_errors(self):
        d = tomllib.loads(DEFAULT_TOML.read_text())
        for mutate, word in (
                (lambda x: x["eq"]["filtreler"][0].update(tip="XX"), "tip"),
                (lambda x: x["genel"].update(mod="yok"), "mod"),
                (lambda x: x["kompresor"].update(oran=0.5), "oran"),
                (lambda x: x["kompresor"].update(bilinmez=1), "bilinmeyen"),
                (lambda x: x["netlik"].update(bilinmez=1), "bilinmeyen"),
                (lambda x: x["genel"].update(giris_desen="("), "giris_desen"),
                (lambda x: x["mod"]["discord"]["kompresor"].update(yok=1), "bilinmeyen"),
                (lambda x: x["gurultu"].update(aktif="evet"), "true/false")):
            raw = copy.deepcopy(d)
            mutate(raw)
            with self.assertRaises(micgen.MicError) as cm:
                micgen.normalize_mic(raw)
            self.assertIn(word, str(cm.exception))


class EffectiveTest(unittest.TestCase):
    def test_discord_mode(self):
        d = shipped()
        d["genel"]["mod"] = "discord"
        e = micgen.effective(d)
        self.assertFalse(e["gurultu"]["aktif"])
        self.assertEqual((e["kompresor"]["oran"], e["kompresor"]["telafi"]), (2.0, 3.0))
        self.assertTrue(e["tepe"]["aktif"])
        self.assertEqual(e["eq"], shipped()["eq"])         # untouched sections stay

    def test_bypass(self):
        d = shipped()
        d["genel"]["aktif"] = False
        c = micgen.node_controls(d, rnnoise=True, lsp=True)
        self.assertEqual(c["bas_filtre"]["Freq"], micgen.HPF_OFF_HZ)
        self.assertEqual(c["secim"], {"Gain 1": 0.0, "Gain 2": 1.0})
        self.assertTrue(all(c[f"e{i}"]["Gain"] == 0.0 for i in (1, 2)))
        self.assertEqual(c["netlik"]["Gain"], 0.0)
        self.assertEqual((c["kompresor"]["enabled"], c["tepe"]["enabled"]), (0.0, 0.0))

    def test_controls_full(self):
        c = micgen.node_controls(shipped(), rnnoise=True, lsp=True)
        self.assertEqual(c["secim"], {"Gain 1": 1.0, "Gain 2": 0.0})
        self.assertAlmostEqual(c["kompresor"]["al"], 0.0501187, places=6)
        self.assertAlmostEqual(c["kompresor"]["mk"], 1.9952623, places=6)
        self.assertAlmostEqual(c["kompresor"]["kn"], 0.5011872, places=6)
        self.assertEqual(c["tepe"]["kn"], 1.0)
        self.assertEqual(c["kompresor"]["sla"], 0.0)         # never lookahead
        self.assertEqual(c["netlik"], {"Freq": 3500.0, "Q": 0.9, "Gain": 2.5})
        d = shipped()
        d["kompresor"]["diz_db"] = 24.0
        self.assertGreaterEqual(micgen.node_controls(d, True, True)["kompresor"]["kn"], 0.0631)

    def test_without_plugins(self):
        c = micgen.node_controls(shipped(), rnnoise=False, lsp=False)
        self.assertNotIn("gurultu", c)
        self.assertNotIn("secim", c)
        self.assertNotIn("kompresor", c)
        self.assertIn("netlik", c)

    def test_static_model(self):
        with mock.patch.object(micgen, "lsp_mono_available", return_value=True):
            self.assertAlmostEqual(micgen.expected_tone_db(shipped(), 1000.0, -20.0), -18.0, delta=0.15)
            self.assertAlmostEqual(micgen.expected_tone_db(shipped(), 1000.0, -50.0), -43.9, delta=0.15)


class RestartTest(unittest.TestCase):
    def test_live_vs_restart(self):
        old = shipped()
        new = copy.deepcopy(old)
        new["kompresor"]["oran"] = 5.0
        new["netlik"]["kazanc"] = 4.0
        new["eq"]["filtreler"][1]["kazanc"] = 4.0
        new["gurultu"]["aktif"] = False
        new["genel"]["mod"] = "discord"
        new["genel"]["aktif"] = False
        self.assertFalse(micgen.needs_restart(old, new))
        more = copy.deepcopy(old)
        more["eq"]["filtreler"].append({"tip": "PK", "fc": 1000.0, "kazanc": 1.0, "q": 1.0})
        self.assertTrue(micgen.needs_restart(old, more))
        typ = copy.deepcopy(old)
        typ["eq"]["filtreler"][1]["tip"] = "PK"
        self.assertTrue(micgen.needs_restart(old, typ))


class TrimTest(unittest.TestCase):
    def test_per_mode_trim(self):
        d = shipped()
        d["giris"] = {"kablosuz_db": 1.0, "kablolu_db": -6.0}
        d = micgen.normalize_mic(d)
        self.assertTrue(micgen.is_wired(WD))
        self.assertFalse(micgen.is_wired(W))
        self.assertEqual(micgen.input_trim_db(d, WD), -6.0)
        self.assertEqual(micgen.input_trim_db(d, W), 1.0)
        c = micgen.node_controls(d, True, True, target=WD)
        self.assertAlmostEqual(c["giris"]["Mult"], 0.5011872, places=6)
        d["genel"]["aktif"] = False                     # bypass = 0 dB
        self.assertEqual(micgen.node_controls(d, True, True, target=WD)["giris"]["Mult"], 1.0)
        bad = shipped()
        bad["giris"] = {"kablolu": 1.0}
        with self.assertRaises(micgen.MicError):
            micgen.normalize_mic(bad)

    def test_retarget_refuses_own_source_and_never_sets_default(self):
        with mock.patch.object(micgen, "_run") as run:
            self.assertFalse(micgen.retarget("oto_eq_mikrofon", data=shipped()))
            run.assert_not_called()


class SaveTest(unittest.TestCase):
    def test_round_trip(self):
        tmp = Path(tempfile.mkdtemp(prefix="oto-eq-mic-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        d = shipped()
        d["genel"]["mod"] = "discord"
        d["eq"]["filtreler"][0]["aktif"] = False
        p = micgen.save_mic(d, tmp / "mikrofon.toml")
        self.assertEqual(micgen.load_mic(p), micgen.normalize_mic(d))
        micgen.save_mic(d, p)
        self.assertTrue((tmp / "mikrofon.toml.bak").exists())
        bad = copy.deepcopy(d)
        bad["tepe"]["oran"] = -1
        with self.assertRaises(micgen.MicError):
            micgen.save_mic(bad, p)
        self.assertEqual(micgen.load_mic(p), micgen.normalize_mic(d))   # file untouched


class TargetTest(unittest.TestCase):
    def r(self, sources, sink):
        return micgen.resolve_target(shipped(), sources=sources, default_sink=sink)

    def test_wireless_only(self):
        self.assertEqual(self.r(OTHERS + [W], SINK_W), W)

    def test_wired_with_dongle_still_plugged(self):
        self.assertEqual(self.r(OTHERS + [W, WD], SINK_WD), WD)
        self.assertEqual(self.r(OTHERS + [WD, W], SINK_W), W)

    def test_default_sink_not_hs80_prefers_wired(self):
        self.assertEqual(self.r(OTHERS + [W, WD], SPK), WD)

    def test_none_and_never_ours_or_monitor(self):
        self.assertIsNone(self.r(OTHERS, SINK_W))
        self.assertIsNone(self.r([W.replace("alsa_input", "alsa_output") + ".monitor",
                                  "oto_eq_mikrofon", "oto_eq_mikrofon_test"], SINK_W))

    def test_fixed_name_wins(self):
        d = shipped()
        d["genel"]["giris_sabit"] = "benim_mikrofonum"
        self.assertEqual(micgen.resolve_target(d, sources=[W], default_sink=SINK_W),
                         "benim_mikrofonum")


class ConfTest(unittest.TestCase):
    def test_generated_conf(self):
        with mock.patch.object(micgen, "rnnoise_path",
                               return_value=Path("/usr/lib/ladspa/librnnoise_ladspa.so")), \
                mock.patch.object(micgen, "lsp_mono_available", return_value=True):
            txt = micgen.generate_conf(shipped(), target=W)
            none = micgen.generate_conf(shipped(), target=None)
        for s in ('node.name            = "oto_eq_mikrofon_giris"',
                  f'target.object        = "{W}"',
                  "node.linger          = true", "state.restore-target = false",
                  'inputs  = [ "giris:In" ]', '{ output = "giris:Out" input = "bas_filtre:In" }',
                  'node.name           = "oto_eq_mikrofon"', 'media.class         = "Audio/Source"',
                  '"VAD Threshold (%)" = 60.0', 'label = mixer', '"Gain 1" = 1.0 "Gain 2" = 0.0',
                  '{ output = "bas_filtre:Out" input = "secim:In 2" }',
                  'outputs = [ "tepe:out" ]', '"Freq" = 90.0',
                  '{ output = "netlik:Out" input = "kompresor:in" }'):
            self.assertIn(s, txt)
        self.assertIn(f'target.object        = "{micgen.MISSING_TARGET}"', none)
        self.assertNotIn("dont-fallback =", txt)
        self.assertEqual(txt.count("{"), txt.count("}"))
        self.assertEqual(txt.count("["), txt.count("]"))


def synth(speech_dbfs: float | None, noise_dbfs: float, seconds: float = 5.0,
          duty: float = 0.6, clip: bool = False, seed: int = 1):
    """Noise floor + 'speech' bursts (band-ish noise at a given RMS), float32."""
    import numpy as np
    rng = np.random.default_rng(seed)
    n = int(48000 * seconds)
    x = rng.standard_normal(n) * 10 ** (noise_dbfs / 20)
    if speech_dbfs is not None:
        seg = 9600                                   # 200 ms syllables
        for i in range(0, n - seg, seg):
            if rng.random() < duty:
                x[i:i + seg] += rng.standard_normal(seg) * 10 ** (speech_dbfs / 20)
    if clip:
        x[n // 2] = 0.99
    return x.astype(np.float32)


class CalibrateTest(unittest.TestCase):
    def run_cal(self, sig, apply=False, data=None):
        data = data or shipped()
        with mock.patch.object(micgen, "load_mic", return_value=data), \
                mock.patch.object(micgen, "resolve_target", return_value=W), \
                mock.patch.object(micgen, "_record", return_value=sig), \
                mock.patch.object(micgen, "lsp_mono_available", return_value=True), \
                mock.patch.object(micgen, "save_mic") as save, \
                mock.patch.object(micgen, "write_conf") as conf, \
                mock.patch.object(micgen, "set_live", return_value=True) as live:
            r = micgen.calibrate(5.0, apply=apply)
        self.conf_calls = conf.call_args_list
        return r, save, live

    def test_analyze(self):
        lv = micgen.analyze_levels(synth(-30.0, -70.0))
        self.assertAlmostEqual(lv["gurultu_dbfs"], -70.0, delta=1.5)
        self.assertAlmostEqual(lv["konusma_dbfs"], -30.0, delta=1.0)
        self.assertGreater(lv["aktif_oran"], 0.4)

    def test_normal_speech(self):
        r, save, live = self.run_cal(synth(-30.0, -70.0))
        self.assertNotIn("hata", r)
        self.assertEqual(set(r["oneri"]), {"kompresor.esik", "kompresor.telafi", "gurultu.esik"})
        self.assertEqual(r["oneri"]["kompresor.esik"], -36.0)
        self.assertEqual(r["oneri"]["gurultu.esik"], 60.0)
        self.assertTrue(0.0 <= r["oneri"]["kompresor.telafi"] <= 15.0)
        self.assertFalse(r["uygulandi"])
        save.assert_not_called()
        self.assertTrue(1 <= len(r["aciklama"].splitlines()) <= 3)

    def test_loud_speech_hits_target(self):
        r, _, _ = self.run_cal(synth(-16.0, -65.0))
        self.assertEqual(r["oneri"]["kompresor.esik"], -22.0)
        self.assertAlmostEqual(r["tahmini_cikis_dbfs"], -18.0, delta=0.5)
        # model check with the suggested values
        d = micgen._with(shipped(), r["oneri"])
        with mock.patch.object(micgen, "lsp_mono_available", return_value=True):
            self.assertAlmostEqual(micgen.predict_out_db(d, -16.0, W), -18.0, delta=0.6)

    def test_clamps(self):
        r, _, _ = self.run_cal(synth(-55.0, -80.0))
        self.assertEqual(r["oneri"]["kompresor.esik"], -40.0)
        self.assertEqual(r["oneri"]["kompresor.telafi"], 15.0)
        import numpy as np
        t = np.arange(48000 * 5) / 48000
        tone = (np.sin(2 * np.pi * 300 * t) * 10 ** (-5.0 / 20) * np.sqrt(2)
                * (np.sin(2 * np.pi * 1.5 * t) > -0.3))            # -5 dBFS RMS bursts
        tone = (tone + np.random.default_rng(2).standard_normal(t.size) * 1e-3).astype(np.float32)
        r, _, _ = self.run_cal(tone)
        self.assertNotIn("hata", r)
        self.assertEqual(r["oneri"]["kompresor.esik"], -12.0)

    def test_small_gap_lowers_vad(self):
        r, _, _ = self.run_cal(synth(-35.0, -50.0))
        self.assertNotIn("hata", r)
        self.assertLess(r["oneri"]["gurultu.esik"], 60.0)

    def test_no_speech(self):
        r, _, _ = self.run_cal(synth(None, -70.0))
        self.assertIn("hata", r)
        self.assertIn("Konuşma algılanmadı", r["hata"])
        self.assertFalse(r["uygulandi"])

    def test_clipping(self):
        r, save, _ = self.run_cal(synth(-20.0, -70.0, clip=True), apply=True)
        self.assertIn("kırpılıyor", r["hata"])
        save.assert_not_called()

    def test_apply_saves_and_sets_live(self):
        r, save, live = self.run_cal(synth(-30.0, -70.0), apply=True)
        self.assertTrue(r["uygulandi"])
        saved = save.call_args[0][0]
        self.assertEqual(saved["kompresor"]["esik"], r["oneri"]["kompresor.esik"])
        self.assertEqual(saved["kompresor"]["telafi"], r["oneri"]["kompresor.telafi"])
        self.assertEqual(saved["gurultu"]["esik"], r["oneri"]["gurultu.esik"])
        live.assert_called_once()
        # --uygula regenerates mikrofon.conf too (else the next service start loads the old one)
        self.assertEqual(len(self.conf_calls), 1)
        self.assertEqual(self.conf_calls[0].args[1], saved)

    def test_apply_in_discord_mode_writes_mode_override(self):
        d = shipped()
        d["genel"]["mod"] = "discord"
        r, save, _ = self.run_cal(synth(-30.0, -70.0), apply=True, data=d)
        saved = save.call_args[0][0]
        self.assertEqual(saved["mod"]["discord"]["kompresor"]["telafi"], r["oneri"]["kompresor.telafi"])
        self.assertEqual(micgen.effective(saved)["kompresor"]["telafi"], r["oneri"]["kompresor.telafi"])


class CalibrateGuiHelpersTest(unittest.TestCase):
    """apply_suggestion() and `kalibre --json` (the window's entry points). Never records,
    never touches the real mikrofon.toml/.conf or services."""

    ONERI = {"kompresor.esik": -30.0, "kompresor.telafi": 8.0, "gurultu.esik": 50.0}

    def apply(self, oneri=None, data=None, live=True):
        data = data or shipped()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        conf = Path(tmp.name) / "mikrofon.conf"
        with mock.patch.object(micgen, "load_mic", return_value=data), \
                mock.patch.object(micgen, "resolve_target", return_value=W), \
                mock.patch.object(micgen, "save_mic") as save, \
                mock.patch.object(micgen, "set_live", return_value=live) as setl, \
                mock.patch.object(micgen.subprocess, "run",
                                  side_effect=AssertionError("no external commands")):
            r = micgen.apply_suggestion(self.ONERI if oneri is None else oneri, conf_path=conf)
        return r, save, setl, conf

    def test_saves_regenerates_conf_and_applies_live(self):
        r, save, setl, conf = self.apply()
        self.assertEqual(r, {"degisti": True, "kaydedildi": True, "conf": True, "canli": True,
                             "yeniden_baslat": False})
        saved = save.call_args[0][0]
        self.assertEqual(saved["kompresor"]["esik"], -30.0)
        self.assertEqual(saved["kompresor"]["telafi"], 8.0)
        self.assertEqual(saved["gurultu"]["esik"], 50.0)
        setl.assert_called_once_with(saved)
        text = conf.read_text()
        self.assertIn("kompresör 3:1 eşik -30 dBFS telafi +8 dB", text)
        self.assertIn(W, text)

    def test_not_live_asks_for_restart(self):
        r, _save, _setl, _conf = self.apply(live=False)
        self.assertTrue(r["kaydedildi"])
        self.assertFalse(r["canli"])
        self.assertTrue(r["yeniden_baslat"])

    def test_unchanged_only_refreshes_conf(self):
        d = shipped()
        eff = micgen.effective(d)
        same = {"kompresor.esik": eff["kompresor"]["esik"], "gurultu.esik": eff["gurultu"]["esik"]}
        r, save, setl, conf = self.apply(same, data=d)
        self.assertFalse(r["degisti"])
        self.assertTrue(r["conf"] and conf.exists())
        save.assert_not_called()
        setl.assert_not_called()
        self.assertFalse(r["yeniden_baslat"])

    def test_discord_mode_override(self):
        d = shipped()
        d["genel"]["mod"] = "discord"
        r, save, _s, _c = self.apply(data=d)
        saved = save.call_args[0][0]
        self.assertEqual(micgen.effective(saved)["kompresor"]["telafi"], 8.0)
        self.assertEqual(saved["mod"]["discord"]["kompresor"]["telafi"], 8.0)

    def test_invalid(self):
        for bad in ({}, {"genel.aktif": False}, {"kompresor": 1}, {"kompresor.yok": 1.0},
                    {"kompresor.esik": 50.0}):
            with self.subTest(bad=bad):
                r, save, setl, conf = self.apply(bad)
                self.assertIn("hata", r)
                save.assert_not_called()
                setl.assert_not_called()
                self.assertFalse(conf.exists())

    def test_calibrate_error_codes(self):
        with mock.patch.object(micgen, "load_mic", return_value=shipped()), \
                mock.patch.object(micgen, "resolve_target", return_value=None):
            self.assertEqual(micgen.calibrate(1.0)["kod"], "mikrofon_yok")
        cal = CalibrateTest()
        r, _, _ = cal.run_cal(synth(None, -70.0))
        self.assertEqual(r["kod"], "sessiz")
        r, _, _ = cal.run_cal(synth(-20.0, -70.0, clip=True))
        self.assertEqual(r["kod"], "kirpma")

    def run_main(self, result=None, exc=None):
        import contextlib
        import io
        import json
        buf = io.StringIO()
        cal = mock.Mock(return_value=result, side_effect=exc)
        with mock.patch.object(micgen, "load_mic", return_value=shipped()), \
                mock.patch.object(micgen, "calibrate", cal), \
                mock.patch.object(micgen.logging, "basicConfig"), \
                contextlib.redirect_stdout(buf):
            rc = micgen.main(["kalibre", "--json", "--saniye", "10"])
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)                     # exactly one JSON line
        return rc, json.loads(lines[0]), cal

    def test_main_json(self):
        ok = {"konusma_dbfs": -24.0, "oneri": dict(self.ONERI), "uygulandi": False}
        rc, out, cal = self.run_main(ok)
        self.assertEqual((rc, out), (0, ok))
        cal.assert_called_once_with(10.0, apply=False)
        rc, out, _ = self.run_main({"hata": "Konuşma algılanmadı", "kod": "sessiz"})
        self.assertEqual((rc, out["kod"]), (1, "sessiz"))
        rc, out, _ = self.run_main(exc=OSError("parec yok"))
        self.assertEqual(rc, 1)
        self.assertEqual(out["kod"], "kayit")
        self.assertIn("parec yok", out["hata"])


def _live_ok() -> bool:
    if not all(shutil.which(x) for x in ("pactl", "pipewire", "pw-play", "parec", "pw-metadata")):
        return False
    try:
        return subprocess.run(["pactl", "info"], env=config.C_ENV, capture_output=True,
                              timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(_live_ok(), "pipewire/pactl yok")
class LiveCheckTest(unittest.TestCase):
    def test_check_with_test_instances(self):
        lines: list[str] = []
        ok = micgen.check(say=lines.append, real_mic=False)
        self.assertTrue(ok, "\n".join(lines))
        self.assertNotIn(micgen.TEST_PREFIX, micgen.list_source_names())



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_error(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"{alan}: true/false bekleniyordu, '{deger}' bulundu":
                            "{alan}: true/false erwartet, '{deger}' gefunden"})
        with self.assertRaisesRegex(micgen.MicError, "^genel.aktif: true/false erwartet, '1' gefunden$"):
            micgen._bool(1, "genel.aktif")

    def test_mikrofon_test_machine_keys_stay_fixed(self):
        """app.MicTestDialog parses "Ham"/"İşlenmiş"/"çalınıyor": fixed when piped."""
        from unittest import mock
        from oto_eq import i18n, mikrofon_test as mt
        i18n.sifirla("de", {"Ham": "Roh", "🔊 ham ses çalınıyor…": "🔊 Roh wird abgespielt…"})
        with mock.patch.dict("os.environ", {"OTO_EQ_MAKINE": "1"}):
            self.assertTrue(mt._makine())
        self.assertEqual(mt._metin(mt.SATIR["ham"], True), "Ham")
        self.assertEqual(mt._metin(mt.CALINIYOR["ham"], True), "🔊 ham ses çalınıyor…")
        with mock.patch.dict("os.environ", {"OTO_EQ_MAKINE": "0"}):
            self.assertFalse(mt._makine())
        self.assertEqual(mt._metin(mt.SATIR["ham"], False), "Roh")


if __name__ == "__main__":
    unittest.main()
