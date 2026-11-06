"""Ayar yedeği: ``kulak yedek al [dosya]`` / ``kulak yedek yukle dosya [--dene]`` / ``kulak yedek liste``.

One ``.tar.gz`` holds the USER files of ~/.config/oto-eq (``DOSYALAR``; generated
confs, plots and caches are NOT included) plus ``manifest.json``::

    kulak-kesil-yedek/manifest.json
    kulak-kesil-yedek/kurallar.toml
    ...

manifest = {"bicim": "kulak-kesil-yedek", "surum": 1, "tarih": ISO-8601,
            "makine": hostname, "dosyalar": {name: {"sha256": hex, "boyut": bytes}}}

Restore (``yukle``): the archive is read safely (regular files with known names
only, size cap, no links/paths), hashes are checked against the manifest, every
file is parsed (TOML/JSON; profiller.toml also through eqgen's validator when
available). ``--dene`` only reports. Otherwise the CURRENT files are first saved
as an automatic backup (``otomatik-yukleme-oncesi-*.tar.gz`` in ``yedek_dizini()``),
then each file is written atomically (temp file + fsync + os.replace). Files the
backup does not contain are left untouched. Finally the user is told what to run
(``kulak yenile`` / ``kulak eq-uret`` / mic regeneration).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import socket
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

from . import config
from .i18n import _

BICIM = "kulak-kesil-yedek"
SURUM = 1
KOK = "kulak-kesil-yedek"
MANIFEST = "manifest.json"
DOSYALAR = ("kurallar.toml", "profiller.toml", "mikrofon.toml", "kisisel.json",
            "tur-atamalari.json", "atamalar.json", "kisma.json")
MAX_BOYUT = 4 * 1024 * 1024        # per member; config files are a few KB
OTO_ONEK = "otomatik-yukleme-oncesi-"
OTO_SAKLA = 10                     # automatic pre-restore backups kept

# regenerating mikrofon.conf from anywhere (`kulak` has no such subcommand; `python3 -m
# oto_eq.micgen` alone only works inside the source dir): the source dir on PYTHONPATH
MIC_URET = (f"PYTHONPATH={shlex.quote(str(config.SRC_DIR))} python3 -m oto_eq.micgen generate"
            " && systemctl --user restart oto-eq-mic")

# what to run after a file changed (shown once per command)
SONRAKI = {
    "kurallar.toml": "kulak yenile",
    "atamalar.json": "kulak yenile",
    "tur-atamalari.json": "kulak yenile",
    "kisma.json": "kulak yenile",
    "profiller.toml": "kulak eq-uret",
    "kisisel.json": "kulak eq-uret",
    "mikrofon.toml": MIC_URET,
}


class YedekHatasi(Exception):
    """User-facing error (Turkish message)."""


def config_dizini() -> Path:
    return Path(config.CONFIG_DIR)       # resolved at call time (tests patch it)


def yedek_dizini() -> Path:
    base = config._xdg("XDG_DATA_HOME", Path.home() / ".local/share")
    return base / "oto-eq" / "yedekler"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ----------------------------------------------------------------- validation

def dogrula(ad: str, data: bytes) -> None:
    """Raise YedekHatasi if ``data`` is not a valid ``ad`` file."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise YedekHatasi(_("{dosya}: UTF-8 değil ({hata})").format(dosya=ad, hata=e)) from e
    if ad.endswith(".toml"):
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise YedekHatasi(_("{dosya}: TOML hatası: {hata}").format(dosya=ad, hata=e)) from e
        if not isinstance(raw, dict):
            raise YedekHatasi(_("{dosya}: TOML tablosu bekleniyordu").format(dosya=ad))
        if ad == config.PROFILES_FILE:
            _profiller_dogrula(text)
        elif ad == "mikrofon.toml":
            _mikrofon_dogrula(raw)
        return
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise YedekHatasi(_("{dosya}: JSON hatası: {hata}").format(dosya=ad, hata=e)) from e
    if not isinstance(raw, dict):
        raise YedekHatasi(_("{dosya}: JSON nesnesi ({ornek}) bekleniyordu").format(
            dosya=ad, ornek="{...}"))


def _profiller_dogrula(text: str) -> None:
    """Semantic check through eqgen when it imports (it may be mid-edit / absent)."""
    try:
        from . import eqgen
        load = getattr(eqgen, "load_profiles")
    except Exception:  # noqa: BLE001 - validator unavailable: TOML parse is enough
        return
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / config.PROFILES_FILE
        p.write_text(text, encoding="utf-8")
        try:
            load(p)
        except Exception as e:  # noqa: BLE001 - e.g. AttributeError for `genel = 1`
            raise YedekHatasi(_("{dosya}: geçersiz profil ayarı: {hata}").format(
                dosya=config.PROFILES_FILE, hata=e)) from e


def _mikrofon_dogrula(raw: dict) -> None:
    """mikrofon.toml through micgen's validator (an invalid one would be restored and then
    `micgen generate` / the router's retarget fail with it)."""
    try:
        from . import micgen
        norm = getattr(micgen, "normalize_mic")
    except Exception:  # noqa: BLE001 - validator unavailable: TOML parse is enough
        return
    try:
        norm(raw)
    except Exception as e:  # noqa: BLE001 - MicError, or e.g. TypeError for `genel = 1`
        raise YedekHatasi(_("{dosya}: geçersiz mikrofon ayarı: {hata}").format(
            dosya="mikrofon.toml", hata=e)) from e


# ----------------------------------------------------------------- create

def mevcut_dosyalar(cdir: Path | None = None) -> dict[str, bytes]:
    cdir = cdir or config_dizini()
    out = {}
    for ad in DOSYALAR:
        p = cdir / ad
        if p.is_file():
            out[ad] = p.read_bytes()
    return out


def arsiv_yaz(hedef: Path, dosyalar: dict[str, bytes], tarih: str | None = None) -> dict:
    """Write the archive atomically; returns the manifest."""
    manifest = {
        "bicim": BICIM, "surum": SURUM,
        "tarih": tarih or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "makine": socket.gethostname(),
        "dosyalar": {ad: {"sha256": _sha(b), "boyut": len(b)} for ad, b in sorted(dosyalar.items())},
    }
    hedef.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".yedek-", suffix=".tmp", dir=hedef.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            with tarfile.open(fileobj=f, mode="w:gz") as tar:
                items = [(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=1).encode())]
                items += sorted(dosyalar.items())
                for ad, data in items:
                    ti = tarfile.TarInfo(f"{KOK}/{ad}")
                    ti.size = len(data)
                    ti.mtime = int(time.time())
                    ti.mode = 0o644
                    tar.addfile(ti, io.BytesIO(data))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, hedef)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return manifest


def al(hedef: str | Path | None = None, cdir: Path | None = None) -> tuple[Path, dict]:
    """Create a backup. ``hedef`` file / directory / None (default dir)."""
    dosyalar = mevcut_dosyalar(cdir)
    if not dosyalar:
        raise YedekHatasi(_("yedeklenecek ayar dosyası yok ({dizin})").format(
            dizin=cdir or config_dizini()))
    if hedef is None:
        path = yedek_dizini() / f"kulak-yedek-{_stamp()}.tar.gz"
    else:
        path = Path(hedef).expanduser()
        if path.is_dir():
            path = path / f"kulak-yedek-{_stamp()}.tar.gz"
        elif not path.name.endswith((".tar.gz", ".tgz")):
            path = path.with_name(path.name + ".tar.gz")
    if path.exists():
        raise YedekHatasi(_("{yol} zaten var; başka bir ad verin").format(yol=path))
    return path, arsiv_yaz(path, dosyalar)


# ----------------------------------------------------------------- read / restore

def oku(kaynak: str | Path) -> tuple[dict, dict[str, bytes]]:
    """Read + verify an archive → (manifest, {name: bytes}). Raises YedekHatasi."""
    path = Path(kaynak).expanduser()
    if not path.is_file():
        raise YedekHatasi(_("yedek dosyası bulunamadı: {yol}").format(yol=path))
    uyeler: dict[str, bytes] = {}
    try:
        with tarfile.open(path, mode="r:gz") as tar:
            for ti in tar:
                if ti.isdir() and ti.name.rstrip("/") == KOK:
                    continue
                if not ti.isfile():
                    raise YedekHatasi(_("arşivde beklenmeyen öğe: {ad}").format(ad=ti.name))
                kok, _sep, ad = ti.name.partition("/")
                if kok != KOK or ad not in DOSYALAR + (MANIFEST,):
                    raise YedekHatasi(_("arşivde beklenmeyen dosya: {ad}").format(ad=ti.name))
                if ti.size > MAX_BOYUT:
                    raise YedekHatasi(_("{dosya} çok büyük ({boyut} bayt)").format(dosya=ad, boyut=ti.size))
                if ad in uyeler:
                    raise YedekHatasi(_("{dosya} arşivde iki kez var").format(dosya=ad))
                f = tar.extractfile(ti)
                uyeler[ad] = f.read() if f is not None else b""
    except (tarfile.TarError, OSError, EOFError) as e:
        raise YedekHatasi(_("arşiv okunamadı ({ad}): {hata}").format(ad=path.name, hata=e)) from e
    if MANIFEST not in uyeler:
        raise YedekHatasi(_("manifest.json yok: bu bir Kulak Kesil yedeği değil"))
    try:
        manifest = json.loads(uyeler.pop(MANIFEST).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise YedekHatasi(_("manifest.json bozuk: {hata}").format(hata=e)) from e
    if not isinstance(manifest, dict) or manifest.get("bicim") != BICIM:
        raise YedekHatasi(_("manifest.json: bu bir Kulak Kesil yedeği değil"))
    surum = manifest.get("surum")
    if not isinstance(surum, int) or surum < 1 or surum > SURUM:
        raise YedekHatasi(_("yedek biçimi sürümü {surum} desteklenmiyor (en çok {en_cok})").format(
            surum=repr(surum), en_cok=SURUM))
    liste = manifest.get("dosyalar")
    if not isinstance(liste, dict) or set(liste) != set(uyeler):
        raise YedekHatasi(_("manifest ile arşivdeki dosyalar uyuşmuyor"))
    for ad, data in uyeler.items():
        bilgi = liste[ad] if isinstance(liste[ad], dict) else {}
        if bilgi.get("sha256") != _sha(data):
            raise YedekHatasi(_("{dosya}: sağlama (sha256) tutmuyor; arşiv bozulmuş").format(dosya=ad))
    if not uyeler:
        raise YedekHatasi(_("yedekte hiç ayar dosyası yok"))
    for ad, data in uyeler.items():
        dogrula(ad, data)
    return manifest, uyeler


def atomik_yaz(path: Path | str, data: bytes | str) -> None:
    """Write ``data`` (str = UTF-8) to ``path`` atomically: a unique temp file in the same
    dir + fsync + os.replace (the mode of an existing file is kept, new files 0644).
    Shared by eqgen / micgen config writers."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _budama(ydir: Path) -> None:
    oto = sorted(ydir.glob(OTO_ONEK + "*.tar.gz"))
    for p in oto[:-OTO_SAKLA]:
        try:
            p.unlink()
        except OSError:
            pass


def yukle(kaynak: str | Path, dene: bool = False, cdir: Path | None = None,
          ydir: Path | None = None) -> dict:
    """Restore. Returns {"degisen", "ayni", "yeni", "on_yedek", "manifest"}."""
    cdir = cdir or config_dizini()
    manifest, uyeler = oku(kaynak)
    simdiki = mevcut_dosyalar(cdir)
    degisen = sorted(a for a in uyeler if a in simdiki and simdiki[a] != uyeler[a])
    yeni = sorted(a for a in uyeler if a not in simdiki)
    ayni = sorted(a for a in uyeler if simdiki.get(a) == uyeler[a])
    sonuc = {"degisen": degisen, "yeni": yeni, "ayni": ayni, "on_yedek": None,
             "manifest": manifest}
    if dene or not (degisen or yeni):
        return sonuc
    if simdiki:
        ydir = ydir or yedek_dizini()
        on = ydir / f"{OTO_ONEK}{_stamp()}.tar.gz"
        n = 1
        while on.exists():
            on = ydir / f"{OTO_ONEK}{_stamp()}-{n}.tar.gz"
            n += 1
        arsiv_yaz(on, simdiki)
        sonuc["on_yedek"] = on
        _budama(ydir)
    yazilan: list[str] = []
    try:
        for ad in degisen + yeni:
            atomik_yaz(cdir / ad, uyeler[ad])
            yazilan.append(ad)
    except OSError as e:
        # put back what was already written (each file is atomic, the set was not)
        geri_alinamayan = []
        for ad in yazilan:
            try:
                if ad in simdiki:
                    atomik_yaz(cdir / ad, simdiki[ad])
                else:
                    (cdir / ad).unlink()
            except OSError:
                geri_alinamayan.append(ad)
        ek = (_("; geri alınamayan: {dosyalar}").format(dosyalar=", ".join(geri_alinamayan))
              if geri_alinamayan else "")
        on = sonuc.get("on_yedek")
        raise YedekHatasi(_("geri yükleme yarıda kaldı ({hata}); yazılanlar geri alındı{ek}"
                            "{onyedek}").format(
            hata=e, ek=ek, onyedek=(_("; ön yedek: {yol}").format(yol=on) if on else ""))) from e
    return sonuc


def sonraki_adimlar(adlar: list[str]) -> list[str]:
    out: list[str] = []
    for ad in adlar:
        c = SONRAKI.get(ad)
        if c and c not in out:
            out.append(c)
    # rules first (router reload), then the EQ conf (restarts the sinks), then the mic
    order = ["kulak yenile", "kulak eq-uret"]
    return sorted(out, key=lambda c: order.index(c) if c in order else len(order))


# ----------------------------------------------------------------- CLI

KULLANIM = _("""kullanım:
  kulak yedek al [dosya|dizin]        ayarları tek .tar.gz dosyasına yedekle
  kulak yedek yukle DOSYA [--dene]    yedeği geri yükle (--dene: yalnız ne değişeceğini göster)
  kulak yedek liste                   varsayılan yedek klasöründeki yedekler
Yedeklenenler: {dosyalar} (üretilen .conf/önbellek dosyaları değil).""").format(
    dosyalar=", ".join(DOSYALAR))


def _manifest_ozet(m: dict) -> str:
    return _("{tarih}, {makine}, {n} dosya").format(
        tarih=m.get("tarih", "?"), makine=m.get("makine", "?"), n=len(m.get("dosyalar") or {}))


def run(argv: list[str] | None = None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "yardim", "yardım"):
        print(KULLANIM, file=out if argv else err)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "al":
            if len(rest) > 1:
                print(KULLANIM, file=err)
                return 2
            path, m = al(rest[0] if rest else None)
            print(_("Yedek alındı: {yol}").format(yol=path), file=out)
            print(f"  {', '.join(m['dosyalar'])}", file=out)
            return 0
        if cmd in ("yukle", "yükle"):
            dene = "--dene" in rest
            dosya = [a for a in rest if a != "--dene"]
            if len(dosya) != 1:
                print(KULLANIM, file=err)
                return 2
            s = yukle(dosya[0], dene=dene)
            print(_("Yedek: {ozet} — doğrulandı.").format(ozet=_manifest_ozet(s["manifest"])), file=out)
            if s["ayni"]:
                print("  " + _("aynı (dokunulmayacak): {dosyalar}").format(dosyalar=", ".join(s["ayni"])),
                      file=out)
            if not (s["degisen"] or s["yeni"]):
                print(_("Değişecek bir şey yok; hiçbir dosya yazılmadı."), file=out)
                return 0
            if s["degisen"]:
                print("  " + (_("değişecek: {dosyalar}") if dene else _("değişti: {dosyalar}"))
                      .format(dosyalar=", ".join(s["degisen"])), file=out)
            if s["yeni"]:
                print("  " + (_("eklenecek: {dosyalar}") if dene else _("eklendi: {dosyalar}"))
                      .format(dosyalar=", ".join(s["yeni"])), file=out)
            if dene:
                print(_("Deneme: hiçbir dosya yazılmadı. Uygulamak için {secenek} olmadan "
                        "çalıştırın.").format(secenek="--dene"), file=out)
                return 0
            if s["on_yedek"]:
                print(_("Önceki ayarlar yedeklendi: {yol}").format(yol=s["on_yedek"]), file=out)
            adimlar = sonraki_adimlar(s["degisen"] + s["yeni"])
            if adimlar:
                print(_("Geçerli olması için çalıştırın:"), file=out)
                for a in adimlar:
                    print(f"  {a}", file=out)
            return 0
        if cmd == "liste":
            ydir = yedek_dizini()
            files = sorted(ydir.glob("*.tar.gz")) if ydir.is_dir() else []
            if not files:
                print(_("{dizin} içinde yedek yok.").format(dizin=ydir), file=out)
                return 0
            for p in files:
                try:
                    m, _uyeler = oku(p)
                    print(f"{p.name}  ({_manifest_ozet(m)})", file=out)
                except Exception as e:  # noqa: BLE001 - one bad archive must not hide the rest
                    print(_("{ad}  (okunamadı: {hata})").format(ad=p.name, hata=e), file=out)
            return 0
    except YedekHatasi as e:
        print(_("hata: {hata}").format(hata=e), file=err)
        return 1
    except OSError as e:
        print(_("hata: {hata}").format(hata=e), file=err)
        return 1
    print(_("bilinmeyen alt komut: {komut}").format(komut=cmd) + "\n" + KULLANIM, file=err)
    return 2


if __name__ == "__main__":
    sys.exit(run())
