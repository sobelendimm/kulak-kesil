"""Unix-socket IPC between the router daemon and the CLI / tray.

Protocol: JSON lines (one UTF-8 JSON object per line), see docs/CONTRACT.md.

Server side: :class:`IpcServer` answers ``status`` itself (via a snapshot
callable), validates ``set_mode`` / ``reload`` / ``rescan`` and hands them to
a command callable, and keeps ``watch`` connections open, pushing every
status the owner passes to :meth:`IpcServer.publish`.  It knows nothing about
router internals.

Client side: :func:`request` (one request, one response) and :func:`watch`
(stream of status events).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import select
import socket
import socketserver
import stat
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from . import config
from .i18n import _
from .models import EQ_PROFILES, GROUP_LABELS, VARIANT_PROFILES, variant_groups

log = logging.getLogger("oto_eq.ipc")

MODES: tuple[str, ...] = ("auto", "fixed", "off")
COMMANDS: tuple[str, ...] = ("status", "set_mode", "set_variant", "reload", "rescan", "watch",
                             "dil")   # dil: the UI language changed → router reloads its catalog

MAX_LINE = 64 * 1024          # bytes per request line
IDLE_TIMEOUT = 60.0           # non-watch connection: max silence between requests
WATCH_SEND_TIMEOUT = 5.0      # a watcher that does not read for this long is dropped
WATCH_QUEUE = 64              # pending events per watcher before it is dropped
WATCH_POLL = 1.0              # how often a watch thread checks for hang-up/shutdown
SERVE_POLL = 0.5              # serve_forever poll interval (= worst-case stop latency)


# --------------------------------------------------------------------------
# validation (shared with the router)

def validate_mode(mode: Any, profile: Any = None) -> tuple[str, str | None]:
    """Normalise a (mode, profile) pair; raise ValueError (Turkish) if invalid."""
    if not isinstance(mode, str) or mode not in MODES:
        raise ValueError(_("geçersiz mod: {mod} (auto, fixed veya off olmalı)").format(mod=repr(mode)))
    if mode != "fixed":
        return mode, None
    if profile is None or profile == "":
        raise ValueError(_("sabit mod için 'profile' gerekli ({olanlar})").format(
            olanlar=", ".join(EQ_PROFILES)))
    if not isinstance(profile, str) or profile not in EQ_PROFILES:
        raise ValueError(_("geçersiz profil: {profil} ({olanlar} olmalı)").format(
            profil=repr(profile), olanlar=", ".join(EQ_PROFILES)))
    return mode, profile


def validate_variant(profile: Any, group: Any) -> tuple[str, str]:
    """``set_variant`` arguments -> (mode, group); raise ValueError (Turkish).

    group "auto" -> ("auto", ""); "" -> ("fixed", "") = sabit Genel;
    a group of that profile -> ("fixed", group).
    """
    if not isinstance(profile, str) or profile not in VARIANT_PROFILES:
        raise ValueError(_("tür alt profili olmayan profil: {profil} ({olanlar} olmalı)").format(
            profil=repr(profile), olanlar=", ".join(VARIANT_PROFILES)))
    if group == "auto":
        return "auto", ""
    if group is None or group == "":
        return "fixed", ""
    groups = variant_groups(profile)
    if not isinstance(group, str) or group not in groups:
        raise ValueError(_("{profil} için geçersiz tür: {tur} (auto, \"\" ya da {olanlar})").format(
            profil=profile, tur=repr(group), olanlar=", ".join(groups)))
    return "fixed", group


def validate_request(req: Any) -> str | None:
    """Return a Turkish error message for a malformed request, else None."""
    if not isinstance(req, dict):
        return _("istek bir JSON nesnesi olmalı")
    cmd = req.get("cmd")
    if cmd is None:
        return _("'cmd' alanı eksik")
    if not isinstance(cmd, str) or cmd not in COMMANDS:
        return _("bilinmeyen komut: {komut} ({olanlar})").format(
            komut=repr(cmd), olanlar=", ".join(COMMANDS))
    if cmd == "set_mode":
        try:
            validate_mode(req.get("mode"), req.get("profile"))
        except ValueError as e:
            return str(e)
    if cmd == "set_variant":
        if "group" not in req:
            return _("'group' alanı eksik (auto, \"\" ya da bir tür)")
        try:
            validate_variant(req.get("profile"), req.get("group"))
        except ValueError as e:
            return str(e)
    return None


def _encode(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _error(msg: str) -> dict:
    return {"ok": False, "error": msg}


# --------------------------------------------------------------------------
# server

class _Subscriber:
    def __init__(self) -> None:
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=WATCH_QUEUE)
        self.dead = False

    def offer(self, line: bytes | None) -> bool:
        if self.dead:
            return False
        try:
            self.q.put_nowait(line)
            return True
        except queue.Full:
            self.dead = True
            return False


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    ipc: "IpcServer"


class _Handler(socketserver.StreamRequestHandler):
    timeout = IDLE_TIMEOUT

    def handle(self) -> None:  # one thread per connection
        ipc = self.server.ipc  # type: ignore[attr-defined]
        while not ipc._closing.is_set():
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except (OSError, ValueError):
                return
            if not raw:
                return
            if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
                self._send(_error(_("istek çok uzun")))
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                if not self._send(_error(_("geçersiz JSON"))):
                    return
                continue
            err = validate_request(req)
            if err is not None:
                if not self._send(_error(err)):
                    return
                continue
            if req["cmd"] == "watch":
                ipc._serve_watch(self.request)
                return
            if not self._send(ipc._dispatch(req)):
                return

    def _send(self, obj: dict) -> bool:
        try:
            self.wfile.write(_encode(obj))
            return True
        except OSError:
            return False


class IpcServer:
    """Threaded JSON-lines server on a Unix socket.

    ``status_fn() -> dict``: current status fields (without ``ok``).
    ``command_fn(req) -> dict | None``: handles validated ``set_mode``,
    ``reload`` and ``rescan`` requests; may raise ``ValueError`` (message is
    sent to the client).  ``None`` means ``{"ok": true}``.
    """

    def __init__(self, status_fn: Callable[[], dict],
                 command_fn: Callable[[dict], dict | None],
                 path: Path | str | None = None):
        self.status_fn = status_fn
        self.command_fn = command_fn
        self._path_arg = path
        self.path: Path | None = None
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._subs: set[_Subscriber] = set()
        self._subs_lock = threading.Lock()
        self._closing = threading.Event()
        self._ino: int | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        path = Path(self._path_arg) if self._path_arg is not None else Path(config.SOCKET_PATH)
        _prepare_socket_path(path)
        self._closing.clear()
        server = _Server(str(path), _Handler)
        server.ipc = self
        try:
            os.chmod(path, 0o600)
            self._ino = os.stat(path).st_ino
        except OSError:
            server.server_close()
            raise
        self.path = path
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": SERVE_POLL},
                                        name="oto-eq-ipc", daemon=True)
        self._thread.start()
        log.debug("IPC soketi dinleniyor: %s", path)

    def stop(self) -> None:
        self._closing.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # pragma: no cover - defensive
                pass
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            sub.offer(None)
        if self.path is not None and self._ino is not None:
            try:
                if os.stat(self.path).st_ino == self._ino:
                    os.unlink(self.path)
            except OSError:
                pass
        self._ino = None

    # -- watch ------------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        with self._subs_lock:
            return sum(1 for s in self._subs if not s.dead)

    def publish(self, status: dict) -> None:
        """Push a status to every watcher (never blocks)."""
        line = _encode({"event": "status", "ok": True, **status})
        with self._subs_lock:
            subs = list(self._subs)
        for sub in subs:
            if not sub.offer(line):
                self._drop(sub)

    def _drop(self, sub: _Subscriber) -> None:
        sub.dead = True
        with self._subs_lock:
            self._subs.discard(sub)

    def _serve_watch(self, sock: socket.socket) -> None:
        sub = _Subscriber()
        with self._subs_lock:
            self._subs.add(sub)
        log.debug("izleyici bağlandı (%d)", self.subscriber_count)
        try:
            sock.settimeout(WATCH_SEND_TIMEOUT)
            try:
                first = {"event": "status", "ok": True, **self.status_fn()}
            except Exception as e:
                log.warning("durum alınamadı: %s", e)
                first = {"event": "status", "ok": False, "error": _("durum alınamadı: {hata}").format(hata=e)}
            sock.sendall(_encode(first))
            while not self._closing.is_set() and not sub.dead:
                try:
                    line = sub.q.get(timeout=WATCH_POLL)
                except queue.Empty:
                    if _peer_closed(sock):
                        return
                    continue
                if line is None:
                    return
                sock.sendall(line)
        except OSError:
            return
        finally:
            self._drop(sub)
            log.debug("izleyici ayrıldı (%d)", self.subscriber_count)

    # -- requests ---------------------------------------------------------

    def _dispatch(self, req: dict) -> dict:
        cmd = req["cmd"]
        if cmd == "status":
            try:
                return {"ok": True, **self.status_fn()}
            except Exception as e:
                log.exception("durum alınamadı")
                return _error(_("durum alınamadı: {hata}").format(hata=e))
        try:
            resp = self.command_fn(req)
        except ValueError as e:
            return _error(str(e))
        except Exception as e:
            log.exception("komut işlenemedi: %s", cmd)
            return _error(_("iç hata: {hata}").format(hata=e))
        if resp is None:
            return {"ok": True}
        if not isinstance(resp, dict) or "ok" not in resp:
            return {"ok": True, **(resp if isinstance(resp, dict) else {})}
        return resp


def _peer_closed(sock: socket.socket) -> bool:
    """True if the client hung up (input from a watcher is discarded)."""
    try:
        r, _w, _x = select.select([sock], [], [], 0)
        if not r:
            return False
        return sock.recv(4096) == b""
    except (OSError, ValueError):
        return True


def socket_in_use(path: Path | str | None = None) -> bool:
    """True if a server is accepting on the socket (another daemon is running)."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(_socket_path(path)))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _prepare_socket_path(path: Path) -> None:
    """Create the parent dir; remove a stale socket; refuse if one is live."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(_("{yol} bir soket değil; elle silinmeli").format(yol=path))
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.connect(str(path))
    except OSError:
        log.debug("eski soket dosyası siliniyor: %s", path)
        os.unlink(path)
        return
    finally:
        probe.close()
    raise RuntimeError(_("başka bir oto-eq zaten çalışıyor ({yol})").format(yol=path))


# --------------------------------------------------------------------------
# client

def _socket_path(path: Path | str | None) -> Path:
    # read config.SOCKET_PATH at call time (tests monkeypatch it)
    return Path(path) if path is not None else Path(config.SOCKET_PATH)


def _decode(raw: bytes) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ConnectionError(_("oto-eq geçersiz yanıt verdi")) from None
    if not isinstance(obj, dict):
        raise ConnectionError(_("oto-eq geçersiz yanıt verdi"))
    return obj


def request(obj: dict, timeout: float = 2.0, path: Path | None = None) -> dict:
    """Send one request, return the response dict.

    Raises ``OSError`` (``FileNotFoundError``, ``ConnectionRefusedError``,
    ``TimeoutError``, ``ConnectionError`` ...) if the daemon is unreachable or
    the connection breaks. Protocol errors come back as ``{"ok": False, ...}``.
    """
    p = _socket_path(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(p))
        s.sendall(_encode(obj))
        with s.makefile("rb") as f:
            raw = f.readline(MAX_LINE * 64)
    finally:
        s.close()
    if not raw:
        raise ConnectionError(_("oto-eq bağlantıyı yanıtsız kapattı"))
    return _decode(raw)


def watch(path: Path | None = None) -> Iterator[dict]:
    """Connect and return an iterator of status events (``{"event":"status",...}``).

    Connects eagerly: raises ``OSError`` right away if the daemon is not
    reachable. Iterating raises ``ConnectionError`` when the connection drops.
    The caller handles reconnecting.
    """
    p = _socket_path(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(str(p))
        s.sendall(_encode({"cmd": "watch"}))
        s.settimeout(None)
        f = s.makefile("rb")
    except BaseException:
        s.close()
        raise
    return _watch_iter(s, f)


def _watch_iter(s: socket.socket, f) -> Iterator[dict]:
    try:
        while True:
            try:
                raw = f.readline(MAX_LINE * 64)
            except OSError as e:
                raise ConnectionError(_("oto-eq bağlantısı koptu: {hata}").format(hata=e)) from e
            if not raw:
                raise ConnectionError(_("oto-eq bağlantısı koptu"))
            msg = _decode(raw)
            if msg.get("event") == "status":
                yield msg
            elif msg.get("ok") is False:
                raise ConnectionError(str(msg.get("error") or _("izleme reddedildi")))
    finally:
        try:
            f.close()
        finally:
            s.close()
