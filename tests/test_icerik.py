"""CHANGE 9 — içerik denetimi: thresholds, hysteresis, tap safety, router integration.

No real PipeWire: the tap gets fake pw-dump / pw-link / pw-record; the judge is scripted.
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from oto_eq import icerik as I
from oto_eq.models import Category, Decision, Stream

from tests.test_router import FakeClassifier, RouterTestBase

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)


def st(index=1, corked=False, binary="vivaldi-bin"):
    return Stream(index=index, sink_index=0, app_name="Vivaldi", binary=binary, pid=4966,
                  media_name="Playback", media_role="", node_name="Vivaldi", corked=corked)


def weak_movie(grup="vivaldi-bin:4427"):
    return Decision(Category.MOVIE, "film", "tarayıcı: YouTube video", kaynak="tarayici",
                    zayif=True, grup=grup)


def sure_music(grup="vivaldi-bin:4427"):
    return Decision(Category.MUSIC, "muzik", "tarayıcı: YouTube müzik listesi '&list=RD'",
                    kaynak="tarayici", grup=grup)


def make(cat, reason):
    prof = {Category.MUSIC: "muzik", Category.MOVIE: "film"}[cat]
    return Decision(cat, prof, reason, kaynak="icerik")


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptJudge:
    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    def judge(self, x, rate):
        self.calls += 1
        return self.verdicts.pop(0) if self.verdicts else I.Verdict(I.KARISIK, 0.3, 0.3)


class FakeTap:
    def __init__(self):
        self.captured: list[int] = []
        self.result = np.full(16000, 0.1, dtype=np.float32)

    def capture(self, serial, seconds=I.PROBE_S):
        self.captured.append(serial)
        return self.result

    def stop(self):
        pass


V_MUSIC_STRONG = I.Verdict(I.MUZIK, 0.93, 0.0, True)
V_MUSIC = I.Verdict(I.MUZIK, 0.5, 0.1, False)
V_SPEECH_STRONG = I.Verdict(I.KONUSMA, 0.02, 0.8, True)
V_SPEECH = I.Verdict(I.KONUSMA, 0.2, 0.45, False)
V_MIX = I.Verdict(I.KARISIK, 0.4, 0.4, False)
V_SILENT = I.Verdict(I.SESSIZ)


class TestScores(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(I.classify_scores(0.93, 0.0), (I.MUZIK, True))      # O Ses performance
        self.assertEqual(I.classify_scores(0.6, 0.15), (I.MUZIK, False))
        self.assertEqual(I.classify_scores(0.05, 0.8), (I.KONUSMA, True))
        self.assertEqual(I.classify_scores(0.2, 0.45), (I.KONUSMA, False))
        self.assertEqual(I.classify_scores(0.5, 0.4)[0], I.KARISIK)          # talk over music
        self.assertEqual(I.classify_scores(0.05, 0.05)[0], I.KARISIK)        # effects / ambience
        self.assertEqual(I.classify_scores(-1, 0)[0], I.KARISIK)                # bad input


class FakeYamnet:
    labels = ["Speech", "Music", "Narration, monologue", "Drum"]

    def __init__(self, act):
        self.act = np.array(act, dtype=np.float64)

    def activations(self, x, rate):
        return self.act


class TestJudge(unittest.TestCase):
    def test_music_and_speech(self):
        j = I.ContentJudge(FakeYamnet([0.0, 0.9, 0.01, 0.2]))
        v = j.judge(np.full(16000, 0.1, dtype=np.float32), 16000)
        self.assertEqual((v.kind, v.strong), (I.MUZIK, True))
        j = I.ContentJudge(FakeYamnet([0.3, 0.05, 0.85, 0.0]))     # narration counts as speech
        self.assertEqual(j.judge(np.full(16000, 0.1, dtype=np.float32), 16000).kind, I.KONUSMA)

    def test_silence_is_not_judged(self):
        y = FakeYamnet([0.0, 0.9, 0.0, 0.0])
        y.activations = mock.Mock(side_effect=AssertionError("model on silence"))
        j = I.ContentJudge(y)
        self.assertEqual(j.judge(np.zeros(16000, dtype=np.float32), 16000).kind, I.SESSIZ)
        self.assertEqual(j.judge(np.array([], dtype=np.float32), 16000).kind, I.SESSIZ)

    def test_missing_labels(self):
        y = FakeYamnet([0.0])
        y.labels = ["Drum"]
        with self.assertRaises(ValueError):
            I.ContentJudge(y)


class TestProber(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tap = FakeTap()
        self.changes = 0

    def prober(self, *verdicts):
        self.judge = ScriptJudge(*verdicts)

        def changed():
            self.changes += 1
        return I.ContentProber(self.judge, tap=self.tap, on_change=changed, clock=self.clock,
                               threaded=False)

    def test_not_eligible_untouched(self):
        p = self.prober(V_SPEECH_STRONG)
        voice = Decision(Category.VOICE, None, "tarayıcı: görüşme", kaynak="tarayici", zayif=True)
        rule = Decision(Category.MOVIE, "film", "kullanıcı kuralı", kaynak="kullanici", zayif=True)
        app = Decision(Category.MUSIC, "muzik", "müzik uygulaması", kaynak="uygulama")
        for d in (voice, rule, app, sure_music(grup="")):   # sure, named tab: no probe
            self.assertIs(p.adjust(st(), d, False, "k", make), d)
        self.assertEqual(self.tap.captured, [])

    def test_strong_verdict_overrides_after_one_probe(self):
        p = self.prober(V_MUSIC_STRONG)
        d = weak_movie()
        self.assertIs(p.adjust(st(), d, False, "k", make), d)     # probe runs, result next scan
        self.assertEqual(self.tap.captured, [1])
        nd = p.adjust(st(), d, False, "k", make)
        self.assertEqual((nd.category, nd.profile, nd.kaynak), (Category.MUSIC, "muzik", "icerik"))
        self.assertIn("ses içeriği", nd.reason)
        self.assertIn("YouTube video", nd.reason)                   # former reason kept
        self.assertEqual(nd.grup, d.grup)
        self.assertEqual(self.changes, 1)

    def test_weak_verdict_needs_two(self):
        p = self.prober(V_MUSIC, V_MUSIC)
        d = weak_movie()
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MOVIE)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "k", make)                         # second probe
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.assertEqual(self.tap.captured, [1, 1])

    def test_no_reprobe_before_interval(self):
        p = self.prober(V_MUSIC, V_MUSIC)
        for _ in range(5):
            p.adjust(st(), weak_movie(), False, "k", make)
        self.assertEqual(len(self.tap.captured), 1)

    def test_sustained_mix_returns_to_metadata(self):
        p = self.prober(V_MUSIC_STRONG, V_MIX, V_MIX)
        d = weak_movie()
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "k", make)                         # one mix: keep music
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "k", make)                         # two mixes: metadata
        self.assertIs(p.adjust(st(), d, False, "k", make), d)

    def test_flip_needs_two_opposite(self):
        p = self.prober(V_MUSIC_STRONG, V_SPEECH, V_SPEECH)
        d = weak_movie()
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "k", make)
        # speech twice = Film = the metadata decision itself (confirmed in the reason)
        nd = p.adjust(st(), d, False, "k", make)
        self.assertEqual(nd.category, Category.MOVIE)
        self.assertIn("doğruladı", nd.reason)

    def test_silence_retries_soon_without_verdict(self):
        p = self.prober(V_SILENT, V_MUSIC_STRONG)  # silence: retry in 5 s
        d = weak_movie()
        p.adjust(st(), d, False, "k", make)
        self.assertIs(p.adjust(st(), d, False, "k", make), d)
        self.clock.t += I.SILENT_RETRY_S
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)

    def test_tap_failure_backs_off(self):
        p = self.prober(V_MUSIC_STRONG)
        self.tap.result = None
        p.adjust(st(), weak_movie(), False, "k", make)
        self.assertEqual(self.judge.calls, 0)
        self.clock.t += I.SILENT_RETRY_S
        p.adjust(st(), weak_movie(), False, "k", make)
        self.assertEqual(len(self.tap.captured), 1)                 # FAIL_RETRY_S, not 5 s

    def test_corked_not_probed_and_resume_resets(self):
        p = self.prober(V_MUSIC_STRONG, V_SPEECH_STRONG)
        d = weak_movie()
        p.adjust(st(corked=True), d, False, "k", make)
        self.assertEqual(self.tap.captured, [])
        p.adjust(st(), d, False, "k", make)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        p.adjust(st(corked=True), d, False, "k", make)              # paused: keep, no probe
        self.assertEqual(len(self.tap.captured), 1)
        nd = p.adjust(st(), d, False, "k", make)                    # resumed: fresh probe
        self.assertEqual(len(self.tap.captured), 2)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MOVIE)
        self.assertIs(nd, d)

    def test_metadata_change_keeps_verdict_and_rechecks(self):
        # 2 Kas live: x.com's muted autoplay took Vivaldi's ONE media session over; the song
        # stream got "x.com → Film" metadata, the verdict was reset → Film/Müzik flapping
        p = self.prober(V_MUSIC_STRONG, V_MUSIC, V_SPEECH_STRONG)
        d = weak_movie()
        p.adjust(st(), d, False, "song|u1", make)
        self.assertEqual(p.adjust(st(), d, False, "song|u1", make).category, Category.MUSIC)
        nd = p.adjust(st(), d, False, "x.com|u2", make)     # session moved: verdict kept
        self.assertEqual(nd.category, Category.MUSIC)
        self.assertEqual(len(self.tap.captured), 1)          # not right after the last probe
        self.clock.t += I.QUICK_REPROBE_S
        p.adjust(st(), d, False, "x.com|u2", make)           # …but soon (8 s), not after 30
        self.assertEqual(len(self.tap.captured), 2)
        for key in ("song|u1", "x.com|u2", "song|u1"):     # flapping metadata: stays music
            self.assertEqual(p.adjust(st(), d, False, key, make).category, Category.MUSIC)
        self.assertEqual(len(self.tap.captured), 2)          # no probe storm
        self.clock.t += 1.0
        for key in ("x.com|u2", "song|u1", "x.com|u2"):
            p.adjust(st(), d, False, key, make)
        self.assertEqual(len(self.tap.captured), 2)
        self.clock.t += I.REPROBE_S
        p.adjust(st(), d, False, "talk|u3", make)            # really new content: strong speech
        self.assertEqual(p.adjust(st(), d, False, "talk|u3", make).category, Category.MOVIE)

    def test_nameless_sure_stream_is_measured_ahead(self):
        # sure metadata (music.youtube.com) is kept, but the verdict is ready when the
        # shared session jumps to a muted x.com video ("x.com → Film", weak)
        p = self.prober(V_MUSIC_STRONG)
        sure = sure_music()
        self.assertIs(p.adjust(st(), sure, False, "song", make), sure)
        self.assertEqual(len(self.tap.captured), 1)
        self.assertIs(p.adjust(st(), sure, False, "song", make), sure)   # sure wins
        xcom = weak_movie()
        nd = p.adjust(st(), xcom, False, "x.com", make)
        self.assertEqual(nd.category, Category.MUSIC)                     # at once

    def test_quick_second_probe(self):
        p = self.prober(V_MUSIC, V_MUSIC)
        d = weak_movie()
        p.adjust(st(), d, False, "k", make)
        self.clock.t += I.QUICK_REPROBE_S
        p.adjust(st(), d, False, "k", make)                  # second probe after 8 s, not 30
        self.assertEqual(len(self.tap.captured), 2)
        self.assertEqual(p.adjust(st(), d, False, "k", make).category, Category.MUSIC)
        self.clock.t += I.QUICK_REPROBE_S
        p.adjust(st(), d, False, "k", make)                  # then the normal interval
        self.assertEqual(len(self.tap.captured), 2)

    def test_ambiguous_sure_decision_is_checked(self):
        p = self.prober(V_SPEECH_STRONG)
        d = sure_music()
        p.adjust(st(), d, True, "k", make)
        nd = p.adjust(st(), d, True, "k", make)
        self.assertEqual((nd.category, nd.profile), (Category.MOVIE, "film"))

    def test_disabled_and_prune(self):
        p = self.prober(V_MUSIC_STRONG)
        p.enabled = False
        d = weak_movie()
        self.assertIs(p.adjust(st(), d, False, "k", make), d)
        self.assertEqual(self.tap.captured, [])
        p.enabled = True
        p.adjust(st(), d, False, "k", make)
        self.assertIn("1", p.status())
        p.prune(set())
        self.assertEqual(p.status(), {})

    def test_threaded_worker_calls_back(self):
        import threading
        done = threading.Event()
        p = I.ContentProber(ScriptJudge(V_MUSIC_STRONG), tap=self.tap, on_change=done.set,
                            clock=self.clock)
        p.adjust(st(), weak_movie(), False, "k", make)
        self.assertTrue(done.wait(5))
        self.assertEqual(p.adjust(st(), weak_movie(), False, "k", make).category, Category.MUSIC)
        p.stop()


class FakeProc:
    def __init__(self, path: Path, frames: np.ndarray):
        path.write_bytes(frames.astype(np.float32).tobytes())
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, t=None):
        return 0


def pw_node(nid, serial, name, cls):
    return {"id": nid, "type": "PipeWire:Interface:Node",
            "info": {"props": {"object.serial": serial, "node.name": name, "media.class": cls}}}


def pw_port(pid, node, direction, ch):
    return {"id": pid, "type": "PipeWire:Interface:Port",
            "info": {"direction": direction, "props": {"node.id": node, "audio.channel": ch}}}


class TestTap(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="oto-eq-test-tap-"))
        self.p = mock.patch.object(I, "_tap_dir", return_value=self.dir)
        self.p.start()
        self.links: list[list[str]] = []
        self.cmd: list[str] = []
        self.procs: list[FakeProc] = []

    def tearDown(self):
        self.p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def tap(self, stream_class="Stream/Output/Audio", channels=("FL", "FR")):
        base = [pw_node(255, 5759, "Vivaldi", stream_class)]
        base += [pw_port(300 + i, 255, "output", ch) for i, ch in enumerate(channels)]

        def dump():
            out = list(base)
            if self.cmd:
                props = json.loads(self.cmd[self.cmd.index("-P") + 1])
                out.append(pw_node(900, 9999, props["node.name"], props["media.class"]))
                out += [pw_port(901, 900, "input", "FL"), pw_port(902, 900, "input", "FR")]
            return out

        def run(cmd, timeout=5.0):
            self.links.append(cmd)
            return mock.Mock(returncode=0, stderr="")

        def popen(cmd, **kw):
            self.cmd = cmd
            frames = np.tile(np.array([[0.2, 0.0]], dtype=np.float32), (1600, 1))
            proc = FakeProc(Path(cmd[-1]), frames)
            self.procs.append(proc)
            return proc
        return I.StreamTap(dump=dump, run=run, popen=popen, sleep=lambda s: None)

    def test_capture_links_only_our_recorder_hidden_from_pulse(self):
        x = self.tap().capture(5759, 4.0)
        props = json.loads(self.cmd[self.cmd.index("-P") + 1])
        self.assertEqual(props["media.class"], I.TAP_CLASS)          # not a pulse source-output
        self.assertEqual(props["node.autoconnect"], "false")
        self.assertIn("--rate", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("--rate") + 1], str(I.TAP_RATE))
        self.assertEqual(self.links, [["pw-link", "300", "901"], ["pw-link", "301", "902"]])
        self.assertTrue(self.procs[0].terminated)
        self.assertEqual(x.shape, (1600,))
        self.assertAlmostEqual(float(x[0]), 0.1, places=5)            # stereo → mono
        self.assertEqual(list(self.dir.iterdir()), [])                # raw file removed

    def test_mono_stream_feeds_both(self):
        self.tap(channels=("MONO",)).capture(5759, 1.0)
        self.assertEqual(self.links, [["pw-link", "300", "901"], ["pw-link", "300", "902"]])

    def test_never_taps_a_sink_or_unknown_stream(self):
        self.assertIsNone(self.tap(stream_class="Audio/Sink").capture(5759))
        self.assertIsNone(self.tap().capture(1234))
        self.assertEqual(self.procs, [])


class ProbeClassifier(FakeClassifier):
    """FakeClassifier + the public decision() the content step needs."""

    def __init__(self, rules):
        super().__init__(rules)
        self.table["vivaldi-bin"] = weak_movie()

    def decision(self, cat, reason, kaynak=""):
        return make(cat, reason)


class SerialJudge:
    """Speech for stream 1, music for stream 2 (two tabs playing at once)."""

    def __init__(self):
        self.by = {1: V_SPEECH_STRONG, 2: V_MUSIC_STRONG}
        self.cur = None

    def judge(self, x, rate):
        return self.by[self.cur]


class SerialTap(FakeTap):
    def __init__(self, judge):
        super().__init__()
        self.j = judge

    def capture(self, serial, seconds=I.PROBE_S):
        self.j.cur = serial
        return super().capture(serial, seconds)


class TestRouterIntegration(RouterTestBase):
    def make_router(self):
        self.judge = SerialJudge()
        self.tap = SerialTap(self.judge)
        prober = I.ContentProber(self.judge, tap=self.tap, threaded=False)
        r = self.make(content_prober=prober)
        r.classifier = ProbeClassifier(self.rules)
        prober.clock = self.clock
        return r

    def test_two_tabs_split_by_content(self):
        r = self.make_router()
        self.pulse.add_stream(1, "vivaldi-bin")
        self.pulse.add_stream(2, "vivaldi-bin")
        r.scan()                                      # metadata: both Film, probes run
        self.assertEqual(sorted(self.tap.captured), [1, 2])
        self.clock.advance(10)
        r.scan()
        self.assertEqual(self.pulse.sink_of(1), "oto_eq_film")
        self.assertEqual(self.pulse.sink_of(2), "oto_eq_muzik")
        self.assertIn("ses içeriği", self.entry(2)["reason"])

    def test_switch_follows_ses_analizi_setting(self):
        r = self.make_router()
        r.variants.audio_on = False
        self.pulse.add_stream(2, "vivaldi-bin")
        r.scan()
        self.assertEqual(self.tap.captured, [])
        self.assertEqual(self.pulse.sink_of(2), "oto_eq_film")

    def test_prober_errors_never_break_routing(self):
        r = self.make_router()
        r.icerik.adjust = mock.Mock(side_effect=RuntimeError("boom"))
        self.pulse.add_stream(2, "vivaldi-bin")
        self.assertTrue(r.scan())
        self.assertEqual(self.pulse.sink_of(2), "oto_eq_film")

    def test_stop_stops_prober(self):
        r = self.make_router()
        r.icerik.stop = mock.Mock()
        r.stop(restore=False)
        r.icerik.stop.assert_called_once()


class TestBuild(unittest.TestCase):
    def test_off_without_yamnet_or_by_env(self):
        self.assertIsNone(I.build(None))
        self.assertIsNone(I.build(mock.Mock(helper=None)))
        with mock.patch.dict("os.environ", {"OTO_EQ_ICERIK": "0"}):
            self.assertIsNone(I.build(mock.Mock(helper=FakeYamnet([0, 0, 0, 0]))))
        self.assertIsInstance(I.build(mock.Mock(helper=FakeYamnet([0, 0, 0, 0]))), I.ContentProber)


if __name__ == "__main__":
    unittest.main()
