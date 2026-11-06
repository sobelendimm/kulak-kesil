#!/usr/bin/env python3
"""Çeviri kataloglarını denetler (CHANGE 8): kapsama, yer tutucular, uzunluk, eski girdiler.

    python3 tools/i18n_denetle.py                    # tüm diller; sorun varsa çıkış 1
    python3 tools/i18n_denetle.py --diller en,de --ayrinti
    python3 tools/i18n_denetle.py --min-kapsama 100  # eksik çeviri de sorun sayılsın

ERRORS (exit 1): missing/broken catalog file, placeholder multiset ≠ msgid's, a value
glued to a word or list-like placeholders reordered (``yerlesim_sorunu``), a config name
(``[genel]``, ``genel.mod``, ``node.passive``: i18n_cikar.korunan_re) lost, entries no
longer in kaynak.json (stale), coverage below ``--min-kapsama`` (default 0 = report only),
over-long translations (> ``--oran`` × msgid length and ≥ ``--min-fark`` chars longer:
the UI may overflow). Edge whitespace and end punctuation (…, :) that differ from the
msgid are WARNINGS (printed, exit 0) unless ``--kati``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_cikar import (DILLER, KATALOG_DIZINI, KAYNAK_DOSYASI, YUZDE_RE,  # noqa: E402
                        kaynak_oku, korunan_adlar, korunan_re, yer_tutucular)

UZUN_ORAN = 2.5
UZUN_MIN_FARK = 10
_UC = ("…", ":", "·", "→")
_UC_ES = {":": (":", "："), "…": ("…", "...")}


_ALAN_RE = re.compile(r"\{\{|\}\}|\{[^{}]*\}")
_CJK = ("zh", "ja")
# letters allowed glued after a value (ordinals, "2x"): "{n}th", "{n}e", "{n}º"
_SONEK_IZIN = {"en": ("st", "nd", "rd", "th", "x"), "fr": ("e", "er", "re", "ère", "x"),
               "es": ("º", "ª", "x"), "pt": ("º", "ª", "x"), "de": ("x",), "ru": ("x",)}


def _konumlar(s: str) -> list[tuple[int, int, str]]:
    out = [(m.start(), m.end(), m.group(0)) for m in _ALAN_RE.finditer(s)
           if m.group(0) not in ("{{", "}}")]
    out += [(m.start(), m.end(), m.group(0)) for m in YUZDE_RE.finditer(s) if m.group(0) != "%%"]
    return sorted(out)


def _liste_benzeri(msgid: str, k: list[tuple[int, int, str]]) -> bool:
    """Label–value sequences ("bas {a}  vokal {b}", "x: {a} · y: {b}", "{a} ok, {b} uyarı")."""
    if len(k) < 2:
        return False
    if " · " in msgid or "  " in msgid or " | " in msgid:
        return True
    return any(", " in msgid[e1:s2] or " / " in msgid[e1:s2]
               for (_s1, e1, _t1), (s2, _e2, _t2) in zip(k, k[1:]))


def yerlesim_sorunu(msgid: str, ceviri: str, dil: str) -> str | None:
    """A value that would be glued to a word ("vocals{x}" → "vocals7", "OK{n}") or
    placeholders reordered in a list-like msgid — the multiset check cannot see these.
    None when fine, else a short Turkish reason."""
    k, c = _konumlar(msgid), _konumlar(ceviri)
    if not c:
        return None
    if dil not in _CJK:
        kaynak_yapisik: dict[str, list[bool]] = {}
        for s, e, t in k:
            y = kaynak_yapisik.setdefault(t, [False, False])
            y[0] = y[0] or (s > 0 and msgid[s - 1].isalpha())
            y[1] = y[1] or (e < len(msgid) and msgid[e].isalpha())
        for s, e, t in c:
            sol_ok, sag_ok = kaynak_yapisik.get(t, [False, False])
            if s > 0 and ceviri[s - 1].isalpha() and not sol_ok:
                return f"{t} önündeki sözcüğe yapışık: {ceviri[max(0, s - 15):e]!r}"
            if e < len(ceviri) and ceviri[e].isalpha() and not sag_ok:
                ek = re.match(r"[^\W\d_]+", ceviri[e:]).group(0)
                if ek.lower() not in _SONEK_IZIN.get(dil, ()):
                    return f"{t} ardındaki sözcüğe yapışık: {ceviri[s:e + 15]!r}"
    if _liste_benzeri(msgid, k) and [t for *_x, t in k] != [t for *_x, t in c]:
        return (f"liste sırası değişti: {' '.join(t for *_x, t in k)} → "
                f"{' '.join(t for *_x, t in c)}")
    return None


@dataclass
class DilSonucu:
    dil: str
    toplam: int = 0
    cevrilmis: int = 0
    dosya_hatasi: str = ""
    yer_tutucu: list[tuple[str, str]] = field(default_factory=list)   # (msgid, neden)
    uzun: list[tuple[str, str]] = field(default_factory=list)
    yerlesim: list[tuple[str, str]] = field(default_factory=list)   # glued / reordered
    korunan: list[tuple[str, str]] = field(default_factory=list)    # config name lost
    eski: list[str] = field(default_factory=list)
    uyari: list[tuple[str, str]] = field(default_factory=list)

    @property
    def kapsama(self) -> float:
        return 100.0 * self.cevrilmis / self.toplam if self.toplam else 100.0


def dil_denetle(dil: str, kaynak: dict, dizin: Path, oran: float = UZUN_ORAN,
                min_fark: int = UZUN_MIN_FARK) -> DilSonucu:
    r = DilSonucu(dil, toplam=len(kaynak))
    p = Path(dizin) / f"{dil}.json"
    try:
        kat = json.loads(p.read_text("utf-8"))
        if not isinstance(kat, dict):
            raise ValueError("JSON nesnesi değil")
    except FileNotFoundError:
        r.dosya_hatasi = "katalog yok"
        return r
    except (OSError, ValueError) as e:
        r.dosya_hatasi = f"katalog okunamadı: {e}"
        return r
    r.eski = sorted(k for k in kat if k not in kaynak)
    desen = korunan_re()
    for msgid in kaynak:
        ceviri = kat.get(msgid)
        if not isinstance(ceviri, str) or not ceviri:
            continue
        r.cevrilmis += 1
        try:
            if yer_tutucular(ceviri) != yer_tutucular(msgid):
                r.yer_tutucu.append((msgid, f"{dict(yer_tutucular(msgid))} ≠ {dict(yer_tutucular(ceviri))}"))
        except ValueError as e:
            r.yer_tutucu.append((msgid, f"sözdizimi bozuk: {e}"))
        sorun = yerlesim_sorunu(msgid, ceviri, dil)
        if sorun:
            r.yerlesim.append((msgid, sorun))
        kayip = korunan_adlar(msgid, desen) - korunan_adlar(ceviri, desen)
        if kayip:
            r.korunan.append((msgid, f"{', '.join(sorted(kayip))} çeviride yok: {ceviri!r}"))
        if len(ceviri) > oran * len(msgid) and len(ceviri) - len(msgid) >= min_fark:
            r.uzun.append((msgid, f"{len(ceviri)}/{len(msgid)} karakter: {ceviri!r}"))
        if (msgid[:1].isspace(), msgid[-1:].isspace()) != (ceviri[:1].isspace(), ceviri[-1:].isspace()):
            r.uyari.append((msgid, "baştaki/sondaki boşluk msgid ile aynı değil"))
        for u in _UC:
            if msgid.rstrip().endswith(u) and not ceviri.rstrip().endswith(_UC_ES.get(u, (u,))):
                r.uyari.append((msgid, f"msgid '{u}' ile bitiyor, çeviri değil: {ceviri!r}"))
    return r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="i18n kataloglarını denetler.")
    ap.add_argument("--kaynak", type=Path, default=KAYNAK_DOSYASI)
    ap.add_argument("--dizin", type=Path, default=KATALOG_DIZINI)
    ap.add_argument("--diller", default=",".join(DILLER))
    ap.add_argument("--min-kapsama", type=float, default=0.0, help="yüzde; altı = sorun")
    ap.add_argument("--oran", type=float, default=UZUN_ORAN)
    ap.add_argument("--min-fark", type=int, default=UZUN_MIN_FARK)
    ap.add_argument("--kati", action="store_true", help="uyarılar da sorun sayılır")
    ap.add_argument("--ayrinti", action="store_true", help="her sorunlu girdiyi listele")
    a = ap.parse_args(argv)
    try:
        kaynak = kaynak_oku(a.kaynak)
    except (OSError, ValueError) as e:
        print(f"HATA: kaynak okunamadı ({e}); önce: python3 tools/i18n_cikar.py", file=sys.stderr)
        return 1
    sorun = False
    print(f"{'dil':4} {'kapsama':>8} {'yer t.':>6} {'yerleşim':>8} {'ad':>4} {'uzun':>5} "
          f"{'eski':>5} {'uyarı':>6}")
    for dil in [d.strip() for d in a.diller.split(",") if d.strip()]:
        r = dil_denetle(dil, kaynak, a.dizin, a.oran, a.min_fark)
        if r.dosya_hatasi:
            print(f"{dil:4} {r.dosya_hatasi}")
            sorun = True
            continue
        print(f"{dil:4} {r.kapsama:7.1f}% {len(r.yer_tutucu):6} {len(r.yerlesim):8} "
              f"{len(r.korunan):4} {len(r.uzun):5} {len(r.eski):5} "
              f"{len(r.uyari):6}")
        if r.yer_tutucu or r.yerlesim or r.korunan or r.uzun or r.eski or r.kapsama < a.min_kapsama or (a.kati and r.uyari):
            sorun = True
        if a.ayrinti:
            for m, n in r.yer_tutucu:
                print(f"  [{dil}] YER TUTUCU {m!r}: {n}")
            for m, n in r.yerlesim:
                print(f"  [{dil}] YERLEŞİM {m!r}: {n}")
            for m, n in r.korunan:
                print(f"  [{dil}] AD {m!r}: {n}")
            for m, n in r.uzun:
                print(f"  [{dil}] UZUN {m!r}: {n}")
            for m in r.eski:
                print(f"  [{dil}] ESKİ {m!r}")
            for m, n in r.uyari:
                print(f"  [{dil}] uyarı {m!r}: {n}")
    print("SORUN VAR" if sorun else "temiz")
    return 1 if sorun else 0


if __name__ == "__main__":
    sys.exit(main())
