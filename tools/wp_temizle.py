#!/usr/bin/env python3
"""WirePlumber durum dosyalarından oto-eq TEST kalıntılarını temizler.

    python3 tools/wp_temizle.py              # varsayılan: --dry-run (yalnız gösterir)
    python3 tools/wp_temizle.py --uygula     # yedekle, WP'yi durdur, temizle, başlat

Only entries whose key/value clearly came from oto-eq's own tests are removed
(``PATTERNS``); real apps and devices are never touched (``PROTECTED`` is an
extra guard). WirePlumber keeps the state in memory and rewrites the files,
so ``--uygula`` stops it first, re-reads the files AFTER the stop, rewrites
them (all other bytes unchanged), starts it again and waits until the
default sink/source are back. Any failure → backups restored + WP restarted.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

FILES = ("stream-properties", "default-nodes", "default-routes")

# Matched against the decoded VALUE part of a key (stream-properties:
# "<class>:<prop>:<value>"), a default-nodes node name, or a default-routes
# card name. Keep these narrow: when unsure, keep the entry.
PATTERNS = [
    r"^oto-eq-test",                 # A1 fake_stream / restore experiments, eqgen check
    r"^oto_eq_test_",                # A1/A4 test null sinks, test EQ sinks
    r"^oto_eq_mikrofon_test",        # micgen check
    r"^oto-eq-mic-check$",           # micgen check recorder/player
    r"^oto_eq_perf_",                # perf tests
    r"^a3_repro_",                   # A3 repro null sinks
    r"^a3rep-",                      # A3 repro streams
    r"^a4 bqtest",                   # A4 biquad test chain
    r"^Kulak Kesil · .*\(test\)$",   # test instances of our own chains
]
_RX = [re.compile(p) for p in PATTERNS]

# Never removed, whatever the patterns say (case-insensitive substring).
PROTECTED = ("firefox", "chromium", "chrome", "discord", "spotify", "vlc", "steam",
             "webrtc", "mpv", "haruna", "obs", "alsa_card.", "alsa_output.", "alsa_input.",
             "bluez", "speech-dispatcher", "notification", "plasma", "kde")

STREAM_PROPS = ("application.name", "application.id", "media.name", "node.name")


def wp_unescape(s: str) -> str:
    r"""WirePlumber state-key escaping: '\s' = space, '\\' = backslash."""
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append({"s": " ", "\\": "\\"}.get(s[i + 1], "\\" + s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def is_test_name(name: str) -> bool:
    low = name.lower()
    if any(p in low for p in PROTECTED):
        return False
    return any(rx.search(name) for rx in _RX)


def entry_name(fname: str, line: str) -> str | None:
    """The name a line refers to (decoded), or None for headers/comments/other."""
    s = line.rstrip("\r\n")
    if not s or s.startswith(("#", "[")) or "=" not in s:
        return None
    key, val = s.split("=", 1)
    if fname == "stream-properties":
        parts = key.split(":", 2)
        if len(parts) == 3 and parts[1] in STREAM_PROPS:
            return wp_unescape(parts[2])
        return None
    if fname == "default-nodes":
        return val.strip() if key.startswith("default.configured.") else None
    if fname == "default-routes":
        card = key.split(":", 1)[0]
        return card[len("alsa_card."):] if card.startswith("alsa_card.") else card
    return None


_DN_KEY = re.compile(r"^(default\.configured\.[a-z]+\.[a-z]+)(?:\.(\d+))?=")


@dataclass
class Plan:
    removed: dict[str, list[str]] = field(default_factory=dict)   # file -> removed lines
    new_text: dict[str, str] = field(default_factory=dict)         # file -> new content

    @property
    def count(self) -> int:
        return sum(len(v) for v in self.removed.values())


def _renumber_default_nodes(lines: list[str]) -> list[str]:
    """Keep WP's history chain gap-free: base, base.0, base.1, ... (state-default-nodes.lua)."""
    seen: dict[str, int] = {}
    out = []
    for ln in lines:
        m = _DN_KEY.match(ln)
        if not m:
            out.append(ln)
            continue
        base = m.group(1)
        n = seen.get(base, -1)
        key = base if n < 0 else f"{base}.{n}"
        seen[base] = n + 1
        out.append(key + ln[m.end() - 1:])
    return out


def plan_file(fname: str, text: str) -> tuple[list[str], str]:
    """(removed lines, new text) for one state file."""
    lines = text.splitlines(keepends=True)
    keep, removed = [], []
    for ln in lines:
        name = entry_name(fname, ln)
        if name is not None and is_test_name(name):
            removed.append(ln.rstrip("\r\n"))
        else:
            keep.append(ln)
    if removed and fname == "default-nodes":
        keep = _renumber_default_nodes(keep)
    return removed, "".join(keep)


def _read(p: Path) -> str:
    return p.read_bytes().decode("utf-8", "surrogateescape")


def make_plan(state_dir: Path) -> Plan:
    plan = Plan()
    for f in FILES:
        p = state_dir / f
        if not p.exists():
            continue
        removed, new = plan_file(f, _read(p))
        if removed:
            plan.removed[f] = removed
            plan.new_text[f] = new
    return plan


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(p.name + ".tmp-wp-temizle")
    tmp.write_bytes(text.encode("utf-8", "surrogateescape"))
    shutil.copymode(p, tmp)
    os.replace(tmp, p)


# ---------------------------------------------------------------- system side

Runner = Callable[[list[str]], subprocess.CompletedProcess]


def default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)


def _defaults(run: Runner) -> tuple[str, str]:
    def get(what):
        try:
            r = run(["pactl", f"get-default-{what}"])
            return r.stdout.strip() if r.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            return ""
    return get("sink"), get("source")


def apply(state_dir: Path, run: Runner = default_runner, say=print,
          wait_s: float = 10.0, sleep=time.sleep, stamp: str | None = None) -> bool:
    """Back up, stop WP, rewrite, start WP, verify defaults. Restores on failure."""
    plan = make_plan(state_dir)
    if not plan.count:
        say("Temizlenecek test kalıntısı yok; hiçbir şey değiştirilmedi.")
        return True
    sink0, src0 = _defaults(run)
    stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
    backups: dict[str, Path] = {}
    stopped = False
    try:
        for f in FILES:
            p = state_dir / f
            if p.exists():
                b = p.with_name(f"{p.name}.yedek-{stamp}")
                shutil.copy2(p, b)
                backups[f] = b
        say(f"Yedeklendi: {', '.join(str(b) for b in backups.values())}")
        r = run(["systemctl", "--user", "stop", "wireplumber"])
        stopped = True
        if r.returncode != 0:
            raise RuntimeError(f"wireplumber durdurulamadı: {(r.stderr or r.stdout).strip()}")
        # WP may flush its in-memory state while stopping: plan again on the final files
        plan = make_plan(state_dir)
        for f, text in plan.new_text.items():
            _write_atomic(state_dir / f, text)
        r = run(["systemctl", "--user", "start", "wireplumber"])
        if r.returncode != 0:
            raise RuntimeError(f"wireplumber başlatılamadı: {(r.stderr or r.stdout).strip()}")
        stopped = False
        deadline = time.monotonic() + wait_s
        while True:
            sink, src = _defaults(run)
            if sink == sink0 and (not src0 or src == src0):
                break
            if time.monotonic() > deadline:
                raise RuntimeError(f"varsayılan çıkış/mikrofon geri gelmedi "
                                   f"(çıkış '{sink}' ≠ '{sink0}' veya mikrofon '{src}' ≠ '{src0}')")
            sleep(0.25)
    except Exception as e:  # noqa: BLE001 - always restore
        say(f"HATA: {e}")
        say("Yedekler geri yükleniyor ve WirePlumber yeniden başlatılıyor…")
        try:
            run(["systemctl", "--user", "stop", "wireplumber"])
        except Exception:  # noqa: BLE001
            pass
        for f, b in backups.items():
            shutil.copy2(b, state_dir / f)
        try:
            run(["systemctl", "--user", "start", "wireplumber"])
        except Exception:  # noqa: BLE001
            pass
        return False
    finally:
        if stopped:   # never leave WP stopped
            try:
                run(["systemctl", "--user", "start", "wireplumber"])
            except Exception:  # noqa: BLE001
                pass
    say(f"Tamam: {plan.count} test kalıntısı silindi "
        f"({', '.join(f'{f}: {len(v)}' for f, v in plan.removed.items())}).")
    say(f"Varsayılan çıkış: {sink0 or '-'}; varsayılan mikrofon: {src0 or '-'} (değişmedi).")
    say("Yedekler duruyor; sorun olursa: " +
        "; ".join(f"cp {b} {state_dir / f}" for f, b in backups.items()))
    return True


def print_plan(plan: Plan, state_dir: Path, say=print) -> None:
    if not plan.count:
        say("Temizlenecek test kalıntısı yok.")
        return
    for f, lines in plan.removed.items():
        say(f"{state_dir / f}: {len(lines)} satır silinecek")
        for ln in lines:
            say(f"  - {ln}")
    say(f"Toplam {plan.count} satır. Uygulamak için: --uygula "
        "(WirePlumber birkaç saniye durdurulur; ses kısa süre kesilebilir).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WirePlumber durumundan oto-eq test kalıntılarını temizle")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True,
                   help="yalnız silinecek satırları göster (varsayılan)")
    g.add_argument("--uygula", action="store_true", help="yedekle ve temizle")
    ap.add_argument("--dizin", type=Path,
                    default=Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
                    / "wireplumber", help="WirePlumber durum dizini")
    a = ap.parse_args(argv)
    if a.uygula:
        return 0 if apply(a.dizin) else 1
    print_plan(make_plan(a.dizin), a.dizin)
    return 0


if __name__ == "__main__":
    sys.exit(main())
