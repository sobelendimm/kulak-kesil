"""Kişisel ton: the user's own three-band taste on top of every EQ profile.

Shared model (CHANGE 5, docs/CONTRACT.md "Kişisel ton"): three LIVE bands per
EQ sink, after the profile/variant bands (see ``eqgen``):

* k1 ``bas``       LSC 120 Hz  q 0.7  (Bas sıcaklığı)
* k2 ``vokal``     PK  2500 Hz q 0.8  (Vokal netliği)
* k3 ``parlaklik`` HSC 8000 Hz q 0.7  (Parlaklık)

Storage ``~/.config/oto-eq/kisisel.json``::

    {"genel": {"bas": 0.0, "vokal": 0.0, "parlaklik": 0.0},
     "tur": {"muzik:elektronik": {"bas": 1.0}, "oyun:rekabetci": {...}}}

effective = clamp(genel + tur["<profile>:<group>"], ±TON_LIMIT). Writes are
atomic (temp file + replace). Reading never raises: a missing or broken file
means "everything 0 dB". The per-profile opt-out (``kisisel_ton`` in
profiller.toml, cs2 off by default) is applied by ``eqgen``, not here.
"""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path

from . import config
from .i18n import _
from .models import VARIANT_PROFILES, variant_groups

log = logging.getLogger("oto_eq.kisisel")

TON_KEYS: tuple[str, ...] = ("bas", "vokal", "parlaklik")
TON_LIMIT = 3.0          # ± dB per band: genel values and the EFFECTIVE value
# genre offsets (tur[...]) may reach ±2·TON_LIMIT so any effective target is
# reachable from any genel value (genel +3, wanted −3 → tur −6); the ±TON_LIMIT
# limit is applied to effective() only.
TUR_LIMIT = 2 * TON_LIMIT
TON_STEP = 0.5           # UI / CLI step (dB); storage keeps 0.01 dB (A/B estimates)
# key -> (tip, fc, q); band node names in the graph are k1..k3 in TON_KEYS order
TON_BANDS: dict[str, tuple[str, float, float]] = {
    "bas": ("LSC", 120.0, 0.7),
    "vokal": ("PK", 2500.0, 0.8),
    "parlaklik": ("HSC", 8000.0, 0.7),
}
TON_LABELS = {"bas": _("Bas sıcaklığı"), "vokal": _("Vokal netliği"), "parlaklik": _("Parlaklık")}
FILE_NAME = "kisisel.json"

# Tests may pin the file here; None = config.CONFIG_DIR / FILE_NAME at CALL time
# (so a test that patches config.CONFIG_DIR never touches the real file).
KISISEL_FILE: Path | None = None


def path() -> Path:
    """Where kisisel.json lives (resolved at call time)."""
    return Path(KISISEL_FILE) if KISISEL_FILE is not None else config.CONFIG_DIR / FILE_NAME


def zero() -> dict[str, float]:
    return {k: 0.0 for k in TON_KEYS}


def clamp(v, step: float | None = None, limit: float = TON_LIMIT) -> float:
    """Number → dB clamped to ±limit (default TON_LIMIT); rounded to ``step`` (0.01 dB)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        raise ValueError(_("sayı bekleniyordu: {deger}").format(deger=repr(v)))
    v = max(-limit, min(limit, float(v)))
    s = step or 0.01
    v = round(round(v / s) * s, 2)
    return 0.0 if v == 0 else v          # no "-0.0"


def _check_keys(vals: dict) -> None:
    bad = [k for k in vals if k not in TON_KEYS]
    if bad:
        raise ValueError(_("bilinmeyen ton ayarı: {ayar} (olanlar: {olanlar})").format(
            ayar=", ".join(bad), olanlar=", ".join(TON_KEYS)))


def tur_key(profile: str, group: str) -> str:
    """'muzik:elektronik'. Raises ValueError for a profile/group without variants."""
    if profile not in VARIANT_PROFILES:
        raise ValueError(_("'{profil}' için tür yok (yalnız {olanlar})").format(
            profil=profile, olanlar=", ".join(VARIANT_PROFILES)))
    if not group:
        raise ValueError(_("tür boş: Genel ayar için set_genel kullanın"))
    if group not in variant_groups(profile):
        raise ValueError(_("{profil}: bilinmeyen tür '{tur}' (olanlar: {olanlar})").format(
            profil=profile, tur=group, olanlar=", ".join(variant_groups(profile))))
    return f"{profile}:{group}"


def _norm_vals(raw, limit: float = TON_LIMIT) -> dict[str, float]:
    out = zero()
    if isinstance(raw, dict):
        for k in TON_KEYS:
            try:
                out[k] = clamp(raw.get(k, 0.0), limit=limit)
            except ValueError:
                log.warning("kisisel.json: '%s' değeri geçersiz (%r), 0 sayıldı", k, raw.get(k))
    return out


def normalize(data) -> dict:
    """Any parsed JSON → {"genel": {3 keys}, "tur": {"p:g": {3 keys}}} (invalid parts dropped)."""
    data = data if isinstance(data, dict) else {}
    out = {"genel": _norm_vals(data.get("genel")), "tur": {}}
    tur = data.get("tur")
    if isinstance(tur, dict):
        for key, vals in tur.items():
            prof, _sep, grp = str(key).partition(":")
            try:
                tur_key(prof, grp)
            except ValueError:
                log.warning("kisisel.json: bilinmeyen tür anahtarı '%s' yok sayıldı", key)
                continue
            v = _norm_vals(vals, TUR_LIMIT)
            if any(v.values()):
                out["tur"][f"{prof}:{grp}"] = v
    return out


def load(p: Path | None = None) -> dict:
    """Read kisisel.json (normalised). Missing/broken file → all zeros; never raises."""
    p = Path(p) if p else path()
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as e:
        log.warning("kisisel.json okunamadı (%s): ton 0 dB sayılıyor", e)
        raw = {}
    return normalize(raw)


def save(data: dict, p: Path | None = None) -> Path:
    """Normalise and write atomically (temp file in the same dir + os.replace)."""
    p = Path(p) if p else path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(normalize(data), ensure_ascii=False, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".kisisel-", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def effective(data: dict, profile: str, group: str = "") -> dict[str, float]:
    """clamp(genel + tur[profile:group], ±TON_LIMIT) for each of TON_KEYS."""
    data = normalize(data)
    extra = data["tur"].get(f"{profile}:{group}", {}) if group else {}
    return {k: clamp(data["genel"][k] + extra.get(k, 0.0)) for k in TON_KEYS}


def set_genel(*, path: Path | None = None, **vals) -> dict:
    """Update (merge) the general values, save, return the new data."""
    _check_keys(vals)
    data = load(path)
    for k, v in vals.items():
        data["genel"][k] = clamp(v)
    save(data, path)
    return data


def set_tur(profile: str, group: str, *, path: Path | None = None, **vals) -> dict:
    """Update (merge) the offsets of one genre ('muzik','elektronik'), save, return data.

    Offsets are stored within ±TUR_LIMIT (±6 dB); only effective() is limited
    to ±TON_LIMIT. An entry whose three values are all 0 is removed.
    """
    _check_keys(vals)
    key = tur_key(profile, group)
    data = load(path)
    cur = dict(data["tur"].get(key) or zero())
    for k, v in vals.items():
        cur[k] = clamp(v, limit=TUR_LIMIT)
    if any(cur.values()):
        data["tur"][key] = cur
    else:
        data["tur"].pop(key, None)
    save(data, path)
    return data


def reset_genel(*, path: Path | None = None) -> dict:
    return set_genel(path=path, **zero())


def reset_tur(profile: str, group: str, *, path: Path | None = None) -> dict:
    return set_tur(profile, group, path=path, **zero())


def reset_all(*, path: Path | None = None) -> dict:
    data = {"genel": zero(), "tur": {}}
    save(data, path)
    return normalize(data)
