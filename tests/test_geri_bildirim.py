"""geri_bildirim: feedback JSONL + artist genre assignment (no Qt, temp files only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, geri_bildirim as gb


class TempBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.fb = d / "data" / "geri_bildirim.jsonl"
        self.cfg = d / "cfg"
        for target, attr, val in ((gb, "FEEDBACK_FILE", self.fb),
                                  (config, "CONFIG_DIR", self.cfg),
                                  (config, "GENRE_FILE", self.cfg / "tur-atamalari.json")):
            p = mock.patch.object(target, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()


class TestFeedbackFile(TempBase):
    def test_kaydet_format(self):
        rec = gb.kaydet("muzik", "sanatci:virgin black", "Virgin Black", "Our Embrace",
                        "pop_vokal", "iTunes: Pop", "metal_rock")
        lines = self.fb.read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        on_disk = json.loads(lines[0])
        self.assertEqual(on_disk, rec)
        self.assertRegex(rec["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual({k: v for k, v in rec.items() if k != "ts"},
                         {"profile": "muzik", "key": "sanatci:virgin black",
                          "display": "Virgin Black", "track": "Our Embrace",
                          "predicted_group": "pop_vokal", "source": "iTunes: Pop",
                          "corrected_group": "metal_rock"})
        self.assertIn("Our Embrace", lines[0])          # ensure_ascii=False, readable
        gb.kaydet("muzik", "sanatci:şebnem", "Şebnem", None, "", "", "pop_vokal")
        self.assertIn("Şebnem", self.fb.read_text("utf-8"))
        self.assertEqual(gb.oku()[-1]["track"], "")

    def test_oku_order_limit_and_bad_lines(self):
        self.assertEqual(gb.oku(), [])                  # no file yet
        for i in range(5):
            gb.kaydet("muzik", f"sanatci:a{i}", f"A{i}", "", "pop_vokal", "", "metal_rock")
        with open(self.fb, "a", encoding="utf-8") as f:
            f.write("{bozuk\n\n[1, 2]\n")
        recs = gb.oku()
        self.assertEqual([r["display"] for r in recs], [f"A{i}" for i in range(5)])
        self.assertEqual([r["display"] for r in gb.oku(limit=2)], ["A3", "A4"])
        self.assertEqual(gb.oku(limit=0), [])

    def test_rotation(self):
        with mock.patch.object(gb, "MAX_BYTES", 400):
            for i in range(12):
                gb.kaydet("muzik", f"sanatci:a{i}", f"A{i}", "", "pop_vokal", "", "metal_rock")
        old = self.fb.with_name(self.fb.name + ".1")
        self.assertTrue(old.exists())
        self.assertLess(self.fb.stat().st_size, 400 + 300)
        names = [r["display"] for r in gb.oku()]
        self.assertEqual(names, sorted(names, key=lambda n: int(n[1:])))   # old first
        self.assertEqual(names[-1], "A11")
        self.assertNotIn("A0", names)                   # only one old copy is kept

    def test_ozet(self):
        for pred, corr in (("pop_vokal", "metal_rock"), ("pop_vokal", "metal_rock"),
                           ("", "elektronik"), ("elektronik", "")):
            gb.kaydet("muzik", "sanatci:x", "X", "", pred, "", corr)
        s = gb.ozet()
        self.assertEqual(s, {("pop_vokal", "metal_rock"): 2, ("", "elektronik"): 1,
                             ("elektronik", ""): 1})
        self.assertEqual(next(iter(s)), ("pop_vokal", "metal_rock"))   # most common first

    def test_no_audio_fields(self):
        rec = gb.kaydet("muzik", "sanatci:x", "X", "t", "", "ses analizi: bas ağır", "konusma")
        self.assertEqual(set(rec), {"ts", *gb.FIELDS})


class TestArtistAssignment(TempBase):
    V = {"group": "pop_vokal", "label": "Pop / Vokal", "for": "Virgin Black",
         "key": "sanatci:virgin black", "source": "iTunes: Pop", "mode": "auto"}

    def test_key_like_gui(self):
        self.assertEqual(gb.sanatci_anahtari(self.V), "virgin black")
        self.assertEqual(gb.sanatci_anahtari({"for": "Virgin Black"}), "virgin black")
        self.assertEqual(gb.sanatci_anahtari(None), "")

    def test_assign_keeps_other_settings_and_replaces_old_spellings(self):
        config.save_genre_settings({"cevrimici": False, "ses_analizi": True,
                                    "sanatci": {"Virgin Black": "pop_vokal", "ghost": "metal_rock"},
                                    "oyun": {"steam:1": "aksiyon_rpg"}})
        self.assertEqual(gb.sanatci_atamasi("sanatci:virgin black", "Virgin Black"), "pop_vokal")
        k = gb.sanatci_ata("sanatci:virgin black", "metal_rock", "Virgin Black")
        self.assertEqual(k, "virgin black")
        data = json.loads(config.GENRE_FILE.read_text("utf-8"))
        self.assertEqual(data["sanatci"], {"ghost": "metal_rock", "virgin black": "metal_rock"})
        self.assertEqual(data["oyun"], {"steam:1": "aksiyon_rpg"})
        self.assertFalse(data["cevrimici"])
        self.assertEqual(config.load_genre_settings()["sanatci"]["virgin black"], "metal_rock")

    def test_general_and_remove(self):
        gb.sanatci_ata("virgin black", "", "Virgin Black")
        self.assertEqual(gb.sanatci_atamasi("sanatci:virgin black", "Virgin Black"), "")
        gb.sanatci_ata("sanatci:virgin black", None, "Virgin Black")
        self.assertIsNone(gb.sanatci_atamasi("sanatci:virgin black", "Virgin Black"))
        self.assertEqual(config.load_genre_settings()["sanatci"], {})

    def test_resolver_honours_assignment(self):
        """The written key is what tur.py's override lookup matches."""
        from oto_eq import tur
        from oto_eq.models import ContentId
        gb.sanatci_ata("sanatci:virgin black", "metal_rock", "Virgin Black")
        r = tur.GenreResolver.__new__(tur.GenreResolver)
        r.settings = config.load_genre_settings()
        res = r._override(ContentId("muzik", "sanatci:virgin black", "Virgin Black"))
        self.assertEqual((res.group, res.source), ("metal_rock", "kullanıcı"))

    def test_injected_store(self):
        store = {"sanatci": {}}
        saved = []
        gb.sanatci_ata("a", "konusma", load=lambda: json.loads(json.dumps(store)),
                       save=saved.append)
        self.assertEqual(saved[0]["sanatci"], {"a": "konusma"})
        self.assertFalse(config.GENRE_FILE.exists())


if __name__ == "__main__":
    unittest.main()
