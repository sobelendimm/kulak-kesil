"""MprisWatcher against a FAKE MPRIS service we register ourselves on the session bus.

The fake service (a child process) owns org.mpris.MediaPlayer2.otoeqtest_<n>;
nothing else on the bus is touched (other players are only listed, read-only).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
import unittest

from oto_eq.config import C_ENV

HAVE_DBUS = bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS")) or os.path.exists(
    f"/run/user/{os.getuid()}/bus")
# the watcher talks D-Bus through jeepney; the fake player needs dbus-python + GLib
HAVE_LIBS = all(importlib.util.find_spec(m) is not None for m in ("jeepney", "dbus", "gi"))

from oto_eq.mpris import MprisWatcher

SERVICE = r'''
import sys, dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib
dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()
NAME = sys.argv[1]
PLAYER = "org.mpris.MediaPlayer2.Player"

class P(dbus.service.Object):
    def __init__(self):
        super().__init__(bus, "/org/mpris/MediaPlayer2")
        self.status = "Paused"
        self.md = {"mpris:trackid": dbus.ObjectPath("/otoeq/1"),
                   "xesam:title": "Test Şarkı",
                   "xesam:artist": dbus.Array(["Sanatçı"], signature="s"),
                   "xesam:url": "file:///tmp/otoeq/a.flac"}

    def _player_props(self):
        return {"PlaybackStatus": self.status,
                "Metadata": dbus.Dictionary(self.md, signature="sv")}

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]

    @dbus.service.method("org.freedesktop.DBus.Properties", in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        if iface == "org.mpris.MediaPlayer2":
            return {"Identity": "OtoEq Test"}
        if iface == PLAYER:
            return self._player_props()
        return {}

    @dbus.service.signal("org.freedesktop.DBus.Properties", signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    @dbus.service.method("org.otoeq.Test", in_signature="s")
    def SetStatus(self, s):
        self.status = s
        self.PropertiesChanged(PLAYER, {"PlaybackStatus": s}, dbus.Array([], signature="s"))

    @dbus.service.method("org.otoeq.Test", in_signature="ss")
    def SetTrack(self, title, url):
        self.md["xesam:title"] = title
        self.md["xesam:url"] = url
        self.PropertiesChanged(PLAYER, {"Metadata": dbus.Dictionary(self.md, signature="sv")},
                               dbus.Array([], signature="s"))

p = P()
name = dbus.service.BusName(NAME, bus)
print("hazir", flush=True)
GLib.MainLoop().run()
'''


def wait_for(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    return None


@unittest.skipUnless(HAVE_DBUS, "oturum D-Bus'ı yok")
@unittest.skipUnless(HAVE_LIBS, "jeepney / dbus-python / gi kurulu değil")
class TestMprisWatcher(unittest.TestCase):
    def setUp(self):
        self.procs: list[subprocess.Popen] = []
        self.changes = 0
        self.lock = threading.Lock()
        self.w = MprisWatcher(self._changed)
        self.w.start()
        from jeepney.io.blocking import open_dbus_connection
        self.ctl = open_dbus_connection(bus="SESSION")   # own connection, no main loop

    def tearDown(self):
        self.w.stop()
        for p in self.procs:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
            p.stdout.close()                  # else: ResourceWarning (unclosed pipe)
        try:
            self.ctl.close()
        except Exception:
            pass

    def _changed(self):
        with self.lock:
            self.changes += 1

    def spawn(self, suffix: str) -> tuple[str, subprocess.Popen]:
        name = f"org.mpris.MediaPlayer2.otoeqtest_{os.getpid()}_{suffix}"
        p = subprocess.Popen([sys.executable, "-c", SERVICE, name], env=C_ENV,
                             stdout=subprocess.PIPE, text=True)
        self.procs.append(p)
        line = p.stdout.readline()
        self.assertEqual(line.strip(), "hazir")
        return name, p

    def find(self, name):
        for pl in self.w.players():
            if pl.bus_name == name:
                return pl
        return None

    def call(self, name, method, *args):
        from jeepney import DBusAddress, MessageType, new_method_call
        addr = DBusAddress("/org/mpris/MediaPlayer2", bus_name=name, interface="org.otoeq.Test")
        sig = "s" * len(args)
        reply = self.ctl.send_and_get_reply(new_method_call(addr, method, sig, args), timeout=3)
        self.assertNotEqual(reply.header.message_type, MessageType.error, reply.body)

    def test_appear_change_disappear(self):
        self.assertEqual(self.w.mode, "sinyal")
        name, proc = self.spawn("a")
        pl = wait_for(lambda: self.find(name))
        self.assertIsNotNone(pl, "oyuncu görünmedi")
        self.assertEqual(pl.pid, proc.pid)
        self.assertEqual(pl.identity, "OtoEq Test")
        self.assertEqual(pl.status, "Paused")
        self.assertEqual(pl.title, "Test Şarkı")
        self.assertEqual(pl.artist, "Sanatçı")
        self.assertEqual(pl.url, "file:///tmp/otoeq/a.flac")

        before = self.changes
        self.call(name, "SetStatus", "Playing")
        self.assertTrue(wait_for(lambda: (p := self.find(name)) and p.status == "Playing"))
        self.assertGreater(self.changes, before)

        self.call(name, "SetTrack", "İkinci", "https://music.youtube.com/watch?v=1")
        self.assertTrue(wait_for(lambda: (p := self.find(name)) and p.title == "İkinci"))
        self.assertEqual(self.find(name).url, "https://music.youtube.com/watch?v=1")

        proc.terminate()
        proc.wait(5)
        self.assertTrue(wait_for(lambda: self.find(name) is None), "oyuncu silinmedi")

    def test_order_most_recent_playing_first(self):
        a, _ = self.spawn("o1")
        b, _ = self.spawn("o2")
        self.assertTrue(wait_for(lambda: self.find(a) and self.find(b)))
        self.call(a, "SetStatus", "Playing")
        self.assertTrue(wait_for(lambda: self.find(a).status == "Playing"))
        time.sleep(0.05)
        self.call(b, "SetStatus", "Playing")
        self.assertTrue(wait_for(lambda: self.find(b).status == "Playing"))
        mine = [p.bus_name for p in self.w.players() if p.bus_name in (a, b)]
        self.assertEqual(mine, [b, a])
        self.call(b, "SetStatus", "Paused")
        self.assertTrue(wait_for(lambda: self.find(b).status == "Paused"))
        names = [p.bus_name for p in self.w.players()]
        self.assertLess(names.index(a), names.index(b))   # Playing before Paused

    def test_snapshot_is_copy_and_stop_idempotent(self):
        snap = self.w.players()
        snap.append(None)
        self.assertNotIn(None, self.w.players())
        self.w.stop()
        self.w.stop()


class TestLifecycle(unittest.TestCase):
    def mpris_threads(self):
        return [t for t in threading.enumerate() if t.name == "oto-eq-mpris" and t.is_alive()]

    @unittest.skipUnless(HAVE_DBUS, "oturum D-Bus'ı yok")
    def test_start_stop_no_leaks(self):
        before = len(self.mpris_threads())
        for _ in range(3):
            w = MprisWatcher(lambda: None)
            w.start()
            self.assertEqual(w.mode, "sinyal")
            t0 = time.monotonic()
            w.stop()
            self.assertLess(time.monotonic() - t0, 2.0)
            w.stop()
        self.assertEqual(len(self.mpris_threads()), before)


class TestWatcherWithoutBus(unittest.TestCase):
    def test_missing_jeepney_is_reported_once_and_stops(self):
        from unittest import mock
        w = MprisWatcher(lambda: None)
        with mock.patch.dict(sys.modules, {"jeepney": None}), \
                self.assertLogs("oto_eq.mpris", "WARNING") as cm:
            w.start()
            w._thread.join(2)
        self.assertFalse(w._thread.is_alive())          # no reconnect loop
        self.assertEqual(w.mode, "yok")
        self.assertIn("jeepney", w.hata or "")
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(w.players(), [])
        w.stop()

    def test_no_bus_does_not_raise(self):
        w = MprisWatcher(lambda: None, bus_address="unix:path=/nonexistent/otoeq-bus")
        w.start()
        self.assertEqual(w.players(), [])
        self.assertEqual(w.mode, "kapali")
        w.stop()


if __name__ == "__main__":
    unittest.main()
