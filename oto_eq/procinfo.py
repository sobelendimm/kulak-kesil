"""Process facts from /proc, and mapping of (possibly namespaced) stream PIDs
to real host processes.

Strategy for ``resolve_pid`` (see docs/NOTES-detect.md):

1. ``application.process.id`` is accepted when the host process with that PID
   exists and its comm / exe / argv[0] matches ``application.process.binary``
   (comm is truncated to 15 chars; wine games may report the exe name or
   ``wine64-preloader``).
2. Otherwise the PID is assumed to be namespace-local (flatpak's bwrap uses
   ``--unshare-pid``; Steam's pressure-vessel does NOT, verified). Every host
   process lists its PIDs in nested namespaces in ``/proc/<pid>/status``
   ``NSpid:``; a process whose innermost NSpid equals the stream PID and
   whose name matches the binary is the stream's process.
3. Fallback: processes whose name matches the binary, scored by flatpak id
   (stream props / ``FLATPAK_ID`` env), Steam/wine env; the oldest best
   candidate wins (main process rather than a helper child).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections.abc import Iterable

from .models import ProcInfo, Stream

log = logging.getLogger("oto_eq.procinfo")

PROC = "/proc"

# Environment variables kept in ProcInfo.env.
ENV_KEYS = frozenset({
    "SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "STEAM_COMPAT_DATA_PATH",
    "STEAM_COMPAT_CLIENT_INSTALL_PATH", "GAMEID", "UMU_ID", "STORE",
    "WINEPREFIX", "WINELOADER", "WINESERVER", "WINEDLLOVERRIDES",
    "LUTRIS_GAME_UUID", "LUTRIS_GAME_NAME", "FLATPAK_ID", "container",
})
ENV_PREFIXES = ("FAUGUS", "PROTON", "UMU_", "LUTRIS_")

# Stream properties that may carry a flatpak application id.
FLATPAK_PROP_KEYS = (
    "pipewire.access.portal.app_id",
    "application.id",
    "flatpak.app_id",
)

INFO_TTL = 5.0          # seconds a ProcInfo is reused for the same (pid, starttime)
RESOLVE_TTL = 30.0      # seconds a successful resolve_pid result is reused
RESOLVE_FAIL_TTL = 2.0  # seconds a failed resolve is remembered
MAX_ANCESTORS = 64
ANCESTOR_CMD_MAX = 300

_lock = threading.Lock()
_info_cache: dict[int, tuple[int, float, ProcInfo]] = {}
_resolve_cache: dict[tuple, tuple[float, int | None, int | None]] = {}


# ---------------------------------------------------------------- low level

def _read(path: str, binary: bool = False, limit: int = 1 << 20):
    try:
        with open(path, "rb") as f:
            data = f.read(limit)
    except (OSError, ValueError):
        return None
    return data if binary else data.decode("utf-8", "replace")


def read_stat(pid: int) -> tuple[str, int, int] | None:
    """(comm, ppid, starttime) from /proc/<pid>/stat, None if gone."""
    raw = _read(f"{PROC}/{pid}/stat")
    if not raw:
        return None
    l, r = raw.find("("), raw.rfind(")")
    if l < 0 or r < 0:
        return None
    comm = raw[l + 1:r]
    rest = raw[r + 2:].split()
    try:
        return comm, int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def read_cmdline(pid: int) -> tuple[str, ...]:
    raw = _read(f"{PROC}/{pid}/cmdline", binary=True, limit=65536)
    if not raw:
        return ()
    return tuple(p.decode("utf-8", "replace") for p in raw.split(b"\0") if p)


def read_env(pid: int, keys: Iterable[str] = ENV_KEYS,
             prefixes: tuple[str, ...] = ENV_PREFIXES) -> dict[str, str]:
    """Subset of the process environment (empty dict if unreadable)."""
    raw = _read(f"{PROC}/{pid}/environ", binary=True, limit=1 << 20)
    if not raw:
        return {}
    keys = set(keys)
    out: dict[str, str] = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.partition(b"=")
        if not sep:
            continue
        ks = k.decode("utf-8", "replace")
        if ks in keys or ks.startswith(prefixes):
            out[ks] = v.decode("utf-8", "replace")
    return out


def read_exe(pid: int) -> str:
    try:
        target = os.readlink(f"{PROC}/{pid}/exe")
    except OSError:
        return ""
    if target.endswith(" (deleted)"):
        target = target[:-10]
    return os.path.basename(target)


def ns_pids(pid: int) -> tuple[int, ...]:
    """NSpid chain (host pid first, innermost namespace last)."""
    raw = _read(f"{PROC}/{pid}/status", limit=8192)
    if not raw:
        return ()
    for line in raw.splitlines():
        if line.startswith("NSpid:"):
            try:
                return tuple(int(x) for x in line.split()[1:])
            except ValueError:
                return ()
    return ()


def flatpak_id(pid: int) -> str:
    """Flatpak app id of a host process ('' if not sandboxed / unknown)."""
    env = read_env(pid, keys=("FLATPAK_ID", "container"), prefixes=())
    if env.get("FLATPAK_ID"):
        return env["FLATPAK_ID"]
    raw = _read(f"{PROC}/{pid}/root/.flatpak-info", limit=16384)
    if raw:
        m = re.search(r"^\[Application\][^\[]*?^name=(\S+)", raw, re.M | re.S)
        if m:
            return m.group(1)
    return ""


def basename_any(path: str) -> str:
    """Basename for both POSIX and Windows (wine argv[0]) paths."""
    return re.split(r"[\\/]", path)[-1] if path else ""


# ---------------------------------------------------------------- proc_info

def proc_info(pid: int) -> ProcInfo | None:
    """Facts about a host process, or None if it does not exist/unreadable."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    st = read_stat(pid)
    if st is None:
        return None
    comm, ppid, start = st
    now = time.monotonic()
    with _lock:
        hit = _info_cache.get(pid)
        if hit and hit[0] == start and hit[1] > now:
            return hit[2]
    cmdline = read_cmdline(pid)
    ancestors: list[tuple[int, str, str]] = []
    seen = {pid}
    cur = ppid
    while cur > 0 and cur not in seen and len(ancestors) < MAX_ANCESTORS:
        seen.add(cur)
        ast = read_stat(cur)
        if ast is None:
            break
        acmd = " ".join(read_cmdline(cur))[:ANCESTOR_CMD_MAX]
        ancestors.append((cur, ast[0], acmd))
        cur = ast[1]
    info = ProcInfo(pid=pid, comm=comm, exe=read_exe(pid), cmdline=cmdline,
                    env=read_env(pid), ancestors=tuple(ancestors))
    with _lock:
        if len(_info_cache) > 2048:
            _info_cache.clear()
        _info_cache[pid] = (start, now + INFO_TTL, info)
    return info


def clear_cache() -> None:
    with _lock:
        _info_cache.clear()
        _resolve_cache.clear()


# ---------------------------------------------------------------- resolve_pid

def binary_matches(info: ProcInfo | None, binary: str) -> bool:
    """True if ``binary`` (application.process.binary) names this process."""
    if info is None:
        return False
    b = (binary or "").strip().lower()
    if not b:
        return True
    names = {info.comm.lower(), info.exe.lower()}
    if info.cmdline:
        names.add(basename_any(info.cmdline[0]).lower())
    names.discard("")
    if b in names:
        return True
    comm = info.comm.lower()
    # comm is truncated to 15 bytes (TASK_COMM_LEN-1)
    if len(comm) >= 15 and b.startswith(comm):
        return True
    if b[:15] == comm:
        return True
    # "Game.exe" vs comm "Game.exe" handled above; "game" vs "Game.exe":
    if b.endswith(".exe") and b[:-4] in names:
        return True
    return False


def _stream_flatpak_hint(stream: Stream) -> str:
    props = stream.props or {}
    for k in FLATPAK_PROP_KEYS:
        v = props.get(k, "")
        if v and "." in v:
            return v
    return ""


def _iter_pids():
    try:
        names = os.listdir(PROC)
    except OSError:
        return
    for n in names:
        if n.isdigit():
            yield int(n)


def _status_name_nspid(pid: int) -> tuple[str, tuple[int, ...]] | None:
    raw = _read(f"{PROC}/{pid}/status", limit=8192)
    if not raw:
        return None
    name, nsp = "", ()
    for line in raw.splitlines():
        if line.startswith("Name:"):
            name = line[5:].strip()
        elif line.startswith("NSpid:"):
            try:
                nsp = tuple(int(x) for x in line.split()[1:])
            except ValueError:
                nsp = ()
            break
    return name, nsp


def _search(stream: Stream) -> int | None:
    binary = (stream.binary or "").strip()
    bl = binary.lower()
    want_ns = stream.pid if stream.pid and stream.pid > 0 else None
    fp_hint = _stream_flatpak_hint(stream)
    best: tuple[int, int, int] | None = None   # (score, -starttime, pid)
    me = os.getpid()
    for pid in _iter_pids():
        if pid == me:
            continue
        sn = _status_name_nspid(pid)
        if sn is None:
            continue
        name, nsp = sn
        nl = name.lower()
        ns_hit = want_ns is not None and len(nsp) > 1 and nsp[-1] == want_ns
        name_hit = bool(bl) and (nl == bl[:15] or (len(nl) >= 15 and bl.startswith(nl))
                                 or (bl.endswith(".exe") and nl == bl[:-4][:15]))
        if not ns_hit and not name_hit:
            continue
        info = proc_info(pid)
        if info is None:
            continue
        if bl and not binary_matches(info, binary):
            continue
        score = 0
        if ns_hit:
            score += 8
        if bl:
            score += 2
        env = info.env
        if fp_hint and env.get("FLATPAK_ID") == fp_hint:
            score += 4
        if any(env.get(k) for k in ("SteamAppId", "SteamGameId", "WINEPREFIX",
                                    "STEAM_COMPAT_DATA_PATH", "GAMEID")):
            score += 1
        if not bl and not ns_hit:
            continue
        st = read_stat(pid)
        start = st[2] if st else 0
        cand = (score, -start, pid)
        if best is None or cand > best:
            best = cand
    return best[2] if best else None


def resolve_pid(stream: Stream) -> int | None:
    """Host PID of the process that owns ``stream`` (None if not found)."""
    try:
        return _resolve(stream)
    except Exception:  # never let detection crash the router
        log.exception("resolve_pid hatası (akış %s)", getattr(stream, "index", "?"))
        return None


def _resolve(stream: Stream) -> int | None:
    key = (stream.index, stream.pid, stream.binary)
    now = time.monotonic()
    with _lock:
        hit = _resolve_cache.get(key)
    if hit and hit[0] > now:
        cached_pid, cached_start = hit[1], hit[2]
        if cached_pid is None:
            return None
        st = read_stat(cached_pid)
        if st and st[2] == cached_start:
            return cached_pid
    result: int | None = None
    pid = stream.pid
    props = stream.props or {}
    # Native PipeWire clients: pipewire.sec.pid comes from the socket
    # credentials (kernel-translated host pid, valid for flatpak too).
    # For pulse clients it is the pipewire-pulse server pid -> ignored.
    sec = props.get("pipewire.sec.pid", "")
    if sec and props.get("client.api") != "pipewire-pulse":
        try:
            sp = int(sec)
        except ValueError:
            sp = 0
        if sp > 0:
            sinfo = proc_info(sp)
            if sinfo is not None and sinfo.comm not in ("pipewire", "pipewire-pulse"):
                result = sp
    if result is None and pid and pid > 0:
        info = proc_info(pid)
        if info is not None and binary_matches(info, stream.binary):
            # Guard against a namespaced pid that happens to hit an unrelated
            # host process when no binary is known: require it not be a
            # kernel thread (empty cmdline).
            if stream.binary or info.cmdline:
                result = pid
    if result is None and (stream.binary or (pid and pid > 0)):
        result = _search(stream)
        if result is not None and result != pid:
            log.debug("akış %s: pid %s → host pid %s", stream.index, pid, result)
    start = None
    if result is not None:
        st = read_stat(result)
        start = st[2] if st else None
    ttl = RESOLVE_TTL if result is not None else RESOLVE_FAIL_TTL
    with _lock:
        if len(_resolve_cache) > 1024:
            _resolve_cache.clear()
        _resolve_cache[key] = (now + ttl, result, start)
    return result


# ---------------------------------------------------------------- open files

def open_files(pid: int, limit: int = 1024) -> tuple[str, ...]:
    """Paths of regular files the process has open (for media-type sniffing)."""
    out: list[str] = []
    base = f"{PROC}/{pid}/fd"
    try:
        fds = os.listdir(base)
    except OSError:
        return ()
    for fd in fds[:limit]:
        try:
            t = os.readlink(f"{base}/{fd}")
        except OSError:
            continue
        if t.startswith("/") and not t.startswith(("/dev/", "/proc/", "/sys/")):
            if t.endswith(" (deleted)"):
                t = t[:-10]
            out.append(t)
    return tuple(out)
