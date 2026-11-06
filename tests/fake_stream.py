"""Spawn a SILENT test playback stream with chosen properties.

    with fake_stream({"application.name": "oto-eq-test-cs2",
                      "application.process.binary": "cs2"}, kind="pulse") as s:
        idx = s.wait_index()

* Audio is always all-zero samples (WAV written with ``wave`` into a temp
  dir) and the stream volume is 0 as well.
* ``kind="native"``: ``pw-play -P '<json props>' --volume 0``.  The ``-P``
  props become node props, so ``application.name`` /
  ``application.process.binary`` show up on the sink-input; the pw-play
  *client* keeps ``application.process.binary=pw-cat`` and the node has no
  ``application.process.id`` (pulse.list_streams merges it from the client).
  ``node.name`` stays ``pw-play`` unless given.
* ``kind="pulse"``: ``paplay --volume=0 --property=k=v ...``; the same keys
  are additionally passed via ``PULSE_PROP`` (libpulse client proplist), so
  ``application.name`` / ``application.process.binary`` really override the
  ``paplay`` defaults.
* A unique ``oto_eq.test_id=<uuid>`` property marks the stream so that
  ``wait_index()`` finds exactly this sink-input.
* ``no_restore=True`` (default) adds ``state.restore-props=false`` and
  ``state.restore-target=false`` so WirePlumber does not remember volume or
  target for the test app name in ~/.local/state/wireplumber/stream-properties.
* The child process is killed on exit, also on exceptions.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from oto_eq import config, pulse

MARKER_KEY = "oto_eq.test_id"
RATE = 48000
CHANNELS = 2


def write_silence_wav(path: Path, seconds: float, rate: int = RATE,
                      channels: int = CHANNELS) -> Path:
    """Write an all-zero s16 WAV file."""
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        chunk = b"\0" * (rate * channels * 2)   # one second of zeros
        left = frames
        while left > 0:
            n = min(left, rate)
            w.writeframes(chunk[: n * channels * 2])
            left -= n
    return path


def _pulse_prop_env(props: dict[str, str]) -> str:
    """PULSE_PROP syntax: space separated key="value" pairs."""
    parts = []
    for k, v in props.items():
        v = str(v).replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'{k}="{v}"')
    return " ".join(parts)


class FakeStream:
    """Handle returned by :func:`fake_stream`."""

    def __init__(self, props: dict[str, str], kind: str, proc: subprocess.Popen,
                 test_id: str, cmd: list[str]):
        self.props = props
        self.kind = kind
        self.proc = proc
        self.test_id = test_id
        self.cmd = cmd
        self.index: int | None = None

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def find(self) -> pulse.Stream | None:
        """Current sink-input for this stream, or None."""
        for s in pulse.list_streams():
            if s.props.get(MARKER_KEY) == self.test_id:
                return s
        return None

    def wait_index(self, timeout: float = 5.0) -> int:
        """Block until the sink-input shows up; return its index."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.alive():
                err = self.proc.stderr.read() if self.proc.stderr else ""
                raise RuntimeError(f"test akışı süreci erken bitti "
                                   f"(kod {self.proc.returncode}): {err.strip()}")
            s = self.find()
            if s is not None:
                self.index = s.index
                return s.index
            if time.monotonic() >= deadline:
                raise TimeoutError(f"sink-input {timeout} sn içinde görünmedi: {self.cmd}")
            time.sleep(0.05)

    def stop(self) -> None:
        p = self.proc
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
        for f in (p.stdout, p.stderr):
            if f:
                f.close()


@contextmanager
def fake_stream(props: dict[str, str], kind: str = "native", duration: float = 60,
                target: str | None = None, no_restore: bool = True) -> Iterator[FakeStream]:
    """Context manager yielding a running silent FakeStream (see module doc)."""
    if kind not in ("native", "pulse"):
        raise ValueError(f"kind must be 'native' or 'pulse', not {kind!r}")
    test_id = uuid.uuid4().hex
    all_props = {str(k): str(v) for k, v in props.items()}
    if no_restore:
        all_props.setdefault("state.restore-props", "false")
        all_props.setdefault("state.restore-target", "false")
    all_props[MARKER_KEY] = test_id

    tmp = Path(tempfile.mkdtemp(prefix="oto-eq-fake-"))
    proc = None
    try:
        wav = write_silence_wav(tmp / "sessizlik.wav", duration)
        # C.UTF-8: untranslated like config.C_ENV, but paplay rejects non-ASCII
        # names/props under plain LC_ALL=C ("Invalid stream name").
        env = {**config.C_ENV, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
        if kind == "native":
            exe = shutil.which("pw-play") or shutil.which("pw-cat")
            if not exe:
                raise FileNotFoundError("pw-play bulunamadı")
            cmd = [exe]
            if os.path.basename(exe) == "pw-cat":
                cmd.append("--playback")
            cmd += ["--volume", "0", "-P", json.dumps(all_props, ensure_ascii=False)]
            if "media.role" in all_props:
                cmd += ["--media-role", all_props["media.role"]]
            if target:
                cmd += ["--target", target]
            cmd.append(str(wav))
        else:
            exe = shutil.which("paplay")
            if not exe:
                raise FileNotFoundError("paplay bulunamadı")
            cmd = [exe, "--volume=0"]
            if "application.name" in all_props:
                cmd.append(f"--client-name={all_props['application.name']}")
            if "media.name" in all_props:
                cmd.append(f"--stream-name={all_props['media.name']}")
            cmd += [f"--property={k}={v}" for k, v in all_props.items()]
            if target:
                cmd.append(f"--device={target}")
            cmd.append(str(wav))
            env["PULSE_PROP"] = _pulse_prop_env(
                {k: v for k, v in all_props.items() if k.startswith("application.")})
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, env=env)
        fs = FakeStream(all_props, kind, proc, test_id, cmd)
        try:
            yield fs
        finally:
            fs.stop()
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        shutil.rmtree(tmp, ignore_errors=True)


def describe(cmd: list[str]) -> str:
    """Shell-quoted command line (for logs/docs)."""
    return shlex.join(cmd)
