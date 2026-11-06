"""EQ engine: profiller.toml -> filter-chain config for a separate pipewire process.

CLI: ``python3 -m oto_eq.eqgen generate|plot|check``

Graph per profile (one ``libpipewire-module-filter-chain`` each), explicit
stereo (L/R chains) so a linked stereo compressor can sit in the middle::

    [ag] -> pre_l -> d1_l .. dN_l -> b1_l .. bM_l [-> v1_l .. v4_l] -> k1_l .. k3_l
         [-> xd_l/xc_l/xt_r -> xm_l] -> comp(in_l/out_l) -> cikis_l -> [lim]
    (right channel the same; ag / comp / lim are stereo LV2 instances)

* ``pre_*``   builtin ``linear`` (Mult) = automatic anti-clip preamp
* ``d<i>_*``  HS80 correction bands (bq_peaking/bq_lowshelf/bq_highshelf)
* ``b<i>_*``  profile bands
* ``v<i>_*``  genre-variant slots (muzik/oyun only)
* ``k<i>_*``  "Kişisel ton" bands (bas/vokal/parlaklik, ``kisisel.TON_BANDS``) on
  every EQ sink; forced to 0 dB when the profile has ``kisisel_ton = false``
* ``comp``    LSP Compressor Stereo (LV2), optional, zero lookahead
* ``cikis_*`` builtin ``linear`` = loudness matching (attenuation only)
* ``ag``      LSP Autogain Stereo ([seviye], CHANGE 6): slow K-weighted loudness
  leveller at the very start, no lookahead (0 latency)
* ``lim``     LSP Limiter Stereo ([sinirlayici], CHANGE 6): brick-wall at
  genel.tavan_dbfs at the very end; its lookahead is the only added latency
  (1.5 ms default; cs2 has none). Lets the static headroom shrink by marj_db.
* ``xd/xc/xt/xm_*`` crossfeed ([profil.muzik.crossfeed]): direct low shelf,
  cross bq_lowpass + delay, mixer; off = shelf 0 dB + cross gain 0 (live)

Every control (Freq/Q/Gain/Mult/LSP ports) is addressable by name, so
``set_live`` can change values on the running sinks via ``pw-cli set-param``.
Disabled bands/correction/profiles are generated with Gain 0 (unity) so they
can be toggled live too; only band count/type changes need a restart
(``needs_restart``).

Biquad math = PipeWire filter-graph biquad.c (RBJ cookbook, cutoff
normalised to Nyquist, shelves use ``alpha = sin(w0) / (2 Q)``), identical to
AutoEq's PK/LSC/HSC definition.
"""
from __future__ import annotations

import argparse
import copy
import itertools
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

from . import config, i18n, kisisel, kulaklik
from .i18n import N_, _, h_, veri_metni
from .models import (ALL_SINK_PROFILES, EQ_PROFILES, FLAT_PROFILE, GROUP_LABELS, GROUP_NAMES,
                     PROFILE_LABELS, PROFILE_NAMES, SINK_PREFIX, VARIANT_PROFILES, variant_groups)

log = logging.getLogger("oto_eq.eqgen")

APP_NAME = "Kulak Kesil"
LINK_GROUP = "oto_eq"
LSP_COMP_URI = "http://lsp-plug.in/plugins/lv2/compressor_stereo"
LSP_COMP_TTL = "lsp-plugins.lv2/compressor_stereo.ttl"
# CHANGE 6 (D1): brick-wall limiter at the end, slow loudness leveller at the start
LSP_LIM_URI = "http://lsp-plug.in/plugins/lv2/limiter_stereo"
LSP_AG_URI = "http://lsp-plug.in/plugins/lv2/autogain_stereo"
# Generated-conf structure version (header marker); older conf -> conf_outdated()
# 7: state.restore-props = false on every sink node (WirePlumber kept a ducked -6 dB)
CONF_VERSION = 7
CONF_VERSION_MARK = "# oto-eq yapı sürümü:"

FILTER_TYPES = {"PK": "bq_peaking", "LSC": "bq_lowshelf", "HSC": "bq_highshelf"}

# Genre/game-type variants: 4 reserved bands v1..v4 on the muzik/oyun chains.
# Types are fixed (changing a biquad type needs a restart): (tip, fc, q) defaults.
VARIANT_SLOTS = 4
VARIANT_SLOT_DEFAULTS: tuple[tuple[str, float, float], ...] = (
    ("LSC", 100.0, 0.7), ("PK", 1000.0, 1.0), ("PK", 3000.0, 1.0), ("HSC", 8000.0, 0.7))
VARIANT_GAIN_LIMIT = 6.0
# Kişisel ton (CHANGE 5): bands k1..k3 (kisisel.TON_KEYS order) after the variant
# slots on EVERY EQ sink (also when kisisel_ton = false: then 0 dB), so toggling
# kisisel_ton or changing the tone never needs a restart.
TONE_SLOTS: tuple[tuple[str, float, float], ...] = tuple(kisisel.TON_BANDS[k]
                                                         for k in kisisel.TON_KEYS)
# worst-case search grid for the gain staging, as a fraction of TON_LIMIT per band
TONE_LEVELS = (-1.0, -0.5, 0.0, 0.5, 1.0)
# Upper limit (dB) of the tone loudness compensation's BOOST (tone cuts → output up).
# None = symmetric (fair blind A/B both ways; costs ~1.4 dB more global headroom
# than +TON_LIMIT alone), 0.0 = attenuation only (headroom cost ≈ TON_LIMIT).
TONE_COMP_MAX_BOOST_DB: float | None = None
RAMP_STEP_S = 0.03
DETECTION = {"tepe": 0, "rms": 1}

GENEL_DEFAULTS = {
    "pay_db": 1.0,
    "tavan_dbfs": -0.5,
    "ortak_link_grubu": True,
    "ses_esitleme": True,
    "esitleme_alt_hz": 100.0,
    "esitleme_ust_hz": 6000.0,
    "referans_seviye_db": -20.0,
    "orneklem_hz": 48000,
}
DUZ_DEFAULTS = {
    "aktif": True,
    # N_: defaults are saved into profiller.toml; they must not depend on the UI language
    "aciklama": N_("Düz geçiş: sesli sohbet, sistem sesleri, kapalı mod (seviye eşitli)"),
    "ek_kazanc_db": 0.0,
}
FLAT_ODAK = N_("Seviye eşitli düz geçiş")      # msgid: Turkish in the .conf, _() in the GUI/plot
DINAMIK_DEFAULTS = {
    "aktif": True,
    "esik_db": -24.0,
    "oran": 3.0,
    "atak_ms": 5.0,
    "birakma_ms": 100.0,
    "diz_db": 6.0,
    "telafi_db": 0.0,
    "algilama": "tepe",
    "tepki_ms": 10.0,
    "yan_zincir_hpf_hz": 0.0,
    "ileri_bakis_ms": 0.0,
}
# [profil.<ad>.sinirlayici] — LSP Limiter Stereo at the very end of the chain.
# Block present = node in the graph (structure); aktif = live.
SINIRLAYICI_DEFAULTS = {
    "aktif": True,
    "marj_db": 4.0,           # static worst case may exceed tavan by this much (limiter catches)
    "ileri_bakis_ms": 1.5,    # lookahead = ADDED LATENCY (measured: exactly this, 48 smp/ms)
    "birakma_ms": 8.0,
    # LSP "ALR" off: measured, it lowered music that never reached the ceiling by ~1.5 dB
    "seviye_duzenleme": False,
}
SINIRLAYICI_LIMITS = {"marj_db": (0.0, 12.0), "ileri_bakis_ms": (0.1, 20.0),
                      "birakma_ms": (0.25, 20.0)}
# [profil.<ad>.seviye] — LSP Autogain Stereo at the very start (slow loudness leveller)
SEVIYE_DEFAULTS = {
    "aktif": True,
    "hedef_lufs": None,               # None -> genel.referans_seviye_db (keeps cross-profile match)
    "en_cok_yukseltme_db": 6.0,
    "yukselme_db_sn": 1.0,
    "dusme_db_sn": 2.0,
    "olcum_ms": 2000.0,
    "sessizlik_lufs": -60.0,
    "ani_tepki_db": 9.0,
}
SEVIYE_LIMITS = {"hedef_lufs": (-40.0, -6.0), "en_cok_yukseltme_db": (0.0, 18.0),
                 "yukselme_db_sn": (0.1, 20.0), "dusme_db_sn": (0.1, 20.0),
                 "olcum_ms": (100.0, 2000.0), "sessizlik_lufs": (-84.0, -36.0),
                 "ani_tepki_db": (0.0, 24.0)}
# fast fall when a sudden loud part arrives (|short - long| > ani_tepki_db)
SEVIYE_ANI_DUSME_DB_SN = 30.0
# [profil.muzik.crossfeed] — bs2b-like, builtins only (bq_lowpass + delay + mixer)
CROSSFEED_PROFILES = ("muzik",)
CROSSFEED_DEFAULTS = {
    "aktif": False,
    "seviye_db": 4.5,      # bs2b "feed": low-frequency level difference direct vs. cross
    "kesim_hz": 700.0,     # cross path low-pass
    "gecikme_us": 0.0,     # extra interaural delay (the low-pass alone gives ~0.3 ms)
}
CROSSFEED_LIMITS = {"seviye_db": (1.0, 15.0), "kesim_hz": (300.0, 2000.0),
                    "gecikme_us": (0.0, 1000.0)}
CROSSFEED_MAX_DELAY_S = 0.002
CROSSFEED_SHELF_RATIO = 0.7   # direct-path low shelf at 0.7 x kesim_hz, Q 0.707 (flattest mono)

PLOT_FREQS = np.geomspace(20.0, 20000.0, 600)


class ProfileError(ValueError):
    """Invalid profiller.toml content (message is Turkish)."""


# --------------------------------------------------------------------------
# Loading / validation
# --------------------------------------------------------------------------

def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ProfileError(_("{alan}: sayı bekleniyordu, '{deger}' bulundu").format(alan=where, deger=repr(v)))
    if not math.isfinite(float(v)):
        raise ProfileError(_("{alan}: sonlu bir sayı olmalı").format(alan=where))
    return float(v)


def _norm_filter(f: dict, where: str, rate: float) -> dict:
    if not isinstance(f, dict):
        raise ProfileError(_("{alan}: filtre bir tablo olmalı {ornek}").format(
            alan=where, ornek="{ tip=..., fc=..., kazanc=..., q=... }"))
    tip = str(f.get("tip", "PK")).upper()
    if tip not in FILTER_TYPES:
        raise ProfileError(_("{alan}: bilinmeyen filtre tipi '{tip}' (PK, LSC, HSC olabilir)").format(
            alan=where, tip=tip))
    out = {
        "tip": tip,
        "fc": _num(f.get("fc"), f"{where}.fc"),
        "kazanc": _num(f.get("kazanc", 0.0), f"{where}.kazanc"),
        "q": _num(f.get("q", 0.7), f"{where}.q"),
        "aktif": bool(f.get("aktif", True)),
    }
    if not 1.0 <= out["fc"] < rate / 2:
        raise ProfileError(_("{alan}: fc {fc} Hz geçersiz (1 .. {en_cok:.0f} Hz)").format(
            alan=where, fc=out["fc"], en_cok=rate / 2))
    if not 0.05 <= out["q"] <= 50:
        raise ProfileError(_("{alan}: q {q} geçersiz (0.05 .. 50)").format(alan=where, q=out["q"]))
    if abs(out["kazanc"]) > 24:
        raise ProfileError(_("{alan}: kazanç ±24 dB'i aşamaz").format(alan=where))
    return out


def _norm_dinamik(d: dict, where: str) -> dict:
    if not isinstance(d, dict):
        raise ProfileError(_("{alan}: [dinamik] bir tablo olmalı").format(alan=where))
    out = dict(DINAMIK_DEFAULTS)
    for k, v in d.items():
        if k not in DINAMIK_DEFAULTS:
            raise ProfileError(_("{alan}: bilinmeyen anahtar '{anahtar}'").format(alan=where, anahtar=k))
        if k == "aktif":
            out[k] = bool(v)
        elif k == "algilama":
            if v not in DETECTION:
                raise ProfileError(_("{alan}.algilama: 'tepe' ya da 'rms' olmalı").format(alan=where))
            out[k] = v
        else:
            out[k] = _num(v, f"{where}.{k}")
    limits = {
        "esik_db": (-60.0, 0.0), "oran": (1.0, 100.0), "atak_ms": (0.0, 2000.0),
        "birakma_ms": (0.0, 5000.0), "diz_db": (0.0, 24.0), "telafi_db": (-30.0, 30.0),
        "tepki_ms": (0.0, 250.0), "yan_zincir_hpf_hz": (0.0, 20000.0),
        "ileri_bakis_ms": (0.0, 20.0),
    }
    for k, (lo, hi) in limits.items():
        if not lo <= out[k] <= hi:
            raise ProfileError(_("{alan} = {deger} aralık dışında ({en_az} .. {en_cok})").format(
                alan=f"{where}.{k}", deger=out[k], en_az=lo, en_cok=hi))
    return out


def _norm_block(d, where: str, defaults: dict, limits: dict, fixed: dict | None = None) -> dict:
    """Validate a simple key/value block (bools + bounded numbers); unknown keys rejected."""
    if not isinstance(d, dict):
        raise ProfileError(_("{alan}: bir tablo olmalı").format(alan=where))
    out = dict(defaults)
    out.update(fixed or {})
    for k, v in d.items():
        if k not in defaults:
            raise ProfileError(_("{alan}: bilinmeyen anahtar '{anahtar}' (olanlar: {olanlar})").format(
                alan=where, anahtar=k, olanlar=", ".join(defaults)))
        if isinstance(defaults[k], bool):
            if not isinstance(v, bool):
                raise ProfileError(_("{alan}: true ya da false olmalı").format(alan=f"{where}.{k}"))
            out[k] = v
        else:
            out[k] = _num(v, f"{where}.{k}")
    for k, (lo, hi) in limits.items():
        if out[k] is not None and not lo <= out[k] <= hi:
            raise ProfileError(_("{alan} = {deger:g} aralık dışında ({en_az:g} .. {en_cok:g})").format(
                alan=f"{where}.{k}", deger=out[k], en_az=lo, en_cok=hi))
    return out


def variant_slot_index(filters: list[dict]) -> list[int]:
    """Slot (0..3) for each variant filter: LSC→v1, PK→v2/v3, HSC→v4. Raises ValueError."""
    free = {"LSC": [0], "PK": [1, 2], "HSC": [3]}
    out = []
    for f in filters:
        slots = free.get(f["tip"])
        if not slots:
            raise ValueError(_("'{tip}' için boş tür yuvası yok (en çok 1 LSC, 2 PK, 1 HSC)").format(
                tip=f["tip"]))
        out.append(slots.pop(0))
    return out


def _norm_variants(name: str, raw: dict, where: str, rate: float) -> dict:
    if name not in VARIANT_PROFILES:
        raise ProfileError(_("{alan}: tür alt profilleri yalnız {olanlar} için").format(
            alan=where, olanlar=", ".join(VARIANT_PROFILES)))
    if not isinstance(raw, dict):
        raise ProfileError(_("{alan}: tablo olmalı").format(alan=where))
    allowed = variant_groups(name)
    out = {}
    for grp, v in raw.items():
        w = f"{where}.{grp}"
        if grp not in allowed:
            raise ProfileError(_("{alan}: bilinmeyen tür (olanlar: {olanlar})").format(
                alan=w, olanlar=", ".join(allowed)))
        if not isinstance(v, dict):
            raise ProfileError(_("{alan}: tablo olmalı").format(alan=w))
        t = {k: x for k, x in v.items() if k != "filtreler"}
        t.setdefault("aciklama", GROUP_NAMES.get(grp, grp))        # stored: language independent
        t.setdefault("odak", "")
        flt = [_norm_filter(f, f"{w}.filtreler[{i + 1}]", rate)
               for i, f in enumerate(v.get("filtreler") or [])]
        if len(flt) > VARIANT_SLOTS:
            raise ProfileError(_("{alan}: en çok {n} filtre").format(alan=w, n=VARIANT_SLOTS))
        try:
            variant_slot_index(flt)
        except ValueError as e:
            raise ProfileError(f"{w}: {e}") from None
        for i, f in enumerate(flt):
            if abs(f["kazanc"]) > VARIANT_GAIN_LIMIT:
                raise ProfileError(_("{alan}: tür farkı ±{db:g} dB'i aşamaz").format(
                    alan=f"{w}.filtreler[{i + 1}]", db=VARIANT_GAIN_LIMIT))
        t["filtreler"] = flt
        out[grp] = t
    return out


def normalize_profiles(raw: dict) -> dict:
    """Validate a parsed profiller.toml and fill defaults. Raises ProfileError."""
    genel = dict(GENEL_DEFAULTS)
    for k, v in (raw.get("genel") or {}).items():
        genel[k] = v
    for k in ("pay_db", "tavan_dbfs", "esitleme_alt_hz", "esitleme_ust_hz", "referans_seviye_db"):
        genel[k] = _num(genel[k], f"genel.{k}")
    genel["ses_esitleme"] = bool(genel["ses_esitleme"])
    genel["ortak_link_grubu"] = bool(genel["ortak_link_grubu"])
    genel["orneklem_hz"] = int(_num(genel["orneklem_hz"], "genel.orneklem_hz"))
    if genel["pay_db"] < 0:
        raise ProfileError(_("{alan} negatif olamaz").format(alan="genel.pay_db"))
    if not -12.0 <= genel["tavan_dbfs"] <= 0.0:
        raise ProfileError(_("{alan} -12 .. 0 arasında olmalı").format(alan="genel.tavan_dbfs"))
    if not 20 <= genel["esitleme_alt_hz"] < genel["esitleme_ust_hz"] <= 20000:
        raise ProfileError(_("{alan} geçersiz").format(alan="genel.esitleme_alt_hz/ust_hz"))
    rate = genel["orneklem_hz"]

    d_raw = raw.get("duzeltme") or {}
    corr = {k: v for k, v in d_raw.items() if k != "filtreler"}
    corr["aktif"] = bool(d_raw.get("aktif", True))
    corr["filtreler"] = [_norm_filter(f, f"duzeltme.filtreler[{i + 1}]", rate)
                        for i, f in enumerate(d_raw.get("filtreler") or [])]
    # CHANGE 7: the headphone model(s) the curve was measured for (other headphones: flat)
    corr["cihaz_desen"] = d_raw.get("cihaz_desen", kulaklik.DEFAULT_DUZELTME_DESEN)
    if not isinstance(corr["cihaz_desen"], str):
        raise ProfileError(_("{alan} metin olmalı").format(alan="duzeltme.cihaz_desen"))
    try:
        re.compile(corr["cihaz_desen"])
    except re.error as e:
        raise ProfileError(_("{alan} geçersiz: {hata}").format(alan="duzeltme.cihaz_desen", hata=e)) from None

    p_raw = raw.get("profil") or {}
    unknown = set(p_raw) - set(EQ_PROFILES)
    if unknown:
        raise ProfileError(_("bilinmeyen profil(ler): {profiller}").format(
            profiller=", ".join(sorted(unknown))))
    profil = {}
    for name in EQ_PROFILES:
        p = p_raw.get(name) or {}
        where = f"profil.{name}"
        q = {k: v for k, v in p.items()
             if k not in ("filtreler", "dinamik", "seviye", "sinirlayici", "crossfeed")}
        q.setdefault("aciklama", PROFILE_NAMES[name])              # stored: language independent
        q.setdefault("odak", "")
        q["aktif"] = bool(p.get("aktif", True))
        q["sifir_gecikme"] = bool(p.get("sifir_gecikme", name == "cs2"))
        # Kişisel ton opt-out: cs2 off by default (footstep tuning stays exact)
        q["kisisel_ton"] = bool(p.get("kisisel_ton", name != "cs2"))
        q["filtreler"] = [_norm_filter(f, f"{where}.filtreler[{i + 1}]", rate)
                          for i, f in enumerate(p.get("filtreler") or [])]
        q.pop("tur", None)
        if "tur" in p:
            q["tur"] = _norm_variants(name, p["tur"], f"{where}.tur", rate)
        if "dinamik" in p:
            q["dinamik"] = _norm_dinamik(p["dinamik"], f"{where}.dinamik")
            if q["sifir_gecikme"] and q["dinamik"]["ileri_bakis_ms"] > 0:
                log.warning("%s: sifir_gecikme açık → ileri_bakis_ms %.1f yerine 0 kullanılacak",
                            where, q["dinamik"]["ileri_bakis_ms"])
                q["dinamik"]["ileri_bakis_ms"] = 0.0
        if "sinirlayici" in p:
            q["sinirlayici"] = _norm_block(p["sinirlayici"], f"{where}.sinirlayici",
                                           SINIRLAYICI_DEFAULTS, SINIRLAYICI_LIMITS)
        if "seviye" in p:
            sv = _norm_block(p["seviye"], f"{where}.seviye", SEVIYE_DEFAULTS, SEVIYE_LIMITS)
            if sv["hedef_lufs"] is None:
                sv["hedef_lufs"] = genel["referans_seviye_db"]
            q["seviye"] = sv
        if "crossfeed" in p:
            if name not in CROSSFEED_PROFILES:
                raise ProfileError(_("{alan}: yalnız {olanlar} profilinde var").format(
                    alan=f"{where}.crossfeed", olanlar=", ".join(CROSSFEED_PROFILES)))
            q["crossfeed"] = _norm_block(p["crossfeed"], f"{where}.crossfeed",
                                         CROSSFEED_DEFAULTS, CROSSFEED_LIMITS)
        profil[name] = q

    f_raw = raw.get(FLAT_PROFILE) or {}
    flat = dict(DUZ_DEFAULTS)
    flat.update(f_raw)
    flat["aktif"] = bool(flat["aktif"])
    flat["aciklama"] = str(flat["aciklama"])
    flat["ek_kazanc_db"] = _num(flat["ek_kazanc_db"], f"{FLAT_PROFILE}.ek_kazanc_db")
    if not -20.0 <= flat["ek_kazanc_db"] <= 12.0:
        raise ProfileError(_("{alan} -20 .. +12 dB arasında olmalı").format(
            alan=f"{FLAT_PROFILE}.ek_kazanc_db"))

    out = {k: copy.deepcopy(v) for k, v in raw.items()
           if k not in ("genel", "duzeltme", "profil", FLAT_PROFILE)}
    out.update({"genel": genel, "duzeltme": corr, "profil": profil, FLAT_PROFILE: flat})
    return out


def load_profiles(path: Path | None = None) -> dict:
    """Read profiller.toml (user copy if present) and return a normalised dict.

    A user copy written before genre variants existed has no ``tur`` tables:
    those are filled from the shipped defaults.
    """
    import tomllib
    path = Path(path) if path else config.config_path(config.PROFILES_FILE)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    shipped = config.DEFAULTS_DIR / config.PROFILES_FILE
    rp = raw.get("profil") or {}
    missing = [p for p in VARIANT_PROFILES if "tur" not in (rp.get(p) or {})]
    # CHANGE 6 blocks: every (profile, block) missing in the user file is taken from
    # the shipped defaults; keys the user wrote win (to switch one off: aktif = false)
    new_blocks = [(p, b) for p in EQ_PROFILES for b in NEW_BLOCKS
                  if p in rp and b not in (rp.get(p) or {})]
    partial = [(p, b) for p in EQ_PROFILES for b in NEW_BLOCKS
               if isinstance((rp.get(p) or {}).get(b), dict)]
    if ((missing or new_blocks or partial) and path.resolve() != shipped.resolve()
            and shipped.exists()):
        with open(shipped, "rb") as f:
            defaults = tomllib.load(f)
        dp = defaults.get("profil") or {}
        for p in missing:
            tur = (dp.get(p) or {}).get("tur")
            if tur and p in rp:
                raw["profil"][p]["tur"] = copy.deepcopy(tur)
        for p, b in new_blocks:
            blk = (dp.get(p) or {}).get(b)
            if blk is not None:
                raw["profil"][p][b] = copy.deepcopy(blk)
        for p, b in partial:          # e.g. only "aktif = false" written: rest from shipped
            blk = (dp.get(p) or {}).get(b)
            if isinstance(blk, dict):
                raw["profil"][p][b] = copy.deepcopy(blk) | raw["profil"][p][b]
    return normalize_profiles(raw)


NEW_BLOCKS = ("sinirlayici", "seviye", "crossfeed")


# --------------------------------------------------------------------------
# Biquad math
# --------------------------------------------------------------------------

def biquad_coefs(tip: str, fc: float, gain_db: float, q: float, rate: float = 48000) -> tuple:
    """(b0, b1, b2, a0, a1, a2) exactly as PipeWire bq_* builtins compute them."""
    cutoff = min(max(fc / (rate / 2.0), 0.0), 1.0)
    A = 10.0 ** (gain_db / 40.0)
    if tip == "PK":
        if cutoff <= 0 or cutoff >= 1:
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        if q <= 0:
            return (A * A, 0.0, 0.0, 1.0, 0.0, 0.0)
        w0 = math.pi * cutoff
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        return (1 + alpha * A, -2 * k, 1 - alpha * A, 1 + alpha / A, -2 * k, 1 - alpha / A)
    if tip == "LSC":
        if cutoff >= 1:
            return (A * A, 0.0, 0.0, 1.0, 0.0, 0.0)
        if cutoff <= 0:
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        w0 = math.pi * cutoff
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        k2 = 2 * math.sqrt(A) * alpha
        ap, am = A + 1, A - 1
        return (A * (ap - am * k + k2), 2 * A * (am - ap * k), A * (ap - am * k - k2),
                ap + am * k + k2, -2 * (am + ap * k), ap + am * k - k2)
    if tip == "HSC":
        if cutoff >= 1:
            return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        if cutoff <= 0:
            return (A * A, 0.0, 0.0, 1.0, 0.0, 0.0)
        w0 = math.pi * cutoff
        alpha = math.sin(w0) / (2 * q)
        k = math.cos(w0)
        k2 = 2 * math.sqrt(A) * alpha
        ap, am = A + 1, A - 1
        return (A * (ap + am * k + k2), -2 * A * (am + ap * k), A * (ap + am * k - k2),
                ap - am * k + k2, 2 * (am - ap * k), ap - am * k - k2)
    raise ValueError(f"bilinmeyen filtre tipi {tip}")


def _filter_tuple(f) -> tuple[str, float, float, float, bool]:
    if isinstance(f, dict):
        return (f["tip"], float(f["fc"]), float(f["kazanc"]), float(f["q"]), bool(f.get("aktif", True)))
    tip, fc, g, q = f[:4]
    return (tip, float(fc), float(g), float(q), True)


def response_db(filters, freqs, rate: float = 48000) -> np.ndarray:
    """Magnitude response (dB) of a cascade of PK/LSC/HSC filters at ``freqs``."""
    freqs = np.asarray(freqs, dtype=float)
    total = np.zeros_like(freqs)
    w = 2 * np.pi * np.clip(freqs, 0, rate / 2) / rate
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    for f in filters:
        tip, fc, g, q, on = _filter_tuple(f)
        if not on or g == 0.0:
            continue
        b0, b1, b2, a0, a1, a2 = biquad_coefs(tip, fc, g, q, rate)
        h = (b0 + b1 * z1 + b2 * z2) / (a0 + a1 * z1 + a2 * z2)
        total += 20 * np.log10(np.maximum(np.abs(h), 1e-12))
    return total


# --------------------------------------------------------------------------
# Gain staging: preamp, dynamics, loudness matching
# --------------------------------------------------------------------------

def lsp_available() -> bool:
    """True if the LSP Compressor Stereo LV2 plugin is installed."""
    dirs = [d for d in os.environ.get("LV2_PATH", "").split(":") if d]
    dirs += [str(Path.home() / ".lv2"), "/usr/local/lib/lv2", "/usr/lib/lv2", "/usr/lib64/lv2"]
    return any((Path(d) / LSP_COMP_TTL).exists() for d in dirs)


def variant_filters(profiles: dict, name: str, group: str) -> list[dict]:
    """The variant's own filters (empty for "" / unknown group / non-variant profile)."""
    if not group or name not in VARIANT_PROFILES:
        return []
    v = (profiles["profil"][name].get("tur") or {}).get(group)
    return list(v["filtreler"]) if v else []


def variant_slots(profiles: dict, name: str, group: str) -> list[dict]:
    """Always 4 slot filters (v1..v4) for a group; unused slots are 0 dB defaults."""
    slots = [{"tip": t, "fc": fc, "kazanc": 0.0, "q": q, "aktif": True}
             for t, fc, q in VARIANT_SLOT_DEFAULTS]
    flt = variant_filters(profiles, name, group)
    for i, f in zip(variant_slot_index(flt), flt):
        slots[i] = dict(f)
    return slots


def defined_groups(profiles: dict, name: str) -> list[str]:
    if name not in VARIANT_PROFILES:
        return []
    return [g for g in variant_groups(name) if g in (profiles["profil"][name].get("tur") or {})]


def correction_on(profiles: dict) -> bool:
    """[duzeltme] audible: switched on AND fits the active headphone (CHANGE 7: the router
    records the device in STATE_DIR/cihaz.json; another model gets the curve flat)."""
    return bool(profiles["duzeltme"].get("aktif", True)) and kulaklik.duzeltme_acik()


def effective_filters(profiles: dict, name: str, group: str = "") -> list[dict]:
    """Correction + profile (+ variant) bands that are audible (respecting aktif flags)."""
    if name == FLAT_PROFILE:
        return []
    p = profiles["profil"][name]
    if not p.get("aktif", True):
        return []
    out = []
    if correction_on(profiles):
        out += [f for f in profiles["duzeltme"]["filtreler"] if f.get("aktif", True)]
    out += [f for f in p["filtreler"] if f.get("aktif", True)]
    out += [f for f in variant_filters(profiles, name, group) if f.get("aktif", True)]
    return out


# --------------------------------------------------------------------------
# Kişisel ton (k1..k3)
# --------------------------------------------------------------------------

def tone_enabled(profiles: dict, name: str) -> bool:
    """True if the profile's k-bands follow kisisel.json (EQ sink, aktif, kisisel_ton)."""
    if name not in EQ_PROFILES:
        return False
    p = profiles["profil"][name]
    return bool(p.get("aktif", True)) and bool(p.get("kisisel_ton", name != "cs2"))


def tone_filters(values: dict | None) -> list[dict]:
    """The three k-band filters (always 3, TON_KEYS order) for tone values in dB."""
    values = values or {}
    return [{"tip": t, "fc": fc, "kazanc": float(values.get(k, 0.0)), "q": q, "aktif": True}
            for k, (t, fc, q) in zip(kisisel.TON_KEYS, TONE_SLOTS)]


def tone_values(profiles: dict, name: str, group: str = "",
                values: dict | None = None) -> dict[str, float]:
    """Tone (dB per TON_KEYS) a sink should get.

    0 dB everywhere when the profile is excluded (``tone_enabled`` False).
    ``values`` None → kisisel.json effective(name, group); a (partial) dict →
    those values (clamped ±TON_LIMIT), missing keys from kisisel.json.
    """
    if not tone_enabled(profiles, name):
        return kisisel.zero()
    saved = kisisel.effective(kisisel.load(), name, group if name in VARIANT_PROFILES else "")
    if values is None:
        return saved
    bad = [k for k in values if k not in kisisel.TON_KEYS]
    if bad:
        raise ValueError(_("bilinmeyen ton ayarı: {ayar}").format(ayar=", ".join(bad)))
    return {k: kisisel.clamp(values[k]) if k in values else saved[k] for k in kisisel.TON_KEYS}


def _eq_band(profiles: dict) -> np.ndarray:
    g = profiles["genel"]
    return np.geomspace(g["esitleme_alt_hz"], g["esitleme_ust_hz"], 400)


def tone_comp_db(profiles: dict, name: str, group: str, tone: dict) -> float:
    """Output-gain correction that keeps the sink's loudness when the tone changes.

    Same pink-noise loudness as the profile matching (genel.esitleme_*); 0 when
    ses_esitleme is off or the tone is flat. Can be + (tone cuts) or − (boosts);
    gain_staging reserves headroom for the worst combination.
    """
    if not profiles["genel"]["ses_esitleme"] or not any(tone.get(k, 0.0)
                                                        for k in kisisel.TON_KEYS):
        return 0.0
    rate = profiles["genel"]["orneklem_hz"]
    lo_f = _eq_band(profiles)
    base = response_db(effective_filters(profiles, name, group), lo_f, rate)
    t = response_db(tone_filters(tone), lo_f, rate)
    return _cap_comp(_pink_loudness(base) - _pink_loudness(base + t))


def _cap_comp(c: float) -> float:
    return c if TONE_COMP_MAX_BOOST_DB is None else min(c, TONE_COMP_MAX_BOOST_DB)


_TONE_RESP: dict[tuple, np.ndarray] = {}


def _tone_band_resp(idx: int, gain: float, freqs: np.ndarray, rate: float) -> np.ndarray:
    key = (idx, gain, rate, float(freqs[0]), float(freqs[-1]), len(freqs))
    r = _TONE_RESP.get(key)
    if r is None:
        t, fc, q = TONE_SLOTS[idx]
        r = response_db([(t, fc, gain, q)], freqs, rate)
        _TONE_RESP[key] = r
    return r


def tone_worst_peak(profiles: dict, flt: list[dict]) -> tuple[float, dict[str, float]]:
    """Worst (peak + tone_comp) over the tone range for a curve; returns (dB, tone).

    Every band ranges ±TON_LIMIT; a 5-level grid per band (125 combinations,
    incl. all vertices) is searched on the peak grid, the winner refined with
    ``peak_db``. Without loudness compensation the worst case is simply every
    band at +TON_LIMIT (a band's response grows with its gain at every frequency).
    """
    g = profiles["genel"]
    rate = g["orneklem_hz"]
    lim = kisisel.TON_LIMIT
    comp_on = bool(g["ses_esitleme"])
    levels = [x * lim for x in TONE_LEVELS] if comp_on else [lim]
    r0 = response_db(flt, _PEAK_GRID, rate)
    lo_f = _eq_band(profiles)
    l0 = response_db(flt, lo_f, rate) if comp_on else None
    L0 = _pink_loudness(l0) if comp_on else 0.0
    best, best_tone = -math.inf, None
    for combo in itertools.product(levels, repeat=len(TONE_SLOTS)):
        arr = r0.copy()
        for i, gdb in enumerate(combo):
            if gdb:
                arr += _tone_band_resp(i, gdb, _PEAK_GRID, rate)
        val = float(arr.max())
        if comp_on and any(combo):
            la = l0.copy()
            for i, gdb in enumerate(combo):
                if gdb:
                    la += _tone_band_resp(i, gdb, lo_f, rate)
            val += _cap_comp(L0 - _pink_loudness(la))
        if val > best:
            best, best_tone = val, dict(zip(kisisel.TON_KEYS, combo))
    # refine the winner exactly (peak_db's local refinement); keep the larger value
    comp = 0.0
    fine = peak_db(list(flt) + tone_filters(best_tone), rate)
    if comp_on and any(best_tone.values()):
        la = response_db(list(flt) + tone_filters(best_tone), lo_f, rate)
        comp = _cap_comp(L0 - _pink_loudness(la))
    return max(best, fine + comp), best_tone


def _dyn_used(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    """Dynamics block that is present AND will be generated (plugin available)."""
    if name == FLAT_PROFILE:
        return None
    d = profiles["profil"][name].get("dinamik")
    if d is None:
        return None
    if lsp is None:
        lsp = lsp_available()
    return d if lsp else None


def _dyn_active(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    d = _dyn_used(profiles, name, lsp)
    if d is None or not d["aktif"] or not profiles["profil"][name].get("aktif", True):
        return None
    return d


def _block_used(profiles: dict, name: str, key: str, lsp: bool | None) -> dict | None:
    """An LSP-plugin block (sinirlayici / seviye) that is present AND will be generated."""
    if name not in EQ_PROFILES:
        return None
    b = profiles["profil"][name].get(key)
    if b is None:
        return None
    if lsp is None:
        lsp = lsp_available()
    return b if lsp else None


def _lim_used(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    return _block_used(profiles, name, "sinirlayici", lsp)


def _lim_active(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    """Active limiter (also on an aktif=false profile: it is pure safety)."""
    b = _lim_used(profiles, name, lsp)
    return b if b is not None and b["aktif"] else None


def _ag_used(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    return _block_used(profiles, name, "seviye", lsp)


def _ag_active(profiles: dict, name: str, lsp: bool | None) -> dict | None:
    b = _ag_used(profiles, name, lsp)
    if b is None or not b["aktif"] or not profiles["profil"][name].get("aktif", True):
        return None
    return b


def _xf_used(profiles: dict, name: str) -> dict | None:
    """Crossfeed block (builtins only, no plugin needed)."""
    if name not in CROSSFEED_PROFILES:
        return None
    return profiles["profil"][name].get("crossfeed")


def _xf_active(profiles: dict, name: str) -> dict | None:
    b = _xf_used(profiles, name)
    if b is None or not b["aktif"] or not profiles["profil"][name].get("aktif", True):
        return None
    return b


def crossfeed_gains(xf: dict) -> dict[str, float]:
    """Derived crossfeed values: direct/cross low-frequency gains (sum 1 = mono flat at LF)."""
    r = 10.0 ** (-xf["seviye_db"] / 20.0)
    cross = r / (1.0 + r)
    direct = 1.0 - cross
    return {"cross": cross, "direct_lf": direct, "shelf_db": 20.0 * math.log10(direct),
            "shelf_hz": xf["kesim_hz"] * CROSSFEED_SHELF_RATIO, "shelf_q": 0.707,
            "lp_hz": xf["kesim_hz"], "lp_q": 0.707, "delay_s": xf["gecikme_us"] * 1e-6}


def _biquad_h(coefs: tuple, freqs, rate: float) -> np.ndarray:
    b0, b1, b2, a0, a1, a2 = coefs
    w = 2 * np.pi * np.clip(np.asarray(freqs, dtype=float), 0, rate / 2) / rate
    z1 = np.exp(-1j * w)
    return (b0 + b1 * z1 + b2 * z1 * z1) / (a0 + a1 * z1 + a2 * z1 * z1)


def _lowpass_coefs(fc: float, q: float, rate: float) -> tuple:
    """RBJ low-pass = PipeWire bq_lowpass (verified in the test instance)."""
    w0 = 2 * math.pi * min(fc, rate / 2 * 0.999) / rate
    alpha = math.sin(w0) / (2 * q)
    c = math.cos(w0)
    b = (1 - c) / 2
    return (b, 2 * b, b, 1 + alpha, -2 * c, 1 - alpha)


def crossfeed_response(xf: dict, freqs, rate: float = 48000) -> tuple[np.ndarray, np.ndarray]:
    """Complex (direct, cross) responses of one output channel (cross = from the other side)."""
    cg = crossfeed_gains(xf)
    freqs = np.asarray(freqs, dtype=float)
    hd = _biquad_h(biquad_coefs("LSC", cg["shelf_hz"], cg["shelf_db"], cg["shelf_q"], rate),
                   freqs, rate)
    hc = (cg["cross"] * _biquad_h(_lowpass_coefs(cg["lp_hz"], cg["lp_q"], rate), freqs, rate)
          * np.exp(-2j * np.pi * freqs * cg["delay_s"]))
    return hd, hc


def crossfeed_worst_db(xf: dict, rate: float = 48000) -> float:
    """Largest possible gain of a crossfed channel (|direct| + |cross|, any L/R phase)."""
    hd, hc = crossfeed_response(xf, _PEAK_GRID, rate)
    return float(20 * np.log10(np.max(np.abs(hd) + np.abs(hc))))


def crossfeed_mono_db(xf: dict, freqs, rate: float = 48000) -> np.ndarray:
    """Response for centre (mono) content: direct + cross."""
    hd, hc = crossfeed_response(xf, freqs, rate)
    return 20 * np.log10(np.maximum(np.abs(hd + hc), 1e-12))


def comp_static_gain_db(d: dict, level_db: float) -> float:
    """Static gain (dB) of the compressor (soft knee + makeup) at an input level."""
    t, r, w = d["esik_db"], d["oran"], d["diz_db"]
    slope = 1.0 / r - 1.0
    x = level_db - t
    if w > 0 and abs(x) <= w / 2:
        gr = slope * (x + w / 2) ** 2 / (2 * w)
    elif x > 0:
        gr = slope * x
    else:
        gr = 0.0
    return gr + d["telafi_db"]


_PEAK_GRID = np.geomspace(20.0, 20000.0, 1200)


def peak_db(filters, rate: float = 48000) -> float:
    """Maximum of the response over 20 Hz-20 kHz (coarse grid + local refinement)."""
    r = response_db(filters, _PEAK_GRID, rate)
    i = int(np.argmax(r))
    lo, hi = _PEAK_GRID[max(i - 1, 0)], _PEAK_GRID[min(i + 1, len(_PEAK_GRID) - 1)]
    fine = response_db(filters, np.geomspace(lo, hi, 201), rate)
    return float(max(r[i], np.max(fine)))


def _pink_loudness(resp_db: np.ndarray) -> float:
    """Pink-noise (equal energy per octave) weighted mean power, in dB."""
    return float(10 * np.log10(np.mean(10 ** (resp_db / 10))))


def gain_staging(profiles: dict, lsp: bool | None = None) -> dict[str, dict]:
    """Per sink profile (4 EQ + duz): peak, preamp, match, loudness, worst case.

    1. preamp = -(max(peak, 0) + pay_db); for muzik/oyun the peak is the
       WORST of the neutral curve and every genre variant, so the shared
       preamp never has to change while variants ramp. For profiles with
       kişisel ton the peak is ``tone_peak_db``: the worst over every tone
       setting within ±TON_LIMIT (k1..k3) incl. its loudness compensation, so
       neither a tone slider nor a variant ever needs a preamp change.
    2. loudness = pink-weighted mean of EQ + preamp (+ compressor static gain
       at referans_seviye_db); computed per variant too. match = target -
       loudness (attenuation only); target = quietest of all profiles AND
       variants, so every variant stays matched via its own output gain.
    3. worst case for a full-scale transient: post-preamp EQ peak (tone incl.) + makeup
       (the compressor passes the first ms of a transient with full makeup)
       + match. If any profile/variant exceeds tavan_dbfs, ALL get the same
       extra attenuation, so the matching stays intact.
    4. duz (flat) = common matched loudness + ek_kazanc_db, capped at tavan.
    5. CHANGE 6: a sink with an active limiter ([sinirlayici]) may exceed tavan
       statically by marj_db: its preamp gives that much back (never > 0 dB) and
       the ceiling check uses tavan + marj_db (out_peak_dbfs stays <= tavan, the
       limiter's worst-case reduction = limiter_gr_db <= marj_db). Leveller
       ([seviye]) active: compressor reference = its hedef_lufs. Crossfeed active
       without a limiter: its worst gain is reserved (extra_peak_db).

    ``res[p]`` holds the neutral ("" = Genel) values, ``res[p]["variants"]``
    one entry per defined group (muzik/oyun only).
    """
    if lsp is None:
        lsp = lsp_available()
    g = profiles["genel"]
    rate = g["orneklem_hz"]
    ceil = g["tavan_dbfs"]
    lo_f = np.geomspace(g["esitleme_alt_hz"], g["esitleme_ust_hz"], 400)
    res: dict[str, dict] = {}
    units: list[dict] = []
    for name in EQ_PROFILES:
        d = _dyn_active(profiles, name, lsp)
        lim = _lim_active(profiles, name, lsp)
        margin = lim["marj_db"] if lim else 0.0
        ag = _ag_active(profiles, name, lsp)
        # with the leveller the content reaches the compressor at its target loudness
        ref_in = ag["hedef_lufs"] if ag else g["referans_seviye_db"]
        xf = _xf_active(profiles, name)
        # crossfeed's worst gain (|direct|+|cross|) is caught by the limiter when present
        xf_peak = max(crossfeed_worst_db(xf, rate), 0.0) if (xf and not lim) else 0.0
        # NOTE (hata avı 1 Kas, G3 F4, deliberately not counted): with [seviye] on and the
        # limiter switched OFF by hand, the leveller may boost a transient after a quiet
        # passage by up to en_cok_yukseltme_db; counting it would make every sink ~5 dB
        # quieter for that rare hand-made combination (test_margin_and_disabled_limiter)
        groups = [""] + defined_groups(profiles, name)
        tone_on = tone_enabled(profiles, name)
        per = {}
        for grp in groups:
            flt = effective_filters(profiles, name, grp)
            pk = peak_db(flt, rate) if flt else 0.0
            # tone_peak_db: worst EQ peak incl. any kişisel ton (±TON_LIMIT per band)
            # plus its loudness compensation -> preamp never changes when tone moves
            # (output side, used for the ceiling); tone_raw_peak_db: every band at
            # +TON_LIMIT without compensation = the largest EQ peak any tone can give
            # (each band's response grows with its gain) -> the anti-clip preamp
            tpk = tone_worst_peak(profiles, flt)[0] if tone_on else pk
            raw = (peak_db(list(flt) + tone_filters({k: kisisel.TON_LIMIT
                                                    for k in kisisel.TON_KEYS}), rate)
                   if tone_on else pk)
            per[grp] = {"flt": flt, "peak_db": pk, "tone_peak_db": max(tpk, pk),
                        "tone_raw_peak_db": raw}
        eq_on = tone_on or any(u["flt"] for u in per.values())
        # Without a limiter the preamp reserves the worst case incl. every kişisel ton
        # (all bands +TON_LIMIT). WITH a limiter (it guarantees <= tavan) only the curve
        # itself counts (worst variant, tone 0 dB) and marj_db more is given back: rare
        # tone / correction peaks of real content are caught by the limiter instead of
        # making everything quieter (CHANGE 6 fix: "ses az geliyor").
        if lim:
            worst_peak = max(u["peak_db"] for u in per.values())
        else:
            worst_peak = max(u["tone_raw_peak_db"] for u in per.values())
        pre = min(0.0, -(max(worst_peak, 0.0) + g["pay_db"]) + margin) if eq_on else 0.0
        for grp, u in per.items():
            loud = _pink_loudness(response_db(u["flt"], lo_f, rate)) + pre
            dyn = comp_static_gain_db(d, ref_in + loud) if d else 0.0
            u.update({"preamp_db": pre, "loudness_db": loud + dyn, "dyn_ref_gain_db": dyn,
                      "match_db": 0.0, "makeup_db": d["telafi_db"] if d else 0.0,
                      "safety_db": 0.0, "limiter_margin_db": margin, "limiter": bool(lim),
                      "extra_peak_db": xf_peak, "_owner": name, "_dyn": d})
            del u["flt"]
            units.append(u)
        res[name] = per[""]                    # neutral entry IS the main entry
        res[name]["variants"] = {grp: u for grp, u in per.items() if grp}

    if g["ses_esitleme"]:
        target = min(u["loudness_db"] for u in units)
        for u in units:
            u["match_db"] = min(0.0, target - u["loudness_db"])

    def worst(u):     # static worst case incl. any kişisel ton (before a limiter)
        return (u["tone_peak_db"] + u["preamp_db"] + u["makeup_db"] + u["match_db"]
                + u["extra_peak_db"])

    def checked(u):   # what the ceiling check uses
        if u["limiter"]:
            # limited: SUSTAINED full-scale sine at the curve's peak (tone 0 dB), after the
            # compressor settled; its attack overshoot and tone peaks = limiter's job
            lvl = u["peak_db"] + u["preamp_db"]
            dg = comp_static_gain_db(u["_dyn"], lvl) if u["_dyn"] else 0.0
            return lvl + dg + u["match_db"]
        return worst(u)

    def over(u):      # how far a unit's checked peak exceeds what its stage may pass
        return checked(u) - ceil - u["limiter_margin_db"]

    if g["ses_esitleme"]:
        extra = max(0.0, max(over(u) for u in units))
        for u in units:
            u["safety_db"] = -extra
            u["match_db"] -= extra
    else:                             # one extra per profile (neutral + its variants)
        for name in EQ_PROFILES:
            mine = [u for u in units if u["_owner"] == name]
            extra = max(0.0, max(over(u) for u in mine))
            for u in mine:
                u["safety_db"] = -extra
                u["match_db"] -= extra
    for u in units:
        # worst_peak_dbfs: static worst case BEFORE the limiter; out_peak_dbfs: what can
        # reach the headset (limiter: never above tavan); limiter_gr_db: its max reduction
        u["worst_peak_dbfs"] = worst(u)
        u["curve_peak_dbfs"] = checked(u)
        u["out_peak_dbfs"] = min(worst(u), ceil) if u["limiter"] else worst(u)
        u["limiter_gr_db"] = max(0.0, worst(u) - ceil) if u["limiter"] else 0.0
        u["final_loudness_db"] = u["loudness_db"] + u["match_db"]
        del u["_owner"], u["_dyn"]

    flat = profiles.get(FLAT_PROFILE) or DUZ_DEFAULTS
    common = min(u["final_loudness_db"] for u in units)
    if flat["aktif"]:
        want = common + flat["ek_kazanc_db"]
        gain = min(want, ceil)
        if want > ceil:
            log.warning("Düz çıkış kazancı %.1f dB tavanı (%.1f dBFS) aşıyor, %.1f dB'e "
                        "sınırlandı", want, ceil, gain)
    else:
        gain = 0.0
    res[FLAT_PROFILE] = {"peak_db": 0.0, "tone_peak_db": 0.0, "tone_raw_peak_db": 0.0,
                         "preamp_db": 0.0, "loudness_db": 0.0,
                         "dyn_ref_gain_db": 0.0, "match_db": gain, "makeup_db": 0.0,
                         "safety_db": 0.0, "worst_peak_dbfs": gain, "out_peak_dbfs": gain,
                         "curve_peak_dbfs": gain,
                         "limiter_margin_db": 0.0, "limiter": False, "limiter_gr_db": 0.0,
                         "extra_peak_db": 0.0, "final_loudness_db": gain, "variants": {}}
    return res


def staging_for(staging: dict, name: str, group: str = "") -> dict:
    """The staging entry of a profile's variant ("" or undefined group = neutral)."""
    st = staging[name]
    return st.get("variants", {}).get(group, st) if group else st


def profile_preamp(profiles: dict, name: str, lsp: bool | None = None) -> float:
    """Total static gain (dB) applied to a profile: anti-clip preamp + loudness match."""
    r = gain_staging(profiles, lsp)[name]
    return r["preamp_db"] + r["match_db"]


def total_response(profiles: dict, name: str, freqs, lsp: bool | None = None,
                   include_dynamics: bool = True, tone: dict | None = None) -> np.ndarray:
    """What reaches the headset (dB re input): correction + profile + preamp + match.

    With ``include_dynamics`` the compressor's static gain at the reference
    content level (genel.referans_seviye_db) is included, so matched profiles
    line up as they will sound at typical levels. ``tone`` (dB per TON_KEYS,
    e.g. ``tone_values(profiles, name)``) adds the k-bands + their loudness
    compensation; None = no kişisel ton.
    """
    r = gain_staging(profiles, lsp)[name]
    rate = profiles["genel"]["orneklem_hz"]
    flt = effective_filters(profiles, name)
    extra = r["preamp_db"] + r["match_db"] + (r["dyn_ref_gain_db"] if include_dynamics else 0.0)
    if tone and tone_enabled(profiles, name):
        flt = flt + tone_filters(tone)
        extra += tone_comp_db(profiles, name, "", tone)
    return response_db(flt, freqs, rate) + extra


# --------------------------------------------------------------------------
# Control values (shared by conf generation and set_live)
# --------------------------------------------------------------------------

def _db2lin(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _band_controls(f: dict, enabled: bool) -> dict[str, float]:
    return {"Freq": f["fc"], "Q": f["q"], "Gain": f["kazanc"] if (enabled and f["aktif"]) else 0.0}


def comp_controls(d: dict, profile_active: bool, zero_latency: bool) -> dict[str, float]:
    """LSP compressor_stereo port values (symbols from its .ttl)."""
    hpf = d["yan_zincir_hpf_hz"]
    return {
        "enabled": 1.0 if (d["aktif"] and profile_active) else 0.0,
        "ssplit": 0.0,            # linked stereo (keeps L/R balance -> direction cues)
        "sct": 0.0,               # feed-forward
        "scm": float(DETECTION[d["algilama"]]),
        "scs": 5.0,               # sidechain source = max(|L|,|R|)
        "sla": 0.0 if zero_latency else d["ileri_bakis_ms"],
        "scr": d["tepki_ms"],
        "scp": 1.0,
        "shpm": 1.0 if hpf > 0 else 0.0,
        "shpf": max(hpf, 10.0),
        "slpm": 0.0,
        "cm": 0.0,                # downward
        "al": _db2lin(d["esik_db"]),
        "at": d["atak_ms"],
        "rt": d["birakma_ms"],
        "hold": 0.0,
        "cr": d["oran"],
        "kn": _db2lin(-d["diz_db"]),
        "mk": _db2lin(d["telafi_db"]),
        "cdr": 0.0,
        "cwt": 1.0,
        "g_in": 1.0,
        "g_out": 1.0,
    }


def lim_controls(b: dict, ceil_dbfs: float) -> dict[str, float]:
    """LSP limiter_stereo port values: brick-wall at tavan, no makeup ("boost" off)."""
    lk = b["ileri_bakis_ms"]
    return {
        "enabled": 1.0 if b["aktif"] else 0.0,
        "mode": 0.0,              # Herm Thin (transparent, fast)
        "th": _db2lin(ceil_dbfs),
        "knee": 1.0,
        "boost": 0.0,             # IMPORTANT: default 1 would add 1/th makeup gain
        "lk": lk,
        "at": min(max(lk, 0.25), 20.0),
        "rt": b["birakma_ms"],
        "alr": 1.0 if b["seviye_duzenleme"] else 0.0,
        "ovs": 0.0,               # sample peak (digital clipping); no oversampling latency/CPU
        "dith": 0.0,
        "g_in": 1.0,
        "g_out": 1.0,
        "scp": 1.0,
    }


# LSP autogain gain-change "amount" enumeration (dB), port value = index
AG_AMOUNTS = (0.1, 0.5, 1.0, 3.0, 6.0, 9.0, 10.0, 12.0, 15.0, 18.0, 20.0, 21.0, 24.0)


def _ag_rate(rate_db_s: float, t_min: float, t_max: float, prefer: float = 1.0
             ) -> tuple[float, float]:
    """(amount index, time ms) for a gain-change speed in dB/s (LSP: amount per time)."""
    order = sorted(range(len(AG_AMOUNTS)), key=lambda i: abs(math.log(AG_AMOUNTS[i] / prefer)))
    for i in order:
        t = AG_AMOUNTS[i] / rate_db_s * 1000.0
        if t_min <= t <= t_max:
            return float(i), t
    i = AG_AMOUNTS.index(0.1)
    return float(i), min(max(AG_AMOUNTS[i] / rate_db_s * 1000.0, t_min), t_max)


def ag_controls(b: dict, active: bool) -> dict[str, float]:
    """LSP autogain_stereo port values (slow K-weighted loudness leveller, no lookahead)."""
    vgl, tgl = _ag_rate(b["yukselme_db_sn"], 10.0, 10000.0)
    vfl, tfl = _ag_rate(b["dusme_db_sn"], 10.0, 10000.0)
    vgs, tgs = _ag_rate(b["yukselme_db_sn"], 1.0, 500.0, prefer=0.1)   # no quick boost
    vfs, tfs = _ag_rate(SEVIYE_ANI_DUSME_DB_SN, 0.1, 40.0, prefer=0.5)
    return {
        "enabled": 1.0 if active else 0.0,
        "scmode": 0.0, "preamp": 0.0, "lkahead": 0.0,
        "weight": 5.0,            # K-weighting (BS.1770)
        "level": b["hedef_lufs"],
        "lperiod": b["olcum_ms"], "speriod": 100.0,
        "drift": b["ani_tepki_db"],
        "silence": b["sessizlik_lufs"],
        "max_on": 1.0, "max_amp": b["en_cok_yukseltme_db"],
        "qamp": 0.0,
        "vgrow_l": vgl, "tgrow_l": tgl, "vfall_l": vfl, "tfall_l": tfl,
        "vgrow_s": vgs, "tgrow_s": tgs, "vfall_s": vfs, "tfall_s": tfs,
    }


def xf_controls(b: dict, active: bool) -> dict[str, dict[str, float]]:
    """Crossfeed node values; off = shelf 0 dB + cross gain 0 (live switch, no restart)."""
    cg = crossfeed_gains(b)
    out = {}
    for ch in ("l", "r"):
        out[f"xd_{ch}"] = {"Freq": cg["shelf_hz"], "Q": cg["shelf_q"],
                           "Gain": cg["shelf_db"] if active else 0.0}
        out[f"xc_{ch}"] = {"Freq": cg["lp_hz"], "Q": cg["lp_q"]}
        out[f"xt_{ch}"] = {"Delay (s)": cg["delay_s"]}
        out[f"xm_{ch}"] = {"Gain 1": 1.0, "Gain 2": cg["cross"] if active else 0.0}
    return out


def node_controls(profiles: dict, name: str, staging: dict | None = None,
                  lsp: bool | None = None, group: str = "",
                  tone: dict | None = None) -> dict[str, dict[str, float]]:
    """{graph node name: {control: value}} for one profile (both channels).

    ``group`` selects the genre variant (muzik/oyun): its v1..v4 slot values and
    the variant's own output gain (cikis). "" = neutral (Genel).
    k1..k3 carry the kişisel ton (``tone_values(profiles, name, group, tone)``:
    None = kisisel.json) and cikis includes its loudness compensation.
    """
    if lsp is None:
        lsp = lsp_available()
    staging = staging or gain_staging(profiles, lsp)
    st = staging[name]
    if name == FLAT_PROFILE:
        return {f"cikis_{ch}": {"Mult": _db2lin(st["match_db"]), "Add": 0.0} for ch in ("l", "r")}
    p = profiles["profil"][name]
    p_on = p.get("aktif", True)
    d_on = p_on and correction_on(profiles)
    nodes: dict[str, dict[str, float]] = {}
    tv = tone_values(profiles, name, group, tone)
    out_db = staging_for(staging, name, group)["match_db"] + tone_comp_db(profiles, name, group, tv)
    for ch in ("l", "r"):
        nodes[f"pre_{ch}"] = {"Mult": _db2lin(st["preamp_db"]), "Add": 0.0}
        for i, f in enumerate(profiles["duzeltme"]["filtreler"], 1):
            nodes[f"d{i}_{ch}"] = _band_controls(f, d_on)
        for i, f in enumerate(p["filtreler"], 1):
            nodes[f"b{i}_{ch}"] = _band_controls(f, p_on)
        if name in VARIANT_PROFILES:
            for i, f in enumerate(variant_slots(profiles, name, group), 1):
                nodes[f"v{i}_{ch}"] = _band_controls(f, p_on)
        for i, f in enumerate(tone_filters(tv), 1):
            nodes[f"k{i}_{ch}"] = _band_controls(f, p_on)
        nodes[f"cikis_{ch}"] = {"Mult": _db2lin(out_db), "Add": 0.0}
    d = _dyn_used(profiles, name, lsp)
    if d is not None:
        nodes["comp"] = comp_controls(d, p_on, p.get("sifir_gecikme", False))
    xf = _xf_used(profiles, name)
    if xf is not None:
        nodes.update(xf_controls(xf, _xf_active(profiles, name) is not None))
    ag = _ag_used(profiles, name, lsp)
    if ag is not None:
        nodes["ag"] = ag_controls(ag, _ag_active(profiles, name, lsp) is not None)
    lim = _lim_used(profiles, name, lsp)
    if lim is not None:
        nodes["lim"] = lim_controls(lim, profiles["genel"]["tavan_dbfs"])
    return nodes


# --------------------------------------------------------------------------
# Config generation (SPA-JSON)
# --------------------------------------------------------------------------

def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        s = f"{float(v):.9g}"
        return s if any(c in s for c in ".en") else s + ".0"
    return json.dumps(str(v), ensure_ascii=False)


def _controls_str(ctrl: dict[str, float]) -> str:
    return " ".join(f'"{k}" = {_fmt(v)}' for k, v in ctrl.items())


def _graph_parts(profiles: dict, name: str, staging: dict, lsp: bool,
                 ctrls: dict | None = None) -> tuple[list[str], list[str], list[str], list[str]]:
    """(nodes, links, inputs, outputs) of one sink's filter graph (SPA-JSON snippets).

    Order: [ag] → pre → d* → b* → [v*] → k* → [crossfeed] → [comp] → cikis → [lim].
    """
    ctrls = ctrls if ctrls is not None else node_controls(profiles, name, staging, lsp)

    def builtin(n, label, extra=""):
        return (f'{{ type = builtin name = {n} label = {label}{extra} '
                f'control = {{ {_controls_str(ctrls[n])} }} }}')

    def link(a, b):
        return f'{{ output = "{a}" input = "{b}" }}'

    if name == FLAT_PROFILE:   # flat: one gain node per channel, nothing else
        return ([builtin(f"cikis_{ch}", "linear") for ch in ("l", "r")], [],
                ["cikis_l:In", "cikis_r:In"], ["cikis_l:Out", "cikis_r:Out"])
    p = profiles["profil"][name]
    nodes, links = [], []
    ends = {}
    for ch in ("l", "r"):
        chain = [f"pre_{ch}"]
        nodes.append(builtin(f"pre_{ch}", "linear"))
        vslots = ([{"tip": t} for t, _f, _q in VARIANT_SLOT_DEFAULTS]
                  if name in VARIANT_PROFILES else [])
        kslots = [{"tip": t} for t, _f, _q in TONE_SLOTS]
        for prefix, flist in (("d", profiles["duzeltme"]["filtreler"]), ("b", p["filtreler"]),
                              ("v", vslots), ("k", kslots)):
            for i, f in enumerate(flist, 1):
                n = f"{prefix}{i}_{ch}"
                nodes.append(builtin(n, FILTER_TYPES[f["tip"]]))
                chain.append(n)
        for a, b in zip(chain, chain[1:]):
            links.append(link(f"{a}:Out", f"{b}:In"))
        ends[ch] = f"{chain[-1]}:Out"
    if "xm_l" in ctrls:        # crossfeed: direct shelf + (other side: low-pass → delay) → mixer
        for ch in ("l", "r"):
            nodes += [builtin(f"xd_{ch}", "bq_lowshelf"), builtin(f"xc_{ch}", "bq_lowpass"),
                      builtin(f"xt_{ch}", "delay",
                              f' config = {{ "max-delay" = {_fmt(CROSSFEED_MAX_DELAY_S)} }}'),
                      builtin(f"xm_{ch}", "mixer")]
        for ch, other in (("l", "r"), ("r", "l")):
            links += [link(ends[ch], f"xd_{ch}:In"), link(ends[ch], f"xc_{ch}:In"),
                      link(f"xc_{ch}:Out", f"xt_{ch}:In"),
                      link(f"xd_{ch}:Out", f"xm_{ch}:In 1"),
                      link(f"xt_{other}:Out", f"xm_{ch}:In 2")]
        ends = {ch: f"xm_{ch}:Out" for ch in ("l", "r")}
    for ch in ("l", "r"):
        nodes.append(builtin(f"cikis_{ch}", "linear"))
        if "comp" in ctrls:
            links.append(link(ends[ch], f"comp:in_{ch}"))
            links.append(link(f"comp:out_{ch}", f"cikis_{ch}:In"))
        else:
            links.append(link(ends[ch], f"cikis_{ch}:In"))
    for key, uri in (("comp", LSP_COMP_URI), ("ag", LSP_AG_URI), ("lim", LSP_LIM_URI)):
        if key in ctrls:
            nodes.append(f'{{ type = lv2 name = {key} plugin = "{uri}" '
                         f'control = {{ {_controls_str(ctrls[key])} }} }}')
    inputs = ["pre_l:In", "pre_r:In"]
    if "ag" in ctrls:
        links += [link("ag:out_l", "pre_l:In"), link("ag:out_r", "pre_r:In")]
        inputs = ["ag:in_l", "ag:in_r"]
    outputs = ["cikis_l:Out", "cikis_r:Out"]
    if "lim" in ctrls:
        links += [link("cikis_l:Out", "lim:in_l"), link("cikis_r:Out", "lim:in_r")]
        outputs = ["lim:out_l", "lim:out_r"]
    return nodes, links, inputs, outputs


def _format_graph(nodes, links, inputs, outputs) -> list[str]:
    ind = " " * 20
    out = [" " * 16 + "nodes = ["] + [ind + n for n in nodes] + [" " * 16 + "]"]
    if links:
        out += [" " * 16 + "links = ["] + [ind + lk for lk in links] + [" " * 16 + "]"]
    out += [" " * 16 + "inputs = [ " + " ".join(f'"{x}"' for x in inputs) + " ]",
            " " * 16 + "outputs = [ " + " ".join(f'"{x}"' for x in outputs) + " ]"]
    return out


def _graph_lines(profiles: dict, name: str, staging: dict, lsp: bool) -> list[str]:
    return _format_graph(*_graph_parts(profiles, name, staging, lsp))


def generate_conf(profiles: dict, prefix: str = SINK_PREFIX, lsp: bool | None = None,
                  shared_link_group: bool | None = None, olcum: bool = False,
                  controls: dict | None = None) -> str:
    """Complete config text for ``pipewire -c <file>`` with the 4 EQ sinks + duz.

    ``olcum`` (TEST ONLY, used by ``check``): the *_out playback nodes get
    ``node.autoconnect = false`` so nothing reaches the headset; the test links
    them to its own recorder. ``controls`` {profile: node_controls} overrides
    the start values of some sinks (e.g. worst-case tone / fast leveller).
    """
    if lsp is None:
        lsp = lsp_available()
    for name in EQ_PROFILES:
        pr = profiles["profil"][name]
        for key, what in (("dinamik", "kompresörsüz"), ("seviye", "seviye eşitleyicisiz"),
                          ("sinirlayici", "sınırlayıcısız")):
            if pr.get(key) is not None and not lsp:
                log.warning("LSP eklentileri (lsp-plugins-lv2) bulunamadı: '%s' profili %s "
                            "üretiliyor", name, what)
    staging = gain_staging(profiles, lsp)
    link_group = prefix.rstrip("_") or LINK_GROUP
    if shared_link_group is None:
        shared_link_group = profiles["genel"].get("ortak_link_grubu", True)
    L = [
        f"# {APP_NAME} (oto-eq) sanal EQ çıkışları — OTOMATİK ÜRETİLDİ, elle düzenlemeyin.",
        "# Kaynak: profiller.toml  →  python3 -m oto_eq.eqgen generate",
        "# Çalıştırma: pipewire -c <bu dosya>  (oto-eq-sinks.service)",
        f"{CONF_VERSION_MARK} {CONF_VERSION}",
        "#",
    ]
    for name in ALL_SINK_PROFILES:
        s = staging[name]
        parts = []
        if name == FLAT_PROFILE:
            parts.append("düz")
        else:
            parts.append("kompresör" if _dyn_active(profiles, name, lsp) else "kompresör yok")
            if _ag_used(profiles, name, lsp):
                parts.append("seviye eşitleyici" + ("" if _ag_active(profiles, name, lsp)
                                                     else " (kapalı)"))
            if _lim_used(profiles, name, lsp):
                lb = _lim_used(profiles, name, lsp)
                parts.append(f"sınırlayıcı {lb['ileri_bakis_ms']:g} ms"
                             + ("" if lb["aktif"] else " (kapalı)"))
            if _xf_used(profiles, name):
                parts.append("crossfeed" + ("" if _xf_active(profiles, name) else " (kapalı)"))
        L.append(f"# {name:6s} tepe {s['peak_db']:+.2f} dB  ön kazanç {s['preamp_db']:+.2f} dB  "
                 f"eşitleme {s['match_db']:+.2f} dB  en kötü tepe {s['worst_peak_dbfs']:+.2f} dBFS"
                 f" → çıkış ≤ {s['out_peak_dbfs']:+.2f} dBFS  ({', '.join(parts)})")
    L.append(f"# kişisel ton: k1..k3 (bas / vokal / parlaklık, ±{kisisel.TON_LIMIT:g} dB, "
             f"kisisel.json) — açık profiller: "
             f"{', '.join(n for n in EQ_PROFILES if tone_enabled(profiles, n)) or 'yok'}; "
             "payı ön kazançta ayrıldı")
    L += [
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
    ]
    for name in ALL_SINK_PROFILES:
        p = profiles["profil"][name] if name != FLAT_PROFILE else {"odak": FLAT_ODAK}
        label = PROFILE_NAMES[name]       # not the UI language: the .conf stays the same
        desc = f"{APP_NAME} · {label}"
        sink = prefix + name
        L += [
            "",
            f"    # ---- {label}: {_yorum(p.get('odak', ''))}",
            "    { name = libpipewire-module-filter-chain",
            "        args = {",
            f"            node.description = {_fmt(desc)}",
            f"            media.name       = {_fmt(desc)}",
            # one shared link-group for all 8 nodes: WirePlumber never links a
            # node into a node of the same link-group (linking-utils canLink),
            # so an *_out playback can never feed another oto_eq_* sink.
            f"            node.link-group  = "
            f"{_fmt(link_group if shared_link_group else link_group + '_' + name)}",
            "            audio.channels   = 2",
            "            audio.position   = [ FL FR ]",
            "            filter.graph = {",
        ]
        if controls and name in controls:
            L += _format_graph(*_graph_parts(profiles, name, staging, lsp, controls[name]))
        else:
            L += _graph_lines(profiles, name, staging, lsp)
        L += [
            "            }",
            "            capture.props = {",
            f"                node.name        = {_fmt(sink)}",
            f"                node.description = {_fmt(desc)}",
            "                media.class      = Audio/Sink",
            "                audio.position   = [ FL FR ]",
            # lowest ranking -> WirePlumber never auto-picks it as default sink
            "                priority.session = 1",
            "                priority.driver  = 1",
            # unity-gain internal node: WirePlumber must not save/restore its volume
            # (it saved the kısma -6 dB and restored it at every start)
            "                state.restore-props = false",
            f"                oto_eq.profile   = {_fmt(name)}",
            "            }",
            "            playback.props = {",
            f"                node.name        = {_fmt(sink + '_out')}",
            f"                node.description = {_fmt(desc + ' (çıkış)')}",
            "                node.passive     = true",
            "                state.restore-props = false",
        ] + (["                node.autoconnect = false"] if olcum else []) + [
            "                audio.position   = [ FL FR ]",
            "                media.role       = \"DSP\"",
            f"                oto_eq.profile   = {_fmt(name)}",
            "            }",
            "        }",
            "    }",
        ]
    L += ["]", ""]
    return "\n".join(L)


def _yorum(text) -> str:
    """Free text (profiller.toml ``odak``…) inside a conf ``#`` comment: ONE line. A newline
    would end the comment and the rest would be parsed as conf syntax (PipeWire then rejects
    the whole file: no EQ sinks at all)."""
    return " ".join(str(text).split())


def keep_previous(path: Path) -> None:
    """Keep the conf being replaced as ``<name>.prev`` (a bad edit can be undone by hand)."""
    try:
        if path.is_file():
            shutil.copy2(path, path.with_name(path.name + ".prev"))
    except OSError as e:
        log.debug("önceki yapılandırma saklanamadı: %s", e)


def write_conf(path: Path = config.SINKS_CONF, profiles: dict | None = None) -> Path:
    """Generate and atomically write the sinks config; returns the path."""
    path = Path(path)
    profiles = profiles if profiles is not None else load_profiles()
    text = generate_conf(profiles)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    keep_previous(path)
    os.replace(tmp, path)
    log.info("EQ yapılandırması yazıldı: %s", path)
    return path


# --------------------------------------------------------------------------
# Saving profiller.toml (own TOML writer; tomllib is read-only)
# --------------------------------------------------------------------------

SAVE_HEADER = """\
# Kulak Kesil (oto-eq) — EQ profilleri. Bu dosya uygulama tarafından kaydedildi.
# Ayrıntılı açıklamalı örnek: ~/.local/src/oto-eq/config/profiller.toml
#
# [genel]      pay_db (kırpılma payı dB), tavan_dbfs (en kötü durum tepe tavanı),
#              ortak_link_grubu (true = güvenli bağlama, false = daha az CPU),
#              ses_esitleme (profiller arası seviye
#              eşitleme, yalnız kısar), esitleme_alt_hz/ust_hz (eşitleme bandı),
#              referans_seviye_db (kompresör hesabı için tipik içerik seviyesi),
#              orneklem_hz
# [duz]        düz çıkış (sohbet/sistem/kapalı mod): aktif (seviye eşitleme),
#              ek_kazanc_db (otomatik kazanca elle ekleme)
# [duzeltme]   HS80 AutoEq düzeltmesi, tüm profillerin tabanı. aktif, filtreler
# [profil.X]   X = cs2 | oyun | muzik | film. aciklama, odak, aktif (false = düz),
#              sifir_gecikme (true = lookahead zorla 0), filtreler,
#              kisisel_ton (true = kişisel ton bantları uygulanır; cs2 varsayılanı false)
# [profil.X.dinamik]  LSP kompresör: aktif, esik_db, oran, atak_ms, birakma_ms,
#              diz_db, telafi_db, algilama ("tepe"|"rms"), tepki_ms,
#              yan_zincir_hpf_hz (0 = kapalı), ileri_bakis_ms (gecikme ekler!)
# [profil.X.sinirlayici]  LSP sınırlayıcı (zincir sonu, tavan): aktif, marj_db,
#              ileri_bakis_ms (= eklenen gecikme), birakma_ms, seviye_duzenleme
# [profil.X.seviye]  LSP autogain (yavaş seviye eşitleyici): aktif, hedef_lufs,
#              en_cok_yukseltme_db, yukselme_db_sn, dusme_db_sn, olcum_ms,
#              sessizlik_lufs, ani_tepki_db
# [profil.muzik.crossfeed]  aktif (canlı), seviye_db, kesim_hz, gecikme_us
#              Ayrıntı: docs/NOTES-seviye.md
# Filtre: { tip = "PK"|"LSC"|"HSC", fc = Hz, kazanc = dB, q = genişlik, aktif }
# Filtre SAYISI/TİPİ veya [dinamik]/[sinirlayici]/[seviye]/[crossfeed] bloğu
# eklenir/silinirse servis yeniden başlar;
# diğer her şey canlı uygulanır.
"""

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v):
            return "nan"
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        if v and all(isinstance(x, dict) for x in v):
            return "[\n" + "".join(f"    {_toml_value(x)},\n" for x in v) + "]"
        return "[ " + ", ".join(_toml_value(x) for x in v) + " ]"
    raise TypeError(f"TOML'a yazılamayan tür: {type(v).__name__}")


def dumps_toml(data: dict, header: str = "") -> str:
    """Serialise nested dicts (tables) / scalars / arrays to TOML."""
    out = [header.rstrip("\n")] if header else []

    def emit(table: dict, path: list[str]):
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        subs = {k: v for k, v in table.items() if isinstance(v, dict)}
        if path and (scalars or not subs):
            out.append("")
            out.append("[" + ".".join(_toml_key(p) for p in path) + "]")
        elif not path and scalars:
            out.append("")
        for k, v in scalars.items():
            out.append(f"{_toml_key(k)} = {_toml_value(v)}")
        for k, v in subs.items():
            emit(v, path + [k])

    emit(data, [])
    return "\n".join(out).lstrip("\n") + "\n"


def save_profiles(profiles: dict, path: Path | None = None) -> Path:
    """Validate and write profiles to TOML (previous file kept as .bak)."""
    path = Path(path) if path else config.CONFIG_DIR / config.PROFILES_FILE
    normalize_profiles(profiles)  # validate before touching disk
    text = dumps_toml(profiles, SAVE_HEADER)
    import tomllib
    tomllib.loads(text)  # self-check
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    log.info("Profiller kaydedildi: %s", path)
    return path


# --------------------------------------------------------------------------
# Live updates
# --------------------------------------------------------------------------

def _structure(profiles: dict) -> tuple:
    def ftypes(fl):
        return tuple(f["tip"] for f in fl)
    prof = tuple((n, ftypes(profiles["profil"][n]["filtreler"]),
                  "dinamik" in profiles["profil"][n])
                 + tuple(b in profiles["profil"][n] for b in NEW_BLOCKS) for n in EQ_PROFILES)
    return (ftypes(profiles["duzeltme"]["filtreler"]), prof,
            profiles["genel"].get("ortak_link_grubu", True))


_DEFAULT_CONF = object()


def conf_version(txt: str) -> int:
    """Structure version written in a generated conf header (0 = before CHANGE 6)."""
    m = re.search(re.escape(CONF_VERSION_MARK) + r"\s*(\d+)", txt)
    return int(m.group(1)) if m else 0


def conf_outdated(path: Path | None = None) -> bool:
    """True if the sinks conf exists but predates the current graph structure.

    CHANGE 5: no k-bands; CHANGE 6: no version mark >= CONF_VERSION (no limiter /
    leveller / crossfeed nodes). Such a running graph lacks controls: one
    regeneration + sinks restart is needed. Missing/unreadable file → False.
    """
    p = Path(path) if path else config.SINKS_CONF
    try:
        txt = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    last = f"name = k{len(TONE_SLOTS)}_r label = {FILTER_TYPES[TONE_SLOTS[-1][0]]}"
    return txt.count(last) < len(EQ_PROFILES) or conf_version(txt) < CONF_VERSION


def needs_restart(old: dict, new: dict, conf_path=_DEFAULT_CONF) -> bool:
    """True if the change cannot be applied live (band count/type, dynamics /
    seviye / sinirlayici / crossfeed block added or removed, link group).

    Also True while the generated sinks conf (``conf_path``, default
    config.SINKS_CONF read at call time; None = skip the check) predates the
    current structure (k-bands, CONF_VERSION).
    """
    if _structure(normalize_profiles(old)) != _structure(normalize_profiles(new)):
        return True
    if conf_path is None:
        return False
    return conf_outdated(None if conf_path is _DEFAULT_CONF else conf_path)


def _run(cmd: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=config.C_ENV, capture_output=True, text=True, timeout=timeout)


def _pw_dump() -> list[dict]:
    r = _run(["pw-dump"], timeout=10)
    return json.loads(r.stdout or "[]")


def _node_ids(dump: list[dict]) -> dict[str, int]:
    ids = {}
    for o in dump:
        if o.get("type") == "PipeWire:Interface:Node":
            nm = ((o.get("info") or {}).get("props") or {}).get("node.name")
            if nm:
                ids[nm] = o["id"]
    return ids


def _params_pod(ctrls: dict[str, dict[str, float]]) -> str:
    items = [f'"{node}:{k}" {_fmt(float(v))}' for node, c in ctrls.items() for k, v in c.items()]
    return "{ params = [ " + " ".join(items) + " ] }"


def _live_params_from_dump(dump: list[dict], node_id: int) -> dict[str, float]:
    for o in dump:
        if o.get("id") != node_id:
            continue
        for prm in ((o.get("info") or {}).get("params") or {}).get("Props", []):
            p = prm.get("params")
            if isinstance(p, list) and p and isinstance(p[0], str) and ":" in p[0]:
                return {p[i]: p[i + 1] for i in range(0, len(p) - 1, 2)
                        if isinstance(p[i + 1], (int, float))}
    return {}


def read_live_params(node_id: int) -> dict[str, float]:
    """Current filter-graph control values of a running sink (from pw-dump)."""
    return _live_params_from_dump(_pw_dump(), node_id)


def _is_variant_node(node: str) -> bool:
    """Nodes that depend on the active genre group: v-slots, k-bands, cikis."""
    return node[:1] in ("v", "k") or node.startswith("cikis_")


def variant_controls(profiles: dict, profile: str, group: str,
                     staging: dict | None = None,
                     tone: dict | None = None) -> dict[str, dict[str, float]]:
    """Controls a genre-variant switch touches: v1..v4 + k1..k3 (both channels) + cikis_l/r.

    The kişisel ton rides with the variant: k-bands = effective(kisisel.json,
    profile, group) unless ``tone`` overrides (see ``tone_values``).
    """
    if profile not in VARIANT_PROFILES:
        raise ValueError(_("{profil}: tür alt profili yok").format(profil=profile))
    if group and group not in variant_groups(profile):
        raise ValueError(_("{profil}: bilinmeyen tür '{tur}'").format(profil=profile, tur=group))
    ctrls = node_controls(profiles, profile, staging, group=group, tone=tone)
    return {k: v for k, v in ctrls.items() if _is_variant_node(k)}


def tone_controls(profiles: dict, profile: str, group: str = "", staging: dict | None = None,
                  tone: dict | None = None) -> dict[str, dict[str, float]]:
    """Controls a kişisel ton change touches.

    muzik/oyun: the full ``variant_controls`` of ``group`` (so a tone change
    that cancels a running variant ramp still completes it); cs2/film: k1..k3 +
    cikis_l/r (group ignored). Raises ValueError for duz / unknown profiles.
    """
    if profile in VARIANT_PROFILES:
        return variant_controls(profiles, profile, group, staging, tone)
    if profile not in EQ_PROFILES:
        raise ValueError(_("{profil}: kişisel ton yalnız EQ çıkışlarında var ({olanlar})").format(
            profil=profile, olanlar=", ".join(EQ_PROFILES)))
    ctrls = node_controls(profiles, profile, staging, group="", tone=tone)
    return {k: v for k, v in ctrls.items() if k[:1] == "k" or k.startswith("cikis_")}


def _flatten(ctrls: dict[str, dict[str, float]]) -> dict[str, float]:
    return {f"{n}:{k}": float(v) for n, c in ctrls.items() for k, v in c.items()
            if k != "Add"}


def ramp_plan(start: dict[str, float], end: dict[str, float]) -> tuple[dict, dict]:
    """Adjust start/end so that silent slots jump instead of sweeping.

    A slot at 0 dB is inaudible, so when ramping FROM 0 dB its Freq/Q jump to the
    target at once, and when ramping TO 0 dB they stay where they are.
    """
    s, e = dict(start), dict(end)
    slots = {k.split(":")[0] for k in e if k.split(":")[1] == "Gain"}
    for n in slots:
        g0, g1 = s.get(f"{n}:Gain", 0.0), e.get(f"{n}:Gain", 0.0)
        for k in ("Freq", "Q"):
            key = f"{n}:{k}"
            if key not in e:
                continue
            if abs(g0) < 1e-6:
                s[key] = e[key]
            elif abs(g1) < 1e-6 and key in s:
                e[key] = s[key]
    return s, e


def ramp_value(key: str, a: float, b: float, t: float) -> float:
    """Interpolate one control at t in [0, 1]: gains dB-linear, Freq/Q log, Mult in dB."""
    t = min(max(t, 0.0), 1.0)
    kind = key.split(":")[1]
    if kind in ("Freq", "Q") and a > 0 and b > 0:
        return float(a * (b / a) ** t)
    if kind == "Mult" and a > 0 and b > 0:
        da, db_ = 20 * math.log10(a), 20 * math.log10(b)
        return float(10 ** ((da + (db_ - da) * t) / 20))
    return float(a + (b - a) * t)


# rid -> (thread, cancel, target): the target is kept so a ramp that cancels another
# one also finishes the cancelled ramp's controls it does not set itself
_RAMPS: dict[str, tuple[threading.Thread, threading.Event, dict[str, float]]] = {}
_RAMPS_LOCK = threading.Lock()


def _set_params(node_id: int, values: dict[str, float]) -> bool:
    pod = "{ params = [ " + " ".join(f'"{k}" {_fmt(v)}' for k, v in values.items()) + " ] }"
    try:
        r = _run(["pw-cli", "set-param", str(node_id), "Props", pod], timeout=3)
    except (OSError, subprocess.SubprocessError) as e:
        log.error("pw-cli set-param başarısız: %s", e)
        return False
    return r.returncode == 0 and "error" not in (r.stderr or "").lower()


def _start_ramp(profile: str, target: dict[str, float], ramp_s: float, prefix: str,
                done_msg: str, fallback=None, key: str = "") -> bool:
    """Ramp a running sink to ``target`` (flattened controls) in a background thread.

    Shared by apply_variant / apply_tone / apply_correction: a running ramp of the
    same sink (in THIS process) is cancelled first and the new one starts from the
    live values; controls the cancelled ramp was still moving and the new target does
    not set are carried over (a genre change must not leave a half-done headphone
    correction behind, CHANGE 7). ``fallback()`` → alternative target used when the running graph
    lacks some controls of ``target`` but has all of the alternative's.
    ``key`` separates ramps on disjoint controls of the same sink (crossfeed
    must not cancel a variant ramp).
    """
    sink = prefix + profile
    rid = sink + (":" + key if key else "")
    with _RAMPS_LOCK:
        old = _RAMPS.pop(rid, None)
    carried: dict[str, float] = {}
    if old is not None:
        old[1].set()
        old[0].join(1.0)
        carried = {k: v for k, v in old[2].items() if k not in target}
        target = {**carried, **target}
    try:
        dump = _pw_dump()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.error("pw-dump başarısız: %s", e)
        return False
    nid = _node_ids(dump).get(sink)
    if nid is None:
        log.error("Sanal çıkış bulunamadı: %s", sink)
        return False
    live = _live_params_from_dump(dump, nid)
    missing = [k for k in target if k not in live]
    if missing and fallback is not None:
        alt = fallback()
        if alt and all(k in live for k in alt):
            log.warning("'%s' grafiğinde kişisel ton bantları yok (eski yapılandırma): ton "
                        "atlandı; yeniden üretip servisi yeniden başlatmak gerekir", sink)
            target = {**{k: v for k, v in carried.items() if k in live and k not in alt}, **alt}
            missing = []
    if missing:
        log.warning("'%s' grafiğinde gerekli denetim yok (ör. %s): yapılandırmayı yeniden "
                    "üretip servisi yeniden başlatmak gerekir", sink, missing[0])
        return False
    start, end = ramp_plan({k: float(live[k]) for k in target}, target)
    cancel = threading.Event()

    def run():
        t0 = time.monotonic()
        ok = True
        while not cancel.is_set():
            t = 1.0 if ramp_s <= 0 else (time.monotonic() - t0) / ramp_s
            vals = {k: ramp_value(k, start[k], end[k], t) for k in end}
            ok = _set_params(nid, vals) and ok
            if t >= 1.0:
                break
            cancel.wait(RAMP_STEP_S)
        if not cancel.is_set() and end != target:
            # silent (0 dB) slots: park Freq/Q at the exact target, inaudible
            ok = _set_params(nid, target) and ok
        if not cancel.is_set() and not ok:
            # a failed pw-cli step must not leave a band at an in-between value: the final
            # values are what matters, so set them once more (twice at most, short pause)
            for _i in range(2):
                cancel.wait(0.05)
                if cancel.is_set() or _set_params(nid, target):
                    ok = True
                    break
        if not cancel.is_set():
            log.info("%s", done_msg)
            if not ok:
                log.warning("'%s' geçişinde bazı adımlar uygulanamadı", sink)
        with _RAMPS_LOCK:
            if _RAMPS.get(rid, (None,))[0] is threading.current_thread():
                del _RAMPS[rid]

    th = threading.Thread(target=run, name=f"oto-eq-rampa-{profile}", daemon=True)
    with _RAMPS_LOCK:
        _RAMPS[rid] = (th, cancel, dict(target))
    th.start()
    return True


def _without_tone(ctrls_fn) -> dict[str, float]:
    """Fallback target for a graph without k-bands: tone 0 dB (no compensation), no k keys."""
    flat = _flatten(ctrls_fn({k: 0.0 for k in kisisel.TON_KEYS}))
    return {k: v for k, v in flat.items() if not k.startswith("k")}


def apply_variant(profile: str, group: str, ramp_s: float = 1.5, profiles: dict | None = None,
                  prefix: str = SINK_PREFIX, staging: dict | None = None) -> bool:
    """Switch the genre variant of a running muzik/oyun sink with a smooth ramp.

    Non-blocking: reads the current values (pw-dump), then a background thread
    interpolates in ~30 ms steps (gains dB-linear, Freq/Q log, output gain in
    dB) via pw-cli set-param. A running ramp of the same sink is cancelled and
    the new one starts from wherever it was. group "" = neutral (Genel).
    The kişisel ton for (profile, group) from kisisel.json ramps along (k1..k3
    + its loudness compensation in cikis). A graph without k-bands (conf from
    before CHANGE 5) still gets the variant, without tone.
    Returns False if the sink is missing or its graph has no variant slots.
    """
    profiles = profiles if profiles is not None else load_profiles()
    target = _flatten(variant_controls(profiles, profile, group, staging))
    label = GROUP_LABELS.get(group, group) or "Genel"
    return _start_ramp(
        profile, target, ramp_s, prefix, f"'{profile}' türü uygulandı: {label}",
        fallback=lambda: _without_tone(
            lambda t: variant_controls(profiles, profile, group, staging, tone=t)))


def apply_tone(profile: str, group: str = "", ramp_s: float = 0.3, values: dict | None = None,
               profiles: dict | None = None, prefix: str = SINK_PREFIX,
               staging: dict | None = None) -> bool:
    """Ramp the kişisel ton (k1..k3) of a running EQ sink; same machinery as apply_variant.

    ``values`` None = effective(kisisel.json, profile, group); a (partial)
    {bas, vokal, parlaklik} dict = preview values (missing keys from the file),
    clamped to ±TON_LIMIT. ``group`` must be the sink's ACTIVE genre (router
    status ``variants[profile].group``; "" = Genel; ignored for cs2/film): for
    muzik/oyun the whole variant state of that group is targeted (cancel /
    restart semantics identical to apply_variant). A profile with
    kisisel_ton = false ramps to 0 dB. The output gain follows the tone's
    loudness compensation; the preamp never changes.
    Non-blocking (a CLI must ``wait_ramps()`` before exiting). Returns False if
    the sink is missing or its graph has no k-bands (regenerate + restart).
    Raises ValueError for duz / unknown profile / group / tone key.
    """
    profiles = profiles if profiles is not None else load_profiles()
    target = _flatten(tone_controls(profiles, profile, group, staging, values))
    tv = tone_values(profiles, profile, group, values)
    txt = ", ".join(f"{k} {v:+g}" for k, v in tv.items())
    return _start_ramp(profile, target, ramp_s, prefix,
                       f"'{profile}' kişisel tonu uygulandı: {txt}")


# what the headphone correction changes: d-bands, preamp, level match. Genre (v) and tone (k)
# bands do not depend on it; a running genre/tone ramp is finished via the carried target.
_CORRECTION_NODE = re.compile(r"(?:d\d+|pre|cikis)_[lr]$")


def apply_correction(groups: dict[str, str] | None = None, ramp_s: float = 1.0,
                     profiles: dict | None = None, prefix: str = SINK_PREFIX) -> bool:
    """Re-ramp every running sink after the active headphone changed (CHANGE 7).

    ``correction_on()`` (STATE_DIR/cihaz.json, written by the router BEFORE this
    call) decides whether the d-bands carry the [duzeltme] curve or stay flat; the
    preamp and the level match (cikis, incl. Düz) are re-staged with it. ``groups``:
    the ACTIVE genre per variant profile (router): the level match depends on it.
    Genre/tone bands are not touched (a running genre/tone ramp of the same sink is
    finished through the carried target of _start_ramp). Same structure either way:
    no restart. Non-blocking. Returns True if every present sink got its ramp.
    """
    profiles = profiles if profiles is not None else load_profiles()
    staging = gain_staging(profiles, lsp_available())
    state = "açık" if correction_on(profiles) else "kapalı (bu kulaklıkta nötr)"
    ok = True
    for name in ALL_SINK_PROFILES:
        group = (groups or {}).get(name, "") if name in VARIANT_PROFILES else ""
        if group and group not in variant_groups(name):
            group = ""
        target = {k: v for k, v in
                  _flatten(node_controls(profiles, name, staging, group=group)).items()
                  if _CORRECTION_NODE.match(k.split(":", 1)[0])}
        ok = _start_ramp(name, target, ramp_s, prefix,
                         f"'{name}' kulaklık düzeltmesi {state}") and ok
    return ok


def apply_crossfeed(aktif: bool | None = None, ramp_s: float = 0.3,
                    profiles: dict | None = None, prefix: str = SINK_PREFIX) -> bool:
    """Switch muzik's crossfeed on/off LIVE (mixer cross gain + direct shelf ramp).

    ``aktif`` None = the saved state from profiller.toml. No restart: the nodes
    are always in the muzik graph when a ``[profil.muzik.crossfeed]`` block
    exists. Does not save; a GUI/CLI should save the toggle with save_profiles.
    Runs as its own ramp (never cancels a variant/tone ramp). Non-blocking.
    Returns False if the block/graph nodes are missing (regenerate + restart).
    """
    profiles = profiles if profiles is not None else load_profiles()
    name = CROSSFEED_PROFILES[0]
    xf = _xf_used(profiles, name)
    if xf is None:
        log.warning("'%s' profilinde [crossfeed] bloğu yok", name)
        return False
    on = xf["aktif"] if aktif is None else bool(aktif)
    on = on and bool(profiles["profil"][name].get("aktif", True))
    target = _flatten(xf_controls(xf, on))
    return _start_ramp(name, target, ramp_s, prefix,
                       f"'{name}' crossfeed {'açıldı' if on else 'kapatıldı'}", key="xf")


def wait_ramps(timeout: float = 5.0) -> None:
    """Block until all running ramps finished (tests / shutdown)."""
    end = time.monotonic() + timeout
    while True:
        with _RAMPS_LOCK:
            threads = [t for t, _e, _tgt in _RAMPS.values()]
        if not threads or time.monotonic() > end:
            return
        threads[0].join(max(0.0, end - time.monotonic()))


def set_live(profile: str, profile_data: dict, prefix: str = SINK_PREFIX,
             staging: dict | None = None, variants: dict[str, str] | None = None) -> bool:
    """Apply changed values to the RUNNING sinks without restart.

    ``profile_data`` is either the full profiles dict (as from
    ``load_profiles``) or just the ``[profil.<profile>]`` table, which is then
    merged into the currently saved profiles. Because loudness matching
    depends on all profiles, the gain stages (pre_*, cikis_*) of every sink are
    refreshed (``staging`` may pin them, e.g. the GUI's frozen levels); all
    controls of ``profile`` are written. ``variants`` = active genre group per
    muzik/oyun sink ({"muzik": "metal_rock"}); for a variant sink NOT listed
    there its v1..v4 slots, k1..k3 (kişisel ton) and output gain are left
    untouched (the router's active variant stays). The k-bands of written
    sinks get the SAVED tone (kisisel.json). Returns False when a
    sink is missing or the running graph lacks a needed control (band
    count/type changed -> regenerate + restart the sinks service).
    """
    if "profil" in profile_data:
        new = normalize_profiles(profile_data)
    else:
        raw = copy.deepcopy(load_profiles())
        if profile == FLAT_PROFILE:
            raw[FLAT_PROFILE] = copy.deepcopy(profile_data)
        else:
            raw["profil"][profile] = copy.deepcopy(profile_data)
        new = normalize_profiles(raw)
    lsp = lsp_available()
    # staging: optional frozen gain stages (GUI "Seviyeyi koru" off)
    staging = staging or gain_staging(new, lsp)
    try:
        dump = _pw_dump()
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        log.error("pw-dump başarısız: %s", e)
        return False
    ids = _node_ids(dump)
    plan = {}
    for name in ALL_SINK_PROFILES:
        nid = ids.get(prefix + name)
        if nid is None:
            log.error("Sanal çıkış bulunamadı: %s (servis çalışıyor mu?)", prefix + name)
            return False
        grp = (variants or {}).get(name, "")
        ctrls = node_controls(new, name, staging, lsp, group=grp)
        if name != profile:   # other sinks: only the gain stages (+ limiter ceiling) change
            ctrls = {k: v for k, v in ctrls.items()
                     if k.startswith(("pre_", "cikis_")) or k == "lim"}
        if name in VARIANT_PROFILES and name not in (variants or {}):
            # active group unknown: leave v-slots, k-bands (tone per group) and cikis alone
            ctrls = {k: v for k, v in ctrls.items() if not _is_variant_node(k)}
        live = _live_params_from_dump(dump, nid)
        missing = [f"{n}:{k}" for n, c in ctrls.items() for k in c if f"{n}:{k}" not in live]
        if missing:
            log.warning("Çalışan '%s' grafiğinde %d denetim yok (ör. %s): yapı değişmiş, "
                        "yeniden üretip servisi yeniden başlatmak gerekir",
                        name, len(missing), missing[0])
            return False
        plan[name] = (nid, ctrls)
    ok = True
    for name, (nid, ctrls) in plan.items():
        try:
            r = _run(["pw-cli", "set-param", str(nid), "Props", _params_pod(ctrls)])
        except (OSError, subprocess.SubprocessError) as e:
            log.error("pw-cli set-param başarısız (%s): %s", name, e)
            ok = False
            continue
        if r.returncode != 0 or "error" in (r.stderr or "").lower():
            log.error("pw-cli set-param hatası (%s): %s", name, (r.stderr or r.stdout).strip())
            ok = False
    if ok:
        log.info("'%s' profili canlı güncellendi", profile)
    return ok


# --------------------------------------------------------------------------
# Plot
# --------------------------------------------------------------------------

# same as ui_common.PROFILE_COLORS (not imported: ui_common pulls in PyQt6)
PLOT_COLORS = {"cs2": "#F28C28", "oyun": "#8E5CD9", "muzik": "#22A559", "film": "#3B82F6",
               "duz": "#9AA0A6"}


def plot(path: Path = config.PLOT_FILE, profiles: dict | None = None) -> Path:
    """Render the curves (dark theme, Turkish labels) to a PNG."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter

    profiles = profiles if profiles is not None else load_profiles()
    lsp = lsp_available()
    staging = gain_staging(profiles, lsp)
    f = PLOT_FREQS
    rate = profiles["genel"]["orneklem_hz"]
    plt.style.use("dark_background")
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 9), height_ratios=[3, 2],
                                   facecolor="#16181d")
    corr = response_db(profiles["duzeltme"]["filtreler"] if correction_on(profiles) else [],
                       f, rate)
    ax1.plot(f, corr, color="#d0d0d0", lw=1.6, ls="--",
             label=_("HS80 düzeltmesi (AutoEq → Harman), ön kazançsız"))
    for name in EQ_PROFILES:
        p = profiles["profil"][name]
        s = staging[name]
        tot = total_response(profiles, name, f, lsp, tone=tone_values(profiles, name))
        dyn = _dyn_active(profiles, name, lsp)
        kw = {"profil": PROFILE_LABELS[name], "odak": veri_metni(p.get("odak")),
              "kazanc": s["preamp_db"] + s["match_db"]}
        if dyn:
            lab = _("{profil} — {odak}  (kazanç {kazanc:+.1f} dB + telafi {telafi:+g} dB, kompresör "
                    "{oran:g}:1 @ {esik:g} dB)").format(telafi=dyn["telafi_db"], oran=dyn["oran"],
                                                         esik=dyn["esik_db"], **kw)
        else:
            lab = _("{profil} — {odak}  (kazanç {kazanc:+.1f} dB)").format(**kw)
        if not p["aktif"]:
            lab = _("{profil} — KAPALI (düz geçiş)").format(profil=PROFILE_LABELS[name])
        ax1.plot(f, tot, color=PLOT_COLORS[name], lw=2.2, label=lab)
        add = response_db(p["filtreler"] if p["aktif"] else [], f, rate)
        ax2.plot(f, add, color=PLOT_COLORS[name], lw=2.2,
                 label=f"{PROFILE_LABELS[name]} — {p.get('odak', '')}")
    fs = staging[FLAT_PROFILE]
    duz = PROFILE_LABELS[FLAT_PROFILE]
    flat_lab = (_("{duz} — {odak} ({db:+.1f} dB; sohbet, sistem, kapalı mod)").format(
                    duz=duz, odak=_(FLAT_ODAK), db=fs["match_db"])
                if profiles[FLAT_PROFILE]["aktif"] else
                _("{duz} — eşitleme kapalı (0 dB)").format(duz=duz))
    ax1.plot(f, np.full_like(f, fs["match_db"]), color=PLOT_COLORS[FLAT_PROFILE], lw=1.6,
             ls=":", label=flat_lab)
    lo, hi = ax1.get_ylim()
    ax1.set_ylim(lo - 10, hi + 1)   # room for the legend under the curves
    ax1.axvspan(2000, 4500, color="#ff6b6b", alpha=0.07)
    ax1.text(2050, ax1.get_ylim()[1] - 1.2, _("adım sesi bandı"),
             color="#ff9b9b", fontsize=9)
    ax1.set_title(_("{ad} — HS80 EQ eğrileri (kulağa giden toplam, seviye eşitlenmiş)").format(
        ad=APP_NAME), fontsize=13)
    ax1.set_ylabel(_("Kazanç (dB)"))
    ax2.set_title(_("Profillerin düzeltmenin üstüne eklediği"), fontsize=11)
    ax2.set_ylabel(_("Kazanç (dB)"))
    ax2.set_xlabel(_("Frekans (Hz)"))
    ticks = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
    for ax in (ax1, ax2):
        ax.set_facecolor("#1d2027")
        ax.set_xscale("log")
        ax.set_xlim(20, 20000)
        ax.xaxis.set_major_locator(FixedLocator(ticks))
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _: f"{v / 1000:g}k" if v >= 1000 else f"{v:g}"))
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.grid(True, which="major", color="#3a3f4b", lw=0.8)
        ax.grid(True, which="minor", color="#2a2e37", lw=0.5)
        ax.axhline(0, color="#777", lw=0.8)
        ax.legend(loc="lower left" if ax is ax1 else "lower right", fontsize=9, framealpha=0.85, facecolor="#22252d",
                  edgecolor="#444")
    fig.tight_layout()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)
    log.info("Eğri grafiği yazıldı: %s", path)
    return path


# --------------------------------------------------------------------------
# Live self-test with a separate test pipewire process
# --------------------------------------------------------------------------

TEST_STREAM_PROPS = {
    "application.name": "oto-eq-test-check",
    "application.process.binary": "oto-eq-test-check",
    "state.restore-props": "false",
    "state.restore-target": "false",
}


def _sink_names() -> list[str]:
    r = _run(["pactl", "list", "short", "sinks"])
    return [ln.split("\t")[1] for ln in r.stdout.splitlines() if "\t" in ln]


def _write_silence(path: Path, seconds: float = 3.0) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\0" * int(48000 * seconds) * 4)


# --------------------------------------------------------------------------
# Measurement (CHANGE 6): worst-case clipping + added latency + CPU, in a TEST
# pipewire process whose *_out nodes are NOT auto-connected (never audible):
# each output is linked by pw-link to our own pw-record only.
# --------------------------------------------------------------------------

OLCUM_PREFIX = SINK_PREFIX + "olcum_"
OLCUM_STREAM_PROPS = TEST_STREAM_PROPS | {"application.name": "oto-eq-olcum",
                                          "application.process.binary": "oto-eq-olcum",
                                          "node.dont-reconnect": "true"}


def worst_unit(profiles: dict, name: str, staging: dict) -> tuple[str, dict]:
    """(group, tone) of the profile's unit with the highest static worst-case peak."""
    st = staging[name]
    grp = max([""] + list(st.get("variants", {})),
              key=lambda g: staging_for(staging, name, g)["worst_peak_dbfs"])
    tone = kisisel.zero()
    if tone_enabled(profiles, name):
        tone = tone_worst_peak(profiles, effective_filters(profiles, name, grp))[1]
    return grp, tone


def olcum_controls(profiles: dict, name: str, staging: dict, lsp: bool) -> dict:
    """Worst-case start values: worst variant + tone, crossfeed ON, leveller pre-charged
    fast (it reaches its max boost within ~0.2 s and keeps it: no quick fall)."""
    if name == FLAT_PROFILE:
        return node_controls(profiles, name, staging, lsp)
    grp, tone = worst_unit(profiles, name, staging)
    ctrls = node_controls(profiles, name, staging, lsp, group=grp, tone=tone)
    xf = _xf_used(profiles, name)
    if xf is not None and profiles["profil"][name].get("aktif", True):
        ctrls.update(xf_controls(xf, True))
    if "ag" in ctrls and ctrls["ag"]["enabled"]:
        ag = dict(ctrls["ag"])
        ag.update({"vgrow_l": float(AG_AMOUNTS.index(6.0)), "tgrow_l": 30.0,
                   "vgrow_s": float(AG_AMOUNTS.index(6.0)), "tgrow_s": 30.0, "drift": 24.0})
        ctrls["ag"] = ag
    return ctrls


def peak_freq(profiles: dict, name: str, staging: dict) -> float:
    """Frequency (Hz) of the largest EQ gain of the profile's worst unit (1 kHz if flat)."""
    if name == FLAT_PROFILE:
        return 1000.0
    grp, tone = worst_unit(profiles, name, staging)
    flt = effective_filters(profiles, name, grp)
    if tone_enabled(profiles, name):
        flt = flt + tone_filters(tone)
    r = response_db(flt, _PEAK_GRID, profiles["genel"]["orneklem_hz"])
    return float(_PEAK_GRID[int(np.argmax(r))]) if len(flt) else 1000.0


def olcum_signal(f_peak: float, rate: int = 48000) -> tuple[np.ndarray, dict]:
    """Worst-case test signal (float32, N x 2, never above 0 dBFS) + segment starts (s).

    quiet pink (−45 dBFS, charges the leveller) → 0 dBFS sine at the EQ peak →
    0 dBFS log sweep 20 Hz–20 kHz (in phase) → same anti-phase (crossfeed) →
    full-scale clicks on silence → 0 dBFS 60 Hz square → peak-normalised noise →
    L-only −20 dBFS 100 Hz (crossfeed check).
    """
    rng = np.random.default_rng(6)

    def t(sec):
        return np.arange(int(rate * sec)) / rate

    def pink(n):
        sp = np.fft.rfft(rng.normal(0, 1, n))
        fr = np.fft.rfftfreq(n, 1 / rate)
        sp[1:] /= np.sqrt(fr[1:])
        sp[0] = 0
        y = np.fft.irfft(sp, n)
        return y / np.max(np.abs(y))

    def sweep(sec):
        tt = t(sec)
        k = math.log(20000 / 20)
        return np.sin(2 * math.pi * 20 * sec / k * (np.exp(tt / sec * k) - 1))

    seg, out, pos = {}, [], 0.0

    def add(key, x):
        nonlocal pos
        x = np.asarray(x, dtype=float)
        if x.ndim == 1:
            x = np.stack([x, x], 1)
        seg[key] = pos
        out.append(x)
        pos += len(x) / rate

    add("sessiz_pembe", 10 ** (-45 / 20) * pink(int(rate * 1.0)))
    add("tepe_sinus", np.sin(2 * math.pi * f_peak * t(0.5)))
    add("tarama", sweep(2.0))
    s = sweep(0.6)
    add("ters_faz", np.stack([s, -s], 1))
    clicks = np.zeros(int(rate * 0.6))
    clicks[int(rate * 0.3)::int(rate * 0.05)] = 1.0
    add("tik", clicks)
    add("kare", np.sign(np.sin(2 * math.pi * 60 * t(0.5))) * 0.999)
    add("gurultu", pink(int(rate * 0.5)))
    lonly = np.zeros((int(rate * 0.5), 2))
    lonly[:, 0] = 0.1 * np.sin(2 * math.pi * 100 * t(0.5))
    add("sol_tek", lonly)
    add("son", np.zeros((int(rate * 0.3), 2)))
    sig = np.concatenate(out).astype(np.float32)
    return np.clip(sig, -1.0, 1.0), seg


def _latency_graph(profiles: dict, name: str, staging: dict, lsp: bool) -> list[str]:
    """The sink's real graph + a reference path: in FL → copy → out FL; in FR → split →
    both chain inputs → chain L → out FR. Added latency = onset(FR) − onset(FL)."""
    nodes, links, ins, outs = _graph_parts(profiles, name, staging, lsp)
    nodes = nodes + ['{ type = builtin name = lat_ref label = copy }',
                     '{ type = builtin name = lat_split label = copy }']
    links = links + [f'{{ output = "lat_split:Out" input = "{i}" }}' for i in ins]
    return _format_graph(nodes, links, ["lat_ref:In", "lat_split:In"], ["lat_ref:Out", outs[0]])


def _olcum_conf(profiles: dict, prefix: str, lsp: bool, graphs: dict[str, list[str]]) -> str:
    """Minimal test conf: one filter-chain per entry of ``graphs`` (outputs not autoconnected)."""
    L = ["context.properties = { log.level = 2 }",
         "context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert",
         "                     support.* = support/libspa-support }",
         "context.modules = [",
         "    { name = libpipewire-module-rt args = { } flags = [ ifexists nofail ] }",
         "    { name = libpipewire-module-protocol-native }",
         "    { name = libpipewire-module-client-node }",
         "    { name = libpipewire-module-adapter }"]
    for name, glines in graphs.items():
        sink = prefix + name
        L += ["    { name = libpipewire-module-filter-chain", "        args = {",
              f"            node.description = {_fmt('oto-eq ölçüm · ' + name)}",
              f"            node.link-group  = {_fmt(prefix.rstrip('_'))}",
              "            audio.channels   = 2", "            audio.position   = [ FL FR ]",
              "            filter.graph = {"] + glines + [
              "            }",
              f"            capture.props = {{ node.name = {_fmt(sink)} media.class = Audio/Sink",
              "                audio.position = [ FL FR ] priority.session = 1 priority.driver = 1",
              "                state.restore-props = false }",
              f"            playback.props = {{ node.name = {_fmt(sink + '_out')} node.passive = true",
              "                node.autoconnect = false audio.position = [ FL FR ]",
              "                state.restore-props = false }",
              "        }", "    }"]
    L += ["]", ""]
    return "\n".join(L)


def _proc_cpu_s(pid: int) -> float:
    try:
        f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError):
        return float("nan")


def _run_measurement(conf_text: str, signals: dict[str, np.ndarray], prefix: str, work: Path,
                     tag: str, say=print) -> tuple[dict[str, np.ndarray], float]:
    """Start ``conf_text`` in a test pipewire, record every sink's output (manually linked,
    verified to go ONLY to our recorder), play ``signals`` {name: N x 2} in parallel.
    Returns ({name: recorded N' x 2}, CPU % of one core of the test process during play)."""
    work.mkdir(parents=True, exist_ok=True)
    conf = work / f"olcum-{tag}.conf"
    conf.write_text(conf_text, encoding="utf-8")
    logf = open(work / f"olcum-{tag}.log", "w")
    proc = None
    recs: dict[str, subprocess.Popen] = {}
    plays: list[subprocess.Popen] = []
    raws = {n: work / f"{tag}-{n}.cikis.raw" for n in signals}
    cpu = float("nan")
    fmt = ["-a", "--format", "f32", "--rate", "48000", "--channels", "2"]
    try:
        proc = subprocess.Popen(["pipewire", "-c", str(conf)], env=config.C_ENV,
                                stdout=logf, stderr=subprocess.STDOUT)
        want = [prefix + n + s for n in signals for s in ("", "_out")]
        for _i in range(60):
            ids = _node_ids(_pw_dump())
            if all(w in ids for w in want) or proc.poll() is not None:
                break
            time.sleep(0.2)
        if not all(w in ids for w in want):
            raise RuntimeError(_("ölçüm çıkışları açılmadı (günlük: {yol})").format(
                yol=work / f"olcum-{tag}.log"))
        for n in signals:
            recs[n] = subprocess.Popen(
                ["pw-record", *fmt, "--target", "0", "-P",
                 json.dumps(OLCUM_STREAM_PROPS | {"node.name": prefix + n + "_kayit",
                                                  "node.autoconnect": "false"}),
                 str(raws[n])], env=config.C_ENV,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _i in range(60):
            ids = _node_ids(_pw_dump())
            if all(prefix + n + "_kayit" in ids for n in signals):
                break
            time.sleep(0.1)
        for n in signals:
            for ch in ("FL", "FR"):
                r = _run(["pw-link", f"{prefix}{n}_out:output_{ch}",
                          f"{prefix}{n}_kayit:input_{ch}"])
                if r.returncode != 0:
                    raise RuntimeError(f"pw-link {n} {ch}: {r.stderr.strip()}")
        # SAFETY: every test output goes ONLY to its own recorder (never to the headset)
        dump = _pw_dump()
        ids = _node_ids(dump)
        for n in signals:
            peers = {(o.get("info") or {}).get("input-node-id") for o in dump
                     if o.get("type") == "PipeWire:Interface:Link"
                     and (o.get("info") or {}).get("output-node-id") == ids[prefix + n + "_out"]}
            if peers != {ids[prefix + n + "_kayit"]}:
                raise RuntimeError(_("güvenlik: {cikis} başka yere bağlı ({bagli}); test durdu").format(
                        cikis=f"{n}_out", bagli=peers))
        time.sleep(0.3)
        for n, sig in signals.items():
            src = work / f"{tag}-{n}.giris.raw"
            np.asarray(sig, dtype=np.float32).tofile(src)
            plays.append(subprocess.Popen(
                ["pw-play", *fmt, "--target", prefix + n, "--volume", "1.0", "-P",
                 json.dumps(OLCUM_STREAM_PROPS | {"media.name": f"oto-eq-olcum {n}"}), str(src)],
                env=config.C_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        c0, t0 = _proc_cpu_s(proc.pid), time.monotonic()
        dur = max(len(s) for s in signals.values()) / 48000
        for p in plays:
            p.wait(dur + 15)
        cpu = 100.0 * (_proc_cpu_s(proc.pid) - c0) / max(time.monotonic() - t0, 1e-3)
        time.sleep(0.4)
    finally:
        for p in plays + list(recs.values()):
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(3)
            except subprocess.TimeoutExpired:
                p.kill()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        logf.close()
    out = {}
    for n, p in raws.items():
        try:
            out[n] = np.fromfile(p, dtype=np.float32).reshape(-1, 2)
        except (OSError, ValueError):
            out[n] = np.zeros((0, 2), dtype=np.float32)
    for f in list(raws.values()) + [work / f"{tag}-{n}.giris.raw" for n in signals]:
        try:                       # synthetic test signals only; do not leave them around
            f.unlink()
        except OSError:
            pass
    return out, cpu


def _onset(x: np.ndarray, rel: float = 1e-3) -> int | None:
    m = float(np.max(np.abs(x))) if len(x) else 0.0
    if m <= 0:
        return None
    return int(np.argmax(np.abs(x) > m * rel))


def olcum(profiles: dict | None = None, workdir: Path | None = None, say=print,
          prefix: str = OLCUM_PREFIX) -> dict:
    """Measure every sink in a test pipewire process (inaudible, see module note):

    * worst case: ``olcum_signal`` (0 dBFS sine at the EQ peak, sweeps, clicks,
      square, noise) with the worst variant + kişisel ton, crossfeed on and the
      leveller at its max boost → output peak must stay ≤ tavan_dbfs;
    * added latency per sink (sample-exact, impulse onset vs. an in-graph reference);
    * CPU of the test process while all 5 sinks process the worst-case signal.

    Returns {"ok": bool, "sinks": {name: {...}}, "cpu_pct": float}.
    """
    profiles = profiles if profiles is not None else load_profiles()
    lsp = lsp_available()
    st = gain_staging(profiles, lsp)
    ceil = profiles["genel"]["tavan_dbfs"]
    work = (Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="oto-eq-olcum-"))).resolve()
    res: dict = {"ok": True, "sinks": {}, "cpu_pct": float("nan")}
    # 1) worst case
    ctrls = {n: olcum_controls(profiles, n, st, lsp) for n in ALL_SINK_PROFILES}
    graphs = {n: _format_graph(*_graph_parts(profiles, n, st, lsp, ctrls[n]))
              for n in ALL_SINK_PROFILES}
    sigs, segs = {}, {}
    for n in ALL_SINK_PROFILES:
        sigs[n], segs[n] = olcum_signal(peak_freq(profiles, n, st))
    rec, cpu = _run_measurement(_olcum_conf(profiles, prefix, lsp, graphs), sigs, prefix, work,
                                "enkotu", say)
    res["cpu_pct"] = cpu
    for n in ALL_SINK_PROFILES:
        y = rec[n]
        pk = float(20 * np.log10(max(float(np.max(np.abs(y))) if len(y) else 0.0, 1e-9)))
        ok = len(y) > 0 and pk <= ceil + 0.01
        info = {"tepe_dbfs": pk, "statik_en_kotu_dbfs": st[n]["worst_peak_dbfs"],
                "sinirlayici": _lim_active(profiles, n, lsp) is not None, "ok": ok}
        # crossfeed functional check on the L-only segment
        if _xf_used(profiles, n) is not None and len(y):
            on = _onset(y[:, 0], 1e-4)
            if on is not None:
                a = on + int(48000 * (segs[n]["sol_tek"] + 0.15))
                b = on + int(48000 * (segs[n]["son"] - 0.05))
                seg_y = y[a:b]
                if len(seg_y):
                    l_rms = float(np.sqrt(np.mean(seg_y[:, 0] ** 2)))
                    r_rms = float(np.sqrt(np.mean(seg_y[:, 1] ** 2)))
                    info["crossfeed_r_l_db"] = 20 * math.log10(max(r_rms, 1e-12)
                                                               / max(l_rms, 1e-12))
        res["sinks"][n] = info
        res["ok"] &= ok
    # 2) latency (impulses at -30 dBFS: no limiter/compressor gain change matters for onset)
    lat_graphs = {n: _latency_graph(profiles, n, st, lsp) for n in EQ_PROFILES}
    imp = np.zeros((48000, 2), dtype=np.float32)
    imp[12000::9000] = 10 ** (-30 / 20)
    lrec, _cpu = _run_measurement(_olcum_conf(profiles, prefix, lsp, lat_graphs),
                                  {n: imp for n in EQ_PROFILES}, prefix, work, "gecikme", say)
    for n in EQ_PROFILES:
        y = lrec[n]
        a, b = (_onset(y[:, 0]), _onset(y[:, 1])) if len(y) else (None, None)
        smp = None if a is None or b is None else b - a
        res["sinks"][n]["gecikme_ornek"] = smp
        res["sinks"][n]["gecikme_ms"] = None if smp is None else smp / 48.0
        lb = _lim_used(profiles, n, lsp)
        expect = round(lb["ileri_bakis_ms"] * 48) if lb else 0
        res["sinks"][n]["beklenen_gecikme_ornek"] = expect
        if smp is None or abs(smp - expect) > 2:
            res["sinks"][n]["ok"] = False
            res["ok"] = False
    res["sinks"][FLAT_PROFILE]["gecikme_ornek"] = 0
    res["sinks"][FLAT_PROFILE]["gecikme_ms"] = 0.0
    return res


# ---- realistic music: how often / how much does the limiter work? -------------

_K_WEIGHT_SOS = ((1.53512485958697, -2.69169618940638, 1.19839281085285,
                  1.0, -1.69065929318241, 0.73248077421585),
                 (1.0, -2.0, 1.0, 1.0, -1.99004745483398, 0.99007225036621))


def lufs_48k(x: np.ndarray) -> float:
    """Ungated BS.1770 loudness of a 48 kHz (N x 2) signal (steady test signals)."""
    from scipy.signal import sosfilt
    y = sosfilt(np.array(_K_WEIGHT_SOS), x, axis=0)
    return float(-0.691 + 10 * np.log10(np.sum(np.mean(y ** 2, axis=0)) + 1e-20))


def _master_limit(x: np.ndarray, ceil_db: float = -0.3, look: int = 72,
                  rel_s: float = 0.05) -> np.ndarray:
    """Simple look-ahead brick-wall (mastering emulation for the test signal)."""
    from scipy.ndimage import minimum_filter1d
    from scipy.signal import lfilter
    th = 10 ** (ceil_db / 20)
    need = np.minimum(1.0, th / np.maximum(np.max(np.abs(x), axis=1), 1e-12))
    g = minimum_filter1d(need, size=2 * look + 1)
    a = math.exp(-1 / (rel_s * 48000))
    sm = lfilter([1 - a], [1, -a], g, zi=[g[0] * a])[0]   # release smoothing
    g = np.minimum(minimum_filter1d(np.minimum(sm, g), size=2 * look + 1), need)
    return x * g[:, None]


def gercekci_muzik(kind: str, target_lufs: float, sec: float = 4.0, seed: int = 1,
                   rate: int = 48000) -> np.ndarray:
    """Synthetic 'mastered' music (N x 2 float32): pink bed (stereo) + kick + bass line,
    brick-wall limited to -0.3 dBFS and scaled to ``target_lufs`` (ungated).
    kind: pop | rock | edm (bass heavy) | akustik (little bass)."""
    rng = np.random.default_rng(seed)
    n = int(sec * rate)

    def pink(m):
        sp = np.fft.rfft(rng.normal(0, 1, m))
        fr = np.fft.rfftfreq(m, 1 / rate)
        sp[1:] /= np.sqrt(fr[1:])
        sp[0] = 0
        sp[fr < 25] *= (fr[fr < 25] / 25) ** 2
        y = np.fft.irfft(sp, m)
        return y / np.sqrt(np.mean(y ** 2))

    mid, side = pink(n), pink(n)
    x = np.stack([mid + 0.4 * side, mid - 0.4 * side], 1) * 0.25
    beat = 60 / (124 if kind == "edm" else 100)
    lo = np.zeros(n)
    k_amp = {"pop": 0.8, "rock": 0.9, "edm": 1.3, "akustik": 0.2}[kind]
    b_amp = {"pop": 0.5, "rock": 0.4, "edm": 0.9, "akustik": 0.15}[kind]
    for k in np.arange(0, sec, beat):
        i = int(k * rate)
        m = min(n - i, int(0.35 * rate))
        tt = np.arange(m) / rate
        f = 50 + 70 * np.exp(-tt / 0.03)
        lo[i:i + m] += k_amp * np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(
            -tt / (0.18 if kind == "edm" else 0.12))
    notes = [36.7, 41.2, 43.7, 32.7] if kind == "edm" else [41.2, 55.0, 49.0, 61.7]
    for j, k in enumerate(np.arange(0, sec, 2 * beat)):
        i = int(k * rate)
        m = min(n - i, int(2 * beat * rate))
        tt = np.arange(m) / rate
        lo[i:i + m] += b_amp * np.sin(2 * np.pi * notes[j % 4] * tt) * (1 - np.exp(-tt / 0.01))
    x = x + lo[:, None]
    g = 1.0
    for _i in range(8):
        g *= 10 ** ((target_lufs - lufs_48k(_master_limit(x * g))) / 20)
    return _master_limit(x * g).astype(np.float32)


GERCEKCI_SETS = (("pop", -8.0), ("edm", -8.0), ("rock", -8.0), ("akustik", -14.0))
GERCEKCI_UNITS = (("cs2", ""), ("oyun", ""), ("muzik", ""), ("muzik", "elektronik"),
                  ("muzik", "metal_rock"), ("film", ""))


def olcum_gercekci(profiles: dict | None = None, workdir: Path | None = None, say=print,
                   prefix: str = OLCUM_PREFIX) -> dict:
    """Realistic masters (pop/edm/rock -8 LUFS, akustik -14) through each unit TWICE in the
    test process: limiter on vs. bypassed (same latency). Per 10 ms block the limiter's
    gain reduction = peak(bypass) - peak(on). Also the net full-band RMS gain (in → out).
    Leveller (autogain) disabled here: it is slow and would only mask the static level."""
    profiles = profiles if profiles is not None else load_profiles()
    lsp = lsp_available()
    st = gain_staging(profiles, lsp)
    work = (Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="oto-eq-olcum-"))).resolve()
    parts = [gercekci_muzik(k, lv, seed=i + 1) for i, (k, lv) in enumerate(GERCEKCI_SETS)]
    gap = np.zeros((int(0.3 * 48000), 2), dtype=np.float32)
    sig = np.concatenate([np.concatenate([p, gap]) for p in parts])
    bounds, pos = [], int(0.0)
    for p in parts:
        bounds.append((pos, pos + len(p)))
        pos += len(p) + len(gap)
    graphs, keys = {}, []
    for prof, grp in GERCEKCI_UNITS:
        if prof == "muzik" and grp and grp not in defined_groups(profiles, prof):
            continue
        base = node_controls(profiles, prof, st, lsp, group=grp)
        if "ag" in base:
            base["ag"] = dict(base["ag"], enabled=0.0)
        for on in (True, False):
            c = copy.deepcopy(base)
            if "lim" in c and not on:
                c["lim"] = dict(c["lim"], enabled=0.0)
            key = f"{prof}{'_' + grp if grp else ''}_{'acik' if on else 'bypass'}"
            graphs[key] = _format_graph(*_graph_parts(profiles, prof, st, lsp, c))
            keys.append(key)
    rec, _cpu = _run_measurement(_olcum_conf(profiles, prefix, lsp, graphs),
                                 {k: sig for k in keys}, prefix, work, "gercekci", say)
    out: dict = {"ok": True, "units": {}}
    blk = 480
    for prof, grp in GERCEKCI_UNITS:
        k_on = f"{prof}{'_' + grp if grp else ''}_acik"
        k_by = f"{prof}{'_' + grp if grp else ''}_bypass"
        if k_on not in rec:
            continue
        y_on, y_by = rec[k_on], rec[k_by]
        a_on, a_by = _onset(y_on[:, 0], 1e-4), _onset(y_by[:, 0], 1e-4)
        a_in = _onset(sig[:, 0], 1e-4)
        rows = {}
        for (kind, lv), (b0, b1) in zip(GERCEKCI_SETS, bounds):
            if a_on is None or a_by is None:
                continue
            o_on = y_on[a_on - a_in + b0:a_on - a_in + b1]
            o_by = y_by[a_by - a_in + b0:a_by - a_in + b1]
            m = min(len(o_on), len(o_by)) // blk * blk
            if m <= 0:
                continue
            p_on = np.max(np.abs(o_on[:m]).reshape(-1, blk, 2), axis=(1, 2))
            p_by = np.max(np.abs(o_by[:m]).reshape(-1, blk, 2), axis=(1, 2))
            gr = np.maximum(0.0, 20 * np.log10(np.maximum(p_by, 1e-9) / np.maximum(p_on, 1e-9)))
            x = sig[b0:b1]
            net = 10 * math.log10(np.mean(o_on[:m] ** 2) / np.mean(x[:m] ** 2))
            pk = 20 * math.log10(max(float(np.max(np.abs(o_on[:m]))), 1e-9))
            rows[f"{kind}{lv:+.0f}"] = {"net_db": net, "tepe_dbfs": pk,
                                        "gr_max_db": float(gr.max()),
                                        "gr_yuzde": float(np.mean(gr > 0.1) * 100),
                                        "gr1_yuzde": float(np.mean(gr > 1.0) * 100)}
            if pk > profiles["genel"]["tavan_dbfs"] + 0.01:
                out["ok"] = False
        out["units"][f"{prof}{':' + grp if grp else ''}"] = rows
    return out


def gercekci_report(res: dict, say=print) -> None:
    say("  " + _("gerçekçi master (sınırlayıcı açık / baypas karşılaştırması; net = tüm bant RMS):"))
    for unit, rows in res["units"].items():
        say(f"  {unit:16s} " + " | ".join(
            _("{sinyal} net {net:+.1f} dB, tepe {tepe:+.1f}, sınırlayıcı %{yuzde:.0f} (>1 dB "
              "%{yuzde1:.0f}, en çok {en_cok:.1f} dB)").format(
                sinyal=k, net=r["net_db"], tepe=r["tepe_dbfs"], yuzde=r["gr_yuzde"],
                yuzde1=r["gr1_yuzde"], en_cok=r["gr_max_db"])
            for k, r in rows.items()))


def olcum_report(res: dict, ceil: float, say=print) -> None:
    for n, i in res["sinks"].items():
        lat = i.get("gecikme_ms")
        lat_s = "?" if lat is None else _("{ms:.2f} ms ({n} örnek)").format(
            ms=lat, n=i.get("gecikme_ornek"))
        kw = {"cikis": n, "tepe": i["tepe_dbfs"], "tavan": ceil,
              "statik": i["statik_en_kotu_dbfs"], "gecikme": lat_s}
        if "crossfeed_r_l_db" in i:
            line = _("ölçüm {cikis}: en kötü sinyalde çıkış tepesi {tepe:+.2f} dBFS (tavan "
                     "{tavan:+.1f}; sınırlayıcısız statik en kötü {statik:+.2f}), eklenen gecikme "
                     "{gecikme}, crossfeed sağ/sol {xf:+.1f} dB").format(
                xf=i["crossfeed_r_l_db"], **kw)
        else:
            line = _("ölçüm {cikis}: en kötü sinyalde çıkış tepesi {tepe:+.2f} dBFS (tavan "
                     "{tavan:+.1f}; sınırlayıcısız statik en kötü {statik:+.2f}), eklenen gecikme "
                     "{gecikme}").format(**kw)
        say(("✓ " if i["ok"] else _("HATA:") + " ") + line)
    say("  " + _("test sürecinin işlemci kullanımı (5 çıkış birlikte, en kötü sinyal): "
                 "%{yuzde:.1f} (tek çekirdek)").format(yuzde=res["cpu_pct"]))


def check(workdir: Path | None = None, say=print, olc: bool = True) -> bool:
    """Start the generated config in a TEST pipewire process and verify it.

    Plays only digital silence (volume 0) into the dynamics sinks so the LV2
    plugin gets instantiated. Everything is torn down in ``finally``. Then (``olc``)
    ``olcum()``: worst-case signal, latency and CPU in a SECOND test process whose
    outputs are linked only to our recorder (inaudible).
    """
    profiles = load_profiles()
    lsp = lsp_available()
    existing = [s for s in _sink_names() if s.startswith(SINK_PREFIX)]
    prefix = SINK_PREFIX if not existing else SINK_PREFIX + "test_"
    if existing:
        say(_("Not: gerçek sanal çıkışlar zaten açık ({cikislar}); test '{onek}*' adlarıyla "
              "yapılıyor").format(cikislar=", ".join(existing), onek=prefix))
    work = (Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="oto-eq-check-"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    conf = work / "oto-eq-sinks.test.conf"
    conf.write_text(generate_conf(profiles, prefix=prefix, lsp=lsp), encoding="utf-8")
    logf = open(work / "pipewire-test.log", "w")
    wav = work / "sessizlik.wav"
    _write_silence(wav)
    names = [prefix + p for p in ALL_SINK_PROFILES]
    ok = True
    proc = None
    players: list[subprocess.Popen] = []

    def fail(msg):
        nonlocal ok
        ok = False
        say(_("HATA: {hata}").format(hata=msg))

    try:
        proc = subprocess.Popen(["pipewire", "-c", str(conf)], env=config.C_ENV,
                                stdout=logf, stderr=subprocess.STDOUT)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:
            if proc.poll() is not None:
                break
            if all(n in _sink_names() for n in names):
                break
            time.sleep(0.3)
        present = [n for n in names if n in _sink_names()]
        if len(present) != len(names):
            fail(_("sanal çıkışlar açılmadı (görünen: {gorunen}); günlük: {yol}").format(
                gorunen=present, yol=work / "pipewire-test.log"))
            return False
        say("✓ " + _("{n} sanal çıkış açıldı ({sn:.1f} sn): {cikislar}").format(
            n=len(names), sn=time.monotonic() - t0, cikislar=", ".join(names)))

        # silence (volume 0) into every sink -> LV2 instantiation + comparable latency.
        # Own app name + no restore: WirePlumber must not remember "volume 0"
        # for a generic "pw-play" entry.
        for name in ALL_SINK_PROFILES:
            players.append(subprocess.Popen(
                ["pw-play", "--target", prefix + name, "--volume", "0",
                 "-P", json.dumps(TEST_STREAM_PROPS | {"media.name": f"oto-eq-check {name}"}),
                 str(wav)],
                env=config.C_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        time.sleep(1.5)

        default = _run(["pactl", "get-default-sink"]).stdout.strip()
        dump = _pw_dump()
        ids = _node_ids(dump)
        props = {o["id"]: (o.get("info") or {}).get("props") or {} for o in dump
                 if o.get("type") == "PipeWire:Interface:Node"}
        links = [((o.get("info") or {}).get("output-node-id"), (o.get("info") or {}).get("input-node-id"))
                 for o in dump if o.get("type") == "PipeWire:Interface:Link"]
        def_id = ids.get(default)
        if default.startswith(SINK_PREFIX):
            fail(_("varsayılan çıkış bir EQ çıkışı oldu: {cikis}").format(cikis=default))
        for name in ALL_SINK_PROFILES:
            cap, out = ids.get(prefix + name), ids.get(prefix + name + "_out")
            if cap is None or out is None:
                fail(_("{cikis}: düğüm bulunamadı").format(cikis=name))
                continue
            cp, op = props[cap], props[out]
            want_desc = f"{APP_NAME} · {PROFILE_NAMES[name]}"     # = generate_conf
            if cp.get("media.class") != "Audio/Sink" or cp.get("node.description") != want_desc:
                fail(_("{cikis}: yanlış özellikler {sinif} / {aciklama}").format(
                    cikis=name, sinif=cp.get("media.class"), aciklama=cp.get("node.description")))
            if str(op.get("node.passive")).lower() != "true":
                fail(_("{cikis}: çıkış node.passive değil").format(cikis=name))
            targets = sorted({props.get(i, {}).get("node.name", str(i)) for o_, i in links if o_ == out})
            if targets != [default]:
                fail(_("{cikis}: çıkış bağlantısı {bagli}, beklenen [{beklenen}]").format(
                    cikis=name, bagli=targets, beklenen=default))
            elif any(t.startswith(SINK_PREFIX) for t in targets):
                fail(_("{cikis}: çıkış başka bir EQ çıkışına bağlandı").format(cikis=name))
            else:
                say(f"✓ {name}: {prefix + name}_out → {default} (link-group {cp.get('node.link-group')})")
        if def_id is None:
            fail(_("varsayılan çıkış düğümü bulunamadı"))

        # latency: no node of the cs2 chain may ADD latency (ProcessLatency = 0) and
        # the propagated Latency must equal that of an EQ-only chain (muzik)
        def lat_params(nm: str, key: str) -> list[dict]:
            o = next((x for x in dump if x.get("id") == ids.get(nm)), None)
            return sorted(({k: v for k, v in prm.items() if k != "direction"} | {"dir": prm.get("direction")}
                           for prm in (((o or {}).get("info") or {}).get("params") or {}).get(key, [])),
                          key=lambda x: str(x["dir"]))
        for nm in (prefix + "cs2", prefix + "cs2_out"):
            pl = lat_params(nm, "ProcessLatency")
            if any(v not in (0, 0.0, None) for x in pl for k, v in x.items() if k != "dir"):
                fail(_("cs2 ek işlem gecikmesi bildiriyor ({dugum}): {deger}").format(dugum=nm, deger=pl))
            ref = nm.replace(prefix + "cs2", prefix + FLAT_PROFILE)
            if lat_params(nm, "Latency") != lat_params(ref, "Latency"):
                fail(_("cs2 gecikmesi düz zincirden farklı: {cs2} ≠ {duz}").format(
                    cs2=lat_params(nm, "Latency"), duz=lat_params(ref, "Latency")))
            else:
                lin = [x for x in lat_params(nm, "Latency") if x["dir"] == "Input"]
                say("✓ " + _("cs2 ek gecikme bildirmiyor ({dugum}: ProcessLatency {pl}, Latency = "
                             "düz zincirle aynı, cihaza doğru {lin}); örnek-hassas ölçüm aşağıda "
                             "(PipeWire LV2 gecikmesini bildirmez)").format(
                    dugum=nm, pl=pl or _("yok"), lin=lin))
        if lsp and _dyn_used(profiles, "cs2", lsp):
            vals = read_live_params(ids[prefix + "cs2"])
            if "comp:sla" in vals:
                say("✓ " + _("LSP kompresör yüklendi (cs2 lookahead comp:sla = {sla} ms, eşik {esik}, "
                             "telafi {telafi})").format(sla=vals["comp:sla"], esik=vals.get("comp:al"),
                                                        telafi=vals.get("comp:mk")))
            else:
                fail(_("cs2 kompresör parametreleri görünmüyor (LV2 yüklenmemiş olabilir)"))

        for vp in VARIANT_PROFILES:
            vals = read_live_params(ids[prefix + vp])
            if all(f"v{i}_{c}:Gain" in vals for i in range(1, VARIANT_SLOTS + 1) for c in "lr"):
                say("✓ " + _("{profil}: {n} tür yuvası (v1..v{n}) hazır").format(profil=vp, n=VARIANT_SLOTS))
            else:
                fail(_("{profil}: tür yuvaları grafikte yok").format(profil=vp))
        for tp in EQ_PROFILES:
            vals = read_live_params(ids[prefix + tp])
            if all(f"k{i}_{c}:Gain" in vals for i in range(1, len(TONE_SLOTS) + 1) for c in "lr"):
                say("✓ " + _("{profil}: kişisel ton bantları (k1..k{n}) hazır").format(
                    profil=tp, n=len(TONE_SLOTS)))
            else:
                fail(_("{profil}: kişisel ton bantları grafikte yok").format(profil=tp))
            for key, msgs, used in (
                    ("lim:th", (N_("{profil}: sınırlayıcı yüklendi ({anahtar} = {deger:.4g}, lk {lk} ms, "
                                   "boost {boost})"),
                                N_("{profil}: sınırlayıcı parametreleri görünmüyor (LV2 yüklenmemiş "
                                   "olabilir)")), _lim_used(profiles, tp, lsp)),
                    ("ag:level", (N_("{profil}: seviye eşitleyici yüklendi ({anahtar} = {deger:.4g}, "
                                     "açık {acik})"),
                                  N_("{profil}: seviye eşitleyici parametreleri görünmüyor (LV2 "
                                     "yüklenmemiş olabilir)")), _ag_used(profiles, tp, lsp))):
                if used is None:
                    continue
                if key in vals:
                    say("✓ " + _(msgs[0]).format(profil=tp, anahtar=key, deger=vals[key],
                                                 lk=vals.get("lim:lk"), boost=vals.get("lim:boost"),
                                                 acik=vals.get("ag:enabled")))
                else:
                    fail(_(msgs[1]).format(profil=tp))
        xf = _xf_used(profiles, CROSSFEED_PROFILES[0])
        if xf is not None:
            xn = ids[prefix + CROSSFEED_PROFILES[0]]
            want = crossfeed_gains(xf)["cross"]
            if apply_crossfeed(True, ramp_s=0.0, profiles=profiles, prefix=prefix):
                wait_ramps(3)
                on = read_live_params(xn).get("xm_l:Gain 2")
                apply_crossfeed(None, ramp_s=0.0, profiles=profiles, prefix=prefix)
                wait_ramps(3)
                back = read_live_params(xn).get("xm_l:Gain 2")
                exp_back = want if xf["aktif"] else 0.0
                if (on is not None and abs(on - want) < 1e-3 and back is not None
                        and abs(back - exp_back) < 1e-3):
                    say("✓ " + _("crossfeed canlı aç/kapa çalışıyor (çapraz kazanç {acik:.3f} → "
                                 "{kapali:.3f}, yeniden başlatmasız)").format(acik=on, kapali=back))
                else:
                    fail(_("crossfeed canlı aç/kapa doğrulanamadı ({acik} / {kapali})").format(
                        acik=on, kapali=back))
            else:
                fail(_("crossfeed düğümleri grafikte yok"))
        # live update test: change one gain on the test sink, read it back, restore
        test_val = 1.25
        nid = ids[prefix + "muzik"]
        before = read_live_params(nid).get("b1_l:Gain")
        r = _run(["pw-cli", "set-param", str(nid), "Props",
                  f'{{ params = [ "b1_l:Gain" {test_val} "b1_r:Gain" {test_val} ] }}'])
        time.sleep(0.4)
        after = read_live_params(nid).get("b1_l:Gain")
        if r.returncode == 0 and after is not None and abs(after - test_val) < 1e-3:
            say("✓ " + _("canlı ayar çalışıyor (muzik b1 Gain {once} → {sonra})").format(
                once=before, sonra=after))
        else:
            fail(_("canlı ayar doğrulanamadı (önce {once}, sonra {sonra}, {hata})").format(
                once=before, sonra=after, hata=r.stderr.strip()))
        if set_live("muzik", profiles, prefix=prefix):
            vals = read_live_params(nid)
            exp = node_controls(profiles, "muzik", lsp=lsp)["b1_l"]["Gain"]
            if abs(vals.get("b1_l:Gain", 99) - exp) < 1e-3:
                say("✓ " + _("set_live() değerleri geri yükledi"))
            else:
                fail(_("set_live sonrası değer {deger} ≠ {beklenen}").format(
                    deger=vals.get("b1_l:Gain"), beklenen=exp))
        else:
            fail(_("set_live() başarısız"))
        logtxt = (work / "pipewire-test.log").read_text(errors="replace")
        errs = [ln for ln in logtxt.splitlines() if " E " in ln or "error" in ln.lower()]
        if errs:
            say(_("Günlükte hata satırları:") + "\n  " + "\n  ".join(errs[:10]))
    finally:
        for pl in players:
            if pl.poll() is None:
                pl.terminate()
            try:
                pl.wait(3)
            except subprocess.TimeoutExpired:
                pl.kill()
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(3)
        logf.close()
        gone = False
        for _i in range(20):
            if not any(n in _sink_names() for n in names):
                gone = True
                break
            time.sleep(0.25)
        if gone:
            say("✓ " + _("test süreci kapatıldı, sanal çıkışlar kaldırıldı"))
        else:
            ok = False
            say(_("HATA: {hata}").format(hata=_("test çıkışları hâlâ görünüyor!")))
    if olc and ok:
        say(_("Ölçüm: en kötü durum sinyali + gecikme + işlemci (ayrı test süreci, çıkışlar "
              "yalnız kayda bağlı — kulaklığa ses GİTMEZ) …"))
        try:
            res = olcum(profiles, work / "olcum", say)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            ok = False
            say(_("HATA: {hata}").format(hata=_("ölçüm yapılamadı: {hata}").format(hata=e)))
        else:
            olcum_report(res, profiles["genel"]["tavan_dbfs"], say)
            ok = ok and res["ok"]
        try:
            res = olcum_gercekci(profiles, work / "olcum", say)
        except (RuntimeError, OSError, subprocess.SubprocessError, ImportError) as e:
            say(_("Not: gerçekçi müzik ölçümü yapılamadı: {hata}").format(hata=e))
        else:
            gercekci_report(res, say)
            if not res["ok"]:
                ok = False
                say(_("HATA: {hata}").format(hata=_("gerçekçi müzikte çıkış tavanı aştı")))
    say((_("SONUÇ: BAŞARILI  (dosyalar: {yol})") if ok
         else _("SONUÇ: BAŞARISIZ  (dosyalar: {yol})")).format(yol=work))
    return ok


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


# `eqgen ozet` table: column widths follow the translated headers (terminal cells, CJK = 2)
def _ozet_sutunlar() -> tuple[list[str], list[int]]:
    heads = [_("profil"), _("tepe"), _("önkaz."), _("eşitl."), _("toplam"), _("enkötü")]
    return heads, [max(8, i18n.genislik(heads[0]))] + [max(7, i18n.genislik(h)) for h in heads[1:]]


def _ozet_baslik() -> str:
    heads, w = _ozet_sutunlar()
    return (i18n.sola(heads[0], w[0]) + "".join(" " + i18n.saga(h, x) for h, x in zip(heads[1:], w[1:]))
            + "  " + _("dinamik"))


def _ozet_satir(name: str, vals, dyn: str) -> str:
    _h, w = _ozet_sutunlar()
    return (i18n.sola(name, w[0]) + "".join(" " + i18n.saga(f"{v:+.2f}", x) for v, x in zip(vals, w[1:]))
            + "  " + dyn)

def _summary(profiles: dict) -> str:
    lsp = lsp_available()
    st = gain_staging(profiles, lsp)
    lines = [_("LSP kompresör eklentisi: var") if lsp else
             _("LSP kompresör eklentisi: YOK (yalnız EQ)"),
             _ozet_baslik()]
    for n in ALL_SINK_PROFILES:
        s = st[n]
        d = _dyn_active(profiles, n, lsp)
        parts = [_("{oran:g}:1 eşik {esik:g} dB, atak {atak:g} ms, telafi {telafi:+g} dB").format(
            oran=d["oran"], esik=d["esik_db"], atak=d["atak_ms"], telafi=d["telafi_db"])
                 if d else "-"]
        if n == FLAT_PROFILE:
            parts = [_("düz geçiş (EQ yok), seviye eşitli") if profiles[n]["aktif"]
                     else _("düz, 0 dB")]
        if n != FLAT_PROFILE:
            parts.append(_("kişisel ton payı {db:+.2f} dB").format(db=s["tone_peak_db"] - s["peak_db"])
                         if tone_enabled(profiles, n) else _("kişisel ton kapalı"))
            lb = _lim_used(profiles, n, lsp)
            if lb is not None:
                gr = max([s["limiter_gr_db"]] + [v["limiter_gr_db"]
                                                 for v in s.get("variants", {}).values()])
                parts.append(_("sınırlayıcı {ms:g} ms (kuramsal en kötü: tam ölçek + ton +3 + "
                               "kompresör atağı → {db:.1f} dB kısar; gerçek müzikte ~0–1.5 dB)")
                             .format(ms=lb["ileri_bakis_ms"], db=gr) if lb["aktif"]
                             else _("sınırlayıcı kapalı"))
            else:
                parts.append(_("sınırlayıcı yok (0 gecikme)"))
            ab = _ag_used(profiles, n, lsp)
            if ab is not None:
                parts.append(_("seviye eşitleyici → {lufs:g} LUFS (en çok +{db:g} dB, "
                               "{yukselme:g}/{dusme:g} dB/sn)").format(
                    lufs=ab["hedef_lufs"], db=ab["en_cok_yukseltme_db"],
                    yukselme=ab["yukselme_db_sn"], dusme=ab["dusme_db_sn"])
                             if _ag_active(profiles, n, lsp) else _("seviye eşitleyici kapalı"))
            xb = _xf_used(profiles, n)
            if xb is not None:
                parts.append(_("crossfeed {db:g} dB / {hz:g} Hz").format(
                    db=xb["seviye_db"], hz=xb["kesim_hz"])
                             if _xf_active(profiles, n) else _("crossfeed kapalı"))
        dyn = "; ".join(parts)
        lines.append(_ozet_satir(n, (s["peak_db"], s["preamp_db"], s["match_db"],
                                     s["preamp_db"] + s["match_db"], s["worst_peak_dbfs"]), dyn))
        for grp, v in s.get("variants", {}).items():
            vf = ", ".join(f"{f['tip']} {f['fc']:g} {f['kazanc']:+g}"
                           for f in variant_filters(profiles, n, grp))
            lines.append(f"  └ {grp:18s} {v['peak_db']:+6.2f}         {v['match_db']:+7.2f} "
                         f"{v['preamp_db'] + v['match_db']:+7.2f} {v['worst_peak_dbfs']:+7.2f}  {vf}")
    extra = -st["cs2"]["safety_db"]
    lines.append(_("tavan {tavan:+.1f} dBFS; ek güvenlik kısması {db:.2f} dB (tüm çıkışlara eşit); "
                   "'enkötü' = sınırlayıcıdan ÖNCEKİ statik en kötü tepe (sınırlayıcılı çıkışlarda "
                   "kulağa giden ≤ tavan)").format(tavan=profiles["genel"]["tavan_dbfs"], db=extra))
    common = min(st[n]["final_loudness_db"] for n in EQ_PROFILES)
    lines.append(_("ortak eşit seviye (tüm EQ çıkışları; {duz} {duz_db:+.2f} dB): {db:+.2f} dB").format(
        duz=PROFILE_LABELS[FLAT_PROFILE], duz_db=st[FLAT_PROFILE]["match_db"], db=common))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m oto_eq.eqgen",
                                 description=_("{ad} EQ motoru").format(ad=APP_NAME))
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", help=h_(_("sanal çıkış yapılandırmasını üret")))
    g.add_argument("-o", "--output", type=Path, default=config.SINKS_CONF)
    g.add_argument("--stdout", action="store_true", help=h_(_("dosyaya değil ekrana yaz")))
    p = sub.add_parser("plot", help=h_(_("EQ eğrilerini çiz (PNG)")))
    p.add_argument("-o", "--output", type=Path, default=config.PLOT_FILE)
    c = sub.add_parser("check", help=h_(_("test pipewire süreciyle canlı doğrula + en kötü durum "
                                       "ölçümü (kulaklığa ses gitmez)")))
    c.add_argument("--dir", type=Path, default=None, help=h_(_("test dosyalarının dizini")))
    c.add_argument("--olcumsuz", action="store_true",
                   help=h_(_("en kötü durum / gecikme / işlemci ölçümünü atla")))
    sub.add_parser("ozet", help=h_(_("profil kazanç özetini göster")))
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        profiles = load_profiles()
    except (ProfileError, OSError) as e:
        print(_("{dosya} okunamadı: {hata}").format(dosya="profiller.toml", hata=e), file=sys.stderr)
        return 2
    except Exception as e:  # tomllib.TOMLDecodeError
        print(_("{dosya} sözdizimi hatası: {hata}").format(dosya="profiller.toml", hata=e), file=sys.stderr)
        return 2
    if a.cmd == "generate":
        if a.stdout:
            print(generate_conf(profiles))
        else:
            print(_("Yazıldı: {yol}").format(yol=write_conf(a.output, profiles)))
        print(_summary(profiles), file=sys.stderr if a.stdout else sys.stdout)
        return 0
    if a.cmd == "plot":
        print(_("Grafik: {yol}").format(yol=plot(a.output, profiles)))
        return 0
    if a.cmd == "ozet":
        print(_summary(profiles))
        return 0
    if a.cmd == "check":
        print(_summary(profiles))
        return 0 if check(a.dir, olc=not a.olcumsuz) else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
