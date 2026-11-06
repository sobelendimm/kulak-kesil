"""Post-install health check ("sağlık kontrolü"): ``python3 -m oto_eq.saglik [--json]``.

Strictly READ-ONLY: it only runs listing commands (systemctl show, pactl list,
pw-dump, pw-top -b), reads files under /proc, /sys and the WirePlumber state,
and asks the router for its status. It never changes the audio graph, units or
files. It also works before install: everything is then reported as missing,
with a ``./install.sh`` hint.

API: ``run_checks(env=None) -> list[dict]``. Each item is
``{"ad", "durum": "tamam"|"uyari"|"hata", "ayrinti", "oneri"}``.
CHANGE 8: "durum" is machine-readable (never translated); "ad", "ayrinti" and "oneri"
are display texts in the UI language (shell commands inside them stay as they are).
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, i18n, kulaklik
from . import ui_text as tx
from .i18n import N_, _
from .models import ALL_SINK_PROFILES, SINK_PREFIX, sink_name

TAMAM, UYARI, HATA = "tamam", "uyari", "hata"
UNITS = (config.DAEMON_SERVICE, config.SINKS_SERVICE, "oto-eq-mic.service")
MIC_SOURCE = "oto_eq_mikrofon"
MIC_CONF = config.CONFIG_DIR / "mikrofon.conf"
MIC_FILE = "mikrofon.toml"
HS80_VENDOR = "1b1c"
HS80_PRODUCTS = {"0a6b": N_("kablosuz alıcı"), "0a69": N_("kablolu")}     # translated at use
LV2_DIRS = ("~/.lv2", "/usr/local/lib/lv2", "/usr/lib/lv2", "/usr/lib64/lv2")
LADSPA_DIRS = ("~/.ladspa", "/usr/local/lib/ladspa", "/usr/lib/ladspa", "/usr/lib64/ladspa")
LSP_TTL = "lsp-plugins.lv2/compressor_stereo.ttl"
RNNOISE_SO = "librnnoise_ladspa.so"
CPU_WARN = 10.0          # % of one core for a sinks/mic pipewire process
TEST_LEFTOVER = re.compile(r"oto[-_]eq[-_](test|perf|e2e)|a3_?rep|a4\\sbqtest", re.I)
INSTALL_HINT = "./install.sh"
DEFAULT_HEADSET_PATTERN = "(?i)hs80"      # kurallar.toml [genel] kulaklik_desen default (router)

# CHANGE 7 texts (whole sentences, named placeholders); CHANGE 8: marked here, translated
# at use with _() (the language is fixed per process, but import order must not matter)
KULAKLIK_TAMAM = "{tur}: {model} ({duzeltme})"
KULAKLIK_ARAMA = N_("{tur}: {model} arama modunda ({profil}): ses dar bantlı ve kalitesiz")
KULAKLIK_ARAMA_ONERI = N_("Müzik ve oyun için KDE ses ayarlarından Bluetooth aygıtının profilini "
                          "A2DP (yüksek kalite) yapın")
KULAKLIK_DEGIL = N_("varsayılan çıkış kulaklık değil: {tur} ({model}; {neden}) — EQ devre dışı")
KULAKLIK_DEGIL_ONERI = N_("Kulaklığı takın ya da ses ayarlarından varsayılan çıkış yapın; "
                          "kulaklığınız tanınmıyorsa kurallar.toml [genel] kulaklik_desen ile "
                          "ekleyin")
KULAKLIK_YOK = N_("varsayılan çıkış yok")
KULAKLIK_YOK_ONERI = N_("Ses ayarlarından bir çıkış seçin (kulaklık takılı mı?)")
HS80_BASKA = N_("takılı değil; şu an {tur} kullanılıyor: {model} (HS80'e özgü düzeltme, "
                "kablolu↔kablosuz takip ve mikrofon zinciri bu kulaklıkta devre dışı)")


def _r(ad: str, durum: str, ayrinti: str, oneri: str = "") -> dict:
    """One result; ad/ayrinti/oneri are already display texts (wrapped at the call site)."""
    return {"ad": ad, "durum": durum, "ayrinti": ayrinti, "oneri": oneri}


# --------------------------------------------------------------------------
# system access (replaced by a fake in tests)

class Env:
    """Read-only access to the system."""

    clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100

    def run(self, cmd: list[str], timeout: float = 5.0) -> tuple[int, str, str]:
        try:
            p = subprocess.run(cmd, env=config.C_ENV, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", _("{komut} bulunamadı").format(komut=cmd[0])
        except subprocess.TimeoutExpired:
            return 124, "", _("{komut} zaman aşımı").format(komut=cmd[0])
        except OSError as e:
            return 126, "", str(e)

    def read_text(self, path: Path | str) -> str | None:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def exists(self, path: Path | str) -> bool:
        return Path(path).expanduser().exists()

    def mtime(self, path: Path | str) -> float | None:
        try:
            return Path(path).expanduser().stat().st_mtime
        except OSError:
            return None

    def listdir(self, path: Path | str) -> list[str]:
        try:
            return sorted(os.listdir(Path(path).expanduser()))
        except OSError:
            return []

    def ipc_status(self) -> dict:
        from . import ipc
        return ipc.request({"cmd": "status"}, timeout=2.0)

    def rules(self) -> dict:
        return config.load_toml(config.RULES_FILE)

    def config_path(self, name: str) -> Path:
        return config.config_path(name)

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def module_available(self, name: str) -> bool:
        """Installed without importing it (find_spec); never raises."""
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def attr(self, module: str, name: str) -> Any:
        """``module.name`` or None (module missing / mid-edit / attribute absent)."""
        try:
            mod = importlib.import_module(module)
        except Exception:  # noqa: BLE001 - another agent's module may be broken
            return None
        return getattr(mod, name, None)

    def conf_outdated(self) -> bool | None:
        """eqgen.conf_outdated() if eqgen has it (read-only), else None."""
        fn = self.attr("oto_eq.eqgen", "conf_outdated")
        if not callable(fn):
            return None
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return None

    def sleep(self, s: float) -> None:
        time.sleep(s)


# --------------------------------------------------------------------------
# shared facts (collected once)

class Facts:
    def __init__(self, env: Env):
        self.env = env
        self._cache: dict[str, Any] = {}

    def _get(self, key: str, fn: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def unit(self, name: str) -> dict[str, str]:
        def f():
            rc, out, _e = self.env.run(["systemctl", "--user", "show", name, "-p",
                                       "LoadState,ActiveState,SubState,UnitFileState,NRestarts,MainPID"])
            if rc != 0:
                return {}
            return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        return self._get("unit:" + name, f)

    def sinks(self) -> list[str] | None:
        def f():
            rc, out, _e = self.env.run(["pactl", "list", "short", "sinks"])
            if rc != 0:
                return None
            return [l.split("\t")[1] for l in out.splitlines() if l.count("\t") >= 1]
        return self._get("sinks", f)

    def default(self, kind: str) -> str:
        def f():
            rc, out, _e = self.env.run(["pactl", f"get-default-{kind}"])
            return out.strip() if rc == 0 else ""
        return self._get("default:" + kind, f)

    def dump(self) -> list | None:
        def f():
            rc, out, _e = self.env.run(["pw-dump"], timeout=8.0)
            if rc != 0:
                return None
            try:
                d = json.loads(out)
                return d if isinstance(d, list) else None
            except ValueError:
                return None
        return self._get("dump", f)

    def sink_infos(self) -> dict[str, dict] | None:
        """Raw ``pactl -f json list sinks`` items by node name (C locale; None = pactl failed)."""
        def f():
            rc, out, _e = self.env.run(["pactl", "-f", "json", "list", "sinks"])
            if rc != 0:
                return None
            try:
                d = json.loads(out)
            except ValueError:
                return None
            return {str(x.get("name")): x for x in d if isinstance(x, dict)} \
                if isinstance(d, list) else None
        return self._get("sink_infos", f)

    def _duzeltme_desen(self) -> str | None:
        """[duzeltme] cihaz_desen of profiller.toml (None = default / unreadable)."""
        import tomllib
        text = self.env.read_text(self.env.config_path(config.PROFILES_FILE))
        if not text:
            return None
        try:
            v = (tomllib.loads(text).get("duzeltme") or {}).get("cihaz_desen")
        except (tomllib.TOMLDecodeError, AttributeError):
            return None
        return v if isinstance(v, str) else None

    def aygit(self) -> "kulaklik.Aygit | None":
        """The default output classified like the router does (None = no default output)."""
        def f():
            name = self.default("sink")
            if not name:
                return None
            try:
                genel = (self.env.rules() or {}).get("genel")
            except Exception:  # noqa: BLE001 - broken rules: check_configs reports it
                genel = None
            g = genel if isinstance(genel, dict) else {}
            desen = g.get("kulaklik_desen")
            desen = desen if isinstance(desen, str) and desen else DEFAULT_HEADSET_PATTERN
            ayar, _err = kulaklik.ayar_oku(g, desen, self._duzeltme_desen())
            info = (self.sink_infos() or {}).get(name)
            return kulaklik.siniflandir(name, "", info, ayar)
        return self._get("aygit", f)

    def unit_start(self, name: str) -> float | None:
        """Unix time the unit last became active (None = unknown / never)."""
        def f():
            rc, out, _e = self.env.run(["systemctl", "--user", "show", name, "-p",
                                       "ActiveEnterTimestamp", "--timestamp=unix"])
            m = re.search(r"ActiveEnterTimestamp=@(\d+(?:\.\d+)?)", out) if rc == 0 else None
            return float(m.group(1)) if m else None
        return self._get("start:" + name, f)

    def status(self) -> dict | None:
        def f():
            try:
                st = self.env.ipc_status()
                return st if isinstance(st, dict) and st.get("ok") else None
            except (OSError, ValueError):
                return None
        return self._get("status", f)


# --------------------------------------------------------------------------
# checks

def check_units(fx: Facts) -> list[dict]:
    out = []
    for u in UNITS:
        ad = _("servis {ad}").format(ad=u.removesuffix(".service"))
        info = fx.unit(u)
        if not info:
            out.append(_r(ad, HATA, _("systemctl --user okunamadı"),
                          _("kullanıcı oturumunda çalıştırın")))
            continue
        if info.get("LoadState") != "loaded":
            out.append(_r(ad, HATA, _("kurulu değil"), INSTALL_HINT))
            continue
        active, sub = info.get("ActiveState", "?"), info.get("SubState", "?")
        enabled = info.get("UnitFileState", "")
        nr = info.get("NRestarts", "0")
        restarts = int(nr) if nr.isdigit() else 0
        if active != "active":
            out.append(_r(ad, HATA, _("çalışmıyor ({durum}/{alt})").format(durum=active, alt=sub),
                          _("systemctl --user start {birim}; günlük: journalctl --user -u {birim} "
                            "-n 30").format(birim=u)))
            continue
        notes, durum, oneri = [], TAMAM, ""
        if enabled not in ("enabled", "static", "linked", "alias", "enabled-runtime"):
            durum, oneri = UYARI, f"systemctl --user enable {u}"
            notes.append(_("çalışıyor ama açılışta başlamıyor ({durum})").format(
                durum=enabled or _("bilinmiyor")))
        if restarts:
            durum = UYARI
            notes.append(_("{n} kez yeniden başladı").format(n=restarts))
            oneri = oneri or f"journalctl --user -u {u} -n 50"
        out.append(_r(ad, durum, "; ".join(notes) or _("etkin ve çalışıyor"), oneri))
    return out


def check_sinks(fx: Facts) -> list[dict]:
    sinks = fx.sinks()
    if sinks is None:
        return [_r(_("EQ çıkışları"), HATA, _("pactl çalışmıyor (ses sunucusu yok?)"),
                   "systemctl --user status pipewire-pulse")]
    want = [sink_name(p) for p in ALL_SINK_PROFILES]
    missing = [s for s in want if s not in sinks]
    out = []
    if missing:
        installed = fx.unit(config.SINKS_SERVICE).get("LoadState") == "loaded"
        out.append(_r(_("EQ çıkışları"), HATA,
                      _("eksik: {cikislar}").format(cikislar=", ".join(missing))
                      if len(missing) < len(want) else _("hiçbiri yok"),
                      f"systemctl --user restart {config.SINKS_SERVICE}" if installed
                      else INSTALL_HINT))
    else:
        out.append(_r(_("EQ çıkışları"), TAMAM, _("{n} çıkışın hepsi var").format(n=len(want))))

    default = fx.default("sink")
    dump = fx.dump()
    present = [s for s in want if s in sinks]
    if present and dump is not None and default:
        names = {o.get("id"): ((o.get("info") or {}).get("props") or {}).get("node.name", "")
                 for o in dump if o.get("type") == "PipeWire:Interface:Node"}
        ids = {v: k for k, v in names.items()}
        targets: dict[Any, set[str]] = {}
        for o in dump:
            if o.get("type") == "PipeWire:Interface:Link":
                i = o.get("info") or {}
                targets.setdefault(i.get("output-node-id"), set()).add(
                    names.get(i.get("input-node-id"), "?"))
        bad, unlinked = [], []
        for s in present:
            nid = ids.get(s + "_out")
            if nid is None:
                bad.append(_("{cikis} yok").format(cikis=s + "_out"))
                continue
            t = targets.get(nid, set())
            if not t:
                unlinked.append(s + "_out")
            elif t != {default}:
                bad.append(f"{s}_out → {', '.join(sorted(t))}")
        if bad:
            out.append(_r(_("çıkış bağlantıları"), HATA, "; ".join(bad),
                          f"systemctl --user restart {config.SINKS_SERVICE}"))
        elif unlinked:
            out.append(_r(_("çıkış bağlantıları"), UYARI,
                          _("bağlı değil: {cikislar}").format(cikislar=", ".join(unlinked)),
                          f"systemctl --user restart {config.SINKS_SERVICE}"))
        else:
            out.append(_r(_("çıkış bağlantıları"), TAMAM, _("hepsi varsayılan çıkışa bağlı")))
    elif present and dump is None:
        out.append(_r(_("çıkış bağlantıları"), UYARI, _("pw-dump çalışmadı, denetlenemedi")))

    dsrc = fx.default("source")
    if default.startswith(SINK_PREFIX):
        out.append(_r(_("varsayılan çıkış"), HATA,
                      _("bir oto-eq çıkışı seçilmiş: {cikis}").format(cikis=default),
                      _("Ses ayarlarından kulaklığınızı (ya da hoparlörü) varsayılan yapın")))
    elif dsrc.startswith(SINK_PREFIX) and dsrc != MIC_SOURCE:
        out.append(_r(_("varsayılan giriş"), HATA,
                      _("bir oto-eq düğümü seçilmiş: {giris}").format(giris=dsrc),
                      _("Ses ayarlarından gerçek mikrofonu ya da Kulak Kesil · Mikrofon'u seçin")))
    else:
        cikis, giris = _short(default) or _("yok"), _short(dsrc) or _("yok")
        text = (_("çıkış {cikis}, giriş {giris} (işlenmiş mikrofon)") if dsrc == MIC_SOURCE
                else _("çıkış {cikis}, giriş {giris}"))
        out.append(_r(_("varsayılanlar"), TAMAM, text.format(cikis=cikis, giris=giris)))
    return out


def _short(name: str) -> str:
    if "HS80" in name.upper():
        return "HS80"
    return name if len(name) <= 40 else name[:37] + "…"


def _headset_re(fx: Facts) -> re.Pattern:
    try:
        pat = (fx.env.rules().get("genel") or {}).get("kulaklik_desen") or "(?i)hs80"
        return re.compile(pat)
    except Exception:
        return re.compile("(?i)hs80")


def check_router(fx: Facts) -> list[dict]:
    st = fx.status()
    if st is None:
        installed = fx.unit(config.DAEMON_SERVICE).get("LoadState") == "loaded"
        return [_r(_("yönlendirici"), HATA, _("soket yanıt vermiyor"),
                   f"systemctl --user restart {config.DAEMON_SERVICE}" if installed
                   else INSTALL_HINT)]
    default = fx.default("sink") or st.get("default_sink", "")
    a = fx.aygit() if fx.default("sink") else None
    if a is not None:                   # CHANGE 7: every headphone type turns the EQ on
        should = a.kulaklik
    else:
        should = bool(default) and not default.startswith(SINK_PREFIX) \
            and bool(_headset_re(fx).search(default))
    mode = {"auto": _("otomatik"), "off": _("kapalı")}.get(st.get("mode"), "")
    if st.get("mode") == "fixed":
        mode = _("sabit ({profil})").format(profil=st.get("fixed_profile"))
    extra = []
    if st.get("dominant"):
        extra.append(_("baskın: {profil}").format(profil=st["dominant"]))
    if st.get("hs80_takip_son"):
        extra.append(_("HS80 takibi: {takip}").format(takip=_(str(st["hs80_takip_son"]))))
    out = []
    if bool(st.get("active")) != should:
        text = (_("etkin={etkin} ama varsayılan çıkış kulaklık ({cikis})") if should
                else _("etkin={etkin} ama varsayılan çıkış kulaklık değil ({cikis})"))
        out.append(_r(_("yönlendirici"), UYARI,
                      text.format(etkin=_("evet") if st.get("active") else _("hayır"),
                                  cikis=_short(default)),
                      "kulak yenile"))
    else:
        parts = [_("yanıt veriyor; mod {mod}").format(mod=mode),
                 _("etkin") if st.get("active") else _("devre dışı (kulaklık varsayılan değil)")]
        out.append(_r(_("yönlendirici"), TAMAM, ", ".join(parts + extra)))
    if not st.get("sinks_ready"):
        out.append(_r(_("yönlendirici çıkışları"), HATA, _("sinks_ready=false (EQ çıkışları eksik)"),
                      f"systemctl --user restart {config.SINKS_SERVICE}"))
    return out


def check_streams(fx: Facts) -> list[dict]:
    st = fx.status()
    if st is None:
        return []
    if not st.get("active"):
        return [_r(_("akışlar"), TAMAM,
                   _("{n} akış; yönlendirici devre dışı, ham çıkış beklenen durum").format(
                       n=len(st.get("streams") or [])))]
    default = st.get("default_sink", "")
    raw = [s for s in st.get("streams") or [] if s.get("sink") == default]
    n = len(st.get("streams") or [])
    if not raw:
        return [_r(_("akışlar"), TAMAM,
                   _("{n} akışın hiçbiri ham kulaklık çıkışında değil").format(n=n))]
    items = "; ".join(f"#{s.get('index')} {s.get('app') or s.get('binary')} "
                      f"({s.get('category')}: {s.get('reason')})" for s in raw)
    return [_r(_("akışlar"), UYARI,
               _("ham kulaklık çıkışında ({cikis}) {n} akış: {akislar}").format(
                   cikis=_short(default), n=len(raw), akislar=items),
               _("kulak yenile; sürerse: kulak gunluk"))]


def _wp_state_file() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "wireplumber" / "stream-properties"


def check_wireplumber(fx: Facts) -> list[dict]:
    text = fx.env.read_text(_wp_state_file())
    if text is None:
        return [_r(_("WirePlumber kayıtları"), TAMAM, _("kayıt dosyası yok"))]
    pinned, leftovers = [], []
    for line in text.splitlines():
        if "=" not in line or line.startswith("["):
            continue
        key, val = line.split("=", 1)
        m = re.search(r'"target"\s*:\s*"([^"]*)"', val)
        if m and m.group(1).startswith(SINK_PREFIX):
            pinned.append(key.split(":")[-1])
        if TEST_LEFTOVER.search(key):
            leftovers.append(key.split(":", 2)[-1].replace("\\s", " "))
    out = [_r(_("WP hatırlanan hedefler"), TAMAM,
              _("{n} uygulama oto-eq çıkışına sabitlenmiş").format(n=len(pinned))
              + (f" ({', '.join(pinned[:5])}{'…' if len(pinned) > 5 else ''})" if pinned else ""))]
    if leftovers:
        out.append(_r(_("WP test kalıntıları"), UYARI,
                      _("{n} test kaydı: {kayitlar}").format(
                          n=len(leftovers),
                          kayitlar=", ".join(leftovers[:4]) + ("…" if len(leftovers) > 4 else "")),
                      _("zararsız; temizlemek isterseniz WirePlumber durdurulup "
                        "~/.local/state/wireplumber/stream-properties içinden silinebilir")))
    return out


def _find(fx: Facts, dirs: tuple[str, ...], envvar: str, rel: str) -> bool:
    cands = [d for d in os.environ.get(envvar, "").split(":") if d] + list(dirs)
    return any(fx.env.exists(Path(d).expanduser() / rel) for d in cands)


def check_plugins(fx: Facts) -> list[dict]:
    out = []
    if _find(fx, LV2_DIRS, "LV2_PATH", LSP_TTL):
        out.append(_r(_("LSP eklentisi"), TAMAM, _("lsp-plugins (kompresör) var")))
    else:
        out.append(_r(_("LSP eklentisi"), UYARI, _("yok: dinamik/kompresör devre dışı kalır"),
                      _("sudo pacman -S lsp-plugins-lv2; ardından kulak eq-uret")))
    if _find(fx, LADSPA_DIRS, "LADSPA_PATH", RNNOISE_SO):
        out.append(_r(_("RNNoise eklentisi"), TAMAM, _("gürültü bastırma eklentisi var")))
    else:
        out.append(_r(_("RNNoise eklentisi"), UYARI, _("yok: mikrofon gürültü bastırması kapalı"),
                      _("sudo pacman -S noise-suppression-for-voice; ardından "
                        "python3 -m oto_eq.micgen generate")))
    return out


def check_configs(fx: Facts) -> list[dict]:
    out = []
    for ad, conf, src, regen, required in (
            (_("EQ ayar dosyası"), config.SINKS_CONF, config.PROFILES_FILE, "kulak eq-uret", True),
            (_("mikrofon ayar dosyası"), MIC_CONF, MIC_FILE,
             "python3 -m oto_eq.micgen generate && systemctl --user restart oto-eq-mic", False)):
        cm = fx.env.mtime(conf)
        if cm is None:
            out.append(_r(ad, HATA if required else UYARI,
                          _("{dosya} üretilmemiş").format(dosya=conf.name),
                          INSTALL_HINT if required else regen))
            continue
        sm = fx.env.mtime(fx.env.config_path(src))
        if sm is not None and sm > cm + 1:
            out.append(_r(ad, UYARI,
                          _("{kaynak} {dosya} dosyasından daha yeni (eski ayar çalışıyor)").format(
                              kaynak=src, dosya=conf.name),
                          regen))
        else:
            out.append(_r(ad, TAMAM, _("{dosya} güncel").format(dosya=conf.name)))
    return out


def _cpu_ticks(fx: Facts, pid: int) -> int | None:
    text = fx.env.read_text(f"/proc/{pid}/stat")
    if not text or ")" not in text:
        return None
    f = text.rsplit(")", 1)[1].split()
    try:
        return int(f[11]) + int(f[12])     # utime, stime (fields 14, 15)
    except (IndexError, ValueError):
        return None


def check_cpu(fx: Facts, interval: float = 1.0) -> list[dict]:
    pids = {}
    for u in (config.SINKS_SERVICE, "oto-eq-mic.service"):
        info = fx.unit(u)
        pid = info.get("MainPID", "0")
        if info.get("ActiveState") == "active" and pid.isdigit() and int(pid) > 0:
            pids[u.removesuffix(".service")] = int(pid)
    if not pids:
        return [_r(_("işlemci kullanımı"), UYARI, _("ölçülemedi: EQ/mikrofon süreçleri çalışmıyor"))]
    t0 = fx.env.monotonic()
    a = {n: _cpu_ticks(fx, p) for n, p in pids.items()}
    fx.env.sleep(interval)
    dt = max(fx.env.monotonic() - t0, 1e-3)
    parts, high = [], []
    for n, p in pids.items():
        b = _cpu_ticks(fx, p)
        if a[n] is None or b is None:
            parts.append(f"{n}: ?")
            continue
        pct = (b - a[n]) / fx.env.clk_tck / dt * 100.0
        parts.append(f"{n}: {tx.percent_text(pct, 1)}")      # "%5,0" / "5.0%"
        if pct > CPU_WARN:
            high.append(n)
    if high:
        return [_r(_("işlemci kullanımı"), UYARI, ", ".join(parts),
                   _("yüksek: profil filtre sayısını/kompresörü azaltın"))]
    return [_r(_("işlemci kullanımı"), TAMAM, ", ".join(parts))]


def check_xruns(fx: Facts) -> list[dict]:
    rc, out, err = fx.env.run(["pw-top", "-b", "-n", "2"], timeout=8.0)
    if rc != 0 or not out.strip():
        return [_r(_("xrun (ses kesintisi)"), UYARI,
                   _("pw-top çalışmadı ({hata})").format(hata=err.strip()[:60]))]
    blocks, cur = [], []
    for line in out.splitlines():
        if line.startswith("S ") and "ERR" in line:
            if cur:
                blocks.append(cur)
            cur = []
        elif line.strip():
            cur.append(line)
    if cur:
        blocks.append(cur)
    def errs_of(block: list[str]) -> dict[str, int]:
        out_: dict[str, int] = {}
        for line in block:
            f = line.split()
            if len(f) < 10:
                continue
            try:
                out_[_short(f[-1].lstrip("+"))] = int(f[8])
            except ValueError:
                continue
        return out_
    last = errs_of(blocks[-1]) if blocks else {}
    if len(blocks) >= 2:
        # ERR is a CUMULATIVE counter (onarim compares two readings too): one old xrun
        # would warn until the node is recreated. Warn only for a rise during the measurement.
        first = errs_of(blocks[0])
        rising = [f"{n} (+{e - first.get(n, 0)})" for n, e in last.items() if e > first.get(n, e)]
        if rising:
            return [_r(_("xrun (ses kesintisi)"), UYARI,
                       _("ölçüm sırasında artan xrun: {dugumler}").format(
                           dugumler=", ".join(rising[:6])),
                       _("kulak gunluk / pw-top ile inceleyin"))]
        old = [f"{n} ({e})" for n, e in last.items() if e > 0]
        if old:
            return [_r(_("xrun (ses kesintisi)"), TAMAM,
                       _("şu an xrun yok (eski sayaç: {dugumler})").format(
                           dugumler=", ".join(old[:6])))]
        return [_r(_("xrun (ses kesintisi)"), TAMAM, _("hiçbir düğümde xrun yok"))]
    bad = [f"{n} ({e})" for n, e in last.items() if e > 0]
    if bad:
        return [_r(_("xrun (ses kesintisi)"), UYARI,
                   _("ERR sayacı > 0: {dugumler}").format(dugumler=", ".join(bad[:6])),
                   _("tekrar ölçün; artıyorsa kulak gunluk / pw-top ile inceleyin"))]
    return [_r(_("xrun (ses kesintisi)"), TAMAM, _("hiçbir düğümde xrun yok"))]


def check_hs80(fx: Facts) -> list[dict]:
    base = "/sys/bus/usb/devices"
    found = []
    for d in fx.env.listdir(base):
        v = (fx.env.read_text(f"{base}/{d}/idVendor") or "").strip().lower()
        p = (fx.env.read_text(f"{base}/{d}/idProduct") or "").strip().lower()
        if v == HS80_VENDOR and p in HS80_PRODUCTS:
            found.append(_(HS80_PRODUCTS[p]))
    if found:
        return [_r("HS80", TAMAM, _("takılı: {baglanti}").format(baglanti=", ".join(sorted(set(found)))))]
    a = fx.aygit()
    if a is not None and a.kulaklik and "hs80" not in f"{a.ad} {a.aciklama}".lower():
        # another headphone in use: the EQ works without the HS80
        return [_r("HS80", TAMAM, _(HS80_BASKA).format(tur=_(a.etiket), model=a.aciklama or a.ad))]
    if a is not None and a.ad and not a.kulaklik:
        # speakers / HDMI chosen as the default output: nothing is broken, EQ is idle by design
        return [_r("HS80", UYARI, _("takılı değil; varsayılan çıkış {tur} ({model}), EQ bu "
                                    "çıkışta çalışmaz").format(tur=_(a.etiket),
                                                               model=a.aciklama or a.ad),
                   _("HS80 ile dinlemek için kablosuz alıcıyı ya da USB kablosunu takın"))]
    return [_r("HS80", HATA, _("USB'de bulunamadı (1b1c:0a6b / 0a69)"),
               _("kablosuz alıcıyı ya da USB kablosunu takın"))]


def check_kulaklik(fx: Facts) -> list[dict]:
    """CHANGE 7: what the default output is (USB / Bluetooth / 3,5 mm headphone or not)."""
    if fx.sinks() is None:
        return []                        # check_sinks reports the missing sound server
    default = fx.default("sink")
    if default.startswith(SINK_PREFIX):
        return []                        # check_sinks reports an oto-eq sink as default
    a = fx.aygit()
    if a is None:
        return [_r(_("kulaklık"), UYARI, _(KULAKLIK_YOK), _(KULAKLIK_YOK_ONERI))]
    model, tur = a.aciklama or a.ad, _(a.etiket)
    if not a.kulaklik:
        return [_r(_("kulaklık"), UYARI,
                   _(KULAKLIK_DEGIL).format(tur=tur, model=model, neden=_(a.neden)),
                   _(KULAKLIK_DEGIL_ONERI))]
    if tx.bt_arama_modu({"bt_profil": a.bt_profil}):
        return [_r(_("kulaklık"), UYARI, _(KULAKLIK_ARAMA).format(tur=tur, model=model,
                                                                  profil=a.bt_profil),
                   _(KULAKLIK_ARAMA_ONERI))]
    duzeltme = _(tx.DUZELTME_ACIK if a.duzeltme else tx.DUZELTME_NOTR)
    return [_r(_("kulaklık"), TAMAM, KULAKLIK_TAMAM.format(tur=tur, model=model, duzeltme=duzeltme))]


# --------------------------------------------------------------------------
# CHANGE 6 parts: kişisel ton, ses analizi/model, conf structure, kısma, limiter,
# otomatik onarım. Every module is optional (guarded): absent → no line.

KISISEL_FILE = "kisisel.json"
KISMA_FILE = "kisma.json"
MODEL_FILES = ("discogs-effnet-bsdynamic-1.onnx", "discogs-effnet-bsdynamic-1.json")
YAMNET_FILE = "audioset-yamnet-1.onnx"
LIMITER_TTL = "lsp-plugins.lv2/limiter_stereo.ttl"
LIMITER_RE = re.compile(r'plugin\s*=\s*"[^"]*limiter_stereo"')
ONARIM_FILE = config.STATE_DIR / "onarim.json"
TON_ADLARI = {"bas": N_("bas"), "vokal": N_("vokal"), "parlaklik": N_("parlaklık")}  # _() at use


def _json_file(fx: Facts, path: Path) -> tuple[str, Any]:
    """("yok"|"bozuk"|"tamam", value-or-error)."""
    text = fx.env.read_text(path)
    if text is None:
        return "yok", None
    try:
        return "tamam", json.loads(text)
    except ValueError as e:
        return "bozuk", e


def check_kisisel(fx: Facts) -> list[dict]:
    durum, val = _json_file(fx, config.CONFIG_DIR / KISISEL_FILE)
    if durum == "yok":
        return [_r(_("kişisel ton"), TAMAM, _("ayar yok (her bant 0 dB)"))]
    if durum == "bozuk" or not isinstance(val, dict):
        why = _("JSON hatası: {hata}").format(hata=val) if durum == "bozuk" else _("JSON nesnesi değil")
        return [_r(_("kişisel ton"), UYARI,
                   _("kisisel.json okunamıyor ({neden}); ton 0 dB sayılıyor").format(neden=why),
                   _("kulak ton sifirla --hepsi (ya da yedekten: kulak yedek yukle DOSYA)"))]
    genel = val.get("genel") if isinstance(val.get("genel"), dict) else {}
    tur = val.get("tur") if isinstance(val.get("tur"), dict) else {}
    parts = []
    for k, ad in TON_ADLARI.items():
        v = genel.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v:
            parts.append(_("{bant} {db:+g} dB").format(bant=_(ad), db=v))
    bad = [k for k in genel if k not in TON_ADLARI]
    text = (_("genel: {bantlar}").format(bantlar=", ".join(parts)) if parts
            else _("genel: düz"))
    if tur:
        text = _("{ozet}; {n} türe özel ayar").format(ozet=text, n=len(tur))
    if bad:
        return [_r(_("kişisel ton"), UYARI,
                   _("{ozet}; bilinmeyen anahtar: {anahtarlar} (yok sayılıyor)").format(
                       ozet=text, anahtarlar=", ".join(bad)),
                   _("kulak ton ile yeniden ayarlayın"))]
    return [_r(_("kişisel ton"), TAMAM, text)]


def _model_dir(fx: Facts) -> Path:
    d = fx.env.attr("oto_eq.ses_model", "MODEL_DIR")
    if d:
        return Path(d)
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "oto-eq" / "models"


def check_ses_analiz(fx: Facts) -> list[dict]:
    if not fx.env.module_available("oto_eq.ses_model"):
        return []
    mdir = _model_dir(fx)
    ort = fx.env.module_available("onnxruntime")
    have = [fx.env.exists(mdir / f) for f in MODEL_FILES]
    yam = fx.env.exists(mdir / YAMNET_FILE)
    if not ort:
        return [_r(_("ses analizi modeli"), UYARI,
                   _("onnxruntime yok: tür sesten tahmin edilmiyor (ad/başlık tabanlı devam)"),
                   _("sudo pacman -S python-onnxruntime-cpu; ardından systemctl --user restart "
                     "oto-eq"))]
    if not all(have):
        return [_r(_("ses analizi modeli"), UYARI,
                   _("model dosyası yok ({yol}): tür sesten tahmin edilmiyor").format(
                       yol=mdir / MODEL_FILES[0]),
                   _("model dosyalarını docs/MODELS.md'deki gibi indirin"))]
    return [_r(_("ses analizi modeli"), TAMAM,
               _("EffNet + YAMNet (konuşma ayrımı)") if yam
               else _("EffNet (YAMNet yok: konuşma ayrımı zayıf)"))]


def check_conf_yapi(fx: Facts) -> list[dict]:
    old = fx.env.conf_outdated()
    if old is None:
        return []
    if old:
        return [_r(_("EQ yapısı"), UYARI,
                   _("çalışan EQ ayarı eski yapıda (yeni bantlar ya da sınırlayıcı yok)"),
                   "kulak eq-uret")]
    return [_r(_("EQ yapısı"), TAMAM, _("kişisel ton bantları ayarda var"))]


# --------------------------------------------------------------------------
# running graph ↔ generated conf (1 Kas 2026). A regenerated conf only takes effect
# when its service restarts (27 Eki: state.restore-props was in the file but not in
# the running graph → −6 dB stuck, unnoticed). Control VALUES are applied live by
# eqgen/micgen, so only the STRUCTURE (graph nodes) and node properties are compared.

_FC_MARK = "libpipewire-module-filter-chain"
_GRAPH_NODE_RE = re.compile(r"\{\s*type\s*=\s*\w+\s+name\s*=\s*([\w.\-]+)([^\n]*)")
_PROPS_BLOCK_RE = re.compile(r"\b(capture|playback)\.props\s*=\s*\{(.*?)\}", re.S)
_PROP_RE = re.compile(r"^[ \t]*([\w.\-]+)[ \t]*=[ \t]*(.*?)[ \t]*$", re.M)
_CONTROL_RE = re.compile(r"\bcontrol\s*=\s*\{")
GRAF_OZELLIKLERI = ("media.class", "node.passive", "priority.session", "priority.driver",
                    "state.restore-props", "state.restore-target", "oto_eq.profile")


def conf_graflari(text: str) -> dict[str, dict]:
    """Filter chains of a generated conf, keyed by the capture node.name:
    ``{"dugumler": all graph node names, "kontrollu": nodes with control ports,
    "props": {node.name: {key: value}}}`` (only GRAF_OZELLIKLERI keys)."""
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    out: dict[str, dict] = {}
    for blok in text.split(_FC_MARK)[1:]:
        cap, props = None, {}
        for kind, body in _PROPS_BLOCK_RE.findall(blok):
            d = {k: v.strip().strip('"') for k, v in _PROP_RE.findall(body)}
            name = d.get("node.name")
            if not name:
                continue
            props[name] = {k: d[k] for k in GRAF_OZELLIKLERI if k in d}
            if kind == "capture":
                cap = name
        if cap is None:
            continue
        dugumler, kontrollu = set(), set()
        for name, rest in _GRAPH_NODE_RE.findall(blok):
            dugumler.add(name)
            if _CONTROL_RE.search(rest):
                kontrollu.add(name)
        out[cap] = {"dugumler": dugumler, "kontrollu": kontrollu, "props": props}
    return out


def calisan_graflar(dump: list) -> dict[str, dict]:
    """node.name → {"props": node props, "kontrol": graph node names that expose
    filter-graph controls ("<node>:<port>" keys of the Props params)}."""
    out: dict[str, dict] = {}
    for o in dump:
        if not isinstance(o, dict) or o.get("type") != "PipeWire:Interface:Node":
            continue
        info = o.get("info") or {}
        props = info.get("props") or {}
        name = props.get("node.name")
        if not name:
            continue
        kontrol = set()
        for prm in (info.get("params") or {}).get("Props", []) or []:
            p = prm.get("params") if isinstance(prm, dict) else None
            if isinstance(p, list):
                kontrol |= {k.split(":", 1)[0] for k in p[0::2] if isinstance(k, str) and ":" in k}
        out[name] = {"props": props, "kontrol": kontrol}
    return out


def _deger(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v).strip().strip('"').lower()


def graf_farklari(conf: dict[str, dict], live: dict[str, dict]) -> dict[str, list[str]]:
    """Per running chain (capture node name) the structural differences between the
    conf and the live graph; chains not running at all are skipped (check_sinks and the
    mic checks report those). Empty dict = the running graph matches the file."""
    out: dict[str, list[str]] = {}
    for cap, g in conf.items():
        if cap not in live:
            continue
        kontrol = set().union(*(live[n]["kontrol"] for n in g["props"] if n in live))
        fark = []
        eksik = sorted(g["kontrollu"] - kontrol)
        fazla = sorted(kontrol - g["dugumler"])
        if eksik:
            fark.append(_("eksik düğüm: {adlar}").format(adlar=", ".join(eksik[:6])
                                                          + (" …" if len(eksik) > 6 else "")))
        if fazla:
            fark.append(_("fazla düğüm: {adlar}").format(adlar=", ".join(fazla[:6])
                                                          + (" …" if len(fazla) > 6 else "")))
        for node, want in g["props"].items():
            if node not in live:
                fark.append(_("{dugum} çalışmıyor").format(dugum=node))
                continue
            got = live[node]["props"]
            for k, v in want.items():
                if _deger(got.get(k)) != _deger(v):
                    fark.append(f"{k}={_deger(got.get(k)) or '—'} ({v})")
        if fark:
            out[cap] = fark
    return out


def check_calisan_graf(fx: Facts) -> list[dict]:
    out = []
    for ad, conf, unit in ((_("çalışan EQ grafiği"), config.SINKS_CONF, config.SINKS_SERVICE),
                           (_("çalışan mikrofon grafiği"), MIC_CONF, "oto-eq-mic.service")):
        if fx.unit(unit).get("ActiveState") != "active":
            continue                                     # check_units reports it
        text = fx.env.read_text(conf)
        graflar = conf_graflari(text) if text else {}
        if not graflar:
            continue                                     # check_configs reports it
        dump = fx.dump()
        if dump is None:
            return out
        live = calisan_graflar(dump)
        n = sum(1 for c in graflar if c in live)
        if not n:
            continue
        fark = graf_farklari(graflar, live)
        if not fark:
            out.append(_r(ad, TAMAM, _("ayar dosyasıyla aynı yapıda ({n} zincir)").format(n=n)))
            continue
        ozet = "; ".join(f"{c.removeprefix(SINK_PREFIX)}: {', '.join(f[:3])}"
                         + (" …" if len(f) > 3 else "") for c, f in list(fark.items())[:3])
        if len(fark) > 3:
            ozet += " …"
        start, cm = fx.unit_start(unit), fx.env.mtime(conf)
        if start is not None and cm is not None and cm > start + 1:
            out.append(_r(ad, UYARI, _("ayar dosyası servis başladıktan sonra değişti, eski "
                                       "ayar çalışıyor ({fark})").format(fark=ozet),
                          f"systemctl --user restart {unit}"))
        else:
            out.append(_r(ad, UYARI, _("çalışan grafik ayar dosyasından farklı ({fark})").format(
                fark=ozet), _("{komut}; günlük: journalctl --user -u {birim} -n 50").format(
                    komut=f"systemctl --user restart {unit}", birim=unit)))
    return out


def check_kisma(fx: Facts) -> list[dict]:
    if not fx.env.module_available("oto_eq.kisma"):
        return []
    durum, val = _json_file(fx, config.CONFIG_DIR / KISMA_FILE)
    if durum == "bozuk" or (durum == "tamam" and not isinstance(val, dict)):
        return [_r(_("sohbette kısma"), UYARI, _("kisma.json okunamıyor; varsayılan ayarlar kullanılıyor"),
                   _("kulak kisma durum; dosyayı düzeltin ya da silin"))]
    norm = fx.env.attr("oto_eq.kisma", "normalize_settings")
    ayar = val if isinstance(val, dict) else {}
    if callable(norm):
        try:
            ayar = norm(ayar)
        except Exception:  # noqa: BLE001
            pass
    acik = ayar.get("acik", True)
    if not acik:
        return [_r(_("sohbette kısma"), TAMAM, _("kapalı"))]
    db = ayar.get("db")
    prof = ayar.get("profiller")
    text = (_("açık, −{db:g} dB").format(db=float(db)) if isinstance(db, (int, float))
            else _("açık"))
    if isinstance(prof, list) and prof:
        text += f" ({', '.join(str(p) for p in prof)})"
    if durum == "yok":
        text = _("{ozet}; varsayılan ayarlar").format(ozet=text)
    # the setting says "on", but is the router's ducker actually running? (it is absent when
    # Ducker() or its start-up restore failed, or with OTO_EQ_KISMA=0)
    st = fx.status()
    if isinstance(st, dict):
        k = st.get("kisma")
        if not isinstance(k, dict):
            return [_r(_("sohbette kısma"), UYARI,
                       _("ayar açık ama yönlendiricide kısma çalışmıyor"),
                       _("journalctl --user -u oto-eq -n 50; systemctl --user restart oto-eq"))]
        if k.get("hata"):
            return [_r(_("sohbette kısma"), UYARI,
                       _("yönlendiricide kısma hata veriyor: {hata}").format(hata=k["hata"]),
                       _("journalctl --user -u oto-eq -n 50"))]
    return [_r(_("sohbette kısma"), TAMAM, text)]


def check_limiter(fx: Facts) -> list[dict]:
    conf = fx.env.read_text(config.SINKS_CONF)
    if conf is None:
        return []                       # check_configs reports the missing conf
    n = len(LIMITER_RE.findall(conf))
    if not n:
        supports = fx.env.attr("oto_eq.eqgen", "LSP_LIMITER_URI")
        if supports:
            return [_r(_("limiter"), UYARI, _("EQ ayarında limiter yok (eski ayar çalışıyor olabilir)"),
                       "kulak eq-uret")]
        return []
    if not _find(fx, LV2_DIRS, "LV2_PATH", LIMITER_TTL):
        return [_r(_("limiter"), HATA,
                   _("EQ ayarı {n} limiter istiyor ama LSP limiter eklentisi yok").format(n=n),
                   _("sudo pacman -S lsp-plugins-lv2; ardından kulak eq-uret"))]
    return [_r(_("limiter"), TAMAM, _("{n} zincirde tepe sınırlayıcı var").format(n=n))]


def check_onarim(fx: Facts) -> list[dict]:
    durum, val = _json_file(fx, ONARIM_FILE)
    son = val.get("son") if durum == "tamam" and isinstance(val, dict) else None
    if not isinstance(son, list) or not son:
        return []
    now = fx.env.time()
    yakin = [x for x in son if isinstance(x, dict) and isinstance(x.get("zaman"), (int, float))
             and now - x["zaman"] <= 86400 and not x.get("deneme")]
    if not yakin:
        return [_r(_("otomatik onarım"), TAMAM, _("son 24 saatte gerekmedi"))]
    last = yakin[-1]
    dk = int((now - last["zaman"]) // 60)
    ne = _("{n} eylem; son: {neden} ({dk} dk önce)").format(
        n=len(yakin), neden=_(str(last.get("neden", "?"))), dk=dk)
    restarts = sum(1 for x in yakin if x.get("tur") == "servis_yeniden_baslat")
    # onarim.json "sonuc" is written by onarim.py in Turkish (log text, never translated);
    # an explicit "basarili": false flag wins if a later onarim.py writes one
    failed = [x for x in yakin if x.get("basarili") is False or any(
        w in str(x.get("sonuc", "")) for w in ("başarısız", "yapılamadı", "başlatılamadı"))]
    if failed or restarts >= 3:
        ne = (_("{ozet}; {n} başarısız").format(ozet=ne, n=len(failed)) if failed else
              _("{ozet}; {n} kez EQ çıkışları yeniden başlatıldı").format(ozet=ne, n=restarts))
        return [_r(_("otomatik onarım"), UYARI, ne, _("kulak gunluk ile inceleyin"))]
    return [_r(_("otomatik onarım"), TAMAM, ne)]


CHECKS: tuple[Callable[[Facts], list[dict]], ...] = (
    check_hs80, check_kulaklik, check_units, check_sinks, check_router, check_streams, check_configs,
    check_conf_yapi, check_calisan_graf, check_plugins, check_limiter, check_kisisel, check_ses_analiz, check_kisma,
    check_wireplumber, check_onarim, check_cpu, check_xruns,
)


def run_checks(env: Env | None = None) -> list[dict]:
    """Run every check; a crashing check becomes one 'hata' item."""
    fx = Facts(env or Env())
    out: list[dict] = []
    for chk in CHECKS:
        try:
            out.extend(chk(fx))
        except Exception as e:  # never let one check kill the report
            out.append(_r(chk.__name__.removeprefix("check_"), HATA,
                          _("kontrol çalışamadı: {tur}: {hata}").format(tur=type(e).__name__, hata=e)))
    return out


# --------------------------------------------------------------------------
# output

ICONS = {TAMAM: "✔", UYARI: "⚠", HATA: "✘"}
COLORS = {TAMAM: "\033[32m", UYARI: "\033[33m", HATA: "\033[31m"}


text_width = i18n.genislik               # terminal cells: CJK counts double
_pad = i18n.sola


def format_report(results: list[dict], color: bool = False) -> str:
    w = max((text_width(r["ad"]) for r in results), default=10)
    lines = [_("Kulak Kesil — sağlık kontrolü"), ""]
    for r in results:
        icon = ICONS.get(r["durum"], "?")
        if color:
            icon = f"{COLORS.get(r['durum'], '')}{icon}\033[0m"
        lines.append(f"{icon} {_pad(r['ad'], w)}  {r['ayrinti']}")
        if r["durum"] != TAMAM and r.get("oneri"):
            lines.append(f"  {'':<{w}}  → {r['oneri']}")
    n = {k: sum(r["durum"] == k for r in results) for k in (TAMAM, UYARI, HATA)}
    lines.append("")
    summary = _("Özet: {tamam} tamam, {uyari} uyarı, {hata} hata").format(
        tamam=n[TAMAM], uyari=n[UYARI], hata=n[HATA])
    if any(r["oneri"] == INSTALL_HINT for r in results):
        summary = _("{ozet} — kurulum yapılmamış görünüyor: {komut}").format(ozet=summary,
                                                                          komut=INSTALL_HINT)
    elif not n[HATA] and not n[UYARI]:
        summary = _("{ozet} — her şey yolunda").format(ozet=summary)
    lines.append(summary)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if any(a in ("-h", "--help") for a in argv):
        print(_("kullanım: python3 -m oto_eq.saglik [--json]   (salt okunur sağlık kontrolü)"))
        return 0
    results = run_checks()
    if "--json" in argv:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(format_report(results, color=color))
    return 1 if any(r["durum"] == HATA for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
