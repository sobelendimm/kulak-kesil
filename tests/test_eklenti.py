"""oto_eq.eklenti: LV2/LADSPA lookup in one place (hata avı 6 Kas: eqgen/micgen/saglik missed
the Debian/Ubuntu multiarch dirs install.sh accepts, and checked only the LSP compressor)."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import eklenti, eqgen, micgen


def fake(paths):
    have = {str(p) for p in paths}
    return lambda p: str(p) in have


class DirsTest(unittest.TestCase):
    def test_env_first_then_system_multiarch_nixos(self):
        with mock.patch.dict(os.environ, {"LV2_PATH": "/opt/a:/opt/b", "USER": "kim"}):
            d = eklenti.lv2_dirs()
        self.assertEqual(d[:2], ["/opt/a", "/opt/b"])
        for x in ("/usr/lib/lv2", "/usr/lib64/lv2", "/usr/local/lib/lv2",
                  "/usr/lib/x86_64-linux-gnu/lv2", "/run/current-system/sw/lib/lv2",
                  "/etc/profiles/per-user/kim/lib/lv2", str(Path.home() / ".lv2")):
            self.assertIn(x, d)
        self.assertEqual(len(d), len(set(d)))
        self.assertIn("/usr/lib/x86_64-linux-gnu/ladspa", eklenti.ladspa_dirs())

    def test_bul_and_lsp_set(self):
        multi = Path("/usr/lib/x86_64-linux-gnu/lv2")
        ex = fake([multi / eklenti.LSP_TTL["comp"], multi / eklenti.LSP_TTL["lim"]])
        self.assertEqual(eklenti.bul(eklenti.LSP_TTL["comp"], exists=ex),
                         multi / eklenti.LSP_TTL["comp"])
        self.assertIsNone(eklenti.bul(eklenti.LSP_TTL["ag"], exists=ex))
        self.assertEqual(eklenti.lsp_eklentileri(exists=ex), frozenset({"comp", "lim"}))
        lad = fake([Path("/usr/lib/x86_64-linux-gnu/ladspa") / eklenti.RNNOISE_SO])
        self.assertIsNotNone(eklenti.bul(eklenti.RNNOISE_SO, "ladspa", exists=lad))
        self.assertIsNone(eklenti.bul(eklenti.RNNOISE_SO, "lv2", exists=lad))


class ConsumersTest(unittest.TestCase):
    """eqgen / micgen use eklenti (a multiarch-only install is found)."""

    def test_multiarch_install_is_found(self):
        multi = Path("/usr/lib/x86_64-linux-gnu")
        have = [multi / "lv2" / t for t in eklenti.LSP_TTL.values()]
        have += [multi / "lv2" / eklenti.LSP_MONO_TTL, multi / "ladspa" / eklenti.RNNOISE_SO]
        ex = fake(have)
        real = eklenti.bul
        with mock.patch.object(eklenti, "bul", lambda rel, tur="lv2", exists=None: real(rel, tur, ex)):
            self.assertIs(eqgen.lsp_available(), True)
            self.assertEqual(micgen.rnnoise_path(), multi / "ladspa" / eklenti.RNNOISE_SO)
            self.assertTrue(micgen.lsp_mono_available())

    def test_compressor_only(self):
        ex = fake([Path("/usr/lib/lv2") / eklenti.LSP_TTL["comp"]])
        real = eklenti.bul
        with mock.patch.object(eklenti, "bul", lambda rel, tur="lv2", exists=None: real(rel, tur, ex)):
            self.assertEqual(eqgen.lsp_available(), frozenset({"comp"}))


if __name__ == "__main__":
    unittest.main()
