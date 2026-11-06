"""Tests for oto_eq.ses_model (front-end + mapping offline; real model only if installed)."""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from oto_eq import ses_model as sm
from oto_eq.models import MUSIC_GROUPS

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)

try:
    import onnxruntime  # noqa: F401
    HAVE_ORT = True
except ImportError:
    HAVE_ORT = False

REAL = sm.MODEL_DIR / sm.MAIN_MODEL


def tone(freq=1000.0, rate=22050, seconds=6.0, amp=0.3):
    t = np.arange(int(rate * seconds)) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class FakeSession:
    """Mimics onnxruntime.InferenceSession: returns fixed activations per patch."""

    def __init__(self, act, in_name="melspectrogram", outs=("activations", "embeddings")):
        self.act = np.asarray(act, dtype=np.float32)
        self.in_name, self.outs = in_name, outs
        self.calls = []

    def get_inputs(self):
        return [SimpleNamespace(name=self.in_name, shape=["n", 128, 96])]

    def get_outputs(self):
        return [SimpleNamespace(name=o, shape=["n", self.act.size]) for o in self.outs]

    def run(self, names, feeds):
        x = feeds[self.in_name]
        self.calls.append(x.shape)
        return [np.tile(self.act, (x.shape[0], 1))]


def fake_clf(key, labels, act):
    return sm.OnnxClassifier(Path(key + "-1.onnx"), sm.SPECS[key], labels, FakeSession(act))


class TestFrontEnd(unittest.TestCase):
    def test_resample_keeps_frequency_and_length(self):
        y = sm.resample(tone(1000.0, 22050, 2.0), 22050, 16000)
        self.assertEqual(y.size, 32000)
        spec = np.abs(np.fft.rfft(y))
        self.assertAlmostEqual(np.argmax(spec) * 16000 / y.size, 1000.0, delta=2.0)
        self.assertAlmostEqual(float(np.sqrt(np.mean(y[1000:-1000] ** 2))), 0.3 / np.sqrt(2), delta=0.01)

    def test_resample_identity_and_upsample(self):
        x = tone(440.0, 16000, 1.0)
        np.testing.assert_allclose(sm.resample(x, 16000, 16000), x, atol=1e-6)
        self.assertEqual(sm.resample(tone(440.0, 8000, 1.0), 8000, 16000).size, 16000)

    def test_slaney_filters(self):
        w = sm.mel_filters(16000, 512, 96, 0.0, 8000.0, "slaney")
        self.assertEqual(w.shape, (96, 257))
        self.assertTrue((w >= 0).all())
        self.assertTrue((w.sum(axis=1) > 0).all())
        peaks = w.argmax(axis=1)
        self.assertTrue((np.diff(peaks) >= 0).all())
        # area normalisation: triangle area in Hz ≈ 1 (for bands wider than one bin)
        hz = 16000 / 512
        self.assertAlmostEqual(float(w[60].sum() * hz), 1.0, delta=0.1)

    def test_htk_tf_filters(self):
        w = sm.mel_filters(16000, 512, 64, 125.0, 7500.0, "htk_tf")
        self.assertEqual(w.shape, (64, 257))
        self.assertEqual(float(w[:, 0].max()), 0.0)
        self.assertLessEqual(float(w.max()), 1.0 + 1e-9)
        self.assertGreater(float(w.max()), 0.9)
        self.assertTrue((w[:, : int(110 / 31.25)] == 0).all())   # nothing below fmin

    def test_log_mel_shapes(self):
        x = sm.resample(tone(), 22050, 16000)
        m = sm.log_mel(x, sm.SPECS["discogs-effnet"].fe)
        self.assertEqual(m.shape, (1 + 96000 // 256, 96))
        self.assertEqual(m.dtype, np.float32)
        self.assertGreater(float(m.max()), 0.0)
        y = sm.log_mel(x, sm.SPECS["audioset-yamnet"].fe)
        self.assertEqual(y.shape, (1 + (96000 - 400) // 160, 64))
        # 1 kHz tone: the loudest YAMNet band is the one around 1 kHz
        w = sm.mel_filters(16000, 512, 64, 125.0, 7500.0, "htk_tf")
        self.assertEqual(int(y.mean(axis=0).argmax()), int(w[:, 32].argmax()))

    def test_patches(self):
        spec = np.zeros((376, 96), np.float32)
        p = sm.patches(spec, 128, 62)
        self.assertEqual(p.shape, (5, 128, 96))
        self.assertEqual(sm.patches(spec[:100], 128, 62).shape[0], 0)
        long = np.zeros((4000, 96), np.float32)
        self.assertEqual(sm.patches(long, 128, 62, max_n=8).shape[0], 8)

    def test_prepare(self):
        self.assertIsNone(sm.prepare(np.zeros(22050 * 6, np.float32), 22050, 16000))
        self.assertIsNone(sm.prepare(tone(seconds=1.0), 22050, 16000))           # too short
        self.assertIsNone(sm.prepare(tone(), 0, 16000))
        self.assertIsNone(sm.prepare("x", 22050, 16000))
        quiet = tone(amp=0.01)                                                   # ≈ -43 dBFS
        y = sm.prepare(quiet, 22050, 16000)
        self.assertAlmostEqual(20 * np.log10(np.sqrt(np.mean(y ** 2))), sm.TARGET_DBFS, delta=0.5)
        y = sm.prepare(tone(amp=0.002), 22050, 16000)                            # ≈ -57 dBFS: gain capped
        self.assertAlmostEqual(20 * np.log10(np.sqrt(np.mean(y ** 2))),
                               20 * np.log10(0.002 / np.sqrt(2)) + sm.MAX_GAIN_DB, delta=0.5)
        x = tone()
        x[100] = np.nan
        self.assertTrue(np.isfinite(sm.prepare(x, 22050, 16000)).all())
        stereo = np.stack([tone(), tone()], axis=1)
        self.assertEqual(sm.prepare(stereo, 22050, 16000).size, 96000)
        y = sm.prepare(tone(), 22050, 16000, normalize=False)
        self.assertAlmostEqual(float(np.abs(y).max()), 0.3, delta=0.02)


class TestMapping(unittest.TestCase):
    def test_tables_are_valid(self):
        for table in (sm.DISCOGS_GENRE, sm.DISCOGS_STYLE, sm.MSD_TAGS, sm.YAMNET_LABELS):
            for label, w in table.items():
                self.assertTrue(set(w) <= set(MUSIC_GROUPS), label)
                self.assertLessEqual(sum(w.values()), 1.0 + 1e-9, label)
        for label in sm.DISCOGS_STYLE:
            self.assertIn(label.split("---")[0], sm.DISCOGS_GENRE, label)

    def test_discogs_examples(self):
        cases = {
            "Rock---Heavy Metal": "metal_rock", "Rock---Black Metal": "metal_rock",
            "Hip Hop---Trap": "elektronik", "Electronic---Dubstep": "elektronik",
            "Electronic---Drum n Bass": "elektronik", "Reggae---Dub": "elektronik",
            "Classical---Baroque": "klasik_akustik", "Jazz---Bop": "klasik_akustik",
            "Latin---Tango": "klasik_akustik", "Rock---Acoustic": "klasik_akustik",
            "Pop---Ballad": "pop_vokal", "Folk, World, & Country---Laïkó": "pop_vokal",
            "Funk / Soul---Soul": "pop_vokal",
            "Non-Music---Spoken Word": "konusma", "Non-Music---Audiobook": "konusma",
            "Children's---Story": "konusma",
        }
        for label, group in cases.items():
            w = sm.discogs_weight(label)
            self.assertEqual(max(w, key=w.get), group, label)
        self.assertEqual(sm.discogs_weight("Non-Music---Field Recording"), {})
        self.assertEqual(sm.discogs_weight("Unknown---X"), {})

    @unittest.skipUnless((sm.MODEL_DIR / "discogs-effnet-bsdynamic-1.json").is_file(), "model json yok")
    def test_every_discogs_label_known(self):
        labels = json.loads((sm.MODEL_DIR / "discogs-effnet-bsdynamic-1.json").read_text())["classes"]
        self.assertEqual(len(labels), 400)
        for lab in labels:
            self.assertTrue(lab.split("---")[0] in sm.DISCOGS_GENRE, lab)
        for lab in sm.DISCOGS_STYLE:
            self.assertTrue(lab in labels, f"modelde olmayan stil: {lab}")

    @unittest.skipUnless((sm.MODEL_DIR / "audioset-yamnet-1.json").is_file(), "yamnet json yok")
    def test_yamnet_labels_exist(self):
        labels = json.loads((sm.MODEL_DIR / "audioset-yamnet-1.json").read_text())["classes"]
        for lab in list(sm.YAMNET_LABELS) + [sm.YAMNET_MUSIC]:
            self.assertTrue(lab in labels, lab)


class TestGenreModel(unittest.TestCase):
    LABELS = ["Rock---Heavy Metal", "Electronic---Techno", "Classical---Baroque", "Pop---Ballad",
              "Non-Music---Spoken Word", "Rock---Pop Rock"]

    def model(self, act, speech_act=None):
        main = fake_clf("discogs-effnet", self.LABELS, act)
        sp = None
        if speech_act is not None:
            sp = fake_clf("audioset-yamnet", ["Speech", "Music", "Rock music"], speech_act)
        return sm.GenreModel(main, sp)

    def test_predict_distribution(self):
        m = self.model([0.8, 0.1, 0.0, 0.05, 0.0, 0.2])
        p = m.predict(tone(), 22050)
        self.assertEqual(set(p), set(MUSIC_GROUPS))
        self.assertAlmostEqual(sum(p.values()), 1.0, places=3)
        self.assertEqual(max(p, key=p.get), "metal_rock")
        self.assertAlmostEqual(p["metal_rock"], 0.9 / 1.15, places=3)   # (0.8 + 0.5·0.2) / Σ
        self.assertEqual(m.last_top[0], ("Rock---Heavy Metal", 0.8))
        self.assertEqual(m.main.session.calls[0][1:], (128, 96))

    def test_silence_and_short_give_none(self):
        m = self.model([0.8, 0, 0, 0, 0, 0])
        self.assertIsNone(m.predict(np.zeros(22050 * 6, np.float32), 22050))
        self.assertIsNone(m.predict(tone(seconds=1.0), 22050))
        self.assertEqual(m.main.session.calls, [])

    def test_all_zero_activations_uniform(self):
        p = self.model([0.0] * 6).predict(tone(), 22050)
        for v in p.values():
            self.assertAlmostEqual(v, 0.2, places=3)

    def test_speech_helper_overrides_konusma(self):
        m = self.model([0.1, 0.6, 0.0, 0.0, 0.3, 0.0], speech_act=[0.9, 0.1, 0.0])
        p = m.predict(tone(), 22050)
        self.assertAlmostEqual(p["konusma"], 0.9, places=3)
        self.assertAlmostEqual(sum(p.values()), 1.0, places=3)
        self.assertGreater(p["elektronik"], p["metal_rock"])
        m = self.model([0.1, 0.6, 0.0, 0.0, 0.3, 0.0], speech_act=[0.05, 0.95, 0.0])
        p = m.predict(tone(), 22050)
        self.assertLess(p["konusma"], 0.06)
        self.assertGreater(p["elektronik"], 0.8)

    def test_helper_genres_are_averaged(self):
        main = fake_clf("discogs-effnet", self.LABELS, [0.0, 0.8, 0.0, 0.0, 0.0, 0.0])
        yam = fake_clf("audioset-yamnet", ["Speech", "Music", "Pop music"], [0.0, 0.9, 0.6])
        p = sm.GenreModel(main, yam).predict(tone(), 22050)
        self.assertAlmostEqual(p["elektronik"], 0.5, places=3)
        self.assertAlmostEqual(p["pop_vokal"], 0.5, places=3)
        self.assertEqual(p["konusma"], 0.0)

    def test_speech_ratio_needs_clear_activity(self):
        m = self.model([0.0, 0.8, 0.0, 0.0, 0.0, 0.0], speech_act=[0.03, 0.02, 0.0])
        self.assertIsNone(m.speech_ratio(np.array([0.03, 0.02, 0.0])))
        p = m.predict(tone(), 22050)
        self.assertAlmostEqual(sum(p.values()), 1.0, places=3)

    def test_top_k_suppresses_many_small(self):
        clf = fake_clf("discogs-effnet", ["Rock---Punk"] * 20 + ["Electronic---House"], [0.04] * 20 + [0.5])
        self.assertGreater(clf.group_scores(clf.activations(tone(), 22050))[0], 0.5)  # all: 0.8 rock
        s = clf.group_scores(clf.activations(tone(), 22050), top_k=10)
        self.assertGreater(s[1], s[0])

    def test_session_error_returns_none(self):
        m = self.model([0.5] * 6)
        m.main.session.run = mock.Mock(side_effect=RuntimeError("boom"))
        self.assertIsNone(m.predict(tone(), 22050))

    def test_long_input_capped_patches(self):
        m = self.model([0.5] * 6)
        m.predict(tone(seconds=40.0), 22050)
        self.assertLessEqual(m.main.session.calls[0][0], sm.MAX_PATCHES)


class TestLoadModel(unittest.TestCase):
    def test_missing_dir_or_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(sm.load_model(d))
            self.assertIsNone(sm.load_model(Path(d) / "yok.onnx"))
            self.assertIsNone(sm.load_model(Path(d) / "nothere"))

    def test_without_onnxruntime(self):
        with mock.patch.dict(sys.modules, {"onnxruntime": None}):
            self.assertIsNone(sm.load_model())

    @unittest.skipUnless(HAVE_ORT, "onnxruntime yok")
    def test_corrupt_model_file(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / sm.MAIN_MODEL).write_bytes(b"not an onnx model")
            (Path(d) / sm.MAIN_MODEL).with_suffix(".json").write_text('{"classes": ["Rock---Punk"]}')
            self.assertIsNone(sm.load_model(d))

    @unittest.skipUnless(HAVE_ORT, "onnxruntime yok")
    def test_json_missing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / sm.MAIN_MODEL).write_bytes(b"x")
            self.assertIsNone(sm.load_model(d))

    def test_unknown_model_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "baska-model.onnx"
            p.write_bytes(b"x")
            p.with_suffix(".json").write_text('{"classes": []}')
            self.assertIsNone(sm.load_model(p))


@unittest.skipUnless(HAVE_ORT and REAL.is_file(), "gerçek model kurulu değil")
class TestRealModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = sm.load_model()

    def test_loaded(self):
        self.assertIsNotNone(self.model)
        self.assertEqual(self.model.main.input_name, "melspectrogram")
        self.assertEqual(len(self.model.main.labels), 400)

    def test_predict_on_synthetic_beat(self):
        rate = 22050
        t = np.arange(rate * 6) / rate
        kick = np.sin(2 * np.pi * 55 * t) * np.exp(-((t % 0.5) * 12))           # 120 BPM kick
        hat = np.random.default_rng(1).normal(0, 0.05, t.size) * ((t % 0.25) < 0.02)
        x = (0.5 * kick + hat).astype(np.float32)
        t0 = time.perf_counter()
        p = self.model.predict(x, rate)
        dt = time.perf_counter() - t0
        self.assertIsNotNone(p)
        self.assertEqual(set(p), set(MUSIC_GROUPS))
        self.assertAlmostEqual(sum(p.values()), 1.0, places=2)
        self.assertTrue(self.model.last_top)
        self.assertLess(dt, 2.0)

    def test_silence_none(self):
        self.assertIsNone(self.model.predict(np.zeros(22050 * 6, np.float32), 22050))


if __name__ == "__main__":
    unittest.main()
