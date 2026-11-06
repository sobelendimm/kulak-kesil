"""`kulak` command line (python3 -m oto_eq.cli).

Exit codes: 0 başarı, 1 hata (servis hata döndürdü / komut başarısız),
2 kullanım hatası (argparse), 3 servis (oto-eq) çalışmıyor.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


from . import config, i18n
from . import ui_text as tx
from .i18n import N_, _
from .models import EQ_PROFILES, PROFILE_LABELS, VARIANT_PROFILES, variant_groups

PROG = "kulak"
EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_DOWN = 0, 1, 2, 3
DOWN_MSG = N_("Kulak Kesil servisi (oto-eq) çalışmıyor — systemctl --user start oto-eq")


class DaemonDown(Exception):
    """The router daemon is unreachable."""


class DaemonError(Exception):
    """The daemon answered ok=false."""


def _ipc():
    from . import ipc  # lazy: keeps `kulak --help` independent of ipc.py
    return ipc


def call(obj: dict, timeout: float = 2.0) -> dict:
    """One IPC request; raises DaemonDown / DaemonError."""
    try:
        resp = _ipc().request(obj, timeout=timeout)
    except (ConnectionError, OSError, TimeoutError) as e:
        raise DaemonDown(str(e)) from e
    if not isinstance(resp, dict) or not resp.get("ok", False):
        err = resp.get("error") if isinstance(resp, dict) else repr(resp)
        raise DaemonError(str(err or _("bilinmeyen hata")))
    if resp.get("uyari"):
        _err(_("uyarı: {uyari}").format(uyari=resp["uyari"]))
    return resp


def _out(s: str = "") -> None:
    print(s, flush=True)


def _err(s: str) -> None:
    print(f"{PROG}: {s}", file=sys.stderr, flush=True)


# --- formatting -----------------------------------------------------------
# Labels come from ui_text / models; `_()` on an already translated (or unknown) text
# returns it unchanged, so wrapping them here is safe whichever module translates.
def plabel(profile: str | None) -> str:
    return _(tx.profile_label(profile))


def slabel(stream: dict) -> str:
    return _(tx.stream_profile_label(stream))


def prof_name(profile: str) -> str:
    """Display name of a profile key ('muzik' → 'Müzik')."""
    return _(PROFILE_LABELS.get(profile, profile))


def group_name(group: str | None) -> str:
    """Display name of a genre group key ('' → 'Genel')."""
    return _(tx.group_label(group))


def kind_name(profile: str) -> str:
    """'Müzik türü' / 'Oyun türü'."""
    return _(tx.VARIANT_KIND_LABELS[profile])


def mode_str(st: dict, explain_off: bool = False) -> str:
    return _(tx.mode_text(st, explain_off))


def stream_app(s: dict) -> str:
    return s.get("app") or s.get("binary") or f"#{s.get('index', '?')}"


# Column widths follow the (translated) text: terminal cells, so CJK counts double.
text_width = i18n.genislik
pad = i18n.sola                           # ``s.ljust(width)`` in terminal cells


def align_rows(rows: list[tuple[str, str]], indent: str = "  ", sep: str = " : ") -> list[str]:
    """['  Ad      : değer', …] with the label column as wide as the widest label."""
    w = max((text_width(k) for k, _v in rows), default=0)
    return [f"{indent}{pad(k, w)}{sep}{v}" for k, v in rows]


def format_status(st: dict) -> str:
    """Aligned status table."""
    rows = [
        (_("Kulaklık"), _(tx.kulaklik_durum(st))),
        (_("Varsayılan çıkış"), st.get("default_sink") or "—"),
        (_("Mod"), mode_str(st, explain_off=True)),
        (_("Baskın profil"), plabel(st.get("dominant")) if st.get("dominant") else "—"),
        (_("EQ çıkışları"), _("hazır") if st.get("sinks_ready")
         else _("HAZIR DEĞİL — systemctl --user status oto-eq-sinks")),
    ]
    for prof in VARIANT_PROFILES:
        v = tx.variant_of(st, prof)
        if v:
            rows.append((kind_name(prof), _(tx.variant_text(v))))
    lines = [_("Kulak Kesil — durum")]
    lines += align_rows(rows)
    streams = st.get("streams") or []
    lines.append("")
    if not streams:
        lines.append("  " + _("Çalan akış yok."))
        return "\n".join(lines)
    lines.append(_("Akışlar ({n}):").format(n=len(streams)))
    aw = min(32, max(len(stream_app(s)) for s in streams))
    pw = max(text_width(slabel(s)) for s in streams)
    for s in streams:
        app = stream_app(s)
        if len(app) > aw:
            app = app[: aw - 1] + "…"
        reason = _(s.get("reason") or "")
        line = f"  {app.ljust(aw)} → {pad(slabel(s), pw)}"
        if reason:
            line += f"  ({reason})"
        lines.append(line.rstrip())
    return "\n".join(lines)


def format_watch_line(st: dict) -> str:
    """One compact line for `kulak izle`."""
    dom = plabel(st.get("dominant")) if st.get("dominant") else "—"
    parts = [time.strftime("%H:%M:%S"),
             _("aktif") if st.get("active") else _("pasif"),
             _("mod: {mod}").format(mod=mode_str(st)),
             _("baskın profil: {profil}").format(profil=dom)]
    k = tx.kulaklik_of(st)
    if k is not None:
        parts.insert(2, _("çıkış: {tur}").format(tur=_(tx.kulaklik_tur(k))))
    if not st.get("sinks_ready"):
        parts.append(_("EQ çıkışları hazır değil"))
    streams = st.get("streams") or []
    if streams:
        parts.append(", ".join(f"{stream_app(s)}→{slabel(s)}" for s in streams))
    else:
        parts.append(_("akış yok"))
    return " | ".join(parts)


# --- commands -------------------------------------------------------------
def cmd_durum(a) -> int:
    st = call({"cmd": "status"})
    if a.json:
        st.pop("ok", None)
        _out(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        _out(format_status(st))
    return EXIT_OK


def _set_mode(mode: str, profile: str | None = None) -> int:
    req = {"cmd": "set_mode", "mode": mode}
    if profile:
        req["profile"] = profile
    call(req)
    _out(_("Mod: {mod}").format(mod=mode_str({"mode": mode, "fixed_profile": profile},
                                             explain_off=True)))
    return EXIT_OK


def cmd_oto(a) -> int:
    return _set_mode("auto")


def cmd_sabitle(a) -> int:
    return _set_mode("fixed", a.profil)


def cmd_kapat(a) -> int:
    return _set_mode("off")


def cmd_yenile(a) -> int:
    call({"cmd": "reload"})
    _out(_("Kurallar yeniden yüklendi."))
    return EXIT_OK


def cmd_izle(a) -> int:
    try:
        for ev in _ipc().watch():
            _out(format_watch_line(ev))
    except KeyboardInterrupt:
        return EXIT_OK
    except (ConnectionError, OSError) as e:
        raise DaemonDown(str(e)) from e
    _err(_("bağlantı kapandı"))
    return EXIT_DOWN


def systemctl(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], env=config.C_ENV,
                          capture_output=True, text=True, timeout=timeout)


def cmd_eq_uret(a) -> int:
    from . import eqgen
    try:
        path = eqgen.write_conf()
    except Exception as e:  # noqa: BLE001 - show any config error to the user
        _err(_("EQ ayarı üretilemedi: {hata}").format(hata=e))
        return EXIT_ERR
    _out(_("EQ ayarı yazıldı: {yol}").format(yol=path))
    if a.yeniden_baslatma:
        return EXIT_OK
    try:
        r = systemctl("restart", config.SINKS_SERVICE)
    except (OSError, subprocess.SubprocessError) as e:
        _err(_("systemctl çalıştırılamadı: {hata}").format(hata=e))
        return EXIT_ERR
    if r.returncode != 0:
        _err(_("{servis} yeniden başlatılamadı: {hata}").format(servis=config.SINKS_SERVICE,
                                                                 hata=(r.stderr or "").strip()))
        return EXIT_ERR
    _out(_("EQ çıkışları yeniden başlatıldı."))
    time.sleep(0.5)  # let the new sinks appear before rescanning
    try:
        call({"cmd": "rescan"})
    except DaemonDown:
        _err(_("uyarı: {mesaj}").format(mesaj=_(DOWN_MSG)))
        return EXIT_DOWN
    _out(_("Akışlar yeniden yönlendirildi."))
    return EXIT_OK


def _gui_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(config.SRC_DIR)
    return env


def _xdg_open(target: Path) -> int:
    try:
        subprocess.Popen(["xdg-open", str(target)], env=_gui_env(),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        _err(_("açılamadı: {hata}").format(hata=e))
        return EXIT_ERR
    return EXIT_OK


def cmd_eq_ciz(a) -> int:
    # C_ENV (LC_ALL=C) keeps tool output parseable, but it would also make the plot's
    # language English: hand the child this process's UI language explicitly.
    env = {**config.C_ENV, "PYTHONPATH": str(config.SRC_DIR), "OTO_EQ_DIL": i18n.dil()}
    try:
        r = subprocess.run([sys.executable, "-m", "oto_eq.eqgen", "plot"],
                           cwd=str(config.SRC_DIR), env=env,
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        _err(_("grafik çizilemedi: {hata}").format(hata=e))
        return EXIT_ERR
    if r.returncode != 0 or not config.PLOT_FILE.exists():
        _err(_("grafik çizilemedi: {hata}").format(
            hata=((r.stderr or "").strip().splitlines() or ["?"])[-1]))
        return EXIT_ERR
    _out(_("EQ eğrileri: {yol}").format(yol=config.PLOT_FILE))
    if a.acma:
        return EXIT_OK
    return _xdg_open(config.PLOT_FILE)


def ensure_user_config(name: str) -> Path:
    """Copy the shipped default to ~/.config/oto-eq if the user has none."""
    dst = config.CONFIG_DIR / name
    if not dst.exists():
        src = config.DEFAULTS_DIR / name
        if not src.exists():
            raise FileNotFoundError(_("varsayılan dosya yok: {yol}").format(yol=src))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        _out(_("Varsayılan kopyalandı: {yol}").format(yol=dst))
    return dst


def _open_config(name: str, hint: str) -> int:
    try:
        path = ensure_user_config(name)
    except OSError as e:
        _err(str(e))
        return EXIT_ERR
    _out(_("Düzenleyicide açılıyor: {yol}").format(yol=path))
    _out(_(hint))
    return _xdg_open(path)


def cmd_kurallar(a) -> int:
    return _open_config(config.RULES_FILE, N_("Kaydettikten sonra: kulak yenile"))


def cmd_profiller(a) -> int:
    return _open_config(config.PROFILES_FILE, N_("Kaydettikten sonra: kulak eq-uret"))


def cmd_gunluk(a) -> int:
    args = ["journalctl", "--user", "-u", "oto-eq", "-u", "oto-eq-sinks", "-u", "oto-eq-mic", "-f"]
    try:
        os.execvpe(args[0], args, config.C_ENV)
    except OSError as e:
        _err(_("journalctl çalıştırılamadı: {hata}").format(hata=e))
        return EXIT_ERR
    return EXIT_OK  # not reached


# --- microphone -----------------------------------------------------------
MIC_SOURCE = "oto_eq_mikrofon"
MIC_SERVICE = "oto-eq-mic.service"
MIC_LABEL = "Kulak Kesil · Mikrofon"     # PipeWire node name shown by KDE too: not translated


def list_sources() -> list[str]:
    """Source names (pactl list short sources), monitors included."""
    r = subprocess.run(["pactl", "list", "short", "sources"], env=config.C_ENV,
                       capture_output=True, text=True, timeout=5)
    return [ln.split("\t")[1] for ln in r.stdout.splitlines() if ln.count("\t") >= 1]


def default_source() -> str:
    r = subprocess.run(["pactl", "get-default-source"], env=config.C_ENV,
                       capture_output=True, text=True, timeout=5)
    return r.stdout.strip()


def pick_real_source(names: list[str]) -> str | None:
    """The HS80 microphone if present, else the first real (non-monitor) input."""
    real = [n for n in names if not n.endswith(".monitor") and not n.startswith("oto_eq_")]
    for n in real:
        if "hs80" in n.lower() and n.startswith("alsa_input"):
            return n
    for n in real:
        if n.startswith("alsa_input"):
            return n
    return real[0] if real else None


def set_default_source(name: str) -> bool:
    r = subprocess.run(["pactl", "set-default-source", name], env=config.C_ENV,
                       capture_output=True, text=True, timeout=5)
    return r.returncode == 0


def cmd_mikrofon(a) -> int:
    if a.islem == "kalibre":
        return cmd_mikrofon_kalibre(a)
    try:
        names = list_sources()
        current = default_source()
    except (OSError, subprocess.SubprocessError) as e:
        _err(_("pactl çalıştırılamadı: {hata}").format(hata=e))
        return EXIT_ERR
    present = MIC_SOURCE in names
    if a.islem == "test":
        if not present:
            _err(_("{mikrofon} çalışmıyor — systemctl --user start oto-eq-mic").format(
                mikrofon=MIC_LABEL))
            return EXIT_ERR
        args = [sys.executable, "-m", "oto_eq.mikrofon_test", str(a.saniye)]
        env = {**os.environ, "PYTHONPATH": str(config.SRC_DIR)}
        try:
            os.execvpe(args[0], args, env)
        except OSError as e:
            _err(_("test başlatılamadı: {hata}").format(hata=e))
            return EXIT_ERR
    if a.islem == "varsayilan":
        if not present:
            _err(_("{mikrofon} çalışmıyor — systemctl --user start oto-eq-mic").format(
                mikrofon=MIC_LABEL))
            return EXIT_ERR
        if not set_default_source(MIC_SOURCE):
            _err(_("varsayılan mikrofon değiştirilemedi"))
            return EXIT_ERR
        _out(_("Varsayılan mikrofon: {mikrofon}").format(mikrofon=MIC_LABEL))
        return EXIT_OK
    if a.islem == "hs80":
        real = pick_real_source(names)
        if not real:
            _err(_("gerçek bir mikrofon bulunamadı"))
            return EXIT_ERR
        if not set_default_source(real):
            _err(_("varsayılan mikrofon değiştirilemedi"))
            return EXIT_ERR
        _out(_("Varsayılan mikrofon: {mikrofon}").format(mikrofon=real))
        return EXIT_OK
    # durum
    try:
        svc = systemctl("is-active", MIC_SERVICE, timeout=5).stdout.strip() or "?"
    except (OSError, subprocess.SubprocessError):
        svc = "?"
    svc_text = {"active": _("çalışıyor"), "inactive": _("durdu"), "failed": _("hata")}.get(svc, svc)
    rows = [(_("Sanal mikrofon"), _("çalışıyor") if present
             else _("YOK — systemctl --user start oto-eq-mic")),
            (_("Servis"), svc_text),
            (_("Varsayılan"), MIC_LABEL if current == MIC_SOURCE else (current or "—"))]
    _out(_("Kulak Kesil — mikrofon"))
    for line in align_rows(rows):
        _out(line)
    if present and current != MIC_SOURCE:
        _out("\n" + _("İşlenmiş mikrofonu kullanmak için: kulak mikrofon varsayilan"))
    return EXIT_OK


# --- genre variants -----------------------------------------------------------
GROUP_ALIASES = {"oto": "auto", "otomatik": "auto", "genel": "", "notr": ""}


def _onoff(v: bool) -> str:
    return _("açık") if v else _("kapalı")


def cmd_tur(a) -> int:
    if a.profil == "dinle":
        return cmd_tur_dinle(a)
    if not a.profil:
        st = call({"cmd": "status"})
        variants = st.get("variants") or {}
        _out(_("Kulak Kesil — tür alt profilleri"))
        gs = config.load_genre_settings()
        rows = []
        for prof in VARIANT_PROFILES:
            v = variants.get(prof)
            if not v:
                rows.append((kind_name(prof), "—"))
                continue
            how = _("sabit") if v.get("mode") == "fixed" else _("otomatik")
            if v.get("source"):
                how = _("{secim}; kaynak: {kaynak}").format(secim=how,
                                                           kaynak=_(tx.source_text(v["source"])))
            shown = _(tx.variant_text({k: x for k, x in v.items() if k != "mode"}))  # no "· sabit"
            rows.append((kind_name(prof), f"{shown}  ({how})"))
        rows.append((_("Çevrimiçi"), _("{durum}   Ses analizi: {analiz}").format(
            durum=_onoff(gs.get("cevrimici", True)), analiz=_onoff(gs.get("ses_analizi", True)))))
        for line in align_rows(rows):
            _out(line)
        _out("")
        for prof in VARIANT_PROFILES:
            _out(f"  kulak tur {prof} <" + "|".join(("oto", "genel") + variant_groups(prof)) + ">")
        _out("  " + _("kulak tur dinle [saniye]   (Müzik çıkışını dinleyip ses analizini göster)"))
        return EXIT_OK
    groups = " | ".join(("oto", "genel") + tuple(variant_groups(a.profil)))
    if not a.grup:
        _err(_("grup gerekli: {gruplar}").format(gruplar=groups))
        return EXIT_USAGE
    g = GROUP_ALIASES.get(a.grup, a.grup)
    if g not in ("auto", "") and g not in variant_groups(a.profil):
        _err(_("bilinmeyen grup '{grup}': {gruplar}").format(grup=a.grup, gruplar=groups))
        return EXIT_USAGE
    call({"cmd": "set_variant", "profile": a.profil, "group": g})
    shown = _("Otomatik") if g == "auto" else group_name(g)
    _out(_("{profil} türü: {tur}").format(profil=prof_name(a.profil), tur=shown))
    return EXIT_OK


# --- `kulak ton`: kişisel ton -----------------------------------------------------------
TON_ALIASES = {"bas": "bas", "vokal": "vokal", "parlaklik": "parlaklik", "parlaklık": "parlaklik",
               "tiz": "parlaklik", "ses": "vokal"}
TON_SHORT = {"bas": N_("bas"), "vokal": N_("vokal"), "parlaklik": N_("parlaklık")}  # translate at use


def _kisisel():
    from . import kisisel   # lazy like the other optional modules
    return kisisel


def _eqgen():
    from . import eqgen
    return eqgen


def _ton_fmt(vals: dict) -> str:
    def one(v):
        return "0" if not v else f"{v:+g}"
    return _("bas {bas}  vokal {vokal}  parlaklık {parlaklik}").format(
        **{k: one(vals.get(k, 0.0)) for k in TON_SHORT})


def parse_ton_args(items: list[str], limit: float, step: float) -> tuple[dict, list[str]]:
    """['bas=+1.5', 'vokal=0'] → ({'bas': 1.5, 'vokal': 0.0}, notes). Raises ValueError (UI text)."""
    vals, notes = {}, []
    for it in items:
        key, sep, raw = it.partition("=")
        k = TON_ALIASES.get(key.strip().lower())
        if not sep or k is None:
            raise ValueError(_("'{ayar}' anlaşılmadı: bas=±X  vokal=±X  parlaklik=±X biçiminde yazın")
                             .format(ayar=it))
        try:
            v = float(raw.strip().replace(",", "."))
        except ValueError:
            raise ValueError(_("'{ayar}': sayı bekleniyordu (ör. {anahtar}=+1.5)")
                             .format(ayar=it, anahtar=key)) from None
        if v != v or abs(v) > limit + 1e-9:
            raise ValueError(_("'{ayar}': değer ±{sinir:g} dB içinde olmalı").format(ayar=it, sinir=limit))
        r = round(round(v / step) * step, 2) + 0.0
        if abs(r - v) > 1e-9:
            notes.append(_("{bant} {deger:+g} → {yeni:+g} dB ({adim:g} dB adımına yuvarlandı)").format(
                bant=_(TON_SHORT[k]), deger=v, yeni=r, adim=step))
        vals[k] = r
    return vals, notes


def _ton_context() -> tuple[dict | None, dict | None]:
    """(profiles or None, router status or None) — both optional for `kulak ton`."""
    try:
        profiles = _eqgen().load_profiles()
    except Exception:
        profiles = None
    try:
        st = call({"cmd": "status"})
    except (DaemonDown, DaemonError):
        st = None
    return profiles, st


def _tone_on(profiles: dict | None, prof: str) -> bool:
    if profiles is None:
        return prof != "cs2"
    return _eqgen().tone_enabled(profiles, prof)


def _current_group(st: dict | None, prof: str) -> str | None:
    """Active genre of a sink ("" = Genel); None when unknown (router unreachable)."""
    if prof not in VARIANT_PROFILES:
        return ""
    if st is None:
        return None
    v = tx.variant_of(st, prof)
    return (v or {}).get("group") or ""


def _apply_tone_live(targets: list[str], profiles: dict | None, st: dict | None) -> None:
    """apply_tone (file values) on the affected sinks; prints one summary."""
    import logging
    lg = logging.getLogger("oto_eq")
    if not lg.handlers:            # library warnings → our own message, not raw stderr
        lg.addHandler(logging.NullHandler())
    eq = _eqgen()
    done, failed, skipped = [], [], []
    for prof in targets:
        grp = _current_group(st, prof)
        if grp is None:
            skipped.append(prof_name(prof))
            continue
        try:
            ok = eq.apply_tone(prof, grp, ramp_s=0.3, profiles=profiles)
        except Exception:
            ok = False
        (done if ok else failed).append(prof_name(prof))
    if done:
        eq.wait_ramps(3.0)        # the ramp threads die with the process otherwise
        _out(_("Canlı uygulandı: {profiller}").format(profiller=", ".join(done)))
    if failed:
        _out(_("Canlı uygulanamadı: {profiller} — EQ çıkışları çalışmıyor ya da yapılandırma eski "
               "(kulak eq-uret). Kaydedildi; çıkışlar açılınca geçerli olur.")
             .format(profiller=", ".join(failed)))
    if skipped:
        _out(_("Yönlendirici yanıt vermiyor, şu an çalan tür bilinmiyor: {profiller} atlandı "
               "(kaydedildi, sonraki tür geçişinde uygulanır).").format(profiller=", ".join(skipped)))


def _parse_tur_opt(value: str) -> tuple[str, str]:
    prof, _sep, grp = value.partition(":")
    _kisisel().tur_key(prof, grp)      # ValueError with a (Turkish) message
    return prof, grp


def _tur_name(prof: str, grp: str) -> str:
    """'Müzik · Elektronik'."""
    return f"{prof_name(prof)} · {group_name(grp)}"


def format_ton(data: dict, profiles: dict | None, st: dict | None) -> str:
    ks = _kisisel()
    bands = " · ".join(f"{_(TON_SHORT[k])} {ks.TON_BANDS[k][1]:g} Hz" for k in ks.TON_KEYS)
    lines = [_("Kulak Kesil — kişisel ton  (±{sinir:g} dB; {bantlar})").format(
        sinir=ks.TON_LIMIT, bantlar=bands)]
    rows = [(_("Genel"), _ton_fmt(data["genel"]))]
    tur = data.get("tur") or {}
    if tur:
        for i, (key, vals) in enumerate(sorted(tur.items())):
            prof, _sep, grp = key.partition(":")
            rows.append((_("Tür farkları") if i == 0 else "",
                         f"{_tur_name(prof, grp)}: {_ton_fmt(vals)}"))
    else:
        rows.append((_("Tür farkları"), _("yok")))
    lines += align_rows(rows)
    lines.append("")
    lines.append(_("Şu an etkin:") if st is not None else
                 _("Şu an etkin (yönlendirici yanıt vermiyor: türler Genel varsayıldı):"))
    rows = []                            # (profile, " · genre" / "" / None = no column, value)
    for prof in EQ_PROFILES:
        if not _tone_on(profiles, prof):
            rows.append((prof_name(prof), None, _("kapalı (profiller.toml kisisel_ton = false)")))
            continue
        grp = _current_group(st, prof) or ""
        where = f" · {group_name(grp)}" if prof in VARIANT_PROFILES and grp else ""
        rows.append((prof_name(prof), where, _ton_fmt(ks.effective(data, prof, grp))))
    # name ≥ 6, genre ≥ 20 cells (opted-out rows have no genre column),
    # but one width for ALL rows: a long genre name now widens every row so the colons
    # stay aligned (before, only that row moved); terminal cell width (CJK = 2)
    wn = max(6, *(text_width(n) for n, _w, _v in rows))
    ww = max(20, *(text_width(w or "") for _n, w, _v in rows))
    lines += [f"  {pad(n, wn)}{'' if w is None else pad(w, ww)} : {v}"
              for n, w, v in rows]
    lines += ["",
              "  kulak ton bas=+1.5 vokal=0 parlaklik=-1 [--tur muzik:elektronik]",
              "  kulak ton sifirla [--tur muzik:elektronik | --hepsi]"]
    return "\n".join(lines)


def cmd_ton(a) -> int:
    ks = _kisisel()
    items = list(a.ayarlar or [])
    reset = bool(items) and items[0] in ("sifirla", "sıfırla")
    if reset:
        items = items[1:]
        if items:
            _err(_("'sifirla' ek değer almaz (yalnız --tur ya da --hepsi)"))
            return EXIT_USAGE
    try:
        tur = _parse_tur_opt(a.tur) if a.tur else None
    except ValueError as e:
        _err(_("--tur: {hata}").format(hata=e))
        return EXIT_USAGE
    if a.hepsi and (not reset or tur):
        _err(_("--hepsi yalnız 'kulak ton sifirla' ile (ve --tur olmadan) kullanılır"))
        return EXIT_USAGE
    if not reset and not items:
        if tur:
            _err(_("--tur ile en az bir değer verin (ör. bas=+1) ya da 'sifirla'"))
            return EXIT_USAGE
        profiles, st = _ton_context()
        _out(format_ton(ks.load(), profiles, st))
        return EXIT_OK
    vals = {}
    if not reset:
        limit = ks.TUR_LIMIT if tur else ks.TON_LIMIT
        try:
            vals, notes = parse_ton_args(items, limit, ks.TON_STEP)
        except ValueError as e:
            _err(str(e))
            return EXIT_USAGE
        for n in notes:
            _out(_("Not: {not_}").format(not_=n))
    try:
        if reset and a.hepsi:
            data = ks.reset_all()
            what = _("Tüm kişisel ton ayarları sıfırlandı (genel + tür farkları).")
        elif reset and tur:
            data = ks.reset_tur(*tur)
            what = _("{tur} tür farkı sıfırlandı.").format(tur=_tur_name(*tur))
        elif reset:
            data = ks.reset_genel()
            what = _("Genel kişisel ton sıfırlandı (tür farkları duruyor; hepsi için --hepsi).")
        elif tur:
            data = ks.set_tur(*tur, **vals)
            what = _("{tur} tür farkı: {ton}").format(
                tur=_tur_name(*tur), ton=_ton_fmt(data["tur"].get(":".join(tur), ks.zero())))
        else:
            data = ks.set_genel(**vals)
            what = _("Genel kişisel ton: {ton}").format(ton=_ton_fmt(data["genel"]))
    except (OSError, ValueError) as e:
        _err(_("kaydedilemedi: {hata}").format(hata=e))
        return EXIT_ERR
    _out(what)
    profiles, st = _ton_context()
    if tur:
        eff = ks.effective(data, *tur)
        _out(_("Etkin ({tur}): {ton}").format(tur=_tur_name(*tur), ton=_ton_fmt(eff)))
        raw = {k: data["genel"][k] + data["tur"].get(":".join(tur), ks.zero())[k]
               for k in ks.TON_KEYS}
        if any(abs(raw[k]) > ks.TON_LIMIT + 1e-9 for k in ks.TON_KEYS):
            _out(_("Not: genel + tür farkı ±{sinir:g} dB'i aşıyor; etkin değer sınırlandı.")
                 .format(sinir=ks.TON_LIMIT))
        targets = [tur[0]] if _current_group(st, tur[0]) == tur[1] else []
        if st is None:
            _out(_("Yönlendirici yanıt vermiyor: kaydedildi, tür başlayınca uygulanır."))
        elif not targets:
            _out(_("Bu tür şu an çalmıyor: kaydedildi, tür başlayınca uygulanır."))
    else:
        targets = list(EQ_PROFILES)
    off = [p for p in targets if not _tone_on(profiles, p)]
    targets = [p for p in targets if p not in off]
    if off and not tur:
        _out(_("Not: {profiller} kişisel tonu kullanmıyor (profiller.toml kisisel_ton = false).")
             .format(profiller=", ".join(prof_name(p) for p in off)))
    if targets:
        _apply_tone_live(targets, profiles, st)
    return EXIT_OK


# --- `kulak tur dinle`: one-shot audio analysis of the Müzik sink -------------------
LISTEN_SOURCE = "oto_eq_muzik.monitor"
LISTEN_RATE = 22050
LISTEN_DEFAULT_S = 8.0
LISTEN_MIN_S, LISTEN_MAX_S = 2.0, 60.0
SILENCE_RMS = 1e-4          # ≈ -80 dBFS: nothing is playing on Müzik


def _wait(seconds: float) -> None:
    time.sleep(seconds)


def _import_ses_analiz():
    from . import ses_analiz  # lazy: B2 module, optional at runtime
    return ses_analiz


def _import_ses_model():
    from . import ses_model  # lazy: B3 module (+ onnxruntime), optional
    return ses_model


def _pct(p) -> str:
    try:
        return tx.percent_text(float(p) * 100, 0)          # "%54" / "54%" / "54 %"
    except (TypeError, ValueError):
        return "?"


def _top_groups(scores, n: int = 3) -> str:
    if not isinstance(scores, dict) or not scores:
        return ""
    items = []
    for g, p in scores.items():
        try:
            items.append((float(p), str(g)))
        except (TypeError, ValueError):
            continue
    items.sort(reverse=True)
    return ", ".join(f"{group_name(g)} {_pct(p)}" for p, g in items[:n])


def _fmt_value(v) -> str:
    if isinstance(v, bool):
        return _("evet") if v else _("hayır")
    if isinstance(v, (int, float)):
        return f"{v:.3g}" if isinstance(v, float) else str(v)
    return str(v)


def format_listen_report(seconds: float, feats: dict | None, dsp, model_state: str,
                         model_probs, combined=None) -> str:
    """Report for `kulak tur dinle` (pure; tests feed fakes)."""
    lines = [_("Kulak Kesil — ses analizi ({sn:g} sn, Müzik çıkışı)").format(sn=seconds), ""]
    if feats:
        lines.append(_("Özellikler:"))
        lines += align_rows([(str(k), _fmt_value(v)) for k, v in feats.items()])
        lines.append("")
    rows = []                            # (label, value, extra line or "")
    if dsp is None:
        rows.append((_("Sinyal işleme tahmini"), _("belirsiz → Genel"), ""))
    else:
        g = getattr(dsp, "group", "") or ""
        val = f"{group_name(g)} ({_pct(getattr(dsp, 'confidence', 0))})"
        if getattr(dsp, "reason", ""):
            val += f" — {_(dsp.reason)}"
        top = _top_groups(getattr(dsp, "scores", None))
        rows.append((_("Sinyal işleme tahmini"), val,
                     "  " + _("puanlar: {puanlar}").format(puanlar=top) if top else ""))
    if model_state:
        rows.append((_("Model tahmini"), model_state, ""))
    else:
        rows.append((_("Model tahmini"), _top_groups(model_probs) or _("belirsiz"), ""))
    if combined is not None:
        extra = []
        if getattr(combined, "confidence", None) is not None:
            extra.append(_pct(combined.confidence))
        src = _(tx.source_text(getattr(combined, "source", "")))
        if src:
            extra.append(_("kaynak: {kaynak}").format(kaynak=src))
        val = group_name(getattr(combined, "group", ""))
        rows.append((_("Birleşik sonuç"), val + (f" ({'; '.join(extra)})" if extra else ""), ""))
    w = max(text_width(k) for k, _v, _x in rows)
    for k, v, x in rows:
        lines.append(f"{pad(k, w)} : {v}")
        if x:
            lines.append(x)
    return "\n".join(lines)


def _load_model_state():
    """(model or None, reason text when unavailable)."""
    try:
        sm = _import_ses_model()
    except ImportError:
        return None, _("yok (oto_eq.ses_model modülü henüz kurulu değil)")
    try:
        model = sm.load_model()
    except Exception as e:  # noqa: BLE001 - optional component
        return None, _("yüklenemedi ({hata})").format(hata=e)
    if model is None:
        return None, _("yok (onnxruntime ya da model dosyası eksik; ~/.local/share/oto-eq/models/)")
    return model, ""


def _combined_guess(sa, tap, model, t0: float, seconds: float):
    """What the router would conclude (DSP + model) from the same in-memory window."""
    if not hasattr(sa, "AudioAnalyzer"):
        return None
    try:
        return sa.AudioAnalyzer(tap=tap, model=model).guess(t0, min_s=min(3.0, seconds))
    except Exception:  # noqa: BLE001 - optional extra line
        return None


def cmd_tur_dinle(a) -> int:
    raw = a.grup
    try:
        seconds = LISTEN_DEFAULT_S if raw in (None, "") else float(str(raw).replace(",", "."))
    except ValueError:
        _err(_("süre sayı olmalı: kulak tur dinle [saniye] (varsayılan {sn:g})").format(
            sn=LISTEN_DEFAULT_S))
        return EXIT_USAGE
    if not LISTEN_MIN_S <= seconds <= LISTEN_MAX_S:
        _err(_("süre {en_az:g}–{en_cok:g} saniye arasında olmalı").format(en_az=LISTEN_MIN_S,
                                                                         en_cok=LISTEN_MAX_S))
        return EXIT_USAGE
    try:
        sa = _import_ses_analiz()
    except ImportError:
        _err(_("ses analizi modülü (oto_eq.ses_analiz) henüz kurulu değil"))
        return EXIT_ERR
    try:
        import numpy as np
    except ImportError:
        _err(_("numpy kurulu değil"))
        return EXIT_ERR
    if not config.load_genre_settings().get("ses_analizi", True):
        _out(_("Not: Ses analizi Ayarlar'da kapalı; yönlendirici kullanmıyor (bu deneme yine de "
               "çalışır)."))

    _out("🎧 " + _("{sn:g} saniye boyunca Müzik çıkışı dinleniyor… (ses yalnızca bellekte tutulur, "
                  "kaydedilmez)").format(sn=seconds))
    x = tap = model = combined = None
    model_state = ""
    try:
        tap = sa.AudioTap(source=LISTEN_SOURCE, rate=LISTEN_RATE, seconds=seconds + 5.0)
        t0 = time.monotonic()
        tap.start()
        _wait(seconds)
        x = tap.window(t0, time.monotonic())
        model, model_state = _load_model_state()
        combined = _combined_guess(sa, tap, model, t0, seconds)
    except KeyboardInterrupt:
        _out(_("İptal edildi."))
        return 130
    except Exception as e:  # noqa: BLE001 - capture tool missing / sink gone
        _err(_("ses yakalanamadı: {hata}").format(hata=e))
        return EXIT_ERR
    finally:
        if tap is not None:
            try:
                tap.stop()
            except Exception:  # noqa: BLE001
                pass
    x = np.asarray(x if x is not None else [], dtype=np.float32).reshape(-1)
    if x.size < LISTEN_RATE * 1.0:
        _err(_("ses yakalanamadı — Müzik çıkışı (oto_eq_muzik) var mı? Kontrol: kulak saglik"))
        return EXIT_ERR
    rms = float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))
    if rms < SILENCE_RMS:
        _err(_("Müzik çıkışında ses yok (sessizlik). Müzik profiline giden bir şarkı çalarken "
               "tekrar dene; neyin nereye gittiğini görmek için: kulak durum"))
        return EXIT_ERR

    try:
        feats = sa.features(x, LISTEN_RATE)
    except Exception as e:  # noqa: BLE001
        _err(_("özellikler hesaplanamadı: {hata}").format(hata=e))
        feats = None
    try:
        dsp = sa.classify(x, LISTEN_RATE)
    except Exception as e:  # noqa: BLE001
        _err(_("sinyal işleme tahmini yapılamadı: {hata}").format(hata=e))
        dsp = None
    probs = None
    if model is not None:
        try:
            probs = model.predict(x, LISTEN_RATE)
        except Exception as e:  # noqa: BLE001
            model_state = _("hata ({hata})").format(hata=e)
    del x
    _out(format_listen_report(seconds, feats if isinstance(feats, dict) else None,
                              dsp, model_state, probs, combined))
    return EXIT_OK


# --- health check / calibration ----------------------------------------------
HEALTH_ICONS = {"tamam": "✔", "uyari": "⚠", "hata": "✘"}
CALIB_LABELS = {"kompresor.esik": (N_("Kompresör eşiği"), "dB"),        # translate at use
                "kompresor.telafi": (N_("Kompresör telafisi"), "dB"),
                "gurultu.esik": (N_("Bastırma eşiği"), "%")}


def cmd_saglik(a) -> int:
    try:
        from . import saglik
    except ImportError:
        _err(_("sağlık kontrolü modülü (oto_eq.saglik) kurulu değil"))
        return EXIT_ERR
    results = saglik.run_checks()
    if a.json:
        _out(json.dumps(results, ensure_ascii=False, indent=1))
    elif hasattr(saglik, "format_report"):
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        _out(saglik.format_report(results, color=color))
    else:
        for r in results:
            _out(f"{HEALTH_ICONS.get(r.get('durum'), '?')} {r.get('ad', '')}: {r.get('ayrinti', '')}")
            if r.get("durum") != "tamam" and r.get("oneri"):
                _out(f"    → {r['oneri']}")
    return EXIT_ERR if any(r.get("durum") == "hata" for r in results) else EXIT_OK


def format_calibration(res: dict, current: dict | None = None) -> str:
    """Table for micgen.calibrate()'s result."""
    def db(v):
        return f"{v:.1f} dBFS" if isinstance(v, (int, float)) else "—"
    lines = [_("Ölçülen:")]
    lines += align_rows([(_("Konuşma"), db(res.get("konusma_dbfs"))),
                         (_("Gürültü"), db(res.get("gurultu_dbfs"))),
                         (_("Tepe"), db(res.get("tepe_dbfs")))])
    lines += ["", _("Önerilen ayarlar:")]
    rows = []
    for key, val in (res.get("oneri") or {}).items():
        name, unit = CALIB_LABELS.get(key, (key, ""))
        cur = None
        if current:
            sec, _sep, k = key.partition(".")
            cur = (current.get(sec) or {}).get(k)
        cur_s = f"{cur:g} {unit}".strip() if isinstance(cur, (int, float)) else "—"
        rows.append((_(name), cur_s, f"{val:g} {unit}".rstrip()))
    if rows:
        wn = max(19, *(text_width(n) for n, _c, _v in rows))
        for name, cur_s, new in rows:
            lines.append(f"  {pad(name, wn)} {cur_s:>9} → {new}")
    if res.get("aciklama"):
        lines += ["", _(str(res["aciklama"]))]
    return "\n".join(lines)


def cmd_mikrofon_kalibre(a) -> int:
    try:
        from . import micgen
    except ImportError:
        _err(_("mikrofon modülü (oto_eq.micgen) kurulu değil"))
        return EXIT_ERR
    if not hasattr(micgen, "calibrate"):
        _err(_("bu sürümde otomatik ayar yok (micgen.calibrate)"))
        return EXIT_ERR
    try:
        current = micgen.load_mic()
    except Exception:  # noqa: BLE001
        current = None
    _out("🎙  " + _("{sn:g} saniye boyunca normal sesinle konuş…").format(sn=a.saniye))
    try:
        res = micgen.calibrate(seconds=a.saniye, apply=a.uygula)
    except Exception as e:  # noqa: BLE001
        _err(_("otomatik ayar yapılamadı: {hata}").format(hata=e))
        return EXIT_ERR
    if not isinstance(res, dict) or res.get("hata"):
        _err(_("otomatik ayar yapılamadı: {hata}").format(
            hata=res.get("hata") if isinstance(res, dict) else res))
        return EXIT_ERR
    _out(format_calibration(res, current))
    if res.get("uygulandi"):
        _out("\n" + _("Önerilen ayarlar uygulandı ✓"))
    elif not a.uygula:
        _out("\n" + _("Uygulamak için: kulak mikrofon kalibre --uygula"))
    return EXIT_OK


# --- parser ---------------------------------------------------------------
def _h(text: str) -> str:
    """argparse %-formats help strings: a translation must not break `--help`."""
    return text.replace("%", "%%")


class _Formatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups,
                                 _("kullanım:") + " " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """ArgumentParser with translated headings / help (Turkish argparse errors in Turkish)."""

    def __init__(self, *args, **kw):
        kw.setdefault("formatter_class", _Formatter)
        kw["add_help"] = False
        super().__init__(*args, **kw)
        self._positionals.title = _("argümanlar")
        self._optionals.title = _("seçenekler")
        self.add_argument("-h", "--help", action="help", help=_h(_("bu yardımı göster ve çık")))

    # argparse's own (English) messages: Turkish only for the Turkish UI, else left as is
    _TR = (("the following arguments are required", "şu argümanlar gerekli"),
           ("unrecognized arguments", "tanınmayan argümanlar"),
           ("invalid choice", "geçersiz seçim"), ("choose from", "seçenekler"),
           ("argument ", "argüman "))

    def error(self, message):
        if i18n.dil() == i18n.KAYNAK:
            for en, tr in self._TR:
                message = message.replace(en, tr)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, _("{prog}: hata: {mesaj}").format(prog=self.prog, mesaj=message) + "\n")


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog=PROG,
                description=_("Kulak Kesil — kulaklık için bağlama göre otomatik ekolayzır."),
                epilog=_("Çıkış kodları: 0 başarı, 1 hata, 2 kullanım hatası, 3 servis çalışmıyor."))
    p._positionals.title = _("komutlar")
    sub = p.add_subparsers(dest="komut", metavar=_("KOMUT"), parser_class=_Parser)

    def add(name, func, help_):
        sp = sub.add_parser(name, help=_h(help_), description=help_)
        sp.set_defaults(func=func)
        return sp

    profil, json_help = _("PROFIL"), _h(_("ham JSON çıktısı"))
    d = add("durum", cmd_durum, _("durumu ve akışları göster"))
    d.add_argument("--json", action="store_true", help=json_help)
    add("oto", cmd_oto, _("otomatik moda geç (bağlama göre EQ)"))
    s = add("sabitle", cmd_sabitle, _("tek profili tüm akışlara sabitle"))
    s.add_argument("profil", choices=EQ_PROFILES, metavar=profil, help=" | ".join(EQ_PROFILES))
    add("kapat", cmd_kapat, _("EQ'yu kapat (seviye eşitli düz geçiş)"))
    add("yenile", cmd_yenile, _("kurallar.toml dosyasını yeniden yükle"))
    add("izle", cmd_izle, _("durumu canlı izle (Ctrl+C ile çık)"))
    e = add("eq-uret", cmd_eq_uret, _("profiller.toml'dan EQ'yu üret ve EQ çıkışlarını yeniden başlat"))
    e.add_argument("--yeniden-baslatma", action="store_true",
                   help=_h(_("sadece dosyayı yaz, servisi yeniden başlatma")))
    c = add("eq-ciz", cmd_eq_ciz, _("EQ eğrilerinin grafiğini çiz ve aç"))
    c.add_argument("--acma", action="store_true", help=_h(_("sadece çiz, açma")))
    add("kurallar", cmd_kurallar, _("kurallar.toml dosyasını düzenleyicide aç"))
    add("profiller", cmd_profiller, _("profiller.toml dosyasını düzenleyicide aç"))
    add("gunluk", cmd_gunluk, _("servis günlüğünü canlı göster (journalctl)"))
    m = add("mikrofon", cmd_mikrofon,
            _("işlenmiş sanal mikrofon: durum / test / varsayılan yap / HS80'e dön"))
    m.add_argument("islem", nargs="?", default="durum",
                   choices=("durum", "test", "kalibre", "varsayilan", "hs80"),
                   metavar=_("İŞLEM"), help=_h(_("durum (varsayılan) | test | kalibre | varsayilan | hs80")))
    m.add_argument("saniye", nargs="?", type=float, default=5.0,
                   help=_h(_("test / ölçüm süresi (saniye, varsayılan 5)")))
    m.add_argument("--uygula", action="store_true",
                   help=_h(_("kalibre: önerilen ayarları hemen uygula")))
    t = add("tur", cmd_tur, _("tür alt profilleri: göster ya da seç (müzik türü / oyun türü); "
                              "'kulak tur dinle [saniye]' Müzik çıkışını dinleyip ses analizini "
                              "gösterir"))
    t.add_argument("profil", nargs="?", choices=VARIANT_PROFILES + ("dinle",), metavar=profil,
                   help=" | ".join(VARIANT_PROFILES + ("dinle",)))
    t.add_argument("grup", nargs="?", metavar=_("GRUP"),
                   help=_h(_("{gruplar}; dinle için saniye (varsayılan {sn:g})").format(
                       gruplar=" | ".join(("oto", "genel") + variant_groups("muzik")
                                          + variant_groups("oyun")),
                       sn=LISTEN_DEFAULT_S)))
    k = add("ton", cmd_ton, _("kişisel ton (bas / vokal / parlaklık, ±3 dB): göster, ayarla, "
                              "sıfırla; canlı uygulanır"))
    k.add_argument("ayarlar", nargs="*", metavar=_("AYAR"),
                   help=_h(_("bas=±X vokal=±X parlaklik=±X (0.5 dB adım) ya da 'sifirla'")))
    k.add_argument("--tur", metavar=_("PROFIL:TÜR"),
                   help=_h(_("genel yerine bir türün farkı, ör. muzik:elektronik (±6 dB fark; "
                             "etkin değer yine ±3 dB)")))
    k.add_argument("--hepsi", action="store_true",
                   help=_h(_("sifirla: genel + tüm tür farklarını sıfırla")))
    b = add("ab", cmd_ab, _("kör A/B dinleme: çalan şarkıda iki ton arasında seçim yaparak kişisel "
                            "tonu bulur (--deneme N, --sure S, --profil muzik); bitince eski ton "
                            "geri gelir"))
    b.add_argument("secenekler", nargs=argparse.REMAINDER, help="kulak ab --help")
    for name, text in (("kisma", _("sesli sohbette kısma: Discord vb. konuşunca oyun/müzik/film sesi "
                                   "iner (durum | ac | kapat | db=6)")),
                       ("model", _("kişisel tür modeli: düzeltmelerinden öğrenir "
                                   "(durum | egit | sifirla)")),
                       ("yedek", _("ayarların yedeği (al [dosya] | yukle dosya)"))):
        x = add(name, _cmd_passthru(name), text)
        x.add_argument("secenekler", nargs=argparse.REMAINDER, help=f"kulak {name} --help")
    h = add("saglik", cmd_saglik, _("sağlık kontrolü (servisler, çıkışlar, eklentiler; salt okunur)"))
    h.add_argument("--json", action="store_true", help=json_help)
    return p


# Subcommands whose module parses its own arguments (``run(argv) -> int``):
# ab (kör A/B), kisma (sesli sohbette kısma), model (kişisel model), yedek.
PASSTHRU = {"ab": "ab", "kisma": "kisma", "model": "kisisel_model", "yedek": "yedek"}


def _passthru(name: str, rest: list[str]) -> int:
    import importlib
    try:
        mod = importlib.import_module(f"{__package__ or 'oto_eq'}.{PASSTHRU[name]}")
    except ImportError:
        _err(_("bu sürümde 'kulak {komut}' yok (oto_eq.{modul} kurulu değil)").format(
            komut=name, modul=PASSTHRU[name]))
        return EXIT_ERR
    return mod.run(list(rest))


def cmd_ab(a) -> int:
    return _passthru("ab", getattr(a, "secenekler", []) or [])


def _cmd_passthru(name: str):
    return lambda a: _passthru(name, getattr(a, "secenekler", []) or [])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in PASSTHRU:     # options go straight to the module's own parser
        try:
            return _passthru(args[0], args[1:])
        except KeyboardInterrupt:
            return 130
    parser = build_parser()
    a = parser.parse_args(args)
    if not getattr(a, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return a.func(a)
    except DaemonDown:
        _err(_(DOWN_MSG))
        return EXIT_DOWN
    except DaemonError as e:
        _err(_("servis hatası: {hata}").format(hata=e))
        return EXIT_ERR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
