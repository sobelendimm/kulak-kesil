"""Kör A/B dinleme testi: `kulak ab [--deneme N] [--sure S] [--profil muzik]`.

While something plays on the target EQ sink, N blind trials compare the current
personal tone ("Kişisel ton": bas / vokal / parlaklik, ±TON_LIMIT dB) with a
slightly changed one. Each trial changes ONE dimension by ±step, randomizes which
label ("1"/"2") is the changed side and alternates 1→2→1→2 (S s each) through
``eqgen.apply_tone(..., ramp_s=0.3, values=...)``. The listener answers
1 / 2 / = (fark yok) / t (tekrar) / q (bitir).

Pure logic (``Estimator``: trial planner + adaptive staircase estimator, and the
summary formatting) has no I/O; ``run()`` gets every side effect injected through
``Deps`` (apply function, kisisel module, status provider, input/output, sleep,
rng) so it is testable with fakes. The pre-test tone is ALWAYS restored on exit,
Ctrl-C, SIGTERM/SIGHUP or error (``apply_tone(profile, group, values=None)`` =
whatever kisisel.json holds, i.e. the old values unless the user saved).
Nothing is recorded; no audio is written anywhere.

Exit codes: 0 bitti (kaydedilsin/kaydedilmesin), 1 hata, 2 kullanım hatası ya da
ön koşul yok (kulaklık yok, çalan bir şey yok), 3 servis çalışmıyor (as every `kulak`
command; the message goes to stderr), 130 iptal.
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import signal
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from . import i18n
from .i18n import N_, _, h_
from .models import EQ_PROFILES, GROUP_LABELS, PROFILE_LABELS, sink_name

log = logging.getLogger("oto_eq.ab")

EXIT_OK, EXIT_ERR, EXIT_USAGE, EXIT_NO_SERVICE, EXIT_ABORT = 0, 1, 2, 3, 130

# Fallbacks; the real values come from oto_eq.kisisel when it is importable.
TON_KEYS: tuple[str, ...] = ("bas", "vokal", "parlaklik")
TON_LIMIT = 3.0
STEPS: tuple[float, ...] = (1.5, 1.0, 0.5)     # adaptive step ladder (dB)
RAMP_S = 0.3
DEFAULT_TRIALS = 10
DEFAULT_SECONDS = 6.0
TRIALS_RANGE = (1, 30)
SECONDS_RANGE = (2.0, 20.0)

DIM_LABELS = {"bas": _("Bas sıcaklığı"), "vokal": _("Vokal netliği"), "parlaklik": _("Parlaklık")}
# (more, less) wording per dimension, one whole sentence per size: (çok az, biraz, belirgin)
_DIM_WORDS = {
    "bas": ((N_("bas çok az daha sıcak / dolgun"), N_("bas biraz daha sıcak / dolgun"),
             N_("bas belirgin şekilde daha sıcak / dolgun")),
            (N_("bas çok az daha sıkı / hafif"), N_("bas biraz daha sıkı / hafif"),
             N_("bas belirgin şekilde daha sıkı / hafif"))),
    "vokal": ((N_("vokaller çok az daha önde / net"), N_("vokaller biraz daha önde / net"),
               N_("vokaller belirgin şekilde daha önde / net")),
              (N_("vokaller çok az daha yumuşak / geride"), N_("vokaller biraz daha yumuşak / geride"),
               N_("vokaller belirgin şekilde daha yumuşak / geride"))),
    "parlaklik": ((N_("tizler çok az daha parlak"), N_("tizler biraz daha parlak"),
                   N_("tizler belirgin şekilde daha parlak")),
                  (N_("tizler çok az daha yumuşak"), N_("tizler biraz daha yumuşak"),
                   N_("tizler belirgin şekilde daha yumuşak"))),
}
_DIM_WORDS_OTHER = ((N_("{boyut} çok az arttı"), N_("{boyut} biraz arttı"),
                     N_("{boyut} belirgin şekilde arttı")),
                    (N_("{boyut} çok az azaldı"), N_("{boyut} biraz azaldı"),
                     N_("{boyut} belirgin şekilde azaldı")))
# confidence key → label (behaviour compares the key, never the translated label)
_CONF_LABELS = {"yok": N_("veri yok"), "yuksek": N_("yüksek"), "orta": N_("orta"),
                "dusuk": N_("düşük")}

ANSWERS = ("1", "2", "=")
_EQUAL_ALIASES = {"=", "0", "e", "esit", "eşit", "fark yok", "f"}
_QUIT_ALIASES = {"q", "ç", "c", "cik", "çık", "bitir"}
_REPLAY_ALIASES = {"t", "tekrar", "r"}


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------
def round_half(x: float) -> float:
    """Round to the 0.5 dB grid (symmetric, no banker's rounding)."""
    r = math.floor(abs(float(x)) * 2 + 0.5) / 2
    return math.copysign(r, x) if r else 0.0


def clamp(x: float, limit: float = TON_LIMIT) -> float:
    return max(-limit, min(limit, float(x)))


def fmt_db(v: float) -> str:
    v = round_half(v)
    return "0.0" if v == 0 else f"{v:+.1f}"


def describe_change(dim: str, before: float, after: float) -> str:
    """Turkish words for a change, e.g. 'bas biraz daha sıcak / dolgun'."""
    d = round_half(after - before)
    if d == 0:
        return _("değişmedi")
    a = abs(d)
    size = 0 if a <= 0.5 else (1 if a <= 1.5 else 2)
    more, less = _DIM_WORDS.get(dim, _DIM_WORDS_OTHER)
    return _((more if d > 0 else less)[size]).format(boyut=dim)


def confidence_key(consistent: int, total: int) -> str:
    """'yok' / 'yuksek' / 'orta' / 'dusuk' (language independent)."""
    if total <= 0:
        return "yok"
    ratio = consistent / total
    if total >= 3 and ratio >= 0.75:
        return "yuksek"
    if total >= 2 and ratio >= 0.6:
        return "orta"
    return "dusuk"


def confidence_label(consistent: int, total: int) -> str:
    return _(_CONF_LABELS[confidence_key(consistent, total)])


def fold(raw: str | None) -> str:
    """Answer key: lower case without accents, Turkish-aware ("ÇIK" → "cik", "EŞİT" →
    "esit"; str.lower() made "ÇIK" "çik" and "EŞİT" "eşi̇t", neither an alias)."""
    s = (raw or "").strip().replace("İ", "i").replace("I", "i").replace("ı", "i").lower()
    s = unicodedata.normalize("NFKD", s)
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def parse_answer(raw: str | None) -> str | None:
    """'1' / '2' / '=' / 't' (replay) / 'q' (quit); None = invalid."""
    s = fold(raw)
    if s in ("1", "2"):
        return s
    if s in {fold(a) for a in _EQUAL_ALIASES}:
        return "="
    if s in {fold(a) for a in _REPLAY_ALIASES}:
        return "t"
    if s in {fold(a) for a in _QUIT_ALIASES}:
        return "q"
    return None


# --------------------------------------------------------------------------
# trial planner + estimator (pure)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Trial:
    number: int
    dim: str
    unchanged: dict          # full tone dict (current estimate)
    changed: dict            # same, with ``dim`` moved by ``delta``
    delta: float
    changed_label: str       # "1" or "2"
    step: float

    def side(self, label: str) -> dict:
        return dict(self.changed if label == self.changed_label else self.unchanged)

    def sequence(self) -> list[tuple[str, dict]]:
        """Playback order 1 → 2 → 1 → 2."""
        return [(lab, self.side(lab)) for lab in ("1", "2", "1", "2")]


@dataclass
class DimState:
    value: float
    sign: int = 1               # direction of the next probe
    step_idx: int = 0
    pref_dir: int = 0           # last preferred direction (+1 / -1), 0 = none yet
    trials: int = 0
    equal: int = 0

    @property
    def step(self) -> float:
        return STEPS[min(self.step_idx, len(STEPS) - 1)]

    def shrink(self) -> None:
        self.step_idx = min(self.step_idx + 1, len(STEPS) - 1)


@dataclass
class Record:
    dim: str
    a: float                 # unchanged value of dim
    b: float                 # changed value of dim
    answer: str              # "changed" | "unchanged" | "equal"

    @property
    def preferred(self) -> float | None:
        return {"changed": self.b, "unchanged": self.a}.get(self.answer)

    @property
    def rejected(self) -> float | None:
        return {"changed": self.a, "unchanged": self.b}.get(self.answer)


class Estimator:
    """Adaptive per-dimension staircase on the 0.5 dB grid.

    - plan(): the dimension with the fewest trials (random tie-break), probe
      value = estimate ± step (flipped at the ±limit), random changed label.
    - record(): preferred changed side → the estimate moves there; preferred
      unchanged side → the next probe goes the other way; a reversal of the
      preferred direction ("contradictory" answer) or "=" shrinks the step
      (1.5 → 1.0 → 0.5 dB).
    """

    def __init__(self, start: dict, rng: random.Random | None = None,
                 keys: tuple[str, ...] = TON_KEYS, limit: float = TON_LIMIT):
        self.rng = rng or random.Random()
        self.keys = tuple(keys)
        self.limit = float(limit)
        self.start = {k: round_half(clamp(float(start.get(k, 0.0) or 0.0), self.limit))
                      for k in self.keys}
        self.dims = {k: DimState(self.start[k], sign=self.rng.choice((1, -1))) for k in self.keys}
        self.history: list[Record] = []
        self._n = 0

    def values(self) -> dict:
        return {k: self.dims[k].value for k in self.keys}

    def _probe(self, st: DimState) -> float | None:
        for sign in (st.sign, -st.sign):
            cand = round_half(clamp(st.value + sign * st.step, self.limit))
            if cand != st.value:
                st.sign = sign
                return cand
        return None

    def plan(self) -> Trial | None:
        order = sorted(self.keys, key=lambda k: (self.dims[k].trials, self.rng.random()))
        for dim in order:
            st = self.dims[dim]
            cand = self._probe(st)
            if cand is None:
                continue
            self._n += 1
            base = self.values()
            changed = dict(base)
            changed[dim] = cand
            return Trial(number=self._n, dim=dim, unchanged=base, changed=changed,
                         delta=round_half(cand - st.value),
                         changed_label=self.rng.choice(("1", "2")), step=st.step)
        return None

    def record(self, trial: Trial, answer: str) -> str:
        """answer '1'/'2'/'=' → outcome 'changed'/'unchanged'/'equal'."""
        if answer not in ANSWERS:
            raise ValueError(_("geçersiz yanıt: {yanit}").format(yanit=repr(answer)))
        st = self.dims[trial.dim]
        a, b = trial.unchanged[trial.dim], trial.changed[trial.dim]
        mv = 1 if b > a else -1
        st.trials += 1
        if answer == "=":
            outcome = "equal"
            st.equal += 1
            st.shrink()
            st.sign = -mv                     # try the other side next
        else:
            outcome = "changed" if answer == trial.changed_label else "unchanged"
            new_dir = mv if outcome == "changed" else -mv
            if st.pref_dir and new_dir != st.pref_dir:
                st.shrink()                   # reversal / contradictory answer
            st.pref_dir = new_dir
            st.sign = new_dir
            if outcome == "changed":
                st.value = b
        self.history.append(Record(trial.dim, a, b, outcome))
        return outcome

    def confidence(self, dim: str) -> tuple[int, int]:
        """(consistent, total) non-'=' answers vs the final estimate; ties are neutral."""
        final = self.dims[dim].value
        ok = total = 0
        for r in self.history:
            if r.dim != dim or r.preferred is None:
                continue
            dp, dr = abs(r.preferred - final), abs(r.rejected - final)
            if dp == dr:
                continue
            total += 1
            ok += dp < dr
        return ok, total

    def answered(self) -> int:
        return len(self.history)


# --------------------------------------------------------------------------
# summary (pure)
# --------------------------------------------------------------------------
def target_text(profile: str, group: str) -> str:
    p = PROFILE_LABELS.get(profile, profile)
    if group:
        return _("{profil} · {tur} için").format(profil=p, tur=GROUP_LABELS.get(group, group))
    return _("Genel (tüm profiller) için")


def format_summary(est: Estimator, before: dict) -> str:
    rows = [(_("Boyut"), _("Önce"), _("Sonra"), _("Güven"), _("Yorum"))]
    for k in est.keys:
        b, a = before.get(k, 0.0), est.dims[k].value
        ok, total = est.confidence(k)
        eq = est.dims[k].equal
        conf = confidence_label(ok, total)
        if total:
            conf += f" ({ok}/{total})"
        if eq:
            conf += f", ={eq}"
        rows.append((DIM_LABELS.get(k, k), fmt_db(b) + " dB", fmt_db(a) + " dB", conf,
                     describe_change(k, b, a)))
    widths = [max(i18n.genislik(r[i]) for r in rows) for i in range(4)]   # cells (CJK = 2)
    lines = []
    for i, r in enumerate(rows):
        lines.append("  " + "  ".join(i18n.sola(c, w) for c, w in zip(r[:4], widths)) + "  " + r[4])
        if i == 0:
            lines.append("  " + "─" * (sum(widths) + 2 * 4 + i18n.genislik(rows[0][4])))
    return "\n".join(lines)


def changed_dims(est: Estimator, before: dict) -> list[str]:
    return [k for k in est.keys if round_half(est.dims[k].value) != round_half(before.get(k, 0.0))]


def save_values(kisisel: Any, data: dict, profile: str, group: str,
                before: dict, after: dict, keys: tuple[str, ...]) -> dict:
    """Stored values that make the effective tone == ``after`` (unchanged dims keep
    their stored value). group "" → genel values; else tur[profile:group] offsets."""
    genel = (data or {}).get("genel") or {}
    if not group:
        return {k: round_half(after[k]) if after[k] != before.get(k) else
                round_half(float(genel.get(k, 0.0) or 0.0)) for k in keys}
    tur = ((data or {}).get("tur") or {}).get(f"{profile}:{group}") or {}
    out = {}
    for k in keys:
        if round_half(after[k]) != round_half(before.get(k, 0.0)):
            out[k] = round_half(after[k] - float(genel.get(k, 0.0) or 0.0))
        else:
            out[k] = round_half(float(tur.get(k, 0.0) or 0.0))
    return out


# --------------------------------------------------------------------------
# I/O + orchestration
# --------------------------------------------------------------------------
class Aborted(KeyboardInterrupt):
    """Ctrl-C / SIGTERM / SIGHUP / EOF on input."""


def _default_out(s: str = "", end: str = "\n") -> None:
    print(s, end=end, flush=True)


def _default_err(s: str = "", end: str = "\n") -> None:
    print(s, end=end, file=sys.stderr, flush=True)


def _default_status() -> dict:
    from . import ipc
    return ipc.request({"cmd": "status"}, timeout=2.0)


def _default_apply(profile: str, group: str = "", ramp_s: float = RAMP_S,
                   values: dict | None = None) -> bool:
    from . import eqgen
    return bool(eqgen.apply_tone(profile, group, ramp_s=ramp_s, values=values))


def _default_wait(timeout: float) -> None:
    """Let a running tone ramp finish (eqgen ramps run in daemon threads: a CLI that exits
    at once leaves the sink half-way, e.g. on the unsaved tone after "h")."""
    from . import eqgen
    waiter = getattr(eqgen, "wait_ramps", None)
    if callable(waiter):
        waiter(timeout)


def _default_kisisel():
    from . import kisisel
    return kisisel


@dataclass
class Deps:
    apply_tone: Callable[..., bool] = _default_apply
    wait_ramps: Callable[[float], Any] = _default_wait
    kisisel: Callable[[], Any] = _default_kisisel        # lazy loader → module-like
    status: Callable[[], dict] = _default_status
    out: Callable[..., None] = _default_out
    err: Callable[..., None] = _default_err              # "service not running" (exit 3)
    ask: Callable[[str], str] = input
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random | None = None
    install_signals: bool = True


class _ArgError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # Turkish, no sys.exit inside run()
        for en, tr in (("unrecognized arguments", _("tanınmayan argümanlar")),
                       ("invalid choice", _("geçersiz seçim")), ("choose from", _("seçenekler")),
                       ("invalid int value", _("geçersiz tam sayı")),
                       ("invalid float value", _("geçersiz sayı")),
                       ("expected one argument", _("bir değer bekleniyor")),
                       ("argument ", _("argüman") + " ")):
            message = message.replace(en, tr)
        raise _ArgError(message)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="kulak ab", add_help=False,
                description=_("Kör A/B: kişisel tonu (bas / vokal / parlaklık) kulakla ayarla."))
    p.add_argument("-h", "--help", action="store_true", help=h_(_("bu yardımı göster")))
    p.add_argument("--deneme", type=int, default=DEFAULT_TRIALS,
                   help=h_(_("deneme sayısı (varsayılan {n})").format(n=DEFAULT_TRIALS)))
    p.add_argument("--sure", type=lambda s: float(str(s).replace(",", ".")),
                   default=DEFAULT_SECONDS,
                   help=h_(_("her taraf kaç saniye çalsın (varsayılan {sn:g})").format(sn=DEFAULT_SECONDS)))
    p.add_argument("--profil", choices=EQ_PROFILES, default="muzik",
                   help=h_(_("{olanlar} (varsayılan {profil})").format(
                       olanlar=" | ".join(EQ_PROFILES), profil="muzik")))
    return p


def check_preconditions(st: Any, profile: str) -> str | None:
    """None = OK, else a Turkish reason (exit 2)."""
    plab = PROFILE_LABELS.get(profile, profile)
    if not isinstance(st, dict) or not st.get("ok", True):
        err = st.get("error") if isinstance(st, dict) else None
        if err:
            return _("Kulak Kesil servisinden durum alınamadı: {hata}.").format(hata=err)
        return _("Kulak Kesil servisinden durum alınamadı.")
    if not st.get("active"):
        return _("Kulaklık varsayılan çıkış değil, EQ devrede değil. "
                 "HS80'i varsayılan çıkış yapıp tekrar deneyin.")
    if st.get("mode") == "off":
        return _("Kulak Kesil kapalı modda. Önce '{komut}' ile açın.").format(komut="kulak oto")
    if not st.get("sinks_ready", True):
        return _("EQ çıkışları hazır değil ({komut}).").format(
            komut="systemctl --user restart oto-eq-sinks")
    target = sink_name(profile)
    if not any(s.get("sink") == target for s in st.get("streams") or [] if isinstance(s, dict)):
        return _("{profil} çıkışında çalan bir şey yok. Önce {profil} profiline giden bir şey "
                 "açın (ör. bir şarkı), sonra tekrar deneyin.").format(profil=plab)
    if not st.get("dominant"):
        return _("{profil} çıkışındaki ses duraklatılmış görünüyor. Oynatmayı başlatıp tekrar "
                 "deneyin.").format(profil=plab)
    return None


def _group_of(st: Any, profile: str) -> str:
    if not isinstance(st, dict):
        return ""
    v = (st.get("variants") or {}).get(profile) or {}
    g = v.get("group") if isinstance(v, dict) else ""
    return g if isinstance(g, str) else ""


def _install_signals() -> dict:
    def handler(signum, frame):
        raise Aborted()
    old = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            old[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):       # not the main thread
            pass
    return old


def _restore_signals(old: dict) -> None:
    for sig, h in old.items():
        try:
            signal.signal(sig, h)
        except (ValueError, OSError):
            pass


def _usage_text(p: argparse.ArgumentParser) -> str:
    return (p.format_help().replace("usage:", _("kullanım:"))
            .replace("options:", _("seçenekler:")).replace("optional arguments:", _("seçenekler:")))


def run(argv: list[str] | None = None, deps: Deps | None = None) -> int:
    d = deps or Deps()
    out = d.out
    parser = build_parser()
    try:
        a = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    except _ArgError as e:
        out(_("kulak ab: hata: {hata}").format(hata=e))
        return EXIT_USAGE
    if a.help:
        out(_usage_text(parser))
        return EXIT_OK
    if not TRIALS_RANGE[0] <= a.deneme <= TRIALS_RANGE[1]:
        out(_("kulak ab: deneme sayısı {en_az}–{en_cok} arasında olmalı").format(
            en_az=TRIALS_RANGE[0], en_cok=TRIALS_RANGE[1]))
        return EXIT_USAGE
    if not SECONDS_RANGE[0] <= a.sure <= SECONDS_RANGE[1]:
        out(_("kulak ab: süre {en_az:g}–{en_cok:g} saniye arasında olmalı").format(
            en_az=SECONDS_RANGE[0], en_cok=SECONDS_RANGE[1]))
        return EXIT_USAGE
    profile = a.profil

    # --- preconditions ------------------------------------------------------
    try:
        st = d.status()
    except (ConnectionError, OSError, TimeoutError) as e:
        log.debug("durum alınamadı: %s", e)
        d.err(_("Kulak Kesil servisi (oto-eq) çalışmıyor — {komut}").format(
            komut="systemctl --user start oto-eq"))
        return EXIT_NO_SERVICE
    why = check_preconditions(st, profile)
    if why:
        out(why)
        return EXIT_USAGE
    try:
        kis = d.kisisel()
    except ImportError:
        out(_("Kişisel ton modülü (oto_eq.kisisel) henüz kurulu değil."))
        return EXIT_ERR
    keys = tuple(getattr(kis, "TON_KEYS", TON_KEYS))
    limit = float(getattr(kis, "TON_LIMIT", TON_LIMIT))
    group = _group_of(st, profile)
    try:
        data = kis.load()
        before = {k: round_half(float(v)) for k, v in kis.effective(data, profile, group).items()
                  if k in keys}
    except Exception as e:  # corrupt file etc.
        log.debug("kişisel ton okunamadı", exc_info=True)
        out(_("Kişisel ton ayarı okunamadı: {hata}").format(hata=e))
        return EXIT_ERR
    for k in keys:
        before.setdefault(k, 0.0)

    est = Estimator(before, rng=d.rng, keys=keys, limit=limit)
    plab = PROFILE_LABELS.get(profile, profile)
    glab = GROUP_LABELS.get(group, group) if group else GROUP_LABELS[""]
    out(_("Kör A/B · {profil} · tür: {tur}").format(profil=plab, tur=glab))
    out(_("{n} deneme. Her denemede iki ses çalar: 1 → 2 → 1 → 2 (her biri {sn:g} sn).").format(
        n=a.deneme, sn=a.sure))
    out(_("Hangisi kulağınıza daha iyi geliyorsa onu seçin: 1, 2 ya da = (fark yok)."))
    out(_("t = tekrar dinle, q = bitir, Ctrl+C = iptal (eski ayar geri gelir)."))
    out(_("Mevcut ton: {ton}").format(ton=", ".join(
        _("{boyut} {db} dB").format(boyut=DIM_LABELS.get(k, k), db=fmt_db(before[k])) for k in keys)))

    old_signals = _install_signals() if d.install_signals else {}
    saved = False
    aborted = False
    restored = True
    result = EXIT_OK
    try:
        try:
            start = d.ask(_("Başlamak için Enter (q = vazgeç): "))
        except EOFError:
            raise Aborted()
        if parse_answer(start) == "q":
            out(_("Vazgeçildi, hiçbir şey değişmedi."))
            return EXIT_OK

        def apply(values: dict) -> None:
            if not d.apply_tone(profile, group, ramp_s=RAMP_S, values=dict(values)):
                raise RuntimeError(_("ton uygulanamadı (bu profilde kişisel ton kapalı olabilir: "
                                     "profiller.toml kisisel_ton) ya da EQ çıkışı hazır değil"))

        apply(before)
        group_note_shown = False
        while est.answered() < a.deneme:
            trial = est.plan()
            if trial is None:
                break
            # light live check: still playing? genre changed?
            try:
                cur = d.status()
            except Exception:
                cur = None
            if isinstance(cur, dict):
                if check_preconditions(cur, profile):
                    out(_("{profil} çıkışında çalma durdu gibi görünüyor.").format(profil=plab))
                    try:
                        r = d.ask(_("Devam etmek için Enter, bitirmek için q: "))
                    except EOFError:
                        raise Aborted()
                    if parse_answer(r) == "q":
                        break
                g2 = _group_of(cur, profile)
                if g2 != group and not group_note_shown:
                    group_note_shown = True
                    out(_("Not: tür değişti ({eski} → {yeni}); sonuç yine {eski} için "
                          "kaydedilir.").format(eski=glab, yeni=GROUP_LABELS.get(g2, g2)
                                                or GROUP_LABELS[""]))
            ans = None
            while ans is None:
                out(_("Deneme {no}/{n} · odak: {boyut} ·").format(
                    no=est.answered() + 1, n=a.deneme, boyut=DIM_LABELS.get(trial.dim, trial.dim)),
                    end="")
                for label, values in trial.sequence():
                    out(f" ▶{label}", end="")
                    apply(values)
                    d.sleep(a.sure)
                out()
                while True:
                    try:
                        raw = d.ask(_("Hangisi daha iyi? [1 / 2 / = fark yok / t tekrar / q bitir]: "))
                    except EOFError:
                        raise Aborted()
                    p = parse_answer(raw)
                    if p is not None:
                        break
                    out(_("Lütfen 1, 2, =, t ya da q yazın."))
                if p == "t":
                    continue
                ans = p
            if ans == "q":
                break
            est.record(trial, ans)
            apply(est.values())        # between trials: current estimate

        # --- result -----------------------------------------------------------
        out()
        if est.answered() == 0:
            out(_("Hiç yanıt verilmedi; hiçbir şey değişmedi."))
            return EXIT_OK
        after = est.values()
        out(_("Sonuç ({n} yanıt) · {hedef}:").format(n=est.answered(), hedef=target_text(profile, group)))
        out(format_summary(est, before))
        changed = changed_dims(est, before)
        if not changed:
            out(_("Değişiklik yok: mevcut ton size uygun görünüyor."))
            return EXIT_OK
        low = [DIM_LABELS.get(k, k) for k in changed
               if confidence_key(*est.confidence(k)) in ("dusuk", "yok")]
        if low:
            out(_("Düşük güven: {boyutlar} (isterseniz testi tekrarlayın).").format(
                boyutlar=", ".join(low)))
        apply(after)
        out(_("Şu an yeni ton çalıyor."))
        # the words of the user's language are shown and accepted ("(yes/no)", "(ja/nein)",
        # "(oui/non)"…), the Turkish e/h and y/n keep working everywhere
        ev, ha = _("evet"), _("hayır")
        yes = {fold(w) for w in ("e", "evet", "y", "yes", ev, ev[:1])}
        no = {fold(w) for w in ("h", "hayır", "n", "no", ha, ha[:1], "")}
        while True:
            try:
                r = fold(d.ask(_("Kaydedilsin mi ({hedef})? ({evet}/{hayir}): ").format(
                    hedef=target_text(profile, group), evet=ev, hayir=ha)))
            except EOFError:
                raise Aborted()
            if r in yes:
                vals = save_values(kis, data, profile, group, before, after, keys)
                if group:
                    kis.set_tur(profile, group, **vals)
                else:
                    kis.set_genel(**vals)
                saved = True
                out(_("Kaydedildi."))
                break
            if r in no:
                out(_("Kaydedilmedi; önceki ton geri yüklendi."))
                break
            out(_("Lütfen “{evet}” ya da “{hayir}” yazın.").format(evet=ev, hayir=ha))
    except KeyboardInterrupt:
        aborted = True
        result = EXIT_ABORT
    except Exception as e:
        log.debug("A/B testi hatası", exc_info=True)
        out("\n" + _("Hata: {hata}").format(hata=e))
        result = EXIT_ERR
    finally:
        # values=None → what kisisel.json holds now (old tone, or the saved one)
        try:
            g_now = _group_of(d.status(), profile)
        except BaseException:
            g_now = group
        try:
            restored = bool(d.apply_tone(profile, g_now, ramp_s=RAMP_S, values=None))
        except BaseException as e:   # never let restore errors hide the real exit
            log.debug("ton geri yüklenemedi: %s", e)
            restored = False
        if restored:
            try:
                d.wait_ramps(3.0)    # the process exits next: let the restore ramp finish
            except BaseException as e:
                log.debug("rampa beklenemedi: %s", e)
        if not restored:
            out(_("Uyarı: önceki ton geri yüklenemedi; '{komut}' ile kontrol edin.").format(komut="kulak ton"))
        _restore_signals(old_signals)
    if aborted:
        out("\n" + (_("İptal edildi.") if saved or not restored
                     else _("İptal edildi. Önceki ton geri yüklendi.")))
    return result


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
