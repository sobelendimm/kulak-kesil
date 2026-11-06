"""Çıkış aygıtı türü: varsayılan çıkış bir kulaklık mı? (CONTRACT CHANGE 7)

Before CHANGE 7 the EQ was gated on one regex (``kulaklik_desen`` = HS80). Now
every headphone-type output turns it on:

    usb         USB kulaklık / kulaklık seti   (name, form factor or port type)
    bluetooth   Bluetooth kulaklık             (device.form_factor headset/headphone/…)
    jak         Kablolu kulaklık (3,5 mm)      (onboard card, active port Headphones)

and these never do: hoparlor (line-out / speaker port, USB speaker), bt_hoparlor,
hdmi (monitor / TV / S/PDIF), sanal (oto_eq_* and other software sinks), bilinmiyor.

kurallar.toml [genel]: ``kulaklik_desen`` FORCES a match (as before),
``kulaklik_haric`` excludes, ``kulaklik_otomatik = false`` restores the old
"pattern only" rule.

The [duzeltme] curve of profiller.toml belongs to ONE model (``cihaz_desen``,
default HS80): ``Aygit.duzeltme`` says whether it fits. The router writes the
active device to ``STATE_DIR/cihaz.json``; eqgen reads ``duzeltme_acik()`` and
flattens the correction on every other headphone (all processes agree, nothing
is written to profiller.toml).

Input: ``Sink.info`` = the raw ``pactl -f json list sinks`` item (C locale), so
classifying costs no extra process.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config
from .i18n import N_, _
from .models import SINK_PREFIX

TUR_ETIKET = {
    "usb": N_("USB kulaklık"),
    "bluetooth": N_("Bluetooth kulaklık"),
    "jak": N_("Kablolu kulaklık (3,5 mm)"),
    "desen": N_("Kulaklık (kulaklik_desen)"),
    "hoparlor": N_("Hoparlör"),
    "bt_hoparlor": N_("Bluetooth hoparlör"),
    "hdmi": N_("Monitör / TV (HDMI)"),
    "sanal": N_("Sanal çıkış"),
    "bilinmiyor": N_("Bilinmeyen çıkış"),
}
KULAKLIK_TURLERI = frozenset({"usb", "bluetooth", "jak", "desen"})
DEFAULT_DUZELTME_DESEN = "(?i)hs80"
STATE_FILE = "cihaz.json"

_HP_FORMS = frozenset({"headset", "headphone", "hands-free", "handset"})
_SPK_FORMS = frozenset({"speaker", "portable", "car", "hifi", "tv", "computer", "internal",
                        "set-top-box", "video-monitor"})
_HP_PORTS = frozenset({"headphones", "headset", "handset", "earpiece"})
_SPK_PORTS = frozenset({"speaker", "line"})
_DIGITAL_PORTS = frozenset({"hdmi", "spdif", "displayport"})
# (?<![a-z0-9]) / (?![a-z0-9]) instead of \b: node names glue words with "_" ("…CORSAIR_HS80_RGB…")
# words that say "worn on / in the ears" (checked before any speaker hint)
_HP_WORDS = re.compile(
    r"head(?:set|phones?)|kulakl[ıi]k|ear(?:buds?|phones?)|airpods|in-ear"
    r"|(?<![a-z0-9])(?:buds\d*|pods|iem)(?![a-z])", re.I)
# headphone-only model families / brands (checked AFTER speaker words: "… Soundbar" stays a speaker)
_HP_MODELS = re.compile(
    r"arctis|kraken|blackshark|barracuda|virtuoso|wh-1000|wf-1000|quietcomfort|freebuds"
    r"|hyperx|turtle\s?beach|sennheiser|beyerdynamic|audeze|jabra|plantronics"
    r"|(?<![a-z0-9])(?:nari|void|epos|fiio|gsx|astro[\s_-]?a\d0|hs\d{2,3})(?![a-z0-9])", re.I)
_SPK_WORDS = re.compile(r"speaker|hoparl[öo]r|soundbar|lautsprecher|enceinte|altavoz", re.I)
_DESC_SUFFIX = re.compile(
    r"\s+(?:Analog|Digital|Pro)\s+(?:Stereo|Mono|Surround\s+\S+)(?:\s+\(.*\))?(?:\s+\[.*\])?$"
    r"|\s+\((?:HDMI|S/PDIF)\)(?:\s+\[.*\])?$", re.I)


@dataclass(frozen=True)
class Aygit:
    ad: str             # sink node name ("" = no default sink)
    aciklama: str       # display name (model)
    tur: str            # key of TUR_ETIKET
    kulaklik: bool      # Kulak Kesil's EQ runs on this output
    duzeltme: bool      # the [duzeltme] curve fits this model (and it is a headphone)
    neden: str          # why (Turkish, short)
    baglanti: str = ""  # device.bus: usb / bluetooth / pci / …
    bt_profil: str = ""  # api.bluez5.profile: a2dp-sink / headset-head-unit / …

    @property
    def etiket(self) -> str:
        return _(TUR_ETIKET.get(self.tur, TUR_ETIKET["bilinmiyor"]))   # current language

    def durum(self) -> dict:
        """router status["kulaklik"]."""
        d = asdict(self)
        d["etiket"] = self.etiket
        return d


@dataclass(frozen=True)
class Ayar:
    otomatik: bool = True
    desen: re.Pattern | None = None       # always a headphone (kulaklik_desen)
    haric: re.Pattern | None = None       # never a headphone (kulaklik_haric)
    duzeltme: re.Pattern | None = re.compile(DEFAULT_DUZELTME_DESEN)


def ayar_oku(genel: dict | None, desen: str = "", duzeltme_desen: str | None = None
             ) -> tuple[Ayar, str | None]:
    """Ayar from kurallar.toml [genel] (+ the correction pattern of profiller.toml).

    ``desen`` = the already validated kulaklik_desen. Returns (ayar, error text or None);
    an invalid pattern is reported and ignored."""
    g = genel if isinstance(genel, dict) else {}
    err = None
    oto = g.get("kulaklik_otomatik", True)

    def rx(text, key):
        nonlocal err
        if not isinstance(text, str) or not text:
            return None
        try:
            return re.compile(text)
        except re.error as e:
            err = _("{anahtar} geçersiz ({desen}): {hata}").format(anahtar=key, desen=repr(text), hata=e)
            return None

    corr_text = DEFAULT_DUZELTME_DESEN if duzeltme_desen is None else duzeltme_desen
    corr = rx(corr_text, "duzeltme.cihaz_desen")
    if corr is None and corr_text:              # invalid → keep the HS80 default, not "nowhere"
        corr = re.compile(DEFAULT_DUZELTME_DESEN)
    return Ayar(otomatik=oto if isinstance(oto, bool) else True,
                desen=rx(desen, "kulaklik_desen"),
                haric=rx(g.get("kulaklik_haric", ""), "kulaklik_haric"),
                duzeltme=corr), err


def _active_port(info: dict) -> tuple[str, str]:
    """(name, type) of the active port, lower case; type guessed from the name if absent."""
    name = str(info.get("active_port") or "").lower()
    typ = ""
    for p in info.get("ports") or ():
        if isinstance(p, dict) and str(p.get("name") or "").lower() == name:
            typ = str(p.get("type") or "").lower()
            break
    if not typ or typ == "unknown":
        typ = ("headphones" if "headphone" in name else "headset" if "headset" in name
               else "hdmi" if "hdmi" in name else "spdif" if "iec958" in name or "spdif" in name
               else "line" if "lineout" in name or "line-out" in name
               else "speaker" if "speaker" in name else "")
    return name, typ


def _model(desc: str, props: dict) -> str:
    for k in ("device.description", "device.product.name"):
        v = str(props.get(k) or "").strip()
        if v:
            return v
    return _DESC_SUFFIX.sub("", desc).strip() or desc


def _otomatik(name: str, info: dict, props: dict, bus: str, text: str) -> tuple[str, str]:
    """(tur, neden) from the device facts alone (no user patterns)."""
    form = str(props.get("device.form_factor") or "").lower()
    icon = str(props.get("device.icon_name") or "").lower()
    api = str(props.get("device.api") or "").lower()
    profile = str(props.get("device.profile.name") or "").lower()
    pname, ptype = _active_port(info)
    bt = bus == "bluetooth" or api == "bluez5" or name.startswith("bluez_output.")
    if info and not bt and not api and not bus and not info.get("ports"):
        return "sanal", _("yazılım (sanal) çıkışı")
    digital = ptype in _DIGITAL_PORTS or "hdmi" in profile or "iec958" in profile \
        or (not info and "hdmi" in name.lower())
    if bt:
        # ear words before the speaker form: earbuds may report "portable"
        if form in _HP_FORMS or "headset" in icon or "headphone" in icon:
            return "bluetooth", _("Bluetooth aygıt biçimi: {bicim}").format(bicim=form or icon)
        if _HP_WORDS.search(text):
            return "bluetooth", _("Bluetooth, adından kulaklık")
        if form in _SPK_FORMS or "speaker" in icon or _SPK_WORDS.search(text):
            return "bt_hoparlor", (_("Bluetooth hoparlör ({bicim})").format(bicim=form) if form
                                   else _("Bluetooth hoparlör (adından)"))
        if _HP_MODELS.search(text):
            return "bluetooth", _("Bluetooth, modelinden kulaklık")
        return "bluetooth", _("Bluetooth ses aygıtı (biçim bildirmiyor, kulaklık sayıldı)")
    usb = bus == "usb" or name.startswith("alsa_output.usb-")
    if form in _HP_FORMS:
        return ("usb" if usb else "jak"), _("aygıt biçimi: {bicim}").format(bicim=form)
    if usb:
        # a USB headset may sit on its S/PDIF profile: names win over the digital check
        if _HP_WORDS.search(text):
            return "usb", _("USB, adından kulaklık")
        if form in _SPK_FORMS or _SPK_WORDS.search(text):
            return "hoparlor", _("USB hoparlör")
        if _HP_MODELS.search(text):
            return "usb", _("USB, modelinden kulaklık")
        if ptype in _HP_PORTS:
            return "usb", _("USB, port türü kulaklık")
        if digital:
            return "hdmi", _("dijital çıkış (USB, S/PDIF)")
        return "bilinmiyor", _("USB ses aygıtı; kulaklık olduğu anlaşılamadı")
    if digital:
        return "hdmi", _("HDMI / dijital çıkış")
    if ptype in _HP_PORTS:
        return "jak", _("kulaklık girişi (3,5 mm) etkin")
    if ptype in _SPK_PORTS or form in _SPK_FORMS:
        return "hoparlor", (_("hat çıkışı portu etkin") if ptype == "line"
                            else _("hoparlör portu etkin"))
    if not info and (_HP_WORDS.search(text) or _HP_MODELS.search(text)):
        return "jak", _("adından kulaklık")
    return "bilinmiyor", _("türü anlaşılamadı")


def siniflandir(name: str, description: str = "", info: dict | None = None,
                ayar: Ayar | None = None) -> Aygit:
    """Classify the output ``name`` (+ its raw pactl JSON ``info``). Never raises."""
    ayar = ayar or Ayar()
    info = info if isinstance(info, dict) else {}
    props = info.get("properties") if isinstance(info.get("properties"), dict) else {}
    desc = description or str(info.get("description") or "")
    bus = str(props.get("device.bus") or "").lower()
    if not bus and name.startswith("bluez_output."):
        bus = "bluetooth"
    text = " ".join(filter(None, (name, desc, str(props.get("device.product.name") or ""),
                                  str(props.get("device.description") or ""))))
    fits = bool(ayar.duzeltme and ayar.duzeltme.search(text))

    def mk(tur: str, hp: bool, neden: str) -> Aygit:
        return Aygit(name, _model(desc, props) if name else "", tur, hp, fits and hp, neden, bus,
                     str(props.get("api.bluez5.profile") or ""))

    if not name:
        return mk("bilinmiyor", False, _("varsayılan çıkış yok"))
    if name.startswith(SINK_PREFIX):
        return mk("sanal", False, _("Kulak Kesil'in kendi çıkışı"))
    try:
        tur, neden = _otomatik(name, info, props, bus, text)
    except Exception as e:  # noqa: BLE001 - odd pactl data must not stop the router
        tur, neden = "bilinmiyor", _("aygıt bilgisi okunamadı: {hata}").format(hata=e)
    if ayar.haric and ayar.haric.search(text):
        return mk(tur, False, _("kulaklik_haric desenine uyuyor"))
    if ayar.otomatik and tur in KULAKLIK_TURLERI:
        return mk(tur, True, neden)
    if ayar.desen and ayar.desen.search(text):
        return mk(tur if tur in KULAKLIK_TURLERI else "desen", True, _("kulaklik_desen'e uyuyor"))
    if not ayar.otomatik:
        return mk(tur, False, _("otomatik tespit kapalı ve kulaklik_desen'e uymuyor"))
    return mk(tur, False, neden)


def duzeltme_desen_oku() -> str:
    """profiller.toml [duzeltme].cihaz_desen without importing eqgen (router side).
    Missing/unreadable → the default (HS80); eqgen validates the value."""
    import tomllib
    try:
        with open(config.config_path(config.PROFILES_FILE), "rb") as f:
            d = tomllib.load(f).get("duzeltme")
    except (OSError, ValueError):
        return DEFAULT_DUZELTME_DESEN
    v = d.get("cihaz_desen") if isinstance(d, dict) else None   # [[duzeltme]] / duzeltme = 1
    return v if isinstance(v, str) else DEFAULT_DUZELTME_DESEN


# ---------------------------------------------------------------- shared device state

def durum_yolu() -> Path:
    return Path(config.STATE_DIR) / STATE_FILE      # resolved at call time (tests patch it)


def durum_yaz(a: Aygit, path: Path | None = None) -> None:
    """Atomically record the active headphone (router; read by every process)."""
    p = Path(path) if path else durum_yolu()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"ad": a.ad, "aciklama": a.aciklama, "tur": a.tur, "kulaklik": a.kulaklik,
            "duzeltme": a.duzeltme, "zaman": int(time.time())}
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


def durum_oku(path: Path | None = None) -> dict:
    try:
        d = json.loads((Path(path) if path else durum_yolu()).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


_cache: tuple[str, int, bool] | None = None


def duzeltme_acik(path: Path | None = None) -> bool:
    """Is the [duzeltme] curve allowed right now? False only while the router has recorded
    an active headphone it does not fit (no file / unreadable = True, the old behaviour)."""
    global _cache
    p = Path(path) if path else durum_yolu()
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        return True
    c = _cache
    if c is not None and c[0] == str(p) and c[1] == m:
        return c[2]
    v = durum_oku(p).get("duzeltme", True)
    v = v if isinstance(v, bool) else True
    _cache = (str(p), m, v)
    return v
