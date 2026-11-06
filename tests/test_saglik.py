"""saglik (health check) tests with a fake, in-memory system."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from oto_eq import config, saglik as S

HS80 = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver-00.analog-stereo"
HS80_IN = "alsa_input.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver-00.mono-fallback"
SPK = "alsa_output.pci-0000_15_00.6.analog-stereo"
OURS = ["oto_eq_cs2", "oto_eq_oyun", "oto_eq_muzik", "oto_eq_film", "oto_eq_duz"]
MODEL_DIR = Path("/fake/data/oto-eq/models")
BT = "bluez_output.AC_80_0A_11_22_33.1"


def sink_info(name, desc, bus, port=None, ports=(), **props):
    """One ``pactl -f json list sinks`` item (as on the live system, C locale)."""
    p = {"device.bus": bus, "device.api": "bluez5" if bus == "bluetooth" else "alsa",
         "device.description": desc, "device.product.name": desc, **props}
    return {"name": name, "description": desc, "properties": p, "active_port": port,
            "ports": [{"name": n, "type": t} for n, t in ports]}


SINK_INFOS = [
    sink_info(HS80, "CORSAIR HS80 RGB Wireless Gaming Receiver", "usb",
              "analog-output-headphones", [("analog-output-headphones", "Headphones")]),
    sink_info(SPK, "Ryzen HD Audio Controller", "pci", "analog-output-lineout",
              [("analog-output-lineout", "Line"), ("analog-output-headphones", "Headphones")]),
    sink_info(BT, "WH-1000XM4", "bluetooth", **{"device.form_factor": "headphone",
                                                 "api.bluez5.profile": "a2dp-sink"}),
]
LIMITER_LINE = '{ type = lv2 name = lim plugin = "http://lsp-plug.in/plugins/lv2/limiter_stereo" }'


def unit(load="loaded", active="active", enabled="enabled", restarts=0, pid=500):
    return (f"LoadState={load}\nActiveState={active}\nSubState=running\n"
            f"UnitFileState={enabled}\nNRestarts={restarts}\nMainPID={pid}\n")


def dump(link_to=HS80, sinks=OURS):
    objs, nid = [], 10
    ids = {}
    for n in [HS80, SPK, BT] + list(sinks) + [s + "_out" for s in sinks]:
        objs.append({"id": nid, "type": "PipeWire:Interface:Node",
                     "info": {"props": {"node.name": n}}})
        ids[n] = nid
        nid += 1
    for s in sinks:
        tgt = link_to(s) if callable(link_to) else link_to
        if tgt:
            objs.append({"id": nid, "type": "PipeWire:Interface:Link",
                         "info": {"output-node-id": ids[s + "_out"],
                                  "input-node-id": ids[tgt]}})
            nid += 1
    return json.dumps(objs)


PWTOP = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
C   42      0      0    ---     ---   ---   ---     0                  {hs}
S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R   42    256  48000  24.7us   7.1us  0.00  0.00    {err}    S24LE 2 48000 {hs}
R   93    256  48000   9.3us  10.9us  0.00  0.00    0    F32LE 2 48000  + game.exe
"""


class FakeEnv(S.Env):
    clk_tck = 100

    def __init__(self):
        self.units = {u: unit() for u in S.UNITS}
        self.sinks = [HS80, SPK] + OURS
        self.default_sink, self.default_source = HS80, HS80_IN
        self.dump_json = dump()
        self.status = {"ok": True, "active": True, "default_sink": HS80, "mode": "auto",
                       "fixed_profile": None, "dominant": "cs2", "sinks_ready": True,
                       "streams": [{"index": 1, "app": "cs2", "binary": "cs2", "category": "cs2",
                                    "profile": "cs2", "reason": "ikili", "sink": "oto_eq_cs2"}],
                       "kisma": {"acik": True, "sesli": [], "olcum": ""}}
        self.files: dict[str, str] = {}
        self.mtimes = {str(config.SINKS_CONF): 200.0, str(S.MIC_CONF): 200.0,
                       "SRC:" + config.PROFILES_FILE: 100.0, "SRC:" + S.MIC_FILE: 100.0}
        self.existing = {str(Path("/usr/lib/lv2") / S.LSP_TTL),
                         str(Path("/usr/lib/lv2") / S.LIMITER_TTL),
                         str(Path("/usr/lib/ladspa") / S.RNNOISE_SO)}
        self.existing |= {str(MODEL_DIR / f) for f in S.MODEL_FILES + (S.YAMNET_FILE,)}
        self.modules = {"onnxruntime", "oto_eq.ses_model", "oto_eq.kisma"}
        self.attrs = {("oto_eq.ses_model", "MODEL_DIR"): MODEL_DIR}
        self.outdated = False
        self.wall = 1_000_000.0
        self.usb = {"1-4": ("1b1c", "0a6b"), "1-1": ("046d", "c52b")}
        self.ticks = {500: [1000, 1005], 501: [2000, 2002]}
        self.pwtop = PWTOP.format(hs=HS80, err=0)
        self.t = 0.0
        self.commands = []
        self.sink_infos = [dict(x) for x in SINK_INFOS]

    def run(self, cmd, timeout=5.0):
        self.commands.append(cmd)
        if cmd[:3] == ["systemctl", "--user", "show"]:
            u = self.units.get(cmd[3])
            return (0, u, "") if u is not None else (1, "", "hata")
        if cmd == ["pactl", "list", "short", "sinks"]:
            if self.sinks is None:
                return 1, "", "Connection failure"
            return 0, "".join(f"{i}\t{n}\tPipeWire\tfloat32le 2ch 48000Hz\tIDLE\n"
                              for i, n in enumerate(self.sinks)), ""
        if cmd == ["pactl", "-f", "json", "list", "sinks"]:
            if self.sinks is None:
                return 1, "", "Connection failure"
            return 0, json.dumps(self.sink_infos), ""
        if cmd == ["pactl", "get-default-sink"]:
            return 0, self.default_sink + "\n", ""
        if cmd == ["pactl", "get-default-source"]:
            return 0, self.default_source + "\n", ""
        if cmd == ["pw-dump"]:
            return 0, self.dump_json, ""
        if cmd[0] == "pw-top":
            return 0, self.pwtop, ""
        return 127, "", "yok"

    def read_text(self, path):
        path = str(path)
        if path in self.files:
            return self.files[path]
        if path.startswith("/sys/bus/usb/devices/"):
            dev, attr = path.split("/")[-2:]
            if dev in self.usb:
                return self.usb[dev][0 if attr == "idVendor" else 1] + "\n"
            return None
        if path.startswith("/proc/") and path.endswith("/stat"):
            pid = int(path.split("/")[2])
            t = self.ticks.get(pid)
            if not t:
                return None
            v = t.pop(0) if len(t) > 1 else t[0]
            return f"{pid} (pipewire) S 1 1 1 0 -1 0 0 0 0 0 {v} 0 0 0 20 0"
        return None

    def exists(self, path):
        return str(Path(path).expanduser()) in self.existing

    def mtime(self, path):
        return self.mtimes.get(str(path))

    def config_path(self, name):
        return "SRC:" + name

    def listdir(self, path):
        return sorted(self.usb) if str(path) == "/sys/bus/usb/devices" else []

    def ipc_status(self):
        if self.status is None:
            raise FileNotFoundError("soket yok")
        return self.status

    def rules(self):
        return {"genel": {"kulaklik_desen": "(?i)hs80"}}

    def monotonic(self):
        return self.t

    def time(self):
        return self.wall

    def module_available(self, name):
        return name in self.modules

    def attr(self, module, name):
        return self.attrs.get((module, name))

    def conf_outdated(self):
        return self.outdated

    def sleep(self, s):
        self.t += s


def by_name(results):
    return {r["ad"]: r for r in results}


class TestHealthy(unittest.TestCase):
    def test_all_ok(self):
        env = FakeEnv()
        res = S.run_checks(env)
        bad = [r for r in res if r["durum"] != S.TAMAM]
        self.assertEqual(bad, [], bad)
        r = by_name(res)
        self.assertIn("kablosuz", r["HS80"]["ayrinti"])
        self.assertIn("%5,0", r["işlemci kullanımı"]["ayrinti"])
        self.assertIn("baskın: cs2", r["yönlendirici"]["ayrinti"])
        for x in res:
            self.assertEqual(set(x), {"ad", "durum", "ayrinti", "oneri"})

    def test_read_only_commands(self):
        env = FakeEnv()
        S.run_checks(env)
        allowed = {("systemctl", "--user", "show"), ("pactl", "list", "short"),
                   ("pactl", "-f", "json", "list", "sinks"),
                   ("pactl", "get-default-sink"), ("pactl", "get-default-source"),
                   ("pw-dump",), ("pw-top", "-b", "-n")}
        for cmd in env.commands:
            self.assertTrue(any(tuple(cmd[:len(a)]) == a for a in allowed), cmd)

    def test_mic_source_is_fine_as_default(self):
        env = FakeEnv()
        env.default_source = "oto_eq_mikrofon"
        r = by_name(S.run_checks(env))
        self.assertEqual(r["varsayılanlar"]["durum"], S.TAMAM)
        self.assertIn("işlenmiş mikrofon", r["varsayılanlar"]["ayrinti"])

    def test_hs80_takip_field_shown(self):
        env = FakeEnv()
        env.status["hs80_takip_son"] = "ses %40"
        self.assertIn("HS80 takibi: ses %40", by_name(S.run_checks(env))["yönlendirici"]["ayrinti"])


class TestPreInstall(unittest.TestCase):
    def test_everything_missing(self):
        env = FakeEnv()
        env.units = {u: unit(load="not-found", active="inactive", enabled="", pid=0)
                     for u in S.UNITS}
        env.sinks = [HS80, SPK]
        env.dump_json = dump(sinks=[])
        env.status = None
        env.mtimes = {}
        res = S.run_checks(env)
        r = by_name(res)
        self.assertEqual(r["servis oto-eq"]["durum"], S.HATA)
        self.assertEqual(r["servis oto-eq"]["oneri"], "./install.sh")
        self.assertEqual(r["EQ çıkışları"]["oneri"], "./install.sh")
        self.assertEqual(r["yönlendirici"]["oneri"], "./install.sh")
        self.assertEqual(r["EQ ayar dosyası"]["durum"], S.HATA)
        self.assertEqual(r["mikrofon ayar dosyası"]["durum"], S.UYARI)
        self.assertEqual(r["işlemci kullanımı"]["durum"], S.UYARI)
        self.assertNotIn("akışlar", r)
        text = S.format_report(res)
        self.assertIn("kurulum yapılmamış görünüyor: ./install.sh", text)

    def test_no_sound_server(self):
        env = FakeEnv()
        env.sinks = None
        r = by_name(S.run_checks(env))
        self.assertEqual(r["EQ çıkışları"]["durum"], S.HATA)
        self.assertIn("pactl", r["EQ çıkışları"]["ayrinti"])


class TestProblems(unittest.TestCase):
    def test_units(self):
        env = FakeEnv()
        env.units["oto-eq.service"] = unit(active="failed")
        env.units["oto-eq-sinks.service"] = unit(restarts=3)
        env.units["oto-eq-mic.service"] = unit(enabled="disabled")
        r = by_name(S.run_checks(env))
        self.assertEqual(r["servis oto-eq"]["durum"], S.HATA)
        self.assertIn("journalctl", r["servis oto-eq"]["oneri"])
        self.assertEqual(r["servis oto-eq-sinks"]["durum"], S.UYARI)
        self.assertIn("3 kez", r["servis oto-eq-sinks"]["ayrinti"])
        self.assertIn("enable", r["servis oto-eq-mic"]["oneri"])

    def test_missing_sink_and_wrong_links(self):
        env = FakeEnv()
        env.sinks = [HS80, SPK] + OURS[:4]
        env.dump_json = dump(link_to=lambda s: SPK if s == "oto_eq_cs2" else
                             (None if s == "oto_eq_oyun" else HS80), sinks=OURS[:4])
        env.status["sinks_ready"] = False
        r = by_name(S.run_checks(env))
        self.assertEqual(r["EQ çıkışları"]["durum"], S.HATA)
        self.assertIn("oto_eq_duz", r["EQ çıkışları"]["ayrinti"])
        self.assertEqual(r["çıkış bağlantıları"]["durum"], S.HATA)
        self.assertIn("oto_eq_cs2_out →", r["çıkış bağlantıları"]["ayrinti"])
        self.assertEqual(r["yönlendirici çıkışları"]["durum"], S.HATA)

    def test_unlinked_only_is_warning(self):
        env = FakeEnv()
        env.dump_json = dump(link_to=None)
        r = by_name(S.run_checks(env))
        self.assertEqual(r["çıkış bağlantıları"]["durum"], S.UYARI)

    def test_our_sink_as_default(self):
        env = FakeEnv()
        env.default_sink = "oto_eq_film"
        env.status["active"] = False
        r = by_name(S.run_checks(env))
        self.assertEqual(r["varsayılan çıkış"]["durum"], S.HATA)
        env = FakeEnv()
        env.default_source = "oto_eq_cs2.monitor"
        self.assertEqual(by_name(S.run_checks(env))["varsayılan giriş"]["durum"], S.HATA)

    def test_gating_mismatch(self):
        env = FakeEnv()
        env.default_sink = SPK          # speakers default, but router claims active
        r = by_name(S.run_checks(env))
        self.assertEqual(r["yönlendirici"]["durum"], S.UYARI)
        env = FakeEnv()
        env.default_sink = SPK
        env.status["active"] = False
        env.status["default_sink"] = SPK
        r = by_name(S.run_checks(env))
        self.assertEqual(r["yönlendirici"]["durum"], S.TAMAM)
        self.assertEqual(r["akışlar"]["durum"], S.TAMAM)

    def test_raw_streams_listed(self):
        env = FakeEnv()
        env.status["streams"].append({"index": 7, "app": "Firefox", "binary": "firefox",
                                      "category": "film", "profile": None,
                                      "reason": "elle taşınmış", "sink": HS80})
        r = by_name(S.run_checks(env))
        self.assertEqual(r["akışlar"]["durum"], S.UYARI)
        self.assertIn("#7 Firefox (film: elle taşınmış)", r["akışlar"]["ayrinti"])

    def test_stale_configs(self):
        env = FakeEnv()
        env.mtimes["SRC:" + config.PROFILES_FILE] = 300.0
        env.mtimes["SRC:" + S.MIC_FILE] = 300.0
        r = by_name(S.run_checks(env))
        self.assertEqual(r["EQ ayar dosyası"]["durum"], S.UYARI)
        self.assertEqual(r["EQ ayar dosyası"]["oneri"], "kulak eq-uret")
        self.assertEqual(r["mikrofon ayar dosyası"]["durum"], S.UYARI)

    def test_plugins_missing(self):
        env = FakeEnv()
        env.existing = set()
        r = by_name(S.run_checks(env))
        self.assertEqual(r["LSP eklentisi"]["durum"], S.UYARI)
        self.assertIn("lsp-plugins-lv2", r["LSP eklentisi"]["oneri"])
        self.assertEqual(r["RNNoise eklentisi"]["durum"], S.UYARI)

    def test_wireplumber_state(self):
        env = FakeEnv()
        env.files[str(S._wp_state_file())] = "\n".join([
            "[stream-properties]",
            'Output/Audio:application.name:Spotify={"volume":1.0, "target":"oto_eq_muzik"}',
            'Output/Audio:application.id:com.x={"target":"alsa_output.x"}',
            'Audio/Sink:node.name:oto_eq_test_null={"volume":1.0}',
            'Audio/Sink:node.name:a3_repro_a_123456={"volume":1.0}',
            'Audio/Sink:media.name:a4\\sbqtest\\sinput={"volume":1.0}',
            'Audio/Sink:node.name:auto_null={"volume":1.0}',
        ])
        r = by_name(S.run_checks(env))
        self.assertIn("1 uygulama", r["WP hatırlanan hedefler"]["ayrinti"])
        self.assertIn("Spotify", r["WP hatırlanan hedefler"]["ayrinti"])
        self.assertEqual(r["WP test kalıntıları"]["durum"], S.UYARI)
        self.assertIn("3 test kaydı", r["WP test kalıntıları"]["ayrinti"])
        self.assertIn("a4 bqtest input", r["WP test kalıntıları"]["ayrinti"])

    def test_cpu_high_and_xruns(self):
        env = FakeEnv()
        env.ticks = {500: [1000, 1030], 501: [2000, 2000]}
        env.pwtop = PWTOP.format(hs=HS80, err=4)
        r = by_name(S.run_checks(env))
        self.assertEqual(r["işlemci kullanımı"]["durum"], S.UYARI)
        self.assertIn("%30,0", r["işlemci kullanımı"]["ayrinti"])
        self.assertEqual(r["xrun (ses kesintisi)"]["durum"], S.UYARI)
        self.assertIn("HS80 (+4)", r["xrun (ses kesintisi)"]["ayrinti"])   # rose 0 → 4

    def test_old_xrun_counter_alone_is_not_a_warning(self):
        env = FakeEnv()
        env.pwtop = PWTOP.replace("     0                  {hs}", "     4                  {hs}"
                                  ).format(hs=HS80, err=4)       # 4 before and after
        r = by_name(S.run_checks(env))["xrun (ses kesintisi)"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertIn("eski sayaç", r["ayrinti"])

    def test_hs80_missing_and_wired(self):
        env = FakeEnv()
        env.usb = {}
        self.assertEqual(by_name(S.run_checks(env))["HS80"]["durum"], S.HATA)
        env = FakeEnv()
        env.usb = {"3-2": ("1b1c", "0a69")}
        self.assertIn("kablolu", by_name(S.run_checks(env))["HS80"]["ayrinti"])

    def test_crashing_check_contained(self):
        env = FakeEnv()
        with mock.patch.object(S, "CHECKS", (lambda fx: 1 / 0, S.check_hs80)):
            res = S.run_checks(env)
        self.assertEqual(res[0]["durum"], S.HATA)
        self.assertIn("ZeroDivisionError", res[0]["ayrinti"])
        self.assertEqual(res[1]["ad"], "HS80")


class TestKulaklik(unittest.TestCase):
    """CHANGE 7: the default output's type (USB / Bluetooth / 3,5 mm headphone or not)."""

    def bt_env(self, profile="a2dp-sink"):
        env = FakeEnv()
        env.usb = {"1-1": ("046d", "c52b")}             # no HS80 on USB
        env.sinks = [SPK, BT] + OURS
        env.sink_infos = [x for x in env.sink_infos if x["name"] != HS80]
        env.sink_infos[-1]["properties"]["api.bluez5.profile"] = profile
        env.default_sink = env.status["default_sink"] = BT
        env.dump_json = dump(link_to=BT)
        return env

    def test_hs80_usb_headphone(self):
        r = by_name(S.run_checks(FakeEnv()))["kulaklık"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertEqual(r["ayrinti"], "USB kulaklık: CORSAIR HS80 RGB Wireless Gaming Receiver "
                                       "(HS80 düzeltmesi açık)")

    def test_speaker_default_warns(self):
        env = FakeEnv()
        env.default_sink = env.status["default_sink"] = SPK
        env.status["active"] = False
        r = by_name(S.run_checks(env))
        self.assertEqual(r["kulaklık"]["durum"], S.UYARI)
        self.assertIn("varsayılan çıkış kulaklık değil: Hoparlör (Ryzen HD Audio Controller; "
                      "hat çıkışı portu etkin) — EQ devre dışı", r["kulaklık"]["ayrinti"])
        self.assertIn("kulaklik_desen", r["kulaklık"]["oneri"])
        self.assertEqual(r["yönlendirici"]["durum"], S.TAMAM)    # router agrees: inactive
        self.assertEqual(r["HS80"]["durum"], S.TAMAM)            # still plugged in

    def test_other_headphone_without_hs80(self):
        env = self.bt_env()
        r = by_name(S.run_checks(env))
        self.assertEqual(r["HS80"]["durum"], S.TAMAM)
        self.assertIn("takılı değil; şu an Bluetooth kulaklık kullanılıyor: WH-1000XM4",
                      r["HS80"]["ayrinti"])
        self.assertEqual(r["kulaklık"]["durum"], S.TAMAM)
        self.assertEqual(r["kulaklık"]["ayrinti"],
                         "Bluetooth kulaklık: WH-1000XM4 (HS80 düzeltmesi nötr)")
        # the router is active on a non-HS80 headphone: no gating mismatch any more
        self.assertEqual(r["yönlendirici"]["durum"], S.TAMAM)
        self.assertEqual(r["çıkış bağlantıları"]["durum"], S.TAMAM)

    def test_no_headphone_and_no_hs80_is_a_warning(self):
        # speakers chosen as the default output: nothing is broken (EQ idles by design),
        # so not a red error / exit 1 any more (1 Kas 2026); still flagged
        env = self.bt_env()
        env.default_sink = env.status["default_sink"] = SPK
        env.status["active"] = False
        r = by_name(S.run_checks(env))
        self.assertEqual(r["HS80"]["durum"], S.UYARI)
        self.assertEqual(r["kulaklık"]["durum"], S.UYARI)

    def test_bluetooth_call_mode(self):
        r = by_name(S.run_checks(self.bt_env("headset-head-unit")))["kulaklık"]
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("arama modunda (headset-head-unit)", r["ayrinti"])
        self.assertIn("A2DP", r["oneri"])

    def test_correction_pattern_from_profiles(self):
        env = self.bt_env()
        env.files["SRC:" + config.PROFILES_FILE] = '[duzeltme]\ncihaz_desen = "(?i)wh-1000"\n'
        r = by_name(S.run_checks(env))["kulaklık"]
        self.assertIn("HS80 düzeltmesi açık", r["ayrinti"])

    def test_gating_uses_detection_not_pattern(self):
        env = self.bt_env()
        env.status["active"] = False                # an old router (HS80 regex only)
        r = by_name(S.run_checks(env))["yönlendirici"]
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("varsayılan çıkış kulaklık", r["ayrinti"])

    def test_no_pactl_json_falls_back_to_name(self):
        env = FakeEnv()
        env.sink_infos = "bozuk"
        res = by_name(S.run_checks(env))
        self.assertEqual(res["kulaklık"]["durum"], S.TAMAM)    # kulaklik_desen still matches
        self.assertEqual(res["yönlendirici"]["durum"], S.TAMAM)

    def test_skipped_when_our_sink_is_default_or_no_server(self):
        env = FakeEnv()
        env.default_sink = "oto_eq_film"
        env.status["active"] = False
        self.assertNotIn("kulaklık", by_name(S.run_checks(env)))
        env = FakeEnv()
        env.sinks = None
        self.assertNotIn("kulaklık", by_name(S.run_checks(env)))


class TestChange6Parts(unittest.TestCase):
    """kişisel ton, ses modeli, EQ yapısı, kısma, limiter, otomatik onarım (read-only)."""

    def kis(self, env, text):
        env.files[str(config.CONFIG_DIR / S.KISISEL_FILE)] = text

    def test_kisisel(self):
        env = FakeEnv()
        self.assertIn("0 dB", by_name(S.run_checks(env))["kişisel ton"]["ayrinti"])
        self.kis(env, json.dumps({"genel": {"bas": 1.5, "vokal": 0, "parlaklik": -0.5},
                                  "tur": {"muzik:metal": {"bas": 1}}}))
        r = by_name(S.run_checks(env))["kişisel ton"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertIn("bas +1.5 dB, parlaklık -0.5 dB", r["ayrinti"])
        self.assertIn("1 türe özel", r["ayrinti"])
        self.kis(env, "{bozuk")
        r = by_name(S.run_checks(env))["kişisel ton"]
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("JSON hatası", r["ayrinti"])
        self.kis(env, "[1]")
        self.assertEqual(by_name(S.run_checks(env))["kişisel ton"]["durum"], S.UYARI)
        self.kis(env, '{"genel": {"tiz": 2}}')
        r = by_name(S.run_checks(env))["kişisel ton"]
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("tiz", r["ayrinti"])

    def test_model(self):
        env = FakeEnv()
        self.assertIn("YAMNet", by_name(S.run_checks(env))["ses analizi modeli"]["ayrinti"])
        env.existing.discard(str(MODEL_DIR / S.YAMNET_FILE))
        r = by_name(S.run_checks(env))["ses analizi modeli"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertIn("YAMNet yok", r["ayrinti"])
        env.existing.discard(str(MODEL_DIR / S.MODEL_FILES[0]))
        r = by_name(S.run_checks(env))["ses analizi modeli"]
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("docs/MODELS.md", r["oneri"])
        env.modules.discard("onnxruntime")
        r = by_name(S.run_checks(env))["ses analizi modeli"]
        self.assertIn("onnxruntime", r["ayrinti"])
        env.modules.discard("oto_eq.ses_model")
        self.assertNotIn("ses analizi modeli", by_name(S.run_checks(env)))

    def test_conf_structure(self):
        env = FakeEnv()
        self.assertEqual(by_name(S.run_checks(env))["EQ yapısı"]["durum"], S.TAMAM)
        env.outdated = True
        r = by_name(S.run_checks(env))["EQ yapısı"]
        self.assertEqual((r["durum"], r["oneri"]), (S.UYARI, "kulak eq-uret"))
        env.outdated = None                     # eqgen without conf_outdated
        self.assertNotIn("EQ yapısı", by_name(S.run_checks(env)))

    def test_kisma_on_but_not_running_in_the_router(self):
        env = FakeEnv()
        del env.status["kisma"]                  # router started without a ducker
        r = by_name(S.run_checks(env))["sohbette kısma"]
        self.assertEqual(r["durum"], S.UYARI)
        env.status["kisma"] = {"hata": "pactl yok"}
        self.assertIn("pactl yok", by_name(S.run_checks(env))["sohbette kısma"]["ayrinti"])
        env.status = None                        # router down: other checks report that
        self.assertEqual(by_name(S.run_checks(env))["sohbette kısma"]["durum"], S.TAMAM)

    def test_kisma(self):
        env = FakeEnv()
        path = str(config.CONFIG_DIR / S.KISMA_FILE)
        self.assertIn("varsayılan", by_name(S.run_checks(env))["sohbette kısma"]["ayrinti"])
        env.files[path] = json.dumps({"acik": True, "db": 9, "profiller": ["muzik"]})
        self.assertIn("−9 dB (muzik)", by_name(S.run_checks(env))["sohbette kısma"]["ayrinti"])
        env.attrs[("oto_eq.kisma", "normalize_settings")] = lambda d: {**d, "db": 24.0}
        self.assertIn("−24 dB", by_name(S.run_checks(env))["sohbette kısma"]["ayrinti"])
        env.files[path] = json.dumps({"acik": False})
        self.assertEqual(by_name(S.run_checks(env))["sohbette kısma"]["ayrinti"], "kapalı")
        env.files[path] = "{x"
        self.assertEqual(by_name(S.run_checks(env))["sohbette kısma"]["durum"], S.UYARI)
        env.modules.discard("oto_eq.kisma")
        self.assertNotIn("sohbette kısma", by_name(S.run_checks(env)))

    def test_limiter(self):
        env = FakeEnv()
        self.assertNotIn("limiter", by_name(S.run_checks(env)))         # no conf text
        env.files[str(config.SINKS_CONF)] = "\n".join([LIMITER_LINE] * 4)
        r = by_name(S.run_checks(env))["limiter"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertIn("4 zincirde", r["ayrinti"])
        env.existing.discard(str(Path("/usr/lib/lv2") / S.LIMITER_TTL))
        self.assertEqual(by_name(S.run_checks(env))["limiter"]["durum"], S.HATA)
        env.files[str(config.SINKS_CONF)] = "limiter yok"
        self.assertNotIn("limiter", by_name(S.run_checks(env)))
        env.attrs[("oto_eq.eqgen", "LSP_LIMITER_URI")] = "http://x/limiter_stereo"
        self.assertEqual(by_name(S.run_checks(env))["limiter"]["durum"], S.UYARI)

    def test_onarim_history(self):
        env = FakeEnv()
        path = str(S.ONARIM_FILE)
        self.assertNotIn("otomatik onarım", by_name(S.run_checks(env)))
        w = env.wall
        env.files[path] = json.dumps({"son": [{"zaman": w - 90000, "tur": "tara", "neden": "eski"}]})
        self.assertIn("gerekmedi", by_name(S.run_checks(env))["otomatik onarım"]["ayrinti"])
        env.files[path] = json.dumps({"son": [
            {"zaman": w - 600, "tur": "servis_yeniden_baslat", "neden": "EQ çıkışları eksik",
             "sonuc": "oto-eq-sinks.service yeniden başlatıldı"},
            {"zaman": w - 60, "tur": "bildirim", "neden": "deneme", "sonuc": "x", "deneme": True}]})
        r = by_name(S.run_checks(env))["otomatik onarım"]
        self.assertEqual(r["durum"], S.TAMAM)
        self.assertIn("1 eylem; son: EQ çıkışları eksik (10 dk önce)", r["ayrinti"])
        env.files[path] = json.dumps({"son": [
            {"zaman": w - 60, "tur": "servis_yeniden_baslat", "neden": "eksik",
             "sonuc": "oto-eq-sinks.service yeniden başlatılamadı: x"}]})
        self.assertEqual(by_name(S.run_checks(env))["otomatik onarım"]["durum"], S.UYARI)
        env.files[path] = "bozuk"
        self.assertNotIn("otomatik onarım", by_name(S.run_checks(env)))

    def test_env_helpers_are_safe(self):
        env = S.Env()
        self.assertFalse(env.module_available("oto_eq_yok_boyle_modul"))
        self.assertIsNone(env.attr("oto_eq_yok_boyle_modul", "x"))
        self.assertIsNone(env.attr("oto_eq.config", "YOK_BOYLE"))
        self.assertIn(env.conf_outdated(), (True, False, None))


class TestOutput(unittest.TestCase):
    RES = [S._r("HS80", S.TAMAM, "takılı"), S._r("servis oto-eq", S.UYARI, "x", "yap"),
           S._r("yönlendirici", S.HATA, "y", "./install.sh")]

    def test_format(self):
        text = S.format_report(self.RES)
        self.assertIn("✔ HS80           takılı", text)
        self.assertIn("⚠ servis oto-eq  x", text)
        self.assertIn("→ yap", text)
        self.assertIn("Özet: 1 tamam, 1 uyarı, 1 hata", text)
        self.assertNotIn("\033[", text)
        self.assertIn("\033[31m✘", S.format_report(self.RES, color=True))

    def test_all_good_summary(self):
        self.assertIn("her şey yolunda", S.format_report(self.RES[:1]))

    def test_main_json_and_exit(self):
        with mock.patch.object(S, "run_checks", return_value=self.RES):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = S.main(["--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(buf.getvalue()), self.RES)
        with mock.patch.object(S, "run_checks", return_value=self.RES[:2]):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(S.main([]), 0)


class TestI18n(unittest.TestCase):
    """CHANGE 8: a forced catalog reaches the check texts; durum stays machine-readable."""

    def test_forced_catalog(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"servis {ad}": "Dienst {ad}", "kurulu değil": "nicht installiert",
                            "kulaklık": "Kopfhörer",
                            "varsayılan çıkış yok": "keine Standardausgabe",
                            "Özet: {tamam} tamam, {uyari} uyarı, {hata} hata":
                                "Zusammenfassung: {tamam} ok, {uyari} Warnungen, {hata} Fehler"})
        self.addCleanup(i18n.sifirla)
        env = FakeEnv()
        env.units[config.DAEMON_SERVICE] = unit(load="not-found", active="inactive")
        env.default_sink = ""
        res = S.run_checks(env)
        r = by_name(res)
        self.assertEqual(r["Dienst oto-eq"]["ayrinti"], "nicht installiert")
        self.assertEqual(r["Dienst oto-eq"]["durum"], S.HATA)          # never translated
        self.assertEqual(r["Dienst oto-eq"]["oneri"], S.INSTALL_HINT)  # command stays
        self.assertEqual(r["Kopfhörer"]["ayrinti"], "keine Standardausgabe")
        text = S.format_report(res)
        self.assertIn("Zusammenfassung: ", text)
        self.assertIn("— kurulum yapılmamış görünüyor: ./install.sh", text)  # untranslated → TR


GRAF_CONF = """# oto-eq yapı sürümü: 7
context.modules = [
    { name = libpipewire-module-filter-chain
        args = {
            filter.graph = {
                nodes = [
                    { type = builtin name = pre_l label = linear control = { "Mult" = 0.5 "Add" = 0.0 } }
                    { type = builtin name = k1_l label = bq_lowshelf control = { "Freq" = 120.0 "Q" = 0.7 "Gain" = 0.0 } }
                    { type = lv2 name = lim plugin = "http://lsp-plug.in/plugins/lv2/limiter_stereo" control = { "enabled" = 1.0 } }
                    { type = builtin name = kopya label = copy }
                ]
                links = [ { output = "pre_l:Out" input = "k1_l:In" } ]
            }
            capture.props = {
                node.name        = "oto_eq_muzik"
                media.class      = Audio/Sink
                audio.position   = [ FL FR ]
                priority.session = 1
                state.restore-props = false
                oto_eq.profile   = "muzik"
            }
            playback.props = {
                node.name        = "oto_eq_muzik_out"
                node.passive     = true
                state.restore-props = false
            }
        }
    }
]
"""


def graf_dump(kontrol=("pre_l", "k1_l", "lim"), cap_props=None, out_props=None):
    cap = {"node.name": "oto_eq_muzik", "media.class": "Audio/Sink", "priority.session": 1,
           "state.restore-props": False, "oto_eq.profile": "muzik"}
    out = {"node.name": "oto_eq_muzik_out", "node.passive": True, "state.restore-props": False}
    cap.update(cap_props or {})
    out.update(out_props or {})
    params = []
    for n in kontrol:
        params += [f"{n}:Gain", 0.0]
    return [{"id": 1, "type": "PipeWire:Interface:Node",
             "info": {"props": {k: v for k, v in cap.items() if v is not None},
                      "params": {"Props": [{"params": ["channelmix.normalize", False]},
                                           {"params": params}]}}},
            {"id": 2, "type": "PipeWire:Interface:Node",
             "info": {"props": {k: v for k, v in out.items() if v is not None},
                      "params": {"Props": [{"params": ["channelmix.normalize", False]}]}}}]


class GrafEnv(FakeEnv):
    def __init__(self, live, start=None):
        super().__init__()
        self.files[str(config.SINKS_CONF)] = GRAF_CONF
        self.dump_json = json.dumps(live)
        self.start = start

    def run(self, cmd, timeout=5.0):
        if "--timestamp=unix" in cmd:
            self.commands.append(cmd)
            return (0, f"ActiveEnterTimestamp=@{self.start}\n" if self.start else
                    "ActiveEnterTimestamp=\n", "")
        return super().run(cmd, timeout)


class TestCalisanGraf(unittest.TestCase):
    def check(self, env):
        return S.check_calisan_graf(S.Facts(env))

    def test_parse_conf(self):
        g = S.conf_graflari(GRAF_CONF)["oto_eq_muzik"]
        self.assertEqual(g["dugumler"], {"pre_l", "k1_l", "lim", "kopya"})
        self.assertEqual(g["kontrollu"], {"pre_l", "k1_l", "lim"})
        self.assertEqual(g["props"]["oto_eq_muzik"]["state.restore-props"], "false")
        self.assertEqual(g["props"]["oto_eq_muzik_out"], {"node.passive": "true",
                                                          "state.restore-props": "false"})

    def test_same_structure_ok(self):
        res = self.check(GrafEnv(graf_dump()))
        self.assertEqual([(r["durum"], r["ayrinti"]) for r in res],
                         [(S.TAMAM, "ayar dosyasıyla aynı yapıda (1 zincir)")])

    def test_values_differ_is_still_ok(self):
        live = graf_dump()
        live[0]["info"]["params"]["Props"][1]["params"] = ["pre_l:Mult", 0.9, "k1_l:Gain", 3.0,
                                                           "lim:th", 0.5]
        self.assertEqual(self.check(GrafEnv(live))[0]["durum"], S.TAMAM)

    def test_old_running_graph_after_regen(self):
        """27 Eki: conf regenerated (new node + restore-props) but the service not restarted."""
        live = graf_dump(kontrol=("pre_l",), cap_props={"state.restore-props": None})
        env = GrafEnv(live, start=150)          # conf mtime 200 > service start 150
        (r,) = self.check(env)
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("servis başladıktan sonra değişti", r["ayrinti"])
        self.assertIn("eksik düğüm: k1_l, lim", r["ayrinti"])
        self.assertIn("state.restore-props=— (false)", r["ayrinti"])
        self.assertEqual(r["oneri"], f"systemctl --user restart {config.SINKS_SERVICE}")

    def test_mismatch_without_newer_conf_points_to_log(self):
        live = graf_dump(kontrol=("pre_l", "k1_l", "lim", "eski"))
        (r,) = self.check(GrafEnv(live, start=500))
        self.assertEqual(r["durum"], S.UYARI)
        self.assertIn("fazla düğüm: eski", r["ayrinti"])
        self.assertIn("journalctl --user -u", r["oneri"])

    def test_silent_when_not_running_or_no_conf(self):
        self.assertEqual(self.check(GrafEnv([])), [])               # sinks absent → check_sinks
        env = GrafEnv(graf_dump())
        env.units[config.SINKS_SERVICE] = unit(active="inactive")
        self.assertEqual(self.check(env), [])
        env = GrafEnv(graf_dump())
        env.files.clear()
        self.assertEqual(self.check(env), [])

    def test_live_conf_files_parse(self):
        """The real generators' output keeps the format this check reads."""
        from oto_eq import eqgen
        g = S.conf_graflari(eqgen.generate_conf(eqgen.load_profiles(), lsp=True))
        self.assertEqual(set(g), {SINK_PREFIX_ + p for p in ("cs2", "oyun", "muzik", "film", "duz")})
        self.assertTrue(all("lim" in g[c]["kontrollu"] for c in g if not c.endswith(("duz",))))
        self.assertTrue(all(v["props"][c]["state.restore-props"] == "false" for c, v in g.items()))


SINK_PREFIX_ = "oto_eq_"


if __name__ == "__main__":
    unittest.main()
