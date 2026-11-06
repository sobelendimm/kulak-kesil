"""LV2 / LADSPA plugin lookup in ONE place (eqgen, micgen, saglik; install.sh uses the same dirs).

PipeWire's filter-chain loads LV2 plugins through lilv (``LV2_PATH`` or lilv's built-in
path) and LADSPA plugins from ``LADSPA_PATH`` / the usual dirs. Our availability checks
must look where the distro installs them, or a feature is silently dropped although the
plugin is there (Debian/Ubuntu multiarch ``/usr/lib/x86_64-linux-gnu/lv2``, NixOS
profiles) — or, worse, a plugin is reported present that PipeWire cannot load.

    lv2_dirs() / ladspa_dirs()          search order (env var first)
    bul(rel, tur="lv2", exists=None)    first existing ``<dir>/<rel>`` or None
    lsp_eklentileri(exists=None)        {"comp", "lim", "ag"} ∩ installed LSP stereo plugins
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path

# LSP stereo plugins the EQ sinks use (key -> TTL inside the bundle); the mic uses the mono one
LSP_BUNDLE = "lsp-plugins.lv2"
LSP_TTL = {
    "comp": f"{LSP_BUNDLE}/compressor_stereo.ttl",
    "lim": f"{LSP_BUNDLE}/limiter_stereo.ttl",
    "ag": f"{LSP_BUNDLE}/autogain_stereo.ttl",
}
LSP_MONO_TTL = f"{LSP_BUNDLE}/compressor_mono.ttl"
RNNOISE_SO = "librnnoise_ladspa.so"

# Debian/Ubuntu multiarch triplets (install.sh checks x86_64 too); CPython knows its own
_TRIPLETS = ("x86_64-linux-gnu", "aarch64-linux-gnu", "i386-linux-gnu", "arm-linux-gnueabihf")


def _triplets() -> list[str]:
    own = getattr(sys.implementation, "_multiarch", "") or ""
    return list(dict.fromkeys(([own] if own else []) + list(_TRIPLETS)))


def _dirs(kind: str, env: str) -> list[str]:
    home = Path.home()
    user = os.environ.get("USER", "")
    out = [d for d in os.environ.get(env, "").split(":") if d]
    out += [str(home / f".{kind}"), f"/usr/local/lib/{kind}", f"/usr/lib/{kind}",
            f"/usr/lib64/{kind}"]
    out += [f"/usr/lib/{t}/{kind}" for t in _triplets()]
    # NixOS: per-user / system profiles (no FHS /usr/lib)
    out += [str(home / f".nix-profile/lib/{kind}"), f"/run/current-system/sw/lib/{kind}"]
    if user:
        out.append(f"/etc/profiles/per-user/{user}/lib/{kind}")
    return list(dict.fromkeys(out))


def lv2_dirs() -> list[str]:
    """LV2 search dirs: LV2_PATH first, then the usual system / multiarch / NixOS dirs."""
    return _dirs("lv2", "LV2_PATH")


def ladspa_dirs() -> list[str]:
    """LADSPA search dirs: LADSPA_PATH first, then the usual system / multiarch / NixOS dirs."""
    return _dirs("ladspa", "LADSPA_PATH")


def bul(rel: str, tur: str = "lv2", exists: Callable[[Path], bool] | None = None) -> Path | None:
    """First ``<dir>/<rel>`` that exists (``tur`` "lv2" or "ladspa"); ``exists`` is replaceable
    (saglik's fake environment)."""
    exists = exists or (lambda p: p.exists())
    for d in (lv2_dirs() if tur == "lv2" else ladspa_dirs()):
        p = Path(d).expanduser() / rel
        if exists(p):
            return p
    return None


def lsp_eklentileri(exists: Callable[[Path], bool] | None = None) -> frozenset[str]:
    """Which of the LSP stereo plugins the EQ graph uses are installed ("comp", "lim", "ag").

    Each one is checked on its own: an older LSP release may ship the compressor but not
    the autogain, and ONE missing LV2 plugin makes PipeWire reject the whole sinks conf."""
    return frozenset(k for k, rel in LSP_TTL.items() if bul(rel, "lv2", exists) is not None)
