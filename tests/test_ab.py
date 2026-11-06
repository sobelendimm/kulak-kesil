"""Tests for oto_eq.ab (Kör A/B) — pure logic + run() with fakes; no audio, no IPC."""
from __future__ import annotations

import os
import random
import signal
import unittest
from unittest import mock

from oto_eq import ab

KEYS = ("bas", "vokal", "parlaklik")


class FakeKisisel:
    TON_KEYS = KEYS
    TON_LIMIT = 3.0

    def __init__(self, genel=None, tur=None):
        self.data = {"genel": dict(genel or {k: 0.0 for k in KEYS}), "tur": dict(tur or {})}
        self.calls = []

    def load(self):
        return {"genel": dict(self.data["genel"]),
                "tur": {k: dict(v) for k, v in self.data["tur"].items()}}

    def effective(self, data, profile, group=""):
        t = data["tur"].get(f"{profile}:{group}", {}) if group else {}
        return {k: max(-3.0, min(3.0, data["genel"].get(k, 0.0) + t.get(k, 0.0))) for k in KEYS}

    def set_genel(self, **vals):
        self.calls.append(("genel", vals))
        self.data["genel"].update(vals)

    def set_tur(self, profile, group, **vals):
        self.calls.append(("tur", profile, group, vals))
        self.data["tur"].setdefault(f"{profile}:{group}", {}).update(vals)


def status(group="elektronik", active=True, mode="auto", sink="oto_eq_muzik", dominant="muzik",
           ready=True):
    streams = [{"index": 52, "app": "Spotify", "binary": "spotify", "category": "muzik",
                "profile": "muzik", "reason": "x", "sink": sink}] if sink else []
    return {"ok": True, "active": active, "default_sink": "hs80", "mode": mode,
            "fixed_profile": None, "dominant": dominant, "sinks_ready": ready,
            "streams": streams,
            "variants": {"muzik": {"group": group, "label": "", "for": "", "source": "",
                                   "mode": "auto", "key": ""}}}


class Harness:
    """Fake deps + a simulated listener who likes ``target`` values."""

    def __init__(self, answers=None, target=None, st=None, kis=None, seed=1,
                 apply_ok=True, sleep_hook=None, signals=False):
        self.applies = []
        self.lines = []
        self.prompts = []
        self.sleeps = []
        self.answers = list(answers or [])
        self.target = target
        self.st = st if st is not None else status()
        self.kis = kis or FakeKisisel()
        self.apply_ok = apply_ok
        self.sleep_hook = sleep_hook
        self.deps = ab.Deps(apply_tone=self.apply, kisisel=lambda: self.kis, status=self.status,
                            out=self.out, ask=self.ask, sleep=self.sleep,
                            rng=random.Random(seed), install_signals=signals)

    def status(self):
        if isinstance(self.st, BaseException):
            raise self.st
        return self.st

    def apply(self, profile, group="", ramp_s=0.3, values=None):
        self.applies.append((profile, group, ramp_s, None if values is None else dict(values)))
        return self.apply_ok if values is not None else True

    def out(self, s="", end="\n"):
        self.lines.append(s + end)

    def sleep(self, s):
        self.sleeps.append(s)
        if self.sleep_hook:
            self.sleep_hook(self)

    def ask(self, prompt):
        self.prompts.append(prompt)
        if prompt.startswith("Hangisi") and self.target is not None and not self.answers:
            return self.listen()
        if not self.answers:
            raise EOFError
        a = self.answers.pop(0)
        if isinstance(a, BaseException):
            raise a
        return a

    def listen(self):
        side1, side2 = self.applies[-4][3], self.applies[-3][3]
        d1 = sum(abs(side1[k] - self.target[k]) for k in KEYS)
        d2 = sum(abs(side2[k] - self.target[k]) for k in KEYS)
        return "=" if d1 == d2 else ("1" if d1 < d2 else "2")

    @property
    def text(self):
        return "".join(self.lines)

    def run(self, argv=()):
        return ab.run(list(argv), self.deps)


class TestHelpers(unittest.TestCase):
    def test_round_half_and_clamp(self):
        self.assertEqual(ab.round_half(0.74), 0.5)
        self.assertEqual(ab.round_half(0.75), 1.0)
        self.assertEqual(ab.round_half(-0.75), -1.0)
        self.assertEqual(ab.round_half(-0.2), 0.0)
        self.assertEqual(ab.clamp(4.2), 3.0)
        self.assertEqual(ab.clamp(-9), -3.0)
        self.assertEqual(ab.fmt_db(0), "0.0")
        self.assertEqual(ab.fmt_db(1.5), "+1.5")
        self.assertEqual(ab.fmt_db(-1), "-1.0")

    def test_describe_change(self):
        self.assertEqual(ab.describe_change("bas", 0, 1.0), "bas biraz daha sıcak / dolgun")
        self.assertEqual(ab.describe_change("bas", 0, -0.5), "bas çok az daha sıkı / hafif")
        self.assertEqual(ab.describe_change("parlaklik", 1, -1), "tizler belirgin şekilde daha yumuşak")
        self.assertEqual(ab.describe_change("vokal", 0.5, 0.5), "değişmedi")
        self.assertIn("vokaller", ab.describe_change("vokal", 0, 1.5))

    def test_parse_answer(self):
        for raw, exp in (("1", "1"), (" 2 ", "2"), ("=", "="), ("0", "="), ("Fark yok", "="),
                         ("t", "t"), ("Q", "q"), ("ç", "q"), ("x", None), ("", None), (None, None)):
            self.assertEqual(ab.parse_answer(raw), exp, raw)

    def test_confidence_label(self):
        self.assertEqual(ab.confidence_label(0, 0), "veri yok")
        self.assertEqual(ab.confidence_label(3, 3), "yüksek")
        self.assertEqual(ab.confidence_label(2, 3), "orta")
        self.assertEqual(ab.confidence_label(1, 3), "düşük")
        self.assertEqual(ab.confidence_label(1, 1), "düşük")


class TestEstimator(unittest.TestCase):
    def test_plan_changes_one_dim_and_sequence(self):
        e = ab.Estimator({"bas": 0, "vokal": 1, "parlaklik": -1}, rng=random.Random(3))
        t = e.plan()
        diff = [k for k in KEYS if t.changed[k] != t.unchanged[k]]
        self.assertEqual(diff, [t.dim])
        self.assertEqual(abs(t.delta), 1.5)
        self.assertEqual(t.unchanged, {"bas": 0, "vokal": 1, "parlaklik": -1})
        seq = t.sequence()
        self.assertEqual([lab for lab, _ in seq], ["1", "2", "1", "2"])
        self.assertEqual(seq[0][1], seq[2][1])
        self.assertEqual(t.side(t.changed_label), t.changed)

    def test_labels_randomized(self):
        labels = {ab.Estimator({}, rng=random.Random(s)).plan().changed_label for s in range(20)}
        self.assertEqual(labels, {"1", "2"})

    def test_dims_balanced(self):
        e = ab.Estimator({}, rng=random.Random(5))
        seen = []
        for _ in range(9):
            t = e.plan()
            seen.append(t.dim)
            e.record(t, "=")
        self.assertEqual(sorted(seen.count(k) for k in KEYS), [3, 3, 3])

    def test_record_changed_moves_estimate(self):
        e = ab.Estimator({}, rng=random.Random(0), keys=("bas",))
        t = e.plan()
        self.assertEqual(e.record(t, t.changed_label), "changed")
        self.assertEqual(e.values()["bas"], t.changed["bas"])
        t2 = e.plan()                                        # keeps going the same way
        self.assertEqual(t2.delta, t.delta)

    def test_record_unchanged_flips_and_reversal_shrinks(self):
        e = ab.Estimator({}, rng=random.Random(0), keys=("bas",))
        t = e.plan()
        e.record(t, t.changed_label)                          # liked moving dir d
        t2 = e.plan()
        other = "1" if t2.changed_label == "2" else "2"
        self.assertEqual(e.record(t2, other), "unchanged")    # overshoot → reversal
        st = e.dims["bas"]
        self.assertEqual(st.step, 1.0)
        self.assertEqual(e.values()["bas"], t.changed["bas"])
        t3 = e.plan()
        self.assertEqual(t3.delta, -t.delta / 1.5 * 1.0)      # back the other way, smaller

    def test_equal_shrinks_down_to_half_db(self):
        e = ab.Estimator({}, rng=random.Random(0), keys=("bas",))
        for exp in (1.0, 0.5, 0.5):
            t = e.plan()
            self.assertEqual(e.record(t, "="), "equal")
            self.assertEqual(e.dims["bas"].step, exp)
        self.assertEqual(e.values()["bas"], 0.0)
        with self.assertRaises(ValueError):
            e.record(e.plan(), "x")

    def test_limit_respected(self):
        e = ab.Estimator({"bas": 3.0}, rng=random.Random(0), keys=("bas",))
        for _ in range(8):
            t = e.plan()
            self.assertLessEqual(t.changed["bas"], 3.0)
            self.assertGreaterEqual(t.changed["bas"], -3.0)
            self.assertNotEqual(t.changed["bas"], t.unchanged["bas"])
            e.record(t, t.changed_label if t.changed["bas"] > t.unchanged["bas"] else
                     ("1" if t.changed_label == "2" else "2"))
        self.assertEqual(e.values()["bas"], 3.0)

    def test_start_clamped_and_rounded(self):
        e = ab.Estimator({"bas": 4.2, "vokal": 0.3}, rng=random.Random(0))
        self.assertEqual(e.values(), {"bas": 3.0, "vokal": 0.5, "parlaklik": 0.0})

    def test_converges_with_simulated_listener(self):
        target = {"bas": 1.5, "vokal": -0.5, "parlaklik": -1.0}
        for seed in range(25):
            e = ab.Estimator({}, rng=random.Random(seed))
            for _ in range(15):
                t = e.plan()
                da = abs(t.unchanged[t.dim] - target[t.dim])
                db = abs(t.changed[t.dim] - target[t.dim])
                if da == db:
                    ans = "="
                else:
                    ans = t.changed_label if db < da else ("1" if t.changed_label == "2" else "2")
                e.record(t, ans)
            for k in KEYS:
                self.assertLessEqual(abs(e.values()[k] - target[k]), 0.5, (seed, k, e.values()))
            ok, total = e.confidence("bas")
            self.assertEqual(ok, total)                       # perfect listener is consistent

    def test_confidence_counts_contradictions(self):
        e = ab.Estimator({}, keys=("bas",))
        e.dims["bas"].value = 1.0
        e.history = [ab.Record("bas", 0.0, 1.5, "changed"),    # consistent
                     ab.Record("bas", 1.5, 0.0, "changed"),    # prefers 0 → inconsistent
                     ab.Record("bas", 0.5, 1.5, "unchanged"),  # tie → neutral
                     ab.Record("bas", 0.0, 1.0, "equal")]      # neutral
        self.assertEqual(e.confidence("bas"), (1, 2))


class TestSummaryAndSave(unittest.TestCase):
    def test_format_summary(self):
        e = ab.Estimator({}, rng=random.Random(0))
        e.dims["bas"].value = 1.0
        e.history = [ab.Record("bas", 0.0, 1.5, "changed")] * 3
        s = ab.format_summary(e, {k: 0.0 for k in KEYS})
        self.assertIn("Bas sıcaklığı", s)
        self.assertIn("0.0 dB", s)
        self.assertIn("+1.0 dB", s)
        self.assertIn("bas biraz daha sıcak / dolgun", s)
        self.assertIn("yüksek (3/3)", s)
        self.assertIn("değişmedi", s)
        self.assertEqual(ab.changed_dims(e, {k: 0.0 for k in KEYS}), ["bas"])

    def test_save_values_tur_offsets(self):
        kis = FakeKisisel(genel={"bas": 1.0, "vokal": 0.0, "parlaklik": -1.0},
                          tur={"muzik:elektronik": {"bas": 0.5, "vokal": 0.0, "parlaklik": 0.5}})
        data = kis.load()
        before = kis.effective(data, "muzik", "elektronik")       # 1.5, 0, -0.5
        after = dict(before, bas=2.5)
        vals = ab.save_values(kis, data, "muzik", "elektronik", before, after, KEYS)
        self.assertEqual(vals, {"bas": 1.5, "vokal": 0.0, "parlaklik": 0.5})
        self.assertEqual(kis.effective({"genel": data["genel"], "tur": {"muzik:elektronik": vals}},
                                       "muzik", "elektronik"), after)

    def test_save_values_genel(self):
        kis = FakeKisisel(genel={"bas": 1.0, "vokal": 0.5, "parlaklik": 0.0})
        data = kis.load()
        before = kis.effective(data, "film", "")
        vals = ab.save_values(kis, data, "film", "", before, dict(before, parlaklik=-1.5), KEYS)
        self.assertEqual(vals, {"bas": 1.0, "vokal": 0.5, "parlaklik": -1.5})

    def test_target_text(self):
        self.assertEqual(ab.target_text("muzik", "elektronik"), "Müzik · Elektronik / Hip-hop için")
        self.assertEqual(ab.target_text("muzik", ""), "Genel (tüm profiller) için")


class TestPreconditions(unittest.TestCase):
    def _check(self, h, needle):
        self.assertEqual(h.run(), 2)
        self.assertIn(needle, h.text)
        self.assertEqual(h.applies, [])
        self.assertEqual(h.kis.calls, [])

    def test_router_down(self):
        self._check(Harness(st=ConnectionRefusedError("x")), "çalışmıyor")

    def test_not_active(self):
        self._check(Harness(st=status(active=False)), "varsayılan çıkış değil")

    def test_mode_off(self):
        self._check(Harness(st=status(mode="off")), "kulak oto")

    def test_sinks_not_ready(self):
        self._check(Harness(st=status(ready=False)), "hazır değil")

    def test_nothing_playing_on_sink(self):
        self._check(Harness(st=status(sink="oto_eq_oyun")), "Müzik çıkışında çalan bir şey yok")
        self._check(Harness(st=status(sink=None)), "çalan bir şey yok")

    def test_paused(self):
        self._check(Harness(st=status(dominant=None)), "duraklatılmış")

    def test_error_reply(self):
        self._check(Harness(st={"ok": False, "error": "boom"}), "boom")

    def test_kisisel_missing(self):
        h = Harness()

        def missing():
            raise ImportError("no kisisel")
        h.deps.kisisel = missing
        self.assertEqual(h.run(), 1)
        self.assertIn("henüz kurulu değil", h.text)
        self.assertEqual(h.applies, [])

    def test_bad_args(self):
        for argv in (["--deneme", "0"], ["--deneme", "x"], ["--sure", "1"], ["--sure", "60"],
                     ["--profil", "duz"], ["--bilinmeyen"]):
            h = Harness()
            self.assertEqual(h.run(argv), 2, argv)
            self.assertEqual(h.applies, [])
        h = Harness()
        self.assertEqual(h.run(["--help"]), 0)
        self.assertIn("--deneme", h.text)

    def test_other_profile(self):
        h = Harness(st=status(sink="oto_eq_film", dominant="film"), answers=["q"])
        self.assertEqual(h.run(["--profil", "film"]), 0)
        self.assertEqual(h.applies[-1][:2], ("film", ""))


class TestRun(unittest.TestCase):
    def test_full_run_saves_tur(self):
        target = {"bas": 1.5, "vokal": 0.0, "parlaklik": -1.0}
        h = Harness(answers=[""], target=target)             # Enter, then the listener answers

        orig_ask = h.ask

        def ask(prompt):
            if prompt.startswith("Kaydedilsin"):
                h.prompts.append(prompt)
                return "e"
            return orig_ask(prompt)
        h.deps.ask = ask
        self.assertEqual(h.run(["--deneme", "12", "--sure", "4"]), 0)
        # 12 trials × 4 phases, each 4 s
        self.assertEqual(h.sleeps, [4.0] * 48)
        self.assertTrue(all(a[2] == 0.3 and a[0] == "muzik" and a[1] == "elektronik"
                            for a in h.applies))
        self.assertEqual(len(h.kis.calls), 1)
        kind, prof, grp, vals = h.kis.calls[0]
        self.assertEqual((kind, prof, grp), ("tur", "muzik", "elektronik"))
        for k in KEYS:
            self.assertLessEqual(abs(vals[k] - target[k]), 0.5)
        self.assertIsNone(h.applies[-1][3])                  # restored from kisisel.json
        self.assertIn("Kaydedildi", h.text)
        self.assertIn("Sonuç (12 yanıt)", h.text)
        self.assertIn("Müzik · Elektronik / Hip-hop için", h.text)

    def test_group_empty_saves_genel(self):
        h = Harness(st=status(group=""), answers=["", "2", "2", "2", "e"], seed=0)
        self.assertEqual(h.run(["--deneme", "3"]), 0)
        self.assertEqual(h.kis.calls[0][0], "genel")
        self.assertIn("Genel (tüm profiller) için", h.text)

    def test_save_prompt_speaks_the_users_language(self):
        # 1 Kas: "(e/h)" in every language; now the catalog's yes/no words are shown and
        # accepted, and the Turkish e/h still work
        from oto_eq import i18n
        kat = {"evet": "ja", "hayır": "nein",
               "Kaydedilsin mi ({hedef})? ({evet}/{hayir}): ": "Speichern ({hedef})? ({evet}/{hayir}): "}
        with mock.patch.dict(i18n._state, {"dil": "de", "katalog": kat}):
            h = Harness(st=status(group=""), answers=["", "2", "2", "2", "ja"], seed=0)
            self.assertEqual(h.run(["--deneme", "3"]), 0)
            self.assertEqual(h.kis.calls[0][0], "genel")
            self.assertTrue(any("(ja/nein)" in pr for pr in h.prompts), h.prompts)
            h = Harness(answers=["", "1", "2", "1", "nein"], seed=2)
            self.assertEqual(h.run(["--deneme", "3"]), 0)
            self.assertEqual(h.kis.calls, [])

    def test_no_save_restores(self):
        h = Harness(answers=["", "1", "2", "1", "h"], seed=2)
        self.assertEqual(h.run(["--deneme", "3"]), 0)
        self.assertEqual(h.kis.calls, [])
        self.assertIsNone(h.applies[-1][3])

    def test_no_change(self):
        # always prefer the unchanged side → estimate stays
        h = Harness(answers=[""], seed=4)
        orig = h.ask

        def ask(prompt):
            if prompt.startswith("Hangisi"):
                h.prompts.append(prompt)
                s1, s2 = h.applies[-4][3], h.applies[-3][3]
                return "1" if s1 == {k: 0.0 for k in KEYS} else "2"
            return orig(prompt)
        h.deps.ask = ask
        self.assertEqual(h.run(["--deneme", "4"]), 0)
        self.assertIn("Değişiklik yok", h.text)
        self.assertFalse(any(p.startswith("Kaydedilsin") for p in h.prompts))
        self.assertEqual(h.kis.calls, [])

    def test_quit_at_start(self):
        h = Harness(answers=["q"])
        self.assertEqual(h.run(), 0)
        self.assertIn("Vazgeçildi", h.text)
        self.assertEqual(h.sleeps, [])
        self.assertTrue(all(a[3] is None for a in h.applies))

    def test_quit_midway_partial_result(self):
        h = Harness(answers=["", "2", "q", "h"], seed=0)
        self.assertEqual(h.run(), 0)
        self.assertIn("Sonuç (1 yanıt)", h.text)

    def test_quit_before_any_answer(self):
        h = Harness(answers=["", "q"])
        self.assertEqual(h.run(), 0)
        self.assertIn("Hiç yanıt verilmedi", h.text)
        self.assertIsNone(h.applies[-1][3])

    def test_replay_and_invalid_answer(self):
        h = Harness(answers=["", "t", "x", "=", "h"])
        self.assertEqual(h.run(["--deneme", "1"]), 0)
        self.assertEqual(len(h.sleeps), 8)                   # replayed once
        self.assertIn("Lütfen 1, 2, =, t ya da q", h.text)

    def test_ctrl_c_during_playback_restores(self):
        def boom(hh):
            if len(hh.sleeps) == 3:
                raise KeyboardInterrupt
        h = Harness(answers=[""], sleep_hook=boom)
        self.assertEqual(h.run(), 130)
        self.assertIsNone(h.applies[-1][3])
        self.assertEqual(h.kis.calls, [])
        self.assertIn("İptal edildi", h.text)
        self.assertIn("geri yüklendi", h.text)

    def test_eof_restores(self):
        h = Harness(answers=["", "1"])                       # then EOF on next question
        self.assertEqual(h.run(), 130)
        self.assertIsNone(h.applies[-1][3])

    def test_apply_failure_is_error_and_restores(self):
        h = Harness(answers=[""], apply_ok=False)
        self.assertEqual(h.run(), 1)
        self.assertIn("ton uygulanamadı", h.text)
        self.assertIsNone(h.applies[-1][3])
        self.assertEqual(h.sleeps, [])

    def test_restore_failure_warns(self):
        h = Harness(answers=["q"])
        h.deps.apply_tone = lambda *a, **k: (_ for _ in ()).throw(OSError("pw-cli yok"))
        self.assertEqual(h.run(), 0)
        self.assertIn("geri yüklenemedi", h.text)

    def test_playback_stopped_midway(self):
        h = Harness(answers=["", "1", "q", "h"], seed=1)
        calls = {"n": 0}

        def st():
            calls["n"] += 1
            return status() if calls["n"] < 3 else status(sink=None)
        h.deps.status = st
        self.assertEqual(h.run(), 0)
        self.assertIn("çalma durdu", h.text)

    def test_genre_change_note(self):
        h = Harness(answers=["", "1", "1", "h"], seed=1)
        calls = {"n": 0}

        def st():
            calls["n"] += 1
            return status() if calls["n"] < 3 else status(group="metal_rock")
        h.deps.status = st
        self.assertEqual(h.run(["--deneme", "2"]), 0)
        self.assertIn("tür değişti", h.text)
        # every test apply still targets the start group
        self.assertTrue(all(a[1] == "elektronik" for a in h.applies[:-1]))
        self.assertEqual(h.applies[-1][1], "metal_rock")     # restore follows the live genre

    def test_sigterm_restores(self):
        old = signal.getsignal(signal.SIGTERM)

        def term(hh):
            os.kill(os.getpid(), signal.SIGTERM)
        h = Harness(answers=[""], sleep_hook=term, signals=True)
        self.assertEqual(h.run(), 130)
        self.assertIsNone(h.applies[-1][3])
        self.assertIs(signal.getsignal(signal.SIGTERM), old)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_and_language_independent_keys(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"bas biraz daha sıcak / dolgun": "Bass etwas wärmer",
                            "düşük": "niedrig"})
        self.assertEqual(ab.describe_change("bas", 0, 1.0), "Bass etwas wärmer")
        self.assertEqual(ab.confidence_label(0, 1), "niedrig")
        self.assertEqual(ab.confidence_key(0, 1), "dusuk")      # behaviour uses the key
        self.assertEqual(ab.confidence_key(0, 0), "yok")


if __name__ == "__main__":
    unittest.main()
