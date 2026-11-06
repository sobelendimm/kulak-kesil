"""'Yanlış tür' geri bildirimi + sanatçı tür ataması (Qt'siz, tepsi ve GUI ortak).

Kayıt dosyası: ``~/.local/share/oto-eq/geri_bildirim.jsonl`` — her satır bir JSON:
``{"ts": "2026-10-25T17:02:11Z", "profile": "muzik", "key": "sanatci:virgin black",
"display": "Virgin Black", "track": "...", "predicted_group": "pop_vokal",
"source": "iTunes: Pop", "corrected_group": "metal_rock"}`` (UTC; ses YOK).
~1 MB'ı geçince ``geri_bildirim.jsonl.1``'e döner (tek eski kopya tutulur).

Sanatçı ataması GUI'nin (app.py GenrePage.write) yaptığıyla aynıdır:
``config.load_genre_settings()["sanatci"][<normalize edilmiş sanatçı>] = grup`` →
``config.save_genre_settings`` → IPC ``reload`` (çağıran gönderir). Anahtar, tur.py'nin
ContentId "sanatci:<anahtar>" biçimindeki ``<anahtar>`` kısmıdır.
"""
from __future__ import annotations

import json
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("oto_eq.geri_bildirim")

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "oto-eq"
FEEDBACK_FILE = DATA_DIR / "geri_bildirim.jsonl"
MAX_BYTES = 1_000_000

FIELDS = ("profile", "key", "display", "track", "predicted_group", "source", "corrected_group")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rotated(path: Path) -> Path:
    return path.with_name(path.name + ".1")


def kaydet(profile: str, key: str, display: str, track: str, predicted_group: str,
           source: str, corrected_group: str, path: Path | None = None) -> dict:
    """Bir düzeltmeyi ekler (JSON satırı) ve kaydı döndürür. OSError yukarı çıkar."""
    path = Path(path or FEEDBACK_FILE)
    rec = {"ts": _now()}
    for name, val in zip(FIELDS, (profile, key, display, track, predicted_group, source,
                                  corrected_group)):
        rec[name] = "" if val is None else str(val)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size >= MAX_BYTES:
            path.replace(_rotated(path))
    except FileNotFoundError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _read_lines(p: Path) -> list[dict]:
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def oku(limit: int | None = None, path: Path | None = None) -> list[dict]:
    """Kayıtlar eskiden yeniye (döndürülmüş .1 dosyası dahil); ``limit`` = son N kayıt."""
    path = Path(path or FEEDBACK_FILE)
    recs = _read_lines(_rotated(path)) + _read_lines(path)
    if limit is not None:
        recs = recs[-limit:] if limit > 0 else []
    return recs


def ozet(path: Path | None = None) -> dict[tuple[str, str], int]:
    """(tahmin edilen grup, düzeltilen grup) → sayı, çoktan aza. '' = Genel."""
    c = Counter((str(r.get("predicted_group") or ""), str(r.get("corrected_group") or ""))
                for r in oku(path=path))
    return dict(c.most_common())


# ---- sanatçı tür ataması (GUI GenrePage.write ile aynı dosya/biçim) ----------

def _norm(s: str) -> str:
    try:
        from .tur import norm
        return norm(s)
    except Exception:  # noqa: BLE001 - tur.py yoksa basit biçim
        return " ".join((s or "").lower().split())


def sanatci_anahtari(variant: dict | None) -> str:
    """Router durumundaki müzik varyantından atama anahtarı ("virgin black")."""
    from .ui_text import variant_override_key
    return variant_override_key(variant, "sanatci")


def _matches(k: str, key: str, display: str) -> bool:
    """tur.GenreResolver._override ile aynı eşleşme kuralı."""
    bare = key.split(":", 1)[-1]
    nk = _norm(k)
    return k == key or nk == _norm(bare) or (bool(display) and nk == _norm(display))


def sanatci_atamasi(key: str, display: str = "", load=None) -> str | None:
    """Bu sanatçı için mevcut kullanıcı ataması (grup, '' = Genel) ya da None."""
    data = (load or config.load_genre_settings)()
    for k, g in (data.get("sanatci") or {}).items():
        if _matches(k, key, display):
            return str(g)
    return None


def sanatci_ata(key: str, group: str | None, display: str = "", load=None, save=None) -> str:
    """Sanatçıya grup ata (``None`` = atamayı kaldır); eşleşen eski yazımları da temizler.

    ``key``: "virgin black" ya da "sanatci:virgin black". Yazılan anahtarı döndürür.
    IPC ``reload`` çağıranın işidir. OSError yukarı çıkar.
    """
    data = (load or config.load_genre_settings)()
    table = dict(data.get("sanatci") or {})
    bare = key.split(":", 1)[1] if key.startswith("sanatci:") else key
    for k in [k for k in table if _matches(k, key, display)]:
        del table[k]
    if group is not None:
        table[bare] = group
    data["sanatci"] = table
    (save or config.save_genre_settings)(data)
    return bare
