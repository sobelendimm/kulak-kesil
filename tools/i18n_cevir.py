#!/usr/bin/env python3
"""kaynak.json → oto_eq/i18n/<kod>.json (8 dil) — Google Translate, LLM YOK (CHANGE 8).

    python3 tools/i18n_cevir.py                     # eksikleri çevir (tüm diller)
    python3 tools/i18n_cevir.py --diller en,de      # yalnız bu diller
    python3 tools/i18n_cevir.py --zorla             # elle düzeltilenler dahil hepsini yeniden yaz
    python3 tools/i18n_cevir.py --kuru              # ağa çıkma: maskelenmiş metinleri göster
    python3 tools/i18n_cevir.py --kaynak X.json --cikti-dizini DIR --onbellek C.json   # deneme

Needs ``deep-translator`` from a private venv (never installed system-wide):
    python3 -m venv ~/.cache/oto-eq-i18n-venv
    ~/.cache/oto-eq-i18n-venv/bin/pip install deep-translator
Started with another python, the tool re-executes itself with the venv's python.

Pipeline per (msgid, language):
  1. glossary "tam" (tools/i18n_terimler.json): whole msgid → forced translation;
  2. MASK: placeholders, %-tokens, newlines, `code`, commands, paths/file names, URLs,
     shortcuts, units, glossary "terim" and product names → ``@N@`` (``MASKE_KURALLARI``);
  3. Google (cache ~/.cache/oto-eq-i18n/onbellek.json keyed by language + masked text;
     parallel, rate limited, retried with back-off; an interruption loses nothing cached);
  4. repair Google's token damage ("@ 3 @", "＠3＠", quotes around tokens, stray Turkish
     suffixes "@0@'in"), restore, align edge whitespace / end punctuation with the msgid;
  5. VERIFY: every token back exactly once, placeholder multiset == msgid's. A broken
     result is NOT written (empty entry → UI shows Turkish) and is listed in the report.
Existing non-empty catalog entries are kept (hand edits) unless ``--zorla``; entries no
longer in kaynak.json are dropped.

Google's free endpoint blocks an IP (429 → "sorry" captcha page) when hit too fast, so
requests are batched (``--toplu`` masked lines joined by newlines — masked text never
contains one; a changed line count → those lines one by one), paced globally
(``--aralik``) and guarded by a circuit breaker: on 429 all threads pause, after 3 in a
row the run stops cleanly. Exit: 0 clean, 1 some entries rejected (report), 3 Google
blocked us — run the same command later, the cache resumes it.

DeepL engine (``--motor deepl``; the default when a key exists): the DeepL REST API is
called directly (urllib, no venv needed). Key: ``$DEEPL_AUTH_KEY``, else the first line of
``~/.config/deepl-key`` (should be mode 600; ":fx" keys → api-free.deepl.com). The key is
never printed. Requests carry ≤50 texts / ≤~100 KB; the ``@N@`` tokens travel as XML
empty elements ``<xN/>`` with ``tag_handling=xml`` (DeepL moves tags, it does not
translate or respace them) and are turned back into ``@N@`` before the usual repair +
VERIFY. Before any translate request ``GET /v2/usage`` gives the remaining quota; a
language whose estimated characters do not fit is SKIPPED whole (its catalog untouched),
in the order en, de, es, fr, pt, ru, zh, ja. HTTP 456 (quota used up) stops cleanly with
exit 4 (cache saved, same command resumes later); a quota skip also exits 4; 429/5xx are
retried with back-off; 401/403 → "anahtar geçersiz", exit 5. ``--kota`` only prints the
quota. The cache keeps engines apart (DeepL keys are prefixed ``deepl``).
``oto_eq/i18n/.kaynak-motor.json`` records which engine wrote each catalog entry (and a
hash of it): ``--motor deepl --yukselt`` re-translates entries written by Google (or of
unknown origin) with DeepL, but never an entry whose text no longer matches its record
(a hand edit); a failed upgrade keeps the old text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_cikar import (DILLER, KATALOG_DIZINI, KAYNAK_DOSYASI, YUZDE_RE,  # noqa: E402
                        json_yaz, kaynak_oku, korunan_re, yer_tutucular)

VENV = Path.home() / ".cache" / "oto-eq-i18n-venv"
ONBELLEK = Path.home() / ".cache" / "oto-eq-i18n" / "onbellek.json"
TERIMLER = Path(__file__).resolve().parent / "i18n_terimler.json"
GOOGLE_KOD = {"zh": "zh-CN"}           # our code → Google's
DEEPL_ANAHTAR = Path.home() / ".config" / "deepl-key"
DEEPL_HEDEF = {"en": "EN-US", "de": "DE", "es": "ES", "fr": "FR", "pt": "PT-BR", "ru": "RU",
               "zh": "ZH-HANS", "ja": "JA"}
DIL_SIRASI = ("en", "de", "es", "fr", "pt", "ru", "zh", "ja")   # quota short → these first
DEEPL_TOPLU = 50                       # API limit: 50 texts per request
DEEPL_BAYT = 100_000                   # API limit: 128 KiB request body
MOTOR_DOSYASI = ".kaynak-motor.json"   # next to the catalogs

# --- the protected-word list (ONE place) ------------------------------------------------
URUN_ADLARI = (
    "Kulak Kesil", "HS80", "CS2", "PipeWire", "WirePlumber", "PulseAudio", "EQ", "RNNoise",
    "LSP", "AutoEq", "Harman", "Discord", "Firefox", "YouTube Music", "YouTube", "Spotify",
    "Steam", "Proton", "Wine", "MPRIS", "MusicBrainz", "iTunes", "Deezer", "Discogs",
    "YAMNet", "EffNet", "Essentia", "Bluetooth", "USB", "HDMI", "Corsair", "KDE", "Plasma",
    "Linux", "Flatpak", "Qt", "ONNX", "TOML", "JSON", "ALSA", "systemd", "GNOME", "Wayland",
    "TeamSpeak", "Mumble", "Zoom", "Chrome", "Chromium", "VLC", "mpv", "OBS", "Lutris",
    "Faugus", "Last.fm", "AcoustID", "Shazam", "Tidal", "SoundCloud", "Twitch", "LUFS",
    "oto-eq-sinks", "oto-eq-mic", "oto-eq",            # service / unit names
)
# Units are kept verbatim except these, which are Turkish abbreviations.
BIRIM_CEVIRI = {
    "sn": {"en": "s", "de": "s", "es": "s", "fr": "s", "pt": "s", "ru": "с", "zh": "秒", "ja": "秒"},
    "dk": {"en": "min", "de": "min", "es": "min", "fr": "min", "pt": "min", "ru": "мин",
           "zh": "分钟", "ja": "分"},
}
_BIRIMLER = r"(?:dBFS|dB|LUFS|kHz|Hz|ms|sn|dk)"
# kulak subcommands (oto_eq/cli.py) — "kulak" alone is the Turkish word "ear".
KULAK_KOMUTLARI = ("ab", "durum", "eq-ciz", "eq-uret", "gunluk", "izle", "kapat", "kurallar",
                   "mikrofon", "mic", "oto", "profiller", "sabitle", "saglik", "ton", "tur",
                   "yenile", "geri", "yedek", "onarim", "kisma", "model", "dil")
_TR_DURAK = r"(?:ile|ve|veya|ya|da|de|ki|mi|ise|sonra|yaz\w*|komut\w*|ya da|çalıştır\w*)"
_ARG = rf"(?:--?[A-Za-z][\w-]*(?:=[^\s`'\"]+)?|(?!{_TR_DURAK}(?![\w-]))[a-z0-9][a-z0-9_.@-]*(?![\w]))"
_TRL = "A-Za-zÇĞİÖŞÜçğıöşü"

# (kind, regex) in PRIORITY order: an earlier rule wins an overlap, then the longer match.
MASKE_KURALLARI: list[tuple[str, str]] = [
    ("kod", r"`[^`\n]+`"),
    ("url", r"https?://[^\s<>\"'`]*[^\s<>\"'`.,;:!?)\]]"),
    # config names: [genel], [mod.X], [uygulamalar.sesli], genel.mod, node.passive (1 Kas)
    ("kod", korunan_re().pattern),
    ("komut", rf"(?<![\w-])kulak\s+(?:{'|'.join(map(re.escape, KULAK_KOMUTLARI))})(?![\w-])"
              rf"(?:[ \t]+{_ARG}){{0,4}}"),
    ("komut", r"(?<![\w-])systemctl(?:\s+--user)?(?:\s+(?:start|stop|restart|status|enable|"
              r"disable|reload|daemon-reload|is-active|is-enabled|is-failed|mask|unmask|"
              r"reset-failed|show|cat|edit|kill|list-units|list-timers)(?![\w-]))?"
              r"(?:\s+(?:--?[\w-]+|[\w@-]+\.(?:service|timer|socket|path|target)"
              r"|[a-z0-9]+(?:-[a-z0-9]+)+|pipewire|wireplumber))*"),
    ("komut", r"(?<![\w-])journalctl(?:\s+(?:--?[\w-]+(?:=\S+)?|[\w@-]+\.service|oto-eq))+"),
    ("komut", rf"(?<![\w-])python3?\s+-m\s+[A-Za-z_][\w.]*(?:[ \t]+{_ARG})*"),
    ("komut", r"(?<![\w-])(?:pw-[a-z-]+|wpctl|pactl)(?:\s+(?:--?[\w-]+|@\w+@|[a-z][\w.-]*\.[\w.-]+"
              r"|(?:set|get|list|load|unload|info|status|inspect|dump|sinks|sources|module)[\w-]*"
              r"|[\d.]+%?))*"),
    ("klavye", r"(?<![\w])(?:Ctrl|Alt|Shift|Super|Meta)(?:\+(?:Ctrl|Alt|Shift|Super|Meta))*\+"
               r"(?:F\d{1,2}|Enter|Esc|Tab|Space|Delete|Del|Backspace|Home|End|PgUp|PgDown|"
               r"Up|Down|Left|Right|[A-Za-z0-9]|[,./;=+\-])(?![A-Za-z0-9])"),
    ("yol", r"(?<![\w/~.])(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]*[\w@+-]"),
    ("yol", r"(?<![\w./-])(?:[\w-]+\.)*[\w-]*\w?\.(?:toml|json|conf|desktop|service|timer|py|sh|"
            r"log|wav|flac|onnx|csv|txt|ini|ya?ml|lua|srt)(?![\w])"),
    ("birim", rf"(?:\{{[^{{}}]*\}}|(?<![\w.,{_TRL}])[-+±−]?\d+(?:[.,]\d+)?)[  ]?{_BIRIMLER}"
              r"(?![\w])"),
    ("alan", r"\{\{|\}\}|\{[^{}]*\}"),
    ("yuzde", YUZDE_RE.pattern),
    ("kod", r"(?<![\w.-])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![\w])"),   # snake_case id
    # literal CLI syntax in help/usage texts: "[durum|ac|kapat]", "<oto|genel>", "[db=6]",
    # "--profiller", the 2-space-indented first column ("  ac | kapat     …"), profile
    # key lists ("oyun,muzik,film") and quoted values ('sifirla', "tam")
    ("kod", r"[\[<](?:[^\]>\s|]+\|)+[^\]>\s|]+[\]>]|\[[^\]\s]*=[^\]\s]*\]"),
    ("kod", r"(?<![\w-])--[a-z][a-z0-9-]*(?:=[^\s\]]+)?"),
    ("kod", r"(?<=\n  )\S[^\n]*?(?=[ \t]{2,}\S)"),
    ("kod", r"(?<![\w])(?:oyun|muzik|film|cs2|duz)(?:,[ ]?(?:oyun|muzik|film|cs2|duz))+(?![\w])"),
    ("kod", r"""(?<![\w])(['"])(?!pompalama\1)[a-z][a-z0-9_]*\1(?![\w])"""),
    ("satir", r"[ \t]*(?:\r?\n|\t)[\r\n\t ]*"),
    ("terim", None),                    # glossary "terim" (per language, filled at runtime)
    ("urun", None),                     # URUN_ADLARI
    ("birim", rf"(?<![\w]){_BIRIMLER}(?![\w])"),
    ("sembol", r"[·→←↑↓↔•✓✔✗✘⚠★☆▶◀■□●○♪♫⏸⏵⏹⟳↻]+|[\U0001F300-\U0001FAFF☀-➿]️?"),
]

_TOKEN_DUZ = re.compile(r"[@＠]\s*([0-9０-９]+)\s*[@＠]")
_TOKEN = re.compile(r"@(\d+)@")
_TIRNAK = {'"': '"', "'": "'", "“": "”", "”": "”", "„": "“", "«": "»", "‹": "›", "「": "」",
           "『": "』", "‘": "’", "`": "`"}
# Turkish case/possessive suffixes Google leaves glued to a token: "@0@'in" → "@0@".
_TR_EKLER = {"a", "e", "ı", "i", "u", "ü", "ya", "ye", "yı", "yi", "yu", "yü", "da", "de", "ta",
             "te", "dan", "den", "tan", "ten", "in", "ın", "un", "ün", "nin", "nın", "nun",
             "nün", "la", "le", "yla", "yle", "dır", "dir", "tır", "tir", "lar", "ler", "ları",
             "leri", "suz", "süz", "sız", "siz", "lı", "li", "lu", "lü", "daki", "deki", "taki",
             "teki", "nı", "ni", "nu", "nü", "na", "ne", "nda", "nde", "ndan", "nden", "yı",
             "ndaki", "ndeki", "nin", "dur", "dür", "tur", "tür", "ken"}
_EK_RE = re.compile(r"(@\d+@)['’]([a-zçğıöşü]{1,5})(?![\w])")
_HARFLI = re.compile(rf"[{_TRL}]")
_CJK = ("zh", "ja")
_KUCUK_BASLA = ("en", "es", "fr", "pt", "ru")    # lower-case first letter allowed / kept


class CeviriHatasi(Exception):
    """One entry could not be translated safely (reason in Turkish)."""


# --- glossary ---------------------------------------------------------------------------
@dataclass
class Terimler:
    tam: dict[str, dict[str, str]] = field(default_factory=dict)
    terim: dict[str, dict[str, str]] = field(default_factory=dict)
    # DeepL glossary: Turkish word form → {dil: target}. Each "sozluk" entry lists its
    # inflected forms in "_bicimler"; a capitalised variant is added automatically.
    sozluk: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def oku(cls, path: Path | None = TERIMLER) -> "Terimler":
        if path is None or not Path(path).exists():
            return cls()
        data = json.loads(Path(path).read_text("utf-8"))

        def temiz(d):
            return {k: {dl: v for dl, v in (vals or {}).items() if isinstance(v, str) and v}
                    for k, vals in (d or {}).items() if not k.startswith("_")}
        sozluk: dict[str, dict[str, str]] = {}
        for k, vals in (data.get("sozluk") or {}).items():
            if k.startswith("_") or not isinstance(vals, dict):
                continue
            hedef = {dl: v for dl, v in vals.items() if dl in DILLER and isinstance(v, str) and v}
            for b in [k, *[x for x in vals.get("_bicimler", []) if isinstance(x, str)]]:
                sozluk.setdefault(b, hedef)
                sozluk.setdefault(_tr_buyuk(b), {dl: _buyuk(v) for dl, v in hedef.items()})
        return cls(temiz(data.get("tam")), temiz(data.get("terim")), sozluk)


def _tr_buyuk(s: str) -> str:
    return (("İ" if s[0] == "i" else "I" if s[0] == "ı" else s[0].upper()) + s[1:]) if s else s


def _buyuk(s: str) -> str:
    return s[0].upper() + s[1:] if s else s


# --- masking ----------------------------------------------------------------------------
@dataclass
class Maske:
    metin: str                       # sent to Google
    geri: list[str]                  # token i → text to put back (target language)
    tur: list[str]                   # token i → rule kind
    kaynak: list[str]                # token i → original Turkish literal


def _sozcuk_re(words) -> str | None:
    words = sorted({w for w in words if w}, key=len, reverse=True)
    if not words:
        return None
    return r"(?<![\w])(?:" + "|".join(re.escape(w) for w in words) + r")(?![\w])"


def _geri_deger(tur: str, lit: str, dil: str, terimler: Terimler) -> str:
    if tur == "terim":
        return terimler.terim[lit][dil]
    if tur == "birim":
        m = re.search(rf"{_BIRIMLER}$", lit)
        if m and m.group(0) in BIRIM_CEVIRI:
            return lit[:m.start()] + BIRIM_CEVIRI[m.group(0)].get(dil, m.group(0))
    return lit


def maskele(text: str, dil: str, terimler: Terimler | None = None) -> Maske:
    """Replace every protected span with ``@N@`` (N in order of appearance)."""
    terimler = terimler or Terimler()
    spans: list[tuple[int, int, int, str]] = []          # (start, end, priority, kind)
    for prio, (tur, pat) in enumerate(MASKE_KURALLARI):
        if tur == "terim":
            pat = _sozcuk_re(k for k, v in terimler.terim.items() if dil in v)
        elif tur == "urun":
            pat = _sozcuk_re(URUN_ADLARI)
        if not pat:
            continue
        for m in re.finditer(pat, text):
            if m.end() > m.start():
                spans.append((m.start(), m.end(), prio, tur))
    secilen: list[tuple[int, int, str]] = []
    for s, e, _p, tur in sorted(spans, key=lambda x: (x[2], -(x[1] - x[0]), x[0])):
        if all(e <= s2 or s >= e2 for s2, e2, _t in secilen):
            secilen.append((s, e, tur))
    secilen.sort()
    out, geri, turler, kaynak, pos = [], [], [], [], 0
    for s, e, tur in secilen:
        lit = text[s:e]
        out.append(text[pos:s])
        # newline tokens get spaces so Google does not glue the two lines into one word
        out.append(f" @{len(geri)}@ " if tur == "satir" else f"@{len(geri)}@")
        geri.append(_geri_deger(tur, lit, dil, terimler))
        turler.append(tur)
        kaynak.append(lit)
        pos = e
    out.append(text[pos:])
    return Maske("".join(out), geri, turler, kaynak)


def belirtecleri_onar(raw: str, maske: Maske) -> str:
    """Undo Google's token damage: spacing/full-width "@ 3 @", quotes, Turkish suffixes."""
    s = _TOKEN_DUZ.sub(lambda m: "@" + m.group(1).translate(
        str.maketrans("０１２３４５６７８９", "0123456789")) + "@", raw)
    # quotes Google wrapped around a token that the source did not have
    for m in reversed(list(_TOKEN.finditer(s))):
        a, b = m.start(), m.end()
        if a > 0 and b < len(s) and _TIRNAK.get(s[a - 1]) == s[b]:
            pair = s[a - 1] + m.group(0) + s[b]
            if pair not in maske.metin:
                s = s[:a - 1] + m.group(0) + s[b + 1:]
    s = _EK_RE.sub(lambda m: m.group(1) if m.group(2) in _TR_EKLER else m.group(0), s)
    # DeepL formats command-looking text as `code`; backticks the source never had go
    if "`" in s and "`" not in maske.metin + "".join(maske.geri):
        s = s.replace("`", "")
    return s


# letters allowed glued after a value (ordinals, "2x") — same list as i18n_denetle
_SONEK_IZIN = {"en": ("st", "nd", "rd", "th", "x"), "fr": ("e", "er", "re", "ère", "x"),
               "es": ("º", "ª", "x"), "pt": ("º", "ª", "x"), "de": ("x",), "ru": ("x",)}
_SAG_YAPISIK = re.compile(r"@(\d+)@([^\W\d_]+)")
_SOL_YAPISIK = re.compile(r"(?<=[^\W_]|@)@(\d+)@")


def bitisikleri_ayir(s: str, maske: Maske, dil: str) -> str:
    """Undo DeepL's glue around tokens; the masked source decides what was separate.

    "start@1@" / "@0@@1@" → a space where the source had one (commands, lists);
    "@0@e mode" → "@0@ mode" (a Turkish case suffix DeepL invented after a value).
    Only placeholders are judged against letters: a unit or term token glued to a word
    may be DeepL spelling it out ("1 @3@econd" = "1 second")."""
    kaynak, sonraki = {}, {}
    for m in _TOKEN.finditer(maske.metin):
        a, b = m.start(), m.end()
        kaynak[m.group(1)] = (maske.metin[a - 1:a], maske.metin[b:b + 1])
        if maske.metin[b:b + 1] == " " and maske.metin[b + 1:b + 2] in "[(<{-":
            sonraki[m.group(1)] = maske.metin[b + 1]       # "@1@ [@2@" must stay apart
    cjk = dil in _CJK

    def alan(i: str, *ek: str) -> bool:
        return int(i) < len(maske.tur) and maske.tur[int(i)] in ("alan", *ek)

    def sag(m):
        sol_k, sag_k = kaynak.get(m.group(1), ("", ""))
        ek = m.group(2)
        if sag_k.isalpha() or not alan(m.group(1)):
            return m.group(0)
        if ek in _TR_EKLER and not (ek in _SONEK_IZIN.get(dil, ()) and sag_k == "."):
            return f"@{m.group(1)}@"
        if cjk or ek in _SONEK_IZIN.get(dil, ()):
            return m.group(0)
        return f"@{m.group(1)}@ {ek}"

    def sol(m):
        sol_k, _sag_k = kaynak.get(m.group(1), ("", ""))
        once = s2[m.start() - 1]
        if once == "@" or (cjk and once.isascii()):
            ayir = sol_k.isspace()
        else:
            ayir = (not cjk and alan(m.group(1), "birim") and once.isalpha()
                    and not sol_k.isalpha())                # "lookahead@0@" ← "{ms:g} ms"
        return (" " if ayir else "") + m.group(0)
    s2 = _SAG_YAPISIK.sub(sag, s)
    for i, c in sonraki.items():
        s2 = s2.replace(f"@{i}@{c}", f"@{i}@ {c}")
    return _SOL_YAPISIK.sub(sol, s2)


def geri_koy(s: str, maske: Maske) -> str:
    """Tokens → values (newline tokens swallow the spaces Google put around them)."""
    def rep(m):
        i = int(m.group(1))
        return maske.geri[i]
    for i, tur in enumerate(maske.tur):
        if tur == "satir":
            s = re.sub(rf"[ \t]*@{i}@[ \t]*", lambda _m, i=i: f"@{i}@", s)
    return _TOKEN.sub(rep, s)


def belirtec_denetle(s: str, maske: Maske) -> None:
    sayim = Counter(int(m.group(1)) for m in _TOKEN.finditer(s))
    beklenen = Counter(range(len(maske.geri)))
    if sayim != beklenen:
        eksik = sorted(beklenen - sayim)
        fazla = sorted(sayim - beklenen)
        parca = []
        if eksik:
            parca.append("kayıp: " + ", ".join(repr(maske.kaynak[i]) for i in eksik))
        if fazla:
            parca.append("fazla/çift: " + ", ".join(f"@{i}@" for i in fazla))
        raise CeviriHatasi("maske bozuldu (" + "; ".join(parca) + ")")


# --- punctuation / case alignment --------------------------------------------------------
_UC_SINIF = {"…": ("…", "...", "……", "。。。"), ":": (":", "："), "?": ("?", "？"),
             "!": ("!", "！"), ".": (".", "。")}
_CJK_UC = {":": "：", "?": "？", "!": "！", ".": "。", "…": "…"}


def _uc_noktalama(s: str) -> str | None:
    for k, forms in _UC_SINIF.items():
        for f in sorted(forms, key=len, reverse=True):
            if s.endswith(f):
                return k
    return None


def noktalama_uyumla(kaynak: str, ceviri: str, dil: str) -> str:
    """End punctuation of the translation follows the msgid (…, :, ., ?, !, none)."""
    if not kaynak or not ceviri:
        return ceviri
    k_uc, c_uc = _uc_noktalama(kaynak), _uc_noktalama(ceviri)

    def kirp(s):
        for k in ("…", ":", "?", "!", "."):
            for f in sorted(_UC_SINIF[k], key=len, reverse=True):
                if s.endswith(f):
                    return s[:-len(f)].rstrip()
        return s
    if k_uc in ("…", ":", "?", "!", "."):
        if c_uc == k_uc:
            if k_uc == "…" and not ceviri.endswith("…"):
                return kirp(ceviri) + "…"
            return ceviri
        hedef = _CJK_UC[k_uc] if dil in _CJK else k_uc
        if c_uc in (".", "…", ":") or c_uc is None:
            return kirp(ceviri) + hedef
        return ceviri                      # ? / ! vs . — Google rephrased; keep
    if k_uc is None and c_uc == ".":           # Google added a full stop to a label
        return kirp(ceviri)
    return ceviri


def buyuk_harf_uyumla(kaynak: str, masked_out: str, dil: str) -> str:
    """First letter case follows the msgid (CLI help is lower-case, labels capitalised)."""
    if dil in _CJK or not masked_out or not kaynak or masked_out[0] == "@":
        return masked_out
    k0, c0 = kaynak[0], masked_out[0]
    if not (k0.isalpha() and c0.isalpha()):
        return masked_out
    if k0.islower() and c0.isupper() and dil in _KUCUK_BASLA and not masked_out[1:2].isupper():
        return c0.lower() + masked_out[1:]
    if k0.isupper() and c0.islower():
        return c0.upper() + masked_out[1:]
    return masked_out


# --- one entry ---------------------------------------------------------------------------
class Onbellek:
    """{"<dil>\\x1f<masked text>": "<raw Google output>"}; other engines prefix their name
    ("deepl\\x1f<dil>\\x1f<masked text>") so results never mix. Thread-safe, atomic."""

    def __init__(self, path: Path | None = ONBELLEK, kaydet_her: int = 20):
        self.path = Path(path) if path else None
        self.kaydet_her = kaydet_her
        self._lock = threading.Lock()
        self._yeni = 0
        self.veri: dict[str, str] = {}
        if self.path and self.path.exists():
            try:
                d = json.loads(self.path.read_text("utf-8"))
                self.veri = {k: v for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else {}
            except (OSError, ValueError):
                self.veri = {}

    @staticmethod
    def anahtar(dil: str, metin: str, motor: str = "google") -> str:
        return f"{dil}\x1f{metin}" if motor == "google" else f"{motor}\x1f{dil}\x1f{metin}"

    def al(self, dil: str, metin: str, motor: str = "google") -> str | None:
        with self._lock:
            return self.veri.get(self.anahtar(dil, metin, motor))

    def koy(self, dil: str, metin: str, ceviri: str, motor: str = "google") -> None:
        with self._lock:
            self.veri[self.anahtar(dil, metin, motor)] = ceviri
            self._yeni += 1
            if self._yeni >= self.kaydet_her:
                self._kaydet_kilitli()

    def kaydet(self) -> None:
        with self._lock:
            self._kaydet_kilitli()

    def _kaydet_kilitli(self) -> None:
        self._yeni = 0
        if self.path:
            json_yaz(self.path, dict(sorted(self.veri.items())))


class CalismaDurdu(Exception):
    """Stops the whole run (cache saved); ``kod`` = exit code, ``devam`` = how to resume."""
    kod = 3
    devam = "aynı komutla kaldığı yerden devam eder."


class GoogleEngeli(CalismaDurdu):
    """Google answered 429 / its "sorry" captcha page repeatedly: stop the whole run."""
    kod = 3
    devam = "bir süre (≥30 dk) sonra aynı komutla kaldığı yerden devam eder."


class DeepLKotaDoldu(CalismaDurdu):
    """HTTP 456: the monthly character quota is used up."""
    kod = 4
    devam = "kota yenilenince (aylık dönem başı) aynı komutla kaldığı yerden devam eder."


class DeepLAnahtarGecersiz(CalismaDurdu):
    """HTTP 401/403: wrong / revoked key (the key itself is never in the message)."""
    kod = 5
    devam = "anahtarı düzeltip (DEEPL_AUTH_KEY ya da ~/.config/deepl-key) aynı komutu çalıştır."


def _engel_mi(e: BaseException) -> bool:
    return "TooManyRequests" in type(e).__name__ or "429" in str(e) or "sorry" in str(e)


class HizSiniri:
    """Global request pacing (all threads) + a circuit breaker for Google's 429 block.

    ``aralik``: minimum seconds between request starts. On a 429 every thread pauses
    ``engel_bekleme`` × 2^(n-1) seconds; after ``en_cok_engel`` consecutive 429s the run
    stops (GoogleEngeli) instead of hammering a blocked IP — the cache keeps the progress."""

    def __init__(self, aralik: float, engel_bekleme: float = 60.0, en_cok_engel: int = 3,
                 uyu: Callable[[float], None] = time.sleep):
        self.aralik = max(0.0, aralik)
        self.engel_bekleme = engel_bekleme
        self.en_cok_engel = max(1, en_cok_engel)
        self._uyu = uyu
        self._lock = threading.Lock()
        self._sonraki = 0.0
        self._ardisik = 0
        self.durdu = False

    def bekle(self) -> None:
        with self._lock:
            if self.durdu:
                raise GoogleEngeli("Google istekleri engelledi (429)")
            simdi = time.monotonic()
            t = max(simdi, self._sonraki)
            self._sonraki = t + self.aralik
        if t > simdi:
            self._uyu(t - simdi)

    def engel(self) -> None:
        with self._lock:
            self._ardisik += 1
            if self._ardisik >= self.en_cok_engel:
                self.durdu = True
                return
            dur = self.engel_bekleme * 2 ** (self._ardisik - 1)
            self._sonraki = max(self._sonraki, time.monotonic() + dur)
        print(f"  Google 429 — tüm istekler {dur:.0f} sn bekletiliyor", file=sys.stderr)

    def basari(self) -> None:
        with self._lock:
            self._ardisik = 0


Cevirici = Callable[[str], str]                  # masked Turkish → raw target text
Fabrika = Callable[[str], Cevirici]              # language code → translator


def google_fabrikasi(dil: str) -> Cevirici:
    from deep_translator import GoogleTranslator         # venv only (imported lazily)
    local = threading.local()

    def cevir(text: str) -> str:
        tr = getattr(local, "tr", None)
        if tr is None:
            tr = local.tr = GoogleTranslator(source="tr", target=GOOGLE_KOD.get(dil, dil))
        return tr.translate(text)
    return cevir


@dataclass
class Ayarlar:
    terimler: Terimler = field(default_factory=Terimler)
    onbellek: Onbellek | None = None
    hiz: HizSiniri | None = None
    deneme: int = 4
    geri_cekilme: float = 2.0
    toplu: int = 20                  # msgids per request (newline-joined); 1 = no batching
    toplu_karakter: int = 3500


def _google_cagir(cevirici: Cevirici, metin: str, ay: Ayarlar) -> str:
    son = None
    for i in range(max(1, ay.deneme)):
        if ay.hiz:
            ay.hiz.bekle()                           # raises GoogleEngeli once blocked
        try:
            out = cevirici(metin)
            if isinstance(out, str) and out.strip():
                if ay.hiz:
                    ay.hiz.basari()
                return out
            son = CeviriHatasi("Google boş yanıt verdi")
        except Exception as e:                       # network / quota / parse errors
            son = e
            if ay.hiz and _engel_mi(e):
                ay.hiz.engel()                       # global pause instead of own sleep
                if ay.hiz.durdu:
                    raise GoogleEngeli(str(e)) from None
                continue
        if i + 1 < ay.deneme:
            time.sleep(ay.geri_cekilme * (2 ** i) * (0.75 + random.random() / 2))
    raise CeviriHatasi(f"Google çevirisi alınamadı: {type(son).__name__}: {son}")


def _google_tek(cevirici: Cevirici, metin: str, dil: str, ay: Ayarlar) -> str:
    raw = _google_cagir(cevirici, metin, ay)
    if ay.onbellek:
        ay.onbellek.koy(dil, metin, raw)
    return raw


def _google_toplu(cevirici: Cevirici, metinler: list[str], dil: str,
                  ay: Ayarlar) -> dict[str, str | Exception]:
    """Several masked lines in ONE request (masked text never contains a newline).

    Line count changed by Google → each line again on its own."""
    if len(metinler) == 1:
        try:
            return {metinler[0]: _google_tek(cevirici, metinler[0], dil, ay)}
        except CeviriHatasi as e:
            return {metinler[0]: e}
    try:
        satirlar = _google_cagir(cevirici, "\n".join(metinler), ay).split("\n")
    except CeviriHatasi:
        satirlar = []
    if len(satirlar) == len(metinler) and all(x.strip() for x in satirlar):
        sonuc: dict[str, str | Exception] = {}
        for m, raw in zip(metinler, satirlar):
            sonuc[m] = raw.strip()
            if ay.onbellek:
                ay.onbellek.koy(dil, m, sonuc[m])
        return sonuc
    sonuc = {}
    for m in metinler:
        try:
            sonuc[m] = _google_tek(cevirici, m, dil, ay)
        except CeviriHatasi as e:
            sonuc[m] = e
    return sonuc


# --- DeepL ------------------------------------------------------------------------------
def _uyari(msg: str) -> None:
    print(msg, file=sys.stderr)


def deepl_anahtari(path: Path | None = DEEPL_ANAHTAR, ortam=None,
                   uyar: Callable[[str], None] = _uyari) -> str | None:
    """$DEEPL_AUTH_KEY, else the first line of ``path`` (stripped); None when absent."""
    ortam = os.environ if ortam is None else ortam
    k = (ortam.get("DEEPL_AUTH_KEY") or "").strip()
    if k:
        return k
    if path is None:
        return None
    p = Path(path)
    try:
        mod = p.stat().st_mode
        satirlar = p.read_text("utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    k = satirlar[0].strip() if satirlar else ""
    if k and mod & 0o077:
        uyar(f"UYARI: {p} izinleri {oct(mod & 0o777)} — başkaları okuyabilir; "
             f"önerilen: chmod 600 {p}")
    return k or None


DEEPL_BAGLAM = (
    "UI string of 'Kulak Kesil', a Linux audio app that automatically switches headphone EQ "
    "profiles per app (game, music, film). Short labels: açık = on/enabled, kapalı = "
    "off/disabled, tür = music/game genre, kısma = ducking (lowering other audio during voice "
    "chat), Düz = the flat output, sağlık kontrolü = health check, ses analizi = audio "
    "analysis, kulaklık = headphones, düzeltme = (headphone) correction, ön kazanç = preamp, "
    "tepsi = system tray.")
DEEPL_YONTEM = "2"                     # bump when the request format changes (cache key)
_XML_KACIS = {"&lt;": "<", "&gt;": ">", "&quot;": '"', "&apos;": "'", "&#39;": "'",
              "&amp;": "&"}
_K_ETIKET = re.compile(r"""<k\s+i\s*=\s*["']?(\d+)["']?\s*(?:/>|>.*?</k\s*>)""", re.S)
_EK_SONRA = re.compile(r"(@\d+@)['’][a-zçğıöşü]{1,6}(?![\w])")


def _kac(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _kac_coz(s: str) -> str:
    return re.sub("|".join(map(re.escape, _XML_KACIS)), lambda m: _XML_KACIS[m.group(0)], s)


def deepl_kodla(parca: str, maske: Maske | None, belirtec: str = "etiket") -> str:
    """One masked segment → DeepL text.

    "etiket": every token becomes ``<k i="N">value</k>`` (value = what will be put back,
    e.g. "{saniye}", "HS80", the target-language glossary term) and ``k`` is an ignored
    tag: DeepL sees a real word in place and keeps it verbatim. A Turkish case suffix
    glued to a token ("EQ'nun") is dropped first — DeepL truncated such sentences.
    "duz": plain ``@N@`` text (no tag handling)."""
    if belirtec == "duz":
        return parca
    s = _kac(_EK_SONRA.sub(r"\1", parca))

    def rep(m):
        i = int(m.group(1))
        deger = maske.geri[i] if maske and i < len(maske.geri) else m.group(0)
        return f'<k i="{i}">{_kac(deger)}</k>'
    return _TOKEN.sub(rep, s)


def deepl_coz(s: str, belirtec: str = "etiket") -> str:
    """DeepL output → masked form (``@N@``) for the shared repair + VERIFY step."""
    if belirtec == "duz":
        return s
    return _kac_coz(_K_ETIKET.sub(lambda m: f"@{m.group(1)}@", s))


def satirlara_bol(metin: str, maske: Maske | None) -> list[tuple[str, str]]:
    """Split a masked text at its newline tokens: [(segment, following separator)].

    Each line is translated on its own (DeepL moved line breaks mid-sentence)."""
    if not maske or "satir" not in maske.tur:
        return [(metin, "")]
    sat = "|".join(str(i) for i, t in enumerate(maske.tur) if t == "satir")
    out, pos = [], 0
    for m in re.finditer(rf"\s*@(?:{sat})@\s*", metin):
        out.append((metin[pos:m.start()], f" {m.group(0).strip()} "))
        pos = m.end()
    out.append((metin[pos:], ""))
    return out


def deepl_karakter(metin: str, maske: Maske | None = None) -> int:
    """Billed-character estimate (token values included; markup is not billed)."""
    if maske is None:
        return len(metin)
    return len(_TOKEN.sub(lambda m: maske.geri[int(m.group(1))]
                          if int(m.group(1)) < len(maske.geri) else m.group(0), metin))


def deepl_bayt(metin: str) -> int:
    return 3 * len(metin.encode("utf-8")) + 2       # tags/escapes: generous


Istek = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict, bytes]"]


def _urllib_istek(yontem: str, url: str, basliklar: dict, govde: bytes | None,
                  zaman_asimi: float) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=govde, headers=basliklar, method=yontem)
    try:
        with urllib.request.urlopen(req, timeout=zaman_asimi) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        try:
            govde = e.read()
        except OSError:
            govde = b""
        return e.code, dict(e.headers or {}), govde


class DeepL:
    """DeepL REST v2 (JSON body, header auth). ``istek`` = HTTP layer (tests fake it).

    ``belirtec``: "etiket" (content tags, ignore_tags) or "duz" (plain ``@N@``);
    ``baglam``: DeepL ``context`` (not billed); ``sozluk``: Turkish form → {dil: target}
    → one DeepL glossary per language (created on first use, stale ones deleted)."""

    YENIDEN = (429, 500, 502, 503, 504, 529)

    def __init__(self, anahtar: str, istek: Istek | None = None,
                 uyu: Callable[[float], None] = time.sleep, deneme: int = 6,
                 geri_cekilme: float = 2.0, zaman_asimi: float = 60.0,
                 belirtec: str = "etiket", baglam: str | None = DEEPL_BAGLAM,
                 sozluk: dict[str, dict[str, str]] | None = None):
        self._anahtar = anahtar
        self._istek = istek
        self._uyu = uyu
        self.deneme = max(1, deneme)
        self.geri_cekilme = geri_cekilme
        self.zaman_asimi = zaman_asimi
        self.belirtec = belirtec
        self.baglam = baglam or None
        self.sozluk = sozluk or {}
        self._sozluk_re = (re.compile(r"(?<![\w])(?:" + "|".join(
            re.escape(k) for k in sorted(self.sozluk, key=len, reverse=True)) + r")(?![\w])")
            if self.sozluk else None)
        self._sozluk_id: dict[str, str | None] = {}
        self.taban = ("https://api-free.deepl.com" if anahtar.endswith(":fx")
                      else "https://api.deepl.com")
        self._lock = threading.Lock()
        self._sozluk_lock = threading.Lock()
        self.durdu: CalismaDurdu | None = None
        self.istek = 0
        self.karakter = 0

    def __repr__(self) -> str:                    # never show the key
        return f"DeepL({self.taban})"

    def _gizle(self, s: str) -> str:
        return s.replace(self._anahtar, "***") if self._anahtar else s

    def _dur(self, hata: CalismaDurdu) -> CalismaDurdu:
        with self._lock:
            if self.durdu is None:
                self.durdu = hata
            return self.durdu

    def _cagir(self, yontem: str, yol: str, veri: dict | None = None) -> dict:
        basliklar = {"Authorization": f"DeepL-Auth-Key {self._anahtar}",
                     "User-Agent": "oto-eq-i18n/1.0", "Accept": "application/json"}
        govde = None
        if veri is not None:
            govde = json.dumps(veri, ensure_ascii=False).encode("utf-8")
            basliklar["Content-Type"] = "application/json"
        istek = self._istek or _urllib_istek
        son = ""
        for i in range(self.deneme):
            if self.durdu is not None:
                raise type(self.durdu)(str(self.durdu))
            try:
                kod, bas, cevap = istek(yontem, self.taban + yol, basliklar, govde,
                                        self.zaman_asimi)
            except (OSError, ValueError) as e:           # URLError, timeout, reset …
                kod, bas, cevap = 0, {}, b""
                son = f"bağlantı hatası {type(e).__name__}"
            with self._lock:
                self.istek += 1
            if kod == 204:
                return {}
            if kod in (200, 201):
                try:
                    d = json.loads(cevap.decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise CeviriHatasi("DeepL yanıtı çözülemedi") from None
                if not isinstance(d, dict):
                    raise CeviriHatasi("DeepL yanıtı beklenen biçimde değil")
                return d
            if kod in (401, 403):
                raise self._dur(DeepLAnahtarGecersiz(
                    f"DeepL anahtarı geçersiz ya da yetkisiz (HTTP {kod})"))
            if kod == 456:
                raise self._dur(DeepLKotaDoldu("DeepL karakter kotası doldu (HTTP 456)"))
            if kod == 0 or kod in self.YENIDEN:
                son = son if kod == 0 else f"HTTP {kod}"
                if i + 1 < self.deneme:
                    bas = {str(k).lower(): v for k, v in (bas or {}).items()}
                    try:
                        bekle = float(bas.get("retry-after", ""))
                    except ValueError:
                        bekle = self.geri_cekilme * (2 ** i) * (0.75 + random.random() / 2)
                    self._uyu(min(120.0, max(0.0, bekle)))
                continue
            mesaj = ""
            try:
                mesaj = str(json.loads(cevap.decode("utf-8")).get("message", ""))
            except (UnicodeError, ValueError, AttributeError):
                pass
            raise CeviriHatasi(f"DeepL isteği reddedildi (HTTP {kod}"
                               + (f": {self._gizle(mesaj)[:200]}" if mesaj else "") + ")")
        raise CeviriHatasi(f"DeepL yanıt vermedi ({son}, {self.deneme} deneme)")

    def kalan_kota(self) -> int | None:
        """Remaining characters this billing period (None = no limit reported)."""
        sayi, sinir = self.kullanim()
        if not isinstance(sinir, int) or not isinstance(sayi, int):
            return None
        return max(0, sinir - sayi)

    def kullanim(self) -> tuple[int | None, int | None]:
        d = self._cagir("GET", "/v2/usage")
        return d.get("character_count"), d.get("character_limit")

    # --- glossary ---------------------------------------------------------------------
    def _sozluk_tsv(self, dil: str) -> str:
        return "\n".join(f"{k}\t{v[dil]}" for k, v in sorted(self.sozluk.items())
                         if dil in v and "\t" not in k + v[dil])

    def sozluk_kimligi(self, dil: str) -> str | None:
        """This language's glossary id (created once; older oto-eq ones deleted)."""
        with self._sozluk_lock:
            if dil in self._sozluk_id:
                return self._sozluk_id[dil]
            tsv = self._sozluk_tsv(dil)
            kimlik = None
            if tsv:
                ad = f"oto-eq {dil} {hashlib.sha1(tsv.encode('utf-8')).hexdigest()[:10]}"
                liste = self._cagir("GET", "/v2/glossaries").get("glossaries") or []
                for g in liste:
                    if g.get("name") == ad and g.get("ready", True):
                        kimlik = g.get("glossary_id")
                if kimlik is None:
                    kimlik = self._cagir("POST", "/v2/glossaries", {
                        "name": ad, "source_lang": "tr", "target_lang": dil,
                        "entries": tsv, "entries_format": "tsv"}).get("glossary_id")
                for g in liste:
                    if (str(g.get("name", "")).startswith(f"oto-eq {dil} ")
                            and g.get("name") != ad and g.get("glossary_id")):
                        try:
                            self._cagir("DELETE", f"/v2/glossaries/{g['glossary_id']}")
                        except CeviriHatasi:
                            pass
            self._sozluk_id[dil] = kimlik
            return kimlik

    def onbellek_motoru(self, dil: str, metin: str) -> str:
        """Cache engine key: method + context + the glossary entries this text hits, so a
        changed term re-translates only the texts containing it."""
        isabet = sorted({m.group(0) for m in self._sozluk_re.finditer(metin)}) \
            if self._sozluk_re else []
        oz = "\x1e".join([DEEPL_YONTEM, self.belirtec, self.baglam or "",
                          *(f"{k}={self.sozluk[k].get(dil, '')}" for k in isabet)])
        return "deepl." + hashlib.sha1(oz.encode("utf-8")).hexdigest()[:10]

    # --- translate ----------------------------------------------------------------------
    def _gonder(self, metinler: list[str], dil: str, baglamli: bool = True) -> list[str]:
        veri = {"text": metinler, "source_lang": "TR", "target_lang": DEEPL_HEDEF[dil],
                "preserve_formatting": True}
        if self.belirtec == "etiket":
            veri.update(tag_handling="xml", ignore_tags=["k"])
        if self.baglam and baglamli:
            veri["context"] = self.baglam
        gid = self.sozluk_kimligi(dil) if self.sozluk else None
        if gid:
            veri["glossary_id"] = gid
        d = self._cagir("POST", "/v2/translate", veri)
        out = [t.get("text") if isinstance(t, dict) else None
               for t in d.get("translations") or []]
        if len(out) != len(metinler):
            raise CeviriHatasi("DeepL yanıtında metin sayısı uyuşmuyor")
        return out

    def toplu(self, isler: list[tuple[str, Maske | None]], dil: str,
              onbellek: Onbellek | None = None) -> dict[str, str | Exception]:
        """Masked texts (with their masks) → raw ``@N@`` results; ≤50 texts per request,
        multi-line texts split per line. Results are cached."""
        parcali = []                       # (metin, [(segment, sep)], [index in gonder])
        gonder: list[str] = []
        for metin, maske in isler:
            bol = satirlara_bol(metin, maske)
            idx = []
            for seg, _sep in bol:
                if _HARFLI.search(_TOKEN.sub("", seg)):
                    idx.append(len(gonder))
                    gonder.append(deepl_kodla(seg.strip(), maske, self.belirtec))
                else:
                    idx.append(None)
            parcali.append((metin, maske, bol, idx))
        ham: list[str | None] = []
        try:
            for i in range(0, len(gonder), DEEPL_TOPLU):
                ham += self._gonder(gonder[i:i + DEEPL_TOPLU], dil)
            # with ``context`` DeepL sometimes answers "" for short "label: <k>…</k>"
            # texts; those get one more try without it (listed: worth a human look)
            bos = [j for j, c in enumerate(ham) if not (isinstance(c, str) and c.strip())]
            if bos and self.baglam:
                for i in range(0, len(bos), DEEPL_TOPLU):
                    parca = bos[i:i + DEEPL_TOPLU]
                    for j, c in zip(parca, self._gonder([gonder[j] for j in parca], dil,
                                                        baglamli=False)):
                        ham[j] = c
                        if isinstance(c, str) and c.strip():
                            _uyari(f"UYARI [{dil}] bağlamsız çevrildi: {gonder[j]!r} → {c!r}")
        except CeviriHatasi as e:
            return {m: e for m, _mk in isler}
        with self._lock:
            self.karakter += sum(deepl_karakter(m, mk) for m, mk in isler)
        sonuc: dict[str, str | Exception] = {}
        for metin, maske, bol, idx in parcali:
            parcalar, bos = [], False
            for (seg, sep), j in zip(bol, idx):
                if j is None:
                    parcalar.append(seg.strip() + sep)
                    continue
                c = ham[j]
                if not isinstance(c, str) or not c.strip():
                    bos = True
                    break
                parcalar.append(deepl_coz(c, self.belirtec).strip() + sep)
            if bos:
                sonuc[metin] = CeviriHatasi("DeepL boş yanıt verdi")
                continue
            sonuc[metin] = "".join(parcalar).strip()
            if onbellek:
                onbellek.koy(dil, metin, sonuc[metin], motor=self.onbellek_motoru(dil, metin))
        return sonuc


# --- which engine wrote each catalog entry ---------------------------------------------
class MotorKaydi:
    """``<catalog dir>/.kaynak-motor.json``: {dil: {msgid: [motor, sha1(text)[:12]]}}.

    An entry whose catalog text no longer matches its hash was edited by hand."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.veri: dict[str, dict[str, list[str]]] = {}
        try:
            d = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            d = {}
        if isinstance(d, dict):
            for dil, kay in d.items():
                if dil in DEEPL_HEDEF and isinstance(kay, dict):
                    self.veri[dil] = {m: v for m, v in kay.items()
                                      if isinstance(v, list) and len(v) == 2}

    @staticmethod
    def ozet(s: str) -> str:
        return hashlib.sha1(s.encode("utf-8")).hexdigest()[:12]

    def motor(self, dil: str, msgid: str, ceviri: str) -> str:
        """"google" / "deepl" / … , "elle" (hand edited) or "bilinmiyor" (no record)."""
        k = self.veri.get(dil, {}).get(msgid)
        if not k:
            return "bilinmiyor"
        return k[0] if k[1] == self.ozet(ceviri) else "elle"

    def dil_yaz(self, dil: str, katalog: dict[str, str], yazan: dict[str, str]) -> None:
        eski = self.veri.get(dil, {})
        yeni = {}
        for m, c in katalog.items():
            if m in yazan:
                yeni[m] = [yazan[m], self.ozet(c)]
            elif m in eski:
                yeni[m] = eski[m]
        self.veri[dil] = yeni

    def kaydet(self) -> None:
        json_yaz(self.path, {"_aciklama": "tools/i18n_cevir.py: girdiyi yazan motor + metin "
                             "özeti (özet uyuşmuyorsa elle düzeltilmiş; --yukselt ezmez)",
                             **{d: dict(sorted(self.veri[d].items()))
                                for d in sorted(self.veri)}})


@dataclass
class Hazirlik:
    msgid: str
    dil: str
    cekirdek: str = ""
    bas: str = ""
    son: str = ""
    maske: Maske | None = None
    sonuc: str | None = None          # final without Google (glossary / nothing to translate)
    raw: str | None = None            # cached Google output


def hazirla(msgid: str, dil: str, ay: Ayarlar,
            motor: str | Callable[[str, str], str] = "google") -> Hazirlik:
    """``motor``: cache engine key, or a function (dil, masked text) → key."""
    h = Hazirlik(msgid, dil)
    zorunlu = ay.terimler.tam.get(msgid, {}).get(dil)
    if zorunlu:
        h.sonuc = zorunlu
        return h
    h.cekirdek = msgid.strip()
    h.bas = msgid[:len(msgid) - len(msgid.lstrip())]
    h.son = msgid[len(msgid.rstrip()):]
    if not h.cekirdek:
        h.sonuc = msgid
        return h
    h.maske = maskele(h.cekirdek, dil, ay.terimler)
    if not _HARFLI.search(_TOKEN.sub("", h.maske.metin)):
        h.raw = h.maske.metin                       # nothing translatable left
    elif ay.onbellek:
        h.raw = ay.onbellek.al(dil, h.maske.metin,
                               motor(dil, h.maske.metin) if callable(motor) else motor)
    return h


# Turkish writes the percent sign first ("%{yuzde:.0f}"); the engines often keep that order.
# Same convention as oto_eq/ui_text.percent(): "54%" (en/zh/ja), "54 %" with NBSP (these);
# test_i18n_araclar keeps the two sets in sync.
YUZDE_BOSLUKLU = frozenset({"de", "fr", "ru", "es", "pt"})
_YUZDE_ONCE = re.compile(r"%(\{[^{}]*\})")


def yuzde_sirasi(s: str, dil: str) -> str:
    """'%{yuzde:.0f}' → '{yuzde:.0f}%' / '{yuzde:.0f}\u00a0%' (target-language order)."""
    ara = "\u00a0" if dil in YUZDE_BOSLUKLU else ""
    return _YUZDE_ONCE.sub(lambda m: m.group(1) + ara + "%", s)


def bitir(h: Hazirlik, raw: str) -> str:
    """Repair + restore + VERIFY one Google output; CeviriHatasi when unsafe."""
    if h.sonuc is not None:
        return h.sonuc
    maske, dil = h.maske, h.dil
    s = bitisikleri_ayir(belirtecleri_onar(raw.strip(), maske), maske, dil)
    belirtec_denetle(s, maske)
    s = buyuk_harf_uyumla(h.cekirdek, s, dil)
    s = geri_koy(s, maske).strip()
    s = noktalama_uyumla(h.cekirdek, s, dil)
    s = yuzde_sirasi(s, dil)
    try:
        beklenen = yer_tutucular(h.msgid)
        olan = yer_tutucular(s)
    except ValueError as e:
        raise CeviriHatasi(f"çeviride yer tutucu sözdizimi bozuk: {e}") from None
    if olan != beklenen:
        raise CeviriHatasi(f"yer tutucular uyuşmuyor: {dict(beklenen)} ≠ {dict(olan)}")
    if not s:
        raise CeviriHatasi("boş çeviri")
    return h.bas + s + h.son


def bir_metni_cevir(msgid: str, dil: str, cevirici: Cevirici, ay: Ayarlar) -> str:
    """Translate + verify one msgid (no batching); CeviriHatasi when the result is unsafe."""
    h = hazirla(msgid, dil, ay)
    if h.sonuc is not None:
        return h.sonuc
    raw = h.raw if h.raw is not None else _google_tek(cevirici, h.maske.metin, dil, ay)
    return bitir(h, raw)


# --- catalogs ----------------------------------------------------------------------------
def katalog_oku(path: Path) -> dict[str, str]:
    try:
        d = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)} \
        if isinstance(d, dict) else {}


@dataclass
class Rapor:
    cevrilen: Counter = field(default_factory=Counter)
    korunan: Counter = field(default_factory=Counter)
    silinen: Counter = field(default_factory=Counter)
    kalan: Counter = field(default_factory=Counter)        # not attempted (run stopped)
    hatalar: list[tuple[str, str, str]] = field(default_factory=list)   # (dil, msgid, neden)
    engellendi: str = ""
    devam: str = GoogleEngeli.devam
    cikis: int = 0                                         # exit code of a stopped run
    istek: int = 0
    yukseltilen: Counter = field(default_factory=Counter)  # --yukselt: re-translated
    tahmin: dict[str, int] = field(default_factory=dict)   # DeepL characters per language
    atlanan: dict[str, tuple[int, int]] = field(default_factory=dict)  # dil → (tahmin, kalan)
    kota: int | None = None                                # remaining before the run

    def yaz(self, out=None) -> None:
        out = out or sys.stdout
        for dil in sorted(set(self.cevrilen) | set(self.korunan) | set(self.silinen)):
            if dil in self.atlanan:
                continue
            h = sum(1 for d, *_ in self.hatalar if d == dil)
            print(f"{dil}: çevrildi {self.cevrilen[dil]}, korundu {self.korunan[dil]}, "
                  f"silindi {self.silinen[dil]}, hatalı {h}"
                  + (f", yükseltildi {self.yukseltilen[dil]}" if self.yukseltilen[dil] else "")
                  + (f", yapılmadı {self.kalan[dil]}" if self.kalan[dil] else ""), file=out)
        for dil, msgid, neden in self.hatalar:
            print(f"HATA [{dil}] {msgid!r}: {neden}", file=out)
        for dil, (tahmin, kalan) in self.atlanan.items():
            print(f"ATLANDI [{dil}]: tahmini {tahmin} karakter > kalan kota {kalan} — "
                  "katalog değişmedi; kota yenilenince aynı komutla çevrilir.", file=out)
        if self.engellendi:
            print(f"YARIM KALDI: {self.engellendi} — önbellek kaydedildi; {self.devam}",
                  file=out)


def _parcala(metinler: list[str], n: int, karakter: int,
             olcu: Callable[[str], int] = lambda m: len(m) + 1) -> list[list[str]]:
    parcalar, cur, uz = [], [], 0
    for m in metinler:
        b = olcu(m)
        if cur and (len(cur) >= max(1, n) or uz + b > karakter):
            parcalar.append(cur)
            cur, uz = [], 0
        cur.append(m)
        uz += b
    if cur:
        parcalar.append(cur)
    return parcalar


def _sira(dil: str) -> int:
    return DIL_SIRASI.index(dil) if dil in DIL_SIRASI else len(DIL_SIRASI)


def kataloglari_guncelle(msgids: list[str], diller: list[str], cikti_dizini: Path,
                         fabrika: Fabrika | None, ay: Ayarlar, zorla: bool = False,
                         isci: int = 4, ilerleme: bool = False, deepl: DeepL | None = None,
                         yukselt: bool = False) -> Rapor:
    """Update the catalogs with Google (``fabrika``) or, when given, ``deepl``.

    ``yukselt``: also re-translate existing entries written by another engine (or of
    unknown origin) — never hand-edited ones; a failed upgrade keeps the old text."""
    motor = "deepl" if deepl is not None else "google"
    rapor = Rapor()
    cikti_dizini = Path(cikti_dizini)
    kayit = MotorKaydi(cikti_dizini / MOTOR_DOSYASI)
    msgids = sorted(set(msgids))
    diller = sorted(dict.fromkeys(diller), key=_sira)
    sonuc: dict[str, dict[str, str]] = {}
    yazan: dict[str, dict[str, str]] = {}                     # dil → msgid → engine
    yedek: dict[tuple[str, str], str] = {}                    # upgrade: old text
    bekleyen: dict[str, dict[str, list[Hazirlik]]] = {}       # dil → masked text → entries

    def kaydet_sonuc(h: Hazirlik, raw: str | Exception) -> None:
        try:
            if isinstance(raw, Exception):
                raise raw
            sonuc[h.dil][h.msgid] = bitir(h, raw)
            yazan[h.dil][h.msgid] = motor
            rapor.cevrilen[h.dil] += 1
            if (h.dil, h.msgid) in yedek:
                rapor.yukseltilen[h.dil] += 1
        except CeviriHatasi as e:
            if (h.dil, h.msgid) in yedek:                 # keep the working old text
                sonuc[h.dil][h.msgid] = yedek[(h.dil, h.msgid)]
                rapor.hatalar.append((h.dil, h.msgid, f"{e} (eski çeviri korundu)"))
                return
            sonuc[h.dil][h.msgid] = ""
            yazan[h.dil][h.msgid] = motor
            rapor.hatalar.append((h.dil, h.msgid, str(e)))

    for dil in diller:
        eski = katalog_oku(cikti_dizini / f"{dil}.json")
        rapor.silinen[dil] = len(set(eski) - set(msgids))
        sonuc[dil], yazan[dil] = {}, {}
        for m in msgids:
            if eski.get(m) and not zorla:
                if not yukselt or kayit.motor(dil, m, eski[m]) in ("elle", motor):
                    sonuc[dil][m] = eski[m]
                    rapor.korunan[dil] += 1
                    continue
                yedek[(dil, m)] = eski[m]
            h = hazirla(m, dil, ay, deepl.onbellek_motoru if deepl is not None else motor)
            if h.sonuc is not None or h.raw is not None:
                kaydet_sonuc(h, h.sonuc if h.sonuc is not None else h.raw)
            else:
                bekleyen.setdefault(dil, {}).setdefault(h.maske.metin, []).append(h)

    atlanan: set[str] = set()
    if deepl is not None and bekleyen:
        try:
            kalan = rapor.kota = deepl.kalan_kota()
        except (CalismaDurdu, CeviriHatasi) as e:
            rapor.engellendi = str(e)
            rapor.devam = getattr(e, "devam", CalismaDurdu.devam)
            rapor.cikis = getattr(e, "kod", 3)
            kalan, atlanan = None, set(bekleyen)
        for d in sorted(bekleyen, key=_sira):
            rapor.tahmin[d] = sum(deepl_karakter(t, hs[0].maske) for t, hs in bekleyen[d].items())
            if d in atlanan or kalan is None:
                continue
            if rapor.tahmin[d] > kalan:                   # never start half a language
                atlanan.add(d)
                rapor.atlanan[d] = (rapor.tahmin[d], kalan)
            else:
                kalan -= rapor.tahmin[d]
    if deepl is not None:
        isler = [(d, parca) for d in sorted(bekleyen, key=_sira) if d not in atlanan
                 for parca in _parcala(list(bekleyen[d]), min(DEEPL_TOPLU, ay.toplu),
                                       DEEPL_BAYT, deepl_bayt)]

        def calis(d: str, parca: list[str]) -> dict[str, str | Exception]:
            return deepl.toplu([(t, bekleyen[d][t][0].maske) for t in parca], d, ay.onbellek)
    else:
        isler = [(d, parca) for d in sorted(bekleyen, key=_sira)
                 for parca in _parcala(list(bekleyen[d]), ay.toplu, ay.toplu_karakter)]
        ceviriciler = {d: fabrika(d) for d in bekleyen}

        def calis(d: str, parca: list[str]) -> dict[str, str | Exception]:
            return _google_toplu(ceviriciler[d], parca, d, ay)
    rapor.istek = len(isler)
    lock = threading.Lock()
    yapilan: set[tuple[str, str]] = set()
    try:
        ex = ThreadPoolExecutor(max_workers=max(1, isci))
        futs = {ex.submit(calis, d, parca): (d, parca) for d, parca in isler}
        try:
            for n, fut in enumerate(as_completed(futs), 1):
                d, parca = futs[fut]
                try:
                    cevap = fut.result()
                except CalismaDurdu as e:
                    if isinstance(e, GoogleEngeli):
                        rapor.engellendi = f"Google istekleri engelledi ({e})"
                    else:
                        rapor.engellendi = str(e)
                    rapor.devam, rapor.cikis = e.devam, e.kod
                    break
                with lock:
                    for metin in parca:
                        yapilan.add((d, metin))
                        for h in bekleyen[d][metin]:
                            kaydet_sonuc(h, cevap[metin])
                if ilerleme and (n % 10 == 0 or n == len(futs)):
                    print(f"  istek {n}/{len(futs)}", file=sys.stderr)
        finally:
            if ay.hiz:
                ay.hiz.durdu = ay.hiz.durdu or bool(rapor.engellendi)
            if deepl is not None and rapor.engellendi and deepl.durdu is None:
                deepl.durdu = CalismaDurdu(rapor.engellendi)
            ex.shutdown(wait=True, cancel_futures=True)
    finally:
        if ay.onbellek:
            ay.onbellek.kaydet()
    for d, grup in bekleyen.items():
        if d in atlanan:
            continue
        for metin, hs in grup.items():
            if (d, metin) not in yapilan:
                rapor.kalan[d] += len(hs)
    for dil in diller:
        if dil in atlanan:                               # quota: catalog left untouched
            continue
        kat = {m: sonuc[dil].get(m, yedek.get((dil, m), "")) for m in msgids}
        json_yaz(cikti_dizini / f"{dil}.json", kat)
        kayit.dil_yaz(dil, kat, yazan[dil])
    kayit.kaydet()
    rapor.hatalar.sort()
    return rapor


def _venv_gerekli(argv: list[str]) -> None:
    """deep-translator missing → re-exec with the venv python, or explain how to make it."""
    try:
        import deep_translator  # noqa: F401
        return
    except ImportError:
        pass
    py = VENV / "bin" / "python"
    if py.exists() and Path(sys.prefix).resolve() != VENV.resolve():
        os.execv(str(py), [str(py), str(Path(__file__).resolve()), *argv])
    sys.exit("HATA: deep-translator bulunamadı. Sistem geneline KURMA; özel ortam oluştur:\n"
             f"  python3 -m venv {VENV}\n  {VENV}/bin/pip install deep-translator\n"
             f"sonra: {VENV}/bin/python tools/i18n_cevir.py …")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(description="kaynak.json → <kod>.json (DeepL ya da Google, "
                                             "maskeli).")
    ap.add_argument("--kaynak", type=Path, default=KAYNAK_DOSYASI)
    ap.add_argument("--cikti-dizini", type=Path, default=KATALOG_DIZINI)
    ap.add_argument("--diller", default=",".join(DILLER), help="virgüllü: en,de,…")
    ap.add_argument("--onbellek", type=Path, default=ONBELLEK)
    ap.add_argument("--terimler", type=Path, default=TERIMLER)
    ap.add_argument("--motor", choices=("deepl", "google"),
                    help="varsayılan: DeepL anahtarı varsa deepl, yoksa google")
    ap.add_argument("--anahtar-dosyasi", type=Path, default=DEEPL_ANAHTAR,
                    help="DeepL anahtarı (ilk satır; DEEPL_AUTH_KEY önce gelir)")
    ap.add_argument("--yukselt", action="store_true",
                    help="başka motorla (ör. Google) yazılmış ya da kaynağı bilinmeyen girdileri "
                         "bu motorla yeniden çevir; elle düzeltilenleri ezme")
    ap.add_argument("--kota", action="store_true", help="yalnız DeepL kotasını göster (çeviri yok)")
    ap.add_argument("--zorla", action="store_true", help="mevcut (elle düzeltilmiş) girdileri de ez")
    ap.add_argument("--isci", type=int, help="paralel istek (≤8; google 4, deepl 2)")
    ap.add_argument("--aralik", type=float, default=0.4, help="Google: istekler arası en az saniye")
    ap.add_argument("--toplu", type=int, help="istek başına metin (google 20, deepl 50; 1 = tek tek)")
    ap.add_argument("--deneme", type=int, default=4)
    ap.add_argument("--engel-bekleme", type=float, default=60.0,
                    help="429'da tüm isteklerin bekleme süresi (sn, her seferde ×2)")
    ap.add_argument("--kuru", action="store_true", help="ağa çıkma, yazma: maskeleri göster")
    ap.add_argument("--rapor", type=Path, help="hataları JSON olarak da yaz")
    a = ap.parse_args(argv)
    diller = [d.strip() for d in a.diller.split(",") if d.strip()]
    bilinmeyen = [d for d in diller if d not in DILLER]
    if bilinmeyen:
        ap.error(f"bilinmeyen dil: {','.join(bilinmeyen)} (geçerli: {','.join(DILLER)})")
    anahtar = None
    if a.motor != "google" and not a.kuru:
        anahtar = deepl_anahtari(a.anahtar_dosyasi)
    motor = a.motor or ("deepl" if anahtar else "google")
    if motor == "deepl" and not anahtar and not a.kuru:
        print(f"HATA: DeepL anahtarı yok — DEEPL_AUTH_KEY ortam değişkenine ya da "
              f"{a.anahtar_dosyasi} dosyasının ilk satırına koy (chmod 600).", file=sys.stderr)
        return 2
    if a.kota:
        if motor != "deepl":
            ap.error("--kota yalnız DeepL içindir (anahtar bulunamadı)")
        try:
            sayi, sinir = DeepL(anahtar, deneme=a.deneme).kullanim()
        except CalismaDurdu as e:
            print(f"HATA: {e} — {e.devam}", file=sys.stderr)
            return e.kod
        except CeviriHatasi as e:
            print(f"HATA: {e}", file=sys.stderr)
            return 3
        kalan = sinir - sayi if isinstance(sinir, int) and isinstance(sayi, int) else None
        print(f"DeepL kotası: kullanılan {sayi} / {sinir} karakter, kalan {kalan}")
        return 0
    try:
        msgids = list(kaynak_oku(a.kaynak))
    except (OSError, ValueError) as e:
        sys.exit(f"HATA: kaynak okunamadı ({e}); önce: python3 tools/i18n_cikar.py")
    terimler = Terimler.oku(a.terimler)
    if a.kuru:
        for m in msgids:
            mk = maskele(m.strip(), diller[0], terimler)
            print(f"{m!r}\n  → {mk.metin!r}  {list(zip(mk.tur, mk.kaynak))}")
            if motor == "deepl":
                print(f"  DeepL: {deepl_kodla(mk.metin, mk)!r}")
        return 0
    deepl = None
    if motor == "google":
        _venv_gerekli(argv)
        ay = Ayarlar(terimler=terimler, onbellek=Onbellek(a.onbellek),
                     hiz=HizSiniri(a.aralik, engel_bekleme=a.engel_bekleme), deneme=a.deneme,
                     toplu=a.toplu or 20)
        isci = min(8, a.isci or 4)
    else:
        deepl = DeepL(anahtar, deneme=max(a.deneme, 6))
        ay = Ayarlar(terimler=terimler, onbellek=Onbellek(a.onbellek), deneme=a.deneme,
                     toplu=min(DEEPL_TOPLU, a.toplu or DEEPL_TOPLU))
        isci = min(8, a.isci or 2)
    t0 = time.monotonic()
    try:
        rapor = kataloglari_guncelle(msgids, diller, a.cikti_dizini, google_fabrikasi, ay,
                                     zorla=a.zorla, isci=isci, ilerleme=True, deepl=deepl,
                                     yukselt=a.yukselt)
    except KeyboardInterrupt:
        print("Kesildi — önbellek kaydedildi; aynı komutla kaldığı yerden devam eder.",
              file=sys.stderr)
        return 130
    rapor.yaz()
    if a.rapor:
        json_yaz(a.rapor, [{"dil": d, "msgid": m, "neden": n} for d, m, n in rapor.hatalar])
    ek = ""
    if deepl is not None:
        ek = (f", DeepL ~{deepl.karakter} karakter gönderildi"
              + (f" (önceden kalan kota {rapor.kota})" if rapor.kota is not None else ""))
    print(f"{len(msgids)} metin × {len(diller)} dil [{motor}], {rapor.istek} istek{ek}, "
          f"{time.monotonic() - t0:.1f} sn → {a.cikti_dizini}")
    if rapor.engellendi:
        return rapor.cikis or 3
    if rapor.atlanan:
        return 4
    return 1 if rapor.hatalar else 0


if __name__ == "__main__":
    sys.exit(main())
