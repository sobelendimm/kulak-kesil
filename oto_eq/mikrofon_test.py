"""Mikrofon A/B testi: ham HS80 ve Kulak Kesil · Mikrofon aynı anda kaydedilir,
seviyeleri gösterilir, önce ham sonra işlenmiş ses geri çalınır.

    python3 -m oto_eq.mikrofon_test [saniye]

Machine-readable output (CHANGE 8): the GUI (app.MicTestDialog) reads this script's
stdout through a pipe and looks for the row labels "Ham" / "İşlenmiş" and the words
"çalınıyor" / "ham" / "işlenmiş". Those lines stay these fixed Turkish keys unless the
output is a terminal (a person reading it), where they are translated like the rest.
``OTO_EQ_MAKINE=1`` / ``=0`` forces either form.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from . import config, i18n, pulse
from .i18n import N_, _

VIRTUAL = "oto_eq_mikrofon"
# fixed keys of the machine-readable lines → their (translatable) text for a person
SATIR = {"ham": N_("Ham"), "islenmis": N_("İşlenmiş")}
CALINIYOR = {"ham": N_("🔊 ham ses çalınıyor…"), "islenmis": N_("🔊 işlenmiş ses çalınıyor…")}


def _makine() -> bool:
    """True = keep the fixed Turkish keys (the GUI parses them)."""
    v = os.environ.get("OTO_EQ_MAKINE", "").strip()
    if v in ("0", "1"):
        return v == "1"
    try:
        return not sys.stdout.isatty()
    except (AttributeError, ValueError):
        return True


def _metin(msgid: str, makine: bool) -> str:
    return msgid if makine else _(msgid)


def _hs80_source() -> str | None:
    out = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                         text=True, env=config.C_ENV, timeout=5).stdout
    for line in out.splitlines():
        name = line.split("\t")[1] if "\t" in line else ""
        if "alsa_input" in name and "hs80" in name.lower():
            return name
    return None


SESSIZ = (-240.0, -240.0, -240.0)       # no usable audio (empty/short/broken recording)


def _levels(path: Path) -> tuple[float, float, float]:
    try:
        raw = path.read_bytes()
    except OSError:
        return SESSIZ
    i = raw.find(b"data")
    body = raw[i + 8:]
    x = np.frombuffer(body[:len(body) - len(body) % 4], dtype=np.float32)[4800:]
    win = 960
    if len(x) < win:                     # recording stopped early / device gave nothing
        return SESSIZ
    k = max(1, len(x) // win)
    r = np.sqrt(np.mean(x[:k * win].reshape(k, win) ** 2, axis=1)) + 1e-12
    db = 20 * np.log10(r)
    speech = db[db > np.percentile(db, 60)]
    return (float(np.percentile(db, 10)), float(np.mean(speech)) if speech.size else -240.0,
            float(20 * np.log10(np.max(np.abs(x)) + 1e-12)))


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    raw_src = _hs80_source()
    if raw_src is None:
        print(_("HS80 mikrofonu bulunamadı."))
        return 1
    if VIRTUAL not in {s.name for s in pulse.list_sinks()} and VIRTUAL not in \
            subprocess.run(["pactl", "list", "short", "sources"], capture_output=True,
                           text=True, env=config.C_ENV, timeout=5).stdout:
        print(_("Kulak Kesil · Mikrofon çalışmıyor ({komut}).").format(
            komut="systemctl --user start oto-eq-mic"))
        return 1
    # The GUI may stop us mid-run (window closed): SIGTERM → SystemExit, so the finally
    # blocks below stop the recorders and the temp dir (the user's voice) is removed.
    signal.signal(signal.SIGTERM, lambda *_a: sys.exit(1))
    # second guard: even a SIGKILLed parent cannot leave a recorder running
    guard = ["timeout", f"{seconds + 10:.0f}"] if shutil.which("timeout") else []
    with tempfile.TemporaryDirectory(prefix="kulak-mik-") as d:
        files = {"ham": Path(d) / "ham.wav", "islenmis": Path(d) / "islenmis.wav"}
        print(_("🎙  {saniye:.0f} saniye boyunca normal sesinle konuş…").format(saniye=seconds))
        procs: list[subprocess.Popen] = []
        try:
            for src, path in ((raw_src, files["ham"]), (VIRTUAL, files["islenmis"])):
                procs.append(subprocess.Popen(
                    [*guard, "pw-record", "--target", src, "--rate", "48000",
                     "--channels", "1", "--format", "f32",
                     "-P", '{ "application.name": "Kulak Kesil mikrofon testi" }', str(path)],
                    env=config.C_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(seconds + 0.3)
        finally:
            _stop_all(procs)
        makine = _makine()
        # header: 4 words whose first is never a row key (the GUI skips it)
        heads = [_metin(h, makine) for h in (N_("gürültü"), N_("konuşma"), N_("tepe"))]
        labels = {key: _metin(SATIR[key], makine) for key in ("ham", "islenmis")}
        w0 = max([10] + [i18n.genislik(x) for x in labels.values()])      # cells (CJK = 2)
        w = [max(x, i18n.genislik(h)) for x, h in zip((9, 9, 8), heads)]
        print("\n" + " " * w0 + "".join(" " + i18n.saga(h, x) for h, x in zip(heads, w)) + "  (dBFS)")
        for key in ("ham", "islenmis"):
            vals = _levels(files[key])
            print(i18n.sola(labels[key], w0)
                  + "".join(" " + i18n.saga(f"{v:.1f}", x) for v, x in zip(vals, w)))
        for key in ("ham", "islenmis"):
            path = files[key]
            print(_metin(CALINIYOR[key], makine))
            play = subprocess.Popen(["pw-play", "-P",
                                     '{ "application.name": "Kulak Kesil mikrofon testi" }',
                                     str(path)], env=config.C_ENV)
            try:
                play.wait(timeout=seconds + 10)
            except subprocess.TimeoutExpired:
                pass
            finally:
                _stop_all([play])
    return 0


def _stop_all(procs: list[subprocess.Popen]) -> None:
    """Terminate + reap children (escalate to SIGKILL); never raises."""
    for p in procs:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass


if __name__ == "__main__":
    sys.exit(main())
