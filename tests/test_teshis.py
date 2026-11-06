"""Tests for oto_eq.teshis formatting/collection with fake streams (no audio graph access)."""
from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import teshis
from oto_eq.models import MediaPlayer, ProcInfo, Sink, Stream
from tests.test_classifier import Env, FIREFOX_PID, INIT, REAPER730, STEAM, proc

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)


def stream(index, app, binary, pid, media_name="", props=None):
    p = {"application.name": app, "application.process.binary": binary,
         "application.process.id": str(pid), "media.name": media_name,
         "node.name": app, "client.api": "pipewire-pulse", "secret.key": "x"}
    p.update(props or {})
    return Stream(index=index, sink_index=7, app_name=app, binary=binary, pid=pid,
                  media_name=media_name, media_role="", node_name=app, corked=False, props=p)


class TestTeshis(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.env.add(proc(8000, "cs2", env={"SteamAppId": "730"}, ancestors=[REAPER730, STEAM]))
        self.env.add(proc(FIREFOX_PID, "firefox"))
        self.c = self.env.classifier()
        self.sinks = {7: "alsa_output.usb-Corsair_HS80"}
        self.ff_player = MediaPlayer("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                                     "Mozilla firefox", "Playing", "Haber", "Kanal",
                                     "https://www.youtube.com/watch?v=1")

    def test_collect_cs2(self):
        d = teshis.collect(stream(1, "cs2", "cs2", 8000), self.c, [], self.sinks)
        self.assertEqual(d["decision"]["category"], "cs2")
        self.assertEqual(d["host_pid"], 8000)
        self.assertEqual(d["sink"], "alsa_output.usb-Corsair_HS80")
        self.assertEqual(d["process"]["env"], {"SteamAppId": "730"})
        self.assertEqual(d["process"]["ancestors"][0]["comm"], "reaper")
        self.assertNotIn("secret.key", d["props"])            # only key props
        json.dumps(d, ensure_ascii=False)                       # serialisable

    def test_block_plain(self):
        d = teshis.collect(stream(2, "Firefox", "firefox", FIREFOX_PID, "Haber - YouTube"),
                           self.c, [self.ff_player], self.sinks)
        out = teshis.format_block(d, color=False)
        self.assertNotIn("\x1b[", out)
        self.assertIn("━━ #2  Firefox  (firefox)", out)
        self.assertIn("app.name", out)
        self.assertIn("process.binary = firefox", out)
        self.assertIn("firefox.instance_1_280 [Playing] 'Haber'", out)
        self.assertIn("KARAR      : film → film", out)
        self.assertIn("✔ tarayici", out)
        self.assertIn("çıkış: alsa_output.usb-Corsair_HS80", out)

    def test_block_color_and_unresolved(self):
        d = teshis.collect(stream(3, "mystery", "mystery", 424242), self.c, [], self.sinks)
        out = teshis.format_block(d, color=True)
        self.assertIn("\x1b[", out)
        self.assertIn("bulunamadı", out)
        self.assertIn("bilinmeyen", out)

    def test_format_line(self):
        d = teshis.collect(stream(2, "Firefox", "firefox", FIREFOX_PID, "Haber - YouTube"),
                           self.c, [self.ff_player], self.sinks)
        line = teshis.format_line(d)
        self.assertTrue(line.startswith("↻ #2 Firefox: film → film"))

    def test_main_once_with_fakes(self):
        fake_pulse = mock.MagicMock()
        fake_pulse.list_streams.return_value = [stream(1, "cs2", "cs2", 8000)]
        fake_pulse.list_sinks.return_value = [Sink(7, "hs80", "HS80", "RUNNING")]
        watcher = mock.MagicMock()
        watcher.return_value.players.return_value = []
        watcher.return_value.mode = "sinyal"
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            jpath = Path(td) / "t.json"
            with mock.patch.dict("sys.modules", {"oto_eq.pulse": fake_pulse}), \
                    mock.patch("oto_eq.teshis.sys.stdout", buf), \
                    mock.patch("oto_eq.mpris.MprisWatcher", watcher), \
                    mock.patch("oto_eq.classifier.load_classifier", return_value=self.c), \
                    mock.patch("oto_eq.pulse", fake_pulse, create=True):
                rc = teshis.main(["--json", str(jpath), "--renk", "hayir"])
            data = json.loads(jpath.read_text())
        self.assertEqual(rc, 0)
        self.assertIn("cs2 → cs2", buf.getvalue())
        self.assertEqual(data["akislar"][0]["decision"]["category"], "cs2")
        fake_pulse.move_stream.assert_not_called()               # read-only



class TestTeshisCeviri(TestTeshis):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_and_media_flag(self):
        from oto_eq import i18n
        d = teshis.collect(stream(2, "Firefox", "firefox", FIREFOX_PID, "Haber - YouTube"),
                           self.c, [self.ff_player], self.sinks)
        self.assertTrue(teshis._is_browser_decision(d))          # a flag, not the reason text
        self.assertFalse(teshis._is_browser_decision(
            teshis.collect(stream(1, "cs2", "cs2", 8000), self.c, [], self.sinks)))
        i18n.sifirla("de", {"KARAR": "ENTSCHEIDUNG", "tarayıcı: YouTube video": "Browser: YouTube-Video"})
        out = teshis.format_block(teshis.collect(
            stream(2, "Firefox", "firefox", FIREFOX_PID, "Haber - YouTube"), self.c, [], self.sinks))
        self.assertIn("ENTSCHEIDUNG", out)
        self.assertIn("Browser: YouTube-Video", out)


if __name__ == "__main__":
    unittest.main()
