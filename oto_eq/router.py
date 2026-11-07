"""Routing daemon ("beyin"): decides per playback stream which EQ sink it goes to.

Run: ``python3 -m oto_eq.router`` (log level from env ``OTO_EQ_LOG``).

Threading: the pulse event listener, the MPRIS watcher and IPC threads only
*enqueue*; one worker thread owns all routing state and is the only caller of
``pulse.*``.  IPC reads a status snapshot guarded by a lock.

Policy: docs/CONTRACT.md (A3) and docs/NOTES-router.md.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, i18n, kulaklik
from .i18n import N_, _
from .ipc import validate_mode, validate_variant
from .models import (
    GROUP_LABELS,
    VARIANT_PROFILES,
    ContentId,
    variant_groups,
    ALL_SINK_PROFILES,
    EQ_PROFILES,
    FLAT_PROFILE,
    PROFILE_LABELS,
    PROFILE_PRIORITY,
    SINK_PREFIX,
    Category,
    Decision,
    Event,
    Stream,
    is_eq_sink,
    sink_name,
)

log = logging.getLogger("oto_eq.router")

# kulaklik_desen default: FORCES a match on top of the automatic detection (CHANGE 7)
DEFAULT_HEADSET_PATTERN = "(?i)hs80"
HS80_PATTERN = "(?i)hs80"        # HS80 takibi (wired <-> wireless) only ever follows HS80 nodes
CORRECTION_RETRY = 30.0          # s: a failed device-correction ramp is retried this late
NO_EQ_CATEGORIES = frozenset({Category.VOICE, Category.SYSTEM})

# timings (seconds)
NEW_STREAM_DELAY = 0.02      # new sink-input -> scan (bounded, not extended by bursts)
COALESCE_DELAY = 0.05        # other relevant pulse events
CHANGE_MIN_GAP = 0.25        # sink-input change/remove: at most ~4 scans/s from these
MPRIS_DEBOUNCE = 1.0         # quiet time after the last MPRIS change
MPRIS_MAX_DELAY = 3.0        # ... but never later than this after the first one
PERIODIC_INTERVAL = 10.0     # safety-net rescan
FLAP_MIN_INTERVAL = 2.0      # never move the same stream more often than this
FLAP_WINDOW = 60.0           # if a stream needed FLAP_MAX_MOVES moves within this window
FLAP_MAX_MOVES = 5           # ... something keeps undoing us -> leave it alone for
FLAP_PARK = 60.0             # ... this long
BACKOFF_MIN = 0.5            # pulse error backoff (doubles up to BACKOFF_MAX)
BACKOFF_MAX = 10.0
COMMAND_WAIT = 1.0           # IPC thread waits this long for the worker (< every client
                             # timeout: GUI 1.5 s, CLI 2 s, so "bekliyor" reaches them)
BACKGROUND_DESKTOP_PROP = "deskhand.sanal"  # stream property set by Deskhand's invisible desktop
KISMA_STATUS_WAIT = 0.05     # status(): max wait for the ducker's lock (its tick runs pactl)
UNLINKED_RECHECK = 0.03      # a stream not linked to any sink yet: look again this soon …
UNLINKED_WINDOW = 1.0        # … for at most this long after we first saw it unlinked
RESTORE_BUDGET = 2.0         # stop(): max time spent moving streams back to the default
STATUS_STALE_AFTER = 3       # consecutive pulse errors before the status is blanked
VERIFY_DELAY = 0.2           # re-check a move this soon (a pactl move can silently no-op)
VERIFY_WINDOW = 1.5          # an unapplied move is retried once within this window
HS80_WINDOW = 30.0           # HS80 takibi: the other mode must appear within this long
HS80_SAFE_VOLUME = 26214     # 40 % (raw, 65536 = 100 %) if the old volume is unknown
HS80_VOL_REFRESH = 30.0      # re-read the remembered volume at least this often
HS80_VOL_DEBOUNCE = 0.3      # sink/source "change" events -> volume re-read after this
HS80_SOURCE_POLL = 5.0       # sources are checked at most this often (unless events)


class MetadataMover:
    """Move a stream by writing WirePlumber's ``target.object`` metadata.

    Why not ``pactl move-sink-input``: measured (NOTES-router.md, "snap-back"),
    it exits 0 but silently does nothing in about 1/180 moves when idle and 38/180
    while other pactl clients are busy. pw-metadata talks to PipeWire natively:
    0/180 moves were lost under the same load. Target = the sink *name*
    (WP 0.5 accepts name or serial); the default sink = ``-1`` (follow the default,
    and WP forgets the saved restore target, exactly like pactl's move to the
    default). The stream node is ``props["object.id"]``. Without it, or when
    pw-metadata fails, ``fallback(index, sink)`` (``pulse.move_stream``) is used.
    """

    def __init__(self, fallback: Callable[[int, str], bool] | None = None,
                 timeout: float = 2.0, run: Callable[..., Any] = subprocess.run):
        self.fallback = fallback
        self.timeout = timeout
        self._run = run

    def __call__(self, stream: Stream, sink: str, to_default: bool) -> bool:
        nid = str((stream.props or {}).get("object.id", "")).strip()
        if nid.isdigit():
            value, typ = ("-1", "Spa:Id") if to_default else (sink, "Spa:String")
            cmd = ["pw-metadata", "-n", "default", "--", nid, "target.object", value, typ]
            try:
                p = self._run(cmd, env=config.C_ENV, capture_output=True, text=True,
                              timeout=self.timeout)
                if p.returncode == 0:
                    return True
                log.debug("pw-metadata başarısız (%s): %s", p.returncode,
                          (p.stderr or "").strip()[:200])
            except (OSError, subprocess.SubprocessError) as e:
                log.debug("pw-metadata çalıştırılamadı: %s", e)
        if self.fallback is not None:
            return bool(self.fallback(stream.index, sink))
        return False


class ModeStore:
    """Persists the mode as JSON: ``{"mode": "fixed", "profile": "cs2", "variants": {...}}``.

    ``variants`` (CHANGE 3): ``{"muzik": {"mode": "auto"|"fixed", "group": str|null}, ...}``.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else Path(config.MODE_FILE)

    def load(self) -> tuple[str, str | None]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return "auto", None
        except (OSError, ValueError) as e:
            log.warning("mod dosyası okunamadı (%s): %s → oto", self.path, e)
            return "auto", None
        try:
            if not isinstance(data, dict):
                raise ValueError("JSON nesnesi değil")
            return validate_mode(data.get("mode"), data.get("profile"))
        except ValueError as e:
            log.warning("mod dosyası geçersiz (%s): %s → oto", self.path, e)
            return "auto", None

    def _raw(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", "utf-8")
        os.replace(tmp, self.path)

    def save(self, mode: str, profile: str | None) -> None:
        data = self._raw()                       # keeps the "variants" key
        data.update({"mode": mode, "profile": profile})
        self._write(data)

    def load_variants(self) -> dict:
        v = self._raw().get("variants")
        return v if isinstance(v, dict) else {}

    def save_variants(self, variants: dict) -> None:
        data = self._raw()
        data.setdefault("mode", "auto")
        data.setdefault("profile", None)
        data["variants"] = variants
        self._write(data)


class _HeadsetFollower:
    """HS80 takibi state for one direction ("sink" or "source"); worker-owned."""

    def __init__(self, kind: str):
        self.kind = kind
        self.name: str | None = None        # last HS80 node we saw in use
        self.vol: int | None = None         # its last known volume (raw)
        self.vol_at = float("-inf")
        self.lost_at: float | None = None   # when ``name`` disappeared
        self.lost_name: str | None = None
        self.fallback: str | None = None    # default WP picked after the loss
        self.cancelled = False              # the user picked another default -> hands off
        self.dirty_at: float | None = None  # volume change event pending
        self.in_use = False                 # ``name`` was the default (or behind the
                                            # virtual mic) at the last check

    def reset_loss(self) -> None:
        self.lost_at = self.lost_name = self.fallback = None
        self.cancelled = False


_KIND_TR = {"sink": "çıkış", "source": "mikrofon"}


def _pct_n(raw: int) -> int:
    return round(raw * 100 / 65536)


# HS80 follower notes (router status "hs80_takip_son", shown in the GUI): one whole msgid
# per output kind; the journal gets the Turkish text, the status the translated one.
_HS80_MSG = {
    "elle": {"sink": N_("çıkış: varsayılan elle değiştirildi ({ad}), dokunulmuyor"),
             "source": N_("mikrofon: varsayılan elle değiştirildi ({ad}), dokunulmuyor")},
    "ses_yok": {"sink": N_("çıkış: {ad} sesi ayarlanamadı → varsayılan değiştirilmedi"),
                "source": N_("mikrofon: {ad} sesi ayarlanamadı → varsayılan değiştirilmedi")},
    "varsayilan_yok": {"sink": N_("çıkış: {ad} varsayılan yapılamadı"),
                       "source": N_("mikrofon: {ad} varsayılan yapılamadı")},
    "gecis": {"sink": N_("çıkış {eski} → {yeni} (ses %{yuzde})"),
              "source": N_("mikrofon {eski} → {yeni} (ses %{yuzde})")},
    "indirildi": {"sink": N_("çıkış {ad} sesi %{yuzde}'e indirildi"),
                  "source": N_("mikrofon {ad} sesi %{yuzde}'e indirildi")},
}


class _NullClassifier:
    """Used when kurallar.toml / the classifier cannot be loaded."""

    def classify(self, stream: Stream, players: list) -> Decision:
        return Decision(Category.UNKNOWN, None, _("kurallar yüklenemedi"))


def _profile_of(sink: str) -> str | None:
    if is_eq_sink(sink):
        p = sink[len(SINK_PREFIX):]
        if p in EQ_PROFILES:
            return p
    return None


FLAT_SINK = sink_name(FLAT_PROFILE)


def _sink_label(sink: str) -> str:
    if sink == FLAT_SINK:
        return PROFILE_LABELS.get(FLAT_PROFILE, FLAT_PROFILE)
    p = _profile_of(sink)
    return PROFILE_LABELS.get(p, p) if p else _("varsayılan çıkış")


def _mode_text(mode: str, profile: str | None) -> str:
    if mode == "fixed":
        return f"sabit ({PROFILE_LABELS.get(profile or '', profile)})"
    return {"auto": "otomatik", "off": "kapalı"}.get(mode, mode)


def _default_mic_retarget(source: str) -> bool:
    """Best-effort: point the virtual mic's capture at ``source`` (micgen, lazily)
    in a short-lived thread, so the worker never waits for pw-dump polling."""
    try:
        from . import micgen
    except Exception as e:
        log.debug("micgen yüklenemedi: %s", e)
        return False

    def run():
        try:
            # positional: the parameter is ``source_name`` (a ``target=`` keyword raised
            # TypeError, so the HS80 wired ↔ wireless mic switch never happened)
            ok = micgen.retarget(source, wait=2.0)
        except Exception as e:
            log.warning("HS80 takibi: mikrofon yeniden hedeflenemedi: %s", e)
            return
        if ok:
            log.info("HS80 takibi: sanal mikrofon girişi %s kaynağına bağlandı", source)
        else:
            log.warning("HS80 takibi: sanal mikrofon %s kaynağına bağlanamadı", source)
    threading.Thread(target=run, name="oto-eq-mic-retarget", daemon=True).start()
    return True                  # started; the thread logs the real result


_SRC_KULLANICI = "kullanıcı"  # tur.GenreResolver._override: the user's own assignment
VARIANT_SETTLE = 1.0         # a new track/game must stay this long before its variant is applied
VARIANT_LATE = 6.0           # wait this long after a boundary for an online answer (MusicBrainz
                             # measured 0.2-5 s); later = Genel for this content, answer cached
VARIANT_RAMP = 1.5           # eqgen.apply_variant ramp (s)
AUDIO_GUESS_FROM = 0.5       # CHANGE 4: audio for the window guess starts this long after the boundary
AUDIO_REMEMBER_AT = 25.0     # ~25 s into a track with no name-based result: a longer audio guess is
                             # stored for the artist (resolver.remember_audio), used at next tracks
AUDIO_REMEMBER_MIN = 10.0    # seconds of audio the remembered guess needs
_VARIANT_WORD = {"muzik": "müzik", "oyun": "oyun"}


def _same_content(a: ContentId | None, b: ContentId | None) -> bool:
    return a is not None and b is not None and a.kind == b.kind and a.key == b.key \
        and (a.track or "") == (b.track or "")


class _VariantState:
    """Per variant profile (muzik / oyun); worker-owned."""

    def __init__(self) -> None:
        self.mode = "auto"           # "auto" | "fixed"
        self.fixed = ""              # fixed group ("" = Genel)
        self.applied: str | None = None   # group live on the sink (None = unknown since start)
        self.applied_for = ""        # content display the applied group belongs to
        self.applied_key = ""        # its ContentId.key (UI: "assign a genre to this artist")
        self.source = ""
        self.current: ContentId | None = None   # current content on the sink
        self.boundary_at: float | None = None
        self.settled = False
        self.waiting = False         # request() sent for the current content
        self.sink_id: int | None = None   # sink index; a new one = sinks restarted (slots at 0 dB)
        # CHANGE 4 (audio analysis, muzik only)
        self.audio_wait = False      # name sources gave nothing: decide by audio at the window end
        self.audio_used = False      # no name-based result for this content (→ remember at ~25 s)
        self.remembered = False      # remember_audio done for this content
        self.offline = False         # the name lookup could not even start
        self.audio_last: dict | None = None   # last audio guess (GUI: "ses analizi ne dedi")

    def new_window(self) -> None:
        """A new decision window (boundary, user action, sinks restarted)."""
        self.settled = self.waiting = False
        self.audio_wait = self.audio_used = self.remembered = self.offline = False


class VariantTracker:
    """Genre variants ("tür alt profilleri", CONTRACT CHANGE 3) for muzik/oyun.

    Variants change ONLY at content boundaries: a new artist/game key, or a new
    track title (music). Every boundary must stay for VARIANT_SETTLE (rapid skips
    are ignored). Then a cached genre is applied at once; otherwise the resolver
    is asked, and its answer is applied only if it arrives within VARIANT_LATE
    of the boundary while the same content is still current. A later answer
    only fills the resolver cache, so it is used at the next boundary. MPRIS
    noise (position/metadata refresh, a briefly empty title) is no boundary.
    Manual mode (``set``) applies immediately; it is a user action.

    Worker-owned: ``update``/``on_result``/``set`` run on the router worker.
    ``schedule(at)`` asks the router for a scan; ``enqueue(item)`` posts a
    resolver answer back to the worker queue.
    """

    def __init__(self, content_id: Callable[..., ContentId | None] | None,
                 resolver: Any, apply: Callable[..., bool] | None,
                 schedule: Callable[[float], None], enqueue: Callable[[tuple], None],
                 proc_lookup: Callable[[Stream], Any] | None = None,
                 analyzer: Any = None,
                 audio_log: Callable[..., Any] | None = None):
        self.content_id = content_id
        # CHANGE 6 (D3): kisisel_model.kaydet_tahmin(cid, detail, asama) — numbers only, no audio
        self.audio_log = audio_log
        self.resolver = resolver
        self.apply_fn = apply
        self.schedule = schedule
        self.enqueue = enqueue
        self.proc_lookup = proc_lookup
        # CHANGE 4: ses_analiz.AudioAnalyzer (or None); used for muzik only, when the
        # name sources give nothing. ``audio_on`` = the "Ses analizi" setting.
        self.analyzer = analyzer
        self.audio_on = True
        self.state = {p: _VariantState() for p in VARIANT_PROFILES}

    # -- persistence ------------------------------------------------------

    def load(self, saved: Any) -> None:
        for p, s in self.state.items():
            v = saved.get(p) if isinstance(saved, dict) else None
            if not isinstance(v, dict):
                continue
            try:
                mode, group = validate_variant(p, "auto" if v.get("mode") != "fixed"
                                               else (v.get("group") or ""))
            except ValueError as e:
                log.warning("kayıtlı tür ayarı geçersiz (%s): %s → otomatik", p, e)
                continue
            s.mode, s.fixed = mode, group

    def saved(self) -> dict:
        return {p: {"mode": s.mode, "group": s.fixed if s.mode == "fixed" else None}
                for p, s in self.state.items()}

    # -- user action ------------------------------------------------------

    def set(self, profile: str, group: str, sink_present: bool, now: float) -> None:
        mode, group = validate_variant(profile, group)
        s = self.state[profile]
        s.mode, s.fixed = mode, group
        if mode == "fixed":
            if sink_present:
                self._apply(profile, group, "kullanıcı (sabit)",
                            s.current.display if s.current else "", s.current.key if s.current else "")
        elif s.current is not None:
            # back to auto = user action: decide for the current content right away
            s.boundary_at = now - VARIANT_SETTLE
            s.new_window()
            self.schedule(now)

    def settings_changed(self, settings: dict | None, title_hints: dict | None, now: float) -> None:
        """Rules/assignments reloaded (GUI "reload"): hand them to the resolver and
        re-decide the current content at once (a user action, like ``set``)."""
        r = self.resolver
        if r is not None:
            try:
                if settings is not None and hasattr(r, "set_settings"):
                    r.set_settings(settings)
                if title_hints is not None and hasattr(r, "set_title_hints"):
                    r.set_title_hints(title_hints)
                if hasattr(r, "invalidate"):
                    r.invalidate()          # cache file may have been cleared by the GUI
            except Exception as e:
                log.warning("tür ayarları yenilenemedi: %s", e)
        if isinstance(settings, dict) and isinstance(settings.get("ses_analizi"), bool):
            self.audio_on = settings["ses_analizi"]
            if not self.audio_on:
                self._audio_ensure(False)
        for s in self.state.values():
            if s.mode == "auto" and s.current is not None:
                s.boundary_at = now - VARIANT_SETTLE
                s.new_window()
                self.schedule(now)

    # -- worker side ------------------------------------------------------

    def update(self, candidates: dict[str, Stream | None], players: list,
               sink_names: set[str], now: float, sink_ids: dict[str, int] | None = None) -> None:
        """``candidates[p]``: most recently started non-corked stream on oto_eq_<p>.
        ``sink_ids``: sink name → index; a changed index means the sinks service
        restarted (all variant slots back at 0 dB), so the variant is re-applied."""
        for p, s in self.state.items():
            present = sink_name(p) in sink_names
            sid = (sink_ids or {}).get(sink_name(p))
            if sid is not None:
                if s.sink_id is not None and sid != s.sink_id and s.applied is not None:
                    log.info("%s çıkışı yeniden oluştu: tür yeniden uygulanacak", _VARIANT_WORD[p])
                    s.applied = None
                    if s.mode == "auto" and s.current is not None:
                        s.boundary_at = now - VARIANT_SETTLE
                        s.new_window()
                s.sink_id = sid
            if s.mode == "fixed" and present and s.applied != s.fixed:
                self._apply(p, s.fixed, "kullanıcı (sabit)",
                            s.current.display if s.current else "", s.current.key if s.current else "")
            st = candidates.get(p)
            if p == "muzik":              # capture only while music plays on the Müzik sink (auto)
                self._audio_ensure(st is not None and present and s.mode == "auto")
            cid = self._cid(st, players) if st is not None else None
            if cid is None:
                continue                  # nothing playing / no info: keep everything
            if s.current is None or self._is_boundary(p, s.current, cid):
                s.current, s.boundary_at = cid, now
                s.new_window()
                log.debug("%s sınırı: %s %s", _VARIANT_WORD[p], cid.display, cid.track)
                self.schedule(now + VARIANT_SETTLE)
                continue
            if cid.track and not s.current.track:
                s.current = cid           # title appeared late: same content, no boundary
            if (s.waiting or s.audio_wait) and s.mode == "auto" and present \
                    and s.boundary_at is not None and now - s.boundary_at >= VARIANT_LATE:
                if s.offline:
                    why = "tür bilinmiyor (çevrimdışı)"
                elif s.waiting:
                    why = "tür bilinmiyor (yanıt gecikti)"   # the answer did not come in time
                else:
                    why = "tür bilinmiyor"
                s.waiting = s.audio_wait = False
                if not self._audio_window(p, s, now):
                    self._unknown(p, s, s.current, why)
                continue
            if p == "muzik" and s.audio_used and not s.remembered and s.mode == "auto" \
                    and s.boundary_at is not None and self._audio_possible(p):
                if now - s.boundary_at >= AUDIO_REMEMBER_AT:
                    self._audio_remember(s, now)
                else:
                    self.schedule(s.boundary_at + AUDIO_REMEMBER_AT)
            if s.mode == "fixed" or not present or s.settled:
                continue
            if now - (s.boundary_at or now) < VARIANT_SETTLE:
                self.schedule((s.boundary_at or now) + VARIANT_SETTLE)
                continue
            s.settled = True
            self._decide(p, s, cid)

    def _decide(self, p: str, s: _VariantState, cid: ContentId) -> None:
        r = None
        if self.resolver is not None:
            try:
                r = self.resolver.cached(cid)
            except Exception as e:
                log.debug("tür önbelleği hatası: %s", e)
        # the user's own assignment is final, "Genel" ("" group) included: it is not a
        # known-unknown for the audio analysis to overrule
        if r is not None and (r.group or r.source == _SRC_KULLANICI or not self._audio_possible(p)):
            self._apply(p, r.group, r.source or "önbellek", cid.display, cid.key)
            return
        if r is not None:                 # known-unknown (negative cache): let the audio decide
            self._audio_later(s)
            return
        if self.resolver is None:
            return
        s.waiting = True

        def done(*args):
            res = next((a for a in args if hasattr(a, "group")), None)
            self.enqueue(("variant", (p, cid, res)))
        try:
            s.waiting = bool(self.resolver.request(cid, done))
        except Exception as e:
            s.waiting = False
            log.debug("tür sorgusu başlatılamadı: %s", e)
        if s.waiting:
            self.schedule((s.boundary_at or 0.0) + VARIANT_LATE + 0.05)
        elif self._audio_possible(p):     # offline / recently failed: the audio may still tell
            s.offline = True
            self._audio_later(s)
        else:                             # offline / recently failed: no answer will come
            self._unknown(p, s, cid, "tür bilinmiyor (çevrimdışı)")

    # -- audio analysis (CHANGE 4) -----------------------------------------

    def _audio_possible(self, p: str) -> bool:
        return p == "muzik" and self.analyzer is not None and self.audio_on

    def _audio_ensure(self, active: bool) -> None:
        if self.analyzer is None:
            return
        try:
            self.analyzer.ensure(bool(active) and self.audio_on)
        except Exception as e:
            log.debug("ses yakalama durumu değiştirilemedi: %s", e)

    def _audio_later(self, s: _VariantState) -> None:
        """No name-based answer: decide by audio at the end of the window (single change)."""
        s.audio_wait = s.audio_used = True
        self.schedule((s.boundary_at or 0.0) + VARIANT_LATE + 0.05)

    def _audio_window(self, p: str, s: _VariantState, now: float) -> bool:
        """End of the window without a name-based genre: apply the audio guess, if any."""
        if not self._audio_possible(p) or s.current is None or s.boundary_at is None:
            return False
        s.audio_used = True
        try:
            g = self.analyzer.guess(s.boundary_at + AUDIO_GUESS_FROM, now)
        except Exception as e:
            log.debug("ses tahmini alınamadı: %s", e)
            return False
        self._audio_logged(s, "pencere")
        s.audio_last = {"group": getattr(g, "group", "") or "",
                        "label": GROUP_LABELS.get(getattr(g, "group", "") or "", _("belirsiz")),
                        "confidence": getattr(g, "confidence", None),
                        "source": getattr(g, "source", "") or "", "for": s.current.display}
        if g is None or not getattr(g, "group", ""):
            return False
        self._apply(p, g.group, getattr(g, "source", "") or "ses analizi",
                    s.current.display, s.current.key)
        return True

    def _audio_logged(self, s: _VariantState, asama: str) -> None:
        """Keep the numbers of an audio decision (even an unclear one) for the personal model."""
        if self.audio_log is None or s.current is None:
            return
        try:
            self.audio_log(s.current, getattr(self.analyzer, "last_detail", None), asama)
        except Exception as e:
            log.debug("ses tahmini kaydı yazılamadı: %s", e)

    def _audio_remember(self, s: _VariantState, now: float) -> None:
        """~25 s into a track with no name-based result: a longer audio guess is stored
        for the artist (used at the next tracks; never changes the current one)."""
        s.remembered = True
        r = self.resolver
        if r is None or not hasattr(r, "remember_audio") or s.current is None:
            return
        try:
            g = self.analyzer.guess(s.boundary_at + AUDIO_GUESS_FROM, now, min_s=AUDIO_REMEMBER_MIN)
        except Exception as e:
            log.debug("ses tahmini (hatırlama) alınamadı: %s", e)
            return
        self._audio_logged(s, "hatirla")
        if g is None or not getattr(g, "group", ""):
            return
        try:
            r.remember_audio(s.current, g.group, getattr(g, "confidence", None),
                             getattr(g, "source", "") or "ses analizi")
            log.info("müzik türü (ses analizi) hatırlandı: %s → %s",
                     s.current.display, GROUP_LABELS.get(g.group, g.group))
        except Exception as e:
            log.debug("ses tahmini kaydedilemedi: %s", e)

    def _unknown(self, p: str, s: _VariantState, cid: ContentId, why: str) -> None:
        """No genre for this content: a variant chosen for OTHER content must not
        carry over (Ezhel's bass on the next artist), so go neutral; the same
        artist/game keeps its variant."""
        if s.applied and s.applied_key != cid.key:
            self._apply(p, "", why, cid.display, cid.key)

    def on_result(self, profile: str, cid: ContentId, result: Any, now: float) -> None:
        s = self.state.get(profile)
        if s is None:
            return
        cur = s.current
        same_key = cur is not None and cur.kind == cid.kind and cur.key == cid.key
        if result is None or s.mode == "fixed":
            if same_key:
                s.waiting = False
                if result is None and s.mode == "auto" and self._audio_possible(profile) \
                        and s.boundary_at is not None and now - s.boundary_at <= VARIANT_LATE:
                    self._audio_later(s)  # no name-based answer: the audio decides at the window end
            return
        if not same_key:
            log.debug("tür yanıtı eski içerik için (%s), sonraki sınırda kullanılacak", cid.display)
            return
        if not s.waiting or s.boundary_at is None or now - s.boundary_at > VARIANT_LATE:
            log.debug("tür yanıtı geç geldi (%s), parçanın ortasında değiştirilmiyor", cid.display)
            return
        s.waiting = False
        if not _same_content(cur, cid):
            # answer for this artist's previous track: decide for the current one
            # (the genre is per artist; a title hint of this track may differ)
            r = None
            try:
                r = self.resolver.cached(cur) if self.resolver is not None else None
            except Exception as e:
                log.debug("tür önbelleği hatası: %s", e)
            result = r or result
        if not result.group and result.source != _SRC_KULLANICI and self._audio_possible(profile):
            self._audio_later(s)          # "unknown" answer: the audio decides at the window end
            return
        s.audio_used = not result.group
        self._apply(profile, result.group, result.source or "", cur.display, cur.key)

    def _apply(self, p: str, group: str, source: str, display: str, key: str = "") -> None:
        s = self.state[p]
        group = group if group in variant_groups(p) else ""
        if group == s.applied:          # same group: no-op (only the status text follows)
            s.applied_for = display or s.applied_for
            s.applied_key = key or s.applied_key
            s.source = source or s.source
            return
        ok = False
        if self.apply_fn is not None:
            try:
                ok = bool(self.apply_fn(p, group, ramp_s=VARIANT_RAMP))
            except Exception as e:
                log.warning("%s türü uygulanamadı: %s", _VARIANT_WORD[p], e)
        if not ok:
            log.debug("%s türü uygulanamadı (%s)", _VARIANT_WORD[p], group or "genel")
            return
        s.applied, s.applied_for, s.applied_key, s.source = group, display, key, source
        who = f" ({display})" if display else ""
        log.info("%s türü: %s%s — %s", _VARIANT_WORD[p], GROUP_LABELS.get(group, group), who,
                 source or "?")

    def _cid(self, st: Stream, players: list) -> ContentId | None:
        if self.content_id is None:
            return None
        proc = None
        if self.proc_lookup is not None:
            try:
                proc = self.proc_lookup(st)
            except Exception:
                proc = None
        try:
            cid = self.content_id(st, players, proc)
        except Exception as e:
            log.debug("içerik kimliği alınamadı (#%d): %s", st.index, e)
            return None
        return cid if isinstance(cid, ContentId) and cid.key else None

    @staticmethod
    def _is_boundary(p: str, old: ContentId, new: ContentId) -> bool:
        if old.key != new.key or old.kind != new.kind:
            return True
        return p == "muzik" and bool(new.track) and bool(old.track) and new.track != old.track

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        out = {}
        for p, s in self.state.items():
            g = s.fixed if s.mode == "fixed" and s.applied is None else (s.applied or "")
            out[p] = {"group": g, "label": GROUP_LABELS.get(g, g), "for": s.applied_for,
                      "key": s.applied_key, "source": s.source, "mode": s.mode,
                      "track": (s.current.track or "") if s.current is not None else "",
                      "ses": dict(s.audio_last) if s.audio_last else None}
        return out

    def stop(self) -> None:
        if self.analyzer is not None and hasattr(self.analyzer, "stop"):
            try:
                self.analyzer.stop()
            except Exception as e:
                log.debug("ses analizi durdurulamadı: %s", e)
        if self.resolver is not None and hasattr(self.resolver, "stop"):
            try:
                self.resolver.stop()
            except Exception as e:
                log.debug("tür çözücü durdurulamadı: %s", e)


class Router:
    """Routing state machine + worker thread.

    ``pulse``: module/object with list_streams, list_sinks, default_sink_name,
    move_stream, EventListener, PulseError (see CONTRACT).
    ``classifier_factory(rules) -> obj with classify(stream, players)``.
    ``mpris``: object with players()/start()/stop() (or None); alternatively
    ``mpris_factory(on_change)`` builds one bound to :meth:`notify_mpris`.
    ``rules_loader() -> dict`` (default: ``config.load_rules()`` = kurallar.toml
    + GUI assignments under ``rules["atama"]``; also used by ``reload``). ``clock``: monotonic
    seconds. ``mode_store``: object with load()/save(mode, profile).
    ``history``: object/module with record(stream, decision) and flush()
    ("seen apps" log for the GUI); None = not recorded (tests).
    ``mover(stream, sink, to_default) -> bool``: how to move (main() uses
    :class:`MetadataMover`); None = ``pulse.move_stream(index, sink)``.
    """

    def __init__(self, pulse: Any, classifier_factory: Callable[[dict], Any],
                 mpris: Any = None, rules_loader: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic, mode_store: Any = None,
                 *, mpris_factory: Callable[[Callable[[], None]], Any] | None = None,
                 history: Any = None,
                 mover: Callable[[Stream, str, bool], bool] | None = None,
                 mic_retarget: Callable[[str], Any] | None = None,
                 content_id: Callable[..., Any] | None = None,
                 genre_resolver: Any = None,
                 apply_variant: Callable[..., bool] | None = None,
                 proc_lookup: Callable[[Stream], Any] | None = None,
                 audio_analyzer: Any = None,
                 ducker: Any = None,
                 voice_check: Callable[..., bool] | None = None,
                 audio_log: Callable[..., Any] | None = None,
                 apply_correction: Callable[..., bool] | None = None,
                 device_writer: Callable[[Any], None] | None = None,
                 content_prober: Any = None):
        self.pulse = pulse
        # CHANGE 9: per-stream content check (icerik.ContentProber) for browser streams whose
        # metadata is a guess or cannot tell tabs apart; None = off
        self.icerik = content_prober
        # CHANGE 7: headphone type detection; the [duzeltme] curve only on its own model.
        # device_writer(Aygit) records the active headphone (STATE_DIR/cihaz.json, read by
        # eqgen in every process) BEFORE apply_correction(groups) re-ramps the sinks.
        self.apply_correction = apply_correction
        self.device_writer = device_writer
        self.kulaklik_ayar = kulaklik.Ayar(desen=re.compile(DEFAULT_HEADSET_PATTERN))
        self._device: kulaklik.Aygit | None = None
        self._device_rec: tuple | None = None       # (ad, tur, duzeltme) last written
        self._device_fail: tuple | None = None      # the record whose write failed last
        self._corr_applied: bool | None = None      # correction state of the live sinks
        self._corr_ids: tuple | None = None         # sink indices it was applied to
        self._corr_retry_at = float("-inf")
        self._corr_retry_want: bool | None = None   # the state whose ramp failed last
        # CHANGE 6: voice ducking (kisma.Ducker) and its "is this a voice stream" test
        self.ducker = ducker
        self.voice_check = voice_check
        self.classifier_factory = classifier_factory
        self.mpris = mpris
        self._mpris_factory = mpris_factory
        self.rules_loader = rules_loader or (lambda: config.load_rules())
        self.clock = clock
        self.mode_store = mode_store if mode_store is not None else ModeStore()
        self.history = history
        self.mover = mover
        # HS80 takibi (wired <-> wireless): see docs/NOTES-hs80-gecis.md
        self.mic_retarget = mic_retarget if mic_retarget is not None else _default_mic_retarget
        self.hs80_follow = True
        self._hs80 = {"sink": _HeadsetFollower("sink"), "source": _HeadsetFollower("source")}
        self._hs80_texts: dict[str, str] = {}   # last action per direction
        self._hs80_text: str | None = None
        self._src_checked_at = float("-inf")
        self._src_event = False
        # genre variants (CHANGE 3); content_id/resolver/apply may be None (absent modules)
        self.variants = VariantTracker(content_id, genre_resolver, apply_variant,
                                       schedule=self._request_scan_at, enqueue=self._q_put,
                                       proc_lookup=proc_lookup, analyzer=audio_analyzer,
                                       audio_log=audio_log)

        self.mode = "auto"
        self.fixed_profile: str | None = None
        self.rules: dict = {}
        self.classifier: Any = _NullClassifier()
        self.headset_re = re.compile(DEFAULT_HEADSET_PATTERN)
        self.hs80_re = re.compile(HS80_PATTERN)
        self._prepared = False
        self.allow_inline = False   # tests only: run IPC commands inline when no worker
        self._error_streak = 0

        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._listener: Any = None
        self._stopping = threading.Event()

        # scheduling (worker-owned)
        self._scan_at: float | None = None
        self._mpris_at: float | None = None
        self._mpris_first: float | None = None
        self._periodic_at: float | None = None
        self._backoff = 0.0
        self._backoff_until = 0.0
        self._last_scan = float("-inf")
        self._sink_names: set[str] = set()

        # anti-flapping (worker-owned)
        self._last_move: dict[int, float] = {}
        self._move_hist: dict[int, list[float]] = {}
        self._parked_until: dict[int, float] = {}
        self._placed: dict[int, str] = {}      # sink we last moved each stream to
        self._unlinked: dict[int, float] = {}  # stream not linked to a sink yet -> first seen
        self._verify: dict[int, tuple[str, str, float, bool]] = {}  # idx -> (from, to, t, retried)

        # transitions, for logging
        self._last_active: bool | None = None
        self._last_default: str | None = None
        self._last_ready: bool | None = None
        self._last_dominant: str | None = None

        # status snapshot (read by IPC threads)
        self._lock = threading.Lock()
        self._status: dict = self._make_status(False, "", None, False, [])
        self._status_key: tuple | None = None
        self._listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------
    # public API (thread-safe unless noted)

    def prepare(self) -> None:
        """Load the persisted mode and the rules (idempotent)."""
        if self._prepared:
            return
        self.mode, self.fixed_profile = self.mode_store.load()
        loader = getattr(self.mode_store, "load_variants", None)
        if loader is not None:
            try:
                self.variants.load(loader())
            except Exception as e:
                log.warning("tür ayarları okunamadı: %s", e)
        err = self.load_rules()
        if err:
            log.error("%s → kurallar olmadan devam (her şey EQ'suz)", err)
        self._set_status_fields()
        self._prepared = True

    def start(self) -> None:
        """Start the worker, pulse event listener and MPRIS watcher."""
        self.prepare()
        if self.mpris is None and self._mpris_factory is not None:
            try:
                self.mpris = self._mpris_factory(self.notify_mpris)
            except Exception as e:
                log.warning("MPRIS izleyici kurulamadı: %s (MPRIS'siz devam)", e)
        self._stopping.clear()
        self._request_scan(0.0)
        self._worker = threading.Thread(target=self._run, name="oto-eq-router", daemon=True)
        self._worker.start()
        try:
            self._listener = self.pulse.EventListener(self.notify_pulse_event)
            self._listener.start()
        except Exception as e:
            self._listener = None
            log.warning("ses olay dinleyicisi başlatılamadı: %s (10 sn'lik taramayla devam)", e)
        if self.mpris is not None and hasattr(self.mpris, "start"):
            try:
                self.mpris.start()
            except Exception as e:
                log.warning("MPRIS izleyici başlatılamadı: %s", e)
        log.info("yönlendirici başladı, mod: %s", _mode_text(self.mode, self.fixed_profile))

    def stop(self, restore: bool = True) -> None:
        """Stop everything. ``restore``: move streams on our sinks back to the default.

        Moving back writes ``target.object=-1``, so WirePlumber forgets the
        remembered EQ-sink target (NOTES-pulse c). Otherwise the speakers could
        inherit HS80 EQ after the router is gone. Best effort, bounded by
        RESTORE_BUDGET seconds; never raises.
        """
        self._stopping.set()
        self._q.put(("stop", None))
        if self.icerik is not None:
            try:
                self.icerik.stop()
            except Exception as e:
                log.debug("içerik denetimi durdurulamadı: %s", e)
        for obj, what in ((self._listener, "ses olay dinleyicisi"), (self.mpris, "MPRIS izleyici")):
            if obj is not None and hasattr(obj, "stop"):
                try:
                    obj.stop()
                except Exception as e:
                    log.warning("%s durdurulamadı: %s", what, e)
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=3.0)
        self._worker = None
        # after the worker: a scan still in flight ends with ducker.ensure(), which would
        # re-arm a ducker stopped before it
        if self.ducker is not None:
            try:
                self.ducker.stop()           # never leave sinks lowered
            except Exception as e:
                log.warning("kısma durdurulamadı: %s", e)
        self.variants.stop()          # variants themselves stay as they are
        if restore:
            t = threading.Thread(target=self._restore_all, name="oto-eq-restore", daemon=True)
            t.start()
            t.join(timeout=RESTORE_BUDGET)
            if t.is_alive():
                log.warning("akışlar %.0f sn içinde geri taşınamadı, bırakılıyor", RESTORE_BUDGET)
        if self.history is not None:
            try:
                self.history.flush()
            except Exception as e:
                log.warning("görülen uygulamalar kaydedilemedi: %s", e)

    def _restore_all(self) -> None:
        """Move every stream on one of our sinks to the default sink (stop path)."""
        deadline = time.monotonic() + RESTORE_BUDGET
        try:
            default = self.pulse.default_sink_name() or ""
            names = {s.index: s.name for s in self.pulse.list_sinks()}
            streams = self.pulse.list_streams()
        except Exception as e:
            log.warning("kapanışta akışlar geri taşınamadı: %s", e)
            return
        if not default or default.startswith(SINK_PREFIX):
            return
        n = 0
        for st in streams:
            if time.monotonic() > deadline:
                break
            if st.node_name.startswith(SINK_PREFIX) or not is_eq_sink(names.get(st.sink_index, "")):
                continue
            try:
                ok = (self.mover(st, default, True) if self.mover is not None
                      else self.pulse.move_stream(st.index, default))
            except Exception as e:
                log.debug("akış #%d geri taşınamadı: %s", st.index, e)
                continue
            n += bool(ok)
        if n:
            log.info("kapanış: %d akış varsayılan çıkışa geri taşındı", n)

    def is_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _q_put(self, item: tuple) -> None:
        self._q.put(item)

    def notify_pulse_event(self, ev: Event) -> None:
        """Pulse listener callback (any thread)."""
        self._q.put(("pulse", ev))

    def notify_mpris(self) -> None:
        """MPRIS watcher callback (any thread)."""
        self._q.put(("mpris", None))

    def request_rescan(self) -> None:
        self._q.put(("rescan", None))

    def request_reload(self) -> None:
        self._q.put(("call", (self._reload, None)))

    def add_status_listener(self, fn: Callable[[dict], None]) -> None:
        """``fn(status)`` is called from the worker whenever the status key changes."""
        self._listeners.append(fn)

    def status(self) -> dict:
        """Snapshot of the IPC status fields (without ``ok``)."""
        with self._lock:
            st = dict(self._status)
            st["streams"] = [dict(s) for s in self._status["streams"]]
            st["variants"] = {p: dict(v) for p, v in self._status["variants"].items()}
        st["dil"] = i18n.dil()          # language of the router's texts (reasons, labels)
        if self.ducker is not None:
            try:
                # bounded: a ducker tick holds its lock across pactl calls (seconds while
                # pipewire-pulse hangs); `kulak durum` / watch / the worker's publish must not
                st["kisma"] = self.ducker.status(wait=KISMA_STATUS_WAIT)
            except Exception as e:
                st["kisma"] = {"hata": _("kısma durumu alınamadı: {hata}").format(hata=e)}
        return st

    def handle_command(self, req: dict) -> dict:
        """IPC command handler (runs on an IPC thread; work goes to the worker)."""
        cmd = req.get("cmd")
        if cmd == "status":
            return {"ok": True, **self.status()}
        if cmd == "set_mode":
            mode, profile = validate_mode(req.get("mode"), req.get("profile"))
            return self._call(lambda: self._set_mode(mode, profile))
        if cmd == "set_variant":
            profile = req.get("profile")
            validate_variant(profile, req.get("group"))
            return self._call(lambda: self._set_variant(profile, req.get("group")))
        if cmd == "reload":
            return self._call(self._reload)
        if cmd == "rescan":
            return self._call(self._rescan)
        if cmd == "dil":         # the user picked another UI language (app Settings → Dil)
            yeni = i18n.yeniden_yukle()
            log.info("arayüz dili yeniden yüklendi: %s", yeni)
            self.request_rescan()        # cached reasons / labels are rebuilt in the new language
            return {"ok": True, "dil": yeni}
        raise ValueError(_("bilinmeyen komut: {komut}").format(komut=repr(cmd)))

    # ------------------------------------------------------------------
    # rules / mode (worker thread, or inline before start)

    def load_rules(self) -> str | None:
        """(Re)load rules + classifier + headset pattern. Returns an error text or None."""
        try:
            rules = self.rules_loader()
            if not isinstance(rules, dict):
                raise ValueError(_("kurallar bir tablo değil"))
        except Exception as e:
            return _("kurallar okunamadı: {hata}").format(hata=e)
        genel = rules.get("genel")
        pattern = DEFAULT_HEADSET_PATTERN
        if isinstance(genel, dict) and isinstance(genel.get("kulaklik_desen"), str) \
                and genel["kulaklik_desen"]:
            pattern = genel["kulaklik_desen"]
        try:
            headset_re = re.compile(pattern)
        except re.error as e:
            return _("{anahtar} geçersiz ({desen}): {hata}").format(
                anahtar="kulaklik_desen", desen=repr(pattern), hata=e)
        ayar, err = kulaklik.ayar_oku(genel, pattern, kulaklik.duzeltme_desen_oku())
        if err:      # optional patterns (kulaklik_haric, cihaz_desen): ignored, rules still load
            log.warning("%s → yok sayıldı", err)
        try:
            clf = self.classifier_factory(rules)
        except Exception as e:
            return _("sınıflandırıcı kurulamadı: {hata}").format(hata=e)
        follow = genel.get("hs80_takip", True) if isinstance(genel, dict) else True
        self.hs80_follow = follow if isinstance(follow, bool) else True
        self.rules, self.headset_re, self.classifier = rules, headset_re, clf
        self.kulaklik_ayar = ayar
        return None

    def _reload(self) -> dict:
        err = self.load_rules()
        if err:
            log.error("yeniden yükleme başarısız: %s (eski kurallar korunuyor)", err)
            return {"ok": False, "error": err}
        log.info("kurallar yeniden yüklendi")
        self._forget_flaps()
        self._reload_genre_settings()
        self.scan()
        return {"ok": True}

    def _reload_genre_settings(self) -> None:
        if self.variants.resolver is None:
            return
        try:
            from . import tur
            hints = tur.title_hints_from_rules(self.rules)
        except Exception:
            hints = None
        try:
            settings = config.load_genre_settings()
        except Exception as e:
            log.warning("tür atamaları okunamadı: %s", e)
            settings = None
        self.variants.settings_changed(settings, hints, self.clock())

    def _rescan(self) -> dict:
        if self.scan():
            return {"ok": True}
        return {"ok": False, "error": _("ses sunucusuna ulaşılamadı (sonra tekrar denenecek)")}

    def _set_mode(self, mode: str, profile: str | None) -> dict:
        mode, profile = validate_mode(mode, profile)
        if (mode, profile) != (self.mode, self.fixed_profile):
            log.info("mod: %s → %s", _mode_text(self.mode, self.fixed_profile),
                     _mode_text(mode, profile))
            self._forget_flaps()
        self.mode, self.fixed_profile = mode, profile
        resp: dict = {"ok": True}
        try:
            self.mode_store.save(mode, profile)
        except OSError as e:
            log.warning("mod kaydedilemedi: %s", e)
            resp["uyari"] = _("uygulandı ama kaydedilemedi (yeniden başlayınca eski ayar döner): "
                              "{hata}").format(hata=e)
        if not self.scan():
            self._set_status_fields()
        return resp

    def _set_variant(self, profile: str, group: str) -> dict:
        self.variants.set(profile, group, sink_name(profile) in self._sink_names, self.clock())
        saver = getattr(self.mode_store, "save_variants", None)
        resp: dict = {"ok": True}
        if saver is not None:
            try:
                saver(self.variants.saved())
            except OSError as e:
                log.warning("tür ayarı kaydedilemedi: %s", e)
                resp["uyari"] = _("uygulandı ama kaydedilemedi (yeniden başlayınca eski ayar "
                                  "döner): {hata}").format(hata=e)
        s = self.variants.state[profile]
        log.info("%s türü modu: %s", _VARIANT_WORD[profile],
                 "otomatik" if s.mode == "auto" else f"sabit ({GROUP_LABELS.get(s.fixed, s.fixed)})")
        self._set_status_fields()
        return resp

    def _call(self, fn: Callable[[], dict]) -> dict:
        """Run ``fn`` on the worker and wait. Never runs on the caller's (IPC) thread,
        except with ``allow_inline`` (unit tests without a worker)."""
        worker = self._worker
        if worker is not None and worker is threading.current_thread():
            return fn()
        if worker is None or not worker.is_alive() or self._stopping.is_set():
            if self.allow_inline and worker is None:
                return fn()
            return {"ok": False, "error": _("yönlendirici hazır değil (başlamadı ya da kapanıyor)")}
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._q.put(("call", (fn, fut)))
        try:
            return fut.result(timeout=COMMAND_WAIT)
        except TimeoutError:
            log.warning("komut zaman aşımına uğradı, sırada bekliyor")
            return {"ok": False, "bekliyor": True,
                    "error": _("yönlendirici meşgul (ses sunucusu yanıt vermiyor); "
                               "komut sırada, birazdan uygulanacak")}

    # ------------------------------------------------------------------
    # worker loop & scheduling (worker thread)

    def _run(self) -> None:
        while not self._stopping.is_set():
            nd = self._next_deadline()
            timeout = PERIODIC_INTERVAL if nd is None else min(PERIODIC_INTERVAL,
                                                                max(0.0, nd - self.clock()))
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = None
            try:
                while item is not None:
                    self.handle_item(item)
                    try:
                        item = self._q.get_nowait()
                    except queue.Empty:
                        item = None
                if self._stopping.is_set():
                    break
                self.tick()
            except Exception:
                log.exception("beklenmeyen hata (devam ediliyor)")
                self._stopping.wait(0.5)

    def handle_item(self, item: tuple) -> None:
        """Process one queue item (worker thread; tests call it directly)."""
        kind, payload = item
        if kind == "pulse":
            ev: Event = payload
            if ev.facility == "sink-input":
                if ev.kind == "new":
                    self._request_scan(NEW_STREAM_DELAY)
                else:  # chatty apps (title/volume/cork updates) must not cause a scan storm
                    self._request_scan_at(max(self.clock() + COALESCE_DELAY,
                                              self._last_scan + CHANGE_MIN_GAP))
            elif ev.facility == "sink" and ev.kind in ("new", "remove"):
                self._request_scan(COALESCE_DELAY)
            elif ev.facility in ("sink", "source") and ev.kind == "change":
                f = self._hs80[ev.facility]
                if f.name and f.dirty_at is None:   # volume may have changed
                    f.dirty_at = self.clock() + HS80_VOL_DEBOUNCE
            elif ev.facility == "card" and ev.kind == "change":
                # CHANGE 7: a headphone plugged into the 3.5 mm jack switches the onboard
                # sink's active port (same sink name) -> re-classify the default output
                self._request_scan_at(max(self.clock() + COALESCE_DELAY,
                                          self._last_scan + CHANGE_MIN_GAP))
            elif ev.facility == "source" and ev.kind in ("new", "remove"):
                self._src_event = True      # checked on the next scan
                if self.hs80_follow and self._hs80["source"].lost_at is not None:
                    self._request_scan(COALESCE_DELAY)   # waiting for the other HS80 mic
            elif ev.facility == "server":
                self._request_scan(COALESCE_DELAY)
        elif kind == "mpris":
            now = self.clock()
            if self._mpris_first is None:
                self._mpris_first = now
            self._mpris_at = min(now + MPRIS_DEBOUNCE, self._mpris_first + MPRIS_MAX_DELAY)
        elif kind == "rescan":
            self._request_scan(0.0)
        elif kind == "variant":
            p, cid, result = payload
            self.variants.on_result(p, cid, result, self.clock())
            self._set_status_fields()
        elif kind == "call":
            fn, fut = payload
            if fut is None:
                try:
                    fn()
                except Exception:
                    log.exception("komut başarısız")
            elif fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as e:
                    fut.set_exception(e)
        elif kind == "stop":
            self._stopping.set()

    def tick(self) -> float | None:
        """Run a scan if one is due; return the next deadline (clock time)."""
        now = self.clock()
        due = self._scan_at is not None and now >= self._scan_at
        if now >= self._backoff_until:
            if self._mpris_at is not None and now >= self._mpris_at:
                due = True
            if self._periodic_at is not None and now >= self._periodic_at:
                due = True
        if due:
            self.scan()
        for f in self._hs80.values():
            if f.dirty_at is not None and now >= f.dirty_at:
                f.dirty_at = None
                self._hs80_refresh_volume(f, now)
        return self._next_deadline()

    def _next_deadline(self) -> float | None:
        cands = [t for t in (self._mpris_at, self._periodic_at) if t is not None]
        cands = [max(t, self._backoff_until) for t in cands]
        if self._scan_at is not None:
            cands.append(self._scan_at)
        cands += [f.dirty_at for f in self._hs80.values() if f.dirty_at is not None]
        return min(cands) if cands else None

    def _request_scan(self, delay: float) -> None:
        self._request_scan_at(self.clock() + delay)

    def _request_scan_at(self, at: float) -> None:
        at = max(at, self._backoff_until)
        if self._scan_at is None or at < self._scan_at:
            self._scan_at = at

    # ------------------------------------------------------------------
    # the policy (worker thread)

    def scan(self) -> bool:
        """Re-evaluate every stream once. Returns False on a pulse error."""
        now = self.clock()
        self._last_scan = now
        self._scan_at = None
        self._mpris_at = None
        self._mpris_first = None
        self._periodic_at = now + PERIODIC_INTERVAL
        try:
            default = self.pulse.default_sink_name() or ""
            sinks = self.pulse.list_sinks()
            streams = self.pulse.list_streams()
        except Exception as e:
            self._on_pulse_error(e, now)
            return False
        self._error_streak = 0
        if self._backoff:
            log.info("ses sunucusuna yeniden ulaşıldı")
        self._backoff = 0.0
        self._backoff_until = 0.0

        sink_by_index = {s.index: s.name for s in sinks}
        sink_names = set(sink_by_index.values())
        if self.hs80_follow:
            new_default = self._hs80_step(default, sink_names, now)
            if new_default:
                default = new_default
        missing = [p for p in ALL_SINK_PROFILES if sink_name(p) not in sink_names]
        sinks_ready = not missing
        default_is_eq = default.startswith(SINK_PREFIX)   # incl. test sinks
        dsink = next((s for s in sinks if s.name == default), None)
        dev = kulaklik.siniflandir(default, dsink.description if dsink else "",
                                   getattr(dsink, "info", None) or {}, self.kulaklik_ayar)
        active = bool(default) and not default_is_eq and dev.kulaklik
        self._device = dev
        self._log_transitions(default, active, default_is_eq, sinks_ready, missing)
        self._correction_step(dev, active, sinks_ready,
                              {n: i for i, n in sink_by_index.items()}, now)

        # always with MPRIS (cheap copy): same decision in every mode -> history/status
        players = self._players()
        present = {st.index for st in streams}
        self._prune(present, now)

        self._sink_names = sink_names
        entries: list[dict] = []
        playing: list[str] = []
        candidates: dict[str, Stream | None] = {p: None for p in VARIANT_PROFILES}
        voice: list[Stream] = []
        ours = [st for st in sorted(streams, key=lambda s: s.index)
                if not st.node_name.startswith(SINK_PREFIX)    # skip our filter-chain nodes
                and st.props.get(BACKGROUND_DESKTOP_PROP) != "1"]
        # ^ streams from an invisible background desktop (Deskhand) play into their own null sink: they must
        #   not be routed, counted as "seen" apps, or trigger voice ducking of the user's outputs
        decisions = {st.index: self._classify(st, players) for st in ours}
        decisions = self._content_step(ours, decisions, players, present)
        for st in ours:
            cur = sink_by_index.get(st.sink_index, "")
            decision = decisions[st.index]
            if self.ducker is not None and self.voice_check is not None and active \
                    and self.mode != "off" and not st.corked:
                try:
                    extra = (getattr(self.ducker, "settings", None) or {}).get("ek_uygulamalar")
                    if self.voice_check(st, decision.category, players, extra=extra):
                        voice.append(st)
                except Exception as e:
                    log.debug("sesli akış denetimi hatası (#%d): %s", st.index, e)
            self._record_history(st, decision)
            owned = self._placed.get(st.index) == cur
            if st.sink_index < 0:
                # not linked yet (WirePlumber links within a few ms): no manual move, and the
                # next look must not wait for a change event + CHANGE_MIN_GAP (≈250 ms
                # un-EQ'd); NOTES-pulse risk 3
                first = self._unlinked.setdefault(st.index, now)
                if now - first < UNLINKED_WINDOW:
                    self._request_scan_at(now + UNLINKED_RECHECK)
                target, reason = None, _("henüz bir çıkışa bağlanmadı")
            else:
                self._unlinked.pop(st.index, None)
                target, reason = self._target(cur, decision, default, active, default_is_eq,
                                              sink_names, owned)
            final = cur
            if target is not None and target != cur:
                moved, note = self._try_move(st, target, reason, now, cur, default)
                if moved:
                    final = target
                else:
                    reason = note.format(neden=reason)
            profile = _profile_of(final)
            if profile is not None and not st.corked:
                playing.append(profile)
                if profile in candidates and (candidates[profile] is None
                                              or st.index > candidates[profile].index):
                    candidates[profile] = st     # most recently started stream on that sink
            entries.append({
                "index": st.index,
                "app": st.app_name or st.binary or st.node_name or f"#{st.index}",
                "binary": st.binary,
                "category": decision.category.value,
                "profile": profile,
                "reason": reason,
                "sink": final,
            })
        try:
            self.variants.update(candidates, players, sink_names, now,
                                 {n: i for i, n in sink_by_index.items()})
        except Exception:
            log.exception("tür takibi hatası")
        if self.ducker is not None:
            try:
                if not self._stopping.is_set():
                    self.ducker.ensure(voice, {n: i for i, n in sink_by_index.items()})
            except Exception:
                log.exception("kısma (sesli sohbet) hatası")
        dominant = next((p for p in PROFILE_PRIORITY if p in playing), None)
        if dominant != self._last_dominant:
            log.debug("baskın profil: %s", PROFILE_LABELS.get(dominant, "yok") if dominant else "yok")
            self._last_dominant = dominant
        self._set_status_fields(active=active, default_sink=default, dominant=dominant,
                                sinks_ready=sinks_ready, streams=entries,
                                kulaklik=dev.durum() if default else None)
        return True

    def _correction_step(self, dev: kulaklik.Aygit, active: bool, ready: bool,
                         sink_ids: dict[str, int], now: float) -> None:
        """CHANGE 7: the [duzeltme] curve follows the active headphone. Record the device
        (eqgen reads it in every process), then re-ramp the sinks when the wanted state
        differs from the live one or the sinks were recreated. Speakers / HDMI: nothing
        changes (the EQ is bypassed there anyway). Never raises."""
        if not active:
            return
        rec = (dev.ad, dev.tur, dev.duzeltme)
        if rec != self._device_rec and self.device_writer is not None:
            try:
                self.device_writer(dev)
            except Exception as e:
                # retried every scan; warn once per device, not up to 4×/s (disk full / RO)
                (log.debug if rec == self._device_fail else log.warning)(
                    "kulaklık durumu kaydedilemedi: %s", e)
                self._device_fail = rec
                return                        # eqgen would read a stale state
        self._device_rec = rec
        self._device_fail = None
        if self.apply_correction is None or not ready:
            return
        ids = tuple(sink_ids.get(sink_name(p)) for p in ALL_SINK_PROFILES)
        if dev.duzeltme == self._corr_applied and ids == self._corr_ids:
            return
        if now < self._corr_retry_at and ids == self._corr_ids \
                and dev.duzeltme == self._corr_retry_want:
            return                            # the same failed state: no retry storm
        groups = {p: (s.applied or "") for p, s in self.variants.state.items()}
        try:
            ok = bool(self.apply_correction(groups))
        except Exception as e:
            log.warning("kulaklık düzeltmesi uygulanamadı: %s", e)
            ok = False
        if ok:
            if dev.duzeltme != self._corr_applied:
                log.info("kulaklık düzeltmesi %s: %s (%s)",
                         "açık" if dev.duzeltme else "kapalı (bu kulaklıkta nötr)",
                         dev.aciklama or dev.ad, dev.etiket)
            self._corr_applied, self._corr_ids = dev.duzeltme, ids
        else:
            # some sinks may already be in the new state: unknown → the next attempt (or a
            # return to the previous headphone) re-applies instead of trusting the old value
            self._corr_applied = None
            self._corr_ids = ids
            self._corr_retry_at = now + CORRECTION_RETRY
            self._corr_retry_want = dev.duzeltme

    def _target(self, cur: str, d: Decision, default: str, active: bool, default_is_eq: bool,
                sink_names: set[str], owned: bool = False) -> tuple[str | None, str]:
        """(target sink or None = leave alone, Turkish reason).

        ``owned``: the stream is on the sink *we* last moved it to (e.g. the old
        default after the default changed) -> still ours, not a manual move.
        """
        on_eq = is_eq_sink(cur)
        if not cur or (not on_eq and cur != default and not owned):
            return None, _("elle taşınmış ({cikis}), dokunulmuyor").format(
                cikis=cur or _("bilinmeyen çıkış"))
        if not active:
            why = _("varsayılan çıkış kulaklık değil")
            if default_is_eq:
                return None, _("varsayılan çıkış kulaklık değil; varsayılan çıkış bir oto-eq "
                               "çıkışı, dokunulmuyor")
            if not default:              # pactl reports no default (WP/pulse restarting)
                return None, why
            return (default if on_eq or owned else None), why
        # active: "no EQ" means the flat level-matched sink (CHANGE 1), else default
        flat = FLAT_SINK if FLAT_SINK in sink_names else default
        if self.mode == "off":
            return flat, _("mod kapalı")
        if self.mode == "fixed":
            if d.category in NO_EQ_CATEGORIES:
                profile, reason = None, d.reason
            else:
                profile = self.fixed_profile
                reason = _("sabit mod: {profil}").format(profil=PROFILE_LABELS.get(profile or "", profile))
        else:
            profile, reason = d.profile, d.reason
        if profile is None:
            return flat, reason
        target = sink_name(profile)
        if target not in sink_names:
            return flat, _("{neden}; {cikis} yok → {hedef}").format(
                neden=reason, cikis=target, hedef=_sink_label(flat))
        return target, reason

    def _try_move(self, st: Stream, target: str, reason: str, now: float,
                  cur: str = "", default: str = "") -> tuple[bool, str]:
        idx = st.index
        pend = self._verify.get(idx)
        if pend is not None and pend[1] == target and pend[0] == cur and not pend[3] \
                and now - pend[2] < VERIFY_WINDOW:
            # our previous move did not take effect: one immediate retry, not a flap
            log.info("akış #%d taşıma uygulanmadı, yeniden deneniyor (%s)", idx, target)
            return self._do_move(st, target, reason, now, cur, default, retried=True)
        parked = self._parked_until.get(idx)
        if parked is not None:
            if now < parked:
                return False, _("{neden} (sürekli geri alınıyor, beklemede)")
            del self._parked_until[idx]
        last = self._last_move.get(idx)
        if last is not None and now - last < FLAP_MIN_INTERVAL:
            self._request_scan_at(last + FLAP_MIN_INTERVAL)
            return False, _("{neden} (bekliyor)")
        hist = [t for t in self._move_hist.get(idx, ()) if now - t < FLAP_WINDOW]
        if len(hist) >= FLAP_MAX_MOVES:
            self._parked_until[idx] = now + FLAP_PARK
            self._move_hist[idx] = []
            self._request_scan_at(now + FLAP_PARK)
            log.warning("akış #%d %s sürekli geri alınıyor; %d sn dokunulmayacak",
                        idx, st.binary or st.app_name, int(FLAP_PARK))
            return False, _("{neden} (sürekli geri alınıyor, beklemede)")
        hist.append(now)
        self._move_hist[idx] = hist
        return self._do_move(st, target, reason, now, cur, default, retried=False)

    def _do_move(self, st: Stream, target: str, reason: str, now: float, cur: str,
                 default: str, retried: bool) -> tuple[bool, str]:
        idx = st.index
        self._last_move[idx] = now
        try:
            if self.mover is not None:
                ok = self.mover(st, target, bool(default) and target == default)
            else:
                ok = self.pulse.move_stream(idx, target)
        except Exception as e:
            log.warning("akış #%d taşınamadı (%s): %s", idx, target, e)
            return False, _("{neden} (taşıma hatası)")
        if not ok:
            log.debug("akış #%d taşınamadı (%s), kaybolmuş olabilir", idx, target)
            return False, _("{neden} (taşınamadı)")
        self._placed[idx] = target
        self._verify[idx] = (cur, target, now, retried)
        self._request_scan_at(now + VERIFY_DELAY)
        log.info("akış #%d %s → %s (%s)", idx, st.binary or st.app_name or st.node_name,
                 _sink_label(target), reason)
        return True, ""

    def _classify(self, st: Stream, players: list) -> Decision:
        try:
            d = self.classifier.classify(st, players)
            if not isinstance(d, Decision):
                raise TypeError(_("beklenmeyen sonuç: {sonuc}").format(sonuc=repr(d)))
        except Exception as e:
            log.debug("sınıflandırma hatası (akış #%d): %s", st.index, e)
            return Decision(Category.UNKNOWN, None, _("sınıflandırma hatası: {hata}").format(hata=e))
        if d.category in NO_EQ_CATEGORIES and d.profile is not None:
            d = Decision(d.category, None, d.reason)
        if d.profile is not None and d.profile not in EQ_PROFILES:
            d = Decision(d.category, None, _("{neden}; bilinmeyen profil {profil}").format(
                neden=d.reason, profil=repr(d.profile)))
        return d

    def _content_step(self, streams: list[Stream], decisions: dict[int, Decision],
                      players: list, present: set[int]) -> dict[int, Decision]:
        """CHANGE 9: let the content check correct weak / ambiguous browser decisions."""
        prober = self.icerik
        if prober is None:
            return decisions
        try:
            prober.enabled = bool(self.variants.audio_on)    # the "Ses analizi" switch
            prober.prune(present)
            groups: dict[str, int] = {}
            for st in streams:
                g = decisions[st.index].grup
                if g and not st.corked:
                    groups[g] = groups.get(g, 0) + 1
            make = getattr(self.classifier, "decision", None)
            if make is None:
                return decisions
            out = dict(decisions)
            for st in streams:
                d = decisions[st.index]
                ambiguous = bool(d.grup) and groups.get(d.grup, 0) >= 2
                out[st.index] = prober.adjust(st, d, ambiguous, self._content_key(st, d, players),
                                              lambda c, r: make(c, r, "icerik"))
            return out
        except Exception:
            log.exception("içerik denetimi hatası")
            return decisions

    def _content_key(self, st: Stream, d: Decision, players: list) -> str:
        """What the stream plays according to metadata: a change restarts its content check."""
        pf = getattr(self.classifier, "player_for", None)
        p = None
        if pf is not None:
            try:
                p = pf(st, players)
            except Exception:
                p = None
        if p is not None:
            return f"{getattr(p, 'title', '')}|{getattr(p, 'url', '')}"
        return f"{st.media_name}|{d.reason}"

    def _record_history(self, st: Stream, decision: Decision) -> None:
        if self.history is None:
            return
        try:
            self.history.record(st, decision)
        except Exception as e:  # history must never break routing
            log.debug("görülen uygulama kaydı hatası: %s", e)

    def _players(self) -> list:
        if self.mpris is None:
            return []
        try:
            return list(self.mpris.players())
        except Exception as e:
            log.debug("MPRIS oynatıcıları alınamadı: %s", e)
            return []

    def _on_pulse_error(self, e: Exception, now: float) -> None:
        self._error_streak += 1
        if self._error_streak == STATUS_STALE_AFTER:
            self._set_status_fields(active=False, sinks_ready=False, dominant=None,
                                    streams=[])
        self._backoff = BACKOFF_MIN if not self._backoff else min(self._backoff * 2, BACKOFF_MAX)
        self._backoff_until = now + self._backoff
        self._scan_at = self._backoff_until
        if self._backoff == BACKOFF_MIN:
            log.warning("ses sunucusu hatası: %s (yeniden denenecek)", e)
        else:
            log.debug("ses sunucusu hatası: %s (%.1f sn sonra tekrar)", e, self._backoff)

    def _log_transitions(self, default: str, active: bool, default_is_eq: bool,
                         ready: bool, missing: list[str]) -> None:
        if active != self._last_active or default != self._last_default:
            dev = self._device
            what = f"{dev.etiket}: {dev.neden}" if dev is not None else "?"
            if active:
                log.info("kulaklık etkin: %s (%s)", default, what)
            elif default_is_eq:
                # expected for ~0.1 s at every boot (EQ sinks have priority.session = 1 until
                # the USB/ALSA nodes appear); onarim notifies the user if it lasts ≥ 5 s
                log.info("varsayılan çıkış bir oto-eq çıkışı (%s); EQ devre dışı", default)
            else:
                log.info("varsayılan çıkış kulaklık değil (%s; %s) → EQ devre dışı",
                         default or "yok", what)
            if self._last_active is not None:
                self._forget_flaps()
            self._last_active, self._last_default = active, default
        if ready != self._last_ready:
            if ready:
                log.info("EQ çıkışları hazır")
            else:
                log.warning("EQ çıkışları eksik: %s → o akışlar düz/varsayılan çıkışa gider",
                            ", ".join(sink_name(p) for p in missing))
            self._last_ready = ready

    # ------------------------------------------------------------------
    # HS80 takibi (worker thread). Wired <-> wireless = two different nodes.
    # Never raises; every pulse call is best-effort.

    def _is_hs80(self, name: str | None) -> bool:
        return bool(name) and not name.startswith(SINK_PREFIX) \
            and not name.endswith(".monitor") and bool(self.hs80_re.search(name))

    def _pulse_call(self, fn_name: str, *args, default: Any = None) -> Any:
        fn = getattr(self.pulse, fn_name, None)
        if fn is None:
            return default
        try:
            return fn(*args)
        except Exception as e:
            log.debug("HS80 takibi: %s başarısız: %s", fn_name, e)
            return default

    def _hs80_refresh_volume(self, f: _HeadsetFollower, now: float) -> None:
        if not f.name or f.lost_at is not None:
            return
        v = self._pulse_call(f"get_{f.kind}_volume", f.name)
        if isinstance(v, int):
            f.vol, f.vol_at = v, now

    def _hs80_note(self, msg: str, warn: bool = False, kind: str = "sink", **kw: Any) -> None:
        """``msg``: a Turkish msgid (N_) with named fields; journal Turkish, status translated."""
        (log.warning if warn else log.info)("HS80 takibi: %s", msg.format(**kw))
        self._hs80_texts[kind] = _(msg).format(**kw)
        self._hs80_text = "; ".join(self._hs80_texts[k] for k in ("sink", "source")
                                    if k in self._hs80_texts)

    def _hs80_step(self, default: str, sink_names: set[str], now: float) -> str | None:
        """Run both followers; returns a new default sink name if we set one."""
        new_sink = None
        try:
            new_sink = self._hs80_follow_one(self._hs80["sink"], default, sink_names, now)
        except Exception:
            log.exception("HS80 takibi (çıkış) hatası")
        try:
            f = self._hs80["source"]
            sink_busy = new_sink is not None or self._hs80["sink"].lost_at is not None
            if self._src_event or sink_busy or f.lost_at is not None \
                    or now - self._src_checked_at >= HS80_SOURCE_POLL:
                self._src_event = False
                self._src_checked_at = now
                names = self._pulse_call("list_source_names")
                src_default = self._pulse_call("default_source_name")
                if isinstance(names, list) and isinstance(src_default, str):
                    self._hs80_follow_one(f, src_default, set(names), now)
        except Exception:
            log.exception("HS80 takibi (mikrofon) hatası")
        return new_sink

    def _hs80_follow_one(self, f: _HeadsetFollower, default: str, present: set[str],
                         now: float) -> str | None:
        kind, tr = f.kind, _KIND_TR[f.kind]
        virtual_mic = kind == "source" and default.startswith(SINK_PREFIX)
        # 1) the HS80 node we were tracking disappeared -> open the 30 s window
        if f.name and f.in_use and f.name not in present and f.lost_at is None:
            f.lost_at, f.lost_name, f.fallback, f.cancelled = now, f.name, None, False
            log.info("HS80 takibi: %s kayboldu (%s); %d sn diğer mod bekleniyor",
                     tr, f.name, int(HS80_WINDOW))
        # 2) remember the HS80 node in use (default, or the mic behind our virtual mic)
        current = default if (self._is_hs80(default) and default in present) else None
        if current is None and virtual_mic:
            current = next((n for n in sorted(present) if self._is_hs80(n)), None)
            if f.lost_at is not None and current is not None and current != f.lost_name \
                    and now - f.lost_at <= HS80_WINDOW:
                # mode switch while the virtual mic is the default: keep the default,
                # move the virtual mic's capture to the new HS80 mic
                self._hs80_apply_volume(f, current, only_lower=False)
                self._hs80_retarget_mic(current)
                self._hs80_note(N_("mikrofon {eski} → {yeni} (sanal mikrofon varsayılan kaldı)"),
                                kind=kind, eski=f.lost_name, yeni=current)
                f.reset_loss()
        if current is not None:
            f.in_use = True
            if f.lost_at is not None and current != f.lost_name \
                    and now - f.lost_at <= HS80_WINDOW:
                # WirePlumber or the user already picked the other HS80: only make
                # sure it is not louder than before (hearing safety)
                self._hs80_apply_volume(f, current, only_lower=True)
                if kind == "source":
                    self._hs80_retarget_mic(current)
            if f.lost_at is not None:
                f.reset_loss()
            if current != f.name:
                f.name, f.vol = current, None
            if f.vol is None or now - f.vol_at >= HS80_VOL_REFRESH:
                self._hs80_refresh_volume(f, now)
            return None
        if f.lost_at is None:
            f.in_use = False      # HS80 not in use (e.g. speakers chosen): never act later
            return None
        # 3) inside the window: wait for the other mode, respect user choices
        if now - f.lost_at > HS80_WINDOW:
            log.info("HS80 takibi: %s için süre doldu (%d sn), varsayılana dokunulmadı",
                     tr, int(HS80_WINDOW))
            f.reset_loss()
            f.name = None
            return None
        if f.cancelled:
            return None
        if default and default in present and default != f.lost_name:
            if f.fallback is None:
                f.fallback = default             # what WirePlumber fell back to
            elif default != f.fallback:
                f.cancelled = True
                self._hs80_note(_HS80_MSG["elle"][kind], kind=kind, ad=default)
                return None
        cands = sorted(n for n in present if self._is_hs80(n) and n != f.lost_name)
        if not cands:
            return None
        new = cands[0]
        # volume FIRST (never louder than before), then the default
        set_vol = self._hs80_apply_volume(f, new, only_lower=False)
        if set_vol is None:
            self._hs80_note(_HS80_MSG["ses_yok"][kind], warn=True, kind=kind, ad=new)
            f.reset_loss()
            f.name = None
            return None
        if not self._pulse_call(f"set_default_{kind}", new, default=False):
            self._hs80_note(_HS80_MSG["varsayilan_yok"][kind], warn=True, kind=kind, ad=new)
            f.reset_loss()
            f.name = None
            return None
        old = f.lost_name
        f.reset_loss()
        f.name, f.vol, f.vol_at = new, set_vol, now
        self._hs80_note(_HS80_MSG["gecis"][kind], kind=kind, eski=old, yeni=new,
                        yuzde=_pct_n(set_vol))
        if kind == "source":
            self._hs80_retarget_mic(new)
        return new if kind == "sink" else None

    def _hs80_apply_volume(self, f: _HeadsetFollower, name: str,
                           only_lower: bool) -> int | None:
        """Set ``name`` to the remembered volume (40 % if unknown); returns the value set
        (or kept). ``only_lower``: never raise, just clamp down."""
        target = f.vol if f.vol is not None else HS80_SAFE_VOLUME
        target = max(0, min(int(target), 65536))
        if only_lower:
            cur = self._pulse_call(f"get_{f.kind}_volume", name)
            if isinstance(cur, int) and cur <= target:
                return cur
        if self._pulse_call(f"set_{f.kind}_volume", name, target, default=False):
            if only_lower:
                self._hs80_note(_HS80_MSG["indirildi"][f.kind], kind=f.kind, ad=name,
                                yuzde=_pct_n(target))
            return target
        return None

    def _hs80_retarget_mic(self, source: str) -> None:
        try:
            r = self.mic_retarget(source)
            log.info("HS80 takibi: sanal mikrofon girişi → %s (%s)", source,
                     "başlatıldı" if r else "bağlanamadı/çalışmıyor")
        except Exception as e:
            log.warning("HS80 takibi: sanal mikrofon yeniden hedeflenemedi: %s", e)

    def _forget_flaps(self) -> None:
        """User-initiated context change: rate-limit history (not the 2 s rule) restarts."""
        self._move_hist.clear()
        self._parked_until.clear()

    def _prune(self, present: set[int], now: float) -> None:
        for d in (self._last_move, self._move_hist, self._parked_until, self._placed,
                  self._verify, self._unlinked):
            for idx in [i for i in d if i not in present]:
                del d[idx]

    # ------------------------------------------------------------------
    # status snapshot

    def _make_status(self, active: bool, default_sink: str, dominant: str | None,
                     sinks_ready: bool, streams: list[dict], device: dict | None = None) -> dict:
        return {
            "active": active,
            "default_sink": default_sink,
            "mode": self.mode,
            "fixed_profile": self.fixed_profile if self.mode == "fixed" else None,
            "dominant": dominant,
            "sinks_ready": sinks_ready,
            "streams": streams,
            "hs80_takip_son": self._hs80_text,
            "variants": self.variants.status(),
            "kulaklik": device,          # CHANGE 7: kulaklik.Aygit.durum() of the default sink
        }

    def _set_status_fields(self, **fields: Any) -> None:
        with self._lock:
            old = self._status
            st = self._make_status(
                fields.get("active", old["active"]),
                fields.get("default_sink", old["default_sink"]),
                fields.get("dominant", old["dominant"]),
                fields.get("sinks_ready", old["sinks_ready"]),
                fields.get("streams", old["streams"]),
                fields.get("kulaklik", old.get("kulaklik")),
            )
            self._status = st
            dev = st["kulaklik"] or {}
            key = (st["active"], st["default_sink"], st["mode"], st["fixed_profile"],
                   st["dominant"], st["sinks_ready"], st["hs80_takip_son"],
                   tuple((s["index"], s["profile"]) for s in st["streams"]),
                   tuple((p, v["group"], v["for"], v["mode"], v.get("track"), v.get("source"))
                         for p, v in sorted(st["variants"].items())),
                   (dev.get("ad"), dev.get("tur"), dev.get("kulaklik"), dev.get("duzeltme")))
            changed = key != self._status_key
            self._status_key = key
        if changed and self._listeners:
            snap = self.status()
            for fn in list(self._listeners):
                try:
                    fn(snap)
                except Exception as e:
                    log.debug("durum dinleyicisi hatası: %s", e)


# ----------------------------------------------------------------------
# daemon entry point

def _setup_logging() -> None:
    name = os.environ.get("OTO_EQ_LOG", "INFO").strip().upper() or "INFO"
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        level = logging.INFO
    if os.environ.get("JOURNAL_STREAM"):
        fmt = "%(levelname)s %(name)s: %(message)s"      # journald adds the time
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr)


def _proc_lookup(stream: Stream) -> Any:
    """Host ProcInfo of a stream (procinfo caches both steps)."""
    from . import procinfo
    pid = procinfo.resolve_pid(stream)
    return procinfo.proc_info(pid) if pid else None


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    from . import classifier, pulse  # lazy: router.py imports without them
    from . import ipc
    from .ipc import IpcServer
    # FIRST, before anything with side effects: kisma.restore_on_start() below would undo
    # the live daemon's ducking mid-call and drop its stale-lowering records (and the
    # models would load for nothing)
    if ipc.socket_in_use():
        log.error("başka bir oto-eq zaten çalışıyor (%s)", config.SOCKET_PATH)
        return 1
    try:
        from . import mpris
        mpris_factory = mpris.MprisWatcher
    except Exception as e:  # MPRIS is optional
        log.warning("MPRIS modülü yüklenemedi: %s (MPRIS'siz devam)", e)
        mpris_factory = None

    try:
        from . import history
    except Exception as e:  # the "seen apps" log is optional
        log.warning("görülen uygulama kaydı yüklenemedi: %s", e)
        history = None

    content_id = resolver = apply_variant = None
    # Genre variants (CHANGE 3): on by default since the 25 Eki integration;
    # OTO_EQ_TUR=0 turns them off (= exactly the behaviour before CHANGE 3).
    tur_on = os.environ.get("OTO_EQ_TUR", "1") != "0"
    try:                                  # genre variants (CHANGE 3), optional
        if not tur_on:
            raise RuntimeError("OTO_EQ_TUR=0")
        from . import tur
        content_id = tur.content_id
        # one resolver for the router AND the classifier's "known artist" check:
        # two instances would each rewrite the cache file with only their entries
        resolver = tur.shared_resolver()
        resolver.set_settings(config.load_genre_settings())
        resolver.set_title_hints(tur.title_hints_from_rules(config.load_rules()))
    except Exception as e:
        (log.warning if tur_on else log.debug)("tür algılama kapalı: %s", e)
    try:
        if not tur_on:
            raise RuntimeError("OTO_EQ_TUR=0")
        from . import eqgen
        apply_variant = getattr(eqgen, "apply_variant", None)
    except Exception as e:
        (log.warning if tur_on else log.debug)("tür alt profilleri devre dışı: %s", e)
    # CHANGE 7: the [duzeltme] curve follows the headphone (independent of the genres)
    apply_correction = None
    try:
        from . import eqgen
        apply_correction = eqgen.apply_correction
    except Exception as e:
        log.warning("kulaklığa göre düzeltme devre dışı: %s", e)
    # CHANGE 4: audio analysis of the Müzik sink when name sources give nothing.
    # Optional (numpy + parec); the ML model is optional on top (onnxruntime + model file).
    # OTO_EQ_SES=0 turns it off; the GUI switch "Ses analizi" (ses_analizi) pauses it live.
    analyzer = audio_log = model = None
    try:
        if resolver is None or apply_variant is None:
            raise RuntimeError("tür alt profilleri kapalı")
        if os.environ.get("OTO_EQ_SES", "1") == "0":
            raise RuntimeError("OTO_EQ_SES=0")
        from . import ses_analiz
        model = None
        try:
            from . import ses_model
            model = ses_model.load_model()
        except Exception as e:
            log.info("ses modeli yok (yalnız DSP): %s", e)
        personal = None
        try:
            from . import kisisel_model
            personal = kisisel_model.yukle()
            audio_log = kisisel_model.kaydet_tahmin
        except Exception as e:
            log.info("kişisel model yok: %s", e)
        try:
            analyzer = ses_analiz.AudioAnalyzer(model=model, personal=personal)
        except TypeError:                     # older ses_analiz without the personal hook
            analyzer = ses_analiz.AudioAnalyzer(model=model)
        log.info("ses analizi hazır (%s)", "DSP + model" if model is not None else "DSP")
    except Exception as e:
        log.info("ses analizi kapalı: %s", e)
    # CHANGE 9: per-stream content check of browser streams (YAMNet of the model above;
    # OTO_EQ_ICERIK=0 turns it off, the "Ses analizi" switch pauses it live)
    prober = None
    try:
        from . import icerik
        prober = icerik.build(model)
        if prober is not None:
            log.info("içerik denetimi hazır (tarayıcı akışları, YAMNet)")
    except Exception as e:
        log.info("içerik denetimi kapalı: %s", e)

    ducker = voice_check = None
    if os.environ.get("OTO_EQ_KISMA", "1") != "0":       # CHANGE 6: voice ducking, optional
        try:
            from . import kisma
            ducker = kisma.Ducker()
            ducker.restore_on_start()                       # undo a stale lowering after a crash
            voice_check = kisma.sesli_mi
            log.info("sesli sohbette kısma hazır")
        except Exception as e:
            log.info("sesli sohbette kısma kapalı: %s", e)
            ducker = voice_check = None

    router = Router(pulse=pulse, classifier_factory=classifier.Classifier,
                    rules_loader=config.load_rules, mpris_factory=mpris_factory,
                    history=history, mover=MetadataMover(fallback=pulse.move_stream),
                    content_id=content_id, genre_resolver=resolver,
                    apply_variant=apply_variant, proc_lookup=_proc_lookup,
                    audio_analyzer=analyzer, ducker=ducker, voice_check=voice_check,
                    audio_log=audio_log, apply_correction=apply_correction,
                    device_writer=kulaklik.durum_yaz, content_prober=prober)
    if prober is not None:
        prober.on_change = router.request_rescan
    try:
        router.variants.audio_on = bool(config.load_genre_settings().get("ses_analizi", True))
    except Exception:
        pass
    router.prepare()
    server = IpcServer(router.status, router.handle_command)
    router.add_status_listener(server.publish)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("sinyal alındı (%s), kapatılıyor", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGHUP, lambda *_: router.request_reload())

    router.start()          # worker first: IPC commands never run inline
    try:
        server.start()
    except (OSError, RuntimeError) as e:
        log.error("IPC soketi açılamadı: %s", e)
        router.stop()
        return 1
    # CHANGE 6 (D4): self-heal (missing EQ sinks → restart, xrun bursts → notify, …);
    # OTO_EQ_ONARIM=0 off, OTO_EQ_ONARIM=izle = dry run (logs what it would do)
    onarici = None
    mode_onarim = os.environ.get("OTO_EQ_ONARIM", "1")
    if mode_onarim != "0":
        try:
            from . import onarim
            onarici = onarim.Onarici(status_fn=router.status, rescan=router.request_rescan,
                                     kulaklik_desen=HS80_PATTERN,   # its checks are HS80 takibi
                                     dry_run=(mode_onarim == "izle"), notify=onarim.bildir)
            onarici.baslat(60)
            log.info("otomatik onarım açık%s", " (yalnız izleme)" if mode_onarim == "izle" else "")
        except Exception as e:
            log.info("otomatik onarım kapalı: %s", e)
            onarici = None
    try:
        while not stop.wait(1.0):
            if not router.is_alive():
                log.error("yönlendirici iş parçacığı durdu, çıkılıyor")
                return 1
    finally:
        if onarici is not None:
            try:
                onarici.durdur()
            except Exception as e:
                log.warning("otomatik onarım durdurulamadı: %s", e)
        server.stop()
        router.stop()
        log.info("oto-eq yönlendirici kapandı")
    return 0


if __name__ == "__main__":
    sys.exit(main())
