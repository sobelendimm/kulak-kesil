"""Kişisel ses modeli (CHANGE 6, D3) — ses yok, sentetik sayılar."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402

from oto_eq import geri_bildirim  # noqa: E402
from oto_eq import kisisel_model as km  # noqa: E402
from oto_eq import ses_analiz as sa  # noqa: E402
from oto_eq.models import MUSIC_GROUPS, ContentId  # noqa: E402

G = km.GROUPS

BASE_F = {
    "rms_db": -14.0, "active_s": 6.0, "r_sub": 0.05, "r_bass": 0.30, "r_lowmid": 0.15,
    "r_mid": 0.30, "r_highmid": 0.15, "r_high": 0.05, "sub_share": 0.14, "centroid": 1500.0,
    "rolloff": 4000.0, "flatness": 0.05, "centroid_cv": 0.7, "flux": 0.04, "low_pump_db": 8.0,
    "beat_low": 0.3, "beat_full": 0.4, "tempo": 120.0, "onset_rate": 4.6, "crest_db": 13.0,
    "lra_db": 9.0, "pause_ratio": 0.08, "env_std_db": 7.0, "mod_speech": 0.3, "mod_peak": 10.0,
    "stability": 0.77, "harm_ratio": 0.63,
}

# sınıf → (DSP olasılıkları, model olasılıkları, özellik kaydırmaları)
# "pop az tespit" yanlılığı: model pop'u güvenle elektronik sanıyor, DSP kararsız →
# taban füzyon pop parçalarında ELEKTRONİK der (yanlış EQ). Özellikler ise ayırıyor
# (pop: tonal/harmonik, az alt bas; gerçek elektronik: alt bas, perküsif).
PROFILES = {
    "pop_vokal": ({"pop_vokal": 0.35, "elektronik": 0.35, "metal_rock": 0.15,
                   "klasik_akustik": 0.1, "konusma": 0.05},
                  {"elektronik": 0.82, "pop_vokal": 0.15, "metal_rock": 0.03},
                  {"harm_ratio": 0.85, "r_sub": 0.02, "stability": 0.88, "mod_speech": 0.42}),
    "elektronik": ({"elektronik": 0.55, "pop_vokal": 0.2, "metal_rock": 0.15,
                    "klasik_akustik": 0.05, "konusma": 0.05},
                   {"elektronik": 0.85, "pop_vokal": 0.1, "metal_rock": 0.05},
                   {"harm_ratio": 0.42, "r_sub": 0.14, "stability": 0.66, "mod_speech": 0.25}),
    "metal_rock": ({"metal_rock": 0.6, "elektronik": 0.15, "pop_vokal": 0.15,
                    "klasik_akustik": 0.05, "konusma": 0.05},
                   {"metal_rock": 0.85, "pop_vokal": 0.1, "elektronik": 0.05},
                   {"flatness": 0.12, "crest_db": 9.0}),
    "klasik_akustik": ({"klasik_akustik": 0.6, "pop_vokal": 0.2, "metal_rock": 0.1,
                        "elektronik": 0.05, "konusma": 0.05},
                       {"klasik_akustik": 0.85, "pop_vokal": 0.15},
                       {"crest_db": 18.0, "r_sub": 0.01}),
}


def _norm(p):
    s = sum(p.values())
    return {g: p.get(g, 0.0) / s for g in G}


def make_detail(group, rng, noise=0.04):
    pd0, pm0, shift = PROFILES[group]
    pd_ = _norm({g: max(1e-4, pd0.get(g, 0.0) + rng.normal(0, noise)) for g in G})
    pm = _norm({g: max(1e-4, pm0.get(g, 0.0) + rng.normal(0, noise)) for g in G})
    f = dict(BASE_F)
    for k, v in shift.items():
        f[k] = v
    for k in ("harm_ratio", "stability", "mod_speech"):
        f[k] = float(f[k] + rng.normal(0, 0.03))
    f["r_sub"] = float(max(0.006, f["r_sub"] + rng.normal(0, 0.005)))
    top, ok, scores = sa.decide(pd_, pm, f)
    return {"features": f, "dsp_scores": pd_, "model_probs": pm, "fused": sa.fuse(pd_, pm),
            "personal": None, "scores": scores, "group": top if ok else "",
            "confidence": scores[top], "since": 1.0, "until": 7.0, "window_s": 6.0}


ARTISTS = {   # sanatçı → gerçek grup
    **{f"pop{i}": "pop_vokal" for i in range(6)},
    **{f"edm{i}": "elektronik" for i in range(6)},
    **{f"rock{i}": "metal_rock" for i in range(3)},
    **{f"klasik{i}": "klasik_akustik" for i in range(3)},
}


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.d = Path(self._td.name)
        self.tah = self.d / "ses_tahminleri.jsonl"
        self.fb = self.d / "geri_bildirim.jsonl"
        self.mf = self.d / "kisisel_model.json"

    def tearDown(self):
        self._td.cleanup()

    def fill(self, artists=ARTISTS, per=5, seed=0, correct=True, strong_artists=None):
        """Tahmin kayıtları + tepsi düzeltmeleri (güçlü etiket) yaz."""
        rng = np.random.default_rng(seed)
        for name, group in artists.items():
            cid = ContentId("muzik", f"sanatci:{name}", name.title(), "parça")
            for _ in range(per):
                km.kaydet_tahmin(cid, make_detail(group, rng), "pencere", path=self.tah)
            if correct and (strong_artists is None or name in strong_artists):
                geri_bildirim.kaydet("muzik", cid.key, cid.display, cid.track, "elektronik",
                                     "ses analizi", group, path=self.fb)

    def train(self, **kw):
        return km.egit(self.tah, self.fb, self.mf, isim_etiketi=kw.pop("isim", None), **kw)


# ------------------------------------------------------------------ log
class TestKayit(Tmp):
    def test_record_fields_and_no_audio(self):
        rec = km.kaydet_tahmin(ContentId("muzik", "sanatci:safi", "SAFI", "Gece"),
                               make_detail("pop_vokal", np.random.default_rng(1)), "pencere",
                               path=self.tah)
        self.assertIsNotNone(rec)
        line = json.loads(self.tah.read_text().strip())
        self.assertEqual(line["key"], "sanatci:safi")
        self.assertEqual(line["display"], "SAFI")
        self.assertEqual(line["track"], "Gece")
        self.assertEqual(line["asama"], "pencere")
        self.assertTrue(line["ts"].endswith("Z"))
        self.assertEqual(line["surum"], km.VERSION)
        self.assertEqual(set(line["dsp"]), set(MUSIC_GROUPS))
        self.assertIn("harm_ratio", line["features"])
        self.assertEqual(line["window_s"], 6.0)
        self.assertNotIn("audio", line)
        self.assertNotIn("x", line)
        self.assertLess(len(self.tah.read_bytes()), 2500)      # sayılar yuvarlanmış

    def test_bad_inputs_never_raise(self):
        cid = ContentId("muzik", "sanatci:a", "A")
        self.assertIsNone(km.kaydet_tahmin(None, {"dsp_scores": {}}, path=self.tah))
        self.assertIsNone(km.kaydet_tahmin(cid, None, path=self.tah))
        self.assertIsNone(km.kaydet_tahmin(cid, {}, path=self.tah))
        self.assertIsNone(km.kaydet_tahmin(ContentId("muzik", "", "A"),
                                           make_detail("pop_vokal", np.random.default_rng(0)),
                                           path=self.tah))
        bad = self.d / "dosya"
        bad.write_text("x")                                     # dizin yerine dosya
        self.assertIsNone(km.kaydet_tahmin(cid, make_detail("pop_vokal",
                                                            np.random.default_rng(0)),
                                           path=bad / "t.jsonl"))
        nan = make_detail("pop_vokal", np.random.default_rng(0))
        nan["features"]["flux"] = float("nan")
        rec = km.kaydet_tahmin(cid, nan, path=self.tah)
        self.assertIsNone(rec["features"]["flux"])
        json.loads(self.tah.read_text())                        # geçerli JSON (NaN yok)

    def test_rotation(self):
        with mock.patch.object(km, "MAX_BYTES", 3000):
            rng = np.random.default_rng(0)
            for _ in range(6):
                km.kaydet_tahmin(ContentId("muzik", "sanatci:a", "A"),
                                 make_detail("pop_vokal", rng), path=self.tah)
        self.assertTrue(Path(str(self.tah) + ".1").exists())
        self.assertLess(self.tah.stat().st_size, 3000 + 2500)
        recs = km.oku_tahminler(self.tah)                       # .1 + güncel, eskiden yeniye
        self.assertGreaterEqual(len(recs), 3)
        self.assertEqual([r["ts"] for r in recs], sorted(r["ts"] for r in recs))


# ------------------------------------------------------------------ labels
class TestEtiketler(unittest.TestCase):
    def rec(self, key, group="pop_vokal", track="t", model=True):
        d = make_detail(group, np.random.default_rng(0))
        return {"key": key, "display": key.split(":")[-1], "track": track,
                "dsp": d["dsp_scores"], "model": d["model_probs"] if model else None,
                "features": d["features"]}

    def test_strong_weak_and_skips(self):
        tah = [self.rec("sanatci:a"), self.rec("sanatci:B"), self.rec("sanatci:c"),
               self.rec("sanatci:d"), self.rec("sanatci:e", model=False),
               self.rec("sanatci:f")]
        fb = [{"ts": "2026-10-25T10:00:00Z", "profile": "muzik", "key": "sanatci:a",
               "corrected_group": "metal_rock"},
              {"ts": "2026-10-25T11:00:00Z", "profile": "muzik", "key": "sanatci:a",
               "corrected_group": "pop_vokal"},                   # en yenisi kazanır
              {"ts": "2026-10-25T11:00:00Z", "profile": "muzik", "key": "sanatci:b",
               "corrected_group": ""},                            # Genel
              {"ts": "x", "profile": "oyun", "key": "sanatci:c", "corrected_group": "fps"}]
        names = {"sanatci:a": ("elektronik", "iTunes: Dance"),     # düzeltme önce gelir
                 "sanatci:c": ("klasik_akustik", "iTunes: Classical"),
                 "sanatci:d": ("metal_rock", "kullanıcı")}
        S, say = km.ornekler(tah, fb, lambda k, d, t: names.get(k))
        by = {o.key: o for o in S}
        self.assertEqual(by["a"].label, "pop_vokal")
        self.assertTrue(by["a"].strong)
        self.assertEqual(by["b"].label, "")                      # büyük/küçük harf eşleşir
        self.assertTrue(by["b"].strong)
        self.assertEqual(by["c"].label, "klasik_akustik")
        self.assertFalse(by["c"].strong)
        self.assertEqual(by["c"].weight, km.WEAK_WEIGHT)
        self.assertTrue(by["d"].strong)                          # kullanıcı ataması = güçlü
        self.assertNotIn("e", by)                                # model olasılığı yok
        self.assertNotIn("f", by)                                # etiket yok
        self.assertEqual(say["guclu"], 3)
        self.assertEqual(say["zayif"], 1)
        self.assertEqual(say["modelsiz"], 1)
        self.assertEqual(say["etiketsiz"], 1)

    def test_name_labeller_ignores_audio_memory(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "tur-onbellek.json"
            cache.write_text(json.dumps({"surum": 1, "kayitlar": {
                "ses:sanatci:x": {"group": "metal_rock", "confidence": 0.7,
                                  "source": "ses analizi: …", "t": 1e12, "name": "X"},
                "sanatci:y": {"tags": [["Metal", 3.0]], "source": "iTunes", "t": 1e12,
                              "name": "Y", "negatif": False, "guven": 0.85},
            }}), "utf-8")
            bul = km.isim_etiketi_fabrikasi(cache, settings={"sanatci": {"zz": "konusma"}})
            self.assertIsNone(bul("sanatci:x", "X", ""))
            self.assertEqual(bul("sanatci:y", "Y", "")[0], "metal_rock")
            self.assertEqual(bul("sanatci:zz", "ZZ", ""), ("konusma", "kullanıcı"))
            self.assertIsNone(bul("sanatci:bilinmeyen", "?", ""))


# ------------------------------------------------------------------ fit
class TestFit(unittest.TestCase):
    def test_zero_weights_reproduce_base_fusion(self):
        d = make_detail("pop_vokal", np.random.default_rng(3))
        W, b = np.zeros((len(km.INPUTS), len(G))), np.zeros(len(G))
        adj = km.uygula(W, b, d["dsp_scores"], d["model_probs"], d["features"])
        for g in G:
            self.assertAlmostEqual(adj[g], d["fused"][g], delta=0.004)

    def test_offdiagonal_probability_weights_pinned_to_zero(self):
        rng = np.random.default_rng(0)
        S = [km.Ornek(f"k{i}", *(lambda d: (d["dsp_scores"], d["model_probs"], d["features"]))(
            make_detail(g, rng)), g, True, 1.0) for i, g in enumerate(list(PROFILES) * 10)]
        W, b = km.fit(*km._matrices(S))
        for j in range(2 * len(G)):
            for k in range(len(G)):
                if j % len(G) != k:
                    self.assertEqual(W[j, k], 0.0)
        self.assertTrue(np.all(np.isfinite(W)))

    def test_deterministic(self):
        rng = np.random.default_rng(5)
        S = [km.Ornek(f"k{i % 7}", *(lambda d: (d["dsp_scores"], d["model_probs"],
                                                 d["features"]))(make_detail(g, rng)),
                      g, True, 1.0) for i, g in enumerate(list(PROFILES) * 8)]
        a = km.fit(*km._matrices(S))
        b = km.fit(*km._matrices(S))
        np.testing.assert_array_equal(a[0], b[0])
        np.testing.assert_array_equal(a[1], b[1])


# ------------------------------------------------------------------ training
class TestEgit(Tmp):
    def test_too_few_strong_samples_inactive(self):
        self.fill({"pop0": "pop_vokal", "edm0": "elektronik"}, per=5)
        m = self.train()
        self.assertFalse(m["aktif"])
        self.assertIn("yetersiz güçlü örnek: 10/20", m["neden"])
        self.assertNotIn("W", m)
        self.assertTrue(self.mf.exists())
        self.assertIsNone(km.KisiselModel(self.mf).adjust({}, {}, {}))

    def test_few_artists_inactive(self):
        self.fill({"pop0": "pop_vokal", "edm0": "elektronik", "rock0": "metal_rock"}, per=8)
        m = self.train()
        self.assertFalse(m["aktif"])
        self.assertIn("sanatçı", m["neden"])

    def test_biased_base_is_fixed_by_personal_model(self):
        """Pop az tespit: taban pop parçalarına elektronik der; kişisel model öğrenir."""
        self.fill(per=5)
        m = self.train()
        cv = m["cv"]
        self.assertTrue(m["aktif"], m["neden"])
        self.assertEqual(m["sayilar"]["guclu"], 90)
        self.assertGreaterEqual(cv["kazanc"], cv["esik"])
        self.assertLess(cv["kisisel"]["yanlis"], cv["taban"]["yanlis"])
        self.assertGreater(cv["kisisel"]["isabet"], cv["taban"]["isabet"])
        # yeni (görülmemiş) pop/elektronik pencerelerinde canlı kural
        pm = km.KisiselModel(self.mf)
        rng = np.random.default_rng(99)
        base_ok = mine_ok = base_bad = mine_bad = 0
        for g in ("pop_vokal", "elektronik") * 20:
            d = make_detail(g, rng)
            bt, bok, _ = sa.decide(d["dsp_scores"], d["model_probs"], d["features"])
            adj = pm.adjust(d["dsp_scores"], d["model_probs"], d["features"])
            self.assertIsNotNone(adj)
            t, ok, _ = sa.decide(d["dsp_scores"], d["model_probs"], d["features"], adj)
            base_ok += bok and bt == g
            base_bad += bok and bt != g
            mine_ok += ok and t == g
            mine_bad += ok and t != g
        self.assertGreaterEqual(base_bad, 10)                   # taban: pop → elektronik
        self.assertLessEqual(mine_bad, 2)
        self.assertGreaterEqual(mine_ok, base_ok + 15)          # pop artık tanınıyor

    def test_no_gain_stays_inactive(self):
        """Taban zaten doğru (etiketler = taban kararları) → iyileşme yok → pasif."""
        arts = {f"edm{i}": "elektronik" for i in range(5)}
        arts.update({f"rock{i}": "metal_rock" for i in range(5)})
        self.fill(arts, per=5)
        m = self.train()
        self.assertFalse(m["aktif"])
        self.assertIn("tabandan iyi değil", m["neden"])
        self.assertIn("cv", m)

    def test_weak_labels_used_for_training_not_for_cv(self):
        strong = {f"pop{i}" for i in range(3)} | {f"edm{i}" for i in range(3)}
        self.fill(per=5, strong_artists=strong)
        names = {f"sanatci:{a}": (g, "iTunes") for a, g in ARTISTS.items()}
        m = self.train(isim=lambda k, d, t: names.get(k))
        self.assertEqual(m["sayilar"]["guclu"], 30)
        self.assertEqual(m["sayilar"]["zayif"], 60)
        self.assertEqual(m["cv"]["taban"]["n"], 30)              # CV yalnız güçlü örnekte

    def test_training_is_deterministic(self):
        self.fill(per=5)
        a = self.train()
        b = self.train()
        self.assertEqual(a["W"], b["W"])
        self.assertEqual(a["cv"], b["cv"])


# ------------------------------------------------------------------ loading
class TestYukle(Tmp):
    def trained(self):
        self.fill(per=5)
        m = self.train()
        self.assertTrue(m["aktif"])
        return m

    def test_missing_file_is_base(self):
        pm = km.KisiselModel(self.mf)
        self.assertFalse(pm.aktif)
        d = make_detail("pop_vokal", np.random.default_rng(0))
        self.assertIsNone(pm.adjust(d["dsp_scores"], d["model_probs"], d["features"]))

    def test_fingerprint_or_version_mismatch_disables(self):
        self.trained()
        self.assertTrue(km.KisiselModel(self.mf).aktif)
        for patch in ({"parmak_izi": "eski"}, {"surum": 0}, {"W": [[1.0]]}):
            d = json.loads(self.mf.read_text())
            d.update(patch)
            p = self.d / "m2.json"
            p.write_text(json.dumps(d))
            self.assertFalse(km.KisiselModel(p).aktif, patch)
        self.mf.write_text("{bozuk")
        self.assertFalse(km.KisiselModel(self.mf).aktif)

    def test_reload_after_retrain_or_reset(self):
        self.trained()
        clock = [0.0]
        pm = km.KisiselModel(self.mf, check_s=60.0, clock=lambda: clock[0])
        self.assertTrue(pm.aktif)
        km.sifirla(self.mf)
        clock[0] = 30.0
        self.assertTrue(pm.aktif)                                # yoklama aralığı dolmadı
        clock[0] = 61.0
        self.assertFalse(pm.aktif)
        self.train()
        clock[0] = 200.0
        self.assertTrue(pm.aktif)
        self.assertFalse(km.sifirla(self.d / "yok.json"))

    def test_adjust_without_model_probs_is_none(self):
        self.trained()
        d = make_detail("pop_vokal", np.random.default_rng(0))
        self.assertIsNone(km.KisiselModel(self.mf).adjust(d["dsp_scores"], None,
                                                          d["features"]))


# ------------------------------------------------------------------ CLI
class TestRun(Tmp):
    def run_cli(self, *argv):
        out = []
        with mock.patch.object(km, "TAHMIN_FILE", self.tah), \
                mock.patch.object(km, "FEEDBACK_FILE", self.fb), \
                mock.patch.object(km, "MODEL_FILE", self.mf), \
                mock.patch.object(km, "isim_etiketi_fabrikasi", lambda *a, **k: None):
            rc = km.run(list(argv), out=out.append)
        return rc, "\n".join(out)

    def test_durum_empty(self):
        rc, txt = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("tahmin kaydı 0", txt)
        self.assertIn("20 güçlü örnek daha", txt)
        self.assertIn("henüz eğitilmedi", txt)

    def test_egit_durum_sifirla(self):
        self.fill(per=5)
        rc, txt = self.run_cli("egit")
        self.assertEqual(rc, 0)
        self.assertIn("ETKİN", txt)
        self.assertIn("kazanç +", txt)
        rc, txt = self.run_cli("durum")
        self.assertIn("güçlü 90", txt)
        self.assertIn("ETKİN", txt)
        self.assertIn("son eğitim", txt)
        rc, txt = self.run_cli("sifirla")
        self.assertEqual(rc, 0)
        self.assertFalse(self.mf.exists())
        self.assertIn("silindi", txt)
        self.assertTrue(self.tah.exists())                       # kayıtlar kalır
        rc, txt = self.run_cli("sifirla")
        self.assertIn("zaten yok", txt)

    def test_reason_shown_in_current_language(self):
        # 6 Kas: the reason was stored translated once; shown later in that old language
        from oto_eq import i18n
        self.addCleanup(i18n.sifirla)
        self.fill({"pop0": "pop_vokal", "edm0": "elektronik"}, per=5)
        self.run_cli("egit")
        m = json.loads(self.mf.read_text("utf-8"))
        self.assertIn("yetersiz güçlü örnek: 10/20", m["neden"])          # plain text kept
        self.assertEqual(m["neden_k"]["arg"], {"n": 10, "gereken": km.MIN_STRONG})
        msg = ("yetersiz güçlü örnek: {n}/{gereken} (tepsiden 'Bu şarkı aslında…' "
               "düzeltmeleri gerekiyor)")
        i18n.sifirla("en", {msg: "too few strong samples: {n}/{gereken}"})
        _rc, txt = self.run_cli("durum")
        self.assertIn("too few strong samples: 10/20", txt)
        self.assertNotIn("yetersiz", txt)

    def test_old_model_file_without_record(self):
        self.mf.write_text(json.dumps({"surum": km.VERSION, "aktif": False,
                                       "neden": "eski düz metin", "egitim": "x"}), "utf-8")
        _rc, txt = self.run_cli("durum")
        self.assertIn("eski düz metin", txt)
        self.assertEqual(km.neden_metni({"neden": "a", "neden_k": {"id": "{yok}", "arg": {"x": 1}}}), "a")
        self.assertEqual(km.neden_metni(None), "")

    def test_usage(self):
        rc, txt = self.run_cli("bilmem")
        self.assertEqual(rc, 2)
        self.assertIn("kulak model", txt)
        self.assertEqual(self.run_cli("--help")[0], 0)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_cli_line(self):
        from oto_eq import i18n
        msg = ("tahmin kaydı {kayit} (modelsiz {modelsiz}, etiketsiz {etiketsiz}) · güçlü "
               "{guclu} · zayıf {zayif} · sanatçı {sanatci_guclu} güçlü / {sanatci} toplam")
        i18n.sifirla("de", {msg: "Einträge {kayit}, stark {guclu}"})
        self.assertEqual(km._say_satiri({"kayit": 4, "guclu": 2}), "  Einträge 4, stark 2")


if __name__ == "__main__":
    unittest.main()
