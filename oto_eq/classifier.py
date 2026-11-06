"""Stream classifier: decides category + EQ profile for every playback stream.

Signal order (configurable via ``[genel] sira``), first match wins:
kullanici -> cs2 -> sesli -> sistem -> oyun -> oynatici -> rol -> uygulama
-> tarayici -> bilinmeyen. See config/kurallar.toml and docs/NOTES-detect.md.

``classify`` never raises: on any internal error it returns UNKNOWN with a
reason starting with "hata:".
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import tomllib
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit, unquote

from . import procinfo
from .config import DEFAULTS_DIR, RULES_FILE
from .i18n import N_, _
from .models import (EQ_PROFILES, PROFILE_LABELS, Category, Decision, MediaPlayer,
                     ProcInfo, Stream)

log = logging.getLogger("oto_eq.classifier")

ALL_STEPS = ("kullanici", "cs2", "sesli", "sistem", "oyun", "oynatici", "rol",
             "uygulama", "tarayici")

# Display names of the trace (explain / teshis); the keys stay the config ids ([genel] sira)
_STEP_NAMES = {"atama": N_("atama"), "kullanici": N_("kullanıcı kuralı"), "cs2": "cs2",
               "sesli": N_("sesli"), "sistem": N_("sistem"), "oyun": N_("oyun"),
               "oynatici": N_("oynatıcı"), "rol": N_("rol"), "uygulama": N_("uygulama"),
               "tarayici": N_("tarayıcı")}
# labels of the process-ish names (_Ctx.names); the keys are internal ("flatpak" in _why)
_NAME_LABELS = {"ikili": N_("ikili"), "uygulama": N_("uygulama"), "süreç": N_("süreç"),
                "komut": N_("komut")}
# same msgids as the GUI's category names (app.CATEGORY_LABELS)
CATEGORY_NAMES = {"cs2": "CS2", "oyun": N_("Oyun"), "muzik": N_("Müzik"), "film": N_("Film"),
                  "sesli": N_("Sesli sohbet"), "sistem": N_("Sistem"),
                  "bilinmeyen": N_("Bilinmeyen")}


def step_label(step: str) -> str:
    """Trace name of a classifier step ("tarayici" → "tarayıcı" / "browser")."""
    return _(_STEP_NAMES.get(step, step))


def category_label(cat: "Category | str") -> str:
    """UI name of a category value ("muzik" → "Müzik" / "Music")."""
    v = cat.value if isinstance(cat, Category) else str(cat)
    return _(CATEGORY_NAMES.get(v, v))


def profile_label(profile: str | None) -> str:
    """UI name of an EQ profile; None/"" = no EQ."""
    return PROFILE_LABELS.get(profile, profile) if profile else _("EQ yok")

STEAM_ID_KEYS = ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID")
APPID_RE = re.compile(r"\bAppId=(\d+)")
GENERIC_MEDIA_NAMES = {"", "playback", "audio stream", "audiostream", "audio",
                       "output", "firefox", "chromium", "chrome", "media",
                       "audiocallbackdriver", "webrtc", "sound",
                       # 25 Eki: a tab titled just by the site (YouTube Music sets it while the
                       # next track loads; the stream keeps it) says nothing about the song
                       "youtube music", "youtube", "spotify", "spotify web player", "soundcloud",
                       "deezer", "tidal", "apple music", "yandex music", "fizy", "muud"}

# 25 Eki (CHANGE 6): video calls in a browser are voice (Düz, no EQ) — Meet/Zoom/Teams/Discord…
# User rules can add more under [tarayici] sesli_siteleri / sesli_sekme.
DEFAULT_VOICE_SITES = ["meet.google.com", "zoom.us", "teams.microsoft.com", "teams.live.com",
                       "discord.com", "meet.jit.si", "whereby.com", "web.whatsapp.com",
                       "web.telegram.org"]
DEFAULT_VOICE_TAB = [r"^Meet\s*[-–—|:]", r"^Meet$", r"\bGoogle Meet\b", r"\bZoom (Meeting|Toplantı|Workplace)\b",
                     r"\bMicrosoft Teams\b", r"\bJitsi Meet\b",
                     # Discord web: "#genel | Sunucu - Discord", "Genel | Sunucu | Discord",
                     # "Discord | #genel | Sunucu", "(2) Discord | …" (G2: "- Discord" went to Film
                     # while kisma treated the tab as voice)
                     r"\s[-–|]\s*Discord$", r"^(\(\d+\)\s*)?Discord\s*\|"]

RESOLVE_TTL = 30.0
INFO_TTL = 5.0
FILES_TTL = 2.0

# Hard fallbacks in case even the shipped kurallar.toml is unreadable.
_FALLBACK_RULES: dict = {
    "genel": {"bilinmeyen_profil": "muzik", "sira": list(ALL_STEPS)},
    "eslesme": {"cs2": "cs2", "oyun": "oyun", "muzik": "muzik", "film": "film"},
    "cs2": {"ikili": ["cs2"], "steam_appid": ["730"]},
    "roller": {"event": "sistem", "notification": "sistem", "phone": "sesli",
               "communication": "sesli", "game": "oyun", "music": "muzik",
               "video": "film", "movie": "film"},
    "uygulamalar": {}, "oyun": {}, "oynatici": {"varsayilan": "film"},
    "tarayici": {"varsayilan": "film"},
}

_shipped_lock = threading.Lock()
_shipped: dict | None = None


def shipped_rules() -> dict:
    """The default kurallar.toml shipped in the repo (cached)."""
    global _shipped
    with _shipped_lock:
        if _shipped is None:
            try:
                with open(DEFAULTS_DIR / RULES_FILE, "rb") as f:
                    _shipped = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as e:
                log.error("Varsayılan kurallar okunamadı: %s", e)
                _shipped = _FALLBACK_RULES
        return _shipped


def merge_rules(user: dict | None, defaults: dict | None = None) -> dict:
    """Section-wise merge: user keys override default keys; [[kural]] is user-only."""
    defaults = shipped_rules() if defaults is None else defaults
    user = user or {}
    out: dict = {}
    for sec in set(defaults) | set(user) | set(_FALLBACK_RULES):
        if sec in ("kural", "atama"):
            continue
        base = {**_FALLBACK_RULES.get(sec, {}), **(defaults.get(sec) or {})}
        u = user.get(sec)
        out[sec] = {**base, **u} if isinstance(u, dict) else base
    out["kural"] = list(user.get("kural", defaults.get("kural", [])) or [])
    out["atama"] = list(user.get("atama") or [])
    return out


# ---------------------------------------------------------------- helpers

def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


_GLOBAL_FLAGS = re.compile(r"^\(\?([aiLmsux]+)\)")


class _AnyOf:
    """Fallback when the patterns cannot share one regex (the same named group in two
    patterns): tries them one by one. Same match API as re.Pattern for the classifier."""

    def __init__(self, rxs: list[re.Pattern]):
        self.rxs = rxs

    def _first(self, meth: str, s: str, *a):
        for rx in self.rxs:
            m = getattr(rx, meth)(s, *a)
            if m:
                return m
        return None

    def fullmatch(self, s: str, *a):
        return self._first("fullmatch", s, *a)

    def match(self, s: str, *a):
        return self._first("match", s, *a)

    def search(self, s: str, *a):
        return self._first("search", s, *a)


def _compile_full(patterns, what: str) -> re.Pattern | _AnyOf | None:
    """Combine patterns into one case-insensitive full-match regex.

    Each pattern is validated in the form it is used: inside ``(?:…)``. A leading global
    flag group ("(?i)discord", the idiom of kulaklik_desen) is turned into a scoped one
    ("(?i:discord)"); inside the combined regex it raised PatternError, which made the whole
    classifier fail to build (every stream without EQ)."""
    good = []
    for p in _as_list(patterns):
        p = _scoped(p)
        try:
            re.compile(f"(?:{p})", re.I)
            good.append(f"(?:{p})")
        except re.error as e:
            log.warning("Geçersiz desen atlandı (%s): %r: %s", what, p, e)
    if not good:
        return None
    try:
        return re.compile("|".join(good), re.I)
    except re.error as e:             # e.g. one named group used by two patterns
        log.warning("Desenler birleştirilemedi (%s): %s; tek tek denenecek", what, e)
        return _AnyOf([re.compile(g, re.I) for g in good])


def _scoped(p: str) -> str:
    """A leading global flag group ("(?i)x", the kulaklik_desen idiom) as a scoped one
    ("(?i:x)"): a global flag inside (?:…) / an alternation is an error."""
    m = _GLOBAL_FLAGS.match(p)
    return f"(?{m.group(1)}:{p[m.end():]})" if m else p


def _rule_pattern(v) -> str:
    """One [[kural]] value → one regex source. A TOML list (``ikili = ["a", "b"]``, the form
    of the name lists above) is "any of them": str(list) used to become a character class
    ("['a', 'b']") that matched nearly every name (search fields) or none (full fields)."""
    items = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
    if not items:
        raise ValueError("boş liste")         # journal only (log messages stay Turkish)
    return "|".join(f"(?:{_scoped(p)})" for p in items)


def _compile_each(patterns, what: str) -> list[re.Pattern]:
    out = []
    for p in _as_list(patterns):
        try:
            out.append(re.compile(p, re.I))
        except re.error as e:
            log.warning("Geçersiz desen atlandı (%s): %r: %s", what, p, e)
    return out


def _host(url: str) -> str:
    try:
        h = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    for pre in ("www.", "m."):
        if h.startswith(pre):
            h = h[len(pre):]
    return h


def _ext(path: str) -> str:
    if not path:
        return ""
    p = path
    if "://" in p:
        try:
            p = unquote(urlsplit(p).path)
        except ValueError:
            return ""
    base = re.split(r"[\\/]", p)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


# HLS manifest: a local .m3u8 is a music playlist, a remote one a live stream (IPTV, Twitch
# via streamlink, TV channels…) — the shipped ses_uzantilari list sent those to Müzik
_HLS_EXTS = frozenset({"m3u8"})


def _remote(path: str) -> bool:
    """A network address (http, https, rtmp …), not a local file / file:// URL."""
    p = (path or "").strip().lower()
    return "://" in p and not p.startswith("file:")


_TITLE_SUFFIX = re.compile(
    r"\s+[-–—|•·]\s+(youtube music|youtube|spotify|soundcloud|netflix|twitch|"
    r"mozilla firefox|firefox|chromium|google chrome)\s*$", re.I)


def _norm_title(s: str) -> str:
    s = (s or "").casefold().strip()
    s = re.sub(r"^\(\d+\)\s*", "", s)
    for _i in range(2):
        s = _TITLE_SUFFIX.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _fmt_vals(names: list[tuple[str, str]]) -> str:
    seen, out = set(), []
    for _k, v in names:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return ", ".join(out)


def _fmt_names(names: list[tuple[str, str]]) -> str:
    return ", ".join(f"{_(_NAME_LABELS.get(label, label))}={value}"
                     for label, value in names if value) or "-"


class _SiteList:
    """Plain domains match as suffix; entries with regex syntax are searched."""

    _META = re.compile(r"[*+?\[\]()|^$\\]")

    def __init__(self, entries, what: str):
        self.domains: list[str] = []
        self.regexes: list[re.Pattern] = []
        for e in _as_list(entries):
            e = e.strip()
            if not e:
                continue
            if self._META.search(e):
                # as written (case-insensitive anyway): lower() turned \S \D \W \B into
                # \s \d \w \b, i.e. their opposites
                self.regexes.extend(_compile_each([e], what))
            else:
                e = e.lower()
                self.domains.append(e[4:] if e.startswith("www.") else e)

    def match(self, host: str) -> str | None:
        if not host:
            return None
        for d in self.domains:
            if host == d or host.endswith("." + d):
                return d
        for r in self.regexes:
            if r.search(host):
                return host
        return None


class _Ctx:
    """Everything known about one stream (built once per classify call)."""

    __slots__ = ("stream", "pid", "info", "flatpak", "role", "names",
                 "proc_names", "env", "ancestors", "not_game", "_linked",
                 "_steam_ids")

    def __init__(self, stream: Stream, pid: int | None, info: ProcInfo | None,
                 flatpak: str):
        self.stream = stream
        self.pid = pid
        self.info = info
        self.flatpak = flatpak
        self.role = (stream.media_role or "").strip()
        self.env = dict(info.env) if info else {}
        self.ancestors = info.ancestors if info else ()
        # process-ish names (binary, comm, exe, argv0)
        pn: list[tuple[str, str]] = []
        if stream.binary:
            pn.append(("ikili", stream.binary))
        if info:
            if info.comm:
                pn.append(("süreç", info.comm))
            if info.exe:
                pn.append(("exe", info.exe))
            if info.cmdline:
                a0 = procinfo.basename_any(info.cmdline[0])
                if a0:
                    pn.append(("komut", a0))
        self.proc_names = pn
        names: list[tuple[str, str]] = []
        if flatpak:
            names.append(("flatpak", flatpak))
        names.extend(pn[:1])
        if stream.app_name:
            names.append(("uygulama", stream.app_name))
        names.extend(pn[1:])
        self.names = names
        self.not_game = ""
        self._linked: list[MediaPlayer] | None = None
        self._steam_ids: list[tuple[str, str]] | None = None

    def steam_ids(self) -> list[tuple[str, str]]:
        if self._steam_ids is None:
            ids = []
            for k in STEAM_ID_KEYS:
                v = (self.env.get(k) or "").strip()
                if v and v != "0":
                    ids.append((f"{k}={v}", v))
            for _apid, _acomm, acmd in self.ancestors:
                m = APPID_RE.search(acmd or "")
                if m and m.group(1) != "0":
                    ids.append((_("ata süreci AppId={no}").format(no=m.group(1)), m.group(1)))
            self._steam_ids = ids
        return self._steam_ids


# ---------------------------------------------------------------- classifier

class Classifier:
    """Rule-based stream classifier. ``rules`` = parsed kurallar.toml dict."""

    def __init__(self, rules: dict | None = None,
                 proc_lookup: Callable[[int], ProcInfo | None] | None = None,
                 resolve: Callable[[Stream], int | None] | None = None,
                 files_lookup: Callable[[int], tuple[str, ...]] | None = None,
                 flatpak_lookup: Callable[[int], str] | None = None,
                 artist_lookup: Callable[[str], bool | None] | None = None):
        injected = proc_lookup is not None
        # "is this a known music artist?" (tur.GenreResolver cache only; unknown names
        # are looked up in the background so the NEXT stream benefits)
        if artist_lookup is None:
            artist_lookup = (lambda name: None) if injected else _shared_artist_lookup
        self._artist_lookup = artist_lookup
        self._proc_lookup = proc_lookup or procinfo.proc_info
        self._resolve = resolve or procinfo.resolve_pid
        # Real /proc side lookups only when we also use real proc_info:
        # with injected fakes, fake PIDs must never hit real processes.
        if files_lookup is None:
            files_lookup = (lambda pid: ()) if injected else procinfo.open_files
        if flatpak_lookup is None:
            flatpak_lookup = (lambda pid: "") if injected else procinfo.flatpak_id
        self._files_lookup = files_lookup
        self._flatpak_lookup = flatpak_lookup
        self._real_env = not injected
        self._cache_lock = threading.Lock()
        self._resolve_cache: dict[tuple, tuple[float, int | None]] = {}
        self._info_cache: dict[int, tuple[float, ProcInfo | None, str]] = {}
        self._files_cache: dict[int, tuple[float, tuple[str, ...]]] = {}
        self.rules = merge_rules(rules)
        self._compile()

    # ------------------------------------------------------------ compile
    def _compile(self) -> None:
        r = self.rules
        g = r["genel"]
        sira = [s for s in _as_list(g.get("sira", ALL_STEPS)) if s in ALL_STEPS]
        bad = [s for s in _as_list(g.get("sira", ())) if s not in ALL_STEPS]
        if bad:
            log.warning("[genel] sira içinde bilinmeyen adım(lar): %s", bad)
        self.steps = sira or list(ALL_STEPS)
        self.unknown_profile = self._norm_profile(g.get("bilinmeyen_profil", ""),
                                                  "bilinmeyen_profil")
        es = r["eslesme"]
        self.mapping: dict[Category, str | None] = {}
        for cat in (Category.CS2, Category.GAME, Category.MUSIC, Category.MOVIE):
            self.mapping[cat] = self._norm_profile(es.get(cat.value, cat.value),
                                                   f"eslesme.{cat.value}")
        self.mapping[Category.VOICE] = None
        self.mapping[Category.SYSTEM] = None
        self.mapping[Category.UNKNOWN] = self.unknown_profile

        cs = r["cs2"]
        self.cs2_bin = _compile_full(cs.get("ikili"), "cs2.ikili")
        self.cs2_ids = {s.strip() for s in _as_list(cs.get("steam_appid"))}

        self.roles: dict[str, Category] = {}
        for k, v in (r.get("roller") or {}).items():
            cat = self._category(v)
            if cat is None:
                log.warning("[roller] %s = %r geçersiz kategori", k, v)
                continue
            self.roles[str(k).lower()] = cat

        ua = r["uygulamalar"]
        self.voice_apps = _compile_full(ua.get("sesli"), "uygulamalar.sesli")
        self.system_apps = _compile_full(ua.get("sistem"), "uygulamalar.sistem")
        self.not_game_apps = _compile_full(ua.get("oyun_haric"), "uygulamalar.oyun_haric")
        self.game_apps = _compile_full(ua.get("oyun"), "uygulamalar.oyun")
        self.music_apps = _compile_full(ua.get("muzik"), "uygulamalar.muzik")
        self.movie_apps = _compile_full(ua.get("film"), "uygulamalar.film")

        oy = r["oyun"]
        self.game_env = _as_list(oy.get("ortam", [
            "SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID", "STEAM_COMPAT_DATA_PATH",
            "GAMEID", "UMU_ID", "WINEPREFIX", "WINELOADER", "LUTRIS_GAME_UUID"]))
        self.wine_bins = _compile_full(oy.get("wine_ikili", ["wine", "wine64",
                                       "wine-preloader", "wine64-preloader"]), "oyun.wine_ikili")
        self.game_anc = _compile_each(oy.get("ata", []), "oyun.ata")

        pl = r["oynatici"]
        self.player_apps = _compile_full(pl.get("uygulamalar"), "oynatici.uygulamalar")
        self.audio_ext = {e.lower().lstrip(".") for e in _as_list(pl.get("ses_uzantilari"))}
        self.video_ext = {e.lower().lstrip(".") for e in _as_list(pl.get("video_uzantilari"))}
        self.player_default = self._category(pl.get("varsayilan", "film")) or Category.MOVIE

        tr = r["tarayici"]
        self.browser_apps = _compile_full(tr.get("uygulamalar"), "tarayici.uygulamalar")
        self.music_sites = _SiteList(_as_list(tr.get("muzik_siteleri")) +
                                     _as_list(tr.get("muzik_site_desen")), "tarayici.muzik")
        self.movie_sites = _SiteList(_as_list(tr.get("film_siteleri")) +
                                     _as_list(tr.get("film_site_desen")), "tarayici.film")
        self.voice_sites = _SiteList(_as_list(tr.get("sesli_siteleri")) + DEFAULT_VOICE_SITES,
                                     "tarayici.sesli_siteleri")
        self.voice_tab = _compile_each(_as_list(tr.get("sesli_sekme")) + DEFAULT_VOICE_TAB,
                                       "tarayici.sesli_sekme")
        self.title_sites = _SiteList(tr.get("baslik_siteleri"), "tarayici.baslik_siteleri")
        self.music_title = _compile_each(tr.get("muzik_baslik"), "tarayici.muzik_baslik")
        self.music_artist = _compile_each(tr.get("muzik_sanatci"), "tarayici.muzik_sanatci")
        self.music_url = _compile_each(tr.get("muzik_adres_desen"), "tarayici.muzik_adres_desen")
        self.music_tab = _compile_each(tr.get("muzik_sekme"), "tarayici.muzik_sekme")
        self.movie_tab = _compile_each(tr.get("film_sekme"), "tarayici.film_sekme")
        self.youtube_tab = _compile_each(tr.get("youtube_sekme"), "tarayici.youtube_sekme")
        self.browser_default = self._category(tr.get("varsayilan", "film")) or Category.MOVIE

        self.assignments: list[tuple[str, str, str | None]] = []
        for i, a in enumerate(r.get("atama") or [], 1):
            try:
                alan = str(a.get("alan", "")).strip().lower()
                deger = str(a.get("deger", "")).strip().casefold()
                prof = self._norm_profile(a.get("profil", ""), f"atama #{i}.profil")
            except AttributeError:
                log.warning("atama #%d geçersiz, atlandı", i)
                continue
            if alan not in ("ikili", "uygulama", "flatpak") or not deger:
                log.warning("atama #%d: geçersiz alan/değer, atlandı", i)
                continue
            self.assignments.append((alan, deger, prof))

        self.user_rules = []
        extra_env: set[str] = set()
        for i, raw in enumerate(r.get("kural") or [], 1):
            cr = self._compile_rule(i, raw)
            if cr is not None:
                self.user_rules.append(cr)
                extra_env.update(cr["ortam"].keys())
        self._extra_env = extra_env - set(procinfo.ENV_KEYS)

    @staticmethod
    def _category(v) -> Category | None:
        try:
            return Category(str(v).strip().lower())
        except ValueError:
            return None

    @staticmethod
    def _norm_profile(v, what: str) -> str | None:
        if v is None:
            return None
        v = str(v).strip().lower()
        if not v:
            return None
        if v not in EQ_PROFILES:
            log.warning("%s = %r geçerli bir profil değil (EQ'suz kabul edildi)", what, v)
            return None
        return v

    _RULE_FULL = ("ikili", "uygulama", "medya_rolu", "flatpak", "steam_appid")
    _RULE_SEARCH = ("medya_adi", "ata", "url")
    _RULE_TABLES = ("ortam", "ozellik")

    def _compile_rule(self, i: int, raw) -> dict | None:
        if not isinstance(raw, dict):
            log.warning("kural #%d tablo değil, atlandı", i)
            return None
        name = str(raw.get("ad") or f"#{i}")
        cat = self._category(raw.get("kategori", ""))
        if cat is None or cat == Category.UNKNOWN:
            log.warning("kural %s: geçersiz/eksik kategori %r, atlandı", name, raw.get("kategori"))
            return None
        known = set(self._RULE_FULL + self._RULE_SEARCH + self._RULE_TABLES +
                    ("ad", "kategori", "profil"))
        unknown = set(raw) - known
        if unknown:
            log.warning("kural %s: bilinmeyen alan(lar) yok sayıldı: %s", name, sorted(unknown))
        cr: dict = {"ad": name, "kategori": cat, "ortam": {}, "ozellik": {}}
        if "profil" in raw:
            cr["profil"] = self._norm_profile(raw.get("profil"), f"kural {name}.profil")
            if cat in (Category.VOICE, Category.SYSTEM):
                cr["profil"] = None
        n = 0
        try:
            for f in self._RULE_FULL:
                if f in raw:
                    cr[f] = re.compile(f"(?:{_rule_pattern(raw[f])})", re.I)
                    n += 1
            for f in self._RULE_SEARCH:
                if f in raw:
                    cr[f] = re.compile(_rule_pattern(raw[f]), re.I)
                    n += 1
            for f in self._RULE_TABLES:
                if f in raw:
                    tbl = raw[f]
                    if not isinstance(tbl, dict) or not tbl:
                        raise ValueError(f"{f} bir tablo olmalı")
                    cr[f] = {str(k): re.compile(_rule_pattern(v), re.I) for k, v in tbl.items()}
                    n += 1
        except (re.error, ValueError) as e:
            log.warning("kural %s: geçersiz desen (%s), atlandı", name, e)
            return None
        if n == 0:
            log.warning("kural %s: hiç eşleşme alanı yok, atlandı", name)
            return None
        return cr

    # ------------------------------------------------------------ caching
    def clear_cache(self) -> None:
        with self._cache_lock:
            self._resolve_cache.clear()
            self._info_cache.clear()
            self._files_cache.clear()

    def _cached_pid(self, stream: Stream) -> int | None:
        key = (stream.index, stream.pid, stream.binary)
        now = time.monotonic()
        with self._cache_lock:
            hit = self._resolve_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            pid = self._resolve(stream)
        except Exception:
            log.exception("PID çözümleme hatası")
            pid = None
        with self._cache_lock:
            if len(self._resolve_cache) > 1024:
                self._resolve_cache.clear()
            self._resolve_cache[key] = (now + (RESOLVE_TTL if pid else 2.0), pid)
        return pid

    def _cached_info(self, pid: int | None) -> tuple[ProcInfo | None, str]:
        if not pid:
            return None, ""
        now = time.monotonic()
        with self._cache_lock:
            hit = self._info_cache.get(pid)
        if hit and hit[0] > now:
            return hit[1], hit[2]
        try:
            info = self._proc_lookup(pid)
        except Exception:
            log.exception("proc_info hatası (pid %s)", pid)
            info = None
        fp = ""
        if info is not None:
            fp = (info.env or {}).get("FLATPAK_ID", "")
            if not fp:
                try:
                    fp = self._flatpak_lookup(pid) or ""
                except Exception:
                    fp = ""
            if self._real_env and self._extra_env:
                extra = procinfo.read_env(pid, keys=self._extra_env, prefixes=())
                if extra:
                    info = ProcInfo(pid=info.pid, comm=info.comm, exe=info.exe,
                                    cmdline=info.cmdline, env={**info.env, **extra},
                                    ancestors=info.ancestors)
        with self._cache_lock:
            if len(self._info_cache) > 1024:
                self._info_cache.clear()
            self._info_cache[pid] = (now + INFO_TTL, info, fp)
        return info, fp

    def _cached_files(self, pid: int | None) -> tuple[str, ...]:
        if not pid:
            return ()
        now = time.monotonic()
        with self._cache_lock:
            hit = self._files_cache.get(pid)
        if hit and hit[0] > now:
            return hit[1]
        try:
            files = tuple(self._files_lookup(pid) or ())
        except Exception:
            files = ()
        with self._cache_lock:
            if len(self._files_cache) > 256:
                self._files_cache.clear()
            self._files_cache[pid] = (now + FILES_TTL, files)
        return files

    # ------------------------------------------------------------ public
    def profile_for(self, cat: Category) -> str | None:
        return self.mapping.get(cat)

    def classify(self, stream: Stream, players: list[MediaPlayer] | None = None) -> Decision:
        try:
            return self._classify(stream, [p for p in (players or ()) if isinstance(p, MediaPlayer)])
        except Exception as e:
            log.exception("Sınıflandırma hatası")
            return Decision(Category.UNKNOWN, self.unknown_profile,
                            _("hata: {tur}").format(tur=type(e).__name__))

    def decision(self, cat: Category, reason: str, kaynak: str = "") -> Decision:
        """Public: a decision for ``cat`` with this rule set's category → profile mapping."""
        return replace(self._decision(cat, reason), kaynak=kaynak)

    def _decision(self, cat: Category, reason: str, profile: object = ...) -> Decision:
        if profile is ...:
            profile = self.mapping.get(cat)
        if cat in (Category.VOICE, Category.SYSTEM):
            profile = None
        return Decision(cat, profile, reason)

    def process_of(self, stream: Stream) -> tuple[int | None, ProcInfo | None, str]:
        """(host pid, ProcInfo, flatpak id) as the classifier sees them (cached)."""
        pid = self._cached_pid(stream)
        info, fp = self._cached_info(pid)
        if not fp:
            fp = procinfo._stream_flatpak_hint(stream)
        return pid, info, fp

    def explain(self, stream: Stream, players: list[MediaPlayer] | None = None
                ) -> tuple[Decision, list[str]]:
        """Same decision as classify() plus a trace of every check (UI language)."""
        trace: list[str] = []
        try:
            d = self._classify(stream, [p for p in (players or ()) if isinstance(p, MediaPlayer)],
                               trace)
        except Exception as e:
            log.exception("Sınıflandırma hatası")
            d = Decision(Category.UNKNOWN, self.unknown_profile,
                         _("hata: {tur}").format(tur=type(e).__name__))
            trace.append("✘ " + _("hata: {tur}: {hata}").format(tur=type(e).__name__, hata=e))
        trace.append("→ " + _("karar: {kategori} / {profil} ({neden})").format(
            kategori=category_label(d.category), profil=profile_label(d.profile), neden=d.reason))
        return d, trace

    def _classify(self, stream: Stream, players: list[MediaPlayer],
                  trace: list[str] | None = None) -> Decision:
        if not isinstance(stream, Stream):
            if trace is not None:
                trace.append("✘ " + _("geçersiz akış nesnesi: {tur}").format(
                    tur=type(stream).__name__))
            return Decision(Category.UNKNOWN, self.unknown_profile, _("hata: geçersiz akış"))
        pid, info, fp = self.process_of(stream)
        ctx = _Ctx(stream, pid, info, fp)
        if trace is not None:
            trace.append("• " + _("süreç: akış pid {akis_pid} → host pid {pid} ({komut}), "
                                  "flatpak {flatpak}").format(
                akis_pid=stream.pid, pid=pid, komut=info.comm if info else _("bulunamadı"),
                flatpak=fp or "-"))
            trace.append("• " + _("adlar: {adlar}").format(adlar=_fmt_names(ctx.names)))
        d = self._assignment(ctx)
        if trace is not None:
            trace.append(self._trace_line("atama", d, _("{n} kayıt").format(n=len(self.assignments))))
        if d is not None:
            return d
        for step in self.steps:
            d = getattr(self, f"_step_{step}")(ctx, players)
            if trace is not None:
                try:
                    seen = getattr(self, f"_seen_{step}")(ctx, players)
                except Exception as e:   # trace must never break the decision
                    seen = _("(iz hatası: {hata})").format(hata=e)
                trace.append(self._trace_line(step, d, seen))
            if d is not None:
                return d if d.kaynak else replace(d, kaynak=step)
        reason = _("tanınmadı")
        if ctx.not_game:
            reason = _("tanınmadı ({neden})").format(neden=ctx.not_game)
        elif pid is None:
            reason = _("tanınmadı (süreç bulunamadı)")
        return Decision(Category.UNKNOWN, self.unknown_profile, reason)

    _ASSIGN_CAT = {"cs2": Category.CS2, "oyun": Category.GAME,
                   "muzik": Category.MUSIC, "film": Category.MOVIE}

    def _assignment(self, ctx: _Ctx) -> Decision | None:
        """GUI per-app assignments (rules["atama"]): checked before everything."""
        if not self.assignments:
            return None
        vals = {"ikili": (ctx.stream.binary or "").strip().casefold(),
                "uygulama": (ctx.stream.app_name or "").strip().casefold(),
                "flatpak": (ctx.flatpak or "").strip().casefold()}
        for alan, deger, prof in self.assignments:
            if vals[alan] and vals[alan] == deger:
                cat = self._ASSIGN_CAT.get(prof or "", Category.UNKNOWN)
                return Decision(cat, prof, _("kullanıcı ataması"), kaynak="atama")
        return None

    # ------------------------------------------------------------ matching
    @staticmethod
    def _match(rx: re.Pattern | None, names: list[tuple[str, str]]) -> tuple[str, str] | None:
        if rx is None:
            return None
        for label, value in names:
            if value and rx.fullmatch(value):
                return label, value
        return None

    @staticmethod
    def _why(msg: str, hit: tuple[str, str]) -> str:
        """``msg``: N_() msgid with ``{ad}`` (the matched name); a flatpak id is shown as is."""
        label, value = hit
        if label == "flatpak":
            return f"flatpak {value}"
        return _(msg).format(ad=value)

    def _role_cat(self, ctx: _Ctx) -> Category | None:
        return self.roles.get(ctx.role.lower()) if ctx.role else None

    # ------------------------------------------------------------ steps
    def _step_kullanici(self, ctx: _Ctx, players) -> Decision | None:
        for cr in self.user_rules:
            if self._rule_mismatch(cr, ctx, players) is None:
                prof = cr["profil"] if "profil" in cr else ...
                return self._decision(cr["kategori"], _("kullanıcı kuralı '{ad}'").format(ad=cr["ad"]), prof)
        return None

    def _rule_mismatch(self, cr: dict, ctx: _Ctx, players) -> str | None:
        """Name of the first field that does not match (None = rule matches)."""
        s = ctx.stream
        if "ikili" in cr and not any(cr["ikili"].fullmatch(v) for _, v in ctx.proc_names if v):
            return "ikili"
        if "uygulama" in cr and not cr["uygulama"].fullmatch(s.app_name or ""):
            return "uygulama"
        if "medya_rolu" in cr and not cr["medya_rolu"].fullmatch(ctx.role):
            return "medya_rolu"
        if "flatpak" in cr and not (ctx.flatpak and cr["flatpak"].fullmatch(ctx.flatpak)):
            return "flatpak"
        if "steam_appid" in cr and not any(cr["steam_appid"].fullmatch(v)
                                           for _, v in ctx.steam_ids()):
            return "steam_appid"
        if "medya_adi" in cr and not cr["medya_adi"].search(s.media_name or ""):
            return "medya_adi"
        if "ata" in cr and not any(cr["ata"].search(c or "") or cr["ata"].search(cmd or "")
                                   for _, c, cmd in ctx.ancestors):
            return "ata"
        if "url" in cr and not any(p.url and cr["url"].search(p.url)
                                   for p in self._linked_players(ctx, players)):
            return "url"
        for k, rx in cr["ortam"].items():
            v = ctx.env.get(k)
            if v is None or not rx.search(v):
                return f"ortam.{k}"
        for k, rx in cr["ozellik"].items():
            v = (s.props or {}).get(k)
            if v is None or not rx.search(str(v)):
                return f"ozellik.{k}"
        return None

    # ------------------------------------------------------------ trace (explain only)
    @staticmethod
    def _trace_line(step: str, d: Decision | None, seen: str) -> str:
        if d is None:
            return f"✘ {step_label(step)}: {seen}"
        return f"✔ {step_label(step)}: {seen} ⇒ {category_label(d.category)} ({d.reason})"

    def _seen_kullanici(self, ctx: _Ctx, players) -> str:
        if not self.user_rules:
            return _("kural yok")
        parts = []
        for cr in self.user_rules:
            miss = self._rule_mismatch(cr, ctx, players)
            parts.append(_("'{ad}' uydu").format(ad=cr["ad"]) if miss is None
                         else _("'{ad}' {alan} uymadı").format(ad=cr["ad"], alan=miss))
            if miss is None:
                break
        return "; ".join(parts)

    def _seen_cs2(self, ctx: _Ctx, players) -> str:
        ids = ", ".join(lbl for lbl, _ in ctx.steam_ids()) or "-"
        return _("süreç adları [{adlar}], Steam no: {no}").format(
            adlar=_fmt_vals(ctx.proc_names), no=ids)

    def _seen_sesli(self, ctx: _Ctx, players) -> str:
        return _("ad listesi [uygulamalar.sesli], rol '{rol}'").format(rol=ctx.role or "-")

    def _seen_sistem(self, ctx: _Ctx, players) -> str:
        return _("ad listesi [uygulamalar.sistem], rol '{rol}'").format(rol=ctx.role or "-")

    def _seen_oyun(self, ctx: _Ctx, players) -> str:
        if ctx.not_game:
            return ctx.not_game
        envs = [k for k in self.game_env if (ctx.env.get(k) or "").strip() not in ("", "0")]
        anc = " ← ".join(c for _, c, _ in ctx.ancestors[:6]) or "-"
        return _("ortam: {ortam}; atalar: {atalar}").format(
            ortam=", ".join(envs) or _("yok"), atalar=anc)

    def _seen_oynatici(self, ctx: _Ctx, players) -> str:
        if not self._match(self.player_apps, ctx.names):
            return _("oynatıcı listesinde değil")
        linked = self._linked_players(ctx, players)
        urls = [p.url for p in linked if p.url][:2]
        files = [f for f in self._cached_files(ctx.pid)
                 if _ext(f) in self.audio_ext | self.video_ext][:3]
        return _("MPRIS url: {url}; açık medya: {dosyalar}; rol '{rol}'").format(
            url=urls or "-", dosyalar=files or "-", rol=ctx.role or "-")

    def _seen_rol(self, ctx: _Ctx, players) -> str:
        cat = self._role_cat(ctx)
        return _("rol '{rol}' → {kategori}").format(
            rol=ctx.role or "-", kategori=cat.value if cat else _("eşleme yok"))

    def _seen_uygulama(self, ctx: _Ctx, players) -> str:
        return _("ad listeleri [uygulamalar.muzik / uygulamalar.film]")

    def _seen_tarayici(self, ctx: _Ctx, players) -> str:
        if not self._match(self.browser_apps, ctx.names):
            return _("tarayıcı değil")
        linked = self._linked_players(ctx, players)
        ps = "; ".join(f"{p.bus_name.removeprefix('org.mpris.MediaPlayer2.')}"
                       f"[{p.status}] '{p.title[:40]}' {_host(p.url) or '-'}" for p in linked[:3])
        return _("sekme '{sekme}'; bağlı MPRIS: {mpris}").format(
            sekme=(ctx.stream.media_name or "")[:60], mpris=ps or _("yok"))

    def _media_app(self, ctx: _Ctx) -> bool:
        """A browser / music / video app of the rule lists: never a game, whatever it
        inherited. A browser first opened by a game's link (winebrowser → xdg-open, a
        launcher's "news") keeps the game's WINEPREFIX / SteamAppId / ancestors for the
        whole session: everything it played went to Oyun (SteamAppId=730: CS2)."""
        return any(self._match(rx, ctx.names) for rx in
                   (self.browser_apps, self.music_apps, self.movie_apps, self.player_apps))

    def media_app(self, stream: Stream, proc: ProcInfo | None = None) -> bool:
        """Public (tur.content_id): ``stream`` belongs to a browser / music / video app."""
        try:
            if proc is not None:
                ctx = _Ctx(stream, proc.pid, proc, (proc.env or {}).get("FLATPAK_ID", "")
                           or procinfo._stream_flatpak_hint(stream))
            else:
                ctx = _Ctx(stream, *self.process_of(stream))
            return self._media_app(ctx)
        except Exception:
            log.exception("media_app hatası")
            return False

    def _step_cs2(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.cs2_bin, ctx.proc_names)
        if hit:
            return self._decision(Category.CS2, _("ikili '{ad}'").format(ad=hit[1]))
        if self._media_app(ctx):
            return None                  # an inherited SteamAppId=730 (see _media_app)
        for label, v in ctx.steam_ids():
            if v in self.cs2_ids:
                return self._decision(Category.CS2, label)
        return None

    def _step_sesli(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.voice_apps, ctx.names)
        if hit:
            return self._decision(Category.VOICE, self._why(N_("sesli uygulama '{ad}'"), hit))
        if self._role_cat(ctx) == Category.VOICE:
            return self._decision(Category.VOICE, _("medya rolü '{rol}'").format(rol=ctx.role))
        return None

    def _step_sistem(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.system_apps, ctx.names)
        if hit:
            return self._decision(Category.SYSTEM, self._why(N_("sistem sesi '{ad}'"), hit))
        if self._role_cat(ctx) == Category.SYSTEM:
            return self._decision(Category.SYSTEM, _("medya rolü '{rol}'").format(rol=ctx.role))
        return None

    def _step_oyun(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.not_game_apps, ctx.names)
        if hit:
            ctx.not_game = _("oyun değil: '{ad}'").format(ad=hit[1])
            return None
        hit = self._match(self.game_apps, ctx.names)
        if hit:
            return self._decision(Category.GAME, self._why(N_("bilinen oyun '{ad}'"), hit))
        # a program the rules list as a browser or a music / video player (foobar2000.exe,
        # mpc-hc.exe under Wine; a browser that inherited a game's environment) is not a
        # game, whatever its .exe name or environment says: the later steps classify it
        if self._media_app(ctx):
            return None
        for _label, v in ctx.names:
            if v and v.lower().endswith(".exe"):
                return self._decision(Category.GAME, _("Windows .exe '{ad}'").format(ad=v))
        for k in self.game_env:
            v = (ctx.env.get(k) or "").strip()
            if not v or v == "0":
                continue
            if k in ("WINEPREFIX", "WINELOADER"):
                return self._decision(Category.GAME, _("wine ortamı ({degisken})").format(degisken=k))
            if k == "LUTRIS_GAME_UUID":
                return self._decision(Category.GAME, _("Lutris oyunu"))
            if k in ("GAMEID", "UMU_ID"):
                return self._decision(Category.GAME, f"umu {k}={v[:40]}")
            if "/" in v:
                return self._decision(Category.GAME, _("Proton ortamı ({degisken})").format(degisken=k))
            return self._decision(Category.GAME, f"{k}={v[:40]}")
        hit = self._match(self.wine_bins, ctx.proc_names)
        if hit:
            return self._decision(Category.GAME, _("wine süreci '{ad}'").format(ad=hit[1]))
        for _apid, acomm, acmd in ctx.ancestors:
            for rx in self.game_anc:
                m = rx.search(acomm or "") or rx.search(acmd or "")
                if not m:
                    continue
                am = APPID_RE.search(acmd or "")
                if "steamlaunch" in rx.pattern.lower() and am:
                    return self._decision(Category.GAME, _("Steam oyunu (AppId={no})").format(no=am.group(1)))
                if self.wine_bins is not None and self.wine_bins.fullmatch(acomm or ""):
                    return self._decision(Category.GAME, _("wine ata süreci '{ad}'").format(ad=acomm))
                return self._decision(Category.GAME, _("oyun ata süreci '{ad}'").format(ad=acomm))
        return None

    def _step_rol(self, ctx: _Ctx, players) -> Decision | None:
        cat = self._role_cat(ctx)
        if cat is not None:
            return self._decision(cat, _("medya rolü '{rol}'").format(rol=ctx.role))
        return None

    def _step_uygulama(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.music_apps, ctx.names)
        if hit:
            return self._decision(Category.MUSIC, self._why(N_("müzik uygulaması '{ad}'"), hit))
        hit = self._match(self.movie_apps, ctx.names)
        if hit:
            return self._decision(Category.MOVIE, self._why(N_("film uygulaması '{ad}'"), hit))
        return None

    # ------------------------------------------------------------ players
    @staticmethod
    def _app_root(ctx: _Ctx) -> int:
        """PID of the topmost run of same-name ancestors (the app's main process), 0 if none."""
        root = 0
        own = (ctx.info.comm if ctx.info else "") or ""
        if own:
            for apid, acomm, _acmd in ctx.ancestors:
                if acomm != own:
                    break
                root = apid
        return root

    def _linked_players(self, ctx: _Ctx, players: list[MediaPlayer]) -> list[MediaPlayer]:
        """MPRIS players that belong to the stream's process tree (best first)."""
        if ctx._linked is not None:
            return ctx._linked
        ranked: list[tuple[int, int, MediaPlayer]] = []
        anc_pids = {a[0] for a in ctx.ancestors}
        # Chromium-family browsers (Vivaldi, Chrome, Brave…) and Electron apps play audio
        # from a helper process (audio service); the MPRIS hosts (plasma-browser-integration
        # -host, the browser's own player) are its SIBLINGS under the main process. The
        # topmost run of same-name ancestors is the app root: a player under it belongs to
        # this app (2 Kas: Vivaldi YouTube Mix went to Film — URL player was unlinked).
        root = self._app_root(ctx)
        name_keys = set()
        for _k, v in ctx.names:
            v = v.lower()
            if v:
                name_keys.add(v)
        if ctx.flatpak:
            parts = ctx.flatpak.lower().split(".")
            name_keys.update(p for p in parts[1:] if len(p) > 2 and p != "client")
        is_browser = self._match(self.browser_apps, ctx.names) is not None
        for order, p in enumerate(players):
            rank = None
            if ctx.pid and p.pid:
                if p.pid == ctx.pid:
                    rank = 0
                elif p.pid in anc_pids:
                    rank = 1
                else:
                    pi, _fp = self._cached_info(p.pid)
                    p_anc = {a[0] for a in pi.ancestors} if pi is not None else set()
                    if ctx.pid in p_anc or (root and root in p_anc):
                        rank = 1
            if rank is None:
                bn = p.bus_name.lower()
                suffix = bn[len("org.mpris.mediaplayer2."):] if bn.startswith("org.mpris.mediaplayer2.") else bn
                base = suffix.split(".instance")[0].split(".")[0]
                ident = (p.identity or "").lower()
                if base and base in name_keys:
                    rank = 2
                elif ident and (ident in name_keys or any(
                        k and len(k) > 2 and k in ident.split() for k in name_keys)):
                    rank = 2
                elif is_browser and base == "plasma-browser-integration" and not ctx.pid:
                    rank = 3
            if rank is not None:
                ranked.append((rank, order, p))
        ranked.sort(key=lambda t: (t[0], t[1]))
        if ranked and ranked[0][0] <= 1:
            # process-tree links exist: drop weaker name-only guesses
            ranked = [t for t in ranked if t[0] <= 1]
        ctx._linked = [p for _, _, p in ranked]
        return ctx._linked

    # ------------------------------------------------------------ oynatici
    def _step_oynatici(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.player_apps, ctx.names)
        if not hit:
            return None
        linked = self._linked_players(ctx, players)
        linked = sorted(linked, key=lambda p: p.status != "Playing")
        for p in linked:
            if not p.url:
                continue
            ext = _ext(p.url)
            d = self._ext_decision(ext, "mpris", remote=_remote(p.url))
            if d:
                return d
            host = _host(p.url)
            if host:
                if self.music_sites.match(host):
                    return self._decision(Category.MUSIC, _("oynatıcı: {site}").format(site=host))
                if self.movie_sites.match(host):
                    return self._decision(Category.MOVIE, _("oynatıcı: {site}").format(site=host))
        exts = [_ext(f) for f in self._cached_files(ctx.pid)]
        vid = [e for e in exts if e in self.video_ext]
        aud = [e for e in exts if e in self.audio_ext]
        if vid:
            return self._decision(Category.MOVIE, _("oynatıcı: video dosyası (.{uzanti})").format(uzanti=vid[0]))
        if aud:
            return self._decision(Category.MUSIC, _("oynatıcı: ses dosyası (.{uzanti})").format(uzanti=aud[0]))
        args = ctx.info.cmdline[1:] if ctx.info else ()
        for a in args:
            if a in ("--no-video", "--vid=no", "--video=no", "--no-vid", "-vn", "--novideo"):
                return self._decision(Category.MUSIC, _("oynatıcı: görüntüsüz ({arguman})").format(arguman=a))
        arg_exts = [_ext(a) for a in args if not a.startswith("-")
                    and not (_remote(a) and _ext(a) in _HLS_EXTS)]
        if any(e in self.video_ext for e in arg_exts):
            e = next(e for e in arg_exts if e in self.video_ext)
            return self._decision(Category.MOVIE, _("oynatıcı: video dosyası (.{uzanti})").format(uzanti=e))
        if any(e in self.audio_ext for e in arg_exts):
            e = next(e for e in arg_exts if e in self.audio_ext)
            return self._decision(Category.MUSIC, _("oynatıcı: ses dosyası (.{uzanti})").format(uzanti=e))
        # a title "index.m3u8" is the name of an HLS stream (a local playlist shows its tracks)
        d = self._ext_decision(_ext(ctx.stream.media_name), "baslik", remote=True)
        if d:
            return d
        for a in args:
            if "://" in a:
                host = _host(a)
                if self.music_sites.match(host):
                    return self._decision(Category.MUSIC, _("oynatıcı: {site}").format(site=host))
        rc = self._role_cat(ctx)
        if rc in (Category.MUSIC, Category.MOVIE):
            return self._decision(rc, _("oynatıcı: medya rolü '{rol}'").format(rol=ctx.role))
        return self._decision(self.player_default, _("oynatıcı '{ad}': varsayılan").format(ad=hit[1]))

    _EXT_WHY = {   # (where the extension came from) → (video msgid, audio msgid)
        "mpris": (N_("oynatıcı: MPRIS video (.{uzanti})"),
                  N_("oynatıcı: MPRIS ses dosyası (.{uzanti})")),
        "baslik": (N_("oynatıcı: başlık video (.{uzanti})"),
                   N_("oynatıcı: başlık ses dosyası (.{uzanti})")),
    }

    def _ext_decision(self, ext: str, kaynak: str, remote: bool = False) -> Decision | None:
        """``remote``: the extension comes from a network address (or a stream title):
        an HLS manifest (.m3u8) is then a live stream, video as often as radio → no
        decision (later evidence: site, media role, default)."""
        if remote and ext in _HLS_EXTS:
            return None
        video, audio = self._EXT_WHY[kaynak]
        if ext in self.video_ext:
            return self._decision(Category.MOVIE, _(video).format(uzanti=ext))
        if ext in self.audio_ext:
            return self._decision(Category.MUSIC, _(audio).format(uzanti=ext))
        return None

    # ------------------------------------------------------------ tarayici
    def _music_hint(self, title: str, artist: str) -> str | None:
        for rx in self.music_title:
            m = rx.search(title or "")
            if m:
                return m.group(0).strip()
        for rx in self.music_artist:
            m = rx.search(artist or "")
            if m:
                return (artist or "").strip()
        return None

    def _step_tarayici(self, ctx: _Ctx, players) -> Decision | None:
        hit = self._match(self.browser_apps, ctx.names)
        if not hit:
            return None
        d = self._tarayici_karar(ctx, players)
        mn = _norm_title(ctx.stream.media_name or "")
        if mn in GENERIC_MEDIA_NAMES or len(mn) < 3:
            # CHANGE 9: nameless streams of one browser instance cannot be told apart by
            # metadata (Chromium "Playback"); the router checks how many play at once
            app = (ctx.stream.binary or ctx.stream.app_name or "?").casefold()
            d = replace(d, grup=f"{app}:{self._app_root(ctx) or ctx.pid or 0}")
            if d.category == Category.MOVIE:
                # the shared media session may belong to ANOTHER tab (a muted autoplay
                # video on x.com/YouTube): a film guess for a nameless stream is weak
                d = replace(d, zayif=True)
        return d

    def _tarayici_karar(self, ctx: _Ctx, players) -> Decision:
        media_name = ctx.stream.media_name or ""
        linked = self._linked_players(ctx, players)
        chosen = self._choose_browser_player(ctx, linked)
        if chosen is not None:
            d = self._browser_from_player(chosen, media_name)
            if d is not None:
                return d
        d = self._browser_from_tab(media_name)
        if d is not None:
            return d
        if chosen is not None:
            t = chosen.title or media_name
            hint = self._music_hint(t, chosen.artist)
            if hint:
                return self._decision(Category.MUSIC, _("tarayıcı: müzik ipucu '{ipucu}'").format(ipucu=hint))
        why = (_("tarayıcı: MPRIS yok → varsayılan") if not linked
               else _("tarayıcı: site tanınmadı → varsayılan"))
        return replace(self._decision(self.browser_default, why), zayif=True)

    @staticmethod
    def _choose_browser_player(ctx: _Ctx, linked: list[MediaPlayer]) -> MediaPlayer | None:
        """The MPRIS player that belongs to THIS browser stream (tab), or None.

        Title match against the stream's media.name (= tab title) first; a generic
        media.name ("Playback") takes the most recently started Playing player."""
        if not linked:
            return None
        mn = _norm_title(ctx.stream.media_name or "")
        generic = mn in GENERIC_MEDIA_NAMES or len(mn) < 3

        def title_ok(p: MediaPlayer) -> bool:
            pt = _norm_title(p.title)
            return bool(pt) and len(pt) >= 3 and not generic and (pt in mn or mn in pt)
        matched = [p for p in linked if title_ok(p)]
        if matched:
            # prefer Playing, then one carrying an URL (plasma-browser-integration)
            matched.sort(key=lambda p: (p.status != "Playing", not p.url))
            return matched[0]
        if generic:
            playing = [p for p in linked if p.status == "Playing"]
            if playing:
                playing.sort(key=lambda p: not p.url)   # stable: keeps recency order
                return playing[0]
        return None

    def player_for(self, stream: Stream, players: list[MediaPlayer] | None,
                   proc: ProcInfo | None = None) -> MediaPlayer | None:
        """The MPRIS player bound to ``stream`` (same rules as classification).

        Browsers: tab-title match; other apps: the Playing linked player first.
        ``proc``: use this ProcInfo instead of resolving the stream's process."""
        try:
            players = [p for p in (players or ()) if isinstance(p, MediaPlayer)]
            if proc is not None:
                ctx = _Ctx(stream, proc.pid, proc, (proc.env or {}).get("FLATPAK_ID", "")
                           or procinfo._stream_flatpak_hint(stream))
            else:
                ctx = _Ctx(stream, *self.process_of(stream))
            linked = self._linked_players(ctx, players)
            if self._match(self.browser_apps, ctx.names):
                return self._choose_browser_player(ctx, linked)
            linked = sorted(linked, key=lambda p: p.status != "Playing")
            return linked[0] if linked else None
        except Exception:
            log.exception("player_for hatası")
            return None

    def _browser_from_player(self, p: MediaPlayer, media_name: str) -> Decision | None:
        host = _host(p.url) if p.url else ""
        if host:
            if self.voice_sites.match(host):
                return self._decision(Category.VOICE, _("tarayıcı: görüşme ({site})").format(site=host))
            if self.music_sites.match(host):
                return self._decision(Category.MUSIC, _("tarayıcı: {site}").format(site=host))
            if self.movie_sites.match(host):
                return self._decision(Category.MOVIE, _("tarayıcı: {site}").format(site=host))
            if self.title_sites.match(host):
                for rx in self.music_url:
                    m = rx.search(p.url)
                    if m:
                        return self._decision(Category.MUSIC, _("tarayıcı: YouTube müzik listesi '{liste}'").format(liste=m.group(0)))
                hint = self._music_hint(p.title or media_name, p.artist)
                if hint:
                    return self._decision(Category.MUSIC, _("tarayıcı: YouTube müzik ipucu '{ipucu}'").format(ipucu=hint))
                d = self._known_artist(p.title or media_name)
                if d is not None:
                    return d
                return replace(self._decision(Category.MOVIE, _("tarayıcı: YouTube video")), zayif=True)
            hint = self._music_hint(p.title, p.artist)
            if hint:
                return self._decision(Category.MUSIC, _("tarayıcı: müzik ipucu '{ipucu}' ({site})").format(ipucu=hint, site=host))
            return None
        return None

    def _browser_from_tab(self, media_name: str) -> Decision | None:
        if not media_name:
            return None
        for rx in self.voice_tab:
            m = rx.search(media_name)
            if m:
                return self._decision(Category.VOICE, _("tarayıcı sekmesi: görüşme '{sekme}'").format(sekme=m.group(0)))
        for rx in self.music_tab:
            m = rx.search(media_name)
            if m:
                return self._decision(Category.MUSIC, _("tarayıcı sekmesi: '{sekme}'").format(sekme=m.group(0)))
        for rx in self.movie_tab:
            m = rx.search(media_name)
            if m:
                return self._decision(Category.MOVIE, _("tarayıcı sekmesi: '{sekme}'").format(sekme=m.group(0)))
        for rx in self.youtube_tab:
            if rx.search(media_name):
                hint = self._music_hint(media_name, "")
                if hint:
                    return self._decision(Category.MUSIC, _("tarayıcı: YouTube müzik ipucu '{ipucu}'").format(ipucu=hint))
                d = self._known_artist(media_name)
                if d is not None:
                    return d
                return replace(self._decision(Category.MOVIE, _("tarayıcı: YouTube video")), zayif=True)
        return None

    def _known_artist(self, title: str) -> Decision | None:
        """"Artist - Song" title whose artist the genre cache knows (MusicBrainz) → muzik."""
        from . import tur
        t = re.sub(r"^\(\d+\)\s*", "", title or "")
        t = re.sub(r"\s+[-–—|]\s+YouTube( Music)?\s*$", "", t)
        parsed = tur.split_title(t)
        if not parsed:
            return None
        rest = re.split(r"\s+[-–—|]\s+", t, maxsplit=1)[-1]     # raw: brackets kept
        if _VIDEO_WORDS.search(rest):
            return None          # "Joker - Official Trailer", "Mavi - Sezon Finali": a video
        artist = tur.clean_artist(parsed[0])
        if not artist:
            return None
        try:
            known = self._artist_lookup(artist)
        except Exception:
            known = None
        if known:
            # weak: the first word of a title may only SHARE a known artist's name ("Gece -
            # Uyku İçin Hikaye", "Boston - Travel Guide", "Queen - … Explained"); the content
            # check (icerik, YAMNet) verifies weak decisions and flips a talk to Film
            return replace(self._decision(
                Category.MUSIC, _("tarayıcı: bilinen sanatçı '{sanatci}'").format(sanatci=artist)),
                zayif=True)
        return None


# words / phrases that only video titles use (not "Season of the Witch", "Son Bölüm" songs)
_VIDEO_WORDS = re.compile(
    r"\b(trailer|teaser|fragman|review|inceleme|gameplay|walkthrough|let'?s play|"
    r"episode \d+|\d+\. b[öo]l[üu]m|b[öo]l[üu]m \d+|sezon finali|season finale|"
    r"g[üu]ndem [öo]zeti|g[üu]n[üu]n [öo]zeti|ma[çc] [öo]zeti|haber b[üu]lteni|"
    r"r[öo]portaj|interview|tutorial|full movie|tam film|reaction|highlights|vlog|podcast|"
    # not "how to" ("How to Save a Life") nor single words a song may be named after
    r"documentary|belgesel|explained|travel guide|itinerary|full course|"
    r"nas[ıi]l yap[ıi]l[ıi]r|kurulum rehberi)\b",
    re.I)


def _shared_artist_lookup(name: str) -> bool | None:
    """Cache-only check via the process-wide GenreResolver; queues a background lookup."""
    from . import tur
    return tur.shared_resolver().known_artist(name, request=True)


def _cached_artist_lookup(name: str) -> bool | None:
    """The same, cache only: no network, no worker thread (read-only tools)."""
    from . import tur
    return tur.shared_resolver().known_artist(name, request=False)


def load_classifier(online: bool = True) -> Classifier:
    """Classifier from the user's (or shipped) kurallar.toml.

    ``online=False`` (teshis, ``python3 -m oto_eq.classifier``): the "known artist" check
    reads the genre cache only — a read-only tool must not start lookups or write the
    cache file from its own short-lived process."""
    from . import config
    try:
        if hasattr(config, "load_rules"):
            rules = config.load_rules()
        else:
            rules = config.load_toml(RULES_FILE)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as e:
        log.error("kurallar.toml okunamadı, varsayılanlar kullanılıyor: %s", e)
        rules = {}
    if online:
        return Classifier(rules)
    return Classifier(rules, artist_lookup=_cached_artist_lookup)


def main() -> None:   # pragma: no cover - manual debugging aid
    """`python3 -m oto_eq.classifier`: classify the current streams once."""
    import sys
    logging.basicConfig(level=logging.WARNING)
    try:
        from . import pulse
    except Exception as e:
        sys.stderr.write(_("pulse modülü yok: {hata}").format(hata=e) + "\n")
        return
    from .mpris import MprisWatcher
    w = MprisWatcher()
    w.start()
    time.sleep(0.5)
    c = load_classifier(online=False)
    t0 = time.perf_counter()
    rows = [(s, c.classify(s, w.players())) for s in pulse.list_streams()]
    dt = (time.perf_counter() - t0) * 1000
    for s, d in rows:
        sys.stdout.write(f"#{s.index:<5} {s.app_name[:20]:20} {s.binary[:18]:18} "
                         f"-> {category_label(d.category):12} {profile_label(d.profile):8} "
                         f"{d.reason}\n")
    sys.stdout.write(_("({n} akış, {sure:.1f} ms)").format(n=len(rows), sure=dt) + "\n")
    w.stop()


if __name__ == "__main__":   # pragma: no cover
    main()
