"""tools/i18n_cikar.py, i18n_cevir.py, i18n_denetle.py (CHANGE 8) — offline only.

The translator is always a fake callable: these tests never touch the network, the real
catalogs or the real cache (~/.cache/oto-eq-i18n).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import textwrap
import threading
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cikar = _load("i18n_cikar")
cevir = _load("i18n_cevir")
denetle = _load("i18n_denetle")


class FakeGoogle:
    """Masked text → canned output; records calls. ``bozuk`` mangles tokens like Google."""

    def __init__(self, cevap=None, bozuk=None, hata_sayisi=0):
        self.cevap = cevap or (lambda t: "EN:" + t)
        self.bozuk = bozuk
        self.hata_sayisi = hata_sayisi
        self.cagrilar: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, text):
        with self._lock:
            self.cagrilar.append(text)
            if self.hata_sayisi > 0:
                self.hata_sayisi -= 1
                raise ConnectionError("connection reset")
        out = "\n".join(self.cevap(line) for line in text.split("\n"))   # batches: per line
        return self.bozuk(out) if self.bozuk else out


class TooManyRequests(Exception):
    """Same class name as deep_translator.exceptions.TooManyRequests."""


def ayarlar(**kw):
    kw.setdefault("geri_cekilme", 0.0)
    return cevir.Ayarlar(**kw)


class TestMaskeleme(unittest.TestCase):
    def test_masks_every_protected_kind(self):
        s = ("Kulak Kesil: {ad} için {db:+.1f} dB, %s ve 5 sn; `kulak yenile` ya da "
             "systemctl --user restart oto-eq; ~/.config/oto-eq/arayuz.json, Ctrl+Q, "
             "https://ornek.org/a · HS80")
        m = cevir.maskele(s, "en")
        kalan = cevir._TOKEN.sub("", m.metin)
        for parca in ("Kulak Kesil", "{ad}", "{db", "%s", "sn", "kulak", "systemctl", "~/",
                      "arayuz.json", "Ctrl", "https", "·", "HS80"):
            self.assertNotIn(parca, kalan, parca)
        self.assertIn("için", kalan)
        self.assertEqual(cevir.geri_koy(m.metin, cevir.maskele(s, "tr")), s)  # tr: units as-is
        self.assertIn("5 s", m.geri)                       # "sn" → "s" for English

    def test_plain_turkish_words_are_not_masked(self):
        for s in ("Kulak yorgunluğu", "Açık/Kapalı", "%50 ses", "kulak ile dinle"):
            with self.subTest(s=s):
                self.assertEqual(cevir.maskele(s, "en").metin, s)

    def test_command_stops_before_turkish_words(self):
        m = cevir.maskele("kulak yenile ile servisi başlat; pactl list sinks çıktısına bak", "en")
        self.assertEqual(m.kaynak, ["kulak yenile", "pactl list sinks"])

    def test_glossary_terms_masked_per_language(self):
        t = cevir.Terimler(terim={"Kişisel ton": {"en": "Personal tone", "de": "Persönlicher Klang"}})
        m = cevir.maskele("Kişisel ton kaydedildi", "de", t)
        self.assertEqual(m.metin, "@0@ kaydedildi")
        self.assertEqual(m.geri, ["Persönlicher Klang"])
        self.assertEqual(cevir.maskele("Kişisel ton kaydedildi", "fr", t).metin,
                         "Kişisel ton kaydedildi")             # no French entry → Google

    def test_google_token_damage_is_repaired(self):
        m = cevir.maskele("{ad} ile {tur} · HS80'in pili", "en")
        self.assertEqual(m.metin, "@0@ ile @1@ @2@ @3@'in pili")
        for raw in ("@ 0 @ with @1 @ @2@ @ 3@'s battery",
                    "＠0＠ with \"@1@\" @2@ @3@'s battery",
                    "@０@ with «@1@» @2@ @3@'in battery"):
            with self.subTest(raw=raw):
                s = cevir.belirtecleri_onar(raw, m)
                cevir.belirtec_denetle(s, m)
                out = cevir.geri_koy(s, m)
                self.assertIn("{ad} with {tur} · HS80", out)
                self.assertNotIn("'in", out)
        self.assertIn("HS80's", cevir.geri_koy(cevir.belirtecleri_onar("@3@'s", m), m))
        k = cevir.maskele("kullanım: python3 -m oto_eq.x icon [YOL]", "en")
        self.assertEqual(cevir.belirtecleri_onar("usage: `@0@ [PATH]`", k), "usage: @0@ [PATH]")
        k = cevir.maskele("`kulak yenile` yazın", "en")                # source has them
        self.assertEqual(cevir.belirtecleri_onar("type `x` @0@", k), "type `x` @0@")

    def test_cli_syntax_in_usage_texts_is_protected(self):
        m = cevir.maskele("kullanım: kulak model [durum|egit|sifirla] [db=6] --profiller "
                          "oyun,muzik,film\n  ac | kapat     özelliği aç\n  durum  ayarlar "
                          "ya da 'sifirla'; 'pompalama' olmasın", "en")
        korunan = [k for k, t in zip(m.kaynak, m.tur) if t == "kod"]
        self.assertEqual(korunan, ["[durum|egit|sifirla]", "[db=6]", "--profiller",
                                   "oyun,muzik,film", "ac | kapat", "durum", "'sifirla'"])
        for metin in ("özelliği aç", "ayarlar", "'pompalama' olmasın"):     # prose stays
            self.assertIn(metin, m.metin)

    def test_glued_tokens_are_separated_as_in_the_source(self):
        cmd = "systemctl --user start {birim}; günlük: journalctl --user -u {birim} -n 30"
        durumlar = [
            ("{profil} modu", "en", "@0@e mode", "{profil} mode"),
            ("A: {profil} dinleniyor", "de", "A: @0@e läuft", "A: {profil} läuft"),
            ("başka bir oto-eq zaten çalışıyor ({yol})", "en",
             "another @0@ is already running (path@1@)",
             "another oto-eq is already running (path {yol})"),
            ("ses ~1 sn kesilir", "en", "audio stops for ~@0@econd", "audio stops for ~1 second"),
            (cmd, "en", "@0@@1@; log: @2@@3@ -n 30",
             "systemctl --user start {birim}; log: journalctl --user -u {birim} -n 30"),
            (cmd, "zh", "@0@@1@；日志：@2@@3@ -n 30",
             "systemctl --user start {birim}；日志：journalctl --user -u {birim} -n 30"),
            ("{n} tür", "zh", "@0@种类型", "{n}种类型"),             # CJK glue is normal
            ("{n}. tur", "fr", "@0@e tour", "{n}e tour"),               # French ordinal kept
            ("{n} tur", "fr", "@0@e tour", "{n} tour"),
            ("{n} kat hızlı", "en", "@0@x faster", "{n}x faster"),
            ("{ad}ler", "en", "@0@s", "{ad}s"),                        # glued in the source
            ("Ses: {ad}", "en", "Sound:@0@", "Sound:{ad}"),            # not after a letter
            ("lookahead {ms:g} ms (gecikme)", "es", "lookahead@0@ (retardo)",
             "lookahead {ms:g} ms (retardo)"),
            ("kulak kisma [db=6] [--profiller x]", "en", "@0@ @1@[@2@ x]",
             "kulak kisma [db=6] [--profiller x]"),
        ]
        for kaynak, dil, raw, beklenen in durumlar:
            with self.subTest(kaynak=kaynak, dil=dil):
                m = cevir.maskele(kaynak, dil)
                s = cevir.bitisikleri_ayir(raw, m, dil)
                cevir.belirtec_denetle(s, m)
                self.assertEqual(cevir.geri_koy(s, m), beklenen)

    def test_lost_or_duplicated_token_is_an_error(self):
        m = cevir.maskele("{ad} ile {tur}", "en")
        for raw in ("@0@ with", "@0@ with @1@ @1@", "@0@ with @1"):
            with self.subTest(raw=raw), self.assertRaises(cevir.CeviriHatasi):
                cevir.belirtec_denetle(cevir.belirtecleri_onar(raw, m), m)

    def test_newlines_survive(self):
        m = cevir.maskele("Birinci satır\n\nİkinci satır", "en")
        self.assertEqual(m.metin.count("@"), 2)
        self.assertEqual(cevir.geri_koy("First line @0@ Second line", m), "First line\n\nSecond line")


class TestBirMetin(unittest.TestCase):
    def test_placeholders_and_punctuation(self):
        g = FakeGoogle(lambda t: {"Kaydedildi: @0@ @1@ @2@": "Saved: @0@ @1@ @2@.",
                                  "Bekleniyor…": "Waiting...",
                                  "durumu göster": "Show status.",
                                  "Seviye: @0@": "Level @0@"}[t])
        ay = ayarlar()
        self.assertEqual(cevir.bir_metni_cevir("Kaydedildi: {ad} → {tur}", "en", g, ay),
                         "Saved: {ad} → {tur}")
        self.assertEqual(cevir.bir_metni_cevir("Bekleniyor…", "en", g, ay), "Waiting…")
        self.assertEqual(cevir.bir_metni_cevir("durumu göster", "en", g, ay), "show status")
        self.assertEqual(cevir.bir_metni_cevir("  Seviye: {db:+.1f} dB:", "en",
                                               FakeGoogle(lambda t: "Level: @0@"), ay),
                         "  Level: {db:+.1f} dB:")

    def test_wrong_placeholder_rejected(self):
        g = FakeGoogle(lambda t: t.replace("@0@", "{name}"))
        with self.assertRaises(cevir.CeviriHatasi):
            cevir.bir_metni_cevir("Merhaba {ad}", "en", g, ayarlar())

    def test_exact_glossary_and_untranslatable_skip_google(self):
        g = FakeGoogle()
        ay = ayarlar(terimler=cevir.Terimler(tam={"Düz": {"en": "Flat"}}))
        self.assertEqual(cevir.bir_metni_cevir("Düz", "en", g, ay), "Flat")
        self.assertEqual(cevir.bir_metni_cevir("HS80 · {ad}", "en", g, ay), "HS80 · {ad}")
        self.assertEqual(cevir.bir_metni_cevir("{n} sn", "ja", g, ay), "{n} 秒")
        self.assertEqual(g.cagrilar, [])

    def test_retry_then_success(self):
        g = FakeGoogle(lambda t: "Hello", hata_sayisi=2)
        self.assertEqual(cevir.bir_metni_cevir("Merhaba", "en", g, ayarlar(deneme=3)), "Hello")
        self.assertEqual(len(g.cagrilar), 3)
        g = FakeGoogle(lambda t: "Hello", hata_sayisi=5)
        with self.assertRaises(cevir.CeviriHatasi):
            cevir.bir_metni_cevir("Merhaba", "en", g, ayarlar(deneme=2))


class TestKatalog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def test_cache_hand_edits_stale_and_broken(self):
        (self.d / "en.json").write_text(json.dumps(
            {"Kaydet": "Store (hand edited)", "Eski metin": "Old"}), "utf-8")
        g = FakeGoogle(lambda t: {"Kaydet": "Save", "Merhaba @0@": "Hello @0@",
                                  "Bozuk @0@": "Broken"}[t])
        onb = cevir.Onbellek(self.d / "cache.json")
        ay = ayarlar(onbellek=onb)
        msgids = ["Kaydet", "Merhaba {ad}", "Bozuk {ad}"]
        r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: g, ay, isci=3)
        kat = json.loads((self.d / "en.json").read_text("utf-8"))
        self.assertEqual(kat, {"Bozuk {ad}": "", "Kaydet": "Store (hand edited)",
                               "Merhaba {ad}": "Hello {ad}"})          # stale entry dropped
        self.assertEqual((r.korunan["en"], r.cevrilen["en"], r.silinen["en"]), (1, 1, 1))
        self.assertEqual([(d, m) for d, m, _n in r.hatalar], [("en", "Bozuk {ad}")])
        self.assertTrue((self.d / "cache.json").exists())
        # second run: cache answers, Google is not called for the already translated text
        g.cagrilar.clear()
        cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: g,
                                   ayarlar(onbellek=cevir.Onbellek(self.d / "cache.json")))
        self.assertEqual(g.cagrilar, [])                 # Merhaba cached, Bozuk cached raw
        # --zorla overwrites the hand edit (from cache: no network either)
        cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: g,
                                   ayarlar(onbellek=cevir.Onbellek(self.d / "cache.json")),
                                   zorla=True)
        self.assertEqual(json.loads((self.d / "en.json").read_text("utf-8"))["Kaydet"], "Save")

    def test_batching_and_line_count_fallback(self):
        msgids = [f"Metin {i}" for i in range(7)]
        g = FakeGoogle(lambda t: t.replace("Metin", "Text"))
        r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: g,
                                       ayarlar(toplu=3), isci=2)
        self.assertEqual((len(g.cagrilar), r.istek, r.cevrilen["en"]), (3, 3, 7))
        self.assertEqual(json.loads((self.d / "en.json").read_text("utf-8"))["Metin 6"], "Text 6")
        # Google merges two lines of a batch → that batch is retried line by line
        g2 = FakeGoogle(lambda t: t.replace("Metin", "Text"),
                        bozuk=lambda out: out.replace("\nText 1", " Text 1"))
        cevir.kataloglari_guncelle(["Metin 0", "Metin 1"], ["de"], self.d, lambda dil: g2,
                                   ayarlar(toplu=5))
        self.assertEqual(g2.cagrilar, ["Metin 0\nMetin 1", "Metin 0", "Metin 1"])
        self.assertEqual(json.loads((self.d / "de.json").read_text("utf-8")),
                         {"Metin 0": "Text 0", "Metin 1": "Text 1"})

    def test_429_circuit_breaker_stops_and_resumes_from_cache(self):
        n = {"cagri": 0}

        def google(text):
            n["cagri"] += 1
            if n["cagri"] > 1:
                raise TooManyRequests("Server Error: You made too many requests")
            return text.replace("Metin", "Text")
        hiz = cevir.HizSiniri(0, engel_bekleme=0, en_cok_engel=3, uyu=lambda s: None)
        onb = cevir.Onbellek(self.d / "c.json")
        msgids = [f"Metin {i}" for i in range(4)]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: google,
                                           ayarlar(toplu=2, hiz=hiz, onbellek=onb), isci=1)
        self.assertTrue(r.engellendi)
        self.assertEqual(n["cagri"], 4)                  # 1 ok + 3 × 429, then it stops
        self.assertEqual((r.cevrilen["en"], r.kalan["en"]), (2, 2))
        self.assertEqual(r.hatalar, [])                  # not attempted ≠ rejected
        kat = json.loads((self.d / "en.json").read_text("utf-8"))
        self.assertEqual(kat["Metin 1"], "Text 1")
        self.assertEqual(kat["Metin 3"], "")
        g = FakeGoogle(lambda t: t.replace("Metin", "Text"))   # later: only the rest is sent
        cevir.kataloglari_guncelle(msgids, ["en"], self.d, lambda dil: g,
                                   ayarlar(onbellek=cevir.Onbellek(self.d / "c.json")))
        self.assertEqual(g.cagrilar, ["Metin 2\nMetin 3"])

    def test_denetle_reports_problems(self):
        kaynak = {"Kaydet": [], "Merhaba {ad}": [], "Aç": [], "Bekle…": []}
        (self.d / "kaynak.json").write_text(json.dumps(kaynak), "utf-8")
        (self.d / "de.json").write_text(json.dumps({
            "Kaydet": "Speichern", "Merhaba {ad}": "Hallo {name}", "Bekle…": "Warten",
            "Aç": "Öffnen Sie bitte jetzt sofort den Dialog", "Weg": "x"}), "utf-8")
        r = denetle.dil_denetle("de", kaynak, self.d)
        self.assertEqual(r.kapsama, 100.0)
        self.assertEqual(r.korunan, [])
        self.assertEqual([m for m, _ in r.yer_tutucu], ["Merhaba {ad}"])
        self.assertEqual([m for m, _ in r.uzun], ["Aç"])
        self.assertEqual(r.eski, ["Weg"])
        self.assertEqual([m for m, _ in r.uyari], ["Bekle…"])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(denetle.main(["--kaynak", str(self.d / "kaynak.json"),
                                           "--dizin", str(self.d), "--diller", "de"]), 1)
        (self.d / "de.json").write_text(json.dumps({"Kaydet": "Speichern"}), "utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(denetle.main(["--kaynak", str(self.d / "kaynak.json"),
                                           "--dizin", str(self.d), "--diller", "de"]), 0)
            self.assertEqual(denetle.main(["--kaynak", str(self.d / "kaynak.json"), "--dizin",
                                           str(self.d), "--diller", "de", "--min-kapsama", "50"]), 1)
            self.assertEqual(denetle.main(["--kaynak", str(self.d / "kaynak.json"),
                                           "--dizin", str(self.d), "--diller", "fr"]), 1)


class TestCikar(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pkg = Path(self.tmp.name) / "pkg"
        (self.pkg / "alt").mkdir(parents=True)
        (self.pkg / "a.py").write_text(textwrap.dedent('''
            from .i18n import _, N_
            ETIKET = {"x": N_("Müzik")}
            def f(ad, x):
                print(_("Merhaba {ad}").format(ad=ad))
                print(_("Uzun bir "
                        "cümle"))
                print(_(f"Kötü {ad}"))
                print(_(x))
                print(_("Toplam: %s" % x))
                print(_("Ad: {a}".format(a=x)))
                log.info("Günlük metni")          # not wrapped → not extracted
                return _("Müzik")
            '''), "utf-8")
        (self.pkg / "alt" / "b.py").write_text(
            'from .. import i18n\nA = i18n._("Bozuk {ad")\nB = _("Konumsal {}")\n', "utf-8")

    def test_scan(self):
        msgs, uyarilar = cikar.tara(self.pkg)
        self.assertEqual(list(msgs), sorted(msgs))
        self.assertEqual(msgs["Müzik"], ["a.py:3", "a.py:13"])
        self.assertEqual(msgs["Uzun bir cümle"], ["a.py:6"])
        self.assertIn("Merhaba {ad}", msgs)
        self.assertNotIn("Günlük metni", msgs)
        nedenler = " | ".join(str(u) for u in uyarilar)
        self.assertIn("a.py:8: _() f-string", nedenler)
        self.assertEqual([u.yer for u in uyarilar if not u.ciddi], ["a.py:9"])  # _(x): info
        self.assertIn("a.py:10: _() içinde metin birleştirme", nedenler)
        self.assertIn("a.py:11: _() içinde metin birleştirme", nedenler)
        self.assertIn("sözdizimi bozuk", nedenler)
        self.assertIn("konumsal", nedenler)

    def test_main_writes_stable_json_and_denetle_exit_code(self):
        out = Path(self.tmp.name) / "kaynak.json"
        with redirect_stdout(io.StringIO()), self._sessiz_stderr():
            self.assertEqual(cikar.main(["--kok", str(self.pkg), "--cikti", str(out)]), 0)
            ilk = out.read_text("utf-8")
            cikar.main(["--kok", str(self.pkg), "--cikti", str(out)])
            self.assertEqual(out.read_text("utf-8"), ilk)
            self.assertEqual(cikar.main(["--kok", str(self.pkg), "--denetle"]), 1)
            (self.pkg / "alt" / "b.py").unlink()
            (self.pkg / "a.py").write_text('X = _("Temiz {ad}")\nY = _(X)\n', "utf-8")
            self.assertEqual(cikar.main(["--kok", str(self.pkg), "--denetle"]), 0)

    @staticmethod
    def _sessiz_stderr():
        from contextlib import redirect_stderr
        return redirect_stderr(io.StringIO())

    def test_denetle_reports_lost_config_names(self):
        d = Path(tempfile.mkdtemp())
        kaynak = {"kurallar.toml [genel] bölümü": [], "genel.mod: {mod}": [], "süre [saniye]": []}
        (d / "en.json").write_text(json.dumps({
            "kurallar.toml [genel] bölümü": "the [general] section of kurallar.toml",
            "genel.mod: {mod}": "genel.mod: {mod}", "süre [saniye]": "duration [seconds]"}), "utf-8")
        (d / "kaynak.json").write_text(json.dumps(kaynak), "utf-8")
        r = denetle.dil_denetle("en", kaynak, d)
        self.assertEqual([m for m, _ in r.korunan], ["kurallar.toml [genel] bölümü"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(denetle.main(["--kaynak", str(d / "kaynak.json"), "--dizin", str(d),
                                           "--diller", "en", "--ayrinti"]), 1)
        self.assertIn("[en] AD", out.getvalue())
        (d / "en.json").write_text(json.dumps({
            "kurallar.toml [genel] bölümü": "the [genel] section of kurallar.toml",
            "genel.mod: {mod}": "genel.mod: {mod}", "süre [saniye]": "duration [seconds]"}), "utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(denetle.main(["--kaynak", str(d / "kaynak.json"), "--dizin", str(d),
                                           "--diller", "en"]), 0)

    def test_placeholder_helpers(self):
        self.assertEqual(cikar.yer_tutucular("{a} %s {b:+.1f} %%"), {"{a}": 1, "%s": 1,
                                                                    "{b:+.1f}": 1})
        self.assertEqual(cikar.yer_tutucular("%50 ses, %{n}"), {"{n}": 1})
        with self.assertRaises(ValueError):
            cikar.yer_tutucular("{a")

    def test_protected_config_names(self):
        """1 Kas: "[uygulamalar.sesli]" → "[apps.audio]", "genel.mod" → "general.mod"."""
        b = frozenset({"genel", "duz", "dinamik", "mod", "uygulamalar", "profil"})
        r = cikar.korunan_re(b)
        s = ("kurallar.toml [genel] kulaklik_desen; [mod.X] ve [uygulamalar.sesli]; genel.mod, "
             "uygulamalar.muzik / uygulamalar.film; node.passive, state.restore-props; "
             "{alan}: [dinamik]; [duz] bölümü; kulak tur dinle [saniye]; al [dosya]; [deneme]")
        self.assertEqual(sorted(m.group(0) for m in r.finditer(s)), sorted([
            "[genel]", "[mod.X]", "[uygulamalar.sesli]", "genel.mod", "uygulamalar.muzik",
            "uygulamalar.film", "node.passive", "state.restore-props", "[dinamik]", "[duz]"]))
        for metin in ("profiller.toml", "oyun.Bu", "genelde", "[saniye]", "dosya.json"):
            self.assertEqual(cikar.korunan_adlar(metin, r), {}, metin)
        self.assertTrue({"genel", "duz", "uygulamalar", "dinamik", "mod"} <= cikar.toml_bolumleri())
        m = cevir.maskele("kurallar.toml [genel] bölümüne ekleyin; genel.mod yanlış", "en")
        self.assertIn("[genel]", m.kaynak)
        self.assertIn("genel.mod", m.kaynak)
        self.assertNotIn("genel", cevir._TOKEN.sub("", m.metin))

    def test_percent_order_follows_ui_text(self):
        from oto_eq import ui_text
        self.assertEqual(cevir.YUZDE_BOSLUKLU, ui_text.PERCENT_SPACE)
        s = "sınırlayıcı %{yuzde:.0f} (>1 dB %{y1:.0f}), %50"
        self.assertEqual(cevir.yuzde_sirasi(s, "en"), "sınırlayıcı {yuzde:.0f}% (>1 dB {y1:.0f}%), %50")
        self.assertEqual(cevir.yuzde_sirasi(s, "de"),
                         "sınırlayıcı {yuzde:.0f}\u00a0% (>1 dB {y1:.0f}\u00a0%), %50")
        self.assertEqual(cevir.yuzde_sirasi("{x}% ok", "ja"), "{x}% ok")

    def test_language_list_matches_core(self):
        from oto_eq import i18n
        self.assertEqual(set(cikar.DILLER), set(i18n.DILLER) - {i18n.KAYNAK})
        self.assertTrue(i18n.CATALOG_DIR.is_dir())                 # oto_eq/i18n/ exists
        self.assertTrue(hasattr(i18n, "_"))                        # module wins over the dir


class FakeDeepLHTTP:
    """Fake HTTP layer for cevir.DeepL: /v2/usage + /v2/translate, no network.

    ``ceviri``: XML text → XML output; ``durumlar``: status codes to return (in order) for
    translate requests before answering normally; ``kalan``: remaining quota."""

    def __init__(self, ceviri=None, durumlar=(), kalan=500_000, usage_durum=200, sinir=500_000):
        self.ceviri = ceviri or (lambda t: t.replace("Metin", "Text"))
        self.durumlar = list(durumlar)
        self.kalan, self.sinir, self.usage_durum = kalan, sinir, usage_durum
        self.istekler: list[tuple[str, str, dict, dict | None]] = []
        self._lock = threading.Lock()

    def ceviri_istekleri(self):
        return [v for _y, u, _b, v in self.istekler if u.endswith("/v2/translate")]

    def __call__(self, yontem, url, basliklar, govde, zaman_asimi):
        veri = json.loads(govde.decode("utf-8")) if govde else None
        with self._lock:
            self.istekler.append((yontem, url, dict(basliklar), veri))
            if url.endswith("/v2/usage"):
                if self.usage_durum != 200:
                    return self.usage_durum, {}, json.dumps(
                        {"message": "bad key " + basliklar["Authorization"]}).encode()
                return 200, {}, json.dumps({"character_count": self.sinir - self.kalan,
                                            "character_limit": self.sinir}).encode()
            if self.durumlar:
                kod = self.durumlar.pop(0)
                if kod != 200:
                    return kod, {"Retry-After": "0"}, b'{"message": "x"}'
        out = [{"detected_source_language": "TR", "text": self.ceviri(t)} for t in veri["text"]]
        return 200, {}, json.dumps({"translations": out}, ensure_ascii=False).encode()


ANAHTAR = "gizli-0123-abcd:fx"


def deepl_motoru(http, **kw):
    kw.setdefault("uyu", lambda s: None)
    return cevir.DeepL(ANAHTAR, istek=http, **kw)


class TestDeepL(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def _kat(self, dil):
        return json.loads((self.d / f"{dil}.json").read_text("utf-8"))

    def test_batches_of_50_and_request_shape(self):
        http = FakeDeepLHTTP()
        msgids = [f"Metin {i} {{ad}}" for i in range(120)]
        r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, None, ayarlar(toplu=50), isci=3,
                                       deepl=deepl_motoru(http))
        istekler = http.ceviri_istekleri()
        self.assertEqual(sorted(len(v["text"]) for v in istekler), [20, 50, 50])
        self.assertEqual((r.istek, r.cevrilen["en"], r.hatalar), (3, 120, []))
        v = istekler[0]
        self.assertEqual((v["source_lang"], v["target_lang"], v["tag_handling"]),
                         ("TR", "EN-US", "xml"))
        self.assertTrue(all(t.endswith(' <k i="0">{ad}</k>') for v in istekler for t in v["text"]))
        self.assertEqual((v["ignore_tags"], v["context"]), (["k"], cevir.DEEPL_BAGLAM))
        yontem, url, bas, _ = http.istekler[0]
        self.assertEqual((yontem, url), ("GET", "https://api-free.deepl.com/v2/usage"))
        self.assertEqual(bas["Authorization"], "DeepL-Auth-Key " + ANAHTAR)
        self.assertEqual(self._kat("en")["Metin 7 {ad}"], "Text 7 {ad}")
        self.assertEqual(cevir.DeepL("abc").taban, "https://api.deepl.com")   # Pro key
        # byte limit splits too
        parca = cevir._parcala(["ş" * 30_000] * 5, 50, cevir.DEEPL_BAYT, cevir.deepl_bayt)
        self.assertEqual([len(p) for p in parca], [1, 1, 1, 1, 1])

    def test_tokens_survive_xml_and_broken_ones_are_rejected(self):
        def ceviri(t):
            return {'<k i="0">{ad}</k> ile <k i="1">{tur}</k> &amp; &lt;b&gt;':
                    "<k i='1'>{tur}</k> and <k i=\"0\" /> &amp; &lt;b&gt;",
                    'Bozuk <k i="0">{ad}</k> metin': "Broken text",
                    'Çift <k i="0">{ad}</k>': 'Double <k i="0">{ad}</k><k i="0">{ad}</k>'
                    }.get(t, t)
        http = FakeDeepLHTTP(ceviri)
        t = cevir.Terimler(terim={"Kişisel ton": {"en": "Personal tone"}})
        http2 = FakeDeepLHTTP(lambda s: s.replace(" kaydedildi", " saved"))
        msgids = ["{ad} ile {tur} & <b>", "Bozuk {ad} metin", "Çift {ad}"]
        r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, None, ayarlar(), isci=1,
                                       deepl=deepl_motoru(http))
        kat = self._kat("en")
        self.assertEqual(kat["{ad} ile {tur} & <b>"], "{tur} and {ad} & <b>")
        self.assertEqual((kat["Bozuk {ad} metin"], kat["Çift {ad}"]), ("", ""))
        self.assertEqual(sorted(m for _d, m, _n in r.hatalar), ["Bozuk {ad} metin", "Çift {ad}"])
        # glossary terms are masked for DeepL exactly like for Google
        cevir.kataloglari_guncelle(["Kişisel ton kaydedildi"], ["en"], self.d, None,
                                   ayarlar(terimler=t), deepl=deepl_motoru(http2))
        self.assertEqual(http2.ceviri_istekleri()[0]["text"],
                         ['<k i="0">Personal tone</k> kaydedildi'])
        self.assertEqual(self._kat("en")["Kişisel ton kaydedildi"], "Personal tone saved")

    def test_quota_precheck_skips_whole_language(self):
        msgids = [f"Metin {i}" for i in range(10)]              # 7-8 chars each → ~75/lang
        http = FakeDeepLHTTP(kalan=100)
        (self.d / "de.json").write_text(json.dumps({"Metin 0": "Alt"}), "utf-8")
        r = cevir.kataloglari_guncelle(msgids, ["de", "en"], self.d, None, ayarlar(),
                                       deepl=deepl_motoru(http))
        self.assertEqual(set(r.atlanan), {"de"})                 # en first, de does not fit
        self.assertEqual(r.atlanan["de"][1], 100 - r.tahmin["en"])
        self.assertEqual({v["target_lang"] for v in http.ceviri_istekleri()}, {"EN-US"})
        self.assertEqual(self._kat("de"), {"Metin 0": "Alt"})   # untouched, not half done
        self.assertEqual(self._kat("en")["Metin 9"], "Text 9")
        out = io.StringIO()
        r.yaz(out)
        self.assertIn("ATLANDI [de]", out.getvalue())

    def _main(self, http, *ek, anahtar=ANAHTAR):
        (self.d / "kaynak.json").write_text(json.dumps(
            {f"Metin {i}": [] for i in range(6)}), "utf-8")
        argv = ["--motor", "deepl", "--kaynak", str(self.d / "kaynak.json"), "--cikti-dizini",
                str(self.d), "--onbellek", str(self.d / "c.json"), "--toplu", "2",
                "--isci", "1", "--diller", "en", *ek]
        out, err = io.StringIO(), io.StringIO()
        eski = cevir._urllib_istek
        cevir._urllib_istek = http
        try:
            with unittest.mock.patch.dict("os.environ", {"DEEPL_AUTH_KEY": anahtar}), \
                    redirect_stdout(out), redirect_stderr(err):
                kod = cevir.main(argv)
        finally:
            cevir._urllib_istek = eski
        return kod, out.getvalue() + err.getvalue()

    def test_456_stops_with_exit_4_and_cache_resumes(self):
        http = FakeDeepLHTTP(durumlar=[200, 456])
        kod, cikti = self._main(http)
        self.assertEqual(kod, 4)
        self.assertIn("kota", cikti)
        self.assertEqual(len(http.ceviri_istekleri()), 2)        # stops after the 456
        onb = json.loads((self.d / "c.json").read_text("utf-8"))
        motor = deepl_motoru(FakeDeepLHTTP()).onbellek_motoru("en", "Metin 0")
        self.assertTrue(motor.startswith("deepl."))             # method + context hash
        self.assertEqual(onb, {f"{motor}\x1fen\x1fMetin 0": "Text 0",
                               f"{motor}\x1fen\x1fMetin 1": "Text 1"})
        self.assertEqual(self._kat("en")["Metin 1"], "Text 1")
        http2 = FakeDeepLHTTP()
        self.assertEqual(self._main(http2)[0], 0)
        self.assertEqual(sorted(t for v in http2.ceviri_istekleri() for t in v["text"]),
                         ["Metin 2", "Metin 3", "Metin 4", "Metin 5"])
        # a Google cache entry never answers for DeepL (and vice versa)
        onb = cevir.Onbellek(self.d / "c.json")
        self.assertIsNone(onb.al("en", "Metin 0"))
        self.assertEqual(onb.al("en", "Metin 0", motor), "Text 0")
        self.assertIsNone(onb.al("en", "Metin 0", "deepl"))       # old key format: stale

    def test_403_message_never_contains_the_key(self):
        kod, cikti = self._main(FakeDeepLHTTP(usage_durum=403))
        self.assertEqual(kod, 5)
        self.assertIn("anahtar", cikti)
        self.assertNotIn(ANAHTAR, cikti)
        self.assertNotIn("gizli", cikti)
        self.assertNotIn("gizli", repr(deepl_motoru(FakeDeepLHTTP())))
        kod, cikti = self._main(FakeDeepLHTTP(usage_durum=403), "--kota")
        self.assertEqual(kod, 5)
        self.assertNotIn("gizli", cikti)
        # other HTTP errors echoing the key are scrubbed
        def yansit(y, u, b, g, z):
            return 400, {}, json.dumps({"message": "bad " + b["Authorization"]}).encode()
        with self.assertRaises(cevir.CeviriHatasi) as cm:
            deepl_motoru(yansit).kalan_kota()
        self.assertNotIn("gizli", str(cm.exception))

    def test_empty_answer_with_context_is_retried_without_it(self):
        istekler = []

        def http(y, u, b, g, z):
            v = json.loads(g.decode("utf-8"))
            istekler.append(v)
            out = [{"text": "" if "context" in v and t.startswith("baskın") else
                    t.replace("baskın", "dominant").replace("Metin", "Text")} for t in v["text"]]
            return 200, {}, json.dumps({"translations": out}).encode()
        motor = deepl_motoru(http)
        with redirect_stderr(io.StringIO()) as err:
            sonuc = motor.toplu([("baskın: @0@", cevir.maskele("baskın: {profil}", "en")),
                                 ("Metin 1", None)], "de")
        self.assertEqual(sonuc["baskın: @0@"], "dominant: @0@")
        self.assertEqual(sonuc["Metin 1"], "Text 1")
        self.assertEqual(["context" in v for v in istekler], [True, False])
        self.assertEqual(istekler[1]["text"], ['baskın: <k i="0">{profil}</k>'])
        self.assertIn("bağlamsız", err.getvalue())

    def test_429_and_503_are_retried(self):
        uykular = []
        http = FakeDeepLHTTP(durumlar=[429, 503])
        motor = cevir.DeepL(ANAHTAR, istek=http, uyu=uykular.append)
        self.assertEqual(motor.toplu([("Metin 1", None)], "de"), {"Metin 1": "Text 1"})
        self.assertEqual(len(uykular), 2)
        http = FakeDeepLHTTP(durumlar=[429] * 9)
        sonuc = cevir.DeepL(ANAHTAR, istek=http, uyu=lambda s: None, deneme=3).toplu([("Metin", None)], "de")
        self.assertIsInstance(sonuc["Metin"], cevir.CeviriHatasi)

    def test_yukselt_replaces_google_entries_but_not_hand_edits(self):
        msgids = ["Metin 1", "Metin 2", "Metin 3", "Metin 4", "Metin 5"]
        g = FakeGoogle(lambda t: t.replace("Metin", "G-Text"))
        cevir.kataloglari_guncelle(msgids[:3], ["en"], self.d, lambda dil: g, ayarlar())
        kat = self._kat("en")
        kat["Metin 2"] = "Hand edited"                          # after Google wrote it
        kat["Metin 4"] = "Legacy"                               # no record: unknown origin
        (self.d / "en.json").write_text(json.dumps(kat), "utf-8")
        kayit = cevir.MotorKaydi(self.d / cevir.MOTOR_DOSYASI)
        self.assertEqual([kayit.motor("en", m, kat[m]) for m in msgids[:4]],
                         ["google", "elle", "google", "bilinmiyor"])
        # without --yukselt nothing existing is touched
        http = FakeDeepLHTTP()
        cevir.kataloglari_guncelle(msgids, ["en"], self.d, None, ayarlar(), deepl=deepl_motoru(http))
        self.assertEqual(self._kat("en")["Metin 1"], "G-Text 1")
        # --yukselt: Google + unknown → DeepL; hand edit stays; failed upgrade keeps old text
        http = FakeDeepLHTTP(lambda t: "" if t == "Metin 3" else t.replace("Metin", "D-Text"))
        r = cevir.kataloglari_guncelle(msgids, ["en"], self.d, None, ayarlar(),
                                       deepl=deepl_motoru(http), yukselt=True)
        self.assertEqual(self._kat("en"), {"Metin 1": "D-Text 1", "Metin 2": "Hand edited",
                                          "Metin 3": "G-Text 3", "Metin 4": "D-Text 4",
                                          "Metin 5": "Text 5"})
        self.assertEqual(sorted(t for v in http.ceviri_istekleri() for t in v["text"]),
                         ["Metin 1", "Metin 3", "Metin 3", "Metin 4"])   # "" → retried once
        self.assertEqual(r.yukseltilen["en"], 2)
        self.assertIn("eski çeviri korundu", r.hatalar[0][2])
        kayit = cevir.MotorKaydi(self.d / cevir.MOTOR_DOSYASI)
        kat = self._kat("en")
        self.assertEqual([kayit.motor("en", m, kat[m]) for m in msgids],
                         ["deepl", "elle", "google", "deepl", "deepl"])
        # a later hand edit of a DeepL entry is protected too
        http = FakeDeepLHTTP()
        kat["Metin 5"] = "Better"
        (self.d / "en.json").write_text(json.dumps(kat), "utf-8")
        cevir.kataloglari_guncelle(msgids, ["en"], self.d, None, ayarlar(),
                                   deepl=deepl_motoru(http), yukselt=True)
        self.assertEqual(self._kat("en")["Metin 5"], "Better")

    def test_key_lookup(self):
        p = self.d / "deepl-key"
        uyarilar = []
        self.assertIsNone(cevir.deepl_anahtari(p, {}, uyarilar.append))
        p.write_text("  abc:fx  \nikinci\n", "utf-8")
        p.chmod(0o644)
        self.assertEqual(cevir.deepl_anahtari(p, {}, uyarilar.append), "abc:fx")
        self.assertEqual(len(uyarilar), 1)
        self.assertNotIn("abc", uyarilar[0])
        p.chmod(0o600)
        uyarilar.clear()
        self.assertEqual(cevir.deepl_anahtari(p, {"DEEPL_AUTH_KEY": " env:fx "}, uyarilar.append),
                         "env:fx")
        self.assertEqual(cevir.deepl_anahtari(p, {}, uyarilar.append), "abc:fx")
        self.assertEqual(uyarilar, [])


if __name__ == "__main__":
    unittest.main()
