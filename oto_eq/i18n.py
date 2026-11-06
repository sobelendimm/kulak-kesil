"""Arayüz dili (CONTRACT CHANGE 8): Türkçe kaynak metinler + 8 çeviri.

    from .i18n import _
    label(_("Kulaklık aktif değil"))
    _("Kaydedildi: {ad} → {tur}").format(ad=ad, tur=tur)

The source language is Turkish: the msgid IS the Turkish text. Catalogs live in
``oto_eq/i18n/<kod>.json`` = {"Türkçe metin": "çeviri", …}; a missing or empty
entry falls back to the Turkish text, so an incomplete catalog never breaks the UI.
Placeholders are named (``{ad}``) and must survive translation (tools/i18n_cevir.py
checks it).

Language: ``~/.config/oto-eq/arayuz.json`` {"dil": "otomatik" | "tr" | "en" | …};
"otomatik" = LC_ALL / LC_MESSAGES / LANG (tr → Türkçe, a supported language → it,
anything else → English). ``OTO_EQ_DIL`` overrides both (tests force "tr").
The language is read once per process (lazily, on the first ``_()``): module-level
labels may call ``_()`` at import time; a change applies after a restart.

Log messages (journal) stay Turkish on purpose; only user-facing text is translated.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unicodedata
from pathlib import Path

from . import config

DILLER = {          # code -> name in its own language (the order of the settings menu)
    "tr": "Türkçe",
    "en": "English",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "pt": "Português",
    "ru": "Русский",
    "zh": "中文（简体）",
    "ja": "日本語",
}
KAYNAK = "tr"
OTOMATIK = "otomatik"
YEDEK = "en"                          # system language not supported → English
CATALOG_DIR = Path(__file__).resolve().parent / "i18n"
SETTINGS_FILE = "arayuz.json"

_lock = threading.Lock()
_state: dict = {"dil": None, "katalog": {}}


def _settings_path() -> Path:
    return Path(config.CONFIG_DIR) / SETTINGS_FILE     # resolved at call time (tests patch it)


def ayar_oku() -> str:
    """Saved choice: "otomatik" or a code of DILLER."""
    try:
        v = json.loads(_settings_path().read_text("utf-8")).get("dil", OTOMATIK)
    except (OSError, ValueError, AttributeError):
        return OTOMATIK
    # a hand-edited value may be a list/dict: `v in DILLER` would raise TypeError on the
    # first _() of EVERY process (router, CLI, GUI, tray)
    return v if isinstance(v, str) and (v == OTOMATIK or v in DILLER) else OTOMATIK


def ayar_yaz(dil: str) -> None:
    """Save the choice atomically (applies after a restart)."""
    if dil != OTOMATIK and dil not in DILLER:
        raise ValueError(f"desteklenmeyen dil: {dil}")
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text("utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    data["dil"] = dil
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _kod(value: str) -> str:
    return value.strip().split(".")[0].split("@")[0].split("_")[0].lower()


def sistem_dili(env: dict | None = None) -> str:
    """Code from the POSIX locale variables, with gettext's rules: the locale is the first
    non-empty of LC_ALL / LC_MESSAGES / LANG; "C"/"POSIX" (or none) = untranslated →
    English. Otherwise LANGUAGE (a priority list, KDE sets it) wins: its first supported
    entry; then the locale's own language; anything unsupported → English."""
    env = os.environ if env is None else env
    loc = next(((env.get(v) or "").strip() for v in ("LC_ALL", "LC_MESSAGES", "LANG")
                if (env.get(v) or "").strip()), "")
    if not loc or loc in ("C", "POSIX") or loc.startswith(("C.", "POSIX.")):
        return YEDEK
    for item in (env.get("LANGUAGE") or "").split(":"):
        if item.strip() and _kod(item) in DILLER:
            return _kod(item)
    code = _kod(loc)
    return code if code in DILLER else YEDEK


def secilen_dil(env: dict | None = None) -> str:
    """The language this process uses: OTO_EQ_DIL > arayuz.json > system locale."""
    env = os.environ if env is None else env
    forced = (env.get("OTO_EQ_DIL") or "").strip().lower()
    if forced in DILLER:
        return forced
    saved = ayar_oku()
    return sistem_dili(env) if saved == OTOMATIK else saved


def katalog_oku(dil: str, directory: Path | None = None) -> dict[str, str]:
    """{msgid: translation} of one language ({} for Turkish / missing / broken file)."""
    if dil == KAYNAK:
        return {}
    p = Path(directory or CATALOG_DIR) / f"{dil}.json"
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and v}


def kapsama(dil: str, directory: Path | None = None) -> float | None:
    """Share of the source texts (kaynak.json) this language's catalog translates, 0..1
    (None: Turkish, or no kaynak.json to compare with)."""
    if dil == KAYNAK:
        return None
    try:
        kaynak = json.loads((Path(directory or CATALOG_DIR) / "kaynak.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(kaynak, dict) or not kaynak:
        return None
    katalog = katalog_oku(dil, directory)
    return sum(1 for m in kaynak if m in katalog) / len(kaynak)


def _load() -> None:
    with _lock:
        if _state["dil"] is None:
            dil = secilen_dil()
            _state["katalog"] = katalog_oku(dil)
            _state["dil"] = dil


def yeniden_yukle() -> str:
    """Forget the loaded catalog; the next _() reads the language setting again (the router
    gets IPC "dil" when the user picks a language). Returns the new language code."""
    with _lock:
        _state["dil"] = None
    return dil()


def dil() -> str:
    """Active language code of this process."""
    if _state["dil"] is None:
        _load()
    return _state["dil"]


def _(text: str) -> str:
    """Translate one Turkish UI text (unknown text → itself)."""
    if _state["dil"] is None:
        _load()
    return _state["katalog"].get(text, text) if _state["katalog"] else text


class Metin(str):
    """A translated text that remembers its msgid + format arguments, so a STORED copy
    (onarim.json) can be shown again in the current UI language:
    ``Metin(N_("{n} kayıt"), n=3)``. Arguments that are Metin are kept as records too."""
    msgid: str
    args: dict

    def __new__(cls, msgid: str, **args) -> "Metin":
        o = super().__new__(cls, _(msgid).format(**args) if args else _(msgid))
        o.msgid, o.args = msgid, args
        return o


def metin_kaydi(t):
    """JSON-safe record of a Metin ({"id": msgid, "arg": {…}}); other values as they are
    (numbers kept: a format spec like {db:+.1f} needs them), anything else as str."""
    if isinstance(t, Metin):
        return {"id": t.msgid, "arg": {k: metin_kaydi(v) for k, v in t.args.items()}}
    if isinstance(t, (int, float)) or t is None:
        return t
    return str(t)


def kayittan(kayit, yedek: str = "") -> str:
    """Text of a metin_kaydi record in the CURRENT UI language; ``yedek`` (the stored,
    already translated text) when there is no record or it does not fit its msgid."""
    if not (isinstance(kayit, dict) and isinstance(kayit.get("id"), str)):
        return yedek
    try:
        args = {k: kayittan(v, "") if isinstance(v, dict) else v
                for k, v in (kayit.get("arg") or {}).items()}
        return _(kayit["id"]).format(**args) if args else _(kayit["id"])
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return yedek


def veri_metni(text) -> str:
    """A text stored in the config (profile/genre "aciklama", "odak"): the shipped
    defaults are Turkish msgids (tools/i18n_cikar.py extracts them from config/) →
    translated; a user's own text has no catalog entry → shown as written."""
    return _(str(text)) if text else ""


def h_(text: str) -> str:
    """argparse help/description text: argparse %-formats it (Python 3.14 even validates it
    in add_argument), so a translation with a lone "%" must not break the command."""
    return text.replace("%", "%%")


def N_(text: str) -> str:
    """Mark a text for the catalog without translating it now (translate at use: _(x))."""
    return text


def genislik(s: str) -> int:
    """Terminal cells of a (translated) text: CJK wide/full-width counts double,
    combining marks zero — so tables stay aligned in zh/ja."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 0 if unicodedata.combining(c)
               else 1 for c in s)


def sola(s: str, width: int) -> str:
    """``s.ljust(width)`` in terminal cells."""
    return s + " " * max(0, width - genislik(s))


def saga(s: str, width: int) -> str:
    """``s.rjust(width)`` in terminal cells."""
    return " " * max(0, width - genislik(s)) + s


def sifirla(dil_kodu: str | None = None, katalog: dict[str, str] | None = None) -> None:
    """Tests/tools: drop the cached language (next _() re-reads) or force one."""
    with _lock:
        _state["dil"] = dil_kodu
        _state["katalog"] = dict(katalog or {}) if dil_kodu is not None else {}
