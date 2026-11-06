"""Kendi kendini onarma ("onarım"): periodic self-checks for the router (CHANGE 6, D4).

Three layers, each separately testable:

* ``gozle(...) -> Gozlem``: cheap READ-ONLY observations (pactl sinks/default,
  ``systemctl --user show oto-eq-sinks``, ``pw-top -b -n 2`` xrun counters,
  source-output count, WirePlumber stream-properties text). ~1 s (pw-top).
* ``kontrol(status, now, gozlem, durum, ayar) -> list[Eylem]``: PURE decision
  logic. ``status`` is the router status dict (``Router.status()`` / IPC
  ``status``), ``now`` a monotonic clock, ``durum`` the mutable rate-limit /
  debounce state (``Durum``), ``ayar`` the settings (``Ayar``). No I/O.
* ``uygula(eylem, ..., dry_run=False) -> str``: executes ONE action
  (systemctl restart, notify-send, router rescan, WirePlumber cleanup) with
  injectable runner/notifier. ``dry_run=True`` only describes what it would do.

``Onarici`` glues them together and can run them in its own daemon thread
(``baslat(aralik=60)``); the coordinator starts it next to the router:

    o = onarim.Onarici(status_fn=router.status, rescan=router.request_rescan,
                       kulaklik_desen=router.headset_re.pattern)
    o.baslat()            # ... o.durdur() on shutdown

Checks (see docs/NOTES-onarim.md):
1. EQ sinks missing while the router is active (headset is the default) for
   ``EKSIK_BEKLE`` s → ``systemctl --user restart oto-eq-sinks`` at most once per
   ``RESTART_ARALIK`` (10 min) and at most ``RESTART_UST`` times in a row; inside the
   window / after the cap → Turkish notification only (itself rate-limited).
2. xrun burst: ERR counter deltas (pw-top) ≥ ``XRUN_ESIK`` between two checks →
   notification (rate-limited ``XRUN_BILDIRIM_ARALIK``).
3. HS80 follow sanity: HS80 present but the default sink is not an HS80 for
   ``HS80_BEKLE`` s → router rescan nudge (existing HS80 takibi logic decides) +
   log; default sink = one of OUR sinks → notification (feedback loop risk);
   router status default ≠ real default for a while → rescan.
4. WirePlumber test leftovers (tools/wp_temizle.py patterns) → cleanup through
   ``wp_temizle.apply`` (backs up, stops/starts WP, verifies defaults) only when
   NOTHING is playing or recording, at most once per ``WP_ARALIK``.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import config
from .i18n import N_, Metin, _, metin_kaydi
from .models import ALL_SINK_PROFILES, SINK_PREFIX, sink_name

log = logging.getLogger("oto_eq.onarim")

# ----------------------------------------------------------------- settings
EKSIK_BEKLE = 20.0            # s: sinks must be missing this long before acting
RESTART_ARALIK = 600.0        # s: at most one automatic restart per 10 min
RESTART_UST = 3               # consecutive restarts without recovery → notify only
EKSIK_BILDIRIM_ARALIK = 600.0
XRUN_ESIK = 8                 # new xruns between two checks that count as a burst
XRUN_MAX_ARALIK = 300.0       # s: older baselines are not compared (no false bursts)
XRUN_BILDIRIM_ARALIK = 1800.0
HS80_BEKLE = 45.0             # > router HS80_WINDOW (30 s): let the router act first
HS80_TARA_ARALIK = 600.0
VARSAYILAN_EQ_BILDIRIM_ARALIK = 1800.0
ESKI_DURUM_BEKLE = 30.0       # router status default ≠ real default this long → rescan
ESKI_DURUM_ARALIK = 300.0
WP_ARALIK = 86400.0           # WirePlumber cleanup at most once a day
SINKS_UNIT = config.SINKS_SERVICE
SINKS_CONF_AD = config.SINKS_CONF.name
GECMIS_DOSYA = config.STATE_DIR / "onarim.json"
GECMIS_UZUNLUK = 20
APP_NAME = "Kulak Kesil"
NOTIFY_ENV = {**config.C_ENV, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}

# action kinds
YENIDEN_BASLAT = "servis_yeniden_baslat"
BILDIRIM = "bildirim"
TARA = "tara"
WP_TEMIZLE = "wp_temizle"
KAYIT = "kayit"


@dataclass
class Ayar:
    """Coordinator-tunable switches (e.g. from kurallar.toml [genel])."""
    otomatik_yeniden_baslat: bool = True
    wp_otomatik_temizle: bool = True
    bildirim: bool = True
    kulaklik_desen: str = "(?i)hs80"


@dataclass
class Gozlem:
    """One round of read-only observations. ``None`` = could not be observed."""
    sinks: list[str] | None = None            # pactl list short sinks (names)
    default_sink: str | None = None           # pactl get-default-sink
    sinks_unit: dict[str, str] | None = None  # systemctl show oto-eq-sinks (LoadState, ActiveState,
                                              # SubState, Result)
    xrun: dict[str, int] | None = None        # "id:name" -> cumulative ERR (pw-top)
    kayit_sayisi: int | None = None           # client-owned source-outputs (apps recording)
    wp_kalinti: int | None = None             # removable WP test entries (wp_temizle plan)
    conf_var: bool | None = None              # the generated sinks conf exists (unit condition)


@dataclass
class Eylem:
    tur: str                       # one of the kinds above
    neden: str                     # Turkish, for the log / status
    anahtar: str = ""              # rate-limit key
    hedef: str = ""                # unit name for YENIDEN_BASLAT
    baslik: str = ""               # notification summary (BILDIRIM / after an action)
    govde: str = ""                # notification body


@dataclass
class Durum:
    """Mutable state between ``kontrol`` calls (debounce + rate limits)."""
    son: dict[str, float] = field(default_factory=dict)      # anahtar -> last time
    since: dict[str, float] = field(default_factory=dict)    # condition -> first seen
    ardisik_restart: int = 0
    xrun_onceki: dict[str, int] | None = None
    xrun_zaman: float | None = None

    def izin(self, anahtar: str, now: float, aralik: float) -> bool:
        t = self.son.get(anahtar)
        return t is None or now - t >= aralik

    def isaretle(self, anahtar: str, now: float) -> None:
        self.son[anahtar] = now

    def sure(self, kosul: str, var: bool, now: float) -> float:
        """How long ``kosul`` has been true (0 when false; starts the clock)."""
        if not var:
            self.since.pop(kosul, None)
            return 0.0
        t0 = self.since.setdefault(kosul, now)
        return now - t0


# ----------------------------------------------------------------- parsing

def parse_pwtop(out: str) -> dict[str, int]:
    """Last sample of ``pw-top -b`` → {"<id>:<name>": ERR}. Unknown lines skipped."""
    blocks: list[list[str]] = []
    cur: list[str] | None = None
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == "S" and f[1] == "ID":
            cur = []
            blocks.append(cur)
        elif cur is not None and line.strip():
            cur.append(line)
    res: dict[str, int] = {}
    for line in (blocks[-1] if blocks else []):
        f = line.split()
        if len(f) < 10 or not f[1].isdigit():
            continue
        try:
            err = int(f[8])
        except ValueError:
            continue
        rest = f[9:]
        if len(rest) >= 4 and rest[1].isdigit() and rest[2].isdigit():
            rest = rest[3:]                       # FORMAT "S24LE 2 48000"
        if rest and rest[0] == "+":
            rest = rest[1:]
        name = " ".join(rest).lstrip("+").strip()
        res[f"{f[1]}:{name}"] = err
    return res


def _xrun_ad(key: str) -> str:
    name = key.split(":", 1)[-1]
    if "HS80" in name.upper():
        return "HS80"
    return name if len(name) <= 32 else name[:31] + "…"


# ----------------------------------------------------------------- decisions

def _eksik(gozlem: Gozlem | None, status: dict) -> list[str] | None:
    if gozlem is not None and gozlem.sinks is not None:
        have = set(gozlem.sinks)
        return [sink_name(p) for p in ALL_SINK_PROFILES if sink_name(p) not in have]
    if "sinks_ready" in status:
        return [] if status.get("sinks_ready") else ["?"]
    return None


def _kontrol_cikislar(status: dict, now: float, g: Gozlem | None, d: Durum, a: Ayar) -> list[Eylem]:
    eksik = _eksik(g, status)
    aktif = bool(status.get("active"))
    var = bool(aktif and eksik)
    sure = d.sure("eksik", var, now)
    if not var:
        if eksik == [] and d.ardisik_restart:
            log.info("EQ çıkışları geri geldi (ardışık yeniden başlatma sayacı sıfırlandı)")
            d.ardisik_restart = 0
        return []
    if sure < EKSIK_BEKLE:
        return []
    unit = (g.sinks_unit if g is not None else None) or {}
    liste = ", ".join(x for x in eksik if x != "?") or _("EQ çıkışları")
    if unit and unit.get("LoadState") not in (None, "loaded"):
        if d.izin("eksik_bildirim", now, EKSIK_BILDIRIM_ARALIK):
            d.isaretle("eksik_bildirim", now)
            return [Eylem(BILDIRIM, Metin(N_("EQ çıkışları eksik ve {servis} kurulu değil"),
                              servis=SINKS_UNIT),
                          "eksik_bildirim", baslik=_("Kulak Kesil: EQ çıkışları yok"),
                          govde=Metin(N_("Eksik: {liste}. Servis kurulu değil; {komut} ile kurun."),
                              liste=liste, komut="./install.sh"))]
        return []
    if unit.get("ActiveState") in ("activating", "reloading", "deactivating"):
        return []                           # a (re)start is already in progress
    if g is not None and g.conf_var is False:
        # ConditionPathExists: without the conf the unit never starts, a restart "succeeds"
        # and changes nothing — say what is really missing
        if d.izin("eksik_bildirim", now, EKSIK_BILDIRIM_ARALIK):
            d.isaretle("eksik_bildirim", now)
            return [Eylem(BILDIRIM, Metin(N_("EQ çıkışları eksik: {dosya} yok"),
                              dosya=SINKS_CONF_AD), "eksik_bildirim",
                          baslik=_("Kulak Kesil: EQ çıkışları yok"),
                          govde=Metin(N_("EQ ayar dosyası ({dosya}) yok; {komut} ile üretin."),
                              dosya=SINKS_CONF_AD, komut="kulak eq-uret"))]
        return []
    if unit.get("ActiveState") == "inactive" and unit.get("Result") == "success":
        # stopped cleanly = on purpose (`systemctl --user stop oto-eq-sinks`): a crash ends
        # in "failed" / auto-restart. Do not fight the user; log it once per episode.
        if d.son.get("durduruldu_bolum") != d.since.get("eksik"):
            d.son["durduruldu_bolum"] = d.since.get("eksik")
            return [Eylem(KAYIT, Metin(N_("EQ çıkışları eksik ama {servis} elle durdurulmuş; "
                                          "yeniden başlatılmıyor"), servis=SINKS_UNIT),
                          "durduruldu")]
        return []
    if a.otomatik_yeniden_baslat and d.ardisik_restart < RESTART_UST \
            and d.izin("restart", now, RESTART_ARALIK):
        d.isaretle("restart", now)
        d.ardisik_restart += 1
        d.since.pop("eksik", None)          # give the restart a fresh grace period
        return [Eylem(YENIDEN_BASLAT, Metin(N_("EQ çıkışları {sn} sn'dir eksik ({liste})"),
                          sn=int(sure), liste=liste),
                      "restart", hedef=SINKS_UNIT,
                      baslik=_("Kulak Kesil: EQ çıkışları onarıldı"),
                      govde=Metin(N_("Eksik çıkışlar ({liste}) için {servis} yeniden başlatıldı."),
                          liste=liste, servis=SINKS_UNIT))]
    if d.izin("eksik_bildirim", now, EKSIK_BILDIRIM_ARALIK):
        d.isaretle("eksik_bildirim", now)
        neden = (_("otomatik onarım sınırına ulaşıldı") if d.ardisik_restart >= RESTART_UST
                 or not a.otomatik_yeniden_baslat else _("10 dakikada en çok bir otomatik onarım"))
        return [Eylem(BILDIRIM, Metin(N_("EQ çıkışları hâlâ eksik ({liste}); {neden}"),
                          liste=liste, neden=neden), "eksik_bildirim",
                      baslik=_("Kulak Kesil: EQ çıkışları eksik"),
                      govde=Metin(N_("Eksik: {liste} ({neden}). Sesler düz çıkışa gidiyor. "
                              "Elle: {komut} · {saglik}"),
                          liste=liste, neden=neden,
                          komut=f"systemctl --user restart {SINKS_UNIT}", saglik="kulak saglik"))]
    return []


def _kontrol_xrun(status: dict, now: float, g: Gozlem | None, d: Durum, a: Ayar) -> list[Eylem]:
    if g is None or g.xrun is None:
        return []
    onceki, t0 = d.xrun_onceki, d.xrun_zaman
    d.xrun_onceki, d.xrun_zaman = dict(g.xrun), now
    if onceki is None or t0 is None or now - t0 > XRUN_MAX_ARALIK:
        return []
    artis: dict[str, int] = {}
    for k, v in g.xrun.items():
        if k in onceki:
            delta = v - onceki[k] if v >= onceki[k] else v   # counter reset → count anew
            if delta > 0:
                artis[k] = delta
    toplam = sum(artis.values())
    if toplam < XRUN_ESIK:
        return []
    en = sorted(artis.items(), key=lambda kv: -kv[1])[:3]
    ayrinti = ", ".join(f"{_xrun_ad(k)} +{v}" for k, v in en)
    if not d.izin("xrun", now, XRUN_BILDIRIM_ARALIK):
        return [Eylem(KAYIT, Metin(N_("xrun artışı ({toplam}: {ayrinti}); bildirim yakın zamanda "
                               "gönderildi"), toplam=toplam, ayrinti=ayrinti),
                      "xrun_kayit")]
    d.isaretle("xrun", now)
    return [Eylem(BILDIRIM, Metin(N_("xrun artışı: {sn} sn'de {toplam} kesinti ({ayrinti})"),
                      sn=int(now - t0), toplam=toplam, ayrinti=ayrinti),
                  "xrun", baslik=_("Kulak Kesil: ses kesintisi (xrun)"),
                  govde=Metin(N_("Son {sn} sn'de {toplam} ses kesintisi: {ayrinti}. Sistem çok "
                          "yüklüyse geçicidir; sürerse: {komut}"),
                      sn=int(now - t0), toplam=toplam, ayrinti=ayrinti, komut="kulak saglik"))]


def _kontrol_hs80(status: dict, now: float, g: Gozlem | None, d: Durum, a: Ayar) -> list[Eylem]:
    out: list[Eylem] = []
    if g is None or g.sinks is None or g.default_sink is None:
        return out
    try:
        rx = re.compile(a.kulaklik_desen or "(?i)hs80")
    except re.error:
        rx = re.compile("(?i)hs80")
    default = g.default_sink
    hs80 = sorted(s for s in g.sinks if rx.search(s) and not s.startswith(SINK_PREFIX))
    bizim = default.startswith(SINK_PREFIX)

    # (a) one of OUR sinks became the default: EQ is bypassed, feedback-loop risk. Not when
    # no real output exists at all (the only headphone was unplugged): WirePlumber then
    # falls back to one of ours (priority 1 is still a candidate) — nothing to fix by hand.
    gercek = [s for s in g.sinks if not s.startswith(SINK_PREFIX) and not s.endswith(".monitor")]
    if d.sure("varsayilan_eq", bizim and bool(gercek), now) >= 5.0 \
            and d.izin("varsayilan_eq", now, VARSAYILAN_EQ_BILDIRIM_ARALIK):
        d.isaretle("varsayilan_eq", now)
        out.append(Eylem(BILDIRIM, Metin(N_("varsayılan çıkış bir Kulak Kesil çıkışı: {cikis}"),
                             cikis=default),
                         "varsayilan_eq", baslik=_("Kulak Kesil: yanlış varsayılan çıkış"),
                         govde=Metin(N_("Varsayılan çıkış '{cikis}' seçilmiş; EQ devre dışı. "
                                 "Ses ayarlarından kulaklığınızı (ya da hoparlörü) seçin."),
                             cikis=default)))

    # (b) HS80 present but not the default (after the router's own 30 s window)
    # once per episode (a speaker chosen on purpose must not cause a nudge every 10 min)
    yok = bool(hs80) and not bizim and not rx.search(default)
    if d.sure("hs80_degil", yok, now) >= HS80_BEKLE \
            and d.son.get("hs80_bolum") != d.since["hs80_degil"] \
            and d.izin("hs80_tara", now, HS80_TARA_ARALIK):
        d.isaretle("hs80_tara", now)
        d.son["hs80_bolum"] = d.since["hs80_degil"]
        out.append(Eylem(TARA, Metin(N_("HS80 takılı ({takili}) ama varsayılan çıkış {cikis}; HS80 "
                                 "takibine yeniden bakılıyor (elle seçildiyse dokunulmaz)"),
                             takili=", ".join(_kisa(h) for h in hs80),
                             cikis=_kisa(default) or _("yok")), "hs80_tara"))

    # (c) router status out of date (its default ≠ the real default)
    st_def = status.get("default_sink")
    eski = st_def is not None and st_def != default
    if d.sure("eski_durum", eski, now) >= ESKI_DURUM_BEKLE \
            and d.izin("eski_durum", now, ESKI_DURUM_ARALIK):
        d.isaretle("eski_durum", now)
        out.append(Eylem(TARA, Metin(N_("yönlendirici durumu eski (onun varsayılanı {onun}, gerçek "
                                 "{gercek}); yeniden taranıyor"),
                             onun=_kisa(st_def), gercek=_kisa(default)), "eski_durum"))
    return out


def _kontrol_wp(status: dict, now: float, g: Gozlem | None, d: Durum, a: Ayar) -> list[Eylem]:
    if g is None or not g.wp_kalinti or not a.wp_otomatik_temizle:
        return []
    sessiz = not (status.get("streams") or []) and not status.get("dominant") \
        and g.kayit_sayisi == 0
    if not sessiz or not d.izin("wp", now, WP_ARALIK):
        return []
    d.isaretle("wp", now)
    return [Eylem(WP_TEMIZLE, Metin(N_("WirePlumber durumunda {n} test kalıntısı var; hiçbir şey "
                                "çalmıyorken temizleniyor (yedekli)"), n=g.wp_kalinti), "wp")]


def _kisa(name: str | None) -> str:
    if not name:
        return ""
    if "HS80" in name.upper():
        return _("HS80 kablolu") if "wireless" not in name.lower() else _("HS80 kablosuz")
    return name if len(name) <= 40 else name[:39] + "…"


KONTROLLER = (_kontrol_cikislar, _kontrol_xrun, _kontrol_hs80, _kontrol_wp)

_VARSAYILAN_DURUM = Durum()


def kontrol(status: dict | None, now: float, gozlem: Gozlem | None = None,
            durum: Durum | None = None, ayar: Ayar | None = None) -> list[Eylem]:
    """Decide the actions for this round. Pure: no I/O; updates ``durum`` only.

    ``status`` = router status dict; ``None``/{} (router down) → no actions (the
    systemd unit's Restart= handles the daemon itself). ``gozlem`` None → only
    the status-based checks run.
    """
    if not status:
        return []
    d = durum if durum is not None else _VARSAYILAN_DURUM
    a = ayar or Ayar()
    out: list[Eylem] = []
    for chk in KONTROLLER:
        try:
            out.extend(chk(status, now, gozlem, d, a))
        except Exception:  # noqa: BLE001 - one broken check must not stop the others
            log.exception("onarım kontrolü %s hata verdi", chk.__name__)
    return out


# ----------------------------------------------------------------- observations

Runner = Callable[[list[str]], tuple[int, str]]


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, env=config.C_ENV, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def wp_state_dir() -> Path:
    return config._xdg("XDG_STATE_HOME", Path.home() / ".local/state") / "wireplumber"


def wp_modul() -> Any | None:
    """tools/wp_temizle.py loaded as a module (it is not a package); None if missing."""
    path = config.SRC_DIR / "tools" / "wp_temizle.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("oto_eq_wp_temizle", path)
    if spec is None or spec.loader is None:
        return None
    if "oto_eq_wp_temizle" in sys.modules:
        return sys.modules["oto_eq_wp_temizle"]
    mod = importlib.util.module_from_spec(spec)
    sys.modules["oto_eq_wp_temizle"] = mod      # dataclasses need the module registered
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001
        sys.modules.pop("oto_eq_wp_temizle", None)
        log.exception("tools/wp_temizle.py yüklenemedi")
        return None
    return mod


def wp_kalinti_say(state_dir: Path | None = None, wp: Any = None) -> int | None:
    wp = wp if wp is not None else wp_modul()
    if wp is None:
        return None
    try:
        return wp.make_plan(Path(state_dir or wp_state_dir())).count
    except OSError:
        return None


def gozle(run: Runner = _run, xrun: bool = True, wp: bool = True,
          state_dir: Path | None = None, conf: Path | None = None) -> Gozlem:
    """Collect the observations (read-only commands only). ``conf``: the sinks conf whose
    existence is checked (default config.SINKS_CONF)."""
    g = Gozlem()
    rc, out = run(["pactl", "list", "short", "sinks"])
    if rc == 0:
        g.sinks = [l.split("\t")[1] for l in out.splitlines() if l.count("\t") >= 1]
    rc, out = run(["pactl", "get-default-sink"])
    if rc == 0:
        g.default_sink = out.strip()
    rc, out = run(["systemctl", "--user", "show", SINKS_UNIT, "-p",
                   "LoadState,ActiveState,SubState,Result"])
    if rc == 0:
        g.sinks_unit = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        try:
            g.conf_var = Path(conf or config.SINKS_CONF).exists()
        except OSError:
            g.conf_var = None
    rc, out = run(["pactl", "list", "short", "source-outputs"])
    if rc == 0:
        n = 0
        for l in out.splitlines():
            f = l.split("\t")
            if len(f) >= 3 and f[2].strip() not in ("", "-"):
                n += 1
        g.kayit_sayisi = n
    if xrun:
        rc, out = run(["pw-top", "-b", "-n", "2"])
        if rc == 0 and out.strip():
            g.xrun = parse_pwtop(out)
    if wp:
        g.wp_kalinti = wp_kalinti_say(state_dir)
    return g


# ----------------------------------------------------------------- execution

def bildir(baslik: str, govde: str = "", run: Callable[..., Any] = subprocess.run) -> bool:
    """notify-send (C.UTF-8 so Turkish text survives); never raises."""
    cmd = ["notify-send", "-a", APP_NAME, "-i", "audio-headphones", "-t", "8000", "--",
           baslik]
    if govde:
        cmd.append(govde)
    try:
        r = run(cmd, capture_output=True, text=True, timeout=3, env=NOTIFY_ENV)
        return getattr(r, "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("bildirim gönderilemedi: %s", e)
        return False


class Sonuc(str):
    """uygula() result line (translated) + ``basarili``: success as a flag, so nothing has
    to search the (translated) text (saglik.check_onarim reads onarim.json "basarili")."""
    basarili: bool

    def __new__(cls, text: str, basarili: bool = True) -> "Sonuc":
        o = super().__new__(cls, text)
        o.basarili = basarili
        o.kayit = metin_kaydi(text)        # msgid record when text is a Metin (re-translatable)
        return o


DOGRULA_SN = 6.0        # after a sinks restart: wait at most this long for the EQ sinks


def cikislar_dondu(run: Callable[..., Any] = subprocess.run, sure: float = DOGRULA_SN) -> bool:
    """Are all EQ sinks back? (`systemctl restart` of a Type=simple unit returns as soon
    as the process starts: a broken conf makes it exit a second later)."""
    want = {sink_name(p) for p in ALL_SINK_PROFILES}
    end = time.monotonic() + sure
    while True:
        try:
            r = run(["pactl", "list", "short", "sinks"], capture_output=True, text=True,
                    timeout=5, env=config.C_ENV)
            have = {ln.split("\t")[1] for ln in (getattr(r, "stdout", "") or "").splitlines()
                    if ln.count("\t") >= 1}
            if want <= have:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.5)


def uygula(e: Eylem, *, dry_run: bool = False, run: Callable[..., Any] = subprocess.run,
           notify: Callable[[str, str], Any] | None = None,
           rescan: Callable[[], Any] | None = None, bildirim: bool = True,
           wp: Any = None, state_dir: Path | None = None,
           dogrula: Callable[[], bool] | None = None,
           conf_var: Callable[[], bool] | None = None) -> Sonuc:
    """Execute one action; returns the (translated) result line as a ``Sonuc``
    (``.basarili`` = success). ``dry_run`` → only describe. ``dogrula``: after restarting
    the sinks, check they came back; ``conf_var``: does the sinks conf exist (a failed
    check then names the real cause). Default for both: really check when ``run`` is the
    real subprocess.run; a test double skips them unless it passes its own."""
    if run is subprocess.run:
        dogrula = dogrula or cikislar_dondu
        conf_var = conf_var or config.SINKS_CONF.exists
    r = _uygula(e, dry_run=dry_run, run=run, notify=notify, rescan=rescan, bildirim=bildirim,
                wp=wp, state_dir=state_dir, dogrula=dogrula, conf_var=conf_var)
    return r if isinstance(r, Sonuc) else Sonuc(r)


def _uygula(e: Eylem, *, dry_run: bool, run: Callable[..., Any],
            notify: Callable[[str, str], Any] | None, rescan: Callable[[], Any] | None,
            bildirim: bool, wp: Any, state_dir: Path | None,
            dogrula: Callable[[], bool] | None = None,
            conf_var: Callable[[], bool] | None = None) -> str:
    notify = notify or (lambda b, g: bildir(b, g, run=run))
    if e.tur == YENIDEN_BASLAT:
        cmd = ["systemctl", "--user", "restart", e.hedef or SINKS_UNIT]
        if dry_run:
            return Metin(N_("[deneme] {komut} ({neden})"), komut=" ".join(cmd), neden=e.neden)
        try:
            r = run(cmd, capture_output=True, text=True, timeout=30, env=config.C_ENV)
            ok = getattr(r, "returncode", 1) == 0
            err = (getattr(r, "stderr", "") or "").strip()[:200]
        except (OSError, subprocess.SubprocessError) as ex:
            ok, err = False, str(ex)
        if ok and dogrula is not None and (e.hedef or SINKS_UNIT) == SINKS_UNIT and not dogrula():
            if conf_var is not None and not conf_var():   # ConditionPathExists failed
                ok, err = False, _("EQ ayar dosyası ({dosya}) yok; {komut} ile üretin").format(
                    dosya=SINKS_CONF_AD, komut="kulak eq-uret")
            else:
                ok, err = False, _("servis başladı ama EQ çıkışları geri gelmedi (yapılandırma "
                                   "bozuk olabilir: journalctl --user -u oto-eq-sinks)")
        if ok:
            log.warning("onarım: %s → %s yeniden başlatıldı", e.neden, e.hedef)
            if bildirim and e.baslik:
                notify(e.baslik, e.govde)
            return Metin(N_("{servis} yeniden başlatıldı ({neden})"), servis=e.hedef, neden=e.neden)
        log.error("onarım: %s yeniden başlatılamadı: %s", e.hedef, err)
        if bildirim:
            notify(_("Kulak Kesil: onarım başarısız"),
                   Metin(N_("{servis} yeniden başlatılamadı: {hata}"),
                       servis=e.hedef, hata=err or _("bilinmeyen hata")))
        return Sonuc(Metin(N_("{servis} yeniden başlatılamadı: {hata}"), servis=e.hedef, hata=err),
                     False)
    if e.tur == BILDIRIM:
        if dry_run:
            return Metin(N_("[deneme] bildirim: {baslik} — {govde}"), baslik=e.baslik, govde=e.govde)
        log.warning("onarım: %s", e.neden)
        if bildirim:
            notify(e.baslik, e.govde)
        return Metin(N_("bildirim: {baslik}"), baslik=e.baslik)
    if e.tur == TARA:
        if dry_run:
            return Metin(N_("[deneme] yeniden tarama ({neden})"), neden=e.neden)
        log.info("onarım: %s", e.neden)
        if rescan is not None:
            rescan()
            return Metin(N_("yeniden tarama istendi ({neden})"), neden=e.neden)
        return Sonuc(Metin(N_("yeniden tarama yapılamadı (yönlendirici bağlı değil): {neden}"),
            neden=e.neden), False)
    if e.tur == WP_TEMIZLE:
        if dry_run:
            return Metin(N_("[deneme] WirePlumber test kalıntıları temizlenecek ({neden})"),
                neden=e.neden)
        mod = wp if wp is not None else wp_modul()
        if mod is None:
            return Sonuc(Metin(N_("{dosya} bulunamadı; temizlenmedi"), dosya="tools/wp_temizle.py"),
                         False)
        log.info("onarım: %s", e.neden)
        ok = mod.apply(Path(state_dir or wp_state_dir()), say=lambda s: log.info("wp_temizle: %s", s))
        return Sonuc(Metin(N_("WirePlumber test kalıntıları temizlendi"))) if ok else \
            Sonuc(Metin(N_("WirePlumber temizliği başarısız (yedekler geri yüklendi)")), False)
    # KAYIT and unknown kinds: log only
    log.info("onarım: %s", e.neden)
    return e.neden


def gecmis_yaz(kayitlar: list[dict], path: Path | None = None) -> None:
    """Last actions → STATE_DIR/onarim.json (atomic; read by `kulak saglik`)."""
    p = Path(path or GECMIS_DOSYA)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"son": kayitlar[-GECMIS_UZUNLUK:]}, ensure_ascii=False,
                                  indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError as ex:
        log.debug("onarım geçmişi yazılamadı: %s", ex)


class Onarici:
    """Periodic runner: ``adim()`` once, or ``baslat(aralik)`` in a daemon thread."""

    def __init__(self, status_fn: Callable[[], dict | None], *,
                 rescan: Callable[[], Any] | None = None, ayar: Ayar | None = None,
                 kulaklik_desen: str | None = None, dry_run: bool = False,
                 gozle_fn: Callable[[], Gozlem] | None = None,
                 run: Callable[..., Any] = subprocess.run,
                 notify: Callable[[str, str], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 gecmis_dosya: Path | None = GECMIS_DOSYA):
        self.status_fn = status_fn
        self.rescan = rescan
        self.ayar = ayar or Ayar()
        if kulaklik_desen:
            self.ayar.kulaklik_desen = kulaklik_desen
        self.dry_run = dry_run
        self.gozle_fn = gozle_fn or gozle
        self.run = run
        self.notify = notify
        self.clock = clock
        self.wall = wall
        self.gecmis_dosya = gecmis_dosya
        self.durum = Durum()
        self.gecmis: deque[dict] = deque(maxlen=GECMIS_UZUNLUK)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def adim(self) -> list[tuple[Eylem, str]]:
        try:
            status = self.status_fn()
        except Exception as ex:  # noqa: BLE001
            log.debug("onarım: durum alınamadı: %s", ex)
            return []
        if not status:
            return []
        try:
            g = self.gozle_fn()
        except Exception:  # noqa: BLE001
            log.exception("onarım: gözlem hatası")
            g = None
        sonuc = []
        for e in kontrol(status, self.clock(), g, self.durum, self.ayar):
            try:
                r = uygula(e, dry_run=self.dry_run, run=self.run, notify=self.notify,
                           rescan=self.rescan, bildirim=self.ayar.bildirim)
            except Exception as ex:  # noqa: BLE001
                log.exception("onarım eylemi başarısız: %s", e.tur)
                r = Sonuc(Metin(N_("hata: {tur}: {hata}"), tur=type(ex).__name__, hata=ex), False)
            sonuc.append((e, r))
            if e.tur != KAYIT:
                kayit = {"zaman": round(self.wall()), "tur": e.tur,
                         "neden": e.neden, "sonuc": str(r), "deneme": self.dry_run,
                         "basarili": bool(getattr(r, "basarili", True))}
                # msgid + arguments: the UI shows a stored entry in ITS current language
                for alan, deger in (("neden", e.neden), ("sonuc", r)):
                    k = getattr(deger, "kayit", None) or metin_kaydi(deger)
                    if isinstance(k, dict):
                        kayit[alan + "_k"] = k
                self.gecmis.append(kayit)
        if sonuc and self.gecmis_dosya is not None and not self.dry_run:
            gecmis_yaz(list(self.gecmis), self.gecmis_dosya)
        return sonuc

    def durum_ozet(self) -> dict:
        """Small dict the coordinator may add to the router status (``onarim``)."""
        return {"son": list(self.gecmis)[-3:], "ardisik_restart": self.durum.ardisik_restart}

    def baslat(self, aralik: float = 60.0, ilk_gecikme: float = 30.0) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            if self._stop.wait(ilk_gecikme):
                return
            while not self._stop.is_set():
                try:
                    self.adim()
                except Exception:  # noqa: BLE001
                    log.exception("onarım adımı hata verdi")
                if self._stop.wait(aralik):
                    return

        self._thread = threading.Thread(target=loop, name="oto-eq-onarim", daemon=True)
        self._thread.start()

    def durdur(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None


def gecmis_oku(path: Path | None = None) -> list[dict]:
    try:
        data = json.loads(Path(path or GECMIS_DOSYA).read_text(encoding="utf-8"))
        son = data.get("son") if isinstance(data, dict) else None
        return [x for x in son if isinstance(x, dict)] if isinstance(son, list) else []
    except (OSError, ValueError):
        return []


__all__ = ["Ayar", "Durum", "Eylem", "Gozlem", "Onarici", "Sonuc", "bildir", "gecmis_oku", "gecmis_yaz",
           "gozle", "kontrol", "parse_pwtop", "uygula"]
