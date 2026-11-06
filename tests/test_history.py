"""Tests for oto_eq.history (temp files only)."""
from __future__ import annotations

import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from oto_eq.history import History
from oto_eq.models import Category, Decision, Stream

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)


def stream(index=1, app="Spotify", binary="spotify", props=None, node=""):
    return Stream(index=index, sink_index=0, app_name=app, binary=binary, pid=None,
                  media_name="", media_role="", node_name=node or app, corked=False,
                  props=props or {})


MUSIC = Decision(Category.MUSIC, "muzik", "müzik uygulaması 'spotify'")
MOVIE = Decision(Category.MOVIE, "film", "kullanıcı ataması")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class TestHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state" / "gorulen.json"
        self.clock = Clock()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        kw.setdefault("min_interval", 0.3)
        h = History(self.path, clock=self.clock, **kw)
        self.addCleanup(h.flush)
        return h

    def test_dedupe_count_and_latest_decision(self):
        h = self.make()
        h.record(stream(1, app="Spotify"), MUSIC)
        h.record(stream(1, app="spotify"), MUSIC)      # same stream, case-insensitive key
        h.record(stream(2), MOVIE)                     # new stream of the same app
        items = h.recent()
        self.assertEqual(len(items), 1)
        e = items[0]
        self.assertEqual(e["sayi"], 2)
        self.assertEqual(e["kategori"], "film")
        self.assertEqual(e["profil"], "film")
        self.assertEqual(e["neden"], "kullanıcı ataması")

    def test_flatpak_part_of_key_and_newest_first(self):
        h = self.make()
        h.record(stream(1, app="vlc", binary="vlc"), MUSIC)
        self.clock.t += 10
        h.record(stream(2, app="vlc", binary="vlc",
                        props={"pipewire.access.portal.app_id": "org.videolan.VLC"}), MOVIE)
        items = h.recent()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["flatpak"], "org.videolan.VLC")
        self.assertEqual(h.recent(limit=1), items[:1])

    def test_ignores_own_and_anonymous_streams(self):
        h = self.make()
        h.record(stream(1, app="Oto-EQ", binary="pipewire", node="oto_eq_muzik_out"), MUSIC)
        h.record(stream(2, app="", binary=""), MUSIC)
        self.assertEqual(h.recent(), [])

    def test_cap(self):
        h = self.make(cap=5)
        for i in range(12):
            self.clock.t += 1
            h.record(stream(i, app=f"app{i}", binary=f"bin{i}"), MUSIC)
        items = h.recent(100)
        self.assertEqual(len(items), 5)
        self.assertEqual([e["uygulama"] for e in items], [f"app{i}" for i in range(11, 6, -1)])
        h.flush()
        self.assertEqual(len(json.loads(self.path.read_text())["uygulamalar"]), 5)

    def test_throttled_writes(self):
        h = self.make(min_interval=0.5)
        h.record(stream(1, app="a", binary="a"), MUSIC)   # first write immediately
        self.assertEqual(h.writes, 1)
        for i in range(20):
            h.record(stream(10 + i, app=f"b{i}", binary="b"), MUSIC)
        self.assertEqual(h.writes, 1)                     # pending, not written yet
        deadline = time.monotonic() + 3
        while h.writes < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(h.writes, 2)                     # one timer write for all 20
        data = json.loads(self.path.read_text())
        self.assertEqual(len(data["uygulamalar"]), 21)

    def test_no_write_when_nothing_changed(self):
        h = self.make()
        h.record(stream(1), MUSIC)
        h.flush()
        w = h.writes
        for _ in range(5):
            h.record(stream(1), MUSIC)     # same stream, same decision, seconds later
        h.flush()
        self.assertEqual(h.writes, w)

    def test_persist_and_reload(self):
        h = self.make()
        h.record(stream(1), MUSIC)
        h.flush()
        h2 = self.make()
        items = h2.recent()
        self.assertEqual(items[0]["ikili"], "spotify")
        h2.record(stream(9), MUSIC)
        self.assertEqual(h2.recent()[0]["sayi"], 2)

    def test_corrupt_file_recovery(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{bozuk json", encoding="utf-8")
        h = self.make()
        self.assertEqual(h.recent(), [])
        self.assertTrue(self.path.with_name("gorulen.json.bozuk").exists())
        h.record(stream(1), MUSIC)
        h.flush()
        self.assertEqual(json.loads(self.path.read_text())["uygulamalar"][0]["ikili"], "spotify")

    def test_bad_entries_skipped(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"uygulamalar": [
            "x", {"ikili": "ok", "son": 5}, {"ikili": "bad", "son": "yok"}]}), encoding="utf-8")
        self.assertEqual([e["ikili"] for e in self.make().recent()], ["ok"])

    def test_record_never_raises(self):
        h = self.make()
        h.record(None, MUSIC)              # type: ignore[arg-type]
        h.record(stream(1), None)          # type: ignore[arg-type]
        self.assertEqual(h.recent(), [])

    def test_forget(self):
        h = self.make()
        h.record(stream(1), MUSIC)
        self.assertTrue(h.forget("spotify", "Spotify"))
        self.assertEqual(h.recent(), [])


if __name__ == "__main__":
    unittest.main()
