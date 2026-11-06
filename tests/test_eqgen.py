"""Unit tests for oto_eq.eqgen (pure math / text; no audio, no pipewire)."""
from __future__ import annotations

import copy
import re
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from oto_eq import config, eqgen, kisisel
from oto_eq.models import ALL_SINK_PROFILES, EQ_PROFILES, FLAT_PROFILE

F = np.geomspace(20, 20000, 2000)
_TMP = tempfile.TemporaryDirectory(prefix="oto-eq-test-eqgen-")
_PATCHES = []


def setUpModule():
    # LIVE-SAFETY + determinism: never read the user's kisisel.json / live conf
    for p in (mock.patch.object(kisisel, "KISISEL_FILE", Path(_TMP.name) / "kisisel.json"),
              mock.patch.object(config, "SINKS_CONF", Path(_TMP.name) / "yok.conf")):
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    for p in _PATCHES:
        p.stop()
    _TMP.cleanup()


def shipped() -> dict:
    return eqgen.load_profiles(config.DEFAULTS_DIR / config.PROFILES_FILE)


class MathTests(unittest.TestCase):
    def test_flat_is_zero(self):
        self.assertTrue(np.allclose(eqgen.response_db([], F), 0.0))
        zero = [{"tip": t, "fc": 1000.0, "kazanc": 0.0, "q": 1.0} for t in ("PK", "LSC", "HSC")]
        self.assertTrue(np.allclose(eqgen.response_db(zero, F), 0.0, atol=1e-9))

    def test_disabled_filter_is_flat(self):
        f = [{"tip": "PK", "fc": 1000.0, "kazanc": 6.0, "q": 1.0, "aktif": False}]
        self.assertTrue(np.allclose(eqgen.response_db(f, F), 0.0))

    def test_peaking_gain_at_fc(self):
        for g in (-6.0, 3.0, 9.0):
            for fc in (100.0, 1000.0, 3485.0):
                r = eqgen.response_db([("PK", fc, g, 1.4)], [fc])[0]
                self.assertAlmostEqual(r, g, places=6)
        # far away from fc -> ~0
        r = eqgen.response_db([("PK", 1000.0, 6.0, 2.0)], [20.0, 20000.0])
        self.assertTrue(np.all(np.abs(r) < 0.2))

    def test_shelves(self):
        ls = eqgen.response_db([("LSC", 105.0, 7.4, 0.7)], [10.0, 105.0, 10000.0])
        self.assertAlmostEqual(ls[0], 7.4, delta=0.1)       # full gain well below fc
        self.assertAlmostEqual(ls[1], 7.4 / 2, delta=0.05)  # half gain at fc (RBJ)
        self.assertAlmostEqual(ls[2], 0.0, delta=0.05)
        hs = eqgen.response_db([("HSC", 2000.0, -4.0, 0.7)], [20.0, 2000.0, 23000.0], rate=48000)
        self.assertAlmostEqual(hs[0], 0.0, delta=0.05)
        self.assertAlmostEqual(hs[1], -2.0, delta=0.05)
        self.assertAlmostEqual(hs[2], -4.0, delta=0.1)

    def test_coefs_match_rbj_peaking(self):
        b0, b1, b2, a0, a1, a2 = eqgen.biquad_coefs("PK", 1000.0, 6.0, 1.0, 48000)
        w0 = 2 * np.pi * 1000 / 48000
        A = 10 ** (6 / 40)
        al = np.sin(w0) / 2
        self.assertAlmostEqual(b0, 1 + al * A)
        self.assertAlmostEqual(a0, 1 + al / A)
        self.assertAlmostEqual(b1, -2 * np.cos(w0))

    def test_comp_static_gain(self):
        d = dict(eqgen.DINAMIK_DEFAULTS, esik_db=-20.0, oran=4.0, diz_db=0.0, telafi_db=6.0)
        self.assertAlmostEqual(eqgen.comp_static_gain_db(d, -40.0), 6.0)
        self.assertAlmostEqual(eqgen.comp_static_gain_db(d, 0.0), 6.0 - 15.0)


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.p = shipped()

    def test_preamp_no_clipping(self):
        for lsp in (True, False):
            st = eqgen.gain_staging(self.p, lsp)
            for n in EQ_PROFILES:
                resp = eqgen.response_db(eqgen.effective_filters(self.p, n), F)
                peak = float(np.max(resp + st[n]["preamp_db"] + st[n]["match_db"]))
                # with a limiter the preamp gives marj_db back (the limiter catches peaks)
                self.assertLessEqual(peak, -self.p["genel"]["pay_db"]
                                     + st[n]["limiter_margin_db"] + 1e-6, n)
                self.assertLessEqual(st[n]["match_db"], 0.0)
                self.assertLessEqual(st[n]["preamp_db"], 0.0)

    def test_loudness_matched(self):
        for lsp in (True, False):
            st = eqgen.gain_staging(self.p, lsp)
            louds = [st[n]["final_loudness_db"] for n in ALL_SINK_PROFILES]
            self.assertLessEqual(max(louds) - min(louds), 0.01, louds)   # spec: ±0.5 dB
            # total_response agrees with staging
            g = self.p["genel"]
            band = np.geomspace(g["esitleme_alt_hz"], g["esitleme_ust_hz"], 400)
            vals = [eqgen._pink_loudness(eqgen.total_response(self.p, n, band, lsp))
                    for n in ALL_SINK_PROFILES]
            self.assertLessEqual(max(vals) - min(vals), 0.01, vals)

    def test_worst_case_ceiling(self):
        """Full-scale transient incl. compressor attack overshoot stays <= tavan."""
        variants = []
        for mk in (0.0, 6.0, 12.0, 20.0):
            for match in (True, False):
                for thr in (-40.0, -20.0):
                    p = copy.deepcopy(self.p)
                    p["profil"]["cs2"]["dinamik"]["telafi_db"] = mk
                    p["profil"]["cs2"]["dinamik"]["esik_db"] = thr
                    p["profil"]["oyun"]["dinamik"]["aktif"] = True
                    p["genel"]["ses_esitleme"] = match
                    variants.append(p)
        for p in variants:
            st = eqgen.gain_staging(p, True)
            ceil = p["genel"]["tavan_dbfs"]
            for n in ALL_SINK_PROFILES:
                r = st[n]
                d = eqgen._dyn_active(p, n, True)
                resp = eqgen.response_db(eqgen.effective_filters(p, n), F)
                # input 0 dBFS at the worst frequency, compressor still at makeup
                worst = (float(np.max(resp)) + r["preamp_db"] + (d["telafi_db"] if d else 0.0)
                         + r["match_db"])
                self.assertLessEqual(r["out_peak_dbfs"], ceil + 1e-6, n)
                if r["limiter"]:
                    # limited: a SUSTAINED full-scale sine (tone 0, compressor settled) may
                    # exceed tavan by marj_db; transients / tone peaks = the limiter's job
                    lvl = float(np.max(resp)) + r["preamp_db"]
                    steady = lvl + (eqgen.comp_static_gain_db(d, lvl) if d else 0.0)
                    self.assertLessEqual(steady + r["match_db"],
                                         ceil + r["limiter_margin_db"] + 1e-6, n)
                    self.assertAlmostEqual(r["curve_peak_dbfs"], steady + r["match_db"], delta=0.01)
                else:
                    self.assertLessEqual(worst, ceil + 1e-6, (n, worst))
            if p["genel"]["ses_esitleme"]:
                louds = [st[n]["final_loudness_db"] for n in ALL_SINK_PROFILES]
                self.assertLessEqual(max(louds) - min(louds), 0.01)

    def test_flat_sink_gain(self):
        st = eqgen.gain_staging(self.p, True)
        common = min(st[n]["final_loudness_db"] for n in EQ_PROFILES)
        self.assertAlmostEqual(st[FLAT_PROFILE]["match_db"], common)
        self.assertLess(st[FLAT_PROFILE]["match_db"], -3.0)
        p = copy.deepcopy(self.p)
        p[FLAT_PROFILE]["ek_kazanc_db"] = 2.0
        self.assertAlmostEqual(eqgen.gain_staging(p, True)[FLAT_PROFILE]["match_db"], common + 2.0)
        p[FLAT_PROFILE]["ek_kazanc_db"] = 12.0     # capped at the ceiling
        for n in EQ_PROFILES:       # (the kişisel ton headroom would hide the cap)
            p["profil"][n]["kisisel_ton"] = False
        with self.assertLogs("oto_eq.eqgen", "WARNING"):
            g = eqgen.gain_staging(p, True)[FLAT_PROFILE]["match_db"]
        self.assertAlmostEqual(g, p["genel"]["tavan_dbfs"])
        p[FLAT_PROFILE]["aktif"] = False
        self.assertEqual(eqgen.gain_staging(p, True)[FLAT_PROFILE]["match_db"], 0.0)
        ctr = eqgen.node_controls(self.p, FLAT_PROFILE, lsp=True)
        self.assertEqual(set(ctr), {"cikis_l", "cikis_r"})
        self.assertAlmostEqual(eqgen.profile_preamp(self.p, FLAT_PROFILE, True), common)
        self.assertTrue(np.allclose(eqgen.total_response(self.p, FLAT_PROFILE, F, True), common))

    def test_matching_off(self):
        p = copy.deepcopy(self.p)
        p["genel"]["ses_esitleme"] = False
        st = eqgen.gain_staging(p, True)
        for n in EQ_PROFILES:   # only the safety attenuation may remain
            self.assertEqual(st[n]["match_db"], st[n]["safety_db"])

    def test_inactive_profile_is_flat(self):
        p = copy.deepcopy(self.p)
        p["profil"]["muzik"]["aktif"] = False
        self.assertEqual(eqgen.effective_filters(p, "muzik"), [])
        st = eqgen.gain_staging(p, True)
        self.assertEqual(st["muzik"]["preamp_db"], 0.0)
        ctr = eqgen.node_controls(p, "muzik", lsp=True)
        self.assertTrue(all(c["Gain"] == 0.0 for k, c in ctr.items() if k[0] in "db"))

    def test_profile_preamp(self):
        st = eqgen.gain_staging(self.p, True)
        self.assertAlmostEqual(eqgen.profile_preamp(self.p, "cs2", True),
                               st["cs2"]["preamp_db"] + st["cs2"]["match_db"])

    def test_cs2_zero_lookahead_forced(self):
        raw = tomllib.loads((config.DEFAULTS_DIR / config.PROFILES_FILE).read_text())
        raw["profil"]["cs2"]["dinamik"]["ileri_bakis_ms"] = 5.0
        with self.assertLogs("oto_eq.eqgen", "WARNING"):
            p = eqgen.normalize_profiles(raw)
        self.assertEqual(p["profil"]["cs2"]["dinamik"]["ileri_bakis_ms"], 0.0)
        self.assertEqual(eqgen.node_controls(p, "cs2", lsp=True)["comp"]["sla"], 0.0)

    def test_validation(self):
        raw = tomllib.loads((config.DEFAULTS_DIR / config.PROFILES_FILE).read_text())
        bad = copy.deepcopy(raw)
        bad["profil"]["cs2"]["filtreler"][0]["tip"] = "XX"
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)
        bad = copy.deepcopy(raw)
        bad["profil"]["yok"] = {}
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)


class ConfTests(unittest.TestCase):
    def setUp(self):
        self.p = shipped()

    def test_conf_has_five_sinks(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        self.assertEqual(txt.count("libpipewire-module-filter-chain"), 5)
        self.assertIn("libpipewire-module-protocol-native", txt)
        self.assertIn("libpipewire-module-client-node", txt)
        self.assertIn("libpipewire-module-adapter", txt)
        for n, label in (("cs2", "CS2"), ("oyun", "Oyun"), ("muzik", "Müzik"), ("film", "Film"),
                         ("duz", "Düz")):
            self.assertIn(f'node.name        = "oto_eq_{n}"', txt)
            self.assertIn(f'node.name        = "oto_eq_{n}_out"', txt)
            self.assertIn(f'node.description = "Kulak Kesil · {label}"', txt)
        self.assertEqual(txt.count("media.class      = Audio/Sink"), 5)
        self.assertEqual(txt.count("node.passive     = true"), 5)
        self.assertEqual(txt.count('node.link-group  = "oto_eq"'), 5)
        self.assertEqual(txt.count("priority.session = 1\n"), 5)
        flat = txt.split('"Kulak Kesil · Düz"')[1]
        self.assertNotIn("bq_", flat)
        self.assertNotIn("lv2", flat)
        self.assertNotIn("target.object", txt)
        self.assertNotIn("filter.smart", txt)
        # braces balanced
        self.assertEqual(txt.count("{"), txt.count("}"))
        self.assertEqual(txt.count("["), txt.count("]"))

    def test_link_group_option(self):
        p = copy.deepcopy(self.p)
        p["genel"]["ortak_link_grubu"] = False
        txt = eqgen.generate_conf(p, lsp=True)
        for n in ALL_SINK_PROFILES:
            self.assertIn(f'node.link-group  = "oto_eq_{n}"', txt)
        self.assertTrue(eqgen.needs_restart(self.p, p))

    def test_dynamics_present_with_lsp(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        self.assertEqual(txt.count(eqgen.LSP_COMP_URI), 3)   # cs2, oyun (disabled), film
        self.assertIn('"sla" = 0.0', txt)

    def test_dynamics_omitted_without_plugin(self):
        with self.assertLogs("oto_eq.eqgen", "WARNING") as cm:
            txt = eqgen.generate_conf(self.p, lsp=False)
        self.assertNotIn("lv2", txt)
        self.assertTrue(any("lsp-plugins-lv2" in m for m in cm.output))
        self.assertEqual(txt.count("libpipewire-module-filter-chain"), 5)

    def test_every_link_refers_to_existing_node(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        for block in txt.split("libpipewire-module-filter-chain")[1:]:
            names = set(re.findall(r"name = (\w+) (?:label|plugin)", block))
            pairs = re.findall(r'output = "(\w+):[\w ]+" input = "(\w+):[\w ]+"', block)
            self.assertEqual(len(pairs), block.count("output = "))
            for a, b in pairs:
                self.assertIn(a, names)
                self.assertIn(b, names)

    def test_write_conf(self):
        with tempfile.TemporaryDirectory() as d:
            out = eqgen.write_conf(Path(d) / "x" / "s.conf", self.p)
            self.assertTrue(out.read_text().startswith("# Kulak Kesil"))


class SaveTests(unittest.TestCase):
    def test_round_trip(self):
        p = shipped()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiller.toml"
            eqgen.save_profiles(p, path)
            again = eqgen.load_profiles(path)
            self.assertEqual(again, p)
            # second save keeps a backup and is still equal
            again["profil"]["cs2"]["filtreler"][0]["kazanc"] = -4.5
            eqgen.save_profiles(again, path)
            self.assertTrue((Path(d) / "profiller.toml.bak").exists())
            self.assertEqual(eqgen.load_profiles(path), again)
            self.assertEqual(eqgen.load_profiles(Path(d) / "profiller.toml.bak"), p)

    def test_flat_section_round_trip(self):
        p = shipped()
        p[FLAT_PROFILE]["ek_kazanc_db"] = 1.5
        p[FLAT_PROFILE]["aktif"] = False
        with tempfile.TemporaryDirectory() as d:
            path = eqgen.save_profiles(p, Path(d) / "p.toml")
            self.assertIn("[duz]", path.read_text())
            self.assertEqual(eqgen.load_profiles(path), p)

    def test_toml_escaping(self):
        data = {"a": {"s": 'tırnak " ve \\ ters', "x": 1e-7, "n": 3, "b": False,
                      "l": [1.5, 2.0], "t": [{"k": "v"}]}}
        self.assertEqual(tomllib.loads(eqgen.dumps_toml(data)), data)

    def test_invalid_not_written(self):
        p = shipped()
        p["profil"]["cs2"]["filtreler"][0]["q"] = -1.0
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "p.toml"
            with self.assertRaises(eqgen.ProfileError):
                eqgen.save_profiles(p, path)
            self.assertFalse(path.exists())


class VariantTests(unittest.TestCase):
    def setUp(self):
        self.p = shipped()
        self.raw = tomllib.loads((config.DEFAULTS_DIR / config.PROFILES_FILE).read_text())

    def test_shipped_variants(self):
        from oto_eq.models import GAME_GROUPS, MUSIC_GROUPS
        self.assertEqual(eqgen.defined_groups(self.p, "muzik"), list(MUSIC_GROUPS))
        self.assertEqual(eqgen.defined_groups(self.p, "oyun"), list(GAME_GROUPS))
        for prof in ("muzik", "oyun"):
            for g, v in self.p["profil"][prof]["tur"].items():
                self.assertLessEqual(len(v["filtreler"]), 4)
                for f in v["filtreler"]:
                    self.assertLessEqual(abs(f["kazanc"]), 3.0, (prof, g))
                slots = eqgen.variant_slots(self.p, prof, g)
                self.assertEqual([x["tip"] for x in slots], ["LSC", "PK", "PK", "HSC"])

    def test_validation(self):
        bad = copy.deepcopy(self.raw)
        bad["profil"]["muzik"]["tur"]["elektronik"]["filtreler"].append(
            {"tip": "LSC", "fc": 40.0, "kazanc": 1.0, "q": 0.7})       # 2nd LSC: no slot
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)
        bad = copy.deepcopy(self.raw)
        bad["profil"]["cs2"]["tur"] = {"rekabetci": {}}
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)
        bad = copy.deepcopy(self.raw)
        bad["profil"]["muzik"]["tur"]["rekabetci"] = {}                # game group on music
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)
        bad = copy.deepcopy(self.raw)
        bad["profil"]["oyun"]["tur"]["yaris_sim"]["filtreler"][0]["kazanc"] = 7.0
        with self.assertRaises(eqgen.ProfileError):
            eqgen.normalize_profiles(bad)

    def test_old_user_copy_gets_default_variants(self):
        raw = copy.deepcopy(self.raw)
        for prof in ("muzik", "oyun"):
            del raw["profil"][prof]["tur"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiller.toml"
            path.write_text(eqgen.dumps_toml(raw), encoding="utf-8")
            self.assertEqual(eqgen.load_profiles(path)["profil"]["muzik"]["tur"],
                             self.p["profil"]["muzik"]["tur"])

    def test_variants_never_clip_and_stay_matched(self):
        for lsp in (True, False):
            st = eqgen.gain_staging(self.p, lsp)
            ceil = self.p["genel"]["tavan_dbfs"]
            louds = []
            for prof in ("cs2", "oyun", "muzik", "film", "duz"):
                for grp in [""] + eqgen.defined_groups(self.p, prof):
                    u = eqgen.staging_for(st, prof, grp)
                    resp = eqgen.response_db(eqgen.effective_filters(self.p, prof, grp), F)
                    d = eqgen._dyn_active(self.p, prof, lsp)
                    worst = (float(np.max(resp)) + u["preamp_db"] + (d["telafi_db"] if d else 0)
                             + u["match_db"])
                    if u["limiter"]:
                        self.assertLessEqual(u["curve_peak_dbfs"],
                                             ceil + u["limiter_margin_db"] + 1e-6, (prof, grp))
                    else:
                        self.assertLessEqual(worst, ceil + 1e-6, (prof, grp))
                    self.assertLessEqual(u["out_peak_dbfs"], ceil + 1e-6, (prof, grp))
                    louds.append(u["final_loudness_db"])
                    # the shared preamp never changes between variants
                    self.assertEqual(u["preamp_db"], st[prof]["preamp_db"])
            self.assertLessEqual(max(louds) - min(louds), 0.01)

    def test_conf_has_variant_slots(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        blocks = {n: b for n, b in zip(ALL_SINK_PROFILES, txt.split("libpipewire-module-filter-chain")[1:])}
        for prof in ALL_SINK_PROFILES:
            has = "name = v1_l label = bq_lowshelf" in blocks[prof]
            self.assertEqual(has, prof in ("muzik", "oyun"), prof)
        self.assertIn("name = v4_r label = bq_highshelf", blocks["muzik"])

    def test_variant_controls(self):
        neutral = eqgen.variant_controls(self.p, "muzik", "")
        self.assertEqual(set(neutral), {f"v{i}_{c}" for i in range(1, 5) for c in "lr"}
                         | {f"k{i}_{c}" for i in range(1, 4) for c in "lr"}
                         | {"cikis_l", "cikis_r"})
        self.assertTrue(all(neutral[f"v{i}_l"]["Gain"] == 0.0 for i in range(1, 5)))
        el = eqgen.variant_controls(self.p, "muzik", "elektronik")
        self.assertEqual(el["v1_l"]["Gain"], 2.5)
        self.assertEqual(el["v4_r"]["Freq"], 10000.0)
        st = eqgen.gain_staging(self.p)
        self.assertAlmostEqual(el["cikis_l"]["Mult"],
                               10 ** (st["muzik"]["variants"]["elektronik"]["match_db"] / 20))
        with self.assertRaises(ValueError):
            eqgen.variant_controls(self.p, "cs2", "")
        with self.assertRaises(ValueError):
            eqgen.variant_controls(self.p, "muzik", "rekabetci")

    def test_ramp_math(self):
        self.assertAlmostEqual(eqgen.ramp_value("v1_l:Freq", 100.0, 10000.0, 0.5), 1000.0)
        self.assertAlmostEqual(eqgen.ramp_value("v1_l:Gain", 0.0, 3.0, 0.5), 1.5)
        self.assertAlmostEqual(eqgen.ramp_value("cikis_l:Mult", 1.0, 0.01, 0.5), 0.1)
        self.assertEqual(eqgen.ramp_value("v1_l:Gain", 0.0, 3.0, 2.0), 3.0)
        s, e = eqgen.ramp_plan({"v1_l:Gain": 0.0, "v1_l:Freq": 100.0, "v2_l:Gain": 2.0,
                                "v2_l:Freq": 500.0},
                               {"v1_l:Gain": 2.0, "v1_l:Freq": 60.0, "v2_l:Gain": 0.0,
                                "v2_l:Freq": 1000.0})
        self.assertEqual(s["v1_l:Freq"], 60.0)     # silent slot jumps to the target
        self.assertEqual(e["v2_l:Freq"], 500.0)    # slot fading out stays in place

    def _fake_dump(self, group=""):
        flat = []
        for k, v in eqgen._flatten(eqgen.variant_controls(self.p, "muzik", group)).items():
            flat += [k, v]
        return [{"id": 77, "type": "PipeWire:Interface:Node",
                 "info": {"props": {"node.name": "oto_eq_muzik"},
                          "params": {"Props": [{"params": flat}]}}}]

    def test_apply_variant_ramp_and_cancel(self):
        calls = []
        with mock.patch.object(eqgen, "_pw_dump", return_value=self._fake_dump("")),                 mock.patch.object(eqgen, "_set_params",
                                  side_effect=lambda nid, v: calls.append(dict(v)) or True):
            self.assertTrue(eqgen.apply_variant("muzik", "elektronik",
                                                ramp_s=0.3, profiles=self.p))
            self.assertTrue(eqgen.apply_variant("muzik", "konusma", ramp_s=0.2,
                                                profiles=self.p))   # cancels the first
            eqgen.wait_ramps(3)
        want = eqgen._flatten(eqgen.variant_controls(self.p, "muzik", "konusma"))
        last = calls[-1]
        for k, v in want.items():
            self.assertAlmostEqual(last[k], v, places=6)
        self.assertGreater(len(calls), 3)               # really ramped in steps
        mids = [c["v2_l:Gain"] for c in calls]
        self.assertTrue(all(-0.01 <= g <= 2.01 for g in mids))

    def test_apply_variant_missing_slots(self):
        dump = self._fake_dump("")
        dump[0]["info"]["params"]["Props"][0]["params"] = ["b1_l:Gain", 1.0]
        with mock.patch.object(eqgen, "_pw_dump", return_value=dump),                 mock.patch.object(eqgen, "_set_params") as sp,                 self.assertLogs("oto_eq.eqgen", "WARNING"):
            self.assertFalse(eqgen.apply_variant("muzik", "elektronik", profiles=self.p))
        sp.assert_not_called()


class RestartTests(unittest.TestCase):
    def test_needs_restart(self):
        p = shipped()
        q = copy.deepcopy(p)
        q["profil"]["cs2"]["filtreler"][0]["kazanc"] = -3.0
        q["profil"]["cs2"]["filtreler"][1]["fc"] = 300.0
        q["profil"]["film"]["dinamik"]["oran"] = 4.0
        q["profil"]["oyun"]["dinamik"]["aktif"] = True
        q["duzeltme"]["aktif"] = False
        q["genel"]["pay_db"] = 2.0
        self.assertFalse(eqgen.needs_restart(p, q, conf_path=None))
        self.assertFalse(eqgen.needs_restart(p, q, conf_path=Path("/nonexistent/x.conf")))
        r = copy.deepcopy(p)
        r["profil"]["muzik"]["filtreler"].append({"tip": "PK", "fc": 5000.0, "kazanc": 1.0, "q": 1.0})
        self.assertTrue(eqgen.needs_restart(p, r))
        r = copy.deepcopy(p)
        r["profil"]["cs2"]["filtreler"][0]["tip"] = "PK"
        self.assertTrue(eqgen.needs_restart(p, r))
        r = copy.deepcopy(p)
        del r["profil"]["film"]["dinamik"]
        self.assertTrue(eqgen.needs_restart(p, r))


class SetLiveTests(unittest.TestCase):
    def test_set_live_builds_params(self):
        p = shipped()
        calls = []
        fake_dump = []
        names = {}
        for i, n in enumerate(ALL_SINK_PROFILES):
            ctr = eqgen.node_controls(p, n, lsp=True)
            flat = []
            for node, c in ctr.items():
                for k, v in c.items():
                    flat += [f"{node}:{k}", v]
            fake_dump.append({"id": 100 + i, "type": "PipeWire:Interface:Node",
                              "info": {"props": {"node.name": "oto_eq_" + n},
                                       "params": {"Props": [{"params": flat}]}}})
            names[100 + i] = n

        def fake_run(cmd, timeout=5.0):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout="", stderr="")

        q = copy.deepcopy(p)
        q["profil"]["cs2"]["filtreler"][2]["kazanc"] = 3.0
        with mock.patch.object(eqgen, "_pw_dump", return_value=fake_dump), \
                mock.patch.object(eqgen, "_run", side_effect=fake_run), \
                mock.patch.object(eqgen, "lsp_available", return_value=True):
            self.assertTrue(eqgen.set_live("cs2", q))
            self.assertEqual(len(calls), 5)
            flat = next(c for c in calls if c[2] == "104")
            self.assertIn("cikis_l:Mult", flat[4])
            cs2 = next(c for c in calls if c[2] == "100")
            self.assertIn('"b3_l:Gain" 3.0', cs2[4])
            self.assertIn('"b3_r:Gain" 3.0', cs2[4])
            other = next(c for c in calls if c[2] == "101")
            self.assertNotIn("b1_l", other[4])
            muz = next(c for c in calls if c[2] == "102")        # muzik: variant untouched
            self.assertNotIn("v1_l", muz[4])
            self.assertNotIn("cikis_l", muz[4])
            calls.clear()
            self.assertTrue(eqgen.set_live("muzik", q, variants={"muzik": "elektronik"}))
            muz = next(c for c in calls if c[2] == "102")
            self.assertIn('"v1_l:Gain" 2.5', muz[4])
            # structural change -> refused, nothing written
            calls.clear()
            r = copy.deepcopy(p)
            r["profil"]["cs2"]["filtreler"].append({"tip": "PK", "fc": 900.0, "kazanc": 1.0, "q": 1.0})
            with self.assertLogs("oto_eq.eqgen", "WARNING"):
                self.assertFalse(eqgen.set_live("cs2", r))
            self.assertEqual(calls, [])


class ToneTests(unittest.TestCase):
    """Kişisel ton (CHANGE 5): k1..k3 bands, headroom, ramp, restart detection."""

    def setUp(self):
        self.p = shipped()
        self.file = Path(_TMP.name) / "kisisel.json"
        self.file.unlink(missing_ok=True)
        self.addCleanup(self.file.unlink, missing_ok=True)

    def test_profile_defaults(self):
        self.assertFalse(self.p["profil"]["cs2"]["kisisel_ton"])
        for n in ("oyun", "muzik", "film"):
            self.assertTrue(self.p["profil"][n]["kisisel_ton"], n)
            self.assertTrue(eqgen.tone_enabled(self.p, n))
        self.assertFalse(eqgen.tone_enabled(self.p, "cs2"))
        self.assertFalse(eqgen.tone_enabled(self.p, FLAT_PROFILE))
        q = copy.deepcopy(self.p)
        q["profil"]["film"]["aktif"] = False
        self.assertFalse(eqgen.tone_enabled(q, "film"))
        raw = tomllib.loads(eqgen.dumps_toml(self.p))       # survives save/load
        self.assertFalse(eqgen.normalize_profiles(raw)["profil"]["cs2"]["kisisel_ton"])

    def test_conf_has_k_bands_everywhere_but_duz(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        blocks = dict(zip(ALL_SINK_PROFILES, txt.split("libpipewire-module-filter-chain")[1:]))
        for prof in ALL_SINK_PROFILES:
            want = prof != FLAT_PROFILE
            self.assertEqual("name = k1_l label = bq_lowshelf" in blocks[prof], want, prof)
            self.assertEqual("name = k2_r label = bq_peaking" in blocks[prof], want, prof)
            self.assertEqual("name = k3_r label = bq_highshelf" in blocks[prof], want, prof)
        # chain order: ... v4 -> k1 -> k2 -> k3 -> comp / cikis
        self.assertIn('{ output = "v4_l:Out" input = "k1_l:In" }', blocks["muzik"])
        self.assertIn('{ output = "k3_l:Out" input = "xd_l:In" }', blocks["muzik"])   # crossfeed
        self.assertIn('{ output = "k3_l:Out" input = "comp:in_l" }', blocks["oyun"])
        nb = len(self.p["profil"]["cs2"]["filtreler"])
        self.assertIn(f'{{ output = "b{nb}_r:Out" input = "k1_r:In" }}', blocks["cs2"])
        self.assertIn('{ output = "k3_r:Out" input = "comp:in_r" }', blocks["cs2"])
        self.assertIn("kişisel ton: k1..k3", txt)
        self.assertNotIn("cs2", txt.split("açık profiller:")[1].split(";")[0])

    def test_node_controls_follow_kisisel_json(self):
        kisisel.set_genel(bas=1.5, vokal=-1.0)
        kisisel.set_tur("muzik", "elektronik", bas=1.0, parlaklik=2.0)
        c = eqgen.node_controls(self.p, "muzik", lsp=True, group="elektronik")
        self.assertEqual((c["k1_l"]["Gain"], c["k2_r"]["Gain"], c["k3_l"]["Gain"]), (2.5, -1.0, 2.0))
        self.assertEqual((c["k1_l"]["Freq"], c["k1_l"]["Q"]), (120.0, 0.7))
        self.assertEqual((c["k2_l"]["Freq"], c["k2_l"]["Q"]), (2500.0, 0.8))
        self.assertEqual((c["k3_l"]["Freq"], c["k3_l"]["Q"]), (8000.0, 0.7))
        self.assertEqual(eqgen.node_controls(self.p, "muzik", lsp=True)["k1_l"]["Gain"], 1.5)
        self.assertEqual(eqgen.node_controls(self.p, "film", lsp=True)["k1_l"]["Gain"], 1.5)
        cs2 = eqgen.node_controls(self.p, "cs2", lsp=True)        # opted out -> 0 dB
        self.assertTrue(all(cs2[f"k{i}_{ch}"]["Gain"] == 0.0 for i in (1, 2, 3) for ch in "lr"))
        q = copy.deepcopy(self.p)
        q["profil"]["cs2"]["kisisel_ton"] = True
        self.assertEqual(eqgen.node_controls(q, "cs2", lsp=True)["k1_r"]["Gain"], 1.5)
        # explicit (partial) values override the file; disabled profiles stay 0
        c = eqgen.node_controls(self.p, "film", lsp=True, tone={"vokal": 2.0})
        self.assertEqual((c["k1_l"]["Gain"], c["k2_l"]["Gain"]), (1.5, 2.0))
        c = eqgen.node_controls(self.p, "cs2", lsp=True, tone={"vokal": 2.0})
        self.assertEqual(c["k2_l"]["Gain"], 0.0)
        with self.assertRaises(ValueError):
            eqgen.tone_values(self.p, "film", "", {"tiz": 1.0})
        self.assertEqual(eqgen.tone_values(self.p, "film", "", {"bas": 9})["bas"], 3.0)

    def test_tone_comp_keeps_loudness(self):
        g = self.p["genel"]
        band = np.geomspace(g["esitleme_alt_hz"], g["esitleme_ust_hz"], 400)
        for prof in ("muzik", "film", "oyun"):
            base = eqgen._pink_loudness(eqgen.total_response(self.p, prof, band, True))
            for tone in ({"vokal": 3.0}, {"vokal": -1.5, "bas": 2.0}, {"parlaklik": -3.0}):
                tv = eqgen.tone_values(self.p, prof, "", tone)
                got = eqgen._pink_loudness(eqgen.total_response(self.p, prof, band, True,
                                                                 include_dynamics=False, tone=tv))
                ref = eqgen._pink_loudness(eqgen.total_response(self.p, prof, band, True,
                                                                include_dynamics=False))
                self.assertAlmostEqual(got, ref, places=6)
            self.assertTrue(np.isfinite(base))
        self.assertLess(eqgen.tone_comp_db(self.p, "muzik", "", {"vokal": 3.0}), -1.0)
        self.assertGreater(eqgen.tone_comp_db(self.p, "muzik", "", {"vokal": -3.0}), 1.0)
        with mock.patch.object(eqgen, "TONE_COMP_MAX_BOOST_DB", 0.0):
            self.assertEqual(eqgen.tone_comp_db(self.p, "muzik", "", {"vokal": -3.0}), 0.0)
        q = copy.deepcopy(self.p)
        q["genel"]["ses_esitleme"] = False
        self.assertEqual(eqgen.tone_comp_db(q, "muzik", "", {"vokal": 3.0}), 0.0)

    def _check_headroom(self, p):
        """Every tone in ±TON_LIMIT (0.5 dB grid) on every unit: no clip, ceiling kept."""
        st = eqgen.gain_staging(p, True)
        pay, ceil = p["genel"]["pay_db"], p["genel"]["tavan_dbfs"]
        lim = kisisel.TON_LIMIT
        grid = np.arange(-lim, lim + 0.01, 0.5)
        for prof in EQ_PROFILES:
            if not eqgen.tone_enabled(p, prof):
                continue
            d = eqgen._dyn_active(p, prof, True)
            for grp in [""] + eqgen.defined_groups(p, prof):
                u = eqgen.staging_for(st, prof, grp)
                flt = eqgen.effective_filters(p, prof, grp)
                for b in grid[::3]:
                    for v in grid[::2]:
                        for h in grid[::3]:
                            tv = {"bas": float(b), "vokal": float(v), "parlaklik": float(h)}
                            pk = eqgen.peak_db(flt + eqgen.tone_filters(tv))
                            m = 0.0
                            self.assertLessEqual(pk + u["preamp_db"], -pay + m + 1e-6,
                                                 (prof, grp, tv))
                            comp = eqgen.tone_comp_db(p, prof, grp, tv)
                            worst = (pk + u["preamp_db"] + (d["telafi_db"] if d else 0.0)
                                     + u["match_db"] + comp)
                            self.assertLessEqual(worst, ceil + m + 1e-6, (prof, grp, tv))

    def test_headroom_reserved_for_tone(self):
        # the static tone reserve applies to sinks WITHOUT a limiter (with one, the
        # limiter catches tone peaks: see LevelLimiterTests)
        p = _without_new_blocks(self.p)
        self._check_headroom(p)
        q = copy.deepcopy(p)
        q["genel"]["ses_esitleme"] = False
        self._check_headroom(q)
        with mock.patch.object(eqgen, "TONE_COMP_MAX_BOOST_DB", 0.0):
            self._check_headroom(p)
        st = eqgen.gain_staging(self.p, True)       # limited: never above tavan anyway
        for prof in EQ_PROFILES:
            self.assertLessEqual(st[prof]["out_peak_dbfs"], self.p["genel"]["tavan_dbfs"] + 1e-6)

    def test_preamp_constant_whatever_the_tone(self):
        st0 = eqgen.gain_staging(self.p, True)
        kisisel.set_genel(bas=3.0, vokal=3.0, parlaklik=3.0)
        kisisel.set_tur("muzik", "elektronik", bas=-6.0)
        st1 = eqgen.gain_staging(self.p, True)
        for prof in EQ_PROFILES:
            self.assertEqual(st0[prof]["preamp_db"], st1[prof]["preamp_db"])
            self.assertEqual(st0[prof]["match_db"], st1[prof]["match_db"])
        a = eqgen.node_controls(self.p, "muzik", st1, True, group="elektronik")
        self.assertEqual(a["pre_l"]["Mult"], 10 ** (st0["muzik"]["preamp_db"] / 20))
        # tone reserve: oyun/muzik/film peak grows by >= TON_LIMIT, cs2 (opted out) not
        for prof in ("oyun", "muzik", "film"):
            self.assertGreaterEqual(st0[prof]["tone_peak_db"] - st0[prof]["peak_db"],
                                    kisisel.TON_LIMIT - 1.6, prof)
        self.assertEqual(st0["cs2"]["tone_peak_db"], st0["cs2"]["peak_db"])

    def test_tone_controls(self):
        film = eqgen.tone_controls(self.p, "film")
        self.assertEqual(set(film), {f"k{i}_{c}" for i in (1, 2, 3) for c in "lr"}
                         | {"cikis_l", "cikis_r"})
        muz = eqgen.tone_controls(self.p, "muzik", "elektronik")
        self.assertEqual(muz, eqgen.variant_controls(self.p, "muzik", "elektronik"))
        with self.assertRaises(ValueError):
            eqgen.tone_controls(self.p, FLAT_PROFILE)
        with self.assertRaises(ValueError):
            eqgen.tone_controls(self.p, "muzik", "rekabetci")

    def _dump(self, profile, ctrls, nid=55):
        flat = []
        for k, v in eqgen._flatten(ctrls).items():
            flat += [k, v]
        return [{"id": nid, "type": "PipeWire:Interface:Node",
                 "info": {"props": {"node.name": "oto_eq_" + profile},
                          "params": {"Props": [{"params": flat}]}}}]

    def test_apply_tone_ramps_and_values_none_restores_file(self):
        kisisel.set_genel(vokal=1.0)
        dump = self._dump("film", eqgen.tone_controls(self.p, "film", tone={"vokal": 0.0}))
        calls = []
        with mock.patch.object(eqgen, "_pw_dump", return_value=dump), \
                mock.patch.object(eqgen, "_set_params",
                                  side_effect=lambda nid, v: calls.append(dict(v)) or True):
            self.assertTrue(eqgen.apply_tone("film", values={"vokal": 2.5, "bas": -1.0},
                                             ramp_s=0.15, profiles=self.p))
            eqgen.wait_ramps(3)
            want = eqgen._flatten(eqgen.tone_controls(self.p, "film",
                                                      tone={"vokal": 2.5, "bas": -1.0}))
            for k, v in want.items():
                self.assertAlmostEqual(calls[-1][k], v, places=6)
            self.assertGreater(len(calls), 2)
            self.assertTrue(all(0.0 - 1e-9 <= c["k2_l:Gain"] <= 2.5 + 1e-9 for c in calls))
            self.assertEqual(calls[-1]["k3_l:Gain"], 0.0)            # parlaklik from file
            calls.clear()
            self.assertTrue(eqgen.apply_tone("film", ramp_s=0, profiles=self.p))   # file values
            eqgen.wait_ramps(3)
        self.assertEqual(calls[-1]["k2_r:Gain"], 1.0)
        self.assertEqual(calls[-1]["k1_r:Gain"], 0.0)
        self.assertAlmostEqual(calls[-1]["cikis_l:Mult"], 10 ** (
            (eqgen.gain_staging(self.p)["film"]["match_db"]
             + eqgen.tone_comp_db(self.p, "film", "", {"vokal": 1.0})) / 20))

    def test_apply_tone_cancels_variant_ramp_and_completes_it(self):
        kisisel.set_tur("muzik", "elektronik", parlaklik=-2.0)
        dump = self._dump("muzik", eqgen.variant_controls(self.p, "muzik", ""))
        calls = []
        with mock.patch.object(eqgen, "_pw_dump", return_value=dump), \
                mock.patch.object(eqgen, "_set_params",
                                  side_effect=lambda nid, v: calls.append(dict(v)) or True):
            self.assertTrue(eqgen.apply_variant("muzik", "elektronik", ramp_s=1.0,
                                                profiles=self.p))
            self.assertTrue(eqgen.apply_tone("muzik", "elektronik", ramp_s=0.1,
                                             values={"bas": 1.0}, profiles=self.p))
            eqgen.wait_ramps(3)
        want = eqgen._flatten(eqgen.variant_controls(self.p, "muzik", "elektronik",
                                                     tone={"bas": 1.0}))
        for k, v in want.items():
            self.assertAlmostEqual(calls[-1][k], v, places=6)
        self.assertEqual(calls[-1]["v1_l:Gain"], 2.5)        # the variant got completed
        self.assertEqual(calls[-1]["k3_l:Gain"], -2.0)       # genre offset from the file
        self.assertEqual(calls[-1]["k1_l:Gain"], 1.0)

    def test_old_graph_without_k_bands(self):
        old = {k: v for k, v in eqgen.variant_controls(self.p, "muzik", "").items()
               if not k.startswith("k")}
        dump = self._dump("muzik", old)
        kisisel.set_genel(bas=2.0)
        calls = []
        with mock.patch.object(eqgen, "_pw_dump", return_value=dump), \
                mock.patch.object(eqgen, "_set_params",
                                  side_effect=lambda nid, v: calls.append(dict(v)) or True):
            with self.assertLogs("oto_eq.eqgen", "WARNING"):      # variant still works
                self.assertTrue(eqgen.apply_variant("muzik", "elektronik", ramp_s=0,
                                                    profiles=self.p))
            eqgen.wait_ramps(3)
            self.assertTrue(calls)
            self.assertFalse(any(k.startswith("k") for c in calls for k in c))
            self.assertAlmostEqual(calls[-1]["cikis_l:Mult"], 10 ** (
                eqgen.gain_staging(self.p)["muzik"]["variants"]["elektronik"]["match_db"] / 20))
            calls.clear()
            with self.assertLogs("oto_eq.eqgen", "WARNING"):      # tone needs the restart
                self.assertFalse(eqgen.apply_tone("muzik", "elektronik", profiles=self.p))
        self.assertEqual(calls, [])

    def test_needs_restart_detects_old_conf(self):
        d = Path(_TMP.name)
        new = d / "yeni.conf"
        new.write_text(eqgen.generate_conf(self.p, lsp=True), encoding="utf-8")
        old = d / "eski.conf"
        old.write_text("\n".join(ln for ln in new.read_text(encoding="utf-8").splitlines()
                                 if "name = k" not in ln), encoding="utf-8")
        self.assertFalse(eqgen.conf_outdated(new))
        self.assertTrue(eqgen.conf_outdated(old))
        self.assertFalse(eqgen.conf_outdated(d / "yok.conf"))
        self.assertFalse(eqgen.needs_restart(self.p, self.p, conf_path=new))
        self.assertTrue(eqgen.needs_restart(self.p, self.p, conf_path=old))
        with mock.patch.object(config, "SINKS_CONF", old):          # default = SINKS_CONF
            self.assertTrue(eqgen.needs_restart(self.p, self.p))
        q = copy.deepcopy(self.p)
        q["profil"]["cs2"]["kisisel_ton"] = True                    # toggle = live, no restart
        self.assertFalse(eqgen.needs_restart(self.p, q, conf_path=new))

    def test_set_live_leaves_unlisted_variant_tone_alone(self):
        kisisel.set_genel(bas=1.0)
        fake_dump, calls = [], []
        for i, n in enumerate(ALL_SINK_PROFILES):
            flat = []
            for node, c in eqgen.node_controls(self.p, n, lsp=True).items():
                for k, v in c.items():
                    flat += [f"{node}:{k}", v]
            fake_dump.append({"id": 100 + i, "type": "PipeWire:Interface:Node",
                              "info": {"props": {"node.name": "oto_eq_" + n},
                                       "params": {"Props": [{"params": flat}]}}})
        with mock.patch.object(eqgen, "_pw_dump", return_value=fake_dump), \
                mock.patch.object(eqgen, "_run", side_effect=lambda cmd, timeout=5.0:
                                  calls.append(cmd) or mock.Mock(returncode=0, stdout="",
                                                                 stderr="")), \
                mock.patch.object(eqgen, "lsp_available", return_value=True):
            self.assertTrue(eqgen.set_live("muzik", self.p))
            muz = next(c for c in calls if c[2] == "102")
            self.assertNotIn("k1_l", muz[4])
            self.assertNotIn("v1_l", muz[4])
            calls.clear()
            self.assertTrue(eqgen.set_live("film", self.p))
            film = next(c for c in calls if c[2] == "103")
            self.assertIn('"k1_l:Gain" 1.0', film[4])

    def test_staging_speed(self):
        import time
        t = time.perf_counter()
        eqgen.gain_staging(self.p, True)
        self.assertLess(time.perf_counter() - t, 1.0)


def _without_new_blocks(p: dict) -> dict:
    q = copy.deepcopy(p)
    for n in EQ_PROFILES:
        for b in eqgen.NEW_BLOCKS:
            q["profil"][n].pop(b, None)
    return q


class LevelLimiterTests(unittest.TestCase):
    """CHANGE 6 (D1): limiter, loudness leveller (autogain), crossfeed."""

    def setUp(self):
        self.p = shipped()
        self.raw = tomllib.loads((config.DEFAULTS_DIR / config.PROFILES_FILE).read_text())

    def test_shipped_defaults(self):
        pr = self.p["profil"]
        self.assertLessEqual(pr["cs2"]["sinirlayici"]["ileri_bakis_ms"], 0.1)   # 4 samples
        self.assertNotIn("seviye", pr["cs2"])
        for n in EQ_PROFILES:
            self.assertTrue(pr[n]["sinirlayici"]["aktif"], n)
            self.assertGreaterEqual(pr[n]["sinirlayici"]["marj_db"], 4.0)
            self.assertFalse(pr[n]["sinirlayici"]["seviye_duzenleme"], n)  # ALR cost ~1.5 dB
        self.assertTrue(pr["film"]["seviye"]["aktif"])
        self.assertEqual(pr["film"]["seviye"]["hedef_lufs"], -16.0)
        self.assertLessEqual(pr["film"]["seviye"]["dusme_db_sn"], 0.5)        # (almost) boost only
        self.assertFalse(pr["muzik"]["seviye"]["aktif"])         # "ses az geliyor" fix
        self.assertFalse(pr["oyun"]["seviye"]["aktif"])
        self.assertFalse(pr["muzik"]["crossfeed"]["aktif"])
        self.assertEqual(pr["muzik"]["seviye"]["hedef_lufs"], self.p["genel"]["referans_seviye_db"])

    def test_at_least_3db_louder_and_still_matched(self):
        st = eqgen.gain_staging(self.p, True)
        old = eqgen.gain_staging(_without_new_blocks(self.p), True)
        new_l = st[FLAT_PROFILE]["match_db"]
        old_l = old[FLAT_PROFILE]["match_db"]
        self.assertGreaterEqual(new_l - old_l, 7.0, (old_l, new_l))   # -12.4 -> -4.3 dB
        self.assertGreaterEqual(new_l, -5.0)
        louds = [eqgen.staging_for(st, n, g)["final_loudness_db"] for n in EQ_PROFILES
                 for g in [""] + eqgen.defined_groups(self.p, n)]
        louds.append(st[FLAT_PROFILE]["final_loudness_db"])
        self.assertLessEqual(max(louds) - min(louds), 0.01, louds)
        # nothing the headset gets can exceed tavan; cs2 (no limiter) statically below it
        ceil = self.p["genel"]["tavan_dbfs"]
        for n in ALL_SINK_PROFILES:
            for g in [""] + eqgen.defined_groups(self.p, n):
                u = eqgen.staging_for(st, n, g)
                self.assertLessEqual(u["out_peak_dbfs"], ceil + 1e-6, (n, g))
                if n != FLAT_PROFILE:
                    self.assertTrue(u["limiter"])
                    self.assertLessEqual(u["curve_peak_dbfs"], ceil + u["limiter_margin_db"] + 1e-6)
        # without the plugin: no limiter -> exactly the old staging
        nolsp = eqgen.gain_staging(self.p, False)
        self.assertAlmostEqual(nolsp[FLAT_PROFILE]["match_db"],
                               eqgen.gain_staging(_without_new_blocks(self.p), False)
                               [FLAT_PROFILE]["match_db"], places=9)

    def test_margin_and_disabled_limiter(self):
        q = copy.deepcopy(self.p)
        for n in EQ_PROFILES:
            q["profil"][n]["sinirlayici"]["aktif"] = False
        st = eqgen.gain_staging(q, True)
        old = eqgen.gain_staging(_without_new_blocks(self.p), True)
        self.assertAlmostEqual(st[FLAT_PROFILE]["match_db"], old[FLAT_PROFILE]["match_db"],
                               delta=0.5)      # only the leveller's compressor ref may differ
        for n in EQ_PROFILES:                  # no limiter -> full static guarantee again
            self.assertLessEqual(st[n]["worst_peak_dbfs"], q["genel"]["tavan_dbfs"] + 1e-6)
        self.assertFalse(eqgen.needs_restart(self.p, q, conf_path=None))   # aktif = live
        q = copy.deepcopy(self.p)
        q["genel"]["ses_esitleme"] = False
        st = eqgen.gain_staging(q, True)
        for n in EQ_PROFILES:
            u = st[n]
            self.assertLessEqual(u["curve_peak_dbfs"], q["genel"]["tavan_dbfs"]
                                 + u["limiter_margin_db"] + 1e-6, n)
            self.assertLessEqual(u["preamp_db"], 0.0)

    def test_validation(self):
        cases = [("cs2", "crossfeed", {"aktif": True}),                 # muzik only
                 ("film", "sinirlayici", {"marj_db": 13.0}),
                 ("film", "sinirlayici", {"ileri_bakis_ms": 0.0}),
                 ("film", "sinirlayici", {"aktif": 1}),                 # bool required
                 ("film", "seviye", {"hiz": 1.0}),                      # unknown key
                 ("film", "seviye", {"dusme_db_sn": 0.0}),
                 ("film", "seviye", {"hedef_lufs": -3.0}),
                 ("muzik", "crossfeed", {"kesim_hz": 50.0}),
                 ("muzik", "crossfeed", {"seviye_db": "x"})]
        for prof, blk, vals in cases:
            bad = copy.deepcopy(self.raw)
            bad["profil"][prof].setdefault(blk, {}).update(vals)
            with self.assertRaises(eqgen.ProfileError, msg=(prof, blk, vals)):
                eqgen.normalize_profiles(bad)
        ok = copy.deepcopy(self.raw)
        ok["profil"]["film"]["seviye"]["hedef_lufs"] = -18
        self.assertEqual(eqgen.normalize_profiles(ok)["profil"]["film"]["seviye"]["hedef_lufs"],
                         -18.0)

    def test_leveller_target_drives_compressor_reference(self):
        q = copy.deepcopy(self.p)
        q["profil"]["film"]["seviye"]["hedef_lufs"] = -12.0
        a = eqgen.gain_staging(self.p, True)["film"]["dyn_ref_gain_db"]
        b = eqgen.gain_staging(q, True)["film"]["dyn_ref_gain_db"]
        self.assertLess(b, a)                 # hotter content -> more gain reduction
        r = copy.deepcopy(self.p)
        r["profil"]["film"]["seviye"]["hedef_lufs"] = self.p["genel"]["referans_seviye_db"]
        c = eqgen.gain_staging(r, True)["film"]["dyn_ref_gain_db"]
        q["profil"]["film"]["seviye"]["aktif"] = False   # off -> genel.referans_seviye_db
        self.assertAlmostEqual(eqgen.gain_staging(q, True)["film"]["dyn_ref_gain_db"], c)

    def test_conf_structure(self):
        txt = eqgen.generate_conf(self.p, lsp=True)
        self.assertIn(f"{eqgen.CONF_VERSION_MARK} {eqgen.CONF_VERSION}", txt)
        blocks = dict(zip(ALL_SINK_PROFILES, txt.split("libpipewire-module-filter-chain")[1:]))
        for n in ("oyun", "muzik", "film"):
            b = blocks[n]
            self.assertIn('"lk" = 1.5', b)
            self.assertEqual(b.count(eqgen.LSP_LIM_URI), 1, n)
            self.assertEqual(b.count(eqgen.LSP_AG_URI), 1, n)
            self.assertIn('outputs = [ "lim:out_l" "lim:out_r" ]', b)
            self.assertIn('inputs = [ "ag:in_l" "ag:in_r" ]', b)
            self.assertIn('{ output = "cikis_l:Out" input = "lim:in_l" }', b)
            self.assertIn('{ output = "ag:out_r" input = "pre_r:In" }', b)
            self.assertIn('"boost" = 0.0', b)                  # no hidden makeup gain
            self.assertIn(f'"th" = {eqgen._fmt(10 ** (-0.5 / 20))}', b)
            self.assertIn('"lkahead" = 0.0', b)
        self.assertEqual(blocks["cs2"].count(eqgen.LSP_LIM_URI), 1)
        self.assertIn('"lk" = 0.1', blocks["cs2"])
        self.assertIn('"alr" = 0.0', blocks["cs2"])
        self.assertNotIn("autogain", blocks["cs2"])
        self.assertIn('outputs = [ "lim:out_l" "lim:out_r" ]', blocks["cs2"])
        self.assertIn('"enabled" = 0.0', blocks["oyun"].split("autogain")[1])   # off, present
        self.assertIn("label = mixer", blocks["muzik"])
        self.assertIn('label = delay config = { "max-delay" = 0.002 }', blocks["muzik"])
        self.assertNotIn("label = mixer", blocks["film"])
        self.assertNotIn("lv2", blocks[FLAT_PROFILE])
        self.assertNotIn("node.autoconnect", txt)
        olc = eqgen.generate_conf(self.p, lsp=True, olcum=True)
        self.assertEqual(olc.count("node.autoconnect = false"), 5)
        with self.assertLogs("oto_eq.eqgen", "WARNING"):
            txt = eqgen.generate_conf(self.p, lsp=False)
        self.assertNotIn("lv2", txt)
        self.assertIn("label = mixer", txt)                     # crossfeed needs no plugin

    def test_wireplumber_never_restores_sink_volume(self):
        # WirePlumber saved the kısma -6 dB of oyun/muzik/film and restored it at every
        # start; every sink node (capture + playback side) must opt out.
        for olcum in (False, True):
            txt = eqgen.generate_conf(self.p, lsp=True, olcum=olcum)
            for n, b in zip(ALL_SINK_PROFILES, txt.split("libpipewire-module-filter-chain")[1:]):
                cap = b.split("capture.props")[1].split("}")[0]
                out = b.split("playback.props")[1].split("}")[0]
                self.assertIn("state.restore-props = false", cap, n)
                self.assertIn("state.restore-props = false", out, n)

    def test_restart_rules_and_old_conf(self):
        for prof, blk in (("cs2", "sinirlayici"), ("film", "seviye"), ("muzik", "crossfeed")):
            q = copy.deepcopy(self.p)
            if blk in q["profil"][prof]:
                del q["profil"][prof][blk]
            else:
                q["profil"][prof][blk] = dict(eqgen.SINIRLAYICI_DEFAULTS)
            self.assertTrue(eqgen.needs_restart(self.p, q, conf_path=None), (prof, blk))
        q = copy.deepcopy(self.p)
        q["profil"]["muzik"]["crossfeed"]["aktif"] = True
        q["profil"]["film"]["seviye"]["en_cok_yukseltme_db"] = 3.0
        q["profil"]["oyun"]["sinirlayici"]["marj_db"] = 2.0
        self.assertFalse(eqgen.needs_restart(self.p, q, conf_path=None))
        d = Path(_TMP.name)
        new = d / "v6.conf"
        new.write_text(eqgen.generate_conf(self.p, lsp=True), encoding="utf-8")
        old = d / "v5.conf"
        old.write_text(new.read_text(encoding="utf-8").replace(eqgen.CONF_VERSION_MARK, "# x"),
                       encoding="utf-8")
        self.assertFalse(eqgen.conf_outdated(new))
        self.assertTrue(eqgen.conf_outdated(old))
        self.assertTrue(eqgen.needs_restart(self.p, self.p, conf_path=old))
        self.assertEqual(eqgen.conf_version(new.read_text(encoding="utf-8")), eqgen.CONF_VERSION)

    def test_old_user_copy_gets_new_blocks_but_choices_are_kept(self):
        raw = copy.deepcopy(self.raw)
        for n in EQ_PROFILES:
            for b in eqgen.NEW_BLOCKS:
                raw["profil"][n].pop(b, None)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "profiller.toml"
            path.write_text(eqgen.dumps_toml(raw), encoding="utf-8")
            p = eqgen.load_profiles(path)
            for n in EQ_PROFILES:
                self.assertIn("sinirlayici", p["profil"][n])
            self.assertIn("crossfeed", p["profil"]["muzik"])
            self.assertEqual(p["profil"]["film"]["seviye"]["hedef_lufs"], -16.0)
            # the live user file (25 Eki gece): only "[profil.muzik.seviye] aktif = false"
            # -> muzik keeps aktif=false, the other keys + film/oyun blocks from shipped
            raw2 = copy.deepcopy(raw)
            raw2["profil"]["muzik"]["seviye"] = {"aktif": False}
            raw2["profil"]["film"]["seviye"] = {"en_cok_yukseltme_db": 4.0}
            path.write_text(eqgen.dumps_toml(raw2), encoding="utf-8")
            p = eqgen.load_profiles(path)
            self.assertFalse(p["profil"]["muzik"]["seviye"]["aktif"])
            self.assertEqual(p["profil"]["muzik"]["seviye"]["olcum_ms"], 2000.0)
            self.assertEqual(p["profil"]["film"]["seviye"]["en_cok_yukseltme_db"], 4.0)  # user
            self.assertEqual(p["profil"]["film"]["seviye"]["hedef_lufs"], -16.0)         # shipped
            self.assertTrue(p["profil"]["film"]["seviye"]["aktif"])
            self.assertIn("sinirlayici", p["profil"]["cs2"])
            # save -> reload round trip keeps the blocks
            eqgen.save_profiles(self.p, path)
            self.assertEqual(eqgen.load_profiles(path)["profil"], self.p["profil"])

    def test_crossfeed_math(self):
        xf = dict(eqgen.CROSSFEED_DEFAULTS, aktif=True)
        f = np.array([20.0, 50.0, 100.0])
        hd, hc = eqgen.crossfeed_response(xf, f)
        ild = 20 * np.log10(np.abs(hc) / np.abs(hd))
        self.assertTrue(np.allclose(ild, -xf["seviye_db"], atol=0.2), ild)   # bs2b "feed"
        mono = eqgen.crossfeed_mono_db(xf, F)
        self.assertLess(abs(mono[0]), 0.05)                     # bass: flat for centre
        self.assertLess(abs(mono[-1]), 0.05)                    # treble: direct only
        self.assertGreater(mono.min(), -2.0)                    # crossover dip is mild
        self.assertLessEqual(eqgen.crossfeed_worst_db(xf), 2.0)
        self.assertGreaterEqual(eqgen.crossfeed_worst_db(xf), 0.0)
        on = eqgen.xf_controls(xf, True)
        off = eqgen.xf_controls(xf, False)
        self.assertAlmostEqual(on["xm_l"]["Gain 2"] + 10 ** (on["xd_l"]["Gain"] / 20), 1.0)
        self.assertEqual(off["xm_r"]["Gain 2"], 0.0)
        self.assertEqual(off["xd_r"]["Gain"], 0.0)
        self.assertEqual(set(on), set(off))                     # same nodes: live switch
        # crossfeed without limiter: its worst gain is reserved statically
        q = copy.deepcopy(self.p)
        q["profil"]["muzik"]["crossfeed"]["aktif"] = True
        del q["profil"]["muzik"]["sinirlayici"]
        st = eqgen.gain_staging(q, True)
        self.assertGreater(st["muzik"]["extra_peak_db"], 0.0)
        self.assertLessEqual(st["muzik"]["worst_peak_dbfs"], q["genel"]["tavan_dbfs"] + 1e-6)

    def test_autogain_rates(self):
        self.assertEqual(eqgen._ag_rate(1.0, 10.0, 10000.0), (2.0, 1000.0))  # 1 dB / 1 s
        for r in (0.1, 0.5, 1.0, 5.0, 20.0):
            for lo, hi, pref in ((10.0, 10000.0, 1.0), (1.0, 500.0, 0.1), (0.1, 40.0, 0.5)):
                i, t = eqgen._ag_rate(r, lo, hi, pref)
                self.assertTrue(lo <= t <= hi, (r, lo, hi, t))
                self.assertIn(i, range(len(eqgen.AG_AMOUNTS)))
        c = eqgen.ag_controls(self.p["profil"]["muzik"]["seviye"], True)
        speed = eqgen.AG_AMOUNTS[int(c["vgrow_l"])] / c["tgrow_l"] * 1000
        self.assertAlmostEqual(speed, 0.5)
        self.assertEqual(c["max_on"], 1.0)
        self.assertEqual(c["max_amp"], 3.0)
        self.assertEqual(c["qamp"], 0.0)
        self.assertEqual(c["weight"], 5.0)
        self.assertLessEqual(c["tfall_s"], 40.0)
        self.assertEqual(eqgen.ag_controls(self.p["profil"]["film"]["seviye"], False)["enabled"],
                         0.0)

    def test_node_controls_and_set_live(self):
        c = eqgen.node_controls(self.p, "muzik", lsp=True)
        self.assertIn("lim", c)
        self.assertIn("ag", c)
        self.assertIn("xm_l", c)
        self.assertIn("lim", eqgen.node_controls(self.p, "cs2", lsp=True))
        self.assertNotIn("ag", eqgen.node_controls(self.p, "cs2", lsp=True))
        self.assertNotIn("lim", eqgen.node_controls(self.p, "muzik", lsp=False))
        # variant / tone targets never touch limiter, leveller, crossfeed
        v = eqgen.variant_controls(self.p, "muzik", "elektronik")
        self.assertFalse(any(k in ("lim", "ag") or k.startswith("x") for k in v))
        fake_dump, calls = [], []
        for i, n in enumerate(ALL_SINK_PROFILES):
            flat = []
            for node, cc in eqgen.node_controls(self.p, n, lsp=True).items():
                for k, val in cc.items():
                    flat += [f"{node}:{k}", val]
            fake_dump.append({"id": 100 + i, "type": "PipeWire:Interface:Node",
                              "info": {"props": {"node.name": "oto_eq_" + n},
                                       "params": {"Props": [{"params": flat}]}}})
        q = copy.deepcopy(self.p)
        q["profil"]["muzik"]["crossfeed"]["aktif"] = True
        with mock.patch.object(eqgen, "_pw_dump", return_value=fake_dump), \
                mock.patch.object(eqgen, "_run", side_effect=lambda cmd, timeout=5.0:
                                  calls.append(cmd) or mock.Mock(returncode=0, stdout="",
                                                                 stderr="")), \
                mock.patch.object(eqgen, "lsp_available", return_value=True):
            self.assertTrue(eqgen.set_live("muzik", q))
        muz = next(c for c in calls if c[2] == "102")
        cross = eqgen.crossfeed_gains(q["profil"]["muzik"]["crossfeed"])["cross"]
        self.assertIn(f'"xm_l:Gain 2" {eqgen._fmt(cross)}', muz[4])
        self.assertIn('"ag:enabled" 0.0', muz[4])               # muzik leveller off
        film = next(c for c in calls if c[2] == "103")
        self.assertIn('"lim:th"', film[4])                      # ceiling refreshed everywhere
        self.assertNotIn('"ag:', film[4])

    def test_apply_crossfeed_is_its_own_ramp(self):
        dump = [{"id": 77, "type": "PipeWire:Interface:Node",
                 "info": {"props": {"node.name": "oto_eq_muzik"},
                          "params": {"Props": [{"params": sum(
                              ([k, v] for k, v in eqgen._flatten(
                                  eqgen.node_controls(self.p, "muzik", lsp=True)).items()),
                              [])}]}}}]
        calls = []
        with mock.patch.object(eqgen, "_pw_dump", return_value=dump), \
                mock.patch.object(eqgen, "_set_params",
                                  side_effect=lambda nid, v: calls.append(dict(v)) or True):
            self.assertTrue(eqgen.apply_variant("muzik", "elektronik", ramp_s=0.4,
                                                profiles=self.p))
            self.assertTrue(eqgen.apply_crossfeed(True, ramp_s=0.1, profiles=self.p))
            eqgen.wait_ramps(3)
        xf_calls = [c for c in calls if any(k.startswith("x") for k in c)]
        var_calls = [c for c in calls if "v1_l:Gain" in c]
        self.assertTrue(xf_calls)
        self.assertTrue(all(all(k.startswith("x") for k in c) for c in xf_calls))
        self.assertAlmostEqual(var_calls[-1]["v1_l:Gain"], 2.5)  # variant NOT cancelled
        cross = eqgen.crossfeed_gains(self.p["profil"]["muzik"]["crossfeed"])["cross"]
        self.assertAlmostEqual(xf_calls[-1]["xm_r:Gain 2"], cross)
        q = copy.deepcopy(self.p)
        del q["profil"]["muzik"]["crossfeed"]
        with self.assertLogs("oto_eq.eqgen", "WARNING"):
            self.assertFalse(eqgen.apply_crossfeed(True, profiles=q))

    def test_realistic_master_signal(self):
        for kind, lv in eqgen.GERCEKCI_SETS:
            x = eqgen.gercekci_muzik(kind, lv, sec=2.0)
            self.assertEqual(x.shape[1], 2)
            self.assertAlmostEqual(eqgen.lufs_48k(x), lv, delta=0.8)
            self.assertLessEqual(float(np.max(np.abs(x))), 10 ** (-0.3 / 20) + 1e-4)
        self.assertEqual(eqgen.lim_controls(self.p["profil"]["muzik"]["sinirlayici"],
                                            -0.5)["alr"], 0.0)
        # realistic loud master through muzik (offline, same biquads): no clipping before the
        # limiter and net gain >= -4 dB (the "ses az geliyor" regression)
        from scipy.signal import sosfilt
        st = eqgen.gain_staging(self.p, True)
        x = eqgen.gercekci_muzik("pop", -8.0, sec=3.0).astype(float)
        sos = []
        for f in eqgen.effective_filters(self.p, "muzik"):
            b0, b1, b2, a0, a1, a2 = eqgen.biquad_coefs(f["tip"], f["fc"], f["kazanc"], f["q"])
            sos.append([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])
        y = sosfilt(np.array(sos), x, axis=0) * 10 ** ((st["muzik"]["preamp_db"]
                                                         + st["muzik"]["match_db"]) / 20)
        net = 10 * np.log10(np.mean(y ** 2) / np.mean(x ** 2))
        self.assertGreater(net, -4.0, net)
        self.assertLess(20 * np.log10(np.max(np.abs(y))), self.p["genel"]["tavan_dbfs"])

    def test_measurement_helpers(self):
        st = eqgen.gain_staging(self.p, True)
        sig, seg = eqgen.olcum_signal(eqgen.peak_freq(self.p, "muzik", st))
        self.assertLessEqual(float(np.max(np.abs(sig))), 1.0)          # never above 0 dBFS
        self.assertGreater(float(np.max(np.abs(sig))), 0.99)
        self.assertEqual(sig.dtype, np.float32)
        self.assertEqual(list(seg), sorted(seg, key=seg.get))
        self.assertTrue(np.all(sig[int(seg["sol_tek"] * 48000) + 10:
                                   int(seg["son"] * 48000) - 10, 1] == 0))
        c = eqgen.olcum_controls(self.p, "muzik", st, True)
        self.assertGreater(c["xm_l"]["Gain 2"], 0.0)                   # crossfeed on (worst)
        grp, tone = eqgen.worst_unit(self.p, "muzik", st)
        self.assertEqual(c["v1_l"]["Gain"], eqgen.node_controls(
            self.p, "muzik", st, True, group=grp, tone=tone)["v1_l"]["Gain"])
        lat = "\n".join(eqgen._latency_graph(self.p, "muzik", st, True))
        self.assertIn('inputs = [ "lat_ref:In" "lat_split:In" ]', lat)
        self.assertIn('outputs = [ "lat_ref:Out" "lim:out_l" ]', lat)
        self.assertIn('{ output = "lat_split:Out" input = "ag:in_r" }', lat)
        conf = eqgen._olcum_conf(self.p, eqgen.OLCUM_PREFIX, True, {"muzik": lat.splitlines()})
        self.assertIn("node.autoconnect = false", conf)
        self.assertEqual(conf.count("{"), conf.count("}"))
        self.assertEqual(eqgen._onset(np.array([0.0, 0.0, 1e-6, 0.5, 1.0])), 3)
        self.assertIsNone(eqgen._onset(np.zeros(4)))



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_profile_error(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"{alan}: sonlu bir sayı olmalı": "{alan}: muss endlich sein"})
        with self.assertRaisesRegex(eqgen.ProfileError, "^genel.pay_db: muss endlich sein$"):
            eqgen._num(float("nan"), "genel.pay_db")

    def test_conf_does_not_follow_ui_language(self):
        from oto_eq import i18n
        p = eqgen.load_profiles(config.DEFAULTS_DIR / config.PROFILES_FILE)
        tr = eqgen.generate_conf(p)
        i18n.sifirla("de", {"Müzik": "Musik", "Seviye eşitli düz geçiş": "Pegel"})
        self.assertEqual(eqgen.generate_conf(p), tr)


if __name__ == "__main__":
    unittest.main()


class DeviceCorrectionTests(unittest.TestCase):
    """CHANGE 7: the [duzeltme] curve only on its own headphone model."""

    def setUp(self):
        from oto_eq import kulaklik
        self.K = kulaklik
        self.p = shipped()
        self.state = Path(_TMP.name) / "cihaz-test.json"
        p = mock.patch.object(kulaklik, "durum_yolu", lambda: self.state)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: self.state.unlink(missing_ok=True))

    def device(self, fits: bool):
        a = self.K.Aygit("x", "Model", "usb", True, fits, "test")
        self.K.durum_yaz(a, self.state)
        st = self.state.stat()            # coarse mtimes: make every write a new version
        import os
        os.utime(self.state, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000 * (2 if fits else 1)))

    def test_other_headphone_gets_flat_correction_and_new_staging(self):
        on = eqgen.node_controls(self.p, "muzik", None, lsp=True)
        self.assertTrue(eqgen.correction_on(self.p))       # no state file: HS80 behaviour
        self.device(False)
        self.assertFalse(eqgen.correction_on(self.p))
        off = eqgen.node_controls(self.p, "muzik", None, lsp=True)
        gains = [off[k]["Gain"] for k in off if re.fullmatch(r"d\d+_[lr]", k)]
        self.assertTrue(gains and all(g == 0.0 for g in gains))
        self.assertTrue(any(on[k]["Gain"] != 0.0 for k in on if re.fullmatch(r"d\d+_[lr]", k)))
        self.assertGreater(off["pre_l"]["Mult"], on["pre_l"]["Mult"])   # less headroom needed
        self.assertEqual(sorted(on), sorted(off))           # same graph: live switch, no restart
        self.assertEqual(eqgen.effective_filters(self.p, "muzik"), self.p["profil"]["muzik"]["filtreler"])
        self.device(True)
        self.assertEqual(eqgen.node_controls(self.p, "muzik", None, lsp=True), on)

    def test_switch_off_in_profiles_still_wins(self):
        q = copy.deepcopy(self.p)
        q["duzeltme"]["aktif"] = False
        self.device(True)
        self.assertFalse(eqgen.correction_on(q))

    def test_apply_correction_ramps_only_affected_nodes_of_every_sink(self):
        self.device(False)
        calls = []
        with mock.patch.object(eqgen, "_start_ramp",
                               side_effect=lambda prof, tgt, *a, **k: calls.append((prof, tgt)) or True):
            self.assertTrue(eqgen.apply_correction({"muzik": "metal_rock", "oyun": "yok"},
                                                   profiles=self.p))
        self.assertEqual([c[0] for c in calls], list(ALL_SINK_PROFILES))
        for prof, tgt in calls:
            nodes = {k.split(":")[0] for k in tgt}
            self.assertTrue(nodes, prof)
            self.assertTrue(all(re.fullmatch(r"(d\d+|pre|cikis)_[lr]", n) for n in nodes), nodes)
            if prof == FLAT_PROFILE:
                self.assertEqual(nodes, {"cikis_l", "cikis_r"})
        muzik = dict(calls)["muzik"]
        want = eqgen._flatten(eqgen.variant_controls(self.p, "muzik", "metal_rock"))
        self.assertTrue(all(abs(muzik[k] - v) < 1e-9 for k, v in want.items() if k in muzik))
        self.assertTrue(all(v == 0.0 for k, v in muzik.items() if re.match(r"d\d+_l:Gain", k)))

    def test_cihaz_desen_validated(self):
        raw = tomllib.loads((config.DEFAULTS_DIR / config.PROFILES_FILE).read_text("utf-8"))
        self.assertEqual(eqgen.normalize_profiles(raw)["duzeltme"]["cihaz_desen"],
                         raw["duzeltme"].get("cihaz_desen", "(?i)hs80"))
        for bad in ("(x", 3):
            raw["duzeltme"]["cihaz_desen"] = bad
            with self.subTest(bad=bad), self.assertRaises(eqgen.ProfileError):
                eqgen.normalize_profiles(raw)


class RampCarryOverTests(unittest.TestCase):
    """A ramp that cancels another one also finishes the cancelled ramp's controls
    (H1, 27 Eki: a genre ramp left the headphone correction's d-bands/preamp half-done)."""

    def test_cancelled_correction_controls_are_finished(self):
        live = {"d1_l:Gain": 6.5, "pre_l:Mult": 0.25, "v1_l:Gain": 0.0}
        sets = []

        def set_params(nid, vals):
            sets.append(dict(vals))
            live.update(vals)
            return True
        with mock.patch.object(eqgen, "_pw_dump", return_value=[]), \
                mock.patch.object(eqgen, "_node_ids", return_value={"oto_eq_muzik": 7}), \
                mock.patch.object(eqgen, "_live_params_from_dump", side_effect=lambda d, n: dict(live)), \
                mock.patch.object(eqgen, "_set_params", side_effect=set_params):
            self.assertTrue(eqgen._start_ramp("muzik", {"d1_l:Gain": 0.0, "pre_l:Mult": 0.475},
                                              30.0, "oto_eq_", "düzeltme"))     # slow: still running
            self.assertTrue(eqgen._start_ramp("muzik", {"v1_l:Gain": 1.0}, 0.0, "oto_eq_", "tür"))
            eqgen.wait_ramps(5)
        self.assertAlmostEqual(live["d1_l:Gain"], 0.0)
        self.assertAlmostEqual(live["pre_l:Mult"], 0.475)
        self.assertAlmostEqual(live["v1_l:Gain"], 1.0)
        self.assertEqual(eqgen._RAMPS, {})
