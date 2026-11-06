"""Read-only diagnosis of live playback streams.

    python3 -m oto_eq.teshis              # one block per current stream
    python3 -m oto_eq.teshis --izle       # + new streams / MPRIS changes until Ctrl+C
    python3 -m oto_eq.teshis --json F     # also write everything to F (JSON)

Never moves streams or changes anything in the audio graph.
"""
from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import sys
import time
from typing import Any

from . import procinfo
from .i18n import N_, _, h_
from .models import SINK_PREFIX, MediaPlayer, Stream

log = logging.getLogger("oto_eq.teshis")

KEY_PROPS = ("application.name", "application.process.binary", "application.process.id",
             "media.role", "media.name", "node.name", "client.api",
             "pipewire.access.portal.app_id", "pipewire.access.portal.instance_id",
             "pipewire.sec.pid", "application.id", "oto_eq.from_client")

_CAT_COLOR = {"cs2": "1;31", "oyun": "1;33", "muzik": "1;35", "film": "1;36",
              "sesli": "1;32", "sistem": "2", "bilinmeyen": "1;37"}


class Style:
    def __init__(self, color: bool):
        self.color = color

    def c(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.color and code else text

    def dim(self, t: str) -> str:
        return self.c("2", t)

    def bold(self, t: str) -> str:
        return self.c("1", t)


# ---------------------------------------------------------------- collect

def _player_dict(p: MediaPlayer) -> dict:
    return {"bus_name": p.bus_name, "pid": p.pid, "identity": p.identity, "status": p.status,
            "title": p.title, "artist": p.artist, "url": p.url}


def collect(stream: Stream, clf, players: list[MediaPlayer],
            sink_names: dict[int, str] | None = None) -> dict[str, Any]:
    """Everything known about one stream as a JSON-serialisable dict."""
    pid, info, fp = clf.process_of(stream)
    decision, trace = clf.explain(stream, players)
    try:
        linked = clf._linked_players(_ctx_for(clf, stream, pid, info, fp), players)
    except Exception:
        linked = []
    props = stream.props or {}
    return {
        "index": stream.index,
        "corked": stream.corked,
        "sink_index": stream.sink_index,
        "sink": (sink_names or {}).get(stream.sink_index, f"#{stream.sink_index}"),
        "props": {k: props[k] for k in KEY_PROPS if k in props},
        "pid": stream.pid,
        "host_pid": pid,
        "process": None if info is None else {
            "comm": info.comm, "exe": info.exe,
            "cmdline": " ".join(info.cmdline)[:300],
            "env": dict(info.env),
            "ancestors": [{"pid": a[0], "comm": a[1], "cmdline": a[2][:120]}
                          for a in info.ancestors],
            "nspid": list(procinfo.ns_pids(info.pid)) if pid else [],
        },
        "flatpak": fp,
        "mpris": [_player_dict(p) for p in linked],
        "decision": {"category": decision.category.value, "profile": decision.profile,
                     "reason": decision.reason},
        # browser / media player stream: re-decided on MPRIS changes (--izle)
        "medya_uygulamasi": _is_media_app(clf, stream, pid, info, fp),
        "trace": trace,
    }


def _is_media_app(clf, stream, pid, info, fp) -> bool:
    try:
        names = _ctx_for(clf, stream, pid, info, fp).names
        return any(clf._match(rx, names) for rx in (clf.browser_apps, clf.player_apps))
    except Exception:
        return False


def _ctx_for(clf, stream, pid, info, fp):
    from .classifier import _Ctx
    return _Ctx(stream, pid, info, fp)


# ---------------------------------------------------------------- format

def _label(st: Style, name: str) -> str:
    return st.dim(f"  {name:<11}: ")


def _q(v: str, n: int = 70) -> str:
    v = v.replace("\n", " ")
    return v if len(v) <= n else v[: n - 1] + "…"


# MprisWatcher.mode values (ids) → shown text; an unknown value is shown as it is
_WATCHER_MODES = {"sinyal": N_("sinyal"), "yoklama": N_("yoklama"), "kapali": N_("kapalı"),
                  "yok": N_("yok (jeepney kurulu değil)")}


def _watcher_mode(mode) -> str:
    m = str(mode)
    return _(_WATCHER_MODES[m]) if m in _WATCHER_MODES else m


def _verdict(dec: dict) -> str:
    """"Müzik → Müzik" in the UI language (the JSON keeps the category / profile ids)."""
    from .classifier import category_label, profile_label
    return (f"{category_label(dec.get('category', 'bilinmeyen'))} → "
            f"{profile_label(dec.get('profile'))}")


def format_block(d: dict, color: bool = False) -> str:
    st = Style(color)
    props = d.get("props", {})
    dec = d.get("decision", {})
    cat = dec.get("category", "bilinmeyen")
    lines = []
    head = (f"━━ #{d['index']}  {props.get('application.name', '?')}"
            f"  ({props.get('application.process.binary', '-')})"
            + ("  " + _("[duraklatılmış]") if d.get("corked") else ""))
    lines.append(st.bold(head) + st.dim("   " + _("çıkış: {cikis}").format(cikis=d.get("sink", "?"))))
    pairs = [(k.replace("application.process.", "process.").replace("application.", "app.")
              .replace("pipewire.access.", ""), props[k])
             for k in KEY_PROPS if k in props]
    if pairs:
        width = max(len(k) for k, _ in pairs)
        for i, (k, v) in enumerate(pairs):
            lines.append((_label(st, _("özellikler")) if i == 0 else " " * 15)
                         + f"{k:<{width}} = {_q(str(v))}")
    p = d.get("process")
    if p:
        nsp = d.get("process", {}).get("nspid") or []
        nstxt = f"  NSpid={'/'.join(map(str, nsp))}" if len(nsp) > 1 else ""
        lines.append(_label(st, _("süreç")) + f"pid {d.get('pid')} → {d.get('host_pid')}  "
                     f"comm={p['comm']}  exe={p['exe']}{nstxt}")
        lines.append(_label(st, _("komut")) + _q(p.get("cmdline", ""), 100))
        env = p.get("env") or {}
        lines.append(_label(st, _("ortam")) + (_q("  ".join(f"{k}={v}" for k, v in sorted(env.items())), 140)
                                            if env else "-"))
        anc = p.get("ancestors") or []
        lines.append(_label(st, _("atalar")) + (" ← ".join(f"{a['comm']}({a['pid']})" for a in anc[:8])
                                             + (" …" if len(anc) > 8 else "") if anc else "-"))
    else:
        lines.append(_label(st, _("süreç")) + _("pid {pid} → bulunamadı").format(pid=d.get("pid")))
    lines.append(_label(st, "flatpak") + (d.get("flatpak") or "-"))
    mp = d.get("mpris") or []
    if mp:
        for i, m in enumerate(mp):
            lines.append((_label(st, "MPRIS") if i == 0 else " " * 15)
                         + f"{m['bus_name'].removeprefix('org.mpris.MediaPlayer2.')} [{m['status']}] "
                         f"'{_q(m['title'], 50)}' — {_q(m['artist'], 30)}  {_q(m['url'], 60)}")
    else:
        lines.append(_label(st, "MPRIS") + "-")
    lines.append(_label(st, _("KARAR")) + st.c(_CAT_COLOR.get(cat, ""), _verdict(dec))
                 + f"   ({dec.get('reason', '')})")
    for i, t in enumerate(d.get("trace", [])):
        txt = t
        if t.startswith("✔"):
            txt = st.c("32", t)
        elif t.startswith("✘"):
            txt = st.dim(t)
        lines.append((_label(st, _("iz")) if i == 0 else " " * 15) + txt)
    return "\n".join(lines)


def format_line(d: dict, color: bool = False) -> str:
    """One-line re-decision (used in --izle on MPRIS changes)."""
    st = Style(color)
    dec = d["decision"]
    cat = dec["category"]
    return (f"↻ #{d['index']} {d['props'].get('application.name', '?')}: "
            + st.c(_CAT_COLOR.get(cat, ""), _verdict(dec))
            + f"  ({dec['reason']})")


# ---------------------------------------------------------------- main

def _sink_names(pulse) -> dict[int, str]:
    try:
        return {s.index: s.name for s in pulse.list_sinks()}
    except Exception:
        return {}


def _write_json(path: str, blocks: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"zaman": time.strftime("%Y-%m-%d %H:%M:%S"), "akislar": blocks},
                  f, ensure_ascii=False, indent=1)


def _is_browser_decision(d: dict) -> bool:
    # a flag, not the (translated) reason text (CHANGE 8 rule 5)
    return bool(d.get("medya_uygulamasi"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m oto_eq.teshis",
                                 description=_("Çalan ses akışlarını teşhis eder (salt okunur)."))
    ap.add_argument("--izle", action="store_true",
                    help=h_(_("yeni akışları ve MPRIS değişikliklerini Ctrl+C'ye kadar izle")))
    ap.add_argument("--json", metavar="DOSYA", help=h_(_("sonuçları JSON dosyasına da yaz")))
    ap.add_argument("--renk", choices=("oto", "evet", "hayir"), default="oto")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    color = sys.stdout.isatty() if args.renk == "oto" else args.renk == "evet"
    out = sys.stdout

    from . import pulse
    from .classifier import load_classifier
    from .mpris import MprisWatcher

    events: queue.Queue = queue.Queue()
    watcher = MprisWatcher(lambda: events.put(("mpris", -1)))
    watcher.start()
    time.sleep(0.3)   # let the first MPRIS sync land
    clf = load_classifier(online=False)     # read-only: genre cache only, no lookups
    sinks = _sink_names(pulse)
    blocks: list[dict] = []
    last: dict[int, dict] = {}

    def show(stream: Stream) -> dict:
        d = collect(stream, clf, watcher.players(), sinks)
        out.write(format_block(d, color) + "\n\n")
        out.flush()
        return d

    def own(s: Stream) -> bool:
        # our filter-chain playback nodes (oto_eq_*_out); the router ignores them too
        return s.node_name.startswith(SINK_PREFIX)

    try:
        streams = [s for s in pulse.list_streams() if not own(s)]
    except Exception as e:
        out.write(_("Akışlar okunamadı: {hata}").format(hata=e) + "\n")
        watcher.stop()
        return 1
    if not streams:
        out.write(_("Şu an çalan akış yok.") + "\n")
    for s in streams:
        d = show(s)
        blocks.append(d)
        last[s.index] = d
    out.write(_("{akis} akış, {oyuncu} MPRIS oyuncusu (izleyici: {izleyici}).").format(
        akis=len(streams), oyuncu=len(watcher.players()), izleyici=_watcher_mode(watcher.mode))
        + "\n")
    if args.json:
        _write_json(args.json, blocks)
    if not args.izle:
        watcher.stop()
        return 0

    out.write(_("İzleniyor… (Ctrl+C ile çık)") + "\n\n")
    out.flush()
    listener = pulse.EventListener(lambda ev: events.put((ev.facility, ev.index, ev.kind)))
    listener.start()
    stop = False

    def _sigint(*_a):
        nonlocal stop
        stop = True
    old = signal.signal(signal.SIGINT, _sigint)
    try:
        while not stop:
            try:
                item = events.get(timeout=0.5)
            except queue.Empty:
                continue
            if item[0] == "mpris":
                time.sleep(0.2)
                while not events.empty() and events.queue[0][0] == "mpris":
                    events.get_nowait()
                try:
                    current = {s.index: s for s in pulse.list_streams()}
                except Exception:
                    continue
                for idx, prev in list(last.items()):
                    s = current.get(idx)
                    if s is None or not _is_browser_decision(prev):
                        continue
                    d = collect(s, clf, watcher.players(), sinks)
                    if d["decision"] != prev["decision"]:
                        out.write(format_line(d, color) + "\n")
                        out.flush()
                    last[idx] = d
                continue
            facility, idx, kind = item
            if facility == "sink" and kind in ("new", "remove"):
                sinks = _sink_names(pulse)
            if facility != "sink-input":
                continue
            if kind == "remove":
                if last.pop(idx, None) is not None:
                    out.write(Style(color).dim("✕ " + _("#{no} kapandı").format(no=idx)) + "\n")
                    out.flush()
                continue
            if kind != "new" or idx in last:
                continue
            time.sleep(0.15)   # let props settle
            try:
                s = next((x for x in pulse.list_streams() if x.index == idx), None)
            except Exception:
                s = None
            if s is None or own(s):
                continue
            d = show(s)
            blocks.append(d)
            last[idx] = d
            if args.json:
                _write_json(args.json, blocks)
    finally:
        signal.signal(signal.SIGINT, old)
        listener.stop()
        watcher.stop()
        if args.json:
            _write_json(args.json, blocks)
    out.write("\n" + _("Çıkıldı.") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
