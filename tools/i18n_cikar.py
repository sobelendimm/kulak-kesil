#!/usr/bin/env python3
"""Arayüz metinlerini çıkarır (CONTRACT CHANGE 8): ``_("…")`` / ``N_("…")`` → kaynak.json.

    python3 tools/i18n_cikar.py              # oto_eq/ tara → oto_eq/i18n/kaynak.json
    python3 tools/i18n_cikar.py --denetle    # yalnız denetle: f-string / birleştirme /
                                             # .format()'lı _() + bozuk yer tutucular → çıkış 1
    python3 tools/i18n_cikar.py --denetle --ayrinti   # + dinamik (değişkenli) çağrılar listesi
    python3 tools/i18n_cikar.py --kok DIR --cikti FILE

The scan is pure AST (nothing is imported or executed). A constant first argument is a
msgid (implicitly concatenated literals are already one constant for the parser); an
f-string, a "+"/"%" built text or ``_("…".format())`` is an ERROR (the msgid would be
the formatted text); a plain variable / attribute / call argument (``_(MIC_LABEL)``,
``_(tx.mode_text(st))``) is the documented N_()-at-use pattern → reported as INFO only.
Output: {msgid: ["dosya.py:satır", …]} sorted by msgid, locations sorted → stable diffs.

This module is stdlib-only on purpose: i18n_cevir.py (venv python) and i18n_denetle.py
import its helpers (``yer_tutucular``, ``DILLER``, ``kaynak_oku``).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import string
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAKET = ROOT / "oto_eq"
KATALOG_DIZINI = PAKET / "i18n"
KAYNAK_DOSYASI = KATALOG_DIZINI / "kaynak.json"

# Target languages (oto_eq/i18n.py DILLER without the Turkish source; a test keeps the two
# in sync — duplicated so the tools never import the app package).
DILLER = ("en", "de", "es", "fr", "pt", "ru", "zh", "ja")
ISLEVLER = ("_", "N_")

_FMT = string.Formatter()
# printf-style tokens: %s %d %(ad)s %5.1f %% … — not followed by a letter, so Turkish
# "%50" / "%{n}" (percent sign before the number) is NOT a placeholder.
YUZDE_RE = re.compile(r"%(?:\([^)]+\))?[-#0 +]*\d*(?:\.\d+)?[sdifrxXeEgGc%]"
                      r"(?![A-Za-zÇĞİÖŞÜçğıöşü])")


CONFIG_DIZINI = ROOT / "config"
# PipeWire property namespaces ("node.passive", "state.restore-props", "media.class" …)
PW_ONEKLER = ("node", "state", "media", "audio", "priority", "target", "api", "device",
              "application", "object", "session", "filter", "stream", "factory")


def toml_bolumleri(dizin: Path = CONFIG_DIZINI) -> frozenset[str]:
    """Every component of the ``[section]`` headers of the default config/*.toml
    (genel, duz, dinamik, mod, uygulamalar, profil, tur …). These are file syntax the
    user types/looks up: a translation must keep them verbatim (1 Kas: "[uygulamalar.sesli]"
    had become "[apps.audio]", "genel.mod" "general.mod")."""
    adlar: set[str] = set()
    for f in sorted(Path(dizin).glob("*.toml")):
        try:
            txt = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r"^[ \t]*\[+[ \t]*([A-Za-z0-9_.]+)[ \t]*\]+", txt, re.M):
            adlar.update(p for p in m.group(1).split(".") if p)
    return frozenset(adlar)


# Default-config values the UI shows (profile/genre "aciklama" + "odak"). They are stored
# as Turkish msgids (language independent) and translated at display time with _(text):
# a user's own edited text has no catalog entry and is shown as written.
METIN_DOSYALARI = ("profiller.toml",)
METIN_ANAHTARLARI = ("aciklama", "odak")
_METIN_SATIRI = re.compile(rf"^[ \t]*(?:{'|'.join(METIN_ANAHTARLARI)})[ \t]*=")


def toml_metinleri(dizin: Path = CONFIG_DIZINI) -> list[tuple[str, str]]:
    """(msgid, "config/<dosya>:<satır>") for every non-empty ``aciklama``/``odak`` string
    of the default config files in METIN_DOSYALARI."""
    import tomllib
    out: list[tuple[str, str]] = []
    for ad in METIN_DOSYALARI:
        f = Path(dizin) / ad
        try:
            satirlar = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for n, satir in enumerate(satirlar, 1):
            if not _METIN_SATIRI.match(satir):
                continue
            try:
                (deger,) = tomllib.loads(satir.strip()).values()
            except (tomllib.TOMLDecodeError, ValueError):
                continue
            if isinstance(deger, str) and deger.strip():
                out.append((deger, f"config/{ad}:{n}"))
    return out


def korunan_re(bolumler: frozenset[str] | None = None) -> re.Pattern:
    """Config names that must survive translation: ``[genel]``, ``[mod.X]``,
    ``[uygulamalar.sesli]`` (bracketed, first part a TOML section name), dotted keys
    ``genel.mod`` / ``uygulamalar.muzik`` and PipeWire properties ``node.passive``.
    Metavars like ``[saniye]`` / ``[dosya]`` are not section names → still translated."""
    b = sorted(toml_bolumleri() if bolumler is None else bolumler, key=len, reverse=True)
    bol = "|".join(map(re.escape, b)) or r"(?!)"
    pw = "|".join(PW_ONEKLER)
    return re.compile(rf"\[(?:{bol})(?:\.[A-Za-z0-9_]+)*\]"
                      rf"|(?<![\w.\-])(?:{bol}|{pw})(?:\.[a-z0-9_][a-z0-9_\-]*)+(?![\w.\-])")


def korunan_adlar(text: str, desen: re.Pattern | None = None) -> Counter:
    """Multiset of the protected config names in ``text`` (see korunan_re)."""
    return Counter(m.group(0) for m in (desen or korunan_re()).finditer(text))


def bicim_alanlari(text: str) -> list[str]:
    """str.format replacement fields as written ("{ad}", "{db:+.1f}", "{x!r}").

    Raises ValueError on syntax str.format cannot parse ("{ad", "a}b")."""
    out = []
    for _lit, field, spec, conv in _FMT.parse(text):
        if field is None:
            continue
        tok = "{" + field + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}"
        out.append(tok)
        if spec and "{" in spec:              # nested field in a spec: validate it too
            bicim_alanlari(spec)
    return out


def yer_tutucular(text: str) -> Counter:
    """Multiset of every placeholder a translation must keep: {…} fields + %-tokens.

    Unparseable brace syntax → ValueError (callers report it)."""
    c = Counter(bicim_alanlari(text))
    c.update(m.group(0) for m in YUZDE_RE.finditer(text) if m.group(0) != "%%")
    return c


def yer_tutucu_hatasi(text: str) -> str | None:
    """None when the msgid is fine, else a short reason (Turkish, for the report)."""
    try:
        alanlar = bicim_alanlari(text)
    except ValueError as e:
        return f"yer tutucu sözdizimi bozuk: {e}"
    for tok in alanlar:
        name = re.split(r"[!:.\[]", tok[1:-1], maxsplit=1)[0]
        if name == "" or name.isdigit():
            return f"konumsal yer tutucu {tok} (adlandırılmış olmalı: {{ad}})"
    return None


@dataclass
class Uyari:
    yer: str          # "dosya.py:satır"
    neden: str
    kod: str = ""
    ciddi: bool = True   # False = dynamic argument (info only, not an error)

    def __str__(self) -> str:
        return f"{self.yer}: {self.neden}" + (f"  → {self.kod}" if self.kod else "")


def _islev_adi(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):        # i18n._("…")
        return node.attr
    return None


def _metin_insasi(node: ast.AST) -> bool:
    """"a" + b, "…%s" % x, "…".format(…), f-strings inside → a built text, not a msgid."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return any(isinstance(n, (ast.Constant, ast.JoinedStr)) and
                   isinstance(getattr(n, "value", ""), str) for n in (node.left, node.right)) \
            or _metin_insasi(node.left) or _metin_insasi(node.right)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ("format", "format_map", "join"):
        return isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
    return False


def dosya_tara(path: Path, goreli: str) -> tuple[list[tuple[str, str]], list[Uyari]]:
    """[(msgid, "dosya.py:satır")], [Uyari] of one file."""
    try:
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as e:
        return [], [Uyari(goreli, f"dosya ayrıştırılamadı: {e}")]
    bulunan, uyarilar = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _islev_adi(node.func) not in ISLEVLER:
            continue
        yer = f"{goreli}:{node.lineno}"
        ad = _islev_adi(node.func)
        if len(node.args) != 1 or node.keywords:
            uyarilar.append(Uyari(yer, f"{ad}() tam olarak bir metin argümanı almalı",
                                  ast.unparse(node)[:80]))
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if not arg.value.strip():
                uyarilar.append(Uyari(yer, f"{ad}() boş metin"))
                continue
            bulunan.append((arg.value, yer))
        elif isinstance(arg, ast.JoinedStr):
            uyarilar.append(Uyari(yer, f"{ad}() f-string ile çağrılmış (çıkarılamaz; "
                                       '_("… {ad} …").format(ad=…) kullan)', ast.unparse(arg)[:80]))
        elif _metin_insasi(arg):
            uyarilar.append(Uyari(yer, f"{ad}() içinde metin birleştirme/biçimleme (msgid "
                                       "biçimlenmiş metin olur; .format() _() DIŞINDA olmalı)",
                                  ast.unparse(arg)[:80]))
        else:
            uyarilar.append(Uyari(yer, f"{ad}() dinamik argüman (çıkarılmaz; N_() ile "
                                       "işaretli olmalı)", ast.unparse(arg)[:80], ciddi=False))
    return bulunan, uyarilar


def tara(kok: Path = PAKET, toml_dizin: Path | None = None
         ) -> tuple[dict[str, list[str]], list[Uyari]]:
    """Scan every .py under ``kok`` (+ the shown default-config texts of ``toml_dizin``,
    see toml_metinleri) → ({msgid: sorted locations}, warnings)."""
    kok = Path(kok)
    msgs: dict[str, set[str]] = {}
    uyarilar: list[Uyari] = []
    for path in sorted(kok.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        goreli = path.relative_to(kok).as_posix()
        bulunan, u = dosya_tara(path, goreli)
        uyarilar.extend(u)
        for msgid, yer in bulunan:
            msgs.setdefault(msgid, set()).add(yer)
    if toml_dizin is not None:
        for msgid, yer in toml_metinleri(toml_dizin):
            msgs.setdefault(msgid, set()).add(yer)

    def yer_anahtari(y: str):
        f, _, n = y.rpartition(":")
        return f, int(n) if n.isdigit() else 0

    sonuc = {m: sorted(msgs[m], key=yer_anahtari) for m in sorted(msgs)}
    for msgid, yerler in sonuc.items():
        h = yer_tutucu_hatasi(msgid)
        if h:
            uyarilar.append(Uyari(yerler[0], h, repr(msgid)[:80]))
    return sonuc, uyarilar


def json_yaz(path: Path, data) -> None:
    """Atomic UTF-8 JSON write (readable, stable)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def kaynak_oku(path: Path = KAYNAK_DOSYASI) -> dict[str, list[str]]:
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: JSON nesnesi bekleniyordu")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="_()/N_() metinlerini kaynak.json'a çıkarır.")
    ap.add_argument("--kok", type=Path, default=PAKET, help="taranacak paket dizini")
    ap.add_argument("--cikti", type=Path, default=KAYNAK_DOSYASI, help="kaynak.json yolu")
    ap.add_argument("--denetle", action="store_true",
                    help="yazma; çıkarılamayan çağrılar/bozuk yer tutucular varsa çıkış 1")
    ap.add_argument("--ayrinti", action="store_true", help="dinamik çağrıları da listele")
    ap.add_argument("--config", type=Path, default=None,
                    help="görünen aciklama/odak metinleri için varsayılan config dizini "
                         "(varsayılan: --kok oto_eq ise config/)")
    a = ap.parse_args(argv)
    toml_dizin = a.config or (CONFIG_DIZINI if Path(a.kok).resolve() == PAKET else None)
    msgs, uyarilar = tara(a.kok, toml_dizin)
    ciddi = [u for u in uyarilar if u.ciddi]
    bilgi = [u for u in uyarilar if not u.ciddi]
    for u in ciddi:
        print(f"HATA {u}", file=sys.stderr)
    if a.ayrinti:
        for u in bilgi:
            print(f"BİLGİ {u}", file=sys.stderr)
    if a.denetle:
        print(f"{len(msgs)} metin, {len(ciddi)} sorun, {len(bilgi)} dinamik çağrı (bilgi)")
        return 1 if ciddi else 0
    json_yaz(a.cikti, msgs)
    print(f"{len(msgs)} metin → {a.cikti} ({len(ciddi)} sorun, {len(bilgi)} dinamik çağrı)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
