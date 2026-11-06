"""MPRIS player watcher (session bus).

Self-contained: a plain daemon thread with its OWN jeepney blocking
connection (no GLib main context, no dbus-python global main loop), so it
cannot interfere with Qt/GLib users in the same process. It tracks
``org.mpris.MediaPlayer2.*`` names via NameOwnerChanged and
``PropertiesChanged`` (PlaybackStatus, Metadata) on /org/mpris/MediaPlayer2.
If subscribing to signals fails it polls every 2 s; a full resync also runs
every 30 s as a safety net. If the bus drops, it reconnects with backoff.

``players()`` returns a snapshot ordered: Playing players first, the one that
most recently switched to Playing first; then the rest by bus name. The
classifier relies on this order to pick among several playing browser tabs.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from .models import MediaPlayer

log = logging.getLogger("oto_eq.mpris")

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
IFACE_ROOT = "org.mpris.MediaPlayer2"
IFACE_PLAYER = "org.mpris.MediaPlayer2.Player"
IFACE_PROPS = "org.freedesktop.DBus.Properties"
CALL_TIMEOUT = 1.0
POLL_INTERVAL = 2.0
RESYNC_INTERVAL = 30.0
RECV_SLICE = 0.25          # max latency of stop()


def _unv(v):
    """Unwrap jeepney variants ('s', value) recursively enough for MPRIS."""
    while isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str):
        v = v[1]
    return v


def _s(v) -> str:
    v = _unv(v)
    try:
        return str(v) if v is not None else ""
    except Exception:
        return ""


def _artist(v) -> str:
    v = _unv(v)
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(_s(x) for x in v)
    return _s(v)


class _Entry:
    __slots__ = ("bus_name", "owner", "pid", "identity", "status", "title",
                 "artist", "url", "play_since")

    def __init__(self, bus_name: str, owner: str):
        self.bus_name = bus_name
        self.owner = owner
        self.pid: int | None = None
        self.identity = ""
        self.status = "Stopped"
        self.title = ""
        self.artist = ""
        self.url = ""
        self.play_since = 0.0

    def apply_metadata(self, md) -> None:
        md = _unv(md) or {}
        if not isinstance(md, dict):
            md = {}
        self.title = _s(md.get("xesam:title", ""))
        self.artist = _artist(md.get("xesam:artist", ""))
        self.url = _s(md.get("xesam:url", ""))

    def apply_status(self, status) -> None:
        status = _s(status) or "Stopped"
        if status == "Playing" and self.status != "Playing":
            self.play_since = time.monotonic()
        self.status = status

    def snapshot(self) -> MediaPlayer:
        return MediaPlayer(bus_name=self.bus_name, pid=self.pid,
                           identity=self.identity, status=self.status,
                           title=self.title, artist=self.artist, url=self.url)


class MprisWatcher:
    """Keeps a thread-safe view of MPRIS players; calls ``on_change()`` on changes."""

    def __init__(self, on_change: Callable[[], None] | None = None,
                 poll_interval: float = POLL_INTERVAL, bus_address: str | None = None):
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._bus_address = bus_address or "SESSION"
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._snapshot: list[MediaPlayer] = []
        self._thread: threading.Thread | None = None
        self._conn = None
        self._stop = threading.Event()
        self._started = threading.Event()
        self.mode = "kapali"          # "sinyal" | "yoklama" | "kapali"

    # ------------------------------------------------------------ public
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._started.clear()
        self._thread = threading.Thread(target=self._run, name="oto-eq-mpris", daemon=True)
        self._thread.start()
        self._started.wait(5.0)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(3.0)
            if t.is_alive():
                log.warning("MPRIS izleyici iş parçacığı zamanında durmadı")
        self._thread = None

    def players(self) -> list[MediaPlayer]:
        with self._lock:
            return list(self._snapshot)

    # ------------------------------------------------------------ thread
    def _run(self) -> None:
        backoff = 1.0
        first = True
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._session(first)
                backoff = 1.0
            except Exception as e:
                if time.monotonic() - started >= 10.0:      # a healthy session: start over
                    backoff = 1.0
                if first:
                    log.error("MPRIS izleyici veri yoluna bağlanamadı: %s", e)
                else:
                    log.warning("MPRIS bağlantısı koptu: %s", e)
            finally:
                first = False
                self._started.set()
                self.mode = "kapali"
                self._close()
            if self._entries and not self._stop.is_set():
                self._entries.clear()      # bus lost: players unknown until reconnect
                self._publish()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 30.0)

    def _close(self) -> None:
        c, self._conn = self._conn, None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _session(self, first: bool) -> None:
        from jeepney import MatchRule, message_bus
        from jeepney.io.blocking import open_dbus_connection

        conn = open_dbus_connection(bus=self._bus_address)
        self._conn = conn
        noc_rule = MatchRule(type="signal", sender="org.freedesktop.DBus",
                             interface="org.freedesktop.DBus", member="NameOwnerChanged",
                             path="/org/freedesktop/DBus")
        noc_rule.add_arg_condition(0, "org.mpris.MediaPlayer2", kind="namespace")
        pc_rule = MatchRule(type="signal", interface=IFACE_PROPS,
                            member="PropertiesChanged", path=MPRIS_PATH)
        signals_ok = True
        try:
            for rule in (noc_rule, pc_rule):
                self._call(message_bus.AddMatch(rule))
        except Exception as e:
            signals_ok = False
            log.warning("MPRIS sinyallerine abone olunamadı, yoklamaya geçiliyor: %s", e)
        self.mode = "sinyal" if signals_ok else "yoklama"
        interval = RESYNC_INTERVAL if signals_ok else self._poll_interval
        with conn.filter(noc_rule, bufsize=4096) as noc_q, \
                conn.filter(pc_rule, bufsize=4096) as pc_q:
            self._resync()
            self._started.set()
            next_sync = time.monotonic() + interval
            while not self._stop.is_set():
                try:
                    conn.recv_messages(timeout=RECV_SLICE)
                except TimeoutError:
                    pass
                self._drain(noc_q, pc_q)
                if time.monotonic() >= next_sync:
                    self._resync()
                    next_sync = time.monotonic() + interval

    # ---- bus helpers (watcher thread only)
    def _call(self, msg):
        from jeepney import MessageType
        reply = self._conn.send_and_get_reply(msg, timeout=CALL_TIMEOUT)
        if reply.header.message_type == MessageType.error:
            raise RuntimeError(f"D-Bus hatası: {reply.body!r}"[:200])
        return reply.body

    def _owner_pid(self, owner: str) -> int | None:
        from jeepney import message_bus
        try:
            return int(self._call(message_bus.GetConnectionUnixProcessID(owner))[0])
        except Exception:
            return None

    def _fetch(self, bus_name: str, owner: str) -> _Entry:
        from jeepney import DBusAddress, Properties
        old = self._entries.get(bus_name)
        e = _Entry(bus_name, owner)
        if old is not None and old.owner == owner:
            e.pid, e.play_since, e.status = old.pid, old.play_since, old.status
        else:
            e.pid = self._owner_pid(owner)
        try:
            root = DBusAddress(MPRIS_PATH, bus_name=owner, interface=IFACE_ROOT)
            try:
                e.identity = _s(self._call(Properties(root).get("Identity"))[0])
            except Exception:
                e.identity = old.identity if old else ""
            player = DBusAddress(MPRIS_PATH, bus_name=owner, interface=IFACE_PLAYER)
            allp = self._call(Properties(player).get_all())[0]
            e.apply_status(allp.get("PlaybackStatus", "Stopped"))
            e.apply_metadata(allp.get("Metadata", {}))
        except Exception as ex:
            log.debug("MPRIS oyuncusu okunamadı %s: %s", bus_name, ex)
            if old is not None and old.owner == owner:
                return old
        return e

    def _resync(self) -> None:
        from jeepney import message_bus
        try:
            names = self._call(message_bus.ListNames())[0]
        except Exception as ex:
            log.debug("ListNames başarısız: %s", ex)
            raise
        present = {}
        for n in names:
            if not n.startswith(MPRIS_PREFIX):
                continue
            try:
                present[n] = str(self._call(message_bus.GetNameOwner(n))[0])
            except Exception:
                continue
        for n in list(self._entries):
            if n not in present:
                del self._entries[n]
        for n, owner in present.items():
            self._entries[n] = self._fetch(n, owner)
        self._publish()

    def _drain(self, noc_q: deque, pc_q: deque) -> None:
        from jeepney import HeaderFields
        changed = False
        while noc_q:
            msg = noc_q.popleft()
            try:
                name, _old, new = msg.body
                if not name.startswith(MPRIS_PREFIX):
                    continue
                if new:
                    self._entries[name] = self._fetch(name, new)
                else:
                    self._entries.pop(name, None)
                changed = True
            except Exception:
                log.exception("NameOwnerChanged işlenemedi")
        while pc_q:
            msg = pc_q.popleft()
            try:
                iface, props, invalidated = msg.body
                if iface != IFACE_PLAYER:
                    continue
                sender = msg.header.fields.get(HeaderFields.sender, "")
                targets = [e for e in self._entries.values() if e.owner == sender]
                if not targets:
                    self._resync()
                    continue
                for e in targets:
                    if "PlaybackStatus" in props:
                        e.apply_status(props["PlaybackStatus"])
                    if "Metadata" in props:
                        e.apply_metadata(props["Metadata"])
                    if {"PlaybackStatus", "Metadata"} & set(invalidated or ()):
                        self._entries[e.bus_name] = self._fetch(e.bus_name, e.owner)
                changed = True
            except Exception:
                log.exception("PropertiesChanged işlenemedi")
        if changed:
            self._publish()

    def _publish(self) -> None:
        """Rebuild the snapshot; notify if it changed. Watcher thread only."""
        ents = list(self._entries.values())
        playing = sorted((e for e in ents if e.status == "Playing"), key=lambda e: -e.play_since)
        rest = sorted((e for e in ents if e.status != "Playing"), key=lambda e: e.bus_name)
        snap = [e.snapshot() for e in playing + rest]
        with self._lock:
            changed = snap != self._snapshot
            self._snapshot = snap
        if changed and self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("MPRIS on_change geri çağrısı hata verdi")


def main() -> None:   # pragma: no cover - manual debugging aid
    """`python3 -m oto_eq.mpris`: print players on every change."""
    import sys
    logging.basicConfig(level=logging.INFO)
    w: MprisWatcher

    def show():
        for p in w.players():
            sys.stdout.write(f"{p.status:8} pid={p.pid} {p.bus_name} | {p.identity} | "
                             f"{p.artist} — {p.title} | {p.url}\n")
        sys.stdout.write("--\n")
        sys.stdout.flush()

    w = MprisWatcher(show)
    w.start()
    show()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        w.stop()


if __name__ == "__main__":   # pragma: no cover
    main()
