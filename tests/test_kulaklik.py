"""kulaklik.py: output device type detection + the shared device state (CHANGE 7)."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, kulaklik as K

HS80 = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo"


def info(bus="", form="", port="", ptype="", desc="", api="alsa", profile="", icon="",
         product="", ports=None):
    props = {k: v for k, v in (("device.bus", bus), ("device.api", api),
                               ("device.description", desc), ("device.form_factor", form),
                               ("api.bluez5.profile", profile), ("device.icon_name", icon),
                               ("device.product.name", product)) if v}
    if ports is None:
        ports = [{"name": port, "type": ptype, "availability": "available"}] if port else []
    return {"properties": props, "ports": ports, "active_port": port or None}


# (name, description, info) -> (tur, kulaklik)
CASES = [
    ("HS80 (live pactl data)", HS80, "CORSAIR HS80 RGB Wireless Gaming Receiver Analog Stereo",
     info("usb", port="analog-output-headphones", ptype="Headphones",
          product="CORSAIR HS80 RGB Wireless Gaming Receiver"), ("usb", True)),
    ("onboard line-out = speakers", "alsa_output.pci-0000_15_00.6.analog-stereo",
     "Ryzen HD Audio Controller Analog Stereo",
     info("pci", port="analog-output-lineout", ptype="Line",
          ports=[{"name": "analog-output-lineout", "type": "Line"},
                 {"name": "analog-output-headphones", "type": "Headphones",
                  "availability": "not available"}]), ("hoparlor", False)),
    ("onboard 3.5 mm headphones", "alsa_output.pci-0000_15_00.6.analog-stereo", "Ryzen HD Audio",
     info("pci", port="analog-output-headphones", ptype="Headphones"), ("jak", True)),
    ("port type missing, name says headphones", "alsa_output.pci-x.analog-stereo", "Onboard",
     info("pci", port="analog-output-headphones", ptype=""), ("jak", True)),
    ("HDMI monitor", "alsa_output.pci-0000_03_00.1.hdmi-stereo",
     "Navi 31 HDMI/DP Audio Digital Stereo (HDMI) [DELL AW2723DF]",
     info("pci", port="hdmi-output-0", ptype="HDMI"), ("hdmi", False)),
    ("S/PDIF profile", "alsa_output.pci-x.iec958-stereo", "Digital Output",
     {"properties": {"device.bus": "pci", "device.api": "alsa",
                     "device.profile.name": "iec958-stereo"}, "ports": []}, ("hdmi", False)),
    ("Bluetooth headphones (A2DP)", "bluez_output.AA_BB_CC_DD_EE_FF.1", "WH-1000XM4",
     info("bluetooth", "headphone", api="bluez5", profile="a2dp-sink"), ("bluetooth", True)),
    ("Bluetooth headset in call mode", "bluez_output.AA_BB.headset-head-unit", "Buds2",
     info("bluetooth", "headset", api="bluez5", profile="headset-head-unit"), ("bluetooth", True)),
    ("Bluetooth speaker", "bluez_output.11_22.1", "JBL Flip 5",
     info("bluetooth", "portable", api="bluez5"), ("bt_hoparlor", False)),
    ("Bluetooth, no form factor, speaker in name", "bluez_output.33_44.1", "Party Speaker",
     info("bluetooth", api="bluez5"), ("bt_hoparlor", False)),
    ("Bluetooth, no form factor at all", "bluez_output.55_66.1", "XYZ-01",
     info("bluetooth", api="bluez5"), ("bluetooth", True)),
    ("Bluetooth icon only", "bluez_output.77_88.1", "Q30",
     info("bluetooth", api="bluez5", icon="audio-headphones-bluetooth"), ("bluetooth", True)),
    ("USB headset reporting a Speaker terminal", "alsa_output.usb-Logitech_PRO_X-00.analog-stereo",
     "PRO X Gaming Headset Analog Stereo",
     info("usb", port="analog-output-speaker", ptype="Speaker"), ("usb", True)),
    ("USB headphone port, generic name", "alsa_output.usb-Generic_USB_Audio-00.analog-stereo",
     "USB Audio", info("usb", port="analog-output-headphones", ptype="Headphones"),
     ("usb", True)),
    ("USB speaker", "alsa_output.usb-Creative_Pebble-00.analog-stereo", "Pebble V3 Speaker",
     info("usb", port="analog-output-speaker", ptype="Speaker"), ("hoparlor", False)),
    ("USB DAC, nothing known", "alsa_output.usb-ACME_DAC-00.analog-stereo", "ACME DAC",
     info("usb", port="analog-output", ptype="Analog"), ("bilinmiyor", False)),
    ("form factor headset on USB", "alsa_output.usb-ACME_X-00.analog-stereo", "ACME X",
     info("usb", "headset", port="analog-output", ptype="Analog"), ("usb", True)),
    ("EasyEffects (software sink)", "easyeffects_sink", "Easy Effects Sink",
     {"properties": {"media.class": "Audio/Sink", "factory.name": "support.null-audio-sink"},
      "ports": []}, ("sanal", False)),
    ("our own sink", "oto_eq_muzik", "Kulak Kesil · Müzik", {}, ("sanal", False)),
    ("text fallback: HS80 name glued with _", HS80, HS80, {}, ("usb", True)),
    ("text fallback: HDMI", "alsa_output.pci-0000_03_00.1.hdmi-stereo", "x", {}, ("hdmi", False)),
    ("text fallback: Bluetooth", "bluez_output.AA.1", "x", {}, ("bluetooth", True)),
    ("text fallback: unknown onboard", "alsa_output.pci-0000_0e_00.6.analog-stereo", "x", {},
     ("bilinmiyor", False)),
    ("no default sink", "", "", {}, ("bilinmiyor", False)),
    # H1 (27 Eki): digital profile / speaker form must not hide a headphone
    ("USB headset on its S/PDIF profile", "alsa_output.usb-SteelSeries_Arctis_7-00.iec958-stereo",
     "Arctis 7 Chat Digital Stereo (IEC958)",
     {"properties": {"device.bus": "usb", "device.api": "alsa",
                     "device.profile.name": "iec958-stereo"},
      "ports": [{"name": "iec958-stereo-output", "type": "SPDIF"}],
      "active_port": "iec958-stereo-output"}, ("usb", True)),
    ("USB S/PDIF DAC, no headphone word", "alsa_output.usb-ACME_Optical-00.iec958-stereo",
     "ACME Optical Digital Stereo (IEC958)",
     {"properties": {"device.bus": "usb", "device.api": "alsa",
                     "device.profile.name": "iec958-stereo"}, "ports": []}, ("hdmi", False)),
    ("earbuds reporting 'portable'", "bluez_output.99_AA.1", "Galaxy Buds2 Pro",
     info("bluetooth", "portable", api="bluez5"), ("bluetooth", True)),
    ("USB brand headset, Analog port", "alsa_output.usb-HP_Inc_HyperX_Cloud_Alpha_Wireless-00.analog-stereo",
     "HyperX Cloud Alpha Wireless Analog Stereo",
     info("usb", port="analog-output", ptype="Analog"), ("usb", True)),
    ("USB soundbar of a headphone brand", "alsa_output.usb-Sennheiser_AMBEO-00.analog-stereo",
     "Sennheiser AMBEO Soundbar", info("usb", port="analog-output", ptype="Analog"),
     ("hoparlor", False)),
]


class TestClassify(unittest.TestCase):
    def test_table(self):
        for label, name, desc, inf, want in CASES:
            with self.subTest(label):
                a = K.siniflandir(name, desc, inf)
                self.assertEqual((a.tur, a.kulaklik), want, a.neden)
                self.assertTrue(a.neden)
                self.assertEqual(a.etiket, K._(K.TUR_ETIKET[a.tur]))

    def test_correction_only_on_its_own_model(self):
        hs = K.siniflandir(HS80, "CORSAIR HS80 RGB", CASES[0][3])
        bt = K.siniflandir("bluez_output.AA.1", "WH-1000XM4", info("bluetooth", "headphone",
                                                                  api="bluez5"))
        self.assertTrue(hs.duzeltme)
        self.assertFalse(bt.duzeltme)
        self.assertEqual(hs.aciklama, "CORSAIR HS80 RGB Wireless Gaming Receiver")
        none = K.Ayar(duzeltme=None)               # cihaz_desen = "" → nowhere
        self.assertFalse(K.siniflandir(HS80, "HS80", CASES[0][3], none).duzeltme)

    def test_model_name_without_card_suffix(self):
        a = K.siniflandir("alsa_output.pci-0000_03_00.1.hdmi-stereo",
                          "Navi 31 HDMI/DP Audio Digital Stereo (HDMI) [DELL AW2723DF]",
                          info("pci", port="hdmi-output-0", ptype="HDMI", desc=""))
        self.assertEqual(a.aciklama, "Navi 31 HDMI/DP Audio")

    def test_user_patterns(self):
        spk = ("alsa_output.pci-0000_15_00.6.analog-stereo", "Ryzen HD Audio",
               info("pci", port="analog-output-lineout", ptype="Line"))
        forced = K.siniflandir(*spk, K.Ayar(desen=re.compile("pci-0000_15")))
        self.assertEqual((forced.tur, forced.kulaklik), ("desen", True))
        hs = K.siniflandir(HS80, "HS80", CASES[0][3], K.Ayar(haric=re.compile("(?i)hs80")))
        self.assertFalse(hs.kulaklik)
        self.assertFalse(hs.duzeltme)              # not a headphone → no correction either
        self.assertIn("kulaklik_haric", hs.neden)
        bt = ("bluez_output.AA.1", "WH-1000XM4", info("bluetooth", "headphone", api="bluez5"))
        old = K.Ayar(otomatik=False, desen=re.compile("(?i)hs80"))
        self.assertFalse(K.siniflandir(*bt, old).kulaklik)
        self.assertTrue(K.siniflandir(HS80, "HS80", CASES[0][3], old).kulaklik)

    def test_ayar_oku(self):
        a, err = K.ayar_oku({"kulaklik_otomatik": False, "kulaklik_haric": "x+"}, "(?i)hs80", "")
        self.assertIsNone(err)
        self.assertFalse(a.otomatik)
        self.assertEqual(a.haric.pattern, "x+")
        self.assertIsNone(a.duzeltme)
        a, err = K.ayar_oku({"kulaklik_otomatik": "evet"}, "")
        self.assertTrue(a.otomatik)                 # not a bool → default
        self.assertEqual(a.duzeltme.pattern, K.DEFAULT_DUZELTME_DESEN)
        _a, err = K.ayar_oku({"kulaklik_haric": "(x"}, "")
        self.assertIn("kulaklik_haric", err)
        a, err = K.ayar_oku(None, "", "(x")
        self.assertIn("cihaz_desen", err)
        self.assertEqual(a.duzeltme.pattern, K.DEFAULT_DUZELTME_DESEN)   # invalid → HS80, not nowhere

    def test_never_raises_on_odd_data(self):
        for inf in ({"properties": "x"}, {"ports": [None, 3, "a"], "active_port": 5},
                    {"properties": {"device.bus": None}}, None):
            with self.subTest(inf=inf):
                self.assertIsInstance(K.siniflandir("alsa_output.usb-x", "x", inf), K.Aygit)


class TestDeviceState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cihaz.json"

    def test_write_read_and_correction_switch(self):
        self.assertTrue(K.duzeltme_acik(self.path))          # no file: old behaviour
        bt = K.siniflandir("bluez_output.AA.1", "WH", info("bluetooth", "headphone", api="bluez5"))
        K.durum_yaz(bt, self.path)
        self.assertFalse(K.duzeltme_acik(self.path))
        self.assertEqual(K.durum_oku(self.path)["tur"], "bluetooth")
        hs = K.siniflandir(HS80, "HS80", CASES[0][3])
        K.durum_yaz(hs, self.path)
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # coarse mtime
        self.assertTrue(K.duzeltme_acik(self.path))
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["cihaz.json"])  # no tmp

    def test_corrupt_file_means_on(self):
        self.path.write_text("{bozuk", encoding="utf-8")
        self.assertTrue(K.duzeltme_acik(self.path))
        self.path.write_text('{"duzeltme": "hayir"}', encoding="utf-8")
        st = self.path.stat()
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
        self.assertTrue(K.duzeltme_acik(self.path))

    def test_default_path_is_state_dir(self):
        with mock.patch.object(config, "STATE_DIR", Path(self.tmp.name)):
            self.assertEqual(K.durum_yolu(), self.path)

    def test_correction_pattern_from_profiles(self):
        prof = Path(self.tmp.name) / "profiller.toml"
        prof.write_text('[duzeltme]\ncihaz_desen = "(?i)arctis"\n', encoding="utf-8")
        with mock.patch.object(config, "config_path", lambda _n: prof):
            self.assertEqual(K.duzeltme_desen_oku(), "(?i)arctis")
        for bad in ("[duzeltme\n", "[[duzeltme]]\naktif = true\n", "duzeltme = 1\n"):
            prof.write_text(bad, encoding="utf-8")
            with self.subTest(bad=bad), mock.patch.object(config, "config_path", lambda _n: prof):
                self.assertEqual(K.duzeltme_desen_oku(), K.DEFAULT_DUZELTME_DESEN)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_neden(self):
        from oto_eq import i18n
        self.assertEqual(K.siniflandir("").neden, "varsayılan çıkış yok")
        i18n.sifirla("de", {"varsayılan çıkış yok": "kein Standardausgang"})
        self.assertEqual(K.siniflandir("").neden, "kein Standardausgang")


if __name__ == "__main__":
    unittest.main()
