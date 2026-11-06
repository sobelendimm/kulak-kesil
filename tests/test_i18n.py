"""i18n.py: language choice + catalog fallback (CHANGE 8)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, i18n


class TestLanguage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(i18n.sifirla)

    def test_system_locale(self):
        for env, want in (({"LANG": "tr_TR.UTF-8"}, "tr"), ({"LANG": "de_DE.UTF-8"}, "de"),
                          ({"LC_ALL": "pt_BR.UTF-8", "LANG": "tr_TR"}, "pt"),
                          ({"LANG": "C.UTF-8", "LANGUAGE": "ja_JP"}, "en"),     # gettext: C → no
                          ({"LANG": "en_US.UTF-8", "LANGUAGE": "de:en"}, "de"),  # LANGUAGE wins
                          ({"LANG": "tr_TR.UTF-8", "LANGUAGE": "ko:ja"}, "ja"),  # 1st supported
                          ({"LC_ALL": "C", "LANG": "de_DE.UTF-8"}, "en"),
                          ({"LANG": "tr_TR.UTF-8", "LANGUAGE": ""}, "tr"),
                          ({"LANG": "ko_KR.UTF-8"}, "en"), ({}, "en"),
                          ({"LC_MESSAGES": "zh_CN.UTF-8"}, "zh"),
                          ({"LANG": "sr_RS@latin"}, "en")):
            with self.subTest(env=env):
                self.assertEqual(i18n.sistem_dili(env), want)

    def test_priority_forced_saved_system(self):
        self.assertEqual(i18n.secilen_dil({"LANG": "fr_FR.UTF-8"}), "fr")      # otomatik
        i18n.ayar_yaz("ru")
        self.assertEqual(i18n.ayar_oku(), "ru")
        self.assertEqual(i18n.secilen_dil({"LANG": "fr_FR.UTF-8"}), "ru")
        self.assertEqual(i18n.secilen_dil({"LANG": "fr_FR", "OTO_EQ_DIL": "es"}), "es")
        i18n.ayar_yaz(i18n.OTOMATIK)
        self.assertEqual(i18n.secilen_dil({"LANG": "fr_FR.UTF-8"}), "fr")
        with self.assertRaises(ValueError):
            i18n.ayar_yaz("xx")
        (Path(self.tmp.name) / i18n.SETTINGS_FILE).write_text("{bozuk", "utf-8")
        self.assertEqual(i18n.ayar_oku(), i18n.OTOMATIK)

    def test_catalog_fallback_and_placeholders(self):
        d = Path(self.tmp.name)
        (d / "de.json").write_text(json.dumps({"Kaydet": "Speichern", "Boş": "",
                                               "Kaydedildi: {ad}": "Gespeichert: {ad}"}), "utf-8")
        cat = i18n.katalog_oku("de", d)
        self.assertEqual(cat, {"Kaydet": "Speichern", "Kaydedildi: {ad}": "Gespeichert: {ad}"})
        self.assertEqual(i18n.katalog_oku("tr", d), {})
        self.assertEqual(i18n.katalog_oku("fr", d), {})            # missing file
        i18n.sifirla("de", cat)
        self.assertEqual(i18n._("Kaydet"), "Speichern")
        self.assertEqual(i18n._("Boş"), "Boş")                      # empty entry → Turkish
        self.assertEqual(i18n._("Çevrilmemiş metin"), "Çevrilmemiş metin")
        self.assertEqual(i18n._("Kaydedildi: {ad}").format(ad="X"), "Gespeichert: X")
        self.assertEqual(i18n.N_("x"), "x")

    def test_coverage_against_the_source_list(self):
        d = Path(self.tmp.name)
        self.assertIsNone(i18n.kapsama("de", d))                    # no kaynak.json
        (d / "kaynak.json").write_text(json.dumps({"A": [], "B": [], "C": [], "D": []}), "utf-8")
        (d / "de.json").write_text(json.dumps({"A": "a", "B": "", "X": "x"}), "utf-8")
        self.assertEqual(i18n.kapsama("de", d), 0.25)               # empty / stale don't count
        self.assertEqual(i18n.kapsama("fr", d), 0.0)
        self.assertIsNone(i18n.kapsama("tr", d))

    def test_reload_follows_a_new_language_choice(self):
        # a long-running router: IPC "dil" → yeniden_yukle() → texts in the new language
        i18n.sifirla()
        self.assertEqual(i18n._("Kaydet"), "Kaydet")
        with mock.patch.dict("os.environ", {"OTO_EQ_DIL": "en"}):
            self.assertEqual(i18n.yeniden_yukle(), "en")
            self.assertEqual(i18n._("Kaydet"), "Save")
        self.assertEqual(i18n.yeniden_yukle(), "tr")
        self.assertEqual(i18n._("Kaydet"), "Kaydet")

    def test_label_mappings_translate_at_access_time(self):
        from oto_eq import kulaklik, models
        self.assertEqual(models.PROFILE_LABELS["muzik"], "Müzik")
        kat = {"Müzik": "Music", "Strateji / İnşa": "Strategy / Building",
               "USB kulaklık": "USB headphones"}
        with mock.patch.dict(i18n._state, {"dil": "en", "katalog": kat}):
            self.assertEqual(models.PROFILE_LABELS["muzik"], "Music")
            self.assertEqual(models.PROFILE_LABELS.get("muzik"), "Music")
            self.assertEqual(models.GROUP_LABELS["strateji_insa"], "Strategy / Building")
            self.assertEqual(models.PROFILE_LABELS.get("yok", "?"), "?")
            self.assertEqual(kulaklik.Aygit("a", "b", "usb", True, True, "").etiket,
                             "USB headphones")
        self.assertEqual(list(models.PROFILE_LABELS), list(models.PROFILE_NAMES))
        self.assertEqual(len(models.GROUP_LABELS), len(models.GROUP_NAMES))
        self.assertIn("film", models.PROFILE_LABELS)

    def test_every_language_has_a_name_and_tests_run_in_turkish(self):
        self.assertEqual(len(i18n.DILLER), 9)                      # Türkçe + 8
        self.assertEqual(next(iter(i18n.DILLER)), "tr")
        i18n.sifirla()
        self.assertEqual(i18n.dil(), "tr")                         # tests/__init__: OTO_EQ_DIL=tr


if __name__ == "__main__":
    unittest.main()
