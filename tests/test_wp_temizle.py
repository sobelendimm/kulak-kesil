"""Tests for tools/wp_temizle.py: temp copies only + fake systemctl/pactl.

    python3 -m unittest discover -s tests -t . -p 'test_wp_temizle.py'

Never touches the real ~/.local/state/wireplumber (only reads it, if present,
to make a temporary copy) and never runs the real systemctl.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("wp_temizle", ROOT / "tools" / "wp_temizle.py")
wp = importlib.util.module_from_spec(_spec)
sys.modules["wp_temizle"] = wp      # dataclasses need the module registered
_spec.loader.exec_module(wp)

SP = """[stream-properties]
Output/Audio:media.role:Notification={"mute":false, "volume":1.000000}
Output/Audio:application.name:Firefox={"mute":false, "volume":1.000000}
Output/Audio:application.name:oto-eq-test-restore-pulse={"volume":1.000000, "target":"oto_eq_test_null"}
Audio/Sink:node.name:oto_eq_test_null={"volume":1.000000}
Audio/Sink:node.name:a3_repro_a_9575f0={"volume":1.000000}
Output/Audio:application.name:a3rep-stream={"volume":1.000000}
Audio/Sink:media.name:a4\\sbqtest\\sinput={"volume":1.000000}
Audio/Sink:node.name:oto_eq_perf_x={"volume":1.000000}
Audio/Source:media.name:Kulak\\sKesil\\s·\\sMikrofon\\s(test)={"volume":1.000000}
Audio/Source:media.name:Kulak\\sKesil\\s·\\sMikrofon={"volume":1.000000}
Audio/Sink:media.name:Kulak\\sKesil\\s·\\sDüz={"volume":1.000000}
Output/Audio:application.name:pw-play={"volume":1.000000}
Output/Audio:application.name:Spotify={"volume":1.000000}
Output/Audio:application.name:WEBRTC\\sVoiceEngine={"volume":1.000000}
Output/Audio:application.name:oto-eq-test-discord={"volume":1.000000}
Input/Audio:application.id:org.kde.plasma-pa={"volume":1.000000}
"""
DN = """[default-nodes]
default.configured.audio.sink=alsa_output.usb-Corsair_HS80.analog-stereo
default.configured.audio.sink.0=oto_eq_test_null
default.configured.audio.sink.1=alsa_output.pci-0000_15_00.6.analog-stereo
default.configured.audio.source=alsa_input.pci-0000_15_00.6.analog-stereo
default.configured.audio.sink.2=alsa_output.usb-Corsair_USB.analog-stereo
"""
DR = """[default-routes]
alsa_card.pci-0000_15_00.6:output:analog-output-lineout={"mute":false}
alsa_card.usb-Corsair_CORSAIR_HS80:profile:output:analog-stereo=["analog-output-headphones"]
"""
REMOVED_SP = {"oto-eq-test-restore-pulse", "oto_eq_test_null", "a3_repro_a_9575f0", "a3rep-stream",
              "a4\\sbqtest\\sinput", "oto_eq_perf_x", "Kulak\\sKesil\\s·\\sMikrofon\\s(test)"}


class FakeSystem:
    """Fake runner: systemctl/pactl. Optional hooks to simulate failures/WP flushes."""

    def __init__(self, sink="alsa_output.usb-Corsair_HS80.analog-stereo", source="mic",
                 fail_start=0, sink_after=None, on_stop=None):
        self.calls: list[list[str]] = []
        self.sink, self.source = sink, source
        self.fail_start = fail_start
        self.sink_after = sink_after
        self.on_stop = on_stop
        self.running = True

    def __call__(self, cmd):
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[:3] == ["systemctl", "--user", "stop"]:
            self.running = False
            if self.on_stop:
                self.on_stop()
        elif cmd[:3] == ["systemctl", "--user", "start"]:
            if self.fail_start > 0:
                self.fail_start -= 1
                rc = 1
            else:
                self.running = True
                if self.sink_after is not None:
                    self.sink = self.sink_after
        elif cmd == ["pactl", "get-default-sink"]:
            out = self.sink if self.running else ""
        elif cmd == ["pactl", "get-default-source"]:
            out = self.source if self.running else ""
        else:
            raise AssertionError(f"beklenmeyen komut: {cmd}")
        return subprocess.CompletedProcess(cmd, rc, out + "\n", "hata" if rc else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="wp-temizle-test-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        for name, text in (("stream-properties", SP), ("default-nodes", DN), ("default-routes", DR)):
            (self.d / name).write_text(text, encoding="utf-8")

    def text(self, name):
        return (self.d / name).read_text(encoding="utf-8")


class PlanTest(Base):
    def test_only_test_entries(self):
        plan = wp.make_plan(self.d)
        sp = plan.removed["stream-properties"]
        keys = {ln.split("=", 1)[0].split(":", 2)[2] for ln in sp}
        self.assertEqual(keys, REMOVED_SP)
        # a protected word wins over a test pattern (when unsure: keep)
        self.assertFalse(any("oto-eq-test-discord" in ln for ln in sp))
        for real in ("Firefox", "Spotify", "WEBRTC", "Notification", "plasma-pa", "pw-play",
                     "Mikrofon={", "Düz"):
            self.assertFalse(any(real in ln for ln in sp), real)
        self.assertEqual(plan.removed["default-nodes"], ["default.configured.audio.sink.0=oto_eq_test_null"])
        self.assertNotIn("default-routes", plan.removed)

    def test_protected_beats_pattern(self):
        self.assertFalse(wp.is_test_name("oto-eq-test-firefox"))   # protected word wins
        self.assertTrue(wp.is_test_name("oto-eq-test-cs2"))
        self.assertFalse(wp.is_test_name("Kulak Kesil · Mikrofon"))
        self.assertTrue(wp.is_test_name("Kulak Kesil · Mikrofon (test)"))

    def test_default_nodes_renumbered_without_gaps(self):
        new = wp.make_plan(self.d).new_text["default-nodes"]
        self.assertEqual(new, "[default-nodes]\n"
                              "default.configured.audio.sink=alsa_output.usb-Corsair_HS80.analog-stereo\n"
                              "default.configured.audio.sink.0=alsa_output.pci-0000_15_00.6.analog-stereo\n"
                              "default.configured.audio.source=alsa_input.pci-0000_15_00.6.analog-stereo\n"
                              "default.configured.audio.sink.1=alsa_output.usb-Corsair_USB.analog-stereo\n")

    def test_rest_byte_identical(self):
        new = wp.make_plan(self.d).new_text["stream-properties"]
        expect = "".join(ln for ln in SP.splitlines(keepends=True)
                         if not any(f":{k}=" in ln for k in REMOVED_SP))
        self.assertEqual(new, expect)

    def test_dry_run_is_default_and_changes_nothing(self):
        before = {n: (self.d / n).read_bytes() for n in wp.FILES}
        out: list[str] = []
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(wp.main(["--dizin", str(self.d)]), 0)
        out = buf.getvalue()
        self.assertIn("7 satır silinecek", out)
        self.assertIn("Toplam 8 satır", out)
        self.assertEqual(before, {n: (self.d / n).read_bytes() for n in wp.FILES})
        self.assertEqual(list(self.d.glob("*.yedek-*")), [])


class ApplyTest(Base):
    def run_apply(self, fake, **kw):
        msgs: list[str] = []
        ok = wp.apply(self.d, run=fake, say=msgs.append, sleep=lambda s: None,
                      stamp="T", wait_s=kw.pop("wait_s", 0.3))
        return ok, msgs

    def test_success(self):
        fake = FakeSystem()
        orig = {n: self.text(n) for n in wp.FILES}
        ok, msgs = self.run_apply(fake)
        self.assertTrue(ok, msgs)
        order = [c[2] for c in fake.calls if c[0] == "systemctl"]
        self.assertEqual(order, ["stop", "start"])
        for n in wp.FILES:
            self.assertEqual((self.d / f"{n}.yedek-T").read_text(encoding="utf-8"), orig[n])
        self.assertNotIn("oto_eq_test_null", self.text("stream-properties"))
        self.assertIn("Firefox", self.text("stream-properties"))
        self.assertEqual(self.text("default-routes"), DR)
        self.assertTrue(any("8 test kalıntısı silindi" in m for m in msgs))

    def test_uses_contents_flushed_by_wp_on_stop(self):
        def flush():   # WP writes its in-memory state while stopping
            with open(self.d / "stream-properties", "a", encoding="utf-8") as f:
                f.write('Output/Audio:application.name:oto-eq-test-late={"volume":1.0}\n'
                        'Output/Audio:application.name:Discord={"volume":0.5}\n')
        fake = FakeSystem(on_stop=flush)
        ok, _ = self.run_apply(fake)
        self.assertTrue(ok)
        t = self.text("stream-properties")
        self.assertNotIn("oto-eq-test-late", t)
        self.assertIn("Discord", t)

    def test_start_failure_restores_backups(self):
        fake = FakeSystem(fail_start=1)
        orig = {n: self.text(n) for n in wp.FILES}
        ok, msgs = self.run_apply(fake)
        self.assertFalse(ok)
        self.assertEqual({n: self.text(n) for n in wp.FILES}, orig)
        self.assertEqual([c[2] for c in fake.calls if c[0] == "systemctl"][-1], "start")
        self.assertTrue(fake.running)
        self.assertTrue(any("geri yükleniyor" in m for m in msgs))

    def test_default_sink_not_back_restores(self):
        fake = FakeSystem(sink_after="alsa_output.pci-0000_15_00.6.analog-stereo")
        orig = {n: self.text(n) for n in wp.FILES}
        ok, msgs = self.run_apply(fake)
        self.assertFalse(ok)
        self.assertEqual({n: self.text(n) for n in wp.FILES}, orig)
        self.assertTrue(fake.running)
        self.assertTrue(any("geri gelmedi" in m for m in msgs))

    def test_nothing_to_do_does_not_touch_wp(self):
        for n in wp.FILES:
            (self.d / n).write_text(f"[{n}]\nOutput/Audio:application.name:Firefox={{}}\n")
        fake = FakeSystem()
        ok, msgs = self.run_apply(fake)
        self.assertTrue(ok)
        self.assertEqual(fake.calls, [])
        self.assertEqual(list(self.d.glob("*.yedek-*")), [])


class RealCopyTest(unittest.TestCase):
    """Run against a COPY of the real state files (if present) with the fake system."""

    def test_real_copy(self):
        real = Path.home() / ".local/state/wireplumber"
        if not (real / "stream-properties").exists():
            self.skipTest("gerçek WirePlumber durumu yok")
        d = Path(tempfile.mkdtemp(prefix="wp-temizle-real-"))
        self.addCleanup(shutil.rmtree, d, True)
        for n in wp.FILES:
            if (real / n).exists():
                shutil.copy2(real / n, d / n)
        before = (d / "stream-properties").read_bytes()
        plan = wp.make_plan(d)
        ok = wp.apply(d, run=FakeSystem(), say=lambda m: None, sleep=lambda s: None, stamp="T")
        self.assertTrue(ok)
        after = (d / "stream-properties").read_bytes()
        removed = set(plan.removed.get("stream-properties", []))
        kept = [ln for ln in before.decode("utf-8", "surrogateescape").splitlines()
                if ln not in removed]
        self.assertEqual(after.decode("utf-8", "surrogateescape").splitlines(), kept)
        for real_app in (b"Firefox", b"Spotify", b"speech-dispatcher"):
            self.assertEqual(before.count(real_app), after.count(real_app))


if __name__ == "__main__":
    unittest.main()
