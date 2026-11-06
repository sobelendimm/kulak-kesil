"""Thin pactl wrapper: list streams/sinks, move streams, listen for events.

Everything goes through the ``pactl`` CLI (pipewire-pulse), always with
``config.C_ENV`` (LC_ALL=C) and a timeout.  JSON output (``pactl -f json``)
is preferred; if it cannot be decoded (pactl 17 has had escaping bugs with
odd characters in media.name etc.) the plain-text output is parsed instead.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from typing import Callable

from . import config
from .i18n import _
from .models import Event, Sink, Stream

log = logging.getLogger("oto_eq.pulse")

PACTL = "pactl"
TIMEOUT = 3.0               # seconds, per pactl call
INVALID_INDEX = 0xFFFFFFFF  # PA_INVALID_INDEX


class PulseError(Exception):
    """pactl missing, timed out, or the sound server is unreachable."""


# ---------------------------------------------------------------- helpers

def _run(args: list[str], timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    """Run pactl with C locale; raise PulseError on spawn failure/timeout."""
    cmd = [PACTL, *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, env=config.C_ENV,
                              timeout=timeout, errors="replace")
    # user-facing (`kulak teshis` shows them): translated; the journal gets the same text
    except FileNotFoundError as e:
        raise PulseError(_("pactl bulunamadı: {hata}").format(hata=e)) from e
    except subprocess.TimeoutExpired as e:
        raise PulseError(_("pactl zaman aşımı ({sn} sn): {komut}").format(
            sn=f"{timeout:.0f}", komut=" ".join(args))) from e
    except OSError as e:
        raise PulseError(_("pactl çalıştırılamadı: {hata}").format(hata=e)) from e


def _run_ok(args: list[str], timeout: float = TIMEOUT) -> str:
    """Run pactl and return stdout; raise PulseError on non-zero exit."""
    p = _run(args, timeout)
    if p.returncode != 0:
        raise PulseError(_("pactl {komut} başarısız (kod {kod}): {hata}").format(
            komut=" ".join(args), kod=p.returncode, hata=p.stderr.strip() or p.stdout.strip()))
    return p.stdout


def _int_or_none(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _norm_index(v) -> int:
    """pactl index → int; missing/PA_INVALID_INDEX → -1."""
    i = _int_or_none(v)
    if i is None or i < 0 or i == INVALID_INDEX:
        return -1
    return i


def _str_props(props) -> dict[str, str]:
    if not isinstance(props, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in props.items():
        if v is None:
            out[str(k)] = ""
        elif isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        elif isinstance(v, (dict, list)):
            out[str(k)] = json.dumps(v, ensure_ascii=False)
        else:
            out[str(k)] = str(v)
    return out


def _bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "yes", "true", "on")


def make_stream(index, sink_index, props: dict[str, str], corked) -> Stream:
    """Build a models.Stream from raw values (shared by JSON and text parsers)."""
    return Stream(
        index=_norm_index(index),
        sink_index=_norm_index(sink_index),
        app_name=props.get("application.name", ""),
        binary=props.get("application.process.binary", ""),
        pid=_int_or_none(props.get("application.process.id")),
        media_name=props.get("media.name", ""),
        media_role=props.get("media.role", ""),
        node_name=props.get("node.name", ""),
        corked=_bool(corked),
        props=props,
    )


CLIENT_KEY = "oto_eq.client_index"   # pactl client index of the stream (added by us)


def _note_client(props: dict[str, str], client) -> None:
    ci = _norm_index(client)
    if ci >= 0:
        props[CLIENT_KEY] = str(ci)


def _loads(text: str):
    # strict=False tolerates raw control characters inside strings.
    return json.loads(text, strict=False)


# ---------------------------------------------------------------- JSON parsers

def parse_streams_json(text: str) -> list[Stream]:
    """Parse ``pactl -f json list sink-inputs``. Raises ValueError on bad JSON."""
    data = _loads(text)
    if not isinstance(data, list):
        raise ValueError("sink-input JSON bir liste değil")
    out = []
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            continue
        props = _str_props(item.get("properties"))
        _note_client(props, item.get("client"))
        corked = item.get("corked", props.get("pulse.corked", False))
        out.append(make_stream(item.get("index"), item.get("sink"), props, corked))
    return out


def parse_sinks_json(text: str) -> list[Sink]:
    """Parse ``pactl -f json list sinks``. Raises ValueError on bad JSON."""
    data = _loads(text)
    if not isinstance(data, list):
        raise ValueError("sink JSON bir liste değil")
    out = []
    for item in data:
        if not isinstance(item, dict) or "index" not in item:
            continue
        out.append(Sink(index=_norm_index(item.get("index")),
                        name=str(item.get("name") or ""),
                        description=str(item.get("description") or ""),
                        state=str(item.get("state") or "").upper(),
                        info=item))
    return out


# ---------------------------------------------------------------- text parsers

_HEADER_RE = re.compile(r"^([A-Z][A-Za-z ]*?) #(\d+)\s*$")
_FIELD_RE = re.compile(r"^\t([A-Za-z][A-Za-z ]*):\s?(.*)$")
_PROP_RE = re.compile(r"^\t\t([^\s=]+) = (.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
        v = re.sub(r'\\(["\\])', r"\1", v)
    return v


def _parse_text_blocks(text: str, header: str) -> list[tuple[int, dict[str, str], dict[str, str]]]:
    """Split pactl text output into (index, fields, props) per object."""
    blocks: list[tuple[int, dict[str, str], dict[str, str]]] = []
    cur: tuple[int, dict[str, str], dict[str, str]] | None = None
    in_props = False
    last_prop: str | None = None
    for raw in text.splitlines():
        m = _HEADER_RE.match(raw)
        if m:
            cur = (int(m.group(2)), {}, {}) if m.group(1) == header else None
            if cur is not None:
                blocks.append(cur)
            in_props, last_prop = False, None
            continue
        if cur is None:
            continue
        _, fields, props = cur
        if in_props:
            pm = _PROP_RE.match(raw)
            if pm:
                last_prop = pm.group(1)
                props[last_prop] = _unquote(pm.group(2))
                continue
            if raw.startswith("\t\t") or (raw and not raw.startswith("\t")):
                # continuation of a multi-line value (newline inside media.name)
                if last_prop is not None:
                    props[last_prop] = _unquote(props[last_prop] + "\n" + raw.strip())
                continue
            in_props = False
        fm = _FIELD_RE.match(raw)
        if fm:
            name = fm.group(1).strip()
            if name == "Properties":
                in_props, last_prop = True, None
            else:
                fields[name] = fm.group(2).strip()
    return blocks


def parse_streams_text(text: str) -> list[Stream]:
    """Parse plain ``pactl list sink-inputs`` (LC_ALL=C)."""
    out = []
    for index, fields, props in _parse_text_blocks(text, "Sink Input"):
        _note_client(props, fields.get("Client"))
        corked = fields.get("Corked", props.get("pulse.corked", "no"))
        out.append(make_stream(index, fields.get("Sink"), props, corked))
    return out


def parse_clients_json(text: str) -> dict[int, dict[str, str]]:
    """Parse ``pactl -f json list clients`` → {client index: props}."""
    data = _loads(text)
    if not isinstance(data, list):
        raise ValueError("client JSON bir liste değil")
    return {_norm_index(c.get("index")): _str_props(c.get("properties"))
            for c in data if isinstance(c, dict) and "index" in c}


def parse_clients_text(text: str) -> dict[int, dict[str, str]]:
    """Parse plain ``pactl list clients``."""
    return {index: props for index, _, props in _parse_text_blocks(text, "Client")}


def parse_sinks_text(text: str) -> list[Sink]:
    """Parse plain ``pactl list sinks`` (LC_ALL=C)."""
    return [Sink(index=index,
                 name=fields.get("Name", ""),
                 description=fields.get("Description", ""),
                 state=fields.get("State", "").upper())
            for index, fields, _ in _parse_text_blocks(text, "Sink")]


# ---------------------------------------------------------------- public API

def _list(kind: str, parse_json, parse_text):
    p = _run(["-f", "json", "list", kind])
    if p.returncode == 0:
        try:
            return parse_json(p.stdout)
        except ValueError as e:
            log.warning("pactl JSON çözümlenemedi (%s), düz metne geçiliyor: %s", kind, e)
    else:
        log.debug("pactl -f json list %s kod %d: %s", kind, p.returncode, p.stderr.strip())
    return parse_text(_run_ok(["list", kind]))


# Client keys copied into a stream's props when the stream itself lacks them.
# Native PipeWire streams (pw-play, SDL/pipewire games, ...) carry NO
# application.process.* in their node props; only their client has them.
# Flatpak clients get pipewire.access.portal.app_id/instance_id on the
# client object only.
_CLIENT_COPY_PREFIXES = ("application.", "pipewire.access.portal.")


def merge_client_props(streams: list[Stream],
                       clients: dict[int, dict[str, str]]) -> list[Stream]:
    """Fill missing application.* (and portal) keys from the owning client.

    Existing stream keys are never overwritten. For native clients (no
    ``client.api=pipewire-pulse``) ``pipewire.sec.pid`` (host PID from the
    socket credentials, also valid for Flatpak apps) is copied too; for pulse
    clients it is the pipewire-pulse server PID, so it is skipped.
    Copied keys are listed in ``props["oto_eq.from_client"]``.
    """
    out = []
    for st in streams:
        cprops = clients.get(_norm_index(st.props.get(CLIENT_KEY)))
        if not cprops:
            out.append(st)
            continue
        props = dict(st.props)
        copied = []
        for k, v in cprops.items():
            if k in props:
                continue
            if k.startswith(_CLIENT_COPY_PREFIXES) or (
                    k == "pipewire.sec.pid" and cprops.get("client.api") != "pipewire-pulse"):
                props[k] = v
                copied.append(k)
        if not copied:
            out.append(st)
            continue
        props["oto_eq.from_client"] = ",".join(sorted(copied))
        out.append(make_stream(st.index, st.sink_index, props, st.corked))
    return out


def list_clients() -> dict[int, dict[str, str]]:
    """{pactl client index: props}."""
    return _list("clients", parse_clients_json, parse_clients_text)


def list_streams() -> list[Stream]:
    """All playback streams (sink-inputs), client props merged in if missing."""
    streams = _list("sink-inputs", parse_streams_json, parse_streams_text)
    if streams:
        # Always merged: Flatpak pulse clients keep pipewire.access.portal.*
        # only on the client object.
        try:
            streams = merge_client_props(streams, list_clients())
        except PulseError as e:
            log.debug("istemci listesi alınamadı: %s", e)
    return streams


def list_sinks() -> list[Sink]:
    """All sinks."""
    return _list("sinks", parse_sinks_json, parse_sinks_text)


def default_sink_name() -> str:
    """Name of the current default sink ('' if none)."""
    name = _run_ok(["get-default-sink"]).strip()
    return "" if name in ("", "@DEFAULT_SINK@") else name


# ---------------------------------------------------------------- HS80 takibi helpers
# (added by A2 for the router's headset follow feature; volumes are raw pulse
# units: 65536 = 100 %, cubic scale as shown by pactl)

VOLUME_NORM = 65536
_VOL_RE = re.compile(r"(\d+)\s*/\s*\d+%")


def parse_volume(text: str) -> int | None:
    """``pactl get-sink-volume`` output → loudest channel (raw), None if unparsable."""
    vals = [int(v) for v in _VOL_RE.findall(text or "")]
    return max(vals) if vals else None


def _get_volume(kind: str, name: str) -> int | None:
    try:
        return parse_volume(_run_ok([f"get-{kind}-volume", name]))
    except PulseError as e:
        log.debug("%s ses seviyesi okunamadı (%s): %s", kind, name, e)
        return None


def _set_volume(kind: str, name: str, raw: int) -> bool:
    raw = max(0, int(raw))
    try:
        _run_ok([f"set-{kind}-volume", name, str(raw)])
        return True
    except PulseError as e:
        log.warning("%s ses seviyesi ayarlanamadı (%s): %s", kind, name, e)
        return False


def get_sink_volume(name: str) -> int | None:
    return _get_volume("sink", name)


def set_sink_volume(name: str, raw: int) -> bool:
    return _set_volume("sink", name, raw)


def get_source_volume(name: str) -> int | None:
    return _get_volume("source", name)


def set_source_volume(name: str, raw: int) -> bool:
    return _set_volume("source", name, raw)


def default_source_name() -> str:
    """Name of the current default source ('' if none)."""
    name = _run_ok(["get-default-source"]).strip()
    return "" if name in ("", "@DEFAULT_SOURCE@") else name


def list_source_names() -> list[str]:
    """Names of all sources (monitors included), from ``pactl list short sources``."""
    out = []
    for line in _run_ok(["list", "short", "sources"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1]:
            out.append(parts[1])
    return out


def set_default_sink(name: str) -> bool:
    try:
        _run_ok(["set-default-sink", name])
        return True
    except PulseError as e:
        log.warning("varsayılan çıkış ayarlanamadı (%s): %s", name, e)
        return False


def set_default_source(name: str) -> bool:
    try:
        _run_ok(["set-default-source", name])
        return True
    except PulseError as e:
        log.warning("varsayılan giriş ayarlanamadı (%s): %s", name, e)
        return False


def move_stream(stream_index: int, sink_name: str) -> bool:
    """Move a sink-input to a sink; False if the stream vanished or pactl failed."""
    try:
        p = _run(["move-sink-input", str(int(stream_index)), sink_name])
    except PulseError as e:
        log.warning("akış #%s taşınamadı: %s", stream_index, e)
        return False
    if p.returncode != 0:
        log.info("akış #%s → %s taşınamadı: %s", stream_index, sink_name,
                 (p.stderr or p.stdout).strip())
        return False
    log.debug("akış #%s → %s taşındı", stream_index, sink_name)
    return True


# ---------------------------------------------------------------- events

_EVENT_RE = re.compile(r"^Event '(\w+)' on ([\w-]+) #(\d+)\s*$")


def parse_event_line(line: str) -> Event | None:
    """``Event 'new' on sink-input #12`` → Event; None for anything else."""
    m = _EVENT_RE.match(line.strip())
    if not m:
        return None
    return Event(kind=m.group(1), facility=m.group(2), index=_norm_index(m.group(3)))


class EventListener:
    """Runs ``pactl subscribe`` on a daemon thread and forwards parsed events.

    If pactl exits (e.g. pipewire-pulse restarted) it is restarted with an
    exponential backoff of 0.5 → 5 s.  After every *re*start a synthetic
    ``Event("change", "server", -1)`` is delivered so the consumer rescans
    (events may have been missed while disconnected).  The callback runs on
    the listener thread; exceptions in it are logged and swallowed.
    """

    BACKOFF_MIN = 0.5
    BACKOFF_MAX = 5.0
    STABLE_SECS = 10.0      # a run this long resets the backoff
    SIGNAL_GRACE = 2.0      # pactl exited 0 (signalled): wait this long for our own stop()

    def __init__(self, callback: Callable[[Event], None]):
        self._callback = callback
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self.restarts = 0   # number of times pactl had to be respawned

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oto_eq.pulse.events",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            _kill(proc)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._thread = None

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- internals
    def _deliver(self, ev: Event) -> None:
        try:
            self._callback(ev)
        except Exception:  # noqa: BLE001 - never let the consumer kill us
            log.exception("olay geri çağrısı hata verdi: %s", ev)

    def _spawn(self) -> subprocess.Popen | None:
        try:
            return subprocess.Popen([PACTL, "subscribe"], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                    text=True, errors="replace", bufsize=1,
                                    env=config.C_ENV)
        except OSError as e:
            log.warning("pactl subscribe başlatılamadı: %s", e)
            return None

    def _run(self) -> None:
        backoff = self.BACKOFF_MIN
        first = True
        while not self._stop.is_set():
            proc = self._spawn()
            started = time.monotonic()
            if proc is not None:
                with self._lock:
                    self._proc = proc
                if self._stop.is_set():
                    _kill(proc)
                    break
                if not first:
                    self.restarts += 1
                    log.info("pactl subscribe yeniden bağlandı")
                    self._deliver(Event("change", "server", -1))
                first = False
                self._pump(proc)
                _kill(proc, close=False)
                with self._lock:
                    self._proc = None
                err = ""
                try:
                    err = (proc.stderr.read(4096) if proc.stderr else "").strip()
                except (OSError, ValueError):
                    pass
                _kill(proc)
                if self._stop.is_set():
                    break
                if proc.returncode == 0:
                    # pactl exits 0 when signalled: systemd SIGTERMs the whole service cgroup
                    # before router.stop() sets _stop (a lost server exits 1). Give the stop a
                    # moment instead of respawning + injecting a rescan during shutdown.
                    log.debug("pactl subscribe sinyalle bitti (kod 0)")
                    if self._stop.wait(self.SIGNAL_GRACE):
                        break
                else:
                    log.warning("pactl subscribe sonlandı (kod %s)%s", proc.returncode,
                                f": {err}" if err else "")
            else:
                first = False
            if time.monotonic() - started >= self.STABLE_SECS:
                backoff = self.BACKOFF_MIN
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.BACKOFF_MAX)

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    return
                ev = parse_event_line(line)
                if ev is not None:
                    self._deliver(ev)
                elif line.strip():
                    log.debug("tanınmayan pactl satırı: %r", line)
        except (OSError, ValueError):
            pass


def _kill(proc: subprocess.Popen, close: bool = True) -> None:
    """Terminate a child process, escalate to SIGKILL, reap it."""
    if proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                log.error("pactl süreci öldürülemedi (pid %s)", proc.pid)
        except OSError:
            pass
    if not close:
        return
    for f in (proc.stdout, proc.stderr):
        try:
            if f:
                f.close()
        except OSError:
            pass

