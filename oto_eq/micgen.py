"""Microphone chain: mikrofon.toml -> filter-chain config for ``pipewire -c``.

CLI: ``python3 -m oto_eq.micgen generate|check|ozet|hedef|izle``

Graph (mono, one ``libpipewire-module-filter-chain``)::

    bas_filtre ─┬─ gurultu (RNNoise LADSPA) ── secim:In 1 ─┐
                └───────────────────────────── secim:In 2 ─┴─ secim (mixer)
      -> e1 .. eN (EQ) -> netlik -> kompresor (LSP compressor_mono) -> tepe (LSP) -> out

* ``secim`` is a builtin ``mixer``: Gain 1 = RNNoise path, Gain 2 = dry path.
  Switching RNNoise on/off (and the "discord" mode) is therefore LIVE
  (``set_live``), no restart, so apps capturing the mic are not disturbed.
* Every control is addressable by name on the capture node
  (``<prefix>_giris``), as with eqgen.
* Capture target: the HS80 mic present at generate time (``resolve_target``).
  No ``node.dont-fallback`` (see A2's NOTES-hs80-gecis d): a missing target
  falls back to the default source; ``node.linger`` keeps the node alive if
  nothing is linkable (e.g. the default source is our own virtual mic, which
  WirePlumber refuses: same link-group). ``retarget(source_name)`` moves the
  running capture to the given/current HS80 mic via the ``default`` metadata
  (``target.object`` = node name) without a restart and applies the per-mode
  input trim (``[giris]``); the router or ``micgen izle`` call it on mode
  changes.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

from . import config
from .eqgen import (FILTER_TYPES, _controls_str, _db2lin, _fmt, _live_params_from_dump,
                    _node_ids, _params_pod, comp_static_gain_db, dumps_toml, response_db)
from .i18n import N_, _, h_

log = logging.getLogger("oto_eq.micgen")

APP_NAME = "Kulak Kesil"
MIC_FILE = "mikrofon.toml"
MIC_CONF = config.CONFIG_DIR / "mikrofon.conf"
PREFIX = "oto_eq_mikrofon"            # virtual source name; capture = PREFIX + "_giris"
TEST_PREFIX = "oto_eq_mikrofon_test"
MISSING_TARGET = "oto_eq_mikrofon_hedef_yok"   # placeholder: linger, never fall back
DESCRIPTION = f"{APP_NAME} · Mikrofon"

RNNOISE_SO = "librnnoise_ladspa.so"
RNNOISE_LABEL = "noise_suppressor_mono"
LSP_MONO_URI = "http://lsp-plug.in/plugins/lv2/compressor_mono"
LSP_MONO_TTL = "lsp-plugins.lv2/compressor_mono.ttl"
DETECTION = {"tepe": 0, "rms": 1}
HPF_OFF_HZ = 5.0                      # "disabled" high-pass = far below audio band

GENEL_DEFAULTS = {"aktif": True, "mod": "tam",
                  "giris_desen": r"(?i)^alsa_input\.usb-Corsair_CORSAIR_HS80",
                  "giris_sabit": "", "orneklem_hz": 48000}
BAS_DEFAULTS = {"aktif": True, "fc": 90.0, "q": 0.707}
GURULTU_DEFAULTS = {"aktif": True, "esik": 60.0, "bekleme_ms": 250.0,
                    "geriye_donuk_ms": 0.0}
NETLIK_DEFAULTS = {"aktif": True, "fc": 3500.0, "kazanc": 2.5, "q": 0.9}
GIRIS_DEFAULTS = {"kablosuz_db": 0.0, "kablolu_db": 0.0}
WIRED_MARK = "USB_Gaming_Headset"     # wired HS80 (0a69); wireless = Wireless_Gaming_Receiver
COMP_DEFAULTS = {
    "kompresor": {"aktif": True, "esik": -26.0, "oran": 3.0, "atak_ms": 10.0,
                  "birakma_ms": 150.0, "diz_db": 6.0, "telafi": 6.0, "algilama": "rms",
                  "tepki_ms": 10.0},
    "tepe": {"aktif": True, "esik": -3.0, "oran": 20.0, "atak_ms": 0.5,
             "birakma_ms": 60.0, "diz_db": 0.0, "telafi": 0.0, "algilama": "tepe",
             "tepki_ms": 1.0},
}
SECTIONS = ("genel", "giris", "bas_filtre", "gurultu", "eq", "netlik", "kompresor", "tepe")
COMPS = ("kompresor", "tepe")          # section name == graph node name


class MicError(ValueError):
    """Invalid mikrofon.toml content (Turkish message)."""


# --------------------------------------------------------------------------
# Loading / validation
# --------------------------------------------------------------------------

def _num(v, where: str, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise MicError(_("{alan}: sayı bekleniyordu, '{deger}' bulundu").format(alan=where, deger=v))
    v = float(v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise MicError(_("{alan}: {deger:g} izin verilen aralığın dışında ({en_az:g}..{en_cok:g})").format(
            alan=where, deger=v, en_az=lo, en_cok=hi))
    return v


def _bool(v, where: str) -> bool:
    if not isinstance(v, bool):
        raise MicError(_("{alan}: true/false bekleniyordu, '{deger}' bulundu").format(alan=where, deger=v))
    return v


def _norm_comp(d: dict, where: str, defaults: dict) -> dict:
    if not isinstance(d, dict):
        raise MicError(_("{alan}: tablo bekleniyordu").format(alan=where))
    unknown = set(d) - set(defaults)
    if unknown:
        raise MicError(_("{alan}: bilinmeyen anahtar(lar): {anahtarlar}").format(
            alan=where, anahtarlar=", ".join(sorted(unknown))))
    out = {**defaults, **d}
    out["aktif"] = _bool(out["aktif"], f"{where}.aktif")
    out["esik"] = _num(out["esik"], f"{where}.esik", -60.0, 0.0)
    out["oran"] = _num(out["oran"], f"{where}.oran", 1.0, 100.0)
    out["atak_ms"] = _num(out["atak_ms"], f"{where}.atak_ms", 0.0, 2000.0)
    out["birakma_ms"] = _num(out["birakma_ms"], f"{where}.birakma_ms", 0.0, 5000.0)
    out["diz_db"] = _num(out["diz_db"], f"{where}.diz_db", 0.0, 24.0)
    out["telafi"] = _num(out["telafi"], f"{where}.telafi", -20.0, 40.0)
    out["tepki_ms"] = _num(out["tepki_ms"], f"{where}.tepki_ms", 0.0, 250.0)
    if out["algilama"] not in DETECTION:
        raise MicError(_("{alan}.algilama: \"tepe\" veya \"rms\" olmalı, '{deger}' bulundu").format(
            alan=where, deger=out["algilama"]))
    return out


def _norm_filter(f: dict, where: str, rate: float) -> dict:
    if not isinstance(f, dict):
        raise MicError(_("{alan}: {tablo} tablosu bekleniyordu").format(
            alan=where, tablo="{ tip, fc, kazanc, q }"))
    if f.get("tip") not in FILTER_TYPES:
        raise MicError(_("{alan}.tip: PK, LSC veya HSC olmalı, '{deger}' bulundu").format(
            alan=where, deger=f.get("tip")))
    return {"tip": f["tip"],
            "fc": _num(f.get("fc"), f"{where}.fc", 10.0, rate / 2 * 0.95),
            "kazanc": _num(f.get("kazanc"), f"{where}.kazanc", -24.0, 24.0),
            "q": _num(f.get("q"), f"{where}.q", 0.05, 20.0),
            "aktif": _bool(f.get("aktif", True), f"{where}.aktif")}


def normalize_mic(raw: dict) -> dict:
    """Validate and fill defaults; returns a new dict (raises MicError)."""
    if not isinstance(raw, dict):
        raise MicError(_("mikrofon ayarları bir tablo olmalı"))
    unknown = set(raw) - set(SECTIONS) - {"mod"}
    if unknown:
        raise MicError(_("bilinmeyen bölüm(ler): {bolumler}").format(bolumler=", ".join(sorted(unknown))))
    g = {**GENEL_DEFAULTS, **raw.get("genel", {})}
    g["aktif"] = _bool(g["aktif"], "genel.aktif")
    g["orneklem_hz"] = int(_num(g["orneklem_hz"], "genel.orneklem_hz", 8000, 192000))
    for k in ("mod", "giris_desen", "giris_sabit"):
        if not isinstance(g[k], str):
            raise MicError(_("{alan}: metin bekleniyordu").format(alan=f"genel.{k}"))
    try:
        re.compile(g["giris_desen"])
    except re.error as e:
        raise MicError(_("{alan} geçersiz düzenli ifade: {hata}").format(
            alan="genel.giris_desen", hata=e)) from e
    mods = raw.get("mod", {})
    if not isinstance(mods, dict):
        raise MicError(_("{alan} bir tablo olmalı").format(alan="[mod]"))
    if g["mod"] != "tam" and g["mod"] not in mods:
        if mods:
            raise MicError(_("genel.mod: '{mod}' tanımlı değil (\"tam\" veya {olanlar})").format(
                mod=g["mod"], olanlar=", ".join(repr(m) for m in mods)))
        raise MicError(_("genel.mod: '{mod}' tanımlı değil (\"tam\" veya hiç [mod.X] yok)").format(
            mod=g["mod"]))
    b = {**BAS_DEFAULTS, **raw.get("bas_filtre", {})}
    b["aktif"] = _bool(b["aktif"], "bas_filtre.aktif")
    b["fc"] = _num(b["fc"], "bas_filtre.fc", 10.0, 1000.0)
    b["q"] = _num(b["q"], "bas_filtre.q", 0.1, 5.0)
    n = {**GURULTU_DEFAULTS, **raw.get("gurultu", {})}
    n["aktif"] = _bool(n["aktif"], "gurultu.aktif")
    n["esik"] = _num(n["esik"], "gurultu.esik", 0.0, 99.0)
    n["bekleme_ms"] = _num(n["bekleme_ms"], "gurultu.bekleme_ms", 0.0, 2000.0)
    n["geriye_donuk_ms"] = _num(n["geriye_donuk_ms"], "gurultu.geriye_donuk_ms", 0.0, 200.0)
    for sec, dflt in (("bas_filtre", BAS_DEFAULTS), ("gurultu", GURULTU_DEFAULTS),
                      ("netlik", NETLIK_DEFAULTS), ("giris", GIRIS_DEFAULTS)):
        bad = set(raw.get(sec, {})) - set(dflt)
        if bad:
            raise MicError(_("{alan}: bilinmeyen anahtar(lar): {anahtarlar}").format(
                alan=sec, anahtarlar=", ".join(sorted(bad))))
    t = {**NETLIK_DEFAULTS, **raw.get("netlik", {})}
    t["aktif"] = _bool(t["aktif"], "netlik.aktif")
    t["fc"] = _num(t["fc"], "netlik.fc", 500.0, 12000.0)
    t["kazanc"] = _num(t["kazanc"], "netlik.kazanc", -12.0, 12.0)
    t["q"] = _num(t["q"], "netlik.q", 0.1, 10.0)
    gi = {**GIRIS_DEFAULTS, **raw.get("giris", {})}
    for k in GIRIS_DEFAULTS:
        gi[k] = _num(gi[k], f"giris.{k}", -30.0, 30.0)
    e = raw.get("eq", {})
    eq = {"aktif": _bool(e.get("aktif", True), "eq.aktif"),
          "filtreler": [_norm_filter(f, f"eq.filtreler[{i}]", g["orneklem_hz"])
                        for i, f in enumerate(e.get("filtreler", []), 1)]}
    out = {"genel": g, "giris": gi, "bas_filtre": b, "gurultu": n, "eq": eq, "netlik": t,
           "kompresor": _norm_comp(raw.get("kompresor", {}), "kompresor",
                                   COMP_DEFAULTS["kompresor"]),
           "tepe": _norm_comp(raw.get("tepe", {}), "tepe", COMP_DEFAULTS["tepe"]),
           "mod": copy.deepcopy(mods)}
    # validate every mode by applying it
    for name in mods:
        _apply_mode(out, name)
    return out


def _apply_mode(data: dict, name: str) -> dict:
    """Return the normalized data with ``[mod.<name>]`` overrides applied."""
    over = data["mod"].get(name)
    if over is None:
        raise MicError(_("mod '{mod}' tanımlı değil").format(mod=name))
    eff = copy.deepcopy({k: v for k, v in data.items() if k != "mod"})
    for sec, vals in over.items():
        if sec == "aciklama":
            continue
        if sec not in SECTIONS[1:] or not isinstance(vals, dict):
            raise MicError(_("{alan}: yalnızca {tablolar} tabloları değiştirilebilir").format(
                alan=f"mod.{name}.{sec}", tablolar="/".join(SECTIONS[1:])))
        if sec == "eq" and "filtreler" in vals:
            raise MicError(_("{alan}: filtre listesi modla değiştirilemez (yalnız aktif)").format(
                alan=f"mod.{name}.eq"))
        bad = set(vals) - set(eff[sec])
        if bad:
            raise MicError(_("{alan}: bilinmeyen anahtar(lar): {anahtarlar}").format(
                alan=f"mod.{name}.{sec}", anahtarlar=", ".join(sorted(bad))))
        eff[sec].update(vals)
    # re-validate the merged values
    chk = normalize_mic({k: v for k, v in eff.items()} | {"mod": {}, "genel": {**eff["genel"], "mod": "tam"}})
    chk["genel"]["mod"] = data["genel"]["mod"]
    return chk


def effective(data: dict) -> dict:
    """Values actually applied: selected mode merged, ``genel.aktif=false`` = bypass."""
    data = data if "mod" in data else normalize_mic(data)
    mode = data["genel"]["mod"]
    eff = _apply_mode(data, mode) if mode != "tam" else copy.deepcopy(
        {k: v for k, v in data.items() if k != "mod"})
    if not eff["genel"]["aktif"]:
        for sec in SECTIONS[2:]:
            eff[sec]["aktif"] = False
    return eff


def load_mic(path: Path | None = None) -> dict:
    """Read mikrofon.toml (user copy preferred), validate, fill defaults."""
    import tomllib
    p = Path(path) if path else config.config_path(MIC_FILE)
    with open(p, "rb") as f:
        return normalize_mic(tomllib.load(f))


SAVE_HEADER = """\
# Kulak Kesil (oto-eq) — mikrofon ayarları. Bu dosya uygulama tarafından kaydedildi.
# Ayrıntılı açıklamalı örnek: ~/.local/src/oto-eq/config/mikrofon.toml
#
# [genel]       aktif (false = düz geçiş), mod ("tam" | [mod.X] adı),
#               giris_desen (HS80 mikrofonu düzenli ifadesi), giris_sabit (elle kaynak adı)
# [bas_filtre]  aktif, fc, q
# [gurultu]     RNNoise: aktif, esik (VAD %), bekleme_ms, geriye_donuk_ms (gecikme!)
# [eq]          aktif, filtreler = [ { tip = "PK"|"LSC"|"HSC", fc, kazanc, q, aktif } ]
# [netlik]      konuşma netliği bandı: aktif, fc, kazanc (dB), q
# [kompresor] [tepe]  LSP kompresör: aktif, esik (dBFS), oran, atak_ms, birakma_ms,
#               diz_db, telafi (dB), algilama ("tepe"|"rms"), tepki_ms
# [mod.X]       hazır mod: yalnız yazılan değerleri değiştirir (ör. mod.discord.gurultu.aktif)
# EQ filtre SAYISI/TİPİ değişirse servis yeniden başlar; diğer her şey canlı uygulanır.
"""


def save_mic(data: dict, path: Path | None = None) -> Path:
    """Validate and write mikrofon.toml (previous file kept as .bak)."""
    import tomllib
    path = Path(path) if path else config.CONFIG_DIR / MIC_FILE
    norm = normalize_mic(data)
    text = dumps_toml(norm, SAVE_HEADER)
    if normalize_mic(tomllib.loads(text)) != norm:   # self-check round trip
        raise MicError(_("kaydetme doğrulaması başarısız (TOML gidiş-dönüş farklı)"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("Mikrofon ayarları kaydedildi: %s", path)
    return path


# --------------------------------------------------------------------------
# Plugins
# --------------------------------------------------------------------------

def _find_plugin(rel: str, env: str, dirs: list[str]) -> Path | None:
    cand = [d for d in os.environ.get(env, "").split(":") if d] + dirs
    for d in cand:
        p = Path(d) / rel
        if p.exists():
            return p
    return None


def rnnoise_path() -> Path | None:
    return _find_plugin(RNNOISE_SO, "LADSPA_PATH",
                        [str(Path.home() / ".ladspa"), "/usr/local/lib/ladspa", "/usr/lib/ladspa",
                         "/usr/lib64/ladspa"])


def lsp_mono_available() -> bool:
    return _find_plugin(LSP_MONO_TTL, "LV2_PATH",
                        [str(Path.home() / ".lv2"), "/usr/local/lib/lv2", "/usr/lib/lv2",
                         "/usr/lib64/lv2"]) is not None


# --------------------------------------------------------------------------
# Controls (shared by conf generation and set_live)
# --------------------------------------------------------------------------

def comp_mono_controls(d: dict) -> dict[str, float]:
    """LSP compressor_mono port values (symbols from its .ttl)."""
    return {
        "enabled": 1.0 if d["aktif"] else 0.0,
        "sct": 0.0,                       # feed-forward
        "scm": float(DETECTION[d["algilama"]]),
        "sla": 0.0,                       # no lookahead (no added latency)
        "scr": d["tepki_ms"],
        "scp": 1.0,
        "shpm": 0.0,
        "slpm": 0.0,
        "cm": 0.0,                        # downward
        "al": _db2lin(d["esik"]),
        "at": d["atak_ms"],
        "rt": d["birakma_ms"],
        "hold": 0.0,
        "cr": d["oran"],
        "kn": max(_db2lin(-d["diz_db"]), 0.0631),   # port minimum = -24 dB
        "mk": _db2lin(d["telafi"]),
        "cdr": 0.0,
        "cwt": 1.0,
        "g_in": 1.0,
        "g_out": 1.0,
    }


def is_wired(source_name: str | None) -> bool:
    return bool(source_name) and WIRED_MARK in source_name


def input_trim_db(data: dict, source_name: str | None) -> float:
    """Input trim ([giris], or the selected mode's [mod.X.giris]) for the captured source."""
    gi = effective(data)["giris"]
    return gi["kablolu_db"] if is_wired(source_name) else gi["kablosuz_db"]


def node_controls(data: dict, rnnoise: bool | None = None, lsp: bool | None = None,
                  target: str | None = None) -> dict[str, dict[str, float]]:
    """{graph node: {control: value}} for the effective settings.

    ``target`` = captured source name (selects the [giris] trim).
    """
    if rnnoise is None:
        rnnoise = rnnoise_path() is not None
    if lsp is None:
        lsp = lsp_mono_available()
    eff = effective(data)
    b, n = eff["bas_filtre"], eff["gurultu"]
    trim = input_trim_db(data, target) if eff["genel"]["aktif"] else 0.0
    nodes: dict[str, dict[str, float]] = {
        "giris": {"Mult": _db2lin(trim), "Add": 0.0},
        "bas_filtre": {"Freq": b["fc"] if b["aktif"] else HPF_OFF_HZ, "Q": b["q"]},
    }
    if rnnoise:
        nodes["gurultu"] = {"VAD Threshold (%)": n["esik"],
                            "VAD Grace Period (ms)": n["bekleme_ms"],
                            "Retroactive VAD Grace (ms)": n["geriye_donuk_ms"]}
        wet = 1.0 if n["aktif"] else 0.0
        nodes["secim"] = {"Gain 1": wet, "Gain 2": 1.0 - wet}
    eq_on = eff["eq"]["aktif"]
    for i, f in enumerate(eff["eq"]["filtreler"], 1):
        nodes[f"e{i}"] = {"Freq": f["fc"], "Q": f["q"],
                          "Gain": f["kazanc"] if (eq_on and f["aktif"]) else 0.0}
    t = eff["netlik"]
    nodes["netlik"] = {"Freq": t["fc"], "Q": t["q"], "Gain": t["kazanc"] if t["aktif"] else 0.0}
    if lsp:
        for c in COMPS:
            nodes[c] = comp_mono_controls(eff[c])
    return nodes


def _structure(data: dict, rnnoise: bool, lsp: bool) -> tuple:
    return (rnnoise, lsp, tuple(f["tip"] for f in normalize_mic(data)["eq"]["filtreler"]))


def needs_restart(old: dict, new: dict) -> bool:
    """True if ``new`` cannot be applied live (EQ band count/type changed)."""
    rn, lsp = rnnoise_path() is not None, lsp_mono_available()
    return _structure(old, rn, lsp) != _structure(new, rn, lsp)


# --------------------------------------------------------------------------
# Capture target (HS80 wireless / wired)
# --------------------------------------------------------------------------

def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=config.C_ENV, capture_output=True, text=True, timeout=timeout)


def list_source_names() -> list[str]:
    r = _run(["pactl", "list", "short", "sources"])
    return [ln.split("\t")[1] for ln in r.stdout.splitlines() if ln.count("\t") >= 1]


def _usb_device(node_name: str) -> str | None:
    """alsa_{input,output}.usb-XXX-00.profile → 'usb-XXX-00'."""
    m = re.match(r"^alsa_(?:input|output)\.(usb-.+?)\.[^.]+$", node_name)
    return m.group(1) if m else None


def resolve_target(data: dict, sources: list[str] | None = None,
                   default_sink: str | None = None) -> str | None:
    """Pick the HS80 mic to capture.

    Order: ``giris_sabit`` → candidate on the SAME USB device as the default
    sink (wired mode with the dongle still plugged in picks the cable) →
    wired ("USB_Gaming_Headset") → any other candidate. Monitors and our own
    ``oto_eq_*`` nodes are never candidates.
    """
    g = (data if "mod" in data else normalize_mic(data))["genel"]
    if g.get("giris_sabit"):
        return g["giris_sabit"]
    if sources is None:
        sources = list_source_names()
    pat = re.compile(g["giris_desen"])
    cands = [s for s in sources if pat.search(s) and not s.endswith(".monitor")
             and not s.startswith("oto_eq")]
    if not cands:
        return None
    if default_sink is None:
        try:
            default_sink = _run(["pactl", "get-default-sink"]).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            default_sink = ""
    dev = _usb_device(default_sink or "")
    if dev:
        same = [s for s in cands if _usb_device(s) == dev]
        if same:
            return same[0]
    wired = [s for s in cands if "USB_Gaming_Headset" in s]
    return (wired or cands)[0]


# --------------------------------------------------------------------------
# Config generation
# --------------------------------------------------------------------------

def _graph_lines(ctrls: dict, data: dict, rn_path: Path | None) -> list[str]:
    eff = effective(data)
    nodes, links = [], []
    nodes.append(f'{{ type = builtin name = giris label = linear '
                 f'control = {{ {_controls_str(ctrls["giris"])} }} }}')
    nodes.append(f'{{ type = builtin name = bas_filtre label = bq_highpass '
                 f'control = {{ {_controls_str(ctrls["bas_filtre"])} }} }}')
    links.append('{ output = "giris:Out" input = "bas_filtre:In" }')
    last = "bas_filtre:Out"
    if "gurultu" in ctrls:
        nodes.append(f'{{ type = ladspa name = gurultu plugin = {_fmt(str(rn_path))} '
                     f'label = {RNNOISE_LABEL} control = {{ {_controls_str(ctrls["gurultu"])} }} }}')
        nodes.append(f'{{ type = builtin name = secim label = mixer '
                     f'control = {{ {_controls_str(ctrls["secim"])} }} }}')
        links += ['{ output = "bas_filtre:Out" input = "gurultu:Input" }',
                  '{ output = "gurultu:Output" input = "secim:In 1" }',
                  '{ output = "bas_filtre:Out" input = "secim:In 2" }']
        last = "secim:Out"
    for i, f in enumerate(eff["eq"]["filtreler"], 1):
        n = f"e{i}"
        nodes.append(f'{{ type = builtin name = {n} label = {FILTER_TYPES[f["tip"]]} '
                     f'control = {{ {_controls_str(ctrls[n])} }} }}')
        links.append(f'{{ output = "{last}" input = "{n}:In" }}')
        last = f"{n}:Out"
    nodes.append(f'{{ type = builtin name = netlik label = bq_peaking '
                 f'control = {{ {_controls_str(ctrls["netlik"])} }} }}')
    links.append(f'{{ output = "{last}" input = "netlik:In" }}')
    last = "netlik:Out"
    for n in COMPS:
        if n in ctrls:
            nodes.append(f'{{ type = lv2 name = {n} plugin = "{LSP_MONO_URI}" '
                         f'control = {{ {_controls_str(ctrls[n])} }} }}')
            links.append(f'{{ output = "{last}" input = "{n}:in" }}')
            last = f"{n}:out"
    ind = " " * 20
    out = [" " * 16 + "nodes = ["] + [ind + x for x in nodes] + [" " * 16 + "]"]
    out += [" " * 16 + "links = ["] + [ind + x for x in links] + [" " * 16 + "]"]
    out += [" " * 16 + 'inputs  = [ "giris:In" ]',
            " " * 16 + f'outputs = [ "{last}" ]']
    return out


def generate_conf(data: dict, prefix: str = PREFIX, target: str | None = None,
                  extra_capture: dict | None = None, extra_source: dict | None = None,
                  description: str = DESCRIPTION) -> str:
    """Complete config text for ``pipewire -c <file>`` (the virtual mic)."""
    data = normalize_mic(data)
    rn = rnnoise_path()
    lsp = lsp_mono_available()
    if rn is None:
        log.warning("RNNoise LADSPA eklentisi (%s) bulunamadı: gürültü bastırmasız üretiliyor",
                    RNNOISE_SO)
    if not lsp:
        log.warning("LSP compressor_mono bulunamadı: kompresörsüz üretiliyor")
    ctrls = node_controls(data, rnnoise=rn is not None, lsp=lsp, target=target)
    eff = effective(data)
    tgt = target or MISSING_TARGET
    lv, tp = eff["kompresor"], eff["tepe"]
    L = [
        f"# {DESCRIPTION}: HS80 mikrofonu için işlenmiş sanal mikrofon — OTOMATİK ÜRETİLDİ.",
        "# Kaynak: mikrofon.toml  →  python3 -m oto_eq.micgen generate",
        "# Çalıştırma: pipewire -c <bu dosya>  (oto-eq-mic.service)",
        "#",
        f"# mod {data['genel']['mod']!r}, işlem {'açık' if data['genel']['aktif'] else 'KAPALI (düz)'};"
        f" RNNoise {'açık' if eff['gurultu']['aktif'] else 'kapalı'}"
        f"{'' if rn else ' (eklenti yok)'}",
        f"# kompresör {lv['oran']:g}:1 eşik {lv['esik']:g} dBFS telafi {lv['telafi']:+g} dB"
        f" ({'açık' if lv['aktif'] else 'kapalı'}); tepe {tp['esik']:g} dBFS {tp['oran']:g}:1"
        f" ({'açık' if tp['aktif'] else 'kapalı'})",
        f"# giriş: {' '.join(str(tgt).split())}" + ("  (HS80 mikrofonu bulunamadı)"
                                                     if target is None else "")
        + f"  giriş kazancı {input_trim_db(data, target):+g} dB",
        "# Hedef yoksa varsayılan mikrofona düşer; kablosuz/kablolu geçişte",
        "# micgen.retarget() (router veya `micgen izle`) yakalamayı yeniden başlatmadan taşır.",
        "",
        "context.properties = {",
        "    log.level = 2",
        "}",
        "",
        "context.spa-libs = {",
        "    audio.convert.* = audioconvert/libspa-audioconvert",
        "    support.*       = support/libspa-support",
        "}",
        "",
        "context.modules = [",
        "    { name = libpipewire-module-rt",
        "        args = { }",
        "        flags = [ ifexists nofail ]",
        "    }",
        "    { name = libpipewire-module-protocol-native }",
        "    { name = libpipewire-module-client-node }",
        "    { name = libpipewire-module-adapter }",
        "",
        "    { name = libpipewire-module-filter-chain",
        "        args = {",
        f"            node.description = {_fmt(description)}",
        f"            media.name       = {_fmt(description)}",
        f"            audio.rate       = {data['genel']['orneklem_hz']}",
        "            audio.channels   = 1",
        "            audio.position   = [ MONO ]",
        "            filter.graph = {",
    ]
    L += _graph_lines(ctrls, data, rn)
    cap = {
        "node.name": prefix + "_giris",
        "node.passive": True,
        "target.object": tgt,
        # no node.dont-fallback: a missing target falls back to the default
        # source; linger keeps the node alive when nothing is linkable
        # (default source = our own virtual mic is refused: same link-group)
        "node.linger": True,
        # WirePlumber must not remember targets/volumes for this stream
        "state.restore-target": False,
        "state.restore-props": False,
    } | (extra_capture or {})
    src = {
        "node.name": prefix,
        "node.description": description,
        "media.class": "Audio/Source",
        "audio.position": "[ MONO ]",
        "state.restore-props": False,
    } | (extra_source or {})

    def props(d: dict) -> list[str]:
        w = max(len(k) for k in d)
        return [f"                {k:<{w}} = {v if k == 'audio.position' else _fmt(v)}"
                for k, v in d.items()]
    L += ["            }", "            capture.props = {"] + props(cap)
    L += ["            }", "            playback.props = {"] + props(src)
    L += ["            }", "        }", "    }", "]", ""]
    return "\n".join(L)


def write_conf(path: Path = MIC_CONF, data: dict | None = None,
               target: str | None = "auto") -> Path:
    """Generate (target auto-resolved) and atomically write mikrofon.conf."""
    path = Path(path)
    data = data if data is not None else load_mic()
    if target == "auto":
        target = resolve_target(data)
    text = generate_conf(data, target=target)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    from .eqgen import keep_previous           # lazy: micgen must not need eqgen at import
    keep_previous(path)
    os.replace(tmp, path)
    log.info("Mikrofon yapılandırması yazıldı: %s (giriş: %s)", path, target or MISSING_TARGET)
    return path


# --------------------------------------------------------------------------
# Live control
# --------------------------------------------------------------------------

def _pw_dump() -> list[dict]:
    r = _run(["pw-dump"], timeout=10)
    return json.loads(r.stdout or "[]")


def set_live(data: dict, prefix: str = PREFIX, target: str | None = None) -> bool:
    """Write all controls of the effective settings to the RUNNING chain.

    Covers gains, frequencies, thresholds, VAD, RNNoise on/off, mode,
    ``genel.aktif`` and the [giris] trim (for ``target``, default: the source
    currently linked into the capture). Returns False if the chain is not
    running or its graph lacks a control (EQ band count/type changed →
    regenerate + restart).
    """
    data = normalize_mic(data)
    try:
        dump = _pw_dump()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.error("pw-dump başarısız: %s", e)
        return False
    nid = _node_ids(dump).get(prefix + "_giris")
    if nid is None:
        log.error("Mikrofon zinciri çalışmıyor (%s_giris yok)", prefix)
        return False
    live = _live_params_from_dump(dump, nid)
    if target is None:
        linked = _capture_state(dump, prefix)[1]
        target = linked[0] if linked else None
    ctrls = node_controls(data, target=target)
    missing = [f"{n}:{k}" for n, c in ctrls.items() for k in c if f"{n}:{k}" not in live]
    if missing:
        log.warning("Çalışan mikrofon grafiğinde %d denetim yok (ör. %s): yeniden üretip "
                    "servisi yeniden başlatmak gerekir", len(missing), missing[0])
        return False
    try:
        r = _run(["pw-cli", "set-param", str(nid), "Props", _params_pod(ctrls)])
    except (OSError, subprocess.SubprocessError) as e:
        log.error("pw-cli set-param başarısız: %s", e)
        return False
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        log.error("pw-cli set-param hatası: %s", (r.stderr or r.stdout).strip())
        return False
    log.info("Mikrofon ayarları canlı uygulandı")
    return True


def read_live(prefix: str = PREFIX) -> dict[str, float]:
    """Current control values of the running chain ({} if not running)."""
    dump = _pw_dump()
    nid = _node_ids(dump).get(prefix + "_giris")
    return _live_params_from_dump(dump, nid) if nid is not None else {}


def _capture_state(dump: list[dict], prefix: str) -> tuple[int | None, list[str]]:
    """(capture node id, names of nodes currently linked INTO the capture)."""
    names = {o["id"]: ((o.get("info") or {}).get("props") or {}).get("node.name")
             for o in dump if o.get("type") == "PipeWire:Interface:Node"}
    cid = next((i for i, n in names.items() if n == prefix + "_giris"), None)
    src = sorted({names.get((o.get("info") or {}).get("output-node-id"), "?")
                  for o in dump if o.get("type") == "PipeWire:Interface:Link"
                  and (o.get("info") or {}).get("input-node-id") == cid})
    return cid, src


def source_volume_db(name: str) -> float | None:
    """Current volume of a source in dB (pactl), None if unknown."""
    try:
        out = _run(["pactl", "get-source-volume", name]).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(-?[\d.]+|-inf) dB", out)
    if not m:
        return None
    return -math.inf if m.group(1) == "-inf" else float(m.group(1))


def retarget(source_name: str | None = None, prefix: str = PREFIX, wait: float = 2.0,
             data: dict | None = None) -> bool:
    """Point the RUNNING capture at ``source_name`` (default: the present HS80 mic).

    No restart: sets ``target.object`` = node NAME (survives replug) for the
    capture node in the ``default`` metadata and waits until WirePlumber has
    relinked; then applies the [giris] trim for that source. Never changes the
    default source. Returns True when linked to ``source_name``.
    API for the router (HS80 kablosuz ↔ kablolu takibi).
    """
    try:
        data = data if data is not None else load_mic()
    except (MicError, OSError) as e:
        log.warning("mikrofon.toml okunamadı (%s); varsayılanlar kullanılıyor", e)
        data = normalize_mic({})
    target = source_name or resolve_target(data)
    if target is None:
        log.info("HS80 mikrofonu bulunamadı; yakalama varsayılan mikrofonda/bekliyor")
        return False
    if target in (prefix, PREFIX, TEST_PREFIX):
        log.error("Mikrofon zinciri kendi çıkışına (%s) yönlendirilemez", target)
        return False
    try:
        dump = _pw_dump()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.error("pw-dump başarısız: %s", e)
        return False
    cid, linked = _capture_state(dump, prefix)
    if cid is None:
        log.warning("Mikrofon zinciri çalışmıyor (%s_giris yok)", prefix)
        return False
    if linked != [target]:
        r = _run(["pw-metadata", "-n", "default", str(cid), "target.object", target,
                  "Spa:String"])
        if r.returncode != 0:
            log.error("pw-metadata başarısız: %s", (r.stderr or r.stdout).strip())
            return False
        deadline = time.monotonic() + wait
        while _capture_state(_pw_dump(), prefix)[1] != [target]:
            if time.monotonic() > deadline:
                log.warning("Mikrofon girişi %s'e bağlanmadı (şu an: %s)", target,
                            linked or "-")
                return False
            time.sleep(0.1)
        log.info("Mikrofon girişi taşındı: %s → %s", linked or "-", target)
    set_live(data, prefix=prefix, target=target)
    vol = source_volume_db(target)
    if vol is not None and vol > 3.0 and abs(input_trim_db(data, target)) < 0.1:
        log.warning("%s mikrofonunun ses düzeyi %+.1f dB (yükseltilmiş); seviye diğer moddan "
                    "farklı olabilir: KDE'de %%100'e çekin ya da [giris] %s ile dengeleyin",
                    "Kablolu" if is_wired(target) else "Kablosuz", vol,
                    "kablolu_db" if is_wired(target) else "kablosuz_db")
    return True


def watch(stop: threading.Event | None = None, prefix: str = PREFIX) -> None:
    """Block: retarget on source add/remove and default-sink changes."""
    from . import pulse
    stop = stop or threading.Event()
    kick = threading.Event()

    def on_event(ev):
        if ev.facility in ("source", "server", "card") and ev.kind in ("new", "remove", "change"):
            if ev.facility == "source" and ev.kind == "change":
                return
            kick.set()

    lst = pulse.EventListener(on_event)
    lst.start()
    kick.set()
    try:
        while not stop.is_set():
            if kick.wait(1.0):
                time.sleep(0.5)        # debounce bursts (device appears with many events)
                kick.clear()
                try:
                    retarget(prefix=prefix)
                except Exception:      # noqa: BLE001 - keep watching
                    log.exception("yeniden hedefleme hatası")
    finally:
        lst.stop()


# --------------------------------------------------------------------------
# Live self-test (separate TEST instance; live chain untouched)
# --------------------------------------------------------------------------

TEST_PROPS = {"application.name": "oto-eq-mic-check",
              "state.restore-props": "false", "state.restore-target": "false"}


def _record(source: str, seconds: float) -> np.ndarray:
    """Capture float32 mono from a source into RAM only (parec → pipe)."""
    env = {**config.C_ENV, "PULSE_PROP": 'application.name="oto-eq-mic-check" '
           'state.restore-props="false" state.restore-target="false"'}
    p = subprocess.Popen(["parec", f"--device={source}", "--raw", "--format=float32le",
                          "--channels=1", "--rate=48000", "--latency-msec=20"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    buf = bytearray()
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            chunk = p.stdout.read1(65536) if hasattr(p.stdout, "read1") else p.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        p.terminate()
        try:
            p.wait(2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(2)
        p.stdout.close()
    x = np.frombuffer(bytes(buf[: len(buf) - len(buf) % 4]), dtype=np.float32).copy()
    buf[:] = b""
    return x


def _db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)) if x.size else -240.0


def _write_tone(path: Path, seconds: float, freq: float, dbfs: float) -> None:
    t = np.arange(int(48000 * seconds)) / 48000.0
    y = (_db2lin(dbfs) * math.sqrt(2) * np.sin(2 * math.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(y.tobytes())


def expected_tone_db(data: dict, freq: float, in_db: float) -> float:
    """Static model of the dry chain (no RNNoise) for a sine (RMS dBFS)."""
    eff = effective(data)
    fl = [f for f in eff["eq"]["filtreler"]] if eff["eq"]["aktif"] else []
    t = eff["netlik"]
    if t["aktif"]:
        fl.append({"tip": "PK", "fc": t["fc"], "kazanc": t["kazanc"], "q": t["q"], "aktif": True})
    lvl = in_db + float(response_db(fl, np.array([freq]))[0]) if fl else in_db
    for n in COMPS:
        d = eff[n]
        if d["aktif"] and lsp_mono_available():
            lvl += comp_static_gain_db({"esik_db": d["esik"], "oran": d["oran"],
                                        "diz_db": d["diz_db"], "telafi_db": d["telafi"]}, lvl)
    return lvl


def _pactl_ok(*args) -> str:
    r = _run(["pactl", *args])
    if r.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def check(say=print, real_mic: bool = True) -> bool:
    """Start TEST instances, verify links/targets/levels, tear everything down.

    1. synthetic: capture from a test null sink (tone, inaudible), dry vs
       RNNoise path switched LIVE, level vs static model, retarget between
       two null sinks, self-link refusal, missing-target linger.
    2. real mic (optional): resolved HS80 mic → 1.5 s into RAM → finite
       samples + level; nothing is written to disk.
    """
    data = load_mic()
    ok = True
    work = Path(tempfile.mkdtemp(prefix="oto-eq-mic-check-"))
    mods: list[str] = []
    procs: list[subprocess.Popen] = []
    nulls = [TEST_PREFIX + "_null1", TEST_PREFIX + "_null2"]
    default_src_before = _run(["pactl", "get-default-source"]).stdout.strip()

    def fail(msg):
        nonlocal ok
        ok = False
        say(_("HATA: {hata}").format(hata=msg))

    def start(conf_text: str, tag: str) -> subprocess.Popen:
        conf = work / f"mikrofon-{tag}.conf"
        conf.write_text(conf_text, encoding="utf-8")
        logf = open(work / f"pipewire-{tag}.log", "w")
        p = subprocess.Popen(["pipewire", "-c", str(conf)], env=config.C_ENV,
                             stdout=logf, stderr=subprocess.STDOUT)
        logf.close()
        procs.append(p)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 8:
            if p.poll() is not None:
                break
            if TEST_PREFIX in list_source_names():
                return p
            time.sleep(0.2)
        raise RuntimeError(_("test mikrofonu açılmadı; günlük: {yol}").format(
            yol=work / f"pipewire-{tag}.log"))

    def stop(p: subprocess.Popen) -> None:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(3)
        for _i in range(20):
            if TEST_PREFIX not in list_source_names():
                return
            time.sleep(0.2)

    def wait_linked(want: list[str] | None, t: float = 3.0) -> list[str]:
        deadline = time.monotonic() + t
        while True:
            got = _capture_state(_pw_dump(), TEST_PREFIX)[1]
            if (want is None and got) or got == want or time.monotonic() > deadline:
                return got
            time.sleep(0.1)

    # own description: WirePlumber keys filter-chain nodes by media.name, so a
    # test instance must never share the live chain's state entry
    test_desc = DESCRIPTION + " (test)"
    test_src = {"priority.session": 1, "priority.driver": 1}
    try:
        for n in nulls:
            mods.append(_pactl_ok("load-module", "module-null-sink", f"sink_name={n}",
                                  "sink_properties='device.description=oto-eq-mic-test "
                                  "state.restore-props=false'"))
        tone = work / "ton.wav"
        _write_tone(tone, 30.0, 1000.0, -20.0)
        player = subprocess.Popen(["pw-play", "--target", nulls[0], "-P", json.dumps(TEST_PROPS),
                                   str(tone)], env=config.C_ENV,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(player)

        # ---- 1. synthetic instance (captures a null sink monitor)
        dry = copy.deepcopy(data)
        dry["genel"]["mod"] = "tam"
        dry["genel"]["aktif"] = True
        dry["gurultu"]["aktif"] = False
        p1 = start(generate_conf(dry, prefix=TEST_PREFIX, target=nulls[0],
                                 extra_capture={"stream.capture.sink": True},
                                 extra_source=test_src, description=test_desc),
                   "sentetik")
        got = wait_linked([nulls[0]])
        if got != [nulls[0]]:
            fail(_("test yakalaması {hedef}'e bağlanmadı: {bagli}").format(hedef=nulls[0], bagli=got))
        else:
            say("✓ " + _("test zinciri açıldı ve bağlandı: {kaynak} → {hedef}").format(
                kaynak=f"{nulls[0]}.monitor", hedef=f"{TEST_PREFIX}_giris"))
        x = _record(TEST_PREFIX, 1.2)[4800:]
        exp = expected_tone_db(dry, 1000.0, -20.0)
        lvl = _db(x)
        if x.size < 20000 or not np.all(np.isfinite(x)):
            fail(_("geçersiz çıkış örnekleri (n={n})").format(n=x.size))
        elif abs(lvl - exp) > 2.0:
            fail(_("düz yol seviyesi {seviye:.1f} dBFS, model {model:.1f} dBFS").format(seviye=lvl, model=exp))
        else:
            say("✓ " + _("düz yol (RNNoise kapalı): 1 kHz −20 dBFS → {seviye:.1f} dBFS (model {model:.1f})")
                .format(seviye=lvl, model=exp))
        if rnnoise_path() is not None:
            wet = copy.deepcopy(dry)
            wet["gurultu"]["aktif"] = True
            if not set_live(wet, prefix=TEST_PREFIX):
                fail(_("set_live (RNNoise aç) başarısız"))
            time.sleep(0.4)
            v = read_live(TEST_PREFIX)
            y = _record(TEST_PREFIX, 1.2)[4800:]
            if v.get("secim:Gain 1") != 1.0 or not np.all(np.isfinite(y)):
                fail(_("RNNoise canlı açılamadı ({deger})").format(deger=v.get("secim:Gain 1")))
            else:
                say("✓ " + (_("RNNoise canlı açıldı: sürekli ton (konuşma değil) {once:.1f} → "
                              "{sonra:.1f} dBFS (bastırıldı)") if _db(y) < lvl - 10 else
                            _("RNNoise canlı açıldı: sürekli ton (konuşma değil) {once:.1f} → "
                              "{sonra:.1f} dBFS (BASTIRILMADI)")).format(once=lvl, sonra=_db(y)))
            set_live(dry, prefix=TEST_PREFIX)
        # retarget to the second null sink (by name, via metadata)
        if retarget(nulls[1], prefix=TEST_PREFIX, data=dry):
            say("✓ " + _("yeniden hedefleme canlı: {eski} → {yeni}").format(eski=nulls[0], yeni=nulls[1]))
        else:
            fail(_("yeniden hedefleme başarısız"))
        stop(p1)

        # ---- 2. self-link refusal + missing target (falls back to the default
        # source; links passively, nothing is recorded)
        p2 = start(generate_conf(dry, prefix=TEST_PREFIX, target=TEST_PREFIX,
                                 extra_source=test_src, description=test_desc), "kendine")
        time.sleep(1.0)
        got = _capture_state(_pw_dump(), TEST_PREFIX)
        if got[0] is None:
            fail(_("yakalama düğümü yok edildi"))
        elif TEST_PREFIX in got[1]:
            fail(_("zincir kendi sanal mikrofonuna bağlandı!"))
        else:
            say("✓ " + _("kendi sanal mikrofonuna bağlanmıyor (link-group); bağlı: {bagli}").format(
                bagli=got[1] or _("yok")))
        stop(p2)
        p3 = start(generate_conf(dry, prefix=TEST_PREFIX, target=None, extra_source=test_src,
                                 description=test_desc), "hedefsiz")
        time.sleep(1.0)
        got = _capture_state(_pw_dump(), TEST_PREFIX)
        if got[0] is None:
            fail(_("hedefsiz yakalama düğümü yok edildi"))
        elif got[1] not in ([default_src_before], []) or TEST_PREFIX in got[1]:
            fail(_("hedef yokken beklenmeyen kaynağa bağlandı: {bagli}").format(bagli=got[1]))
        else:
            say("✓ " + _("hedef ({hedef}) yokken varsayılan mikrofona düştü: {bagli}").format(
                hedef=MISSING_TARGET, bagli=got[1] or _("yok (bekliyor)")))
        # ---- 3. real HS80 mic (RAM only)
        if real_mic:
            tgt = resolve_target(data)
            if tgt is None:
                say("- " + _("gerçek mikrofon testi atlandı (HS80 mikrofonu yok)"))
            elif not retarget(tgt, prefix=TEST_PREFIX, data=dry):
                fail(_("gerçek mikrofona ({kaynak}) bağlanamadı").format(kaynak=tgt))
            else:
                z = _record(TEST_PREFIX, 1.5)[4800:]
                if z.size < 20000 or not np.all(np.isfinite(z)):
                    fail(_("gerçek mikrofondan geçersiz örnekler (n={n})").format(n=z.size))
                else:
                    say("✓ " + _("gerçek mikrofon: {kaynak} → {hedef} ({n} örnek, seviye "
                                 "{seviye:.1f} dBFS; kayıt tutulmadı)").format(
                        kaynak=tgt, hedef=TEST_PREFIX, n=z.size, seviye=_db(z)))
                z[:] = 0
        stop(p3)
        if _run(["pactl", "get-default-source"]).stdout.strip() != default_src_before:
            fail(_("varsayılan mikrofon değişti!"))
    except Exception as e:  # noqa: BLE001
        fail(str(e))
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    p.kill()
        for m in reversed(mods):
            _run(["pactl", "unload-module", m])
        time.sleep(0.5)
        left = [n for n in list_source_names() if n.startswith(TEST_PREFIX)]
        sinks = _run(["pactl", "list", "short", "sinks"]).stdout
        if left or TEST_PREFIX in sinks:
            ok = False
            say(_("HATA: {hata}").format(hata=_("test düğümleri kaldı: {dugumler}").format(dugumler=left)))
        else:
            say("✓ " + _("test süreçleri ve null sink'ler kaldırıldı"))
        if ok:
            shutil.rmtree(work, ignore_errors=True)
    say(_("SONUÇ: BAŞARILI") if ok else
        _("SONUÇ: BAŞARISIZ (günlükler: {yol})").format(yol=work))
    return ok


# --------------------------------------------------------------------------
# Level calibration (raw HS80 mic, RAM only)
# --------------------------------------------------------------------------

CAL_WIN_S = 0.020            # 20 ms RMS windows
CAL_TARGET_DBFS = -18.0      # processed speech RMS goal
CAL_CLIP_DBFS = -1.0


def analyze_levels(x: np.ndarray, rate: int = 48000) -> dict:
    """Noise floor, active-speech level and peak (dBFS) of a mono float signal.

    noise  = 10th percentile of 20 ms RMS windows;
    speech = power mean of windows ≥ noise + 10 dB (and ≥ -65 dBFS);
    ``aktif_oran`` = share of such windows.
    """
    x = np.asarray(x, dtype=np.float64)
    x = x[int(0.1 * rate):]                      # skip start-up transient
    win = int(CAL_WIN_S * rate)
    k = len(x) // win
    if k < 10:
        return {"konusma_dbfs": -240.0, "gurultu_dbfs": -240.0, "tepe_dbfs": -240.0,
                "aktif_oran": 0.0}
    pw = np.mean(x[: k * win].reshape(k, win) ** 2, axis=1) + 1e-24
    db = 10 * np.log10(pw)
    noise = float(np.percentile(db, 10))
    act = db >= max(noise + 10.0, -65.0)
    speech = float(10 * np.log10(np.mean(pw[act]))) if act.any() else -240.0
    peak = float(20 * np.log10(np.max(np.abs(x)) + 1e-12))
    return {"konusma_dbfs": round(speech, 1), "gurultu_dbfs": round(noise, 1),
            "tepe_dbfs": round(peak, 1), "aktif_oran": round(float(act.mean()), 3)}


def _mode_key_owner(data: dict, sec: str, key: str) -> dict:
    """Table that actually controls ``sec.key`` (mode override or base)."""
    mode = data["genel"]["mod"]
    over = data["mod"].get(mode, {}).get(sec, {}) if mode != "tam" else {}
    return over if key in over else data[sec]


def _with(data: dict, values: dict[str, float]) -> dict:
    d = copy.deepcopy(data)
    for dotted, v in values.items():
        sec, key = dotted.split(".")
        _mode_key_owner(d, sec, key)[key] = v
    return normalize_mic(d)


def predict_out_db(data: dict, in_db: float, source_name: str | None = None) -> float:
    """Static model: processed RMS for an input RMS (giris trim + EQ + compressors)."""
    return expected_tone_db(data, 1000.0, in_db + input_trim_db(data, source_name))


def suggest(levels: dict, data: dict, source_name: str | None = None) -> dict:
    """Derive compressor threshold/makeup and VAD threshold from measured levels."""
    sp, nz = levels["konusma_dbfs"], levels["gurultu_dbfs"]
    trim = input_trim_db(data, source_name)
    esik = float(min(-12.0, max(-40.0, round((sp + trim - 6.0) * 2) / 2)))
    base = _with(data, {"kompresor.esik": esik, "kompresor.telafi": 0.0})
    telafi = float(min(15.0, max(0.0, round((CAL_TARGET_DBFS - predict_out_db(
        base, sp, source_name)) * 2) / 2)))
    gap = sp - nz
    vad = 60.0 if gap >= 25 else 50.0 if gap >= 18 else 40.0 if gap >= 12 else 30.0
    oneri = {"kompresor.esik": esik, "kompresor.telafi": telafi, "gurultu.esik": vad}
    pred = predict_out_db(_with(data, oneri), sp, source_name)
    return {"oneri": oneri, "tahmini_cikis_dbfs": round(pred, 1), "fark_db": round(gap, 1)}


def calibrate(seconds: float = 5.0, apply: bool = False) -> dict:
    """Measure the RAW HS80 mic while the user speaks; suggest (and optionally apply) levels.

    Audio stays in RAM (parec → pipe) and is discarded; nothing is written to
    disk. ``apply=True`` → ``save_mic`` + ``set_live`` (no restart).
    """
    out = {"konusma_dbfs": -240.0, "gurultu_dbfs": -240.0, "tepe_dbfs": -240.0,
           "oneri": {}, "aciklama": "", "uygulandi": False}
    try:
        data = load_mic()
    except (MicError, OSError) as e:
        return out | {"hata": _("{dosya} okunamadı: {hata}").format(dosya="mikrofon.toml", hata=e),
                      "kod": "ayar"}
    src = resolve_target(data)
    if src is None:
        return out | {"hata": _("HS80 mikrofonu bulunamadı (kulaklık açık ve bağlı mı?)"),
                      "kod": "mikrofon_yok"}
    x = _record(src, float(seconds) + 0.1)
    if x.size < int(0.2 * float(seconds) * 48000):     # parec gave (almost) nothing
        return out | {"giris": src,
                      "hata": _("Kayıt alınamadı ({kaynak}): mikrofon bağlı ve açık mı?"
                                ).format(kaynak=src),
                      "kod": "kayit"}
    try:
        lv = analyze_levels(x)
    finally:
        x[:] = 0
        del x
    out.update({k: lv[k] for k in ("konusma_dbfs", "gurultu_dbfs", "tepe_dbfs")})
    out["giris"] = src
    if lv["tepe_dbfs"] > CAL_CLIP_DBFS:
        return out | {"hata": _("Mikrofon kırpılıyor (tepe {tepe:+.1f} dBFS). KDE'de mikrofon "
                                "seviyesini düşürüp tekrar deneyin.").format(tepe=lv["tepe_dbfs"]),
                      "kod": "kirpma"}
    if lv["aktif_oran"] < 0.10 or lv["konusma_dbfs"] < lv["gurultu_dbfs"] + 10:
        return out | {"hata": _("Konuşma algılanmadı. Mikrofon açık mı (kol aşağıda)? {saniye:g} "
                                "saniye boyunca normal sesinle konuşup tekrar deneyin.").format(
                          saniye=seconds),
                      "kod": "sessiz"}
    s = suggest(lv, data, src)
    out["oneri"] = s["oneri"]
    out["tahmini_cikis_dbfs"] = s["tahmini_cikis_dbfs"]
    o = s["oneri"]
    lines = [_("Konuşma {konusma:.1f} dBFS, ortam gürültüsü {gurultu:.1f} dBFS (fark {fark:.0f} dB)."
               ).format(konusma=lv["konusma_dbfs"], gurultu=lv["gurultu_dbfs"], fark=s["fark_db"]),
             _("Öneri: kompresör eşiği {esik:g} dBFS, telafi {telafi:+g} dB → işlenmiş konuşma ≈ "
               "{cikis:.1f} dBFS; VAD %{vad:g}.").format(
                 esik=o["kompresor.esik"], telafi=o["kompresor.telafi"],
                 cikis=s["tahmini_cikis_dbfs"], vad=o["gurultu.esik"])]
    if s["fark_db"] < 12:
        lines.append(_("Ortam gürültülü: RNNoise'u açık tutun, mikrofonu ağza yaklaştırın."))
    elif lv["konusma_dbfs"] < -45:
        lines.append(_("Konuşma çok kısık: KDE'de mikrofon seviyesini biraz artırın."))
    out["aciklama"] = "\n".join(lines[:3])
    if apply:
        new = _with(data, o)
        try:
            save_mic(new)
        except (MicError, OSError) as e:
            return out | {"hata": _("kaydedilemedi: {hata}").format(hata=e), "kod": "kaydetme"}
        try:          # like apply_suggestion: else the next oto-eq-mic start loads the old conf
            write_conf(MIC_CONF, new)
        except (MicError, OSError, subprocess.SubprocessError) as e:
            log.warning("mikrofon.conf yazılamadı: %s", e)
            return out | {"hata": _("{dosya} yazılamadı: {hata}").format(dosya="mikrofon.conf",
                                                                          hata=e),
                          "kod": "kaydetme"}
        out["uygulandi"] = set_live(new, target=src)
        if not out["uygulandi"]:
            out["aciklama"] += "\n" + _("Kaydedildi ama çalışan mikrofona canlı uygulanamadı.")
    return out


def apply_suggestion(oneri: dict, data: dict | None = None, conf_path: Path | None = None) -> dict:
    """Apply a calibration suggestion (``calibrate()['oneri']``) WITHOUT re-recording.

    Values go to the table that actually controls them (mode override or base,
    like ``calibrate(apply=True)``). Steps: ``save_mic`` → ``write_conf``
    (mikrofon.conf regenerated, so a later service start uses the new values and
    ``kulak saglik`` does not warn) → ``set_live`` on the running chain.
    Returns ``{"degisti", "kaydedildi", "conf", "canli", "yeniden_baslat"}``
    (+ ``"hata"``). ``yeniden_baslat`` = the caller should restart
    ``oto-eq-mic`` (chain not running or live update impossible); this function
    never restarts services itself.
    """
    out = {"degisti": False, "kaydedildi": False, "conf": False, "canli": False,
           "yeniden_baslat": False}
    if not isinstance(oneri, dict) or not oneri:
        return out | {"hata": _("uygulanacak öneri yok")}
    try:
        data = data if data is not None else load_mic()
        bad = [k for k in oneri if not isinstance(k, str) or k.count(".") != 1
               or k.split(".")[0] not in SECTIONS[1:]]
        if bad:
            raise MicError(_("geçersiz öneri anahtarı: {anahtar}").format(anahtar=", ".join(map(str, bad))))
        new = _with(data, oneri)
    except (MicError, OSError, KeyError, TypeError) as e:
        return out | {"hata": _("öneri uygulanamadı: {hata}").format(hata=e)}
    except Exception as e:  # noqa: BLE001 - tomllib.TOMLDecodeError
        return out | {"hata": _("{dosya} okunamadı: {hata}").format(dosya="mikrofon.toml", hata=e)}
    out["degisti"] = effective(new) != effective(data)
    if out["degisti"]:
        try:
            save_mic(new)
        except (MicError, OSError) as e:
            return out | {"hata": _("kaydedilemedi: {hata}").format(hata=e)}
        out["kaydedildi"] = True
    try:
        write_conf(conf_path or MIC_CONF, new)
        out["conf"] = True
    except (MicError, OSError, subprocess.SubprocessError) as e:
        log.warning("mikrofon.conf yazılamadı: %s", e)
        return out | {"hata": _("{dosya} yazılamadı: {hata}").format(dosya="mikrofon.conf", hata=e)}
    if out["degisti"]:
        out["canli"] = bool(set_live(new))
        out["yeniden_baslat"] = not out["canli"]
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def summary(data: dict) -> str:
    eff = effective(data)
    rn, lsp = rnnoise_path(), lsp_mono_available()
    tgt = resolve_target(data)
    g = data["genel"]
    vol = source_volume_db(tgt) if tgt else None
    if not tgt:
        giris = _("Giriş: {kaynak}").format(kaynak=_("HS80 mikrofonu bulunamadı"))
    elif vol is None:
        giris = _("Giriş: {kaynak}").format(kaynak=tgt)
    else:
        giris = (_("Giriş: {kaynak} (kablolu, kaynak sesi {ses:+.1f} dB, giriş kazancı {kazanc:+g} dB)")
                 if is_wired(tgt) else
                 _("Giriş: {kaynak} (kablosuz, kaynak sesi {ses:+.1f} dB, giriş kazancı {kazanc:+g} dB)")
                 ).format(kaynak=tgt, ses=vol, kazanc=input_trim_db(data, tgt))
    gu = eff["gurultu"]
    lines = [(_("Mod: {mod}   işlem: açık") if g["aktif"]
              else _("Mod: {mod}   işlem: KAPALI (düz geçiş)")).format(mod=g["mod"]),
             giris,
             _("Bas filtresi: {fc:g} Hz").format(fc=eff["bas_filtre"]["fc"]) if eff["bas_filtre"]["aktif"]
             else _("Bas filtresi: kapalı"),
             _("RNNoise: YOK (eklenti kurulu değil)") if rn is None else
             (_("RNNoise: açık (VAD %{vad:g}, bekleme {bekleme:g} ms)") if gu["aktif"] else
              _("RNNoise: kapalı (VAD %{vad:g}, bekleme {bekleme:g} ms)")).format(
                 vad=gu["esik"], bekleme=gu["bekleme_ms"])]
    eq = [f"{f['tip']} {f['fc']:g} Hz {f['kazanc']:+g} dB" for f in eff["eq"]["filtreler"]
          if eff["eq"]["aktif"] and f["aktif"]]
    if eff["netlik"]["aktif"]:
        eq.append(_("netlik {fc:g} Hz {kazanc:+g} dB").format(
            fc=eff["netlik"]["fc"], kazanc=eff["netlik"]["kazanc"]))
    lines.append(_("EQ: {filtreler}").format(filtreler=", ".join(eq)) if eq else _("EQ: kapalı"))
    comp_text = {   # (açık, kapalı) per stage; one whole line each
        "kompresor": (N_("Kompresör: açık {oran:g}:1 eşik {esik:g} dBFS, atak {atak:g} ms, bırakma "
                         "{birakma:g} ms, telafi {telafi:+g} dB"),
                      N_("Kompresör: kapalı {oran:g}:1 eşik {esik:g} dBFS, atak {atak:g} ms, bırakma "
                         "{birakma:g} ms, telafi {telafi:+g} dB"), N_("Kompresör: YOK (LSP yok)")),
        "tepe": (N_("Tepe: açık {oran:g}:1 eşik {esik:g} dBFS, atak {atak:g} ms, bırakma "
                    "{birakma:g} ms, telafi {telafi:+g} dB"),
                 N_("Tepe: kapalı {oran:g}:1 eşik {esik:g} dBFS, atak {atak:g} ms, bırakma "
                    "{birakma:g} ms, telafi {telafi:+g} dB"), N_("Tepe: YOK (LSP yok)")),
    }
    for n in ("kompresor", "tepe"):
        d = eff[n]
        acik, kapali, yok = comp_text[n]
        lines.append(_(yok) if not lsp else _(acik if d["aktif"] else kapali).format(
            oran=d["oran"], esik=d["esik"], atak=d["atak_ms"], birakma=d["birakma_ms"],
            telafi=d["telafi"]))
    rows = ["  " + _("{giris:+4.0f} dBFS giriş → {cikis:+6.1f} dBFS").format(
                giris=i, cikis=expected_tone_db(data, 1000.0, i))
            for i in (-50, -40, -30, -20, -10, -3)]
    lines.append(_("Statik eğri (1 kHz, RNNoise hariç):") + "\n" + "\n".join(rows))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m oto_eq.micgen",
                                 description=_("{ad} yapılandırıcı").format(ad=DESCRIPTION))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help=h_(_("{dosya} üret").format(dosya="mikrofon.conf")))
    g.add_argument("-o", "--output", type=Path, default=MIC_CONF)
    g.add_argument("--stdout", action="store_true", help=h_(_("dosyaya değil ekrana yaz")))
    c = sub.add_parser("check", help=h_(_("test kopyasıyla canlı doğrula (canlı mikrofona dokunmaz)")))
    c.add_argument("--gercek-mikrofon-yok", action="store_true",
                   help=h_(_("gerçek HS80 mikrofonuyla 1,5 sn'lik (yalnız bellek) testi atla")))
    sub.add_parser("ozet", help=h_(_("zincir özetini göster")))
    sub.add_parser("hedef", help=h_(_("çalışan zinciri şu anki HS80 mikrofonuna taşı")))
    sub.add_parser("izle", help=h_(_("kablosuz/kablolu geçişlerde girişi otomatik taşı (sürekli)")))
    sub.add_parser("canli", help=h_(_("{dosya}'u çalışan zincire canlı uygula").format(dosya="mikrofon.toml")))
    k = sub.add_parser("kalibre", help=h_(_("konuşarak seviye ölç, kompresör/VAD öner")))
    k.add_argument("--uygula", action="store_true", help=h_(_("öneriyi kaydet ve canlı uygula")))
    k.add_argument("--saniye", type=float, default=5.0)
    k.add_argument("--json", action="store_true",
                   help=h_(_("sonucu tek satır JSON yaz (pencere bunu kullanır)")))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        data = load_mic()
    except (MicError, OSError) as e:
        print(_("{dosya} okunamadı: {hata}").format(dosya="mikrofon.toml", hata=e), file=sys.stderr)
        return 2
    except Exception as e:  # tomllib.TOMLDecodeError
        print(_("{dosya} sözdizimi hatası: {hata}").format(dosya="mikrofon.toml", hata=e), file=sys.stderr)
        return 2
    if a.cmd == "generate":
        if a.stdout:
            print(generate_conf(data, target=resolve_target(data)))
        else:
            print(_("Yazıldı: {yol}").format(yol=write_conf(a.output, data)))
        return 0
    if a.cmd == "ozet":
        print(summary(data))
        return 0
    if a.cmd == "check":
        print(summary(data))
        return 0 if check(real_mic=not a.gercek_mikrofon_yok) else 1
    if a.cmd == "hedef":
        t = resolve_target(data)
        ok = retarget(t, data=data) if t else False
        print(_("Giriş: {kaynak}").format(kaynak=t) if ok
              else _("HS80 mikrofonu bulunamadı ya da zincir çalışmıyor"))
        return 0 if ok else 1
    if a.cmd == "canli":
        return 0 if set_live(data) else 1
    if a.cmd == "kalibre" and a.json:
        try:
            r = calibrate(a.saniye, apply=a.uygula)
        except Exception as e:  # noqa: BLE001 - report as JSON, never a traceback
            r = {"hata": _("ölçüm yapılamadı: {hata}").format(hata=e), "kod": "kayit"}
        print(json.dumps(r, ensure_ascii=False, default=float), flush=True)
        return 1 if r.get("hata") else 0
    if a.cmd == "kalibre":
        print(_("🎙  {saniye:g} saniye boyunca normal sesinle konuş…").format(saniye=a.saniye), flush=True)
        r = calibrate(a.saniye, apply=a.uygula)
        if "hata" in r:
            print(_("HATA: {hata}").format(hata=r["hata"]))
            return 1
        print(r["aciklama"])
        print(_("Uygulandı.") if r["uygulandi"] else
              (_("Uygulanamadı.") if a.uygula else _("Uygulamak için: {secenek}").format(
                  secenek="--uygula")))
        return 0
    if a.cmd == "izle":
        try:
            watch()
        except KeyboardInterrupt:
            pass
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
