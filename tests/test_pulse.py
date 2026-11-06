"""Tests for oto_eq.pulse (pure parsing + live tests against pipewire-pulse).

Run from the project root:
    python3 -m unittest discover -s tests -t . -p 'test_pulse.py'

Live tests create two temporary null sinks (oto_eq_test_null, *_null2) and
silent volume-0 test streams that never touch the real headset sink; all of
it is torn down in cleanups. They are skipped if pactl / the server is
unavailable.

Flakiness under load (CHANGE 6): ``pactl move-sink-input`` exits 0 but silently
does nothing in ~1/180 moves when idle and ~21 % under heavy pactl load
(NOTES-router.md "snap-back"; production therefore moves with pw-metadata).
The live move checks retry the pactl move (``MOVE_TRIES``) and, if every try is
lost, try the pw-metadata path: success there → the test is SKIPPED with the
reason (pactl lost the moves, pw-metadata works); both failing → a real failure,
unless the machine is under heavy load (skip with the load figure).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import time
import unittest
from unittest import mock

from oto_eq import config, pulse
from oto_eq.models import Event

try:
    from tests.fake_stream import fake_stream, MARKER_KEY
except ImportError:  # run with -s tests and no -t
    from fake_stream import fake_stream, MARKER_KEY  # type: ignore


# keep expected warnings (missing pactl, killed subscribe) out of test output
logging.getLogger("oto_eq.pulse").addHandler(logging.NullHandler())

NULL_SINK = "oto_eq_test_null"
NULL_SINK2 = "oto_eq_test_null2"


# ===================================================================== pure

class ParseStreamsTest(unittest.TestCase):
    def test_json_basic_fields(self):
        streams = pulse.parse_streams_json(SINK_INPUTS_JSON)
        self.assertEqual([s.index for s in streams], [648, 2442, 3147, 3148])
        sd = streams[0]
        self.assertEqual(sd.sink_index, 67)
        self.assertEqual(sd.app_name, "speech-dispatcher-dummy")
        self.assertEqual(sd.binary, "sd_dummy")
        self.assertEqual(sd.pid, 38763)
        self.assertEqual(sd.media_name, "playback")
        self.assertEqual(sd.node_name, "speech-dispatcher-dummy")
        self.assertIs(sd.corked, False)
        self.assertEqual(sd.props[pulse.CLIENT_KEY], "647")
        for s in streams:
            self.assertTrue(all(isinstance(v, str) for v in s.props.values()))

    def test_json_native_stream_has_no_pid(self):
        native = pulse.parse_streams_json(SINK_INPUTS_JSON)[2]
        self.assertEqual(native.app_name, "oto-eq-test-native")
        self.assertEqual(native.binary, "cs2")      # given via pw-play -P
        self.assertIsNone(native.pid)               # node props lack it
        self.assertEqual(native.node_name, "pw-play")

    def test_text_equals_json(self):
        a = pulse.parse_streams_json(SINK_INPUTS_JSON)
        b = pulse.parse_streams_text(SINK_INPUTS_TEXT)
        self.assertEqual(a, b)
        for x, y in zip(a, b):
            self.assertEqual(x.props, y.props)

    def test_tricky_media_name(self):
        want = 'Şarkı "alıntı" \\ ters\tsekme = x'
        for streams in (pulse.parse_streams_json(SINK_INPUTS_JSON),
                        pulse.parse_streams_text(SINK_INPUTS_TEXT)):
            self.assertEqual(streams[3].media_name, want)
            self.assertIn("İYİSİ", streams[1].media_name)

    def test_text_multiline_value(self):
        txt = ("Sink Input #5\n\tSink: 3\n\tClient: 9\n\tCorked: yes\n\tProperties:\n"
               '\t\tmedia.name = "line one\nline two"\n'
               '\t\tapplication.name = "x"\n')
        s = pulse.parse_streams_text(txt)[0]
        self.assertEqual(s.media_name, "line one\nline two")
        self.assertEqual(s.app_name, "x")
        self.assertIs(s.corked, True)
        self.assertEqual((s.index, s.sink_index), (5, 3))

    def test_missing_props_and_invalid_sink(self):
        data = json.dumps([{"index": 7, "sink": 4294967295, "corked": True}])
        s = pulse.parse_streams_json(data)[0]
        self.assertEqual((s.index, s.sink_index, s.app_name, s.binary, s.pid),
                         (7, -1, "", "", None))
        self.assertTrue(s.corked)
        self.assertEqual(s.props, {})

    def test_non_str_props_coerced(self):
        data = json.dumps([{"index": 1, "sink": 2, "corked": False,
                            "properties": {"a": 5, "b": True, "c": None,
                                           "application.process.id": "x"}}])
        s = pulse.parse_streams_json(data)[0]
        self.assertEqual(s.props["a"], "5")
        self.assertEqual(s.props["b"], "true")
        self.assertEqual(s.props["c"], "")
        self.assertIsNone(s.pid)

    def test_bad_json_raises_value_error(self):
        with self.assertRaises(ValueError):
            pulse.parse_streams_json(SINK_INPUTS_JSON.replace('\\"alıntı', '\\xalıntı'))


class ParseSinksTest(unittest.TestCase):
    def test_json(self):
        sinks = pulse.parse_sinks_json(SINKS_JSON)
        self.assertEqual([s.index for s in sinks], [67, 535])
        self.assertTrue(sinks[0].name.startswith("alsa_output.usb-Corsair"))
        self.assertEqual(sinks[0].state, "RUNNING")
        self.assertEqual(sinks[1].state, "SUSPENDED")
        self.assertIn("Sayısal", sinks[1].description)

    def test_text_equals_json(self):
        self.assertEqual(pulse.parse_sinks_text(SINKS_TEXT), pulse.parse_sinks_json(SINKS_JSON))


class MergeClientTest(unittest.TestCase):
    def test_native_gets_client_pid_and_portal(self):
        streams = pulse.parse_streams_json(SINK_INPUTS_JSON)
        clients = {
            3146: {"application.name": "pw-cat", "application.process.binary": "pw-cat",
                   "application.process.id": "4242", "pipewire.sec.pid": "4242",
                   "pipewire.access": "flatpak",
                   "pipewire.access.portal.app_id": "com.spotify.Client"},
            647: {"client.api": "pipewire-pulse", "pipewire.sec.pid": "1202",
                  "application.process.id": "1"},
        }
        merged = pulse.merge_client_props(streams, clients)
        native = merged[2]
        self.assertEqual(native.pid, 4242)
        self.assertEqual(native.binary, "cs2")              # stream key wins
        self.assertEqual(native.app_name, "oto-eq-test-native")
        self.assertEqual(native.props["pipewire.sec.pid"], "4242")
        self.assertEqual(native.props["pipewire.access.portal.app_id"], "com.spotify.Client")
        self.assertIn("application.process.id", native.props["oto_eq.from_client"])
        sd = merged[0]
        self.assertEqual(sd.pid, 38763)                      # not overwritten
        self.assertNotIn("pipewire.sec.pid", sd.props)       # pulse server PID skipped
        self.assertIs(merged[1], streams[1])                 # no client → unchanged

    def test_clients_text_parse(self):
        txt = ('Client #12\n\tDriver: PipeWire\n\tOwner Module: n/a\n\tProperties:\n'
               '\t\tapplication.name = "Firefox"\n\t\tapplication.process.id = "9"\n')
        self.assertEqual(pulse.parse_clients_text(txt),
                         {12: {"application.name": "Firefox", "application.process.id": "9"}})


class EventLineTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(pulse.parse_event_line("Event 'new' on sink-input #12\n"),
                         Event("new", "sink-input", 12))
        self.assertEqual(pulse.parse_event_line("Event 'change' on server #4294967295"),
                         Event("change", "server", -1))
        self.assertEqual(pulse.parse_event_line("Event 'remove' on source-output #3"),
                         Event("remove", "source-output", 3))
        self.assertIsNone(pulse.parse_event_line("garbage"))
        self.assertIsNone(pulse.parse_event_line(""))


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess(["pactl"], rc, stdout, stderr)


class FallbackTest(unittest.TestCase):
    def test_broken_json_falls_back_to_text(self):
        broken = SINK_INPUTS_JSON.replace('\\"alıntı', '\\xalıntı')

        def fake_run(args, timeout=pulse.TIMEOUT):
            if args[:2] == ["-f", "json"]:
                return _cp(broken if args[-1] == "sink-inputs" else "[]")
            if args == ["list", "sink-inputs"]:
                return _cp(SINK_INPUTS_TEXT)
            return _cp("", 1, "unexpected")

        with mock.patch.object(pulse, "_run", side_effect=fake_run), \
                self.assertLogs("oto_eq.pulse", "WARNING"):
            streams = pulse.list_streams()
        self.assertEqual([s.index for s in streams], [648, 2442, 3147, 3148])

    def test_pactl_missing(self):
        with mock.patch.object(pulse, "PACTL", "/nonexistent/pactl"):
            with self.assertRaises(pulse.PulseError):
                pulse.list_sinks()
            with self.assertRaises(pulse.PulseError):
                pulse.default_sink_name()
            self.assertFalse(pulse.move_stream(1, "x"))

    def test_listener_stops_while_backing_off(self):
        with mock.patch.object(pulse, "PACTL", "/nonexistent/pactl"):
            lst = pulse.EventListener(lambda ev: None)
            lst.start()
            time.sleep(0.8)
            t = time.monotonic()
            lst.stop()
            self.assertLess(time.monotonic() - t, 1.0)
            self.assertFalse(lst.is_running())


# ===================================================================== live

def _pactl(*args, timeout=5):
    return subprocess.run(["pactl", *args], capture_output=True, text=True,
                          env=config.C_ENV, timeout=timeout)


def _server_ok() -> bool:
    if not shutil.which("pactl"):
        return False
    try:
        return _pactl("info").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


MOVE_TRIES = 4           # P(all lost) ≈ 0.21**4 ≈ 0.2 % even under heavy load
LIVE_TIMEOUT = 10.0      # generous: stream/sink start-up is slow under load
HEAVY_LOAD = 1.5         # 1-min load average per CPU considered "heavy"


def _load_text() -> str:
    try:
        l1 = os.getloadavg()[0]
    except OSError:
        return "yük bilinmiyor"
    return f"yük {l1:.1f} / {os.cpu_count() or 1} çekirdek"


def _heavy_load() -> bool:
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1) >= HEAVY_LOAD
    except OSError:
        return False


def _pw_metadata_move(stream, sink: str) -> bool:
    """The production move path (router.MetadataMover), inlined to stay independent."""
    nid = str((stream.props or {}).get("object.id", "")).strip()
    if not nid.isdigit() or not shutil.which("pw-metadata"):
        return False
    try:
        p = subprocess.run(["pw-metadata", "-n", "default", "--", nid, "target.object", sink,
                            "Spa:String"], capture_output=True, text=True, env=config.C_ENV,
                           timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _wait(pred, timeout=5.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


@unittest.skipUnless(_server_ok(), "pactl / pipewire-pulse yok")
class LiveTest(unittest.TestCase):
    """Real pipewire-pulse. Streams start on NULL_SINK2 and move to NULL_SINK."""

    @classmethod
    def setUpClass(cls):
        cls.default_before = pulse.default_sink_name()
        cls.modules = []
        try:
            for name in (NULL_SINK, NULL_SINK2):
                p = _pactl("load-module", "module-null-sink", f"sink_name={name}",
                           "sink_properties='device.description=oto-eq-test "
                           "state.restore-props=false'")
                if p.returncode != 0:
                    raise unittest.SkipTest(f"null sink yüklenemedi: {p.stderr}")
                cls.modules.append(p.stdout.strip())
            if not _wait(lambda: {NULL_SINK, NULL_SINK2} <= {s.name for s in pulse.list_sinks()},
                         timeout=LIVE_TIMEOUT):
                raise RuntimeError("null sink'ler görünmedi")
        except BaseException:
            cls._unload()
            raise

    @classmethod
    def _unload(cls):
        for m in reversed(cls.modules):
            _pactl("unload-module", m)
        cls.modules = []

    @classmethod
    def tearDownClass(cls):
        cls._unload()
        names = {s.name for s in pulse.list_sinks()}
        assert NULL_SINK not in names and NULL_SINK2 not in names, names

    def sink_index(self, name):
        return next(s.index for s in pulse.list_sinks() if s.name == name)

    PROPS = {"application.name": "oto-eq-test-cs2", "application.process.binary": "cs2",
             "media.role": "game"}

    def test_default_sink_untouched(self):
        self.assertEqual(pulse.default_sink_name(), self.default_before)
        self.assertIn(self.default_before, {s.name for s in pulse.list_sinks()})

    def _lost_moves(self, s, sink: str, arrived) -> None:
        """Every pactl move was silently lost: try pw-metadata, then skip or fail."""
        st = s.find()
        if st is not None and _pw_metadata_move(st, sink) and arrived(LIVE_TIMEOUT / 2):
            self.skipTest(f"pactl move-sink-input {MOVE_TRIES} kez sessizce etkisiz kaldı "
                          f"({_load_text()}); pw-metadata yolu çalıştı (üretim onu kullanır)")
        if _heavy_load():
            self.skipTest(f"sistem çok yüklü ({_load_text()}): taşıma {MOVE_TRIES} denemede "
                          "gerçekleşmedi; canlı taşıma testi güvenilir değil")
        self.fail(f"akış {sink} çıkışına taşınamadı (pactl {MOVE_TRIES} deneme + pw-metadata, "
                  f"{_load_text()})")

    def _move_verified(self, s, idx: int, st, sink: str) -> None:
        """pulse.move_stream + wait until the server shows the new sink (retried)."""
        target = self.sink_index(sink)

        def arrived(timeout):
            return _wait(lambda: (s.find() or st).sink_index == target, timeout=timeout)
        for attempt in range(MOVE_TRIES):
            ok = pulse.move_stream(idx, sink)
            if attempt == 0:
                self.assertTrue(ok, "pactl move-sink-input başarısız döndü")
            if arrived(2.0 + attempt):
                return
        self._lost_moves(s, sink, arrived)

    def _check_props_and_move(self, kind):
        with fake_stream(self.PROPS, kind=kind, target=NULL_SINK2) as s:
            idx = s.wait_index(timeout=LIVE_TIMEOUT)
            # a new stream can be listed before it is linked (sink -1): wait for the link
            first = self.sink_index(NULL_SINK2)
            st = _wait(lambda: next((x for x in pulse.list_streams()
                                     if x.index == idx and x.sink_index == first), None),
                       timeout=LIVE_TIMEOUT) \
                or next(x for x in pulse.list_streams() if x.index == idx)
            self.assertEqual(st.app_name, "oto-eq-test-cs2")
            self.assertEqual(st.binary, "cs2")
            self.assertEqual(st.media_role.lower(), "game")
            self.assertEqual(st.pid, s.pid)          # native: merged from client
            self.assertEqual(st.props[MARKER_KEY], s.test_id)
            self.assertEqual(st.sink_index, self.sink_index(NULL_SINK2))
            self._move_verified(s, idx, st, NULL_SINK)
        # gone after exit
        self.assertTrue(_wait(lambda: all(x.index != idx for x in pulse.list_streams()),
                              timeout=LIVE_TIMEOUT))
        self.assertFalse(pulse.move_stream(idx, NULL_SINK))

    def test_native_props_and_move(self):
        self._check_props_and_move("native")

    def test_pulse_props_and_move(self):
        self._check_props_and_move("pulse")

    def test_move_to_missing_sink(self):
        with fake_stream(self.PROPS, kind="pulse", target=NULL_SINK2) as s:
            idx = s.wait_index(timeout=LIVE_TIMEOUT)
            self.assertFalse(pulse.move_stream(idx, "oto_eq_test_yok"))

    def _events(self, kind):
        q: queue.Queue[Event] = queue.Queue()
        lst = pulse.EventListener(q.put)
        lst.start()
        self.addCleanup(lst.stop)
        time.sleep(0.3)   # let pactl subscribe connect

        def wait_for(kind_, idx, timeout=LIVE_TIMEOUT, required=True):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    ev = q.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if ev.facility == "sink-input" and ev.kind == kind_ and ev.index == idx:
                    return ev
            if required:
                self.fail(f"{kind_} olayı gelmedi (#{idx}, {_load_text()})")
            return None

        with fake_stream(self.PROPS, kind=kind, target=NULL_SINK2) as s:
            idx = s.wait_index(timeout=LIVE_TIMEOUT)
            wait_for("new", idx)
            target = self.sink_index(NULL_SINK)
            # a lost pactl move produces no "change": retry until the stream really moved
            for attempt in range(MOVE_TRIES):
                pulse.move_stream(idx, NULL_SINK)
                if wait_for("change", idx, timeout=2.0 + attempt, required=False) \
                        and _wait(lambda: getattr(s.find(), "sink_index", None) == target,
                                  timeout=2.0):
                    break
            else:
                self._lost_moves(s, NULL_SINK, lambda t: wait_for("change", idx, t, False)
                                 is not None)
        wait_for("remove", idx)
        lst.stop()
        self.assertFalse(lst.is_running())

    def test_events_native(self):
        self._events("native")

    def test_events_pulse(self):
        self._events("pulse")

    def test_listener_restarts_after_pactl_dies(self):
        q: queue.Queue[Event] = queue.Queue()
        lst = pulse.EventListener(q.put)
        lst.start()
        self.addCleanup(lst.stop)
        self.assertTrue(_wait(lambda: lst._proc is not None))
        time.sleep(0.2)
        old = lst._proc
        old.kill()
        ev = q.get(timeout=LIVE_TIMEOUT)
        while not (ev.facility == "server" and ev.index == -1):
            ev = q.get(timeout=LIVE_TIMEOUT)
        self.assertEqual(lst.restarts, 1)
        self.assertIsNot(lst._proc, old)
        time.sleep(0.2)
        with fake_stream(self.PROPS, kind="pulse", target=NULL_SINK2) as s:
            idx = s.wait_index(timeout=LIVE_TIMEOUT)
            got = _wait(lambda: any(e.kind == "new" and e.index == idx
                                    for e in list(q.queue)), timeout=LIVE_TIMEOUT)
            self.assertTrue(got, "yeniden başlatmadan sonra olay gelmedi")
        proc = lst._proc
        lst.stop()
        self.assertIsNotNone(proc.poll())
        self.assertFalse(lst.is_running())

    def test_fake_stream_killed_on_exception(self):
        with self.assertRaises(ZeroDivisionError):
            with fake_stream(self.PROPS, kind="native", target=NULL_SINK2) as s:
                s.wait_index(timeout=LIVE_TIMEOUT)
                proc = s.proc
                1 / 0
        self.assertIsNotNone(proc.poll())


# ===================================================================== samples
# Captured live (pactl 17, PipeWire 1.6.10, LC_ALL=C): two real streams plus
# fake_stream test streams (native #3147, pulse #3148) with a tricky media.name.

SINK_INPUTS_JSON = r'''[{"index":648,"driver":"PipeWire","owner_module":null,"client":"647","sink":67,"sample_specification":"s16le 1ch 44100Hz","channel_map":"mono","format":"pcm, format.sample_format = \"\\\"s16le\\\"\"  format.rate = \"44100\"  format.channels = \"1\"  format.channel_map = \"\\\"mono\\\"\"","corked":false,"mute":false,"volume":{"mono":{"value":65536,"value_percent":"100%","db":"0.00 dB"}},"balance":0.00,"buffer_latency_usec":0.00,"sink_latency_usec":0.00,"resample_method":"PipeWire","properties":{"client.api":"pipewire-pulse","pulse.server.type":"unix","application.name":"speech-dispatcher-dummy","application.process.id":"38763","application.process.user":"user","application.process.host":"desktop","application.process.binary":"sd_dummy","application.language":"C","window.x11.display":":0","application.process.machine_id":"9f282b0d49e04a2b88d500945ce09107","application.process.session_id":"2","pulse.min.req":"512/48000","pulse.min.quantum":"512/48000","pulse.idle.timeout":"5","media.name":"playback","pulse.corked":"false","node.rate":"1/44100","node.latency":"471/44100","stream.is-live":"true","node.name":"speech-dispatcher-dummy","node.want-driver":"true","node.autoconnect":"true","media.class":"Stream/Output/Audio","port.group":"stream.0","adapt.follower.spa-node":"","object.register":"false","factory.id":"7","clock.quantum-limit":"8192","node.loop.name":"data-loop.0","library.name":"audioconvert/libspa-audioconvert","client.id":"81","object.id":"94","object.serial":"648","pulse.attr.minreq":"942","pulse.attr.maxlength":"4194304","pulse.attr.tlength":"2826","pulse.attr.prebuf":"1886","module-stream-restore.id":"sink-input-by-application-name:speech-dispatcher-dummy"}},{"index":2442,"driver":"PipeWire","owner_module":null,"client":"2425","sink":67,"sample_specification":"float32le 2ch 48000Hz","channel_map":"front-left,front-right","format":"pcm, format.sample_format = \"\\\"float32le\\\"\"  format.rate = \"48000\"  format.channels = \"2\"  format.channel_map = \"\\\"front-left,front-right\\\"\"","corked":false,"mute":false,"volume":{"front-left":{"value":65536,"value_percent":"100%","db":"0.00 dB"},"front-right":{"value":65536,"value_percent":"100%","db":"0.00 dB"}},"balance":0.00,"buffer_latency_usec":0.00,"sink_latency_usec":0.00,"resample_method":"PipeWire","properties":{"client.api":"pipewire-pulse","pulse.server.type":"unix","application.name":"Firefox","application.process.id":"90832","application.process.user":"user","application.process.host":"desktop","application.process.binary":"firefox","application.language":"tr_TR.UTF-8","window.x11.display":":0","application.process.machine_id":"9f282b0d49e04a2b88d500945ce09107","application.process.session_id":"2","module-stream-restore.id":"sink-input-by-application-name:Firefox","media.name":"(211) SON 10 YILIN EN İYİSİ BU! (BU OYUNUN GRAFİKLER FENA!) - YouTube","pulse.corked":"false","node.rate":"1/48000","node.latency":"3600/48000","stream.is-live":"true","node.name":"Firefox","node.want-driver":"true","node.autoconnect":"true","media.class":"Stream/Output/Audio","port.group":"stream.0","adapt.follower.spa-node":"","object.register":"false","factory.id":"7","clock.quantum-limit":"8192","node.loop.name":"data-loop.0","library.name":"audioconvert/libspa-audioconvert","client.id":"110","object.id":"112","object.serial":"2442","pulse.attr.maxlength":"4194304","pulse.attr.tlength":"48000","pulse.attr.prebuf":"38408","pulse.attr.minreq":"9600","node.driver-id":"42"}},{"index":3147,"driver":"PipeWire","owner_module":null,"client":"3146","sink":3139,"sample_specification":"s16le 2ch 48000Hz","channel_map":"front-left,front-right","format":"pcm, format.sample_format = \"\\\"s16le\\\"\"  format.rate = \"48000\"  format.channels = \"2\"  format.channel_map = \"\\\"front-left,front-right\\\"\"","corked":false,"mute":false,"volume":{"front-left":{"value":65536,"value_percent":"100%","db":"0.00 dB"},"front-right":{"value":65536,"value_percent":"100%","db":"0.00 dB"}},"balance":0.00,"buffer_latency_usec":0.00,"sink_latency_usec":0.00,"resample_method":"PipeWire","properties":{"application.name":"oto-eq-test-native","node.name":"pw-play","application.process.binary":"cs2","media.name":"Şarkı \"alıntı\" \\ ters\tsekme = x","state.restore-props":"false","state.restore-target":"false","oto_eq.test_id":"50fa51eac1664ea88b9250a912a4ae0d","media.format":"WAV (Microsoft)","node.rate":"1/48000","node.latency":"4800/48000","media.type":"Audio","media.category":"Playback","media.role":"music","media.filename":"/tmp/oto-eq-fake-i9p5n8o3/sessizlik.wav","target.object":"oto_eq_test_null","stream.is-live":"true","node.want-driver":"true","node.autoconnect":"true","node.loop.class":"main","node.async":"true","media.class":"Stream/Output/Audio","port.group":"stream.0","adapt.follower.spa-node":"","object.register":"false","factory.id":"9","clock.quantum-limit":"8192","node.loop.name":"main-loop","library.name":"audioconvert/libspa-audioconvert","client.id":"123","object.id":"122","object.serial":"3147","node.driver-id":"138","module-stream-restore.id":"sink-input-by-media-role:music"}},{"index":3148,"driver":"PipeWire","owner_module":null,"client":"3144","sink":3139,"sample_specification":"s16le 2ch 48000Hz","channel_map":"front-left,front-right","format":"pcm, format.sample_format = \"\\\"s16le\\\"\"  format.rate = \"48000\"  format.channels = \"2\"  format.channel_map = \"\\\"front-left,front-right\\\"\"","corked":false,"mute":false,"volume":{"front-left":{"value":0,"value_percent":"0%","db":"-inf dB"},"front-right":{"value":0,"value_percent":"0%","db":"-inf dB"}},"balance":0.00,"buffer_latency_usec":0.00,"sink_latency_usec":0.00,"resample_method":"PipeWire","properties":{"client.api":"pipewire-pulse","pulse.server.type":"unix","application.name":"oto-eq-test-cs2","media.name":"Şarkı \"alıntı\" \\ ters\tsekme = x","application.process.binary":"cs2","state.restore-props":"false","state.restore-target":"false","oto_eq.test_id":"7f240b3a086a462481551a76beea496f","media.format":"WAV (Microsoft)","application.process.id":"99437","application.process.user":"user","application.process.host":"desktop","application.language":"C.UTF-8","window.x11.display":":0","application.process.machine_id":"9f282b0d49e04a2b88d500945ce09107","application.process.session_id":"2","pulse.corked":"false","node.rate":"1/48000","node.latency":"8192/48000","target.object":"oto_eq_test_null","stream.is-live":"true","node.name":"oto-eq-test-cs2","node.want-driver":"true","node.autoconnect":"true","media.class":"Stream/Output/Audio","port.group":"stream.0","adapt.follower.spa-node":"","object.register":"false","factory.id":"7","clock.quantum-limit":"8192","node.loop.name":"data-loop.0","library.name":"audioconvert/libspa-audioconvert","client.id":"111","object.id":"121","object.serial":"3148","node.driver-id":"138","pulse.attr.maxlength":"4194304","pulse.attr.tlength":"384000","pulse.attr.prebuf":"380164","pulse.attr.minreq":"3840","module-stream-restore.id":"sink-input-by-application-name:oto-eq-test-cs2"}}]'''

SINK_INPUTS_TEXT = r'''Sink Input #648
	Driver: PipeWire
	Owner Module: n/a
	Client: 647
	Sink: 67
	Sample Specification: s16le 1ch 44100Hz
	Channel Map: mono
	Format: pcm, format.sample_format = "\"s16le\""  format.rate = "44100"  format.channels = "1"  format.channel_map = "\"mono\""
	Corked: no
	Mute: no
	Volume: mono: 65536 / 100% / 0.00 dB
	        balance 0.00
	Buffer Latency: 0 usec
	Sink Latency: 0 usec
	Resample method: PipeWire
	Properties:
		client.api = "pipewire-pulse"
		pulse.server.type = "unix"
		application.name = "speech-dispatcher-dummy"
		application.process.id = "38763"
		application.process.user = "user"
		application.process.host = "desktop"
		application.process.binary = "sd_dummy"
		application.language = "C"
		window.x11.display = ":0"
		application.process.machine_id = "9f282b0d49e04a2b88d500945ce09107"
		application.process.session_id = "2"
		pulse.min.req = "512/48000"
		pulse.min.quantum = "512/48000"
		pulse.idle.timeout = "5"
		media.name = "playback"
		pulse.corked = "false"
		node.rate = "1/44100"
		node.latency = "471/44100"
		stream.is-live = "true"
		node.name = "speech-dispatcher-dummy"
		node.want-driver = "true"
		node.autoconnect = "true"
		media.class = "Stream/Output/Audio"
		port.group = "stream.0"
		adapt.follower.spa-node = ""
		object.register = "false"
		factory.id = "7"
		clock.quantum-limit = "8192"
		node.loop.name = "data-loop.0"
		library.name = "audioconvert/libspa-audioconvert"
		client.id = "81"
		object.id = "94"
		object.serial = "648"
		pulse.attr.minreq = "942"
		pulse.attr.maxlength = "4194304"
		pulse.attr.tlength = "2826"
		pulse.attr.prebuf = "1886"
		module-stream-restore.id = "sink-input-by-application-name:speech-dispatcher-dummy"

Sink Input #2442
	Driver: PipeWire
	Owner Module: n/a
	Client: 2425
	Sink: 67
	Sample Specification: float32le 2ch 48000Hz
	Channel Map: front-left,front-right
	Format: pcm, format.sample_format = "\"float32le\""  format.rate = "48000"  format.channels = "2"  format.channel_map = "\"front-left,front-right\""
	Corked: no
	Mute: no
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
	        balance 0.00
	Buffer Latency: 0 usec
	Sink Latency: 0 usec
	Resample method: PipeWire
	Properties:
		client.api = "pipewire-pulse"
		pulse.server.type = "unix"
		application.name = "Firefox"
		application.process.id = "90832"
		application.process.user = "user"
		application.process.host = "desktop"
		application.process.binary = "firefox"
		application.language = "tr_TR.UTF-8"
		window.x11.display = ":0"
		application.process.machine_id = "9f282b0d49e04a2b88d500945ce09107"
		application.process.session_id = "2"
		module-stream-restore.id = "sink-input-by-application-name:Firefox"
		media.name = "(211) SON 10 YILIN EN İYİSİ BU! (BU OYUNUN GRAFİKLER FENA!) - YouTube"
		pulse.corked = "false"
		node.rate = "1/48000"
		node.latency = "3600/48000"
		stream.is-live = "true"
		node.name = "Firefox"
		node.want-driver = "true"
		node.autoconnect = "true"
		media.class = "Stream/Output/Audio"
		port.group = "stream.0"
		adapt.follower.spa-node = ""
		object.register = "false"
		factory.id = "7"
		clock.quantum-limit = "8192"
		node.loop.name = "data-loop.0"
		library.name = "audioconvert/libspa-audioconvert"
		client.id = "110"
		object.id = "112"
		object.serial = "2442"
		pulse.attr.maxlength = "4194304"
		pulse.attr.tlength = "48000"
		pulse.attr.prebuf = "38408"
		pulse.attr.minreq = "9600"
		node.driver-id = "42"

Sink Input #3147
	Driver: PipeWire
	Owner Module: n/a
	Client: 3146
	Sink: 3139
	Sample Specification: s16le 2ch 48000Hz
	Channel Map: front-left,front-right
	Format: pcm, format.sample_format = "\"s16le\""  format.rate = "48000"  format.channels = "2"  format.channel_map = "\"front-left,front-right\""
	Corked: no
	Mute: no
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
	        balance 0.00
	Buffer Latency: 0 usec
	Sink Latency: 0 usec
	Resample method: PipeWire
	Properties:
		application.name = "oto-eq-test-native"
		node.name = "pw-play"
		application.process.binary = "cs2"
		media.name = "Şarkı \"alıntı\" \ ters	sekme = x"
		state.restore-props = "false"
		state.restore-target = "false"
		oto_eq.test_id = "50fa51eac1664ea88b9250a912a4ae0d"
		media.format = "WAV (Microsoft)"
		node.rate = "1/48000"
		node.latency = "4800/48000"
		media.type = "Audio"
		media.category = "Playback"
		media.role = "music"
		media.filename = "/tmp/oto-eq-fake-i9p5n8o3/sessizlik.wav"
		target.object = "oto_eq_test_null"
		stream.is-live = "true"
		node.want-driver = "true"
		node.autoconnect = "true"
		node.loop.class = "main"
		node.async = "true"
		media.class = "Stream/Output/Audio"
		port.group = "stream.0"
		adapt.follower.spa-node = ""
		object.register = "false"
		factory.id = "9"
		clock.quantum-limit = "8192"
		node.loop.name = "main-loop"
		library.name = "audioconvert/libspa-audioconvert"
		client.id = "123"
		object.id = "122"
		object.serial = "3147"
		node.driver-id = "138"
		module-stream-restore.id = "sink-input-by-media-role:music"

Sink Input #3148
	Driver: PipeWire
	Owner Module: n/a
	Client: 3144
	Sink: 3139
	Sample Specification: s16le 2ch 48000Hz
	Channel Map: front-left,front-right
	Format: pcm, format.sample_format = "\"s16le\""  format.rate = "48000"  format.channels = "2"  format.channel_map = "\"front-left,front-right\""
	Corked: no
	Mute: no
	Volume: front-left: 0 /   0% / -inf dB,   front-right: 0 /   0% / -inf dB
	        balance 0.00
	Buffer Latency: 0 usec
	Sink Latency: 0 usec
	Resample method: PipeWire
	Properties:
		client.api = "pipewire-pulse"
		pulse.server.type = "unix"
		application.name = "oto-eq-test-cs2"
		media.name = "Şarkı \"alıntı\" \ ters	sekme = x"
		application.process.binary = "cs2"
		state.restore-props = "false"
		state.restore-target = "false"
		oto_eq.test_id = "7f240b3a086a462481551a76beea496f"
		media.format = "WAV (Microsoft)"
		application.process.id = "99437"
		application.process.user = "user"
		application.process.host = "desktop"
		application.language = "C.UTF-8"
		window.x11.display = ":0"
		application.process.machine_id = "9f282b0d49e04a2b88d500945ce09107"
		application.process.session_id = "2"
		pulse.corked = "false"
		node.rate = "1/48000"
		node.latency = "8192/48000"
		target.object = "oto_eq_test_null"
		stream.is-live = "true"
		node.name = "oto-eq-test-cs2"
		node.want-driver = "true"
		node.autoconnect = "true"
		media.class = "Stream/Output/Audio"
		port.group = "stream.0"
		adapt.follower.spa-node = ""
		object.register = "false"
		factory.id = "7"
		clock.quantum-limit = "8192"
		node.loop.name = "data-loop.0"
		library.name = "audioconvert/libspa-audioconvert"
		client.id = "111"
		object.id = "121"
		object.serial = "3148"
		node.driver-id = "138"
		pulse.attr.maxlength = "4194304"
		pulse.attr.tlength = "384000"
		pulse.attr.prebuf = "380164"
		pulse.attr.minreq = "3840"
		module-stream-restore.id = "sink-input-by-application-name:oto-eq-test-cs2"'''

SINKS_JSON = r'''[{"index": 67, "state": "RUNNING", "name": "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo", "description": "CORSAIR HS80 RGB Wireless Gaming Receiver Analog Stereo", "driver": "PipeWire", "sample_specification": "s24le 2ch 48000Hz", "channel_map": "front-left,front-right", "owner_module": 4294967295, "mute": false, "volume": {"front-left": {"value": 32768, "value_percent": "50%", "db": "-18.06 dB"}, "front-right": {"value": 32768, "value_percent": "50%", "db": "-18.06 dB"}}, "balance": 0.0, "base_volume": {"value": 65536, "value_percent": "100%", "db": "0.00 dB"}, "monitor_source": "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo.monitor", "latency": {"actual": 0.0, "configured": 0.0}, "flags": ["HARDWARE", "HW_MUTE_CTRL", "HW_VOLUME_CTRL", "DECIBEL_VOLUME", "LATENCY"], "properties": {"object.path": "alsa:acp:Gamin:3:playback", "api.alsa.path": "front:0", "api.alsa.pcm.card": "0", "api.alsa.pcm.stream": "playback", "port.group": "playback", "device.icon_name": "audio-card-analog", "device.bus": "usb", "audio.channels": "2", "audio.position": "[ FL, FR ]", "device.routes": "1", "alsa.resolution_bits": "16", "alsa.card": "0", "alsa.card_name": "CORSAIR HS80 RGB Wireless Gamin", "alsa.long_card_name": "Corsair CORSAIR HS80 RGB Wireless Gamin at usb-0000:13:00.0-3, full speed", "alsa.driver_name": "snd_usb_audio", "alsa.mixer_name": "USB Mixer", "alsa.components": "USB1b1c:0a6b", "alsa.id": "Gamin", "device.api": "alsa", "device.class": "sound", "alsa.class": "generic", "alsa.subclass": "generic-mix", "alsa.name": "USB Audio", "alsa.subdevice": "0", "alsa.subdevice_name": "subdevice #0", "alsa.device": "0", "alsa.sync.id": "00000000:00000000:00000000:00000000", "device.profile.name": "analog-stereo", "device.profile.description": "Analog Stereo", "card.profile.device": "3", "device.id": "53", "factory.name": "api.alsa.pcm.sink", "node.pause-on-idle": "false", "priority.driver": "1109", "priority.session": "1109", "media.class": "Audio/Sink", "node.name": "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo", "node.nick": "CORSAIR HS80 RGB Wireless Gamin", "device.description": "CORSAIR HS80 RGB Wireless Gaming Receiver", "api.alsa.card.name": "CORSAIR HS80 RGB Wireless Gamin", "api.alsa.card.longname": "Corsair CORSAIR HS80 RGB Wireless Gamin at usb-0000:13:00.0-3, full speed", "factory.id": "19", "clock.quantum-limit": "8192", "client.id": "32", "node.driver": "true", "node.loop.name": "data-loop.0", "library.name": "audioconvert/libspa-audioconvert", "object.id": "42", "object.serial": "67", "node.max-latency": "16384/48000", "api.alsa.period-size": "512", "api.alsa.period-num": "64", "api.alsa.headroom": "512", "device.enum.api": "udev", "api.alsa.card": "0", "device.plugged.usec": "5197857", "device.bus_path": "pci-0000:13:00.0-usb-0:3:1.0", "sysfs.path": "/devices/pci0000:00/0000:00:02.1/0000:05:00.0/0000:06:0c.0/0000:13:00.0/usb1/1-3/1-3:1.0/sound/card0", "device.bus-id": "usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00", "device.subsystem": "sound", "device.vendor.id": "0x1b1c", "device.vendor.name": "Corsair", "device.product.id": "0x0a6b", "device.product.name": "CORSAIR HS80 RGB Wireless Gaming Receiver", "device.serial": "Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001", "device.name": "alsa_card.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00", "device.nick": "CORSAIR HS80 RGB Wireless Gamin", "api.alsa.use-acp": "true", "api.acp.auto-profile": "false", "api.acp.auto-port": "false", "api.dbus.ReserveDevice1.Priority": "-20", "api.alsa.split-enable": "true", "api.dbus.ReserveDevice1": "Audio0", "spa.object.id": "2", "device.string": "0"}, "ports": [{"name": "analog-output-headphones", "description": "Kulaklık", "type": "Headphones", "priority": 9900, "availability_group": "", "availability": "availability unknown"}], "active_port": "analog-output-headphones", "formats": ["pcm"]}, {"index": 535, "state": "SUSPENDED", "name": "alsa_output.pci-0000_03_00.1.hdmi-stereo", "description": "Navi 31 HDMI/DP Audio Sayısal Stereo (HDMI) [DELL AW2723DF]", "driver": "PipeWire", "sample_specification": "s32le 2ch 48000Hz", "channel_map": "front-left,front-right", "owner_module": 4294967295, "mute": false, "volume": {"front-left": {"value": 62259, "value_percent": "95%", "db": "-1.34 dB"}, "front-right": {"value": 62259, "value_percent": "95%", "db": "-1.34 dB"}}, "balance": 0.0, "base_volume": {"value": 65536, "value_percent": "100%", "db": "0.00 dB"}, "monitor_source": "alsa_output.pci-0000_03_00.1.hdmi-stereo.monitor", "latency": {"actual": 0.0, "configured": 0.0}, "flags": ["HARDWARE", "DECIBEL_VOLUME", "LATENCY", "SET_FORMATS"], "properties": {"object.path": "alsa:acp:HDMI:4:playback", "api.alsa.path": "hdmi:1", "api.alsa.pcm.card": "1", "api.alsa.pcm.stream": "playback", "port.group": "playback", "device.icon_name": "audio-card-analog", "device.bus": "pci", "audio.channels": "2", "audio.position": "[ FL, FR ]", "iec958.codecs": "[\"PCM\"]", "device.routes": "1", "alsa.resolution_bits": "16", "alsa.card": "1", "alsa.card_name": "HDA ATI HDMI", "alsa.long_card_name": "HDA ATI HDMI at 0xdf920000 irq 109", "alsa.driver_name": "snd_hda_intel", "alsa.mixer_name": "ATI R6xx HDMI", "alsa.components": "HDA:1002aa01,00aa0100,00100800", "alsa.id": "HDMI", "device.api": "alsa", "device.class": "sound", "alsa.class": "generic", "alsa.subclass": "generic-mix", "alsa.name": "DELL AW2723DF", "alsa.subdevice": "0", "alsa.subdevice_name": "subdevice #0", "alsa.device": "3", "alsa.sync.id": "00000000:00000000:00000000:00000000", "device.profile.name": "hdmi-stereo", "device.profile.description": "Sayısal Stereo (HDMI)", "card.profile.device": "4", "device.id": "52", "factory.name": "api.alsa.pcm.sink", "node.pause-on-idle": "false", "priority.driver": "696", "priority.session": "696", "media.class": "Audio/Sink", "node.name": "alsa_output.pci-0000_03_00.1.hdmi-stereo", "node.nick": "DELL AW2723DF", "device.description": "Navi 31 HDMI/DP Audio", "api.alsa.card.name": "HDA ATI HDMI", "api.alsa.card.longname": "HDA ATI HDMI at 0xdf920000 irq 109", "factory.id": "19", "clock.quantum-limit": "8192", "client.id": "32", "node.driver": "true", "node.loop.name": "data-loop.0", "library.name": "audioconvert/libspa-audioconvert", "object.id": "66", "object.serial": "535", "device.enum.api": "udev", "api.alsa.card": "1", "device.plugged.usec": "4673756", "device.bus_path": "pci-0000:03:00.1", "sysfs.path": "/devices/pci0000:00/0000:00:01.1/0000:01:00.0/0000:02:00.0/0000:03:00.1/sound/card1", "device.subsystem": "sound", "device.vendor.id": "0x1002", "device.vendor.name": "Advanced Micro Devices, Inc. [AMD/ATI]", "device.product.id": "0xab30", "device.product.name": "Navi 31 HDMI/DP Audio", "device.name": "alsa_card.pci-0000_03_00.1", "device.nick": "HDA ATI HDMI", "api.alsa.use-acp": "true", "api.acp.auto-profile": "false", "api.acp.auto-port": "false", "api.dbus.ReserveDevice1.Priority": "-20", "api.alsa.split-enable": "true", "api.dbus.ReserveDevice1": "Audio1", "spa.object.id": "4", "device.string": "1"}, "ports": [{"name": "hdmi-output-0", "description": "HDMI / DisplayPort", "type": "HDMI", "priority": 5900, "availability_group": "Legacy 1", "availability": "available"}], "active_port": "hdmi-output-0", "formats": ["pcm"]}]'''

SINKS_TEXT = r'''Sink #67
	State: RUNNING
	Name: alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo
	Description: CORSAIR HS80 RGB Wireless Gaming Receiver Analog Stereo
	Driver: PipeWire
	Sample Specification: s24le 2ch 48000Hz
	Channel Map: front-left,front-right
	Owner Module: 4294967295
	Mute: no
	Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB
	        balance 0.00
	Base Volume: 65536 / 100% / 0.00 dB
	Monitor Source: alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo.monitor
	Latency: 0 usec, configured 0 usec
	Flags: HARDWARE HW_MUTE_CTRL HW_VOLUME_CTRL DECIBEL_VOLUME LATENCY 
	Properties:
		object.path = "alsa:acp:Gamin:3:playback"
		api.alsa.path = "front:0"
		api.alsa.pcm.card = "0"
		api.alsa.pcm.stream = "playback"
		port.group = "playback"
		device.icon_name = "audio-card-analog"
		device.bus = "usb"
		audio.channels = "2"
		audio.position = "[ FL, FR ]"
		device.routes = "1"
		alsa.resolution_bits = "16"
		alsa.card = "0"
		alsa.card_name = "CORSAIR HS80 RGB Wireless Gamin"
		alsa.long_card_name = "Corsair CORSAIR HS80 RGB Wireless Gamin at usb-0000:13:00.0-3, full speed"
		alsa.driver_name = "snd_usb_audio"
		alsa.mixer_name = "USB Mixer"
		alsa.components = "USB1b1c:0a6b"
		alsa.id = "Gamin"
		device.api = "alsa"
		device.class = "sound"
		alsa.class = "generic"
		alsa.subclass = "generic-mix"
		alsa.name = "USB Audio"
		alsa.subdevice = "0"
		alsa.subdevice_name = "subdevice #0"
		alsa.device = "0"
		alsa.sync.id = "00000000:00000000:00000000:00000000"
		device.profile.name = "analog-stereo"
		device.profile.description = "Analog Stereo"
		card.profile.device = "3"
		device.id = "53"
		factory.name = "api.alsa.pcm.sink"
		node.pause-on-idle = "false"
		priority.driver = "1109"
		priority.session = "1109"
		media.class = "Audio/Sink"
		node.name = "alsa_output.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00.analog-stereo"
		node.nick = "CORSAIR HS80 RGB Wireless Gamin"
		device.description = "CORSAIR HS80 RGB Wireless Gaming Receiver"
		api.alsa.card.name = "CORSAIR HS80 RGB Wireless Gamin"
		api.alsa.card.longname = "Corsair CORSAIR HS80 RGB Wireless Gamin at usb-0000:13:00.0-3, full speed"
		factory.id = "19"
		clock.quantum-limit = "8192"
		client.id = "32"
		node.driver = "true"
		node.loop.name = "data-loop.0"
		library.name = "audioconvert/libspa-audioconvert"
		object.id = "42"
		object.serial = "67"
		node.max-latency = "16384/48000"
		api.alsa.period-size = "512"
		api.alsa.period-num = "64"
		api.alsa.headroom = "512"
		device.enum.api = "udev"
		api.alsa.card = "0"
		device.plugged.usec = "5197857"
		device.bus_path = "pci-0000:13:00.0-usb-0:3:1.0"
		sysfs.path = "/devices/pci0000:00/0000:00:02.1/0000:05:00.0/0000:06:0c.0/0000:13:00.0/usb1/1-3/1-3:1.0/sound/card0"
		device.bus-id = "usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00"
		device.subsystem = "sound"
		device.vendor.id = "0x1b1c"
		device.vendor.name = "Corsair"
		device.product.id = "0x0a6b"
		device.product.name = "CORSAIR HS80 RGB Wireless Gaming Receiver"
		device.serial = "Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001"
		device.name = "alsa_card.usb-Corsair_CORSAIR_HS80_RGB_Wireless_Gaming_Receiver_1a2b3c4d00000001-00"
		device.nick = "CORSAIR HS80 RGB Wireless Gamin"
		api.alsa.use-acp = "true"
		api.acp.auto-profile = "false"
		api.acp.auto-port = "false"
		api.dbus.ReserveDevice1.Priority = "-20"
		api.alsa.split-enable = "true"
		api.dbus.ReserveDevice1 = "Audio0"
		spa.object.id = "2"
		device.string = "0"
	Ports:
		analog-output-headphones: Kulaklık (type: Headphones, priority: 9900, availability unknown)
	Active Port: analog-output-headphones
	Formats:
		pcm

Sink #535
	State: SUSPENDED
	Name: alsa_output.pci-0000_03_00.1.hdmi-stereo
	Description: Navi 31 HDMI/DP Audio Sayısal Stereo (HDMI) [DELL AW2723DF]
	Driver: PipeWire
	Sample Specification: s32le 2ch 48000Hz
	Channel Map: front-left,front-right
	Owner Module: 4294967295
	Mute: no
	Volume: front-left: 62259 /  95% / -1.34 dB,   front-right: 62259 /  95% / -1.34 dB
	        balance 0.00
	Base Volume: 65536 / 100% / 0.00 dB
	Monitor Source: alsa_output.pci-0000_03_00.1.hdmi-stereo.monitor
	Latency: 0 usec, configured 0 usec
	Flags: HARDWARE DECIBEL_VOLUME LATENCY SET_FORMATS 
	Properties:
		object.path = "alsa:acp:HDMI:4:playback"
		api.alsa.path = "hdmi:1"
		api.alsa.pcm.card = "1"
		api.alsa.pcm.stream = "playback"
		port.group = "playback"
		device.icon_name = "audio-card-analog"
		device.bus = "pci"
		audio.channels = "2"
		audio.position = "[ FL, FR ]"
		iec958.codecs = "[\"PCM\"]"
		device.routes = "1"
		alsa.resolution_bits = "16"
		alsa.card = "1"
		alsa.card_name = "HDA ATI HDMI"
		alsa.long_card_name = "HDA ATI HDMI at 0xdf920000 irq 109"
		alsa.driver_name = "snd_hda_intel"
		alsa.mixer_name = "ATI R6xx HDMI"
		alsa.components = "HDA:1002aa01,00aa0100,00100800"
		alsa.id = "HDMI"
		device.api = "alsa"
		device.class = "sound"
		alsa.class = "generic"
		alsa.subclass = "generic-mix"
		alsa.name = "DELL AW2723DF"
		alsa.subdevice = "0"
		alsa.subdevice_name = "subdevice #0"
		alsa.device = "3"
		alsa.sync.id = "00000000:00000000:00000000:00000000"
		device.profile.name = "hdmi-stereo"
		device.profile.description = "Sayısal Stereo (HDMI)"
		card.profile.device = "4"
		device.id = "52"
		factory.name = "api.alsa.pcm.sink"
		node.pause-on-idle = "false"
		priority.driver = "696"
		priority.session = "696"
		media.class = "Audio/Sink"
		node.name = "alsa_output.pci-0000_03_00.1.hdmi-stereo"
		node.nick = "DELL AW2723DF"
		device.description = "Navi 31 HDMI/DP Audio"
		api.alsa.card.name = "HDA ATI HDMI"
		api.alsa.card.longname = "HDA ATI HDMI at 0xdf920000 irq 109"
		factory.id = "19"
		clock.quantum-limit = "8192"
		client.id = "32"
		node.driver = "true"
		node.loop.name = "data-loop.0"
		library.name = "audioconvert/libspa-audioconvert"
		object.id = "66"
		object.serial = "535"
		device.enum.api = "udev"
		api.alsa.card = "1"
		device.plugged.usec = "4673756"
		device.bus_path = "pci-0000:03:00.1"
		sysfs.path = "/devices/pci0000:00/0000:00:01.1/0000:01:00.0/0000:02:00.0/0000:03:00.1/sound/card1"
		device.subsystem = "sound"
		device.vendor.id = "0x1002"
		device.vendor.name = "Advanced Micro Devices, Inc. [AMD/ATI]"
		device.product.id = "0xab30"
		device.product.name = "Navi 31 HDMI/DP Audio"
		device.name = "alsa_card.pci-0000_03_00.1"
		device.nick = "HDA ATI HDMI"
		api.alsa.use-acp = "true"
		api.acp.auto-profile = "false"
		api.acp.auto-port = "false"
		api.dbus.ReserveDevice1.Priority = "-20"
		api.alsa.split-enable = "true"
		api.dbus.ReserveDevice1 = "Audio1"
		spa.object.id = "4"
		device.string = "1"
	Ports:
		hdmi-output-0: HDMI / DisplayPort (type: HDMI, priority: 5900, availability group: Legacy 1, available)
	Active Port: hdmi-output-0
	Formats:
		pcm'''
