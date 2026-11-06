"""Unit tests for oto_eq.kisisel (kişisel ton storage; temp files only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, kisisel


class KisiselTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oto-eq-kisisel-")
        self.file = Path(self.tmp.name) / "cfg" / "kisisel.json"
        p = mock.patch.object(kisisel, "KISISEL_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_constants(self):
        self.assertEqual(kisisel.TON_KEYS, ("bas", "vokal", "parlaklik"))
        self.assertEqual(kisisel.TON_LIMIT, 3.0)
        self.assertEqual(kisisel.TUR_LIMIT, 6.0)
        self.assertEqual(kisisel.TON_BANDS["bas"], ("LSC", 120.0, 0.7))
        self.assertEqual(kisisel.TON_BANDS["vokal"], ("PK", 2500.0, 0.8))
        self.assertEqual(kisisel.TON_BANDS["parlaklik"], ("HSC", 8000.0, 0.7))

    def test_missing_file_is_zero(self):
        d = kisisel.load()
        self.assertEqual(d, {"genel": kisisel.zero(), "tur": {}})
        self.assertEqual(kisisel.effective(d, "muzik", "elektronik"), kisisel.zero())
        self.assertFalse(self.file.exists())          # load never writes

    def test_broken_file_is_zero(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{bozuk", encoding="utf-8")
        with self.assertLogs("oto_eq.kisisel", "WARNING"):
            self.assertEqual(kisisel.load()["genel"], kisisel.zero())
        self.file.write_text(json.dumps({"genel": {"bas": "çok", "vokal": 9.0},
                                         "tur": {"muzik:yok": {"bas": 1}, "cs2:x": {},
                                                 "oyun:rekabetci": {"vokal": 1.25}}}),
                             encoding="utf-8")
        with self.assertLogs("oto_eq.kisisel", "WARNING"):
            d = kisisel.load()
        self.assertEqual(d["genel"], {"bas": 0.0, "vokal": 3.0, "parlaklik": 0.0})
        self.assertEqual(list(d["tur"]), ["oyun:rekabetci"])
        self.assertEqual(d["tur"]["oyun:rekabetci"]["vokal"], 1.25)

    def test_set_genel_merge_and_clamp(self):
        kisisel.set_genel(bas=1.5)
        d = kisisel.set_genel(vokal=-7, parlaklik=0.504)
        self.assertEqual(d["genel"], {"bas": 1.5, "vokal": -3.0, "parlaklik": 0.5})
        self.assertEqual(kisisel.load(), d)
        with self.assertRaises(ValueError):
            kisisel.set_genel(tiz=1.0)
        with self.assertRaises(ValueError):
            kisisel.set_genel(bas=float("nan"))
        with self.assertRaises(ValueError):
            kisisel.set_genel(bas=True)

    def test_atomic_save_leaves_no_temp(self):
        kisisel.set_genel(bas=1.0)
        files = sorted(p.name for p in self.file.parent.iterdir())
        self.assertEqual(files, ["kisisel.json"])
        raw = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(raw["genel"]["bas"], 1.0)

    def test_set_tur_and_effective(self):
        kisisel.set_genel(bas=1.0, vokal=2.5)
        d = kisisel.set_tur("muzik", "elektronik", bas=1.5, vokal=1.0)
        self.assertEqual(kisisel.effective(d, "muzik", "elektronik"),
                         {"bas": 2.5, "vokal": 3.0, "parlaklik": 0.0})   # 3.5 clamped
        self.assertEqual(kisisel.effective(d, "muzik", ""), {"bas": 1.0, "vokal": 2.5,
                                                             "parlaklik": 0.0})
        self.assertEqual(kisisel.effective(d, "muzik", "metal_rock")["bas"], 1.0)
        self.assertEqual(kisisel.effective(d, "film")["vokal"], 2.5)
        # all-zero entry is removed
        d = kisisel.set_tur("muzik", "elektronik", bas=0, vokal=0)
        self.assertNotIn("muzik:elektronik", d["tur"])
        with self.assertRaises(ValueError):
            kisisel.set_tur("muzik", "rekabetci", bas=1)      # game group for music
        with self.assertRaises(ValueError):
            kisisel.set_tur("cs2", "", bas=1)
        with self.assertRaises(ValueError):
            kisisel.set_tur("muzik", "", bas=1)

    def test_tur_offsets_reach_any_target(self):
        """C2 saves set_tur(p, g, wanted - genel): offsets up to ±2·TON_LIMIT must stick."""
        kisisel.set_genel(bas=3.0, vokal=-3.0)
        d = kisisel.set_tur("oyun", "rekabetci", bas=-6.0, vokal=6.0, parlaklik=-9.0)
        self.assertEqual(d["tur"]["oyun:rekabetci"], {"bas": -6.0, "vokal": 6.0,
                                                     "parlaklik": -6.0})
        eff = kisisel.effective(kisisel.load(), "oyun", "rekabetci")
        self.assertEqual(eff, {"bas": -3.0, "vokal": 3.0, "parlaklik": -3.0})
        # any wanted effective value within ±3 is reachable from any genel value
        for genel in (-3.0, 0.0, 3.0):
            for want in (-3.0, -1.5, 0.0, 2.0, 3.0):
                kisisel.set_genel(bas=genel)
                kisisel.set_tur("muzik", "pop_vokal", bas=want - genel)
                self.assertEqual(kisisel.effective(kisisel.load(), "muzik", "pop_vokal")["bas"],
                                 want)
        # genel itself stays within ±TON_LIMIT
        self.assertEqual(kisisel.set_genel(bas=-6.0)["genel"]["bas"], -3.0)

    def test_resets(self):
        kisisel.set_genel(bas=1.0)
        kisisel.set_tur("muzik", "elektronik", bas=1.0)
        kisisel.set_tur("oyun", "rekabetci", vokal=1.0)
        d = kisisel.reset_tur("muzik", "elektronik")
        self.assertEqual(list(d["tur"]), ["oyun:rekabetci"])
        self.assertEqual(d["genel"]["bas"], 1.0)
        d = kisisel.reset_genel()
        self.assertEqual(d["genel"], kisisel.zero())
        self.assertIn("oyun:rekabetci", d["tur"])
        self.assertEqual(kisisel.reset_all(), {"genel": kisisel.zero(), "tur": {}})

    def test_clamp_step(self):
        self.assertEqual(kisisel.clamp(1.3, step=0.5), 1.5)
        self.assertEqual(kisisel.clamp(-1.2, step=0.5), -1.0)
        self.assertEqual(kisisel.clamp(-0.2, step=0.5), 0.0)
        self.assertEqual(str(kisisel.clamp(-0.001)), "0.0")
        self.assertEqual(kisisel.clamp(4, step=0.5), 3.0)

    def test_path_follows_config_dir(self):
        with mock.patch.object(kisisel, "KISISEL_FILE", None), \
                mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name) / "baska"):
            self.assertEqual(kisisel.path(), Path(self.tmp.name) / "baska" / "kisisel.json")
            kisisel.set_genel(parlaklik=-1.0)
            self.assertTrue((Path(self.tmp.name) / "baska" / "kisisel.json").exists())



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_error(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"tür boş: Genel ayar için set_genel kullanın": "Genre leer"})
        with self.assertRaisesRegex(ValueError, "^Genre leer$"):
            kisisel.tur_key("muzik", "")


if __name__ == "__main__":
    unittest.main()
