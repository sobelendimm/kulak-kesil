"""Optional pretrained music-genre model (ONNX, CPU, one thread) for audio analysis.

``load_model()`` returns an object with ``predict(x, rate) -> dict[group, prob] | None``
over ``MUSIC_GROUPS`` (consumed by ``ses_analiz.AudioAnalyzer(model=...)``), or None
when onnxruntime or the model file is missing — everything works without it.

Models (Essentia model zoo, ONNX exports; see docs/MODELS.md):
  * discogs-effnet-bsdynamic-1.onnx  — main: 400 Discogs styles (music genres +
    "Non-Music" spoken word). CC BY-NC-SA 4.0 (personal, non-commercial use only).
  * audioset-yamnet-1.onnx           — optional helper (YAMNet, Google, Apache-2.0):
    AudioSet genre classes averaged with EffNet + speech-vs-music for "konusma".
  * msd-musicnn-1.onnx               — supported for comparison (50 MSD tags).

Front-end is numpy only (no librosa/torch): FFT resampling to 16 kHz, STFT, mel
filterbank (librosa-compatible Slaney or TF-compatible HTK), log compression.
Each model file needs its Essentia ``.json`` (label list) next to it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .i18n import _
from .models import MUSIC_GROUPS

log = logging.getLogger("oto_eq.ses_model")

MODEL_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "oto-eq" / "models"
MAIN_MODEL = "discogs-effnet-bsdynamic-1.onnx"
SPEECH_MODEL = "audioset-yamnet-1.onnx"

MIN_SECONDS = 2.0          # shorter input → None
MAX_PATCHES = 8            # long inputs are sub-sampled to at most this many patches (CPU bound)
SILENCE_DBFS = -60.0       # RMS below this → None (nothing to classify)
TARGET_DBFS = -14.0        # RMS normalisation target (app volume must not change the answer)
MAX_GAIN_DB = 30.0


# ============================================================================
#  FRONT-END (numpy)
# ============================================================================
def resample(x: np.ndarray, rate: int, target: int) -> np.ndarray:
    """Band-limited FFT resampling (whole window at once; fine for a few seconds)."""
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    if rate == target or x.size == 0:
        return x
    n_out = int(round(x.size * target / rate))
    if n_out < 2:
        return np.zeros(0)
    spec = np.fft.rfft(x)
    keep = n_out // 2 + 1
    if keep <= spec.size:
        spec = spec[:keep]
    else:
        spec = np.concatenate([spec, np.zeros(keep - spec.size, dtype=spec.dtype)])
    return np.fft.irfft(spec, n_out) * (n_out / x.size)


def _hz_to_mel(f, htk: bool):
    f = np.asarray(f, dtype=np.float64)
    if htk:
        return 2595.0 * np.log10(1.0 + f / 700.0)
    f_sp, min_log_hz, min_log_mel, logstep = 200.0 / 3, 1000.0, 15.0, np.log(6.4) / 27.0
    mel = f / f_sp
    return np.where(f >= min_log_hz, min_log_mel + np.log(np.maximum(f, 1e-10) / min_log_hz) / logstep, mel)


def _mel_to_hz(m, htk: bool):
    m = np.asarray(m, dtype=np.float64)
    if htk:
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    f_sp, min_log_hz, min_log_mel, logstep = 200.0 / 3, 1000.0, 15.0, np.log(6.4) / 27.0
    return np.where(m >= min_log_mel, min_log_hz * np.exp(logstep * (m - min_log_mel)), f_sp * m)


def mel_filters(rate: int, n_fft: int, n_mels: int, fmin: float, fmax: float, kind: str) -> np.ndarray:
    """(n_mels, n_fft//2+1) weights.

    kind "slaney": librosa default (Slaney mel, triangles linear in Hz, area-normalised) —
    Essentia's TensorflowInputMusiCNN (MusiCNN / Discogs-EffNet).
    kind "htk_tf": tf.signal.linear_to_mel_weight_matrix (HTK mel, triangles linear in
    mel, peak 1, DC bin 0) — YAMNet.
    """
    n_bins = n_fft // 2 + 1
    freqs = np.linspace(0.0, rate / 2.0, n_bins)
    if kind == "slaney":
        edges = _mel_to_hz(np.linspace(_hz_to_mel(fmin, False), _hz_to_mel(fmax, False), n_mels + 2), False)
        fdiff = np.diff(edges)
        ramps = edges[:, None] - freqs[None, :]
        lower = -ramps[:-2] / fdiff[:-1, None]
        upper = ramps[2:] / fdiff[1:, None]
        w = np.maximum(0.0, np.minimum(lower, upper))
        w *= (2.0 / (edges[2:n_mels + 2] - edges[:n_mels]))[:, None]
        return w
    if kind == "htk_tf":
        spec_mel = _hz_to_mel(freqs, True)
        edges = np.linspace(_hz_to_mel(fmin, True), _hz_to_mel(fmax, True), n_mels + 2)
        lo, ce, hi = edges[:-2, None], edges[1:-1, None], edges[2:, None]
        w = np.maximum(0.0, np.minimum((spec_mel - lo) / (ce - lo), (hi - spec_mel) / (hi - ce)))
        w[:, 0] = 0.0
        return w
    raise ValueError(kind)


@dataclass(frozen=True)
class FrontEnd:
    rate: int = 16000
    n_fft: int = 512
    win: int = 512
    hop: int = 256
    n_mels: int = 96
    fmin: float = 0.0
    fmax: float = 8000.0
    mel_kind: str = "slaney"
    power: float = 2.0
    log: str = "log10_1p10000"       # or "ln_0.001"
    center: bool = True
    patch: int = 128
    patch_hop: int = 62


def stft_mag(x: np.ndarray, fe: FrontEnd) -> np.ndarray:
    """|STFT| frames × bins (periodic Hann)."""
    if fe.center:
        x = np.pad(x, (fe.n_fft // 2, fe.n_fft // 2), mode="reflect" if x.size > fe.n_fft // 2 else "constant")
    if x.size < fe.win:
        return np.zeros((0, fe.n_fft // 2 + 1))
    n = 1 + (x.size - fe.win) // fe.hop
    idx = np.arange(fe.win)[None, :] + fe.hop * np.arange(n)[:, None]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(fe.win) / fe.win)
    return np.abs(np.fft.rfft(x[idx] * window, n=fe.n_fft, axis=1))


_FILTER_CACHE: dict[FrontEnd, np.ndarray] = {}


def log_mel(x: np.ndarray, fe: FrontEnd) -> np.ndarray:
    """frames × n_mels float32 log-mel spectrogram (x already at fe.rate)."""
    w = _FILTER_CACHE.get(fe)
    if w is None:
        w = _FILTER_CACHE[fe] = mel_filters(fe.rate, fe.n_fft, fe.n_mels, fe.fmin, fe.fmax, fe.mel_kind)
    spec = stft_mag(x, fe)
    if fe.power != 1.0:
        spec = spec ** fe.power
    mel = spec @ w.T
    if fe.log == "log10_1p10000":
        out = np.log10(1.0 + 10000.0 * mel)
    elif fe.log == "ln_0.001":
        out = np.log(mel + 0.001)
    else:
        raise ValueError(fe.log)
    return out.astype(np.float32)


def patches(spec: np.ndarray, size: int, hop: int, max_n: int = MAX_PATCHES) -> np.ndarray:
    """n × size × bins; the last partial patch is dropped (≥1 patch when spec ≥ size)."""
    if spec.shape[0] < size:
        return np.zeros((0, size, spec.shape[1]), dtype=np.float32)
    starts = np.arange(0, spec.shape[0] - size + 1, hop)
    if starts.size > max_n:
        starts = starts[np.linspace(0, starts.size - 1, max_n).round().astype(int)]
    return np.stack([spec[s:s + size] for s in starts]).astype(np.float32)


def prepare(x, rate: int, target: int, normalize: bool = True) -> np.ndarray | None:
    """Mono float, resampled, RMS-normalised; None for silence/too short."""
    try:
        x = np.asarray(x, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if x.ndim == 2:                        # (n, ch) interleaved → mono
        x = x.mean(axis=1)
    x = x.reshape(-1)
    if not rate or rate <= 0 or x.size < MIN_SECONDS * rate:
        return None
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    x = x - x.mean()
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 0 or 20 * np.log10(rms) < SILENCE_DBFS:
        return None
    x = resample(x, int(rate), target)
    if normalize:
        gain_db = min(MAX_GAIN_DB, TARGET_DBFS - 20 * np.log10(max(rms, 1e-12)))
        x = np.clip(x * 10 ** (gain_db / 20), -1.0, 1.0)
    return x


# ============================================================================
#  LABEL → GROUP MAPPING TABLES  (documented in docs/NOTES-ses-model.md)
#  Each label gets weights over MUSIC_GROUPS (sum ≤ 1); unmapped labels count 0.
#  Same genre decisions as tur.py TAG_TABLES: hip-hop/rap → elektronik,
#  blues/folk/country/jazz/soundtrack → klasik_akustik, R&B/soul/funk → pop_vokal.
# ============================================================================
M, E, K, P, S = MUSIC_GROUPS            # metal_rock, elektronik, klasik_akustik, pop_vokal, konusma

DISCOGS_GENRE: dict[str, dict[str, float]] = {
    "Blues": {K: 1.0},
    "Brass & Military": {K: 1.0},
    "Children's": {P: 1.0},
    "Classical": {K: 1.0},
    "Electronic": {E: 1.0},
    "Folk, World, & Country": {K: 1.0},
    "Funk / Soul": {P: 1.0},
    "Hip Hop": {E: 1.0},
    "Jazz": {K: 1.0},
    "Latin": {P: 1.0},
    "Non-Music": {S: 1.0},
    "Pop": {P: 1.0},
    "Reggae": {P: 1.0},
    "Rock": {M: 1.0},
    "Stage & Screen": {K: 1.0},
}
DISCOGS_STYLE: dict[str, dict[str, float]] = {
    # Blues
    "Blues---Rhythm & Blues": {P: 1.0},
    "Blues---Electric Blues": {K: 0.6, M: 0.4}, "Blues---Modern Electric Blues": {K: 0.6, M: 0.4},
    # Children's
    "Children's---Story": {S: 1.0}, "Children's---Educational": {S: 0.5, P: 0.5},
    # Electronic
    "Electronic---Modern Classical": {K: 1.0}, "Electronic---Neofolk": {K: 1.0},
    "Electronic---New Age": {K: 0.5, E: 0.5},
    "Electronic---Dance-pop": {E: 0.5, P: 0.5}, "Electronic---Synth-pop": {E: 0.6, P: 0.4},
    "Electronic---Disco": {E: 0.5, P: 0.5}, "Electronic---Euro-Disco": {E: 0.5, P: 0.5},
    "Electronic---Italo-Disco": {E: 0.5, P: 0.5}, "Electronic---Disco Polo": {E: 0.4, P: 0.6},
    "Electronic---Freestyle": {E: 0.5, P: 0.5},
    "Electronic---New Wave": {E: 0.5, P: 0.5}, "Electronic---Industrial": {E: 0.6, M: 0.4},
    "Electronic---Acid Jazz": {E: 0.5, K: 0.5}, "Electronic---Future Jazz": {E: 0.5, K: 0.5},
    "Electronic---Jazzdance": {E: 0.5, K: 0.5},
    # Folk, World, & Country — pop-like world styles go with arabesk/Türk pop (pop_vokal)
    "Folk, World, & Country---Laïkó": {P: 1.0}, "Folk, World, & Country---Raï": {P: 1.0},
    "Folk, World, & Country---Zouk": {P: 1.0}, "Folk, World, & Country---Soukous": {P: 1.0},
    "Folk, World, & Country---Highlife": {P: 1.0}, "Folk, World, & Country---Séga": {P: 1.0},
    "Folk, World, & Country---Éntekhno": {P: 0.5, K: 0.5}, "Folk, World, & Country---Gospel": {P: 1.0},
    "Folk, World, & Country---Canzone Napoletana": {P: 0.5, K: 0.5},
    # Funk / Soul
    "Funk / Soul---Disco": {P: 0.5, E: 0.5}, "Funk / Soul---Psychedelic": {P: 0.5, M: 0.5},
    # Jazz
    "Jazz---Easy Listening": {K: 0.5, P: 0.5}, "Jazz---Jazz-Rock": {K: 0.6, M: 0.4},
    "Jazz---Jazz-Funk": {K: 0.6, P: 0.4},
    # Latin
    "Latin---Tango": {K: 1.0}, "Latin---Bossanova": {K: 1.0}, "Latin---Bolero": {K: 0.5, P: 0.5},
    "Latin---Mariachi": {K: 0.5, P: 0.5}, "Latin---Ranchera": {K: 0.5, P: 0.5},
    "Latin---Nueva Cancion": {K: 0.5, P: 0.5}, "Latin---Reggaeton": {P: 0.6, E: 0.4},
    # Non-Music
    "Non-Music---Field Recording": {},
    # Pop
    "Pop---Light Music": {K: 0.5, P: 0.5},
    # Reggae — bass-heavy sound-system styles → elektronik
    "Reggae---Dub": {E: 1.0}, "Reggae---Dancehall": {E: 0.6, P: 0.4}, "Reggae---Ragga": {E: 0.6, P: 0.4},
    # Rock — vocal pop-rock leans pop_vokal; acoustic → klasik_akustik
    "Rock---Acoustic": {K: 1.0}, "Rock---Folk Rock": {M: 0.5, K: 0.5}, "Rock---Neofolk": {K: 1.0},
    "Rock---Soft Rock": {P: 0.6, M: 0.4}, "Rock---AOR": {M: 0.5, P: 0.5}, "Rock---Pop Rock": {M: 0.5, P: 0.5},
    "Rock---Brit Pop": {M: 0.5, P: 0.5}, "Rock---Power Pop": {M: 0.5, P: 0.5},
    "Rock---Dream Pop": {M: 0.4, P: 0.6}, "Rock---Doo Wop": {P: 1.0}, "Rock---Yé-Yé": {P: 1.0},
    "Rock---Twist": {P: 0.6, M: 0.4}, "Rock---Beat": {M: 0.5, P: 0.5}, "Rock---Lounge": {K: 0.5, P: 0.5},
    "Rock---Ethereal": {P: 0.5, E: 0.5}, "Rock---New Wave": {M: 0.4, P: 0.3, E: 0.3},
    "Rock---Industrial": {M: 0.5, E: 0.5}, "Rock---Coldwave": {M: 0.5, E: 0.5},
    # Stage & Screen
    "Stage & Screen---Musical": {P: 0.5, K: 0.5},
}


def discogs_weight(label: str) -> dict[str, float]:
    if label in DISCOGS_STYLE:
        return DISCOGS_STYLE[label]
    return DISCOGS_GENRE.get(label.split("---", 1)[0], {})


MSD_TAGS: dict[str, dict[str, float]] = {
    **{t: {M: 1.0} for t in ("rock", "alternative", "alternative rock", "metal", "classic rock",
                              "indie rock", "punk", "hard rock", "heavy metal", "Progressive rock")},
    **{t: {E: 1.0} for t in ("electronic", "dance", "electronica", "electro", "Hip-Hop", "House",
                              "ambient", "chillout", "chill")},
    **{t: {K: 1.0} for t in ("jazz", "folk", "blues", "acoustic", "country", "easy listening",
                              "instrumental", "guitar")},
    **{t: {P: 1.0} for t in ("pop", "female vocalists", "male vocalists", "soul", "rnb", "indie pop",
                              "funk", "catchy", "female vocalist", "party", "sexy")},
}

_YAM_SPEECH = ("Speech", "Child speech, kid speaking", "Conversation", "Narration, monologue",
               "Speech synthesizer", "Whispering", "Laughter")
YAMNET_LABELS: dict[str, dict[str, float]] = {
    **{t: {S: 1.0} for t in _YAM_SPEECH},
    **{t: {M: 1.0} for t in ("Rock music", "Heavy metal", "Punk rock", "Grunge", "Progressive rock",
                              "Rock and roll", "Psychedelic rock", "Electric guitar")},
    **{t: {E: 1.0} for t in ("Hip hop music", "Rapping", "Beatboxing", "Electronic music", "House music",
                              "Techno", "Dubstep", "Drum and bass", "Electronica", "Electronic dance music",
                              "Ambient music", "Trance music", "Synthesizer", "Drum machine", "Dance music",
                              "Disco", "Scratching (performance technique)")},
    **{t: {K: 1.0} for t in ("Classical music", "Opera", "Orchestra", "Jazz", "Swing music", "Bluegrass",
                              "Folk music", "Country", "Blues", "Flamenco", "Middle Eastern music",
                              "Traditional music", "New-age music", "Acoustic guitar", "Piano",
                              "String section", "Violin, fiddle", "Cello", "Harp", "Carnatic music")},
    **{t: {P: 1.0} for t in ("Pop music", "Rhythm and blues", "Soul music", "Funk", "Reggae",
                              "Music of Latin America", "Salsa music", "Vocal music", "A capella",
                              "Gospel music", "Christian music", "Music of Bollywood", "Afrobeat", "Ska",
                              "Music of Asia", "Music of Africa", "Singing")},
}
YAMNET_MUSIC = "Music"


# ============================================================================
#  MODEL SPECS
# ============================================================================
@dataclass(frozen=True)
class Spec:
    name: str
    licence: str
    fe: FrontEnd
    mapping: str              # "discogs" | "msd" | "yamnet"
    sigmoid: bool = False     # output is logits


_MUSICNN_FE = dict(rate=16000, n_fft=512, win=512, hop=256, n_mels=96, fmin=0.0, fmax=8000.0,
                   mel_kind="slaney", power=2.0, log="log10_1p10000", center=True)
SPECS: dict[str, Spec] = {
    "discogs-effnet": Spec(_("Discogs-EffNet (Essentia, 400 Discogs stili)"), "CC BY-NC-SA 4.0",
                           FrontEnd(**_MUSICNN_FE, patch=128, patch_hop=62), "discogs"),
    "msd-musicnn": Spec(_("MSD-MusiCNN (Essentia, 50 etiket)"), "CC BY-NC-SA 4.0",
                        FrontEnd(**_MUSICNN_FE, patch=187, patch_hop=93), "msd"),
    "audioset-yamnet": Spec(_("YAMNet (AudioSet 521 sınıf)"), "Apache-2.0 (Google YAMNet)",
                            FrontEnd(rate=16000, n_fft=512, win=400, hop=160, n_mels=64, fmin=125.0,
                                     fmax=7500.0, mel_kind="htk_tf", power=1.0, log="ln_0.001",
                                     center=False, patch=96, patch_hop=48), "yamnet"),
}


def spec_for(path: Path) -> Spec | None:
    stem = path.name
    for key, spec in SPECS.items():
        if stem.startswith(key):
            return spec
    return None


def _session(path: Path):
    import onnxruntime as ort   # optional dependency
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.log_severity_level = 3
    opts.enable_cpu_mem_arena = False      # small batches: arena only adds resident memory
    try:   # always-on background service: no busy-wait spinning between ops
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
    except Exception:  # noqa: BLE001
        pass
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


class OnnxClassifier:
    """One ONNX model + its numpy front-end + label→group table."""

    def __init__(self, path: Path, spec: Spec, labels: list[str], session) -> None:
        self.path, self.spec, self.labels, self.session = path, spec, labels, session
        self.rate = spec.fe.rate
        self.input_name = session.get_inputs()[0].name
        outs = [o.name for o in session.get_outputs()]
        self.output_name = next((o for o in outs if "activation" in o), outs[0])
        table = {"discogs": discogs_weight, "msd": lambda l: MSD_TAGS.get(l, {}),
                 "yamnet": lambda l: YAMNET_LABELS.get(l, {})}[spec.mapping]
        self.weights = np.zeros((len(labels), len(MUSIC_GROUPS)), dtype=np.float64)
        for i, lab in enumerate(labels):
            for g, w in table(lab).items():
                self.weights[i, MUSIC_GROUPS.index(g)] = w
        self._lock = threading.Lock()

    def activations(self, x, rate: int, normalize: bool = True) -> np.ndarray | None:
        """Mean label activations over the patches of the window (None: silence/short)."""
        y = prepare(x, rate, self.rate, normalize)
        if y is None:
            return None
        fe = self.spec.fe
        p = patches(log_mel(y, fe), fe.patch, fe.patch_hop)
        if p.shape[0] == 0:
            return None
        with self._lock:
            out = self.session.run([self.output_name], {self.input_name: p})[0]
        out = np.asarray(out, dtype=np.float64).reshape(p.shape[0], -1)
        if self.spec.sigmoid:
            out = 1.0 / (1.0 + np.exp(-out))
        return out.mean(axis=0)

    def group_scores(self, act: np.ndarray, top_k: int = 0) -> np.ndarray:
        a = act.copy()
        if top_k and a.size > top_k:
            a[np.argsort(a)[:-top_k]] = 0.0
        return a @ self.weights

    def top_labels(self, act: np.ndarray, k: int = 5) -> list[tuple[str, float]]:
        order = np.argsort(act)[::-1][:k]
        return [(self.labels[i], round(float(act[i]), 3)) for i in order]


def _norm(v: np.ndarray) -> np.ndarray:
    t = float(v.sum())
    return v / t if t > 1e-9 else np.full(v.size, 1.0 / v.size)


class GenreModel:
    """``predict(x, rate) -> {group: prob}`` (sums to 1) or None.

    Main model: Discogs-EffNet styles → groups (top-K styles, so the large Rock /
    Electronic families do not win on many tiny activations). When YAMNet is installed
    next to it (``helper``), the two group distributions are averaged (``MAIN_WEIGHT``)
    and YAMNet's speech-vs-music ratio sets "konusma" (validated: docs/NOTES-ses-model.md).
    ``last_top`` keeps the main model's top labels of the last call (for `kulak tur dinle`).
    """

    TOP_K = 10
    MAIN_WEIGHT = 0.5          # EffNet share; YAMNet adds pop/akustik recall, EffNet metal/elektronik
    SPEECH_MIN_ACT = 0.1       # speech/music ratio only when one of them is clearly active

    def __init__(self, main: OnnxClassifier, helper: OnnxClassifier | None = None) -> None:
        self.main, self.helper = main, helper
        self.name = main.spec.name + (" + YAMNet" if helper else "")
        self.licence = main.spec.licence + (f"; YAMNet {helper.spec.licence}" if helper else "")
        self.last_top: list[tuple[str, float]] = []

    def predict(self, x, rate: int) -> dict[str, float] | None:
        try:
            act = self.main.activations(x, rate)
            if act is None:
                return None
            self.last_top = self.main.top_labels(act)
            p = _norm(self.main.group_scores(act, self.TOP_K if self.main.spec.mapping == "discogs" else 0))
            if self.helper is not None:
                ya = self.helper.activations(x, rate)
                if ya is not None:
                    py = _norm(self.helper.group_scores(ya, self.TOP_K))
                    p = self.MAIN_WEIGHT * p + (1.0 - self.MAIN_WEIGHT) * py
                    sp = self.speech_ratio(ya)
                    if sp is not None:
                        si = MUSIC_GROUPS.index(S)
                        p = np.insert(_norm(np.delete(p, si)) * (1.0 - sp), si, sp)
            return {g: round(float(v), 4) for g, v in zip(MUSIC_GROUPS, p)}
        except Exception as e:  # noqa: BLE001 — never break the caller
            log.warning("ses modeli tahmini başarısız: %s", e)
            return None

    def speech_ratio(self, act: np.ndarray) -> float | None:
        """P(speech, not music) from YAMNet activations: max speech class vs "Music"."""
        lab = self.helper.labels
        sp = max((float(act[lab.index(t)]) for t in _YAM_SPEECH if t in lab), default=0.0)
        mu = float(act[lab.index(YAMNET_MUSIC)]) if YAMNET_MUSIC in lab else 0.0
        if max(sp, mu) < self.SPEECH_MIN_ACT:
            return None
        return sp / (sp + mu)


def _load_one(path: Path) -> OnnxClassifier | None:
    spec = spec_for(path)
    meta = path.with_suffix(".json")
    if spec is None or not path.is_file() or not meta.is_file():
        return None
    labels = json.loads(meta.read_text("utf-8")).get("classes") or []
    session = _session(path)
    clf = OnnxClassifier(path, spec, [str(l) for l in labels], session)
    out_dim = session.get_outputs()[[o.name for o in session.get_outputs()].index(clf.output_name)].shape[-1]
    if isinstance(out_dim, int) and out_dim != len(labels):
        raise ValueError(f"{path.name}: {out_dim} çıkış, {len(labels)} etiket")
    return clf


def load_model(path: str | os.PathLike | None = None, helper: bool = True) -> GenreModel | None:
    """Load the genre model; None when onnxruntime / the model file is missing or broken.

    ``path``: model file or directory (default ~/.local/share/oto-eq/models/). In a
    directory the main model is ``discogs-effnet-bsdynamic-1.onnx``; YAMNet next to it
    is used as helper (genre average + speech) when ``helper`` is True.
    """
    p = Path(path).expanduser() if path else MODEL_DIR
    main_path = p / MAIN_MODEL if p.is_dir() or not p.suffix else p
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        log.info("onnxruntime yok: ses modeli kapalı")
        return None
    try:
        main = _load_one(main_path)
        if main is None:
            log.info("ses modeli bulunamadı: %s", main_path)
            return None
        yam_clf = None
        yam = main_path.parent / SPEECH_MODEL
        if helper and main.spec.mapping != "yamnet" and yam.is_file():
            try:
                yam_clf = _load_one(yam)
            except Exception as e:  # noqa: BLE001
                log.warning("YAMNet yüklenemedi (konuşma ayrımı ana modelle): %s", e)
        model = GenreModel(main, yam_clf)
        log.info("ses modeli yüklendi: %s", model.name)
        return model
    except Exception as e:  # noqa: BLE001
        log.warning("ses modeli yüklenemedi (%s): %s", main_path, e)
        return None
