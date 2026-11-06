"""IPC tests over a real Unix socket in a private temp dir."""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from oto_eq import config, ipc


_saved = {}


def setUpModule():
    logging.getLogger("oto_eq").setLevel(logging.CRITICAL)
    _saved.update(SERVE_POLL=ipc.SERVE_POLL, WATCH_POLL=ipc.WATCH_POLL)
    ipc.SERVE_POLL = 0.05   # faster server shutdown in tests
    ipc.WATCH_POLL = 0.1


def tearDownModule():
    logging.getLogger("oto_eq").setLevel(logging.NOTSET)
    for k, v in _saved.items():
        setattr(ipc, k, v)


def _short_tmpdir() -> Path:
    # AF_UNIX paths are limited to ~107 bytes; the runtime dir is short and private.
    base = config.RUNTIME_DIR if config.RUNTIME_DIR.is_dir() else None
    return Path(tempfile.mkdtemp(prefix="oeq-test-", dir=base))


STATUS = {"active": True, "default_sink": "hs80", "mode": "auto", "fixed_profile": None,
          "dominant": None, "sinks_ready": True, "streams": []}


class Backend:
    def __init__(self):
        self.st = dict(STATUS)
        self.calls = []
        self.fail = None

    def status(self):
        return dict(self.st)

    def command(self, req):
        self.calls.append(req)
        if self.fail == "value":
            raise ValueError("olmadı")
        if self.fail == "crash":
            raise RuntimeError("çöktü")
        if req["cmd"] == "reload" and self.fail == "reload":
            return {"ok": False, "error": "kurallar okunamadı"}
        if req["cmd"] == "set_mode":
            self.st["mode"] = req["mode"]
        return None


class IpcTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = _short_tmpdir()
        self.path = self.dir / "oto-eq.sock"
        self.backend = Backend()
        self.server = ipc.IpcServer(self.backend.status, self.backend.command, self.path)
        self.server.start()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def req(self, obj):
        return ipc.request(obj, path=self.path)

    def raw(self, data: bytes) -> list[dict]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(self.path))
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            buf = b""
            while chunk := s.recv(65536):
                buf += chunk
        return [json.loads(l) for l in buf.decode().splitlines() if l]


class TestRequests(IpcTestBase):
    def test_socket_file(self):
        mode = os.stat(self.path).st_mode
        self.assertTrue(stat.S_ISSOCK(mode))
        self.assertEqual(stat.S_IMODE(mode), 0o600)

    def test_status(self):
        resp = self.req({"cmd": "status"})
        self.assertEqual(resp, {"ok": True, **STATUS})
        self.assertEqual(self.backend.calls, [])

    def test_set_mode_valid(self):
        self.assertEqual(self.req({"cmd": "set_mode", "mode": "auto"}), {"ok": True})
        self.assertEqual(self.req({"cmd": "set_mode", "mode": "fixed", "profile": "cs2"}),
                         {"ok": True})
        self.assertEqual(self.req({"cmd": "set_mode", "mode": "off"}), {"ok": True})
        self.assertEqual(len(self.backend.calls), 3)

    def test_set_mode_invalid(self):
        cases = [
            ({"cmd": "set_mode", "mode": "sessiz"}, "geçersiz mod"),
            ({"cmd": "set_mode"}, "geçersiz mod"),
            ({"cmd": "set_mode", "mode": "fixed"}, "'profile' gerekli"),
            ({"cmd": "set_mode", "mode": "fixed", "profile": "bas"}, "geçersiz profil"),
            ({"cmd": "set_mode", "mode": "fixed", "profile": 3}, "geçersiz profil"),
            ({"cmd": "uç"}, "bilinmeyen komut"),
            ({"mode": "auto"}, "'cmd' alanı eksik"),
        ]
        for obj, msg in cases:
            with self.subTest(obj=obj):
                resp = self.req(obj)
                self.assertFalse(resp["ok"])
                self.assertIn(msg, resp["error"])
        self.assertEqual(self.backend.calls, [])

    def test_set_variant(self):
        for g in ("auto", "", "metal_rock"):
            self.assertEqual(self.req({"cmd": "set_variant", "profile": "muzik", "group": g}),
                             {"ok": True})
        self.assertEqual(self.req({"cmd": "set_variant", "profile": "oyun",
                                   "group": "rekabetci"}), {"ok": True})
        self.assertEqual(len(self.backend.calls), 4)
        cases = [
            ({"cmd": "set_variant", "profile": "cs2", "group": "auto"}, "tür alt profili olmayan"),
            ({"cmd": "set_variant", "profile": "muzik", "group": "rekabetci"}, "geçersiz tür"),
            ({"cmd": "set_variant", "profile": "oyun", "group": 5}, "geçersiz tür"),
            ({"cmd": "set_variant", "profile": "muzik"}, "'group' alanı eksik"),
        ]
        for obj, msg in cases:
            with self.subTest(obj=obj):
                resp = self.req(obj)
                self.assertFalse(resp["ok"])
                self.assertIn(msg, resp["error"])
        self.assertEqual(len(self.backend.calls), 4)
        self.assertEqual(ipc.validate_variant("muzik", "auto"), ("auto", ""))
        self.assertEqual(ipc.validate_variant("muzik", ""), ("fixed", ""))
        self.assertEqual(ipc.validate_variant("oyun", "yaris_sim"), ("fixed", "yaris_sim"))

    def test_reload_rescan(self):
        self.assertEqual(self.req({"cmd": "reload"}), {"ok": True})
        self.assertEqual(self.req({"cmd": "rescan"}), {"ok": True})
        self.backend.fail = "reload"
        self.assertEqual(self.req({"cmd": "reload"}),
                         {"ok": False, "error": "kurallar okunamadı"})

    def test_handler_errors(self):
        self.backend.fail = "value"
        self.assertEqual(self.req({"cmd": "rescan"}), {"ok": False, "error": "olmadı"})
        self.backend.fail = "crash"
        resp = self.req({"cmd": "rescan"})
        self.assertFalse(resp["ok"])
        self.assertIn("çöktü", resp["error"])
        # server still alive
        self.assertTrue(self.req({"cmd": "status"})["ok"])

    def test_bad_json_and_multiple_lines(self):
        out = self.raw(b'{bozuk\n[1,2]\n\n{"cmd":"status"}\n{"cmd":"set_mode","mode":"off"}\n')
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0], {"ok": False, "error": "geçersiz JSON"})
        self.assertEqual(out[1], {"ok": False, "error": "istek bir JSON nesnesi olmalı"})
        self.assertTrue(out[2]["ok"])
        self.assertEqual(out[3], {"ok": True})

    def test_utf8(self):
        self.backend.st["streams"] = [{"index": 1, "app": "Müzik çalar ğüşiöç", "binary": "x",
                                       "category": "muzik", "profile": "muzik",
                                       "reason": "müzik uygulaması", "sink": "oto_eq_muzik"}]
        resp = self.req({"cmd": "status"})
        self.assertEqual(resp["streams"][0]["app"], "Müzik çalar ğüşiöç")

    def test_too_long(self):
        out = self.raw(b"x" * (ipc.MAX_LINE + 10))
        self.assertEqual(out, [{"ok": False, "error": "istek çok uzun"}])

    def test_default_path_read_at_call_time(self):
        old = config.SOCKET_PATH
        config.SOCKET_PATH = self.path
        try:
            self.assertTrue(ipc.request({"cmd": "status"})["ok"])
            it = ipc.watch()
            self.assertEqual(next(it)["event"], "status")
            it.close()
        finally:
            config.SOCKET_PATH = old


class TestWatch(IpcTestBase):
    def wait_subs(self, n):
        t0 = time.monotonic()
        while self.server.subscriber_count != n and time.monotonic() - t0 < 3:
            time.sleep(0.01)
        self.assertEqual(self.server.subscriber_count, n)

    def test_initial_and_changes(self):
        it = ipc.watch(self.path)
        first = next(it)
        self.assertEqual(first, {"event": "status", "ok": True, **STATUS})
        self.wait_subs(1)
        it2 = ipc.watch(self.path)
        next(it2)
        self.wait_subs(2)
        self.server.publish({**STATUS, "dominant": "cs2"})
        self.server.publish({**STATUS, "mode": "off"})
        for w in (it, it2):
            self.assertEqual(next(w)["dominant"], "cs2")
            self.assertEqual(next(w)["mode"], "off")
        it.close()
        it2.close()
        self.wait_subs(0)

    def test_dead_subscriber_dropped(self):
        it = ipc.watch(self.path)
        next(it)
        self.wait_subs(1)
        it.close()                       # client hangs up without telling us
        self.wait_subs(0)                # noticed by the hang-up poll
        self.server.publish(STATUS)      # publishing to nobody is fine

    def test_slow_subscriber_dropped(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        s.connect(str(self.path))
        s.sendall(b'{"cmd":"watch"}\n')
        try:
            self.wait_subs(1)
            big = {**STATUS, "streams": [{"reason": "x" * 2000}] * 20}
            for _ in range(ipc.WATCH_QUEUE * 4):
                self.server.publish(big)
            self.wait_subs(0)
        finally:
            s.close()

    def test_server_stop_ends_watch(self):
        it = ipc.watch(self.path)
        next(it)
        self.wait_subs(1)
        done = threading.Event()
        err = []

        def consume():
            try:
                for _ in it:
                    pass
            except ConnectionError as e:
                err.append(e)
            done.set()
        th = threading.Thread(target=consume, daemon=True)
        th.start()
        self.server.stop()
        self.assertTrue(done.wait(3))
        self.assertEqual(len(err), 1)
        self.assertFalse(self.path.exists())


class TestNoServer(unittest.TestCase):
    def setUp(self):
        self.dir = _short_tmpdir()
        self.path = self.dir / "oto-eq.sock"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_request_missing_socket(self):
        with self.assertRaises(OSError):
            ipc.request({"cmd": "status"}, path=self.path)
        with self.assertRaises(OSError):
            ipc.watch(self.path)

    def test_request_stale_socket(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.path))
        s.close()                         # file stays, nobody listens
        with self.assertRaises(ConnectionRefusedError):
            ipc.request({"cmd": "status"}, path=self.path)
        # a server removes the stale file and starts
        server = ipc.IpcServer(dict, lambda r: None, self.path)
        server.start()
        try:
            self.assertTrue(ipc.request({"cmd": "status"}, path=self.path)["ok"])
            # a second server on a live socket refuses
            with self.assertRaises(RuntimeError):
                ipc.IpcServer(dict, lambda r: None, self.path).start()
            self.assertTrue(ipc.request({"cmd": "status"}, path=self.path)["ok"])
        finally:
            server.stop()
        self.assertFalse(self.path.exists())

    def test_socket_in_use(self):
        self.assertFalse(ipc.socket_in_use(self.path))
        server = ipc.IpcServer(dict, lambda r: None, self.path)
        server.start()
        try:
            self.assertTrue(ipc.socket_in_use(self.path))
        finally:
            server.stop()
        self.assertFalse(ipc.socket_in_use(self.path))

    def test_not_a_socket(self):
        self.path.write_text("x")
        with self.assertRaises(RuntimeError):
            ipc.IpcServer(dict, lambda r: None, self.path).start()

    def test_server_closes_without_answer(self):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.path))
        srv.listen(1)

        def accept_and_close():
            c, _ = srv.accept()
            c.recv(100)
            c.close()
        th = threading.Thread(target=accept_and_close, daemon=True)
        th.start()
        try:
            with self.assertRaises(ConnectionError):
                ipc.request({"cmd": "status"}, path=self.path)
        finally:
            th.join(2)
            srv.close()

    def test_validate_mode(self):
        self.assertEqual(ipc.validate_mode("auto", "cs2"), ("auto", None))
        self.assertEqual(ipc.validate_mode("fixed", "muzik"), ("fixed", "muzik"))
        with self.assertRaises(ValueError):
            ipc.validate_mode(None)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_error(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"'cmd' alanı eksik": "Feld 'cmd' fehlt"})
        self.assertEqual(ipc.validate_request({}), "Feld 'cmd' fehlt")


if __name__ == "__main__":
    unittest.main()
