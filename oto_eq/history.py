"""Persistent "seen apps" log (``STATE_DIR/gorulen.json``).

The router calls ``record(stream, decision)`` for every classified stream; the
GUI's Kurallar page shows ``recent()`` ("son görülen uygulamalar") for quick
assignment. Entries are deduplicated by (binary, app_name, flatpak id),
case-insensitively, capped at 200 (least recently seen dropped). Writes are
throttled to at most one per 5 s (a timer flushes pending changes) and are
atomic (tmp + rename). A corrupt file is moved aside as ``gorulen.json.bozuk``.

Entry (JSON object):
  ikili, uygulama, flatpak : str      identity (as last seen)
  ilk, son                 : float    first / last seen (Unix time)
  sayi                     : int      number of distinct streams seen
  kategori, profil, neden  : str/None last decision (profil None = EQ'suz)
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from pathlib import Path

from . import config
from .models import Decision, SINK_PREFIX, Stream

log = logging.getLogger("oto_eq.history")

HISTORY_FILE = "gorulen.json"
CAP = 200
MIN_WRITE_INTERVAL = 5.0
TOUCH_RESAVE = 60.0      # a pure last-seen update only forces a save after this long

_FLATPAK_KEYS = ("pipewire.access.portal.app_id", "application.id", "flatpak.app_id")


def _flatpak_of(stream: Stream) -> str:
    props = stream.props or {}
    for k in _FLATPAK_KEYS:
        v = props.get(k, "")
        if v and "." in v:
            return v
    return ""


class History:
    def __init__(self, path: Path | None = None, cap: int = CAP,
                 min_interval: float = MIN_WRITE_INTERVAL, clock=time.time):
        self.path = Path(path) if path else config.STATE_DIR / HISTORY_FILE
        self.cap = cap
        self.min_interval = min_interval
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str, str], dict] = {}
        self._last_index: dict[tuple[str, str, str], int] = {}
        self._loaded = False
        self._dirty = False
        self._last_write = 0.0          # monotonic
        self._timer: threading.Timer | None = None
        self.writes = 0                 # for tests/diagnostics

    # ------------------------------------------------------------ io
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("uygulamalar", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("liste değil")
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("Görülen uygulamalar dosyası bozuk, sıfırlanıyor: %s", e)
            try:
                self.path.replace(self.path.with_name(self.path.name + ".bozuk"))
            except OSError:
                pass
            return
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                e = {
                    "ikili": str(it.get("ikili", "")), "uygulama": str(it.get("uygulama", "")),
                    "flatpak": str(it.get("flatpak", "")),
                    "ilk": float(it.get("ilk", it.get("son", 0)) or 0),
                    "son": float(it.get("son", 0) or 0),
                    "sayi": int(it.get("sayi", 1) or 1),
                    "kategori": str(it.get("kategori", "") or ""),
                    "profil": it.get("profil") if isinstance(it.get("profil"), str) else None,
                    "neden": str(it.get("neden", "") or ""),
                }
            except (TypeError, ValueError):
                continue
            self._entries[self._key(e["ikili"], e["uygulama"], e["flatpak"])] = e
        self._trim()

    def _write_now(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            items = sorted(self._entries.values(), key=lambda e: -e["son"])
            payload = {"surum": 1, "uygulamalar": items}
            self._dirty = False
            self._last_write = time.monotonic()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            tmp.replace(self.path)
            self.writes += 1
        except OSError as e:
            log.warning("Görülen uygulamalar yazılamadı: %s", e)
            with self._lock:
                self._dirty = True

    def _schedule(self) -> None:
        """Write now if allowed, else arm one timer for the remaining time."""
        wait = self.min_interval - (time.monotonic() - self._last_write)
        if wait <= 0 and self._timer is None:
            self._write_now()
            return
        if self._timer is None:
            t = threading.Timer(max(wait, 0.0), self._timer_fire)
            t.daemon = True
            self._timer = t
            t.start()

    def _timer_fire(self) -> None:
        with self._lock:
            self._timer = None
        self._write_now()

    def flush(self) -> None:
        """Write pending changes immediately (exit / tests)."""
        with self._lock:
            t, self._timer = self._timer, None
        if t is not None:
            t.cancel()
        self._write_now()

    # ------------------------------------------------------------ api
    @staticmethod
    def _key(binary: str, app: str, flatpak: str) -> tuple[str, str, str]:
        return (binary.strip().casefold(), app.strip().casefold(), flatpak.strip().casefold())

    def _trim(self) -> None:
        if len(self._entries) <= self.cap:
            return
        keep = sorted(self._entries.items(), key=lambda kv: -kv[1]["son"])[: self.cap]
        self._entries = dict(keep)
        self._last_index = {k: v for k, v in self._last_index.items() if k in self._entries}

    def record(self, stream: Stream, decision: Decision, flatpak: str | None = None) -> None:
        """Remember that this app was seen with this decision. Never raises."""
        try:
            self._record(stream, decision, flatpak)
        except Exception:
            log.exception("Görülen uygulama kaydedilemedi")

    def _record(self, stream: Stream, decision: Decision, flatpak: str | None) -> None:
        if (stream.node_name or "").startswith(SINK_PREFIX):
            return
        binary, app = stream.binary or "", stream.app_name or ""
        fp = flatpak if flatpak is not None else _flatpak_of(stream)
        if not (binary or app or fp):
            return
        key = self._key(binary, app, fp)
        now = self._clock()
        cat = decision.category.value if hasattr(decision.category, "value") else str(decision.category)
        with self._lock:
            self._load()
            e = self._entries.get(key)
            changed = False
            if e is None:
                e = {"ikili": binary, "uygulama": app, "flatpak": fp, "ilk": now, "son": now,
                     "sayi": 0, "kategori": "", "profil": None, "neden": ""}
                self._entries[key] = e
                changed = True
            if self._last_index.get(key) != stream.index:
                self._last_index[key] = stream.index
                e["sayi"] += 1
                changed = True
            new = {"kategori": cat, "profil": decision.profile, "neden": decision.reason}
            if any(e[k] != v for k, v in new.items()):
                e.update(new)
                changed = True
            if now - e["son"] >= TOUCH_RESAVE:
                changed = True
            e["son"] = now
            if changed:
                self._dirty = True
                self._trim()
                self._schedule()

    def recent(self, limit: int = 50) -> list[dict]:
        """Newest first (copies)."""
        with self._lock:
            self._load()
            items = sorted(self._entries.values(), key=lambda e: -e["son"])
            return [dict(e) for e in items[: max(0, int(limit))]]

    def forget(self, binary: str, app: str, flatpak: str = "") -> bool:
        with self._lock:
            self._load()
            gone = self._entries.pop(self._key(binary, app, flatpak), None)
            if gone is not None:
                self._dirty = True
                self._schedule()
            return gone is not None


# ---------------------------------------------------------------- module-level default
_default: History | None = None
_default_lock = threading.Lock()


def default() -> History:
    global _default
    with _default_lock:
        if _default is None:
            _default = History()
            atexit.register(_default.flush)
        return _default


def record(stream: Stream, decision: Decision, flatpak: str | None = None) -> None:
    default().record(stream, decision, flatpak)


def recent(limit: int = 50) -> list[dict]:
    return default().recent(limit)


def flush() -> None:
    if _default is not None:
        _default.flush()
