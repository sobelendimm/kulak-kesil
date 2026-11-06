"""Kişisel ses modeli (CONTRACT CHANGE 6, D3): ses analizinin füzyonunu kullanıcıya uydurur.

Akış (ses YOK, yalnız sayılar):

1. ``kaydet_tahmin(cid, detail)`` — yönlendirici her ses tabanlı karardan sonra çağırır
   (``AudioAnalyzer.last_detail``): DSP özellikleri + DSP/model olasılıkları + karar →
   ``~/.local/share/oto-eq/ses_tahminleri.jsonl`` (~2 MB'ta ``.1``'e döner).
2. ``egit()`` — tahmin kayıtlarını etiketlerle birleştirir:
   * GÜÇLÜ etiket: ``geri_bildirim.jsonl`` düzeltmesi (tepsi "Bu şarkı aslında…") ya da
     kullanıcının sanatçı ataması (tur-atamalari.json, kaynak "kullanıcı");
   * ZAYIF etiket (ağırlık ``WEAK_WEIGHT``): aynı sanatçı için SONRADAN gelen ad tabanlı
     olumlu sonuç (iTunes/Deezer/MusicBrainz/yerleşik liste/şarkı adı; ses hafızası hariç).
   Küçük bir çok sınıflı lojistik "düzeltme başı" öğrenir:
   ``p = softmax(log(birleşik_taban) + x·W + b)``, x = [log model olasılıkları, log DSP
   olasılıkları, seçili DSP özellikleri (ses_analiz standardı)]. θ=0 → taban füzyonun aynısı;
   L2 cezası θ'yı sıfıra çeker (az veri = tabana yakın).
3. Etkinleşme: ≥ ``MIN_STRONG`` güçlü örnek, ≥ ``MIN_KEYS`` farklı sanatçı VE sanatçıya göre
   gruplu çapraz doğrulamada (yanlış = −2, doğru = +1, kararsız = 0; B2'nin puanı) kişisel
   kural tabandan en az ``max(MIN_GAIN, GAIN_FRAC·n)`` puan iyi. Değilse model dosyası
   ``aktif: false`` + Türkçe neden ile yazılır; ses_analiz tabanı kullanır.
4. ``yukle()`` → ``KisiselModel``: ``adjust(dsp, model, features)``; dosyayı en çok
   ``CHECK_S`` sn'de bir yoklar (``kulak model egit/sifirla`` yeniden başlatmadan geçerli).

Belirlenimci (Newton, sabit sıra), sürümlü (``VERSION`` + ses_analiz parmak izi), silinmesi
güvenli. Yalnız numpy, küçük matrisler (tek iş parçacığı yeter). ``run(argv)`` =
``kulak model [durum|egit|sifirla]``. Ayrıntı: docs/NOTES-kisisel-model.md.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from . import ses_analiz as sa
from .i18n import N_, Metin, _, kayittan, metin_kaydi
from .models import MUSIC_GROUPS

log = logging.getLogger("oto_eq.kisisel_model")

VERSION = 1
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "oto-eq"
TAHMIN_FILE = DATA_DIR / "ses_tahminleri.jsonl"
MODEL_FILE = DATA_DIR / "kisisel_model.json"
FEEDBACK_FILE = DATA_DIR / "geri_bildirim.jsonl"
MAX_BYTES = 2_000_000

MIN_STRONG = 20          # güçlü etiketli örnek (tahmin kaydı) en az
MIN_KEYS = 4             # güçlü etiketli farklı sanatçı en az (gruplu CV için)
WEAK_WEIGHT = 0.3        # ad tabanlı (zayıf) etiketin ağırlığı
# L2 adayları (λ olasılık girdileri, özellik çarpanı; b için 0.1·λ). Eğitim CV ile seçer,
# eşitlikte ilk (daha temkinli) kazanır. B2 tablosunda (3, 10) en iyisiydi.
ADAYLAR: tuple[tuple[float, float], ...] = ((3.0, 10.0), (1.0, 3.0))
LAMBDA, FEAT_REG = ADAYLAR[0]
MIN_GAIN = 2.0           # CV puan kazancı en az (doğru +1, yanlış −2)
GAIN_FRAC = 0.02         # … ya da n·GAIN_FRAC (büyük olan)
LOKO_MAX_KEYS = 10       # ≤ bu kadar sanatçı: sanatçı-bırak-dışarı; fazlası: 5 kat
CHECK_S = 60.0           # KisiselModel dosya yoklama aralığı

GROUPS: tuple[str, ...] = tuple(sa._GROUPS)
_K = len(GROUPS)
# ses_analiz._vector() içinden seçilen (standartlaştırılmış) DSP özellikleri
_VEC_NAMES = ("r_sub", "r_bass", "r_lowmid", "r_mid", "r_highmid", "r_high", "sub_share",
              "centroid", "rolloff", "flatness", "centroid_cv", "flux", "low_pump_db",
              "beat_low", "beat_full", "onset_rate", "crest_db", "lra_db", "pause_ratio",
              "env_std_db", "mod_speech", "mod_peak", "stability", "harm_ratio")
FEATURES: tuple[str, ...] = ("r_sub", "r_bass", "r_highmid", "centroid", "flatness", "beat_low",
                             "onset_rate", "crest_db", "pause_ratio", "mod_speech", "stability",
                             "harm_ratio")
_FEAT_IDX = [_VEC_NAMES.index(n) for n in FEATURES]
INPUTS: tuple[str, ...] = (tuple(f"model:{g}" for g in GROUPS) + tuple(f"dsp:{g}" for g in GROUPS)
                           + FEATURES)
_P_EPS = 0.01            # log(p + eps) girdileri
_OFF_EPS = 1e-3          # taban füzyon logiti


def parmak_izi() -> str:
    """ses_analiz sözleşmesinin izi: özellik dönüşümü/füzyon değişirse model geçersiz."""
    blob = json.dumps([VERSION, GROUPS, list(sa._MU), list(sa._SD), FEATURES,
                       sa.DSP_WEIGHT, sa.MODEL_WEIGHT, sa.FUSED_MIN, sa.PERSONAL_OVERRIDE_MIN,
                       sa.PERSONAL_KEEP_MIN], sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(v: Any, nd: int = 4) -> Any:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _rd(d: Any, nd: int = 4) -> dict | None:
    if not isinstance(d, dict):
        return None
    return {str(k): _r(v, nd) for k, v in d.items()}


def _rotated(p: Path) -> Path:
    return p.with_name(p.name + ".1")


# ============================================================================
#  1) TAHMİN KAYDI
# ============================================================================


def kaydet_tahmin(cid: Any, detail: dict | None, asama: str = "",
                  path: Path | None = None) -> dict | None:
    """Bir ses kararının sayılarını ekler (JSON satırı). Hiç hata fırlatmaz.

    ``cid``: ContentId (key/display/track); ``detail``: ``AudioAnalyzer.last_detail``
    (kararsız pencere de kaydedilir: group ''). ``asama``: "pencere" | "hatirla" (serbest).
    """
    if cid is None or not isinstance(detail, dict) or not detail.get("dsp_scores"):
        return None
    try:
        rec = {
            "ts": _now(), "surum": VERSION,
            "key": str(getattr(cid, "key", "") or ""),
            "display": str(getattr(cid, "display", "") or ""),
            "track": str(getattr(cid, "track", "") or ""),
            "asama": str(asama or ""),
            "window_s": _r(detail.get("window_s"), 2),
            "group": str(detail.get("group") or ""),
            "confidence": _r(detail.get("confidence"), 3),
            "kisisel": detail.get("personal") is not None,
            "dsp": _rd(detail.get("dsp_scores")),
            "model": _rd(detail.get("model_probs")),
            "features": _rd(detail.get("features")),
        }
        if not rec["key"]:
            return None
        p = Path(path or TAHMIN_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.stat().st_size >= MAX_BYTES:
                p.replace(_rotated(p))
        except FileNotFoundError:
            pass
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception as e:  # noqa: BLE001 - a log write must never break the router
        log.debug("ses tahmini kaydedilemedi: %s", e)
        return None


def _read_jsonl(p: Path) -> list[dict]:
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


def oku_tahminler(path: Path | None = None) -> list[dict]:
    p = Path(path or TAHMIN_FILE)
    return _read_jsonl(_rotated(p)) + _read_jsonl(p)


# ============================================================================
#  2) ETİKETLER + ÖRNEKLER
# ============================================================================


def _norm(s: str) -> str:
    try:
        from .tur import norm
        return norm(s)
    except Exception:  # noqa: BLE001
        return " ".join((s or "").lower().split())


def _bare(key: str) -> str:
    return _norm(str(key or "").split(":", 1)[-1])


def isim_etiketi_fabrikasi(cache_path: Path | None = None,
                           settings: dict | None = None) -> Callable[[str, str, str], Any]:
    """Ad tabanlı etiket bulucu: (key, display, track) → (grup, kaynak) | None.

    tur.GenreResolver önbelleği (ağ YOK: çevrimdışı ayarla) + kullanıcı atamaları;
    ses hafızası ("ses analizi …") etiket sayılmaz (döngüsel olurdu).
    """
    from . import config, tur
    from .models import ContentId
    st = dict(settings if settings is not None else config.load_genre_settings())
    st["cevrimici"] = False
    res = tur.GenreResolver(settings=st, cache_path=cache_path)

    def bul(key: str, display: str, track: str):
        try:
            r = res.cached(ContentId("muzik", key, display, track))
        except Exception:  # noqa: BLE001
            return None
        if r is None or str(r.source or "").startswith("ses analizi"):
            return None
        if r.group and r.group not in MUSIC_GROUPS:
            return None
        if not r.group and r.source != "kullanıcı":
            return None                 # "bilinmiyor" etiketi değildir
        return r.group, str(r.source or "")
    return bul


@dataclass
class Ornek:
    key: str
    pd: dict[str, float]
    pm: dict[str, float]
    f: dict[str, float]
    label: str            # MUSIC_GROUPS ya da '' (= Genel: hiçbir grup değil)
    strong: bool
    weight: float
    kaynak: str = ""


def _clean_probs(d: Any) -> dict[str, float] | None:
    return sa.clean_model_probs(d) if isinstance(d, dict) else None


def ornekler(tahminler: Iterable[dict], duzeltmeler: Iterable[dict],
             isim_etiketi: Callable[[str, str, str], Any] | None = None) -> tuple[list[Ornek], dict]:
    """Tahmin kayıtları × etiketler → eğitim örnekleri + sayaçlar."""
    corr: dict[str, tuple[str, str]] = {}           # bare key → (ts, grup) en yenisi
    for c in duzeltmeler:
        if str(c.get("profile") or "muzik") != "muzik":
            continue
        g = str(c.get("corrected_group") or "")
        if g and g not in MUSIC_GROUPS:
            continue
        k = _bare(c.get("key", ""))
        if not k:
            continue
        ts = str(c.get("ts") or "")
        if k not in corr or ts >= corr[k][0]:
            corr[k] = (ts, g)
    say = {"kayit": 0, "modelsiz": 0, "etiketsiz": 0, "guclu": 0, "zayif": 0}
    name_cache: dict[tuple, Any] = {}
    out: list[Ornek] = []
    need = set(_VEC_NAMES) | {"r_sub", "r_bass", "beat_low", "pause_ratio", "flatness"}
    for t in tahminler:
        say["kayit"] += 1
        pd_, pm = _clean_probs(t.get("dsp")), _clean_probs(t.get("model"))
        f = t.get("features")
        if pm is None or pd_ is None or not isinstance(f, dict) \
                or any(not isinstance(f.get(n), (int, float)) for n in need):
            say["modelsiz"] += 1
            continue
        key = str(t.get("key") or "")
        bk = _bare(key)
        label, strong, src = None, False, ""
        if bk in corr:
            label, strong, src = corr[bk][1], True, "geri bildirim"
        elif isim_etiketi is not None:
            ck = (key, str(t.get("display") or ""), str(t.get("track") or ""))
            if ck not in name_cache:
                name_cache[ck] = isim_etiketi(*ck)
            r = name_cache[ck]
            if r:
                label, src = r[0], r[1]
                strong = src == "kullanıcı"
        if label is None:
            say["etiketsiz"] += 1
            continue
        say["guclu" if strong else "zayif"] += 1
        out.append(Ornek(bk, pd_, pm, {k: float(v) for k, v in f.items()
                                       if isinstance(v, (int, float))},
                         label, strong, 1.0 if strong else WEAK_WEIGHT, src))
    say["sanatci_guclu"] = len({o.key for o in out if o.strong})
    say["sanatci"] = len({o.key for o in out})
    return out, say


# ============================================================================
#  3) MODEL (çok sınıflı lojistik düzeltme başı, Newton)
# ============================================================================


def girdi(pd_: dict[str, float], pm: dict[str, float], f: dict[str, float]) -> np.ndarray:
    z = (sa._vector(f) - np.asarray(sa._MU)) / np.asarray(sa._SD)
    z = np.clip(z, -6.0, 6.0)[_FEAT_IDX]
    lm = np.log(np.array([pm.get(g, 0.0) for g in GROUPS]) + _P_EPS) / 2.0
    ld = np.log(np.array([pd_.get(g, 0.0) for g in GROUPS]) + _P_EPS) / 2.0
    return np.concatenate([lm, ld, z])


def ofset(pd_: dict[str, float], pm: dict[str, float]) -> np.ndarray:
    fu = sa.fuse(pd_, pm)
    return np.log(np.array([fu[g] for g in GROUPS]) + _OFF_EPS)


def _softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def _targets(labels: list[str]) -> np.ndarray:
    T = np.full((len(labels), _K), 1.0 / _K)          # '' (Genel) = düzgün dağılım
    for i, g in enumerate(labels):
        if g:
            T[i] = 0.0
            T[i, GROUPS.index(g)] = 1.0
    return T


def _reg(d: int, lam: float, feat_reg: float = FEAT_REG) -> np.ndarray:
    """L2 matrix (d+1, K). Probability inputs act per class only ("re-weight the fusion":
    log p_model[k], log p_dsp[k] → class k; other entries pinned to 0), DSP features
    ``FEAT_REG·lam``, bias ``0.1·lam``."""
    reg = np.full((d + 1, _K), float(lam))
    for j in range(2 * _K):
        for k in range(_K):
            if j % _K != k:
                reg[j, k] = _PIN
    reg[2 * _K:d] = lam * feat_reg
    reg[-1] = lam * 0.1
    return reg


_PIN = 1e9               # "sabit 0" cezası (fit sonunda tam 0 yapılır)


def fit(X: np.ndarray, O: np.ndarray, T: np.ndarray, w: np.ndarray,
        lam: float = LAMBDA, feat_reg: float = FEAT_REG,
        iters: int = 30) -> tuple[np.ndarray, np.ndarray]:
    """Ağırlıklı yumuşak-hedef softmax regresyon, ofsetli, L2 (Newton) → (W (d,K), b (K,))."""
    n, d = X.shape
    Xa = np.hstack([X, np.ones((n, 1))])
    d1 = d + 1
    reg = _reg(d, lam, feat_reg)
    th = np.zeros((d1, _K))

    def obj(th_):
        P = _softmax(O + Xa @ th_)
        ce = -(w[:, None] * T * np.log(np.maximum(P, 1e-300))).sum()
        return ce + 0.5 * (reg * th_ ** 2).sum(), P

    f0, P = obj(th)
    regd = reg.T.ravel()                                # (K*d1,) sıralama: k*d1 + j
    for _i in range(iters):
        G = Xa.T @ (w[:, None] * (P - T)) + reg * th                    # (d1, K)
        S = w[:, None, None] * (np.einsum("ik,kl->ikl", P, np.eye(_K))
                                - P[:, :, None] * P[:, None, :])        # (n, K, K)
        H = np.einsum("ikl,id,ie->kdle", S, Xa, Xa).reshape(_K * d1, _K * d1)
        H[np.diag_indices_from(H)] += regd
        step = np.linalg.solve(H, G.T.ravel()).reshape(_K, d1).T
        t = 1.0
        while True:
            f1, P1 = obj(th - t * step)
            if f1 <= f0 + 1e-12 or t < 1e-4:
                break
            t *= 0.5
        if f1 > f0:
            break
        th, P, dec = th - t * step, P1, f0 - f1
        f0 = f1
        if dec < 1e-10 * max(1.0, abs(f0)):
            break
    th[reg >= _PIN] = 0.0
    return th[:-1], th[-1]


def _matrices(S: list[Ornek]):
    X = np.array([girdi(o.pd, o.pm, o.f) for o in S]).reshape(len(S), len(INPUTS))
    O = np.array([ofset(o.pd, o.pm) for o in S]).reshape(len(S), _K)
    T = _targets([o.label for o in S])
    w = np.array([o.weight for o in S], dtype=float)
    return X, O, T, w


def uygula(W: np.ndarray, b: np.ndarray, pd_: dict, pm: dict, f: dict) -> dict[str, float]:
    p = _softmax(ofset(pd_, pm) + girdi(pd_, pm, f) @ W + b)
    return {g: float(v) for g, v in zip(GROUPS, p)}


# ---- değerlendirme ---------------------------------------------------------

def _puan(top: str, ok: bool, label: str) -> tuple[int, int, int]:
    """(puan, doğru, yanlış): doğru +1, yanlış −2 (Genel'e karar da yanlış), kararsız 0."""
    if not ok:
        return 0, 0, 0
    return (1, 1, 0) if top == label else (-2, 0, 1)


def degerlendir(S: list[Ornek], W: np.ndarray | None = None,
                b: np.ndarray | None = None) -> dict:
    """Taban (W None) ya da kişisel kuralın puanı (ses_analiz.decide ile birebir)."""
    tot = dogru = yanlis = 0
    per: dict[str, list[int]] = {g: [0, 0, 0] for g in GROUPS + ("",)}   # doğru/yanlış/hiç
    for o in S:
        adj = uygula(W, b, o.pd, o.pm, o.f) if W is not None else None
        top, ok, _kanit = sa.decide(o.pd, o.pm, o.f, adj)
        p, c, wr = _puan(top, ok, o.label)
        tot += p
        dogru += c
        yanlis += wr
        per[o.label][0 if c else 1 if wr else 2] += 1
    n = len(S)
    return {"n": n, "puan": tot, "dogru": dogru, "yanlis": yanlis,
            "kapsama": round((dogru + yanlis) / n, 3) if n else 0.0,
            "isabet": round(dogru / (dogru + yanlis), 3) if dogru + yanlis else 0.0,
            "grup": {g or "genel": v for g, v in per.items() if sum(v)}}


def _katlar(keys: list[str]) -> list[list[str]]:
    ks = sorted(set(keys), key=lambda k: (hashlib.sha1(k.encode()).hexdigest(), k))
    if len(ks) <= LOKO_MAX_KEYS:
        return [[k] for k in ks]
    return [ks[i::5] for i in range(5)]


def capraz_dogrula(S: list[Ornek], lam: float = LAMBDA, feat_reg: float = FEAT_REG) -> dict:
    """Sanatçıya göre gruplu CV: test = dışarıda bırakılan sanatçıların GÜÇLÜ örnekleri."""
    strong_keys = [o.key for o in S if o.strong]
    folds = _katlar(strong_keys)
    test_all: list[Ornek] = []
    base = {"puan": 0, "dogru": 0, "yanlis": 0, "n": 0}
    mine = dict(base)
    for fold in folds:
        fs = set(fold)
        tr = [o for o in S if o.key not in fs]
        te = [o for o in S if o.key in fs and o.strong]
        if not te:
            continue
        test_all += te
        if tr:
            W, b = fit(*_matrices(tr), lam=lam, feat_reg=feat_reg)
        else:
            W, b = np.zeros((len(INPUTS), _K)), np.zeros(_K)
        rb, rm = degerlendir(te), degerlendir(te, W, b)
        for acc, r in ((base, rb), (mine, rm)):
            for k in acc:
                acc[k] += r[k]
    n = base["n"]
    for acc in (base, mine):
        acc["kapsama"] = round((acc["dogru"] + acc["yanlis"]) / n, 3) if n else 0.0
        acc["isabet"] = (round(acc["dogru"] / (acc["dogru"] + acc["yanlis"]), 3)
                         if acc["dogru"] + acc["yanlis"] else 0.0)
    return {"kat": len(folds), "taban": base, "kisisel": mine,
            "kazanc": mine["puan"] - base["puan"],
            "esik": max(MIN_GAIN, GAIN_FRAC * n), "lambda": [lam, feat_reg]}


# ============================================================================
#  4) EĞİTİM + DOSYA
# ============================================================================


def egit(tahmin_path: Path | None = None, feedback_path: Path | None = None,
         model_path: Path | None = None, isim_etiketi: Any = "varsayilan",
         adaylar: Iterable[tuple[float, float]] | None = None, kaydet: bool = True) -> dict:
    """Eğit + CV + etkinleşme kararı; model dosyasını yazar (``kaydet``) ve döndürür.

    ``adaylar`` = (λ, özellik çarpanı) çiftleri (varsayılan ``ADAYLAR``); CV kazancı en
    yüksek olan seçilir (eşitlikte önceki), etkinleşme yine o kazancın eşiği geçmesine bağlı.
    """
    if isim_etiketi == "varsayilan":
        try:
            isim_etiketi = isim_etiketi_fabrikasi()
        except Exception as e:  # noqa: BLE001
            log.info("ad tabanlı etiketler okunamadı: %s", e)
            isim_etiketi = None
    tah = oku_tahminler(tahmin_path)
    fb = _read_jsonl(_rotated(Path(feedback_path or FEEDBACK_FILE))) \
        + _read_jsonl(Path(feedback_path or FEEDBACK_FILE))
    S, say = ornekler(tah, fb, isim_etiketi)
    m: dict[str, Any] = {"surum": VERSION, "parmak_izi": parmak_izi(), "egitim": _now(),
                         "aktif": False, "neden": "", "sayilar": say,
                         "girdiler": list(INPUTS), "gruplar": list(GROUPS)}
    if say["guclu"] < MIN_STRONG:
        _neden(m, N_("yetersiz güçlü örnek: {n}/{gereken} (tepsiden 'Bu şarkı aslında…' "
                     "düzeltmeleri gerekiyor)"), n=say["guclu"], gereken=MIN_STRONG)
    elif say["sanatci_guclu"] < MIN_KEYS:
        _neden(m, N_("yetersiz sanatçı çeşidi: {n}/{gereken} (çapraz doğrulama için)"),
               n=say["sanatci_guclu"], gereken=MIN_KEYS)
    if not m["neden"]:
        cv = None
        for lam, fr in (adaylar or ADAYLAR):
            c = capraz_dogrula(S, lam, fr)
            if cv is None or c["kazanc"] > cv["kazanc"]:
                cv = c
        m["cv"] = cv
        m["lambda"] = cv["lambda"]
        if cv["kazanc"] >= cv["esik"]:
            W, b = fit(*_matrices(S), lam=cv["lambda"][0], feat_reg=cv["lambda"][1])
            m["W"] = [[round(float(v), 6) for v in row] for row in W]
            m["b"] = [round(float(v), 6) for v in b]
            m["aktif"] = True
            _neden(m, N_("çapraz doğrulamada +{kazanc} puan (eşik {esik:.0f}, {n} güçlü örnek)"),
                   kazanc=cv["kazanc"], esik=cv["esik"], n=cv["taban"]["n"])
        else:
            _neden(m, N_("tabandan iyi değil: çapraz doğrulama kazancı {kazanc:+d} puan "
                         "(gereken ≥ {esik:.0f})"), kazanc=cv["kazanc"], esik=cv["esik"])
    if kaydet:
        p = Path(model_path or MODEL_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(p)
    log.info("kişisel model: %s — %s", "aktif" if m["aktif"] else "pasif", m["neden"])
    return m


def _neden(m: dict, msgid: str, **args) -> None:
    """The model file's reason: the text in today's language ("neden", read by older
    versions as is) + its msgid record ("neden_k") to show it in the UI language of later."""
    t = Metin(msgid, **args)
    m["neden"], m["neden_k"] = str(t), metin_kaydi(t)


def neden_metni(m: dict | None) -> str:
    """The model's reason in the CURRENT UI language (a file without "neden_k": as stored)."""
    if not isinstance(m, dict):
        return ""
    return kayittan(m.get("neden_k"), str(m.get("neden") or ""))


def oku_model(path: Path | None = None) -> dict | None:
    try:
        d = json.loads(Path(path or MODEL_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _gecerli(d: dict | None) -> tuple[np.ndarray, np.ndarray] | None:
    """Aktif + doğru sürüm/parmak izi + biçim → (W, b); yoksa None."""
    if not d or not d.get("aktif") or d.get("surum") != VERSION \
            or d.get("parmak_izi") != parmak_izi():
        return None
    try:
        W = np.asarray(d["W"], dtype=float)
        b = np.asarray(d["b"], dtype=float)
    except (KeyError, TypeError, ValueError):
        return None
    if W.shape != (len(INPUTS), _K) or b.shape != (_K,) or not np.all(np.isfinite(W)) \
            or not np.all(np.isfinite(b)):
        return None
    return W, b


class KisiselModel:
    """ses_analiz'in ``personal`` nesnesi: ``adjust(dsp, model, features)`` → dict | None.

    Dosyayı en çok ``check_s`` sn'de bir yoklar (mtime); yoksa/pasifse/eskiyse None
    döner (= taban füzyon). Hiç hata fırlatmaz.
    """

    def __init__(self, path: Path | None = None, check_s: float = CHECK_S,
                 clock: Callable[[], float] = time.monotonic):
        self.path = Path(path or MODEL_FILE)
        self.check_s = float(check_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._checked: float | None = None
        self._mtime: float | None = None
        self._wb: tuple[np.ndarray, np.ndarray] | None = None

    def _refresh(self) -> None:
        now = self._clock()
        if self._checked is not None and now - self._checked < self.check_s:
            return
        self._checked = now
        try:
            mt = self.path.stat().st_mtime_ns
        except OSError:
            mt = None
        if mt == self._mtime:
            return
        self._mtime = mt
        self._wb = _gecerli(oku_model(self.path)) if mt is not None else None
        log.info("kişisel ses modeli %s", "etkin" if self._wb else "devre dışı")

    @property
    def aktif(self) -> bool:
        with self._lock:
            self._refresh()
            return self._wb is not None

    def adjust(self, pd_: dict, pm: dict, f: dict) -> dict[str, float] | None:
        try:
            with self._lock:
                self._refresh()
                wb = self._wb
            if wb is None or pm is None:
                return None
            return uygula(wb[0], wb[1], pd_, pm, f)
        except Exception as e:  # noqa: BLE001
            log.debug("kişisel model hatası: %s", e)
            return None


def yukle(path: Path | None = None) -> KisiselModel:
    """Yönlendirici: ``AudioAnalyzer(model=…, personal=kisisel_model.yukle())``."""
    return KisiselModel(path)


def sifirla(path: Path | None = None) -> bool:
    p = Path(path or MODEL_FILE)
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False


# ============================================================================
#  5) CLI: kulak model [durum|egit|sifirla]
# ============================================================================

_USAGE = _("kullanım: kulak model [durum|egit|sifirla]\n"
           "  durum    örnek sayıları, son eğitim, çapraz doğrulama kazancı, etkin mi\n"
           "  egit     şimdi eğit (yeterli düzeltme + iyileşme varsa etkinleşir)\n"
           "  sifirla  kişisel modeli sil (tahmin kayıtları kalır; taban füzyona dönülür)")


def _cv_satirlari(cv: dict) -> list[str]:
    tb, ks = cv["taban"], cv["kisisel"]
    satir = _("puan {puan:+d}  doğru {dogru}  yanlış {yanlis}  kapsama %{kapsama:.0f}  "
              "isabet %{isabet:.0f}")

    def sonuc(r: dict) -> str:
        return satir.format(puan=r["puan"], dogru=r["dogru"], yanlis=r["yanlis"],
                            kapsama=100 * r["kapsama"], isabet=100 * r["isabet"])
    return ["  " + _("çapraz doğrulama ({kat} kat, {n} güçlü örnek; doğru +1, yanlış −2):").format(
                kat=cv["kat"], n=tb["n"]),
            "    " + _("taban    {sonuc}").format(sonuc=sonuc(tb)),
            "    " + _("kişisel  {sonuc}").format(sonuc=sonuc(ks)),
            "    " + _("kazanç {kazanc:+d} (gereken ≥ {esik:.0f})").format(
                kazanc=cv["kazanc"], esik=cv["esik"])]


def _say_satiri(say: dict) -> str:
    return "  " + _("tahmin kaydı {kayit} (modelsiz {modelsiz}, etiketsiz {etiketsiz}) · güçlü "
                    "{guclu} · zayıf {zayif} · sanatçı {sanatci_guclu} güçlü / {sanatci} "
                    "toplam").format(**{k: say.get(k, 0) for k in (
                        "kayit", "modelsiz", "etiketsiz", "guclu", "zayif", "sanatci_guclu",
                        "sanatci")})


def run(argv: list[str] | None = None, out: Callable[[str], None] = print) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else "durum"
    if cmd in ("-h", "--help", "yardim", "yardım"):
        out(_USAGE)
        return 0
    if cmd == "sifirla":
        out(_("Kişisel model silindi; taban füzyon kullanılacak.") if sifirla()
            else _("Kişisel model zaten yok."))
        return 0
    if cmd == "egit":
        try:
            m = egit()
        except Exception as e:  # noqa: BLE001
            out(_("kulak model: eğitim başarısız: {hata}").format(hata=e))
            return 1
        out((_("Kişisel model ETKİN: {neden}") if m["aktif"]
             else _("Kişisel model devre dışı: {neden}")).format(neden=neden_metni(m)))
        out(_say_satiri(m["sayilar"]))
        if "cv" in m:
            for s in _cv_satirlari(m["cv"]):
                out(s)
        return 0
    if cmd == "durum":
        try:
            _orn, say = ornekler(oku_tahminler(), _read_jsonl(_rotated(FEEDBACK_FILE))
                                 + _read_jsonl(FEEDBACK_FILE), _safe_isim())
        except Exception as e:  # noqa: BLE001
            say = {}
            out("  " + _("(kayıtlar okunamadı: {hata})").format(hata=e))
        out(_("Kişisel ses modeli"))
        out(_say_satiri(say))
        need = max(0, MIN_STRONG - say.get("guclu", 0))
        if need:
            out("  " + _("etkinleşmek için en az {n} güçlü örnek daha gerekiyor").format(n=need))
        m = oku_model()
        if m is None:
            out("  " + _("model: yok (henüz eğitilmedi) — {komut}").format(komut="`kulak model egit`"))
            return 0
        etkin = _gecerli(m) is not None
        out("  " + (_("son eğitim: {zaman} · ETKİN — {neden}") if etkin
                    else _("son eğitim: {zaman} · devre dışı — {neden}")).format(
            zaman=m.get("egitim", "?"), neden=neden_metni(m)))
        if m.get("aktif") and not etkin:
            out("  " + _("(model dosyası bu sürümle uyumsuz: {komut} ile yeniden eğit)").format(
                komut="`kulak model egit`"))
        if isinstance(m.get("cv"), dict):
            for s in _cv_satirlari(m["cv"]):
                out(s)
        return 0
    out(_USAGE)
    return 2


def _safe_isim():
    try:
        return isim_etiketi_fabrikasi()
    except Exception:  # noqa: BLE001
        return None

