"""Shared UI wording (pure, no Qt): used by cli, tray and app.

Texts are Turkish msgids translated with ``_()`` (CONTRACT CHANGE 8); module-level
constants are translated at import time (the language is fixed per process).

Terms: "profil" (CS2/Oyun/Müzik/Film), "mod" (Otomatik/Sabit/Kapalı),
"Düz" = EQ'suz ama seviye eşitli geçiş (oto_eq_duz çıkışı).
"""
from __future__ import annotations

import re

from . import i18n
from .i18n import N_, _
from .models import FLAT_PROFILE, PROFILE_LABELS, sink_name

FLAT_SINK = sink_name(FLAT_PROFILE)
FLAT_LABEL = PROFILE_LABELS.get(FLAT_PROFILE) or _("Düz")
FLAT_TOOLTIP = _("EQ'suz, seviye eşitli")
NO_EQ_LABEL = _("EQ yok")
NO_EQ_TOOLTIP = _("EQ'suz, doğrudan varsayılan çıkışa")
OFF_EXPLAIN = _("EQ kapalı (seviye eşitli düz geçiş)")
MODE_LABELS = {"auto": _("Otomatik"), "fixed": _("Sabit"), "off": _("Kapalı")}


def profile_label(profile: str | None) -> str:
    """'CS2' / 'Müzik' ...; 'EQ yok' for None."""
    if not profile:
        return NO_EQ_LABEL
    return PROFILE_LABELS.get(profile, profile)


def assignment_profile_label(profil: str | None) -> str:
    """Label for a rule/assignment profile; '' / None = flat (Düz)."""
    return PROFILE_LABELS.get(profil, profil) if profil else FLAT_LABEL


def is_flat(stream: dict) -> bool:
    return not stream.get("profile") and stream.get("sink") == FLAT_SINK


def stream_profile_label(stream: dict) -> str:
    """Chip text for a status stream: profile label, 'Düz' or 'EQ yok'."""
    if stream.get("profile"):
        return profile_label(stream["profile"])
    return FLAT_LABEL if is_flat(stream) else NO_EQ_LABEL


def stream_profile_tooltip(stream: dict) -> str:
    if stream.get("profile"):
        return _("{profil} profili").format(profil=profile_label(stream["profile"]))
    return FLAT_TOOLTIP if is_flat(stream) else NO_EQ_TOOLTIP


def mode_text(status: dict | None, explain_off: bool = False) -> str:
    """'Otomatik' / 'Sabit: CS2' / 'Kapalı' (+ explanation)."""
    if not status:
        return _("bilinmiyor")
    mode = status.get("mode")
    if mode == "fixed":
        return _("Sabit: {profil}").format(profil=profile_label(status.get("fixed_profile")))
    if mode == "off" and explain_off:
        return _("Kapalı — {aciklama}").format(aciklama=OFF_EXPLAIN)
    return MODE_LABELS.get(mode, str(mode))


# --- genre variants ("tür alt profilleri") -------------------------------------
from .models import GROUP_LABELS, VARIANT_PROFILES, variant_groups  # noqa: E402

VARIANT_KIND_LABELS = {"muzik": _("Müzik türü"), "oyun": _("Oyun türü")}
GENRE_KINDS = {"sanatci": ("muzik", _("Sanatçı")), "oyun": ("oyun", _("Oyun"))}


def group_label(group: str | None) -> str:
    return GROUP_LABELS.get(group or "", group or _("Genel"))


def _router_label(d: dict, group: str | None) -> str:
    """Label of a genre group: our own translation when the key is known (the router's
    "label" is in ITS language, maybe not ours); else the router's text."""
    if group is not None and group in GROUP_LABELS:      # "" = Genel is a known key too
        return GROUP_LABELS[group]
    return d.get("label") or group_label(group)


def variant_of(status: dict | None, profile: str) -> dict | None:
    v = ((status or {}).get("variants") or {}).get(profile)
    return v if isinstance(v, dict) else None


def variant_text(v: dict | None, with_for: bool = True) -> str:
    """'Metal / Rock — Virgin Black' (+ ' · sabit' when pinned)."""
    if not v:
        return ""
    text = _router_label(v, v.get("group"))
    if with_for and v.get("for"):
        text += f" — {v['for']}"
    if v.get("mode") == "fixed":
        text += " · " + _("sabit")
    return text


# Genre sources: display names for the "source" text written by tur.py / router
# ("MusicBrainz: gothic metal", "iTunes: Dance", "ses analizi: bas ağır, 124 BPM" …).
SOURCE_NAMES = {
    "musicbrainz": "MusicBrainz", "itunes": "iTunes", "deezer": "Deezer",
    "youtube": _("YouTube etiketleri"), "youtube etiketleri": _("YouTube etiketleri"),
    "steam": "Steam", "steamspy": "SteamSpy",
    "ses analizi": _("Ses analizi"), "şarkı adı": _("Şarkı adı"),
    "kullanıcı": _("Kullanıcı ataması"), "yerleşik liste": _("Yerleşik liste"),
    "önbellek": _("Önbellek"), "çevrimiçi": _("Çevrimiçi"),
    # untranslated keys the router / tur.py put into "source" (machine-readable there)
    "yerleşik veritabanı (wikidata)": _("Yerleşik veritabanı (Wikidata)"),
    # shown lower-case as before (only translated now)
    "kullanıcı (sabit)": _("kullanıcı (sabit)"),
    "tür bilinmiyor": _("tür bilinmiyor"),
    "tür bilinmiyor (çevrimdışı)": _("tür bilinmiyor (çevrimdışı)"),
    "tür bilinmiyor (yanıt gecikti)": _("tür bilinmiyor (yanıt gecikti)"),
}
# detail suffixes tur.py appends after "Kaynak: …" (also untranslated keys)
SOURCE_SUFFIXES = {"(zayıf)": _("(zayıf)"), "(zayıf ipucu)": _("(zayıf ipucu)")}
# Order used by the resolver (CONTRACT CHANGE 4) — shown on Ayarlar and in `kulak tur`.
GENRE_SOURCE_ORDER = (_("senin atamaların"), _("şarkı adı ipuçları"), _("yerleşik liste"),
                      _("Wikidata veritabanı"), "iTunes", "Deezer", _("YouTube etiketleri"),
                      "MusicBrainz", _("ses analizi"))
GENRE_SOURCES_TEXT = _("Müzik türü şu sırayla aranır: {sira}. Oyun türü: Steam / SteamSpy "
                       "etiketleri.").format(sira=" → ".join(GENRE_SOURCE_ORDER))
GENRE_PRIVACY_TEXT = _("Gizlilik: bilgisayardan yalnızca sanatçı + şarkı adı, YouTube video "
                       "kimliği ve oyun kimliği çıkar; o da sadece Çevrimiçi açıkken. Ses analizi "
                       "tamamen bu bilgisayarda yapılır, ses kaydedilmez ve hiçbir yere "
                       "gönderilmez.")


def _source_key(name: str) -> str:
    """Stable (untranslated) key of a source name: 'ses analizi', 'kullanıcı', 'itunes'…"""
    return name.strip().lower()


def _source_name(name: str) -> str:
    return SOURCE_NAMES.get(_source_key(name), name.strip())


def source_text(source: str | None) -> str:
    """Readable genre source: 'iTunes — Dance', 'Ses analizi — bas ağır', 'Kullanıcı ataması'."""
    s = (source or "").strip()
    if not s:
        return ""
    name, sep, detail = s.partition(": ")
    if sep and name and len(name) <= 32:
        detail = detail.strip()
        for raw, shown in SOURCE_SUFFIXES.items():
            if detail == raw[1:-1]:                    # weak tier without details: "zayıf"
                detail = shown[1:-1] if shown[:1] == "(" and shown[-1:] == ")" else shown
            elif detail.endswith(" " + raw):
                detail = detail[: -len(raw)] + shown
        return f"{_source_name(name)} — {detail}" if detail else _source_name(name)
    return _source_name(s)


def variant_tooltip(v: dict | None) -> str:
    if not v:
        return ""
    lines = [variant_text(v, with_for=False)]
    if v.get("for"):
        lines.append(_("İçerik: {icerik}").format(icerik=v["for"]))
    if v.get("source"):
        lines.append(_("Kaynak: {kaynak}").format(kaynak=source_text(v["source"])))
    lines.append(_("Seçim: elle sabitlendi") if v.get("mode") == "fixed" else _("Seçim: otomatik"))
    return "\n".join(lines)


def normalize_game_key(key: str) -> str:
    """A game key as the resolver compares it (tur._game_id / GenreResolver._override):
    lower-case prefix, ``exe:`` without ``.exe`` and spaces, ``oyun:`` normalised. Typed
    as shown ("exe:PathOfExileSteam.exe", "Steam:2694490") it was saved but never matched."""
    key = (key or "").strip()
    pre, sep, rest = key.partition(":")
    if not sep:
        return key                      # a bare title: matched against the game's name
    pre, rest = pre.strip().lower(), rest.strip()
    try:
        from . import tur
        n = tur.norm
    except Exception:  # noqa: BLE001 - tur.py is optional here
        def n(s: str) -> str:
            return " ".join(s.casefold().split())
    if pre == "steam":
        return f"steam:{rest}"
    if pre == "exe":
        stem = re.sub(r"\.exe$", "", rest, flags=re.I)
        return f"exe:{n(stem).replace(' ', '')}"
    if pre == "oyun":
        return f"oyun:{n(rest)}"
    return key


def normalize_artist(name: str) -> str:
    """Key form of an artist name for tur-atamalari.json (A2's normalizer if available)."""
    try:
        from . import tur
        fn = getattr(tur, "normalize_artist", None)
        if callable(fn):
            return fn(name)
    except Exception:  # noqa: BLE001 - tur.py is optional here
        pass
    return " ".join(name.lower().split())


def variant_override_key(v: dict | None, kind: str) -> str:
    """Override key for the playing content: status key if given, else derived."""
    if not v:
        return ""
    key = str(v.get("key") or "")
    if kind == "sanatci":
        if key.startswith("sanatci:"):
            return key.split(":", 1)[1]
        return normalize_artist(v.get("for") or "") if v.get("for") else ""
    return key if key and not key.startswith("sanatci:") else ""


# --- Durum page: "Akıllı özellikler" tiles (pure; app.py renders them) ----------
# A tile is a dict: {"title", "value", "lines": [str], "tip", "state"} where state
# is "on" (working), "hot" (acting right now), "wait" (learning / waiting) or "off".
MINUS = "−"
NBSP = "\u00a0"


# --- numbers / dates in the UI language (CHANGE 8): not machine-translated -------
DECIMAL_COMMA = frozenset({"tr", "de", "es", "fr", "pt", "ru"})
PERCENT_SPACE = frozenset({"de", "fr", "ru", "es", "pt"})       # "78 %"
MONTHS = {
    "tr": "Oca Şub Mar Nis May Haz Tem Ağu Eyl Eki Kas Ara",
    "en": "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec",
    "de": "Jan Feb Mär Apr Mai Jun Jul Aug Sep Okt Nov Dez",
    "es": "ene feb mar abr may jun jul ago sep oct nov dic",
    "fr": "janv. févr. mars avr. mai juin juil. août sept. oct. nov. déc.",
    "pt": "jan fev mar abr mai jun jul ago set out nov dez",
    "ru": "янв фев мар апр мая июн июл авг сен окт ноя дек",
}


def ondalik(text: str) -> str:
    """'1.5' → '1,5' in comma languages (Turkish, German, …); unchanged otherwise."""
    return text.replace(".", ",") if i18n.dil() in DECIMAL_COMMA else text


def fmt_db(v, unit: bool = False) -> str:
    """Number for a dB value in the UI language: '+1,5' / '+1.5', '0', '−1' (+ ' dB')."""
    try:
        x = round(float(v), 1)
    except (TypeError, ValueError):
        return "?"
    if x == 0:
        s = "0"
    else:
        body = ondalik(f"{abs(x):.1f}".rstrip("0").rstrip("."))
        s = ("+" if x > 0 else MINUS) + body
    return s + " dB" if unit else s


def percent(conf) -> str:
    """0.78 / 78 → '%78' (Turkish order) / '78%' / '78 %'; '' when unknown."""
    if isinstance(conf, bool) or not isinstance(conf, (int, float)):
        return ""
    v = round(conf * 100 if conf <= 1 else conf)
    lang = i18n.dil()
    if lang == "tr":
        return f"%{v}"
    return f"{v}{NBSP}%" if lang in PERCENT_SPACE else f"{v}%"


def percent_text(value: float, decimals: int = 0) -> str:
    """A percentage already in 0..100 with ``decimals``: '%3,5' / '3.5%' / '3,5 %'."""
    num = ondalik(f"{float(value):.{decimals}f}")
    lang = i18n.dil()
    if lang == "tr":
        return f"%{num}"
    return f"{num}{NBSP}%" if lang in PERCENT_SPACE else f"{num}%"


def short_date(day: int, month: int, clock: str) -> str:
    """'27 Eki 09:45' / 'Oct 27 09:45' / '10月27日 09:45' (month 1..12)."""
    lang = i18n.dil()
    if lang in ("zh", "ja"):
        return f"{month}月{day}日 {clock}"
    m = MONTHS.get(lang, MONTHS["en"]).split()[month - 1]
    return f"{m} {day} {clock}" if lang == "en" else f"{day} {m} {clock}"


TONE_SHORT = {"bas": _("Bas"), "vokal": _("Vokal"), "parlaklik": _("Parlaklık")}


def tone_text(vals: dict | None) -> str:
    """'Bas +1,5 · Vokal 0 · Parlaklık −1'."""
    vals = vals if isinstance(vals, dict) else {}
    return " · ".join(f"{name} {fmt_db(vals.get(k, 0))}" for k, name in TONE_SHORT.items())


def tone_changes_text(vals: dict | None) -> str:
    """Only the non-zero bands: 'Bas +1' ('' when all 0)."""
    vals = vals if isinstance(vals, dict) else {}
    return " · ".join(f"{name} {fmt_db(vals[k])}" for k, name in TONE_SHORT.items()
                      if isinstance(vals.get(k), (int, float)) and round(float(vals[k]), 1) != 0)


def _short_name(source: str | None) -> str:
    """The raw source name of a 'name: detail' source ('' when unknown / too long)."""
    s = (source or "").strip()
    if not s:
        return ""
    name, sep, _detail = s.partition(":")
    if sep and name.strip() and len(name.strip()) <= 24:
        return name
    return s if len(s) <= 24 else ""


def source_short_key(source: str | None) -> str:
    """Untranslated key of the source name ('itunes', 'ses analizi', 'kullanıcı'; '' unknown)."""
    name = _short_name(source)
    return _source_key(name) if name else ""


def source_short(source: str | None) -> str:
    """Just the source name: 'iTunes: Dance' → 'iTunes', 'ses analizi: …' → 'Ses analizi'."""
    name = _short_name(source)
    return _source_name(name) if name else ""


def _variant_focus(status: dict) -> str | None:
    """Which variant profile the genre tile talks about (dominant, else what plays)."""
    variants = status.get("variants")
    if not isinstance(variants, dict) or not variants:
        return None
    playing = [s.get("profile") for s in status.get("streams") or [] if isinstance(s, dict)]
    order = [status.get("dominant")] + playing + ["muzik", "oyun"]
    for p in order:
        if p in VARIANT_PROFILES and isinstance(variants.get(p), dict):
            return p
    return None


def genre_tile(status: dict | None) -> dict | None:
    """'Müzik türü' tile; None when the router sends no variants (older router)."""
    if not status:
        return None
    p = _variant_focus(status)
    if p is None:
        return None
    v = status["variants"][p]
    title = VARIANT_KIND_LABELS.get(p, _("Tür"))
    playing = any(isinstance(s, dict) and s.get("profile") == p for s in status.get("streams") or [])
    who = (v.get("for") or "").strip()
    track = (v.get("track") or "").strip()
    group = v.get("group") or ""
    if not playing and not who and not group:
        wait = (_("Müzik çalınca türü burada görünür") if p == "muzik"
                else _("Oyun çalınca türü burada görünür"))
        return {"title": title, "value": _("Bekliyor"), "state": "off",
                "lines": [wait],
                "tip": GENRE_SOURCES_TEXT}
    value = _router_label(v, group)
    src = source_short(v.get("source"))
    src_key = source_short_key(v.get("source"))
    if src and (group or src_key == "kullanıcı"):
        value += f" · {src.replace(' ', NBSP)}"       # the source name wraps as one unit
    if v.get("mode") == "fixed":
        value += " · " + _("sabit")
    lines = []
    if who and track and track.lower() not in who.lower():
        lines.append(f"{who} — {track}")
    elif who or track:
        lines.append(who or track)
    if not group and v.get("mode") != "fixed":
        lines.append(_("Tür bulunamadı, nötr ayar"))
    ses = v.get("ses")
    if isinstance(ses, dict) and (not ses.get("for") or not who or ses.get("for") == who):
        # the analysis label is more specific than the group ("Elektronik" in
        # "Elektronik / Hip-hop"): keep the router's text here
        sl = ses.get("label") or (group_label(ses.get("group")) if ses.get("group")
                                  else _("belirsiz"))
        pct = percent(ses.get("confidence"))
        if src_key == "ses analizi" and group and ses.get("group") == group:
            value += f"{NBSP}{pct}" if pct and v.get("mode") != "fixed" else ""   # same answer: no 2nd line
        elif pct:
            lines.append(_("ses analizi: {tur} {yuzde}").format(tur=sl, yuzde=pct))
        else:
            lines.append(_("ses analizi: {tur}").format(tur=sl))
    tip = variant_tooltip(v)
    return {"title": title, "value": value, "lines": lines, "tip": tip, "state": "on"}


def kisma_tile(status: dict | None) -> dict | None:
    """'Sohbette kısma' tile from ``status['kisma']`` (Ducker.status()); None if absent."""
    k = (status or {}).get("kisma")
    if not isinstance(k, dict) or "acik" not in k:
        return None
    title = _("Sohbette kısma")
    hedef = k.get("hedef_db") or 6
    profs = ", ".join(profile_label(p) for p in k.get("profiller") or []) or _("diğer sesler")
    hedef_db = fmt_db(-abs(hedef), True)
    tip = _("Sesli sohbette biri konuşunca {profiller} sesi {db} kısılır, susunca yavaşça geri "
            "gelir. Komut: kulak kisma ac / kapat / db=6").format(profiller=profs, db=hedef_db)
    if not k.get("acik"):
        return {"title": title, "value": _("Kapalı"), "lines": [_("Açmak için: kulak kisma ac")],
                "tip": tip, "state": "off"}
    sesli = ", ".join(k.get("sesli") or [])
    now_db = k.get("kisma_db")
    now_db = abs(float(now_db)) if isinstance(now_db, (int, float)) else 0.0
    if k.get("aktif") and (k.get("konusma") or now_db >= 0.5):
        shown = now_db if now_db >= 0.5 else abs(float(hedef))
        lines = [_("{profiller} kısıldı").format(profiller=profs)]
        if sesli:
            lines.append(_("{kisiler} konuşuyor").format(kisiler=sesli))
        return {"title": title,
                "value": _("Konuşma var · {db}").format(db=fmt_db(-shown, True)),
                "lines": lines, "tip": tip, "state": "hot"}
    if k.get("aktif"):
        line = (_("{kisiler} dinleniyor").format(kisiler=sesli) if sesli
                else _("Sesli sohbet dinleniyor"))
        return {"title": title, "value": _("Hazır"),
                "lines": [line, _("konuşunca {profiller} {db}").format(profiller=profs,
                                                                       db=hedef_db)],
                "tip": tip, "state": "on"}
    return {"title": title, "value": _("Hazır"),
            "lines": [_("Sesli sohbette {profiller} {db} kısılır").format(profiller=profs,
                                                                          db=hedef_db)],
            "tip": tip, "state": "on"}


def tone_tile(data: dict | None, status: dict | None = None) -> dict | None:
    """'Kişisel ton' tile from kisisel.load(); clickable (→ Ekolayzır)."""
    if not isinstance(data, dict):
        return None
    genel = data.get("genel") if isinstance(data.get("genel"), dict) else {}
    tur_raw = data.get("tur") if isinstance(data.get("tur"), dict) else {}
    tur = {k: v for k, v in tur_raw.items() if tone_changes_text(v)}
    title = _("Kişisel ton")
    tip = _("Bas sıcaklığı, vokal netliği ve parlaklık: her profilin üstüne eklenen kendi "
            "zevkin (CS2'de kapalı). Ekolayzır sayfasında ayarlanır; kör A/B testi de orada.")
    if not tone_changes_text(genel) and not tur:
        return {"title": title, "value": _("Ayarlanmadı"), "lines": [_("Ekolayzır'dan ayarla →")],
                "tip": tip, "state": "off", "link": True}
    lines = []
    p = _variant_focus(status or {}) if status else None
    v = ((status or {}).get("variants") or {}).get(p) if p else None
    key = f"{p}:{v.get('group')}" if isinstance(v, dict) and v.get("group") else ""
    if key in tur:
        lines.append(_("bu tür için ayrıca: {ton}").format(ton=tone_changes_text(tur[key])))
    elif tur:
        lines.append(_("türe özel ayar: {n}").format(n=len(tur)))
    else:
        lines.append(_("Tüm profillerde (CS2 hariç)"))
    lines.append(_("Değiştir →"))
    return {"title": title, "value": tone_text(genel), "lines": lines, "tip": tip,
            "state": "on", "link": True}


def level_tile(info: dict | None) -> dict | None:
    """'Ses seviyesi' tile: limiter / loudness leveller / crossfeed per profile."""
    if not isinstance(info, dict):
        return None
    lim = [profile_label(p) for p in info.get("sinirlayici") or []]
    ag = [profile_label(p) for p in info.get("seviye") or []]
    xf = [profile_label(p) for p in info.get("crossfeed") or []]
    value = _("Sınırlayıcı açık") if lim else _("Sınırlayıcı kapalı")
    lines = [_("seviye dengeleme: {profiller}").format(profiller=", ".join(ag)) if ag
             else _("seviye dengeleme kapalı")]
    if xf:
        lines.append(_("crossfeed: {profiller}").format(profiller=", ".join(xf)))
    tip = []
    if lim:
        tip.append(_("Sınırlayıcı ({profiller}): ani tepeleri yakalar, böylece genel ses daha "
                     "yüksek olabilir ve patlama olmaz.").format(profiller=", ".join(lim)))
    if ag:
        tip.append(_("Seviye dengeleme ({profiller}): sessiz ve gürültülü bölümleri yavaşça "
                     "dengeler (fısıltı da patlama da rahat duyulur).").format(
                         profiller=", ".join(ag)))
    if isinstance(info.get("seviye_db"), (int, float)):
        tip.append(_("Profiller arası ortak seviye: {db}").format(
            db=fmt_db(info["seviye_db"], True)))
    return {"title": _("Ses seviyesi"), "value": value, "lines": lines, "tip": "\n".join(tip),
            "state": "on" if lim or ag else "off"}


def model_tile(info: dict | None) -> dict | None:
    """'Kişisel model' tile: learns the genre from the user's tray corrections."""
    if not isinstance(info, dict):
        return None
    title = _("Kişisel model")
    need = int(info.get("gerek") or 20)
    n = int(info.get("duzeltme") or 0)
    neden = (info.get("neden") or "").strip()
    tip = _("Tepsideki «Bu şarkı aslında…» düzeltmelerinden öğrenir; yalnızca tahminleri "
            "gerçekten iyileştirdiğinde devreye girer. Komut: kulak model durum")
    if neden:
        tip += "\n" + _("Son eğitim: {neden}").format(neden=neden)
    if info.get("aktif"):
        return {"title": title, "value": _("Etkin"), "state": "on",
                "lines": [_("Tür tahminleri senin düzeltmelerinle iyileşiyor")], "tip": tip}
    if n >= need:
        return {"title": title, "value": _("Eğitime hazır"), "state": "wait",
                "lines": [_("{n} düzeltme toplandı").format(n=n), "kulak model egit"], "tip": tip}
    return {"title": title,
            "value": _("Öğreniyor {n}/{gerek} düzeltme").format(n=n, gerek=need),
            "state": "wait", "lines": [_("Tepsiden tür düzelttikçe öğrenir")], "tip": tip}


def onarim_metni(entry: dict, alan: str) -> str:
    """"neden"/"sonuc" of an onarim.json entry in the CURRENT UI language (its msgid record
    "<alan>_k"), else the stored text (written in the router's language at that time)."""
    return i18n.kayittan(entry.get(alan + "_k"), str(entry.get(alan) or ""))


def onarim_tile(entries: list | None, now: float | None = None) -> dict | None:
    """'Kendini onarma' tile from onarim.json history; None when nothing happened yet."""
    import time as _time
    real = [e for e in entries or [] if isinstance(e, dict) and not e.get("deneme")]
    if not real:
        return None
    last = real[-1]
    ts = last.get("zaman")
    now = _time.time() if now is None else now
    when = ""
    if isinstance(ts, (int, float)):
        lt, ln = _time.localtime(ts), _time.localtime(now)
        when = _time.strftime("%H:%M", lt)
        if (lt.tm_year, lt.tm_yday) != (ln.tm_year, ln.tm_yday):
            when = short_date(lt.tm_mday, lt.tm_mon, when)
    recent = isinstance(ts, (int, float)) and now - ts < 3600
    lines = [onarim_metni(last, "neden")]
    if len(real) > 1:
        lines.append(_("toplam {n} kayıt").format(n=len(real)))
    tip = "\n".join(f"{onarim_metni(e, 'neden')} → {onarim_metni(e, 'sonuc')}" for e in real[-5:])
    return {"title": _("Kendini onarma"),
            "value": _("Son: {zaman}").format(zaman=when) if when else _("Kayıt var"),
            "lines": [ln for ln in lines if ln], "tip": tip, "state": "wait" if recent else "on"}


# --- "Yenilikler" card (one-time; shown until closed) ---------------------------
YENILIKLER_SURUM = "2026-10-25"
# (title, text, target page key or None, anchor, button text)
YENILIKLER = (
    (_("Kişisel ton"), _("Bas, vokal ve parlaklığı kendi zevkine göre ayarla; istersen kör A/B "
     "testiyle kulağın seçsin."), "eq", "ton", _("Ekolayzır'da aç")),
    (_("Daha iyi tür algılama"), _("iTunes, Deezer ve YouTube etiketleri eklendi; adı "
     "bulunamayan şarkıda ses analizi devreye girer."), "ayarlar", "ses_analizi",
     _("Ayarlar'da aç")),
    (_("Sohbette kısma"), _("Sesli sohbette biri konuşunca oyun, müzik ve film sesi kısılır, "
     "susunca geri gelir."), None, "", ""),
    (_("Akıllı ses seviyesi"), _("Sınırlayıcı ve seviye dengeleme: daha yüksek ses, ani "
     "patlama yok."), None, "", ""),
    (_("Mikrofon kalibrasyonu"), _("10 saniye konuş; gürültü kapısı ve kazanç sesine göre "
     "önerilsin."), "mikrofon", "kalibrasyon", _("Mikrofon'da aç")),
)
YENILIKLER_TEPSI = _("Tür yanlışsa: tepsi simgesine sağ tıkla → «Bu şarkı aslında…». "
                     "Düzelttikçe kişisel model senden öğrenir.")

# --- output device: headphone type (CONTRACT CHANGE 7) --------------------------
# status["kulaklik"] = kulaklik.Aygit.durum() (or None: no default output / older router):
# ad, aciklama (model), tur, etiket, kulaklik (EQ runs here), duzeltme (HS80 curve applied),
# neden, baglanti, bt_profil. Every helper below returns "" / None without it.
# Sentences are whole templates with named placeholders (for later translation).
BT_CALL_PROFILES = ("headset-head-unit", "headset-audio-gateway", "hfp", "hsp", "handsfree")
KULAKLIK_BEKLE = _("Varsayılan çıkış bir kulaklık olunca (USB, Bluetooth ya da 3,5 mm) EQ "
                   "kendiliğinden devreye girer.")
KULAKLIK_DEGIL = _("Varsayılan çıkış bir kulaklık değil: {tur} ({model}).")
DUZELTME_ACIK = _("HS80 düzeltmesi açık")
DUZELTME_NOTR = _("HS80 düzeltmesi nötr")
DUZELTME_YOK_NOT = _("Bu kulaklıkta ({model}) HS80 düzeltmesi uygulanmıyor; eğri HS80 için "
                     "ölçüldü.")
DUZELTME_YOK_GENEL = _("Bu kulaklıkta HS80 düzeltmesi uygulanmıyor; eğri HS80 için ölçüldü.")
DUZELTME_TIP = _("HS80 düzeltmesi (AutoEQ eğrisi) yalnız Corsair HS80 için ölçüldü. Başka bir "
                 "kulaklıkta uygulanmaz (nötr kalır); profil bantları, kişisel ton ve dinamik "
                 "her kulaklıkta çalışır.")
ARAMA_MODU = _("Bluetooth arama modu (dar bant): ses kalitesi düşük")
ARAMA_MODU_TIP = _("Kulaklık Bluetooth arama modunda ({profil}): mikrofon açık olduğu için ses "
                   "dar bantlı ve kalitesiz. Müzik ve oyun için A2DP (yüksek kalite) profiline "
                   "geç: KDE ses ayarları → Bluetooth aygıtı → Profil.")
KULAKLIK_ON = "{model} · {tur} · {duzeltme}"          # no words: nothing to translate
KULAKLIK_OFF = _("{model} · {tur} — EQ devre dışı ({neden})")
KULAKLIK_KISA = "{model} ({tur})"
KULAKLIK_NEDEN = _("Algılama: {neden}")


def kulaklik_of(status: dict | None) -> dict | None:
    """status["kulaklik"] as a dict, or None (older router / no default output)."""
    k = (status or {}).get("kulaklik")
    return k if isinstance(k, dict) else None


def kulaklik_model(k: dict) -> str:
    return str(k.get("aciklama") or k.get("ad") or "—")


def kulaklik_tur(k: dict) -> str:
    # the router writes "etiket" in ITS language: prefer our own label for its type key,
    # else translate the text again (works when the router speaks Turkish = the msgid)
    from .kulaklik import TUR_ETIKET            # lazy: keep ui_text's imports light
    tur = k.get("tur")
    if isinstance(tur, str) and tur in TUR_ETIKET:
        return _(TUR_ETIKET[tur])
    return _(str(k.get("etiket") or tur or N_("Bilinmeyen çıkış")))


def kulaklik_neden(k: dict) -> str:
    """Detection reason from the router (its language) → this process's language; '' if none."""
    return _(str(k["neden"])) if k.get("neden") else ""


def bt_arama_modu(k: dict | None) -> bool:
    """A Bluetooth headset in its call profile (HFP/HSP: narrow band, mic on)."""
    prof = str((k or {}).get("bt_profil") or "").lower()
    return bool(prof) and any(p in prof for p in BT_CALL_PROFILES)


def duzeltme_text(k: dict) -> str:
    return DUZELTME_ACIK if k.get("duzeltme") else DUZELTME_NOTR


def kulaklik_text(status: dict | None) -> str:
    """'CORSAIR HS80 … · USB kulaklık · HS80 düzeltmesi açık' or
    'Ryzen HD Audio Controller · Hoparlör — EQ devre dışı (hat çıkışı portu etkin)'."""
    k = kulaklik_of(status)
    if k is None:
        return ""
    if k.get("kulaklik"):
        text = KULAKLIK_ON.format(model=kulaklik_model(k), tur=kulaklik_tur(k),
                                  duzeltme=duzeltme_text(k))
        return f"{text} · {ARAMA_MODU}" if bt_arama_modu(k) else text
    return KULAKLIK_OFF.format(model=kulaklik_model(k), tur=kulaklik_tur(k),
                               neden=kulaklik_neden(k) or _("kulaklık değil"))


def kulaklik_kisa(status: dict | None) -> str:
    """'CORSAIR HS80 RGB Wireless Gaming Receiver (USB kulaklık)'; '' without the field."""
    k = kulaklik_of(status)
    if k is None:
        return ""
    return KULAKLIK_KISA.format(model=kulaklik_model(k), tur=kulaklik_tur(k))


def kulaklik_degil_text(status: dict | None) -> str:
    """'Varsayılan çıkış bir kulaklık değil: Hoparlör (Ryzen …).' ('' when it is one / unknown)."""
    k = kulaklik_of(status)
    if k is None or k.get("kulaklik"):
        return ""
    return KULAKLIK_DEGIL.format(tur=kulaklik_tur(k), model=kulaklik_model(k))


def duzeltme_yok_notu(status: dict | None) -> str:
    """Note for the EQ page: the HS80 curve is not applied on this headphone ('' otherwise)."""
    k = kulaklik_of(status)
    if k is None or not k.get("kulaklik") or k.get("duzeltme"):
        return ""
    return DUZELTME_YOK_NOT.format(model=kulaklik_model(k))


def kulaklik_tile(status: dict | None) -> dict | None:
    """'Kulaklık' tile (type + HS80 correction); None when the router sends no device."""
    k = kulaklik_of(status)
    if k is None:
        return None
    model, tur = kulaklik_model(k), kulaklik_tur(k)
    neden = KULAKLIK_NEDEN.format(neden=kulaklik_neden(k)) if k.get("neden") else ""
    if not k.get("kulaklik"):
        tip = "\n".join(x for x in (KULAKLIK_BEKLE, neden) if x)
        return {"title": _("Kulaklık"), "value": tur, "state": "off",
                "lines": [model, _("EQ devre dışı")], "tip": tip}
    lines = [model, duzeltme_text(k)]
    tips = [DUZELTME_TIP]
    if bt_arama_modu(k):
        lines.append(ARAMA_MODU)
        tips.append(ARAMA_MODU_TIP.format(profil=k.get("bt_profil")))
    if neden:
        tips.append(neden)
    return {"title": _("Kulaklık"), "value": tur, "lines": lines, "tip": "\n".join(tips),
            "state": "wait" if bt_arama_modu(k) else "on"}


DURUM_AKTIF = _("aktif — {aygit}")
DURUM_PASIF = _("aktif değil — varsayılan çıkış kulaklık değil: {tur} ({model}; {neden})")
DURUM_PASIF_KULAKLIK = _("aktif değil — {aygit}")
DURUM_PASIF_ESKI = _("aktif değil (varsayılan çıkış kulaklık değil)")


def kulaklik_durum(status: dict | None) -> str:
    """`kulak durum` "Kulaklık" row: active? + model, type and HS80 correction."""
    st = status or {}
    k = kulaklik_of(st)
    if st.get("active"):
        return DURUM_AKTIF.format(aygit=kulaklik_text(st)) if k else _("aktif")
    if k is None:
        return DURUM_PASIF_ESKI
    if not k.get("kulaklik"):
        return DURUM_PASIF.format(tur=kulaklik_tur(k), model=kulaklik_model(k),
                                  neden=kulaklik_neden(k) or _("kulaklık değil"))
    return DURUM_PASIF_KULAKLIK.format(aygit=kulaklik_text(st))
