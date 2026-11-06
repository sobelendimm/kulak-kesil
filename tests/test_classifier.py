"""Classifier tests with synthetic streams, fake /proc data and fake MPRIS players."""
from __future__ import annotations

import logging
import time
import tomllib
import unittest

from oto_eq.classifier import Classifier, merge_rules, shipped_rules, step_label
from oto_eq.config import DEFAULTS_DIR, RULES_FILE
from oto_eq.models import Category, MediaPlayer, ProcInfo, Stream

INIT = (1, "systemd", "/usr/lib/systemd/systemd --user")
STEAM = (6655, "steam", "/home/u/.local/share/Steam/ubuntu12_32/steam -srt-logger-opened")
REAPER730 = (7001, "reaper", "/home/u/.local/share/Steam/ubuntu12_32/reaper SteamLaunch AppId=730 -- "
             "/home/u/.local/share/Steam/steamapps/common/SteamLinuxRuntime_sniper/_v2-entry-point")
PV = (7005, "pv-adverb", "/usr/lib/pressure-vessel/from-host/libexec/steam-runtime-tools-0/pv-adverb")
FIREFOX_PID = 90832

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)   # tests feed broken input on purpose


def stream(index=1, app="", binary="", pid=None, media_name="", role="", props=None):
    return Stream(index=index, sink_index=0, app_name=app, binary=binary, pid=pid,
                  media_name=media_name, media_role=role, node_name=app, corked=False,
                  props=props or {})


def proc(pid, comm, exe="", cmdline=(), env=None, ancestors=()):
    return ProcInfo(pid=pid, comm=comm, exe=exe or comm, cmdline=tuple(cmdline) or (comm,),
                    env=env or {}, ancestors=tuple(ancestors) + (INIT,))


def player(bus, pid, title="", artist="", url="", status="Playing", identity=""):
    return MediaPlayer(bus_name=bus, pid=pid, identity=identity, status=status,
                       title=title, artist=artist, url=url)


class Env:
    """Fake process table + namespace map + open files."""

    def __init__(self):
        self.procs: dict[int, ProcInfo] = {}
        self.ns: dict[int, int] = {}          # namespaced stream pid -> host pid
        self.files: dict[int, tuple[str, ...]] = {}
        self.flatpak: dict[int, str] = {}

    def add(self, p: ProcInfo, ns_pid: int | None = None, files=(), flatpak=""):
        self.procs[p.pid] = p
        if ns_pid is not None:
            self.ns[ns_pid] = p.pid
        if files:
            self.files[p.pid] = tuple(files)
        if flatpak:
            self.flatpak[p.pid] = flatpak
        return p

    def resolve(self, s: Stream):
        if s.pid in self.ns:
            return self.ns[s.pid]
        return s.pid if s.pid in self.procs else None

    def classifier(self, rules=None) -> Classifier:
        if rules is None:
            rules = load_shipped()
        return Classifier(rules, proc_lookup=self.procs.get, resolve=self.resolve,
                          files_lookup=lambda pid: self.files.get(pid, ()),
                          flatpak_lookup=lambda pid: self.flatpak.get(pid, ""))


def load_shipped() -> dict:
    with open(DEFAULTS_DIR / RULES_FILE, "rb") as f:
        return tomllib.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.c = self.env.classifier()

    def check(self, s, players, cat, profile=..., reason_has=None, c=None):
        d = (c or self.c).classify(s, players)
        self.assertEqual(d.category, cat, d)
        if profile is not ...:
            self.assertEqual(d.profile, profile, d)
        if reason_has:
            self.assertIn(reason_has, d.reason, d)
        return d


class TestShippedRules(unittest.TestCase):
    def test_parses_and_has_sections(self):
        r = load_shipped()
        for sec in ("genel", "eslesme", "cs2", "roller", "uygulamalar", "oyun",
                    "oynatici", "tarayici"):
            self.assertIn(sec, r)
        self.assertEqual(r["genel"]["kulaklik_desen"], "(?i)hs80")
        self.assertIn("bilinmeyen_profil", r["genel"])
        self.assertEqual(r["eslesme"]["sesli"], "")
        self.assertEqual(r["eslesme"]["sistem"], "")

    def test_merge_fills_missing_keys(self):
        m = merge_rules({"genel": {"bilinmeyen_profil": ""}})
        self.assertEqual(m["genel"]["bilinmeyen_profil"], "")
        self.assertIn("muzik", m["uygulamalar"])
        self.assertEqual(m["kural"], [])
        self.assertTrue(shipped_rules())

    def test_empty_rules_dict_works(self):
        c = Classifier({}, proc_lookup=lambda p: None, resolve=lambda s: None)
        d = c.classify(stream(app="spotify", binary="spotify", pid=5), [])
        self.assertEqual(d.category, Category.MUSIC)


class TestCS2(Base):
    def test_binary_cs2(self):
        self.env.add(proc(8000, "cs2", env={"SteamAppId": "730"}, ancestors=[REAPER730, STEAM]))
        self.check(stream(app="cs2", binary="cs2", pid=8000), [], Category.CS2, "cs2", "ikili 'cs2'")

    def test_steam_appid_env(self):
        self.env.add(proc(8001, "GameThread", exe="cs2-helper", env={"SteamGameId": "730"}))
        self.check(stream(app="Counter-Strike 2", binary="GameThread", pid=8001), [],
                   Category.CS2, "cs2", "SteamGameId=730")

    def test_namespaced_pid_via_ancestor_appid(self):
        # stream reports a container-local pid; resolver maps it to the host pid
        self.env.add(proc(8002, "SDLAudioP2", exe="ld-linux-x86-64.so.2",
                          ancestors=[PV, REAPER730, STEAM]), ns_pid=12)
        self.check(stream(app="SDL Application", binary="SDLAudioP2", pid=12), [],
                   Category.CS2, "cs2", "AppId=730")

    def test_other_appid_not_cs2(self):
        self.env.add(proc(8003, "game", env={"SteamAppId": "1091500"}))
        self.check(stream(app="game", binary="game", pid=8003), [], Category.GAME, "oyun")


class TestGames(Base):
    def test_proton_exe(self):
        self.env.add(proc(9000, "eldenring.exe", exe="wine64-preloader",
                          env={"SteamAppId": "1245620", "STEAM_COMPAT_DATA_PATH": "/x/compatdata/1245620"}))
        self.check(stream(app="eldenring.exe", binary="eldenring.exe", pid=9000), [],
                   Category.GAME, "oyun", ".exe")

    def test_proton_env_only(self):
        self.env.add(proc(9001, "UnityMain", env={"SteamAppId": "1245620"}))
        self.check(stream(app="UnityMain", binary="UnityMain", pid=9001), [],
                   Category.GAME, "oyun", "SteamAppId=1245620")

    def test_faugus_umu_env(self):
        self.env.add(proc(9002, "GameThread", exe="wine64-preloader",
                          env={"GAMEID": "umu-default", "SteamAppId": "0",
                               "WINEPREFIX": "/home/u/Faugus/game"},
                          ancestors=[(8990, "umu-run", "/usr/bin/python3 /usr/bin/umu-run game.exe"),
                                     (8980, "python3", "/usr/bin/python3 -m faugus.runner ...")]))
        self.check(stream(app="GameThread", binary="GameThread", pid=9002), [], Category.GAME, "oyun")

    def test_faugus_ancestor_only(self):
        self.env.add(proc(9003, "AudioThread", exe="AudioThread",
                          ancestors=[(8981, "python3", "/usr/bin/python3 -m faugus.runner GAMEID=x")]))
        self.check(stream(app="AudioThread", binary="AudioThread", pid=9003), [],
                   Category.GAME, "oyun", "ata süreci")

    def test_lutris_wine(self):
        self.env.add(proc(9004, "Game-Win64-Shi", exe="wine64-preloader",
                          env={"WINEPREFIX": "/home/u/Games/kcd2", "LUTRIS_GAME_UUID": "abc"},
                          ancestors=[(8970, "lutris-wrapper", "lutris-wrapper: KCD2")]))
        self.check(stream(app="Game-Win64-Shipping", binary="wine64-preloader", pid=9004), [],
                   Category.GAME, "oyun", "wine")

    def test_lutris_wrapper_ancestor(self):
        self.env.add(proc(9005, "native_game", ancestors=[(8971, "lutris-wrapper", "lutris-wrapper: X")]))
        self.check(stream(app="native", binary="native_game", pid=9005), [], Category.GAME)

    def test_steamwebhelper_not_game(self):
        self.env.add(proc(6926, "steamwebhelper",
                          env={"container": "pressure-vessel"},
                          ancestors=[(6829, "pv-adverb", "pv-adverb --generate-locales"),
                                     (6709, "srt-bwrap", "srt-bwrap --args 26"), STEAM]))
        d = self.check(stream(app="Steam", binary="steamwebhelper", pid=6926), [], Category.UNKNOWN)
        self.assertIn("oyun değil", d.reason)

    def test_steam_client_not_game(self):
        self.env.add(proc(6655, "steam"))
        self.check(stream(app="Steam", binary="steam", pid=6655), [], Category.UNKNOWN)


class TestApps(Base):
    def test_spotify_flatpak_namespaced(self):
        self.env.add(proc(50100, "spotify", env={"FLATPAK_ID": "com.spotify.Client",
                                                  "container": "flatpak"},
                          ancestors=[(50090, "bwrap", "bwrap --args 40 spotify")]), ns_pid=2)
        self.check(stream(app="spotify", binary="spotify", pid=2), [], Category.MUSIC, "muzik",
                   "flatpak com.spotify.Client")

    def test_spotify_unresolved(self):
        self.check(stream(app="Spotify", binary="spotify", pid=2), [], Category.MUSIC, "muzik")

    def test_discord_flatpak_voice(self):
        self.env.add(proc(50200, "Discord", env={"FLATPAK_ID": "com.discordapp.Discord"}), ns_pid=3)
        self.check(stream(app="Discord", binary="Discord", pid=3), [], Category.VOICE, None)

    def test_discord_webrtc_voice(self):
        self.check(stream(app="WEBRTC VoiceEngine", binary="Discord", pid=None), [],
                   Category.VOICE, None)

    def test_speech_dispatcher_system(self):
        self.env.add(proc(38763, "sd_dummy"))
        self.check(stream(app="speech-dispatcher-dummy", binary="sd_dummy", pid=38763), [],
                   Category.SYSTEM, None)

    def test_event_role_system(self):
        self.env.add(proc(1489, "kded6"))
        self.check(stream(app="libcanberra", binary="kded6", pid=1489, role="event"), [],
                   Category.SYSTEM, None)

    def test_event_role_unknown_app(self):
        self.check(stream(app="someapp", binary="someapp", role="event"), [], Category.SYSTEM, None)

    def test_phone_role_voice(self):
        self.check(stream(app="callapp", binary="callapp", role="phone"), [], Category.VOICE, None)

    def test_unknown_app(self):
        d = self.check(stream(app="mystery", binary="mystery", pid=None), [], Category.UNKNOWN)
        self.assertEqual(d.profile, load_shipped()["genel"]["bilinmeyen_profil"] or None)

    def test_unknown_profile_empty(self):
        rules = load_shipped()
        rules["genel"]["bilinmeyen_profil"] = ""
        self.check(stream(app="mystery", binary="mystery"), [], Category.UNKNOWN, None,
                   c=self.env.classifier(rules))


class TestPlayers(Base):
    def test_vlc_flatpak_video_file(self):
        self.env.add(proc(51000, "vlc", env={"FLATPAK_ID": "org.videolan.VLC"}),
                     ns_pid=2, files=["/usr/lib/x.so", "/home/u/Filmler/Dune.2021.mkv"])
        self.check(stream(app="VLC media player (LibVLC 3.0.21)", binary="vlc", pid=2, role="video"),
                   [], Category.MOVIE, "film", ".mkv")

    def test_vlc_audio_via_mpris_url(self):
        self.env.add(proc(51001, "vlc", env={"FLATPAK_ID": "org.videolan.VLC"}), ns_pid=2)
        # MPRIS pid is xdg-dbus-proxy (unrelated): linked by bus name
        p = player("org.mpris.MediaPlayer2.vlc", 51500, title="Song", url="file:///home/u/M%C3%BCzik/song.flac",
                   identity="VLC media player")
        self.check(stream(app="VLC media player (LibVLC 3.0.21)", binary="vlc", pid=2, role="video"),
                   [p], Category.MUSIC, "muzik", ".flac")

    def test_vlc_no_evidence_defaults_film(self):
        self.check(stream(app="VLC media player", binary="vlc", role="video"), [], Category.MOVIE, "film")

    def test_mpv_mp3_open_file(self):
        self.env.add(proc(52000, "mpv", cmdline=("mpv", "--shuffle", "/home/u/Müzik"),
                          ), files=["/home/u/Müzik/a.mp3"])
        self.check(stream(app="mpv", binary="mpv", pid=52000, media_name="Artist - Song"),
                   [], Category.MUSIC, "muzik", ".mp3")

    def test_mpv_mkv_cmdline(self):
        self.env.add(proc(52001, "mpv", cmdline=("mpv", "/home/u/Film.mkv")))
        self.check(stream(app="mpv", binary="mpv", pid=52001, media_name="Film.mkv"),
                   [], Category.MOVIE, "film", ".mkv")

    def test_mpv_role_music(self):
        self.env.add(proc(52002, "mpv", cmdline=("mpv", "https://example.org/stream")))
        self.check(stream(app="mpv", binary="mpv", pid=52002, media_name="stream", role="Music"),
                   [], Category.MUSIC, "muzik", "Music")

    def test_mpv_media_name_ext(self):
        self.check(stream(app="mpv", binary="mpv", media_name="track01.flac"), [], Category.MUSIC)


class TestBrowser(Base):
    def setUp(self):
        super().setUp()
        self.env.add(proc(FIREFOX_PID, "firefox", exe="firefox", cmdline=("/usr/lib/firefox/firefox",)))

    def ff(self, media_name="", index=1):
        return stream(index=index, app="Firefox", binary="firefox", pid=FIREFOX_PID,
                      media_name=media_name)

    def test_youtube_music_url(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Bohemian Rhapsody", artist="Queen", url="https://music.youtube.com/watch?v=x")
        self.check(self.ff("Bohemian Rhapsody - YouTube Music"), [p], Category.MUSIC, "muzik",
                   "music.youtube.com")

    def test_youtube_official_video(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Queen – Bohemian Rhapsody (Official Video Remastered)", artist="Queen Official",
                   url="https://www.youtube.com/watch?v=fJ9rUzIMcZQ")
        self.check(self.ff("(3) Queen – Bohemian Rhapsody (Official Video Remastered) - YouTube"), [p],
                   Category.MUSIC, "muzik", "Official Video")

    def test_youtube_topic_artist(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Gel Gör Beni Aşk Neyledi", artist="Barış Manço - Topic",
                   url="https://www.youtube.com/watch?v=abc")
        self.check(self.ff("Gel Gör Beni Aşk Neyledi - YouTube"), [p], Category.MUSIC)

    def test_youtube_regular_video_is_film(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="SON 10 YILIN EN İYİSİ BU! (BU OYUNUN GRAFİKLER FENA!)", artist="Enis Kirazoglu",
                   url="https://www.youtube.com/watch?v=sleuwTieVeE", status="Paused")
        self.check(self.ff("(211) SON 10 YILIN EN İYİSİ BU! (BU OYUNUN GRAFİKLER FENA!) - YouTube"), [p],
                   Category.MOVIE, "film", "YouTube video")

    def test_netflix(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Netflix", url="https://www.netflix.com/watch/81234567")
        self.check(self.ff("Netflix"), [p], Category.MOVIE, "film", "netflix.com")

    def test_no_mpris_default(self):
        d = self.check(self.ff("Bir haber sitesi"), [], Category.MOVIE, "film", "varsayılan")
        self.assertIn("MPRIS yok", d.reason)

    def test_no_mpris_tab_title_youtube_music(self):
        self.check(self.ff("Song - YouTube Music"), [], Category.MUSIC, "muzik")

    def test_no_mpris_tab_title_youtube_lyrics(self):
        self.check(self.ff("Artist - Song (Lyrics) - YouTube"), [], Category.MUSIC, "muzik")

    def test_plasma_browser_integration_preferred(self):
        native = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID, title="Song X")
        self.env.add(proc(91000, "plasma-browser-", exe="plasma-browser-integration-host",
                          ancestors=[(FIREFOX_PID, "firefox", "/usr/lib/firefox/firefox")]))
        pbi = player("org.mpris.MediaPlayer2.plasma-browser-integration", 91000, title="Song X",
                     url="https://open.spotify.com/track/1")
        self.check(self.ff("Song X • Artist"), [native, pbi], Category.MUSIC, "muzik", "open.spotify.com")

    def test_two_tabs(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Lofi beats", url="https://music.youtube.com/watch?v=1")
        self.check(self.ff("Lofi beats - YouTube Music", index=1), [p], Category.MUSIC)
        # second tab's stream title does not match the MPRIS player -> judged by its own title
        self.check(self.ff("Haber bülteni - YouTube", index=2), [p], Category.MOVIE, "film", "YouTube video")

    def test_generic_stream_name_uses_recent_playing(self):
        a = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="a", url="https://www.twitch.tv/x")
        self.check(self.ff("Playback"), [a], Category.MOVIE, "film", "twitch.tv")

    def test_site_only_tab_name_binds_playing_player(self):
        # 25 Eki: YouTube Music titles the tab just "YouTube Music" while the next track
        # loads; the stream keeps that media.name → it must still bind the Playing player
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Seni Yazdım", url="https://music.youtube.com/watch?v=wtOHNhG0EZc")
        st = self.ff("YouTube Music")
        self.assertIs(self.env.classifier().player_for(st, [p]), p)

    def test_browser_calls_are_voice(self):
        # CHANGE 6: Meet/Zoom/Teams/Discord in a browser → Düz (no EQ), so ducking can see them
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Takım toplantısı", url="https://meet.google.com/abc-defg-hij")
        self.check(self.ff("Takım toplantısı"), [p], Category.VOICE)
        self.check(self.ff("Meet – abc-defg-hij"), [], Category.VOICE)
        self.check(self.ff("Genel | Sunucum | Discord"), [], Category.VOICE)
        # G2 (25 Eki): other Discord web title shapes (were Film = EQ + compressor)
        self.check(self.ff("#genel | Sunucum - Discord"), [], Category.VOICE)
        self.check(self.ff("Discord | #genel | Sunucum"), [], Category.VOICE)
        self.check(self.ff("(2) Discord | Arkadaşlar"), [], Category.VOICE)
        self.check(self.ff("Discord botu nasıl yapılır - YouTube"), [], Category.MOVIE)

    def test_radio_and_dizi_sites(self):
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Canlı yayın", url="https://www.radyofenomen.com/canli")
        self.check(self.ff("Canlı yayın"), [p], Category.MUSIC)
        p2 = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                    title="Bölüm 5", url="https://dizibox.tv/x-5-bolum")
        self.check(self.ff("Bölüm 5"), [p2], Category.MOVIE)


class TestChromiumAudioService(Base):
    """2 Kas 2026: Vivaldi plays from an audio-service child; the URL-carrying
    plasma-browser-integration player is its SIBLING under the main process."""

    VMAIN = (4427, "vivaldi-bin", "/opt/vivaldi/vivaldi-bin")

    def setUp(self):
        super().setUp()
        self.env.add(proc(4427, "vivaldi-bin", cmdline=("/opt/vivaldi/vivaldi-bin",)))
        self.env.add(proc(4966, "vivaldi-bin", cmdline=(
            "/opt/vivaldi/vivaldi-bin", "--type=utility",
            "--utility-sub-type=audio.mojom.AudioService"), ancestors=[self.VMAIN]))
        self.env.add(proc(4623, "plasma-browser-", cmdline=(
            "/usr/bin/plasma-browser-integration-host",), ancestors=[self.VMAIN]))
        self.pbi = player("org.mpris.MediaPlayer2.plasma-browser-integration", 4623,
                          title="Dursun Dünya", artist="Teoman - Topic",
                          url="https://www.youtube.com/watch?v=_KdDYkNC_jw&list=RD_KdDYkNC_jw")
        self.viv = player("org.mpris.MediaPlayer2.vivaldi.instance4427", 4427,
                          title="(269) Dursun Dünya - YouTube")
        self.st = stream(app="Vivaldi", binary="vivaldi-bin", pid=4966, media_name="Playback")

    def test_sibling_url_player_is_linked(self):
        self.assertIs(self.c.player_for(self.st, [self.viv, self.pbi]), self.pbi)
        self.check(self.st, [self.viv, self.pbi], Category.MUSIC)
        self.check(self.st, [self.pbi, self.viv], Category.MUSIC)

    def test_change9_marks_group_and_weak(self):
        d = self.c.classify(self.st, [self.viv, self.pbi])
        self.assertEqual((d.kaynak, d.zayif, d.grup), ("tarayici", False, "vivaldi-bin:4427"))
        video = player("org.mpris.MediaPlayer2.plasma-browser-integration", 4623,
                       title="Yapay Genel Zeka", artist="Evrim Ağacı",
                       url="https://www.youtube.com/watch?v=Ec9jyLqYpxg")
        d = self.c.classify(self.st, [video])
        self.assertEqual((d.category, d.zayif), (Category.MOVIE, True))
        # a nameless stream's film guess is weak: the session may be another tab's
        # (x.com muted autoplay took it over while a song played, 2 Kas)
        xcom = player("org.mpris.MediaPlayer2.plasma-browser-integration", 4623,
                      title="Bir gönderi", url="https://x.com/u/status/1")
        d = self.c.classify(self.st, [xcom])
        self.assertEqual((d.category, d.zayif), (Category.MOVIE, True))
        # Firefox-style named tab: no ambiguity group
        named = stream(app="Vivaldi", binary="vivaldi-bin", pid=4966, media_name="Bir sekme")
        self.assertEqual(self.c.classify(named, []).grup, "")
        self.assertTrue(self.c.classify(named, []).zayif)            # site unknown → default

    def test_other_browsers_player_is_not_linked(self):
        # a plasma host under ANOTHER browser (Firefox) must not bind the Vivaldi stream
        ff = (FIREFOX_PID, "firefox", "/usr/lib/firefox/firefox")
        self.env.add(proc(FIREFOX_PID, "firefox"))
        self.env.add(proc(5500, "plasma-browser-", ancestors=[ff]))
        other = player("org.mpris.MediaPlayer2.plasma-browser-integration", 5500,
                       title="Dursun Dünya", artist="Teoman - Topic",
                       url="https://www.youtube.com/watch?v=_KdDYkNC_jw&list=RD_KdDYkNC_jw")
        self.assertIs(self.c.player_for(self.st, [self.viv, other]), self.viv)
        self.check(self.st, [self.viv, other], Category.MOVIE)

    def test_root_stops_at_first_foreign_ancestor(self):
        # a same-name process further up (behind a different one) is not the app root
        sh = (4000, "sh", "sh -c vivaldi")
        top = (3900, "vivaldi-bin", "/opt/vivaldi/vivaldi-bin")
        self.env.add(proc(4100, "vivaldi-bin", ancestors=[sh, top]))
        self.env.add(proc(4200, "plasma-browser-", ancestors=[top]))
        far = player("org.mpris.MediaPlayer2.plasma-browser-integration", 4200,
                     title="x", url="https://www.youtube.com/watch?v=1&list=RD1")
        st = stream(app="Vivaldi", binary="vivaldi-bin", pid=4100, media_name="Playback")
        self.assertIsNone(self.c.player_for(st, [far]))


class TestHataAvi1Kas(Base):
    """Bug hunt 1 Kas 2026 (G2): pattern compile, Meet tabs, Wine players, video titles."""

    def test_inline_flag_pattern_does_not_break_the_classifier(self):
        rules = load_shipped()
        rules["uygulamalar"]["sesli"] = ["(?i)discord", "(?P<x>mumble)", "(?P<x>teamspeak)"]
        c = self.env.classifier(rules)                 # raised PatternError before
        self.check(stream(app="Discord", binary="Discord"), [], Category.VOICE, c=c)

    def test_meet_film_title_is_not_a_call(self):
        self.env.add(proc(FIREFOX_PID, "firefox", exe="firefox", cmdline=("/usr/lib/firefox/firefox",)))
        d = self.c.classify(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                                   media_name="Meet Joe Black - Netflix"), [])
        self.assertNotEqual(d.category, Category.VOICE, d)
        d = self.c.classify(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                                   media_name="Meet - abc-defg-hij"), [])
        self.assertEqual(d.category, Category.VOICE, d)

    def test_wine_music_player_is_not_a_game(self):
        self.env.add(proc(9100, "foobar2000.exe", exe="wine64-preloader"))
        d = self.c.classify(stream(app="foobar2000.exe", binary="foobar2000.exe", pid=9100), [])
        self.assertNotEqual(d.category, Category.GAME, d)

    def test_known_name_before_a_video_title_is_not_music(self):
        self.env.add(proc(FIREFOX_PID, "firefox", exe="firefox", cmdline=("/usr/lib/firefox/firefox",)))
        c = Classifier(load_shipped(), proc_lookup=self.env.procs.get, resolve=self.env.resolve,
                       files_lookup=lambda pid: (), flatpak_lookup=lambda pid: "",
                       artist_lookup=lambda name: True)          # every name is "known"
        s = stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                   media_name="Joker - Official Trailer - YouTube")
        self.assertNotEqual(c.classify(s, []).category, Category.MUSIC)
        s = stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                   media_name="Lana Del Rey - Season of the Witch - YouTube")
        self.assertEqual(c.classify(s, []).category, Category.MUSIC)


class TestUserRules(Base):
    def rules(self, *kural, **genel):
        r = load_shipped()
        r["kural"] = list(kural)
        r["genel"].update(genel)
        return r

    def test_rule_overrides_game(self):
        self.env.add(proc(9100, "witcher3.exe", env={"WINEPREFIX": "/p"}))
        c = self.env.classifier(self.rules({"ad": "witcher", "ikili": "witcher3\\.exe", "kategori": "film"}))
        self.check(stream(app="witcher3.exe", binary="witcher3.exe", pid=9100), [], Category.MOVIE,
                   "film", "kullanıcı kuralı 'witcher'", c=c)

    def test_rule_profile_override_empty(self):
        c = self.env.classifier(self.rules({"uygulama": "spotify", "kategori": "muzik", "profil": ""}))
        self.check(stream(app="spotify", binary="spotify"), [], Category.MUSIC, None, c=c)

    def test_rule_env_table(self):
        self.env.add(proc(9101, "Game.exe", env={"WINEPREFIX": "/home/u/Faugus/rpg"}))
        c = self.env.classifier(self.rules({"ortam": {"WINEPREFIX": "Faugus/rpg"}, "kategori": "cs2"}))
        self.check(stream(app="Game.exe", binary="Game.exe", pid=9101), [], Category.CS2, "cs2", c=c)

    def test_rule_url(self):
        self.env.add(proc(FIREFOX_PID, "firefox"))
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID, title="t",
                   url="https://www.youtube.com/watch?v=1")
        c = self.env.classifier(self.rules({"url": "youtube\\.com", "kategori": "muzik"}))
        self.check(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID, media_name="t - YouTube"),
                   [p], Category.MUSIC, c=c)

    def test_invalid_rules_skipped(self):
        c = self.env.classifier(self.rules(
            {"ikili": "(", "kategori": "film"},          # bad regex
            {"ikili": "x"},                              # no category
            {"kategori": "film"},                        # no matcher
            "not a table",
            {"ikili": "spotify", "kategori": "yok"},     # bad category
        ))
        self.assertEqual(c.user_rules, [])
        self.check(stream(app="spotify", binary="spotify"), [], Category.MUSIC, c=c)

    def test_mapping_override_and_order(self):
        r = self.rules()
        r["eslesme"]["muzik"] = "film"
        r["genel"]["sira"] = ["uygulama"]
        c = self.env.classifier(r)
        self.check(stream(app="spotify", binary="spotify"), [], Category.MUSIC, "film", c=c)
        # 'sesli' step removed from order -> Discord no longer recognised
        self.check(stream(app="Discord", binary="Discord"), [], Category.UNKNOWN, c=c)

    def test_voice_rule_profile_forced_none(self):
        c = self.env.classifier(self.rules({"ikili": "mumble2", "kategori": "sesli", "profil": "muzik"}))
        self.check(stream(app="m", binary="mumble2"), [], Category.VOICE, None, c=c)


class TestRobustness(Base):
    def test_broken_inputs_never_raise(self):
        for bad in (None, 42, "x"):
            d = self.c.classify(bad, [])          # type: ignore[arg-type]
            self.assertEqual(d.category, Category.UNKNOWN)
        d = self.c.classify(stream(app="Firefox", binary="firefox", pid=1), [None, 5])  # type: ignore[list-item]
        self.assertEqual(d.category, Category.MOVIE)   # garbage players ignored
        self.assertIn("varsayılan", d.reason)

    def test_lookup_exceptions(self):
        def boom(*a):
            raise RuntimeError("x")
        c = Classifier(load_shipped(), proc_lookup=boom, resolve=boom, files_lookup=boom,
                       flatpak_lookup=boom)
        d = c.classify(stream(app="spotify", binary="spotify", pid=3), [])
        self.assertEqual(d.category, Category.MUSIC)

    def test_weird_values(self):
        s = stream(app="\x00\udcff", binary="a" * 5000, pid=-5, media_name="((((", role="???")
        d = self.c.classify(s, [player("bogus", None, url="http://[::1")])
        self.assertIsNotNone(d.category)

    def test_fast(self):
        self.env.add(proc(FIREFOX_PID, "firefox"))
        players = [player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                          title="x", url="https://www.youtube.com/watch?v=1")]
        streams = [stream(index=i, app="Firefox", binary="firefox", pid=FIREFOX_PID,
                          media_name="x - YouTube") for i in range(50)]
        streams += [stream(index=100 + i, app="mystery", binary="mystery") for i in range(50)]
        t0 = time.perf_counter()
        for _ in range(10):
            for s in streams:
                self.c.classify(s, players)
        avg_ms = (time.perf_counter() - t0) * 1000 / (10 * len(streams))
        self.assertLess(avg_ms, 5.0)


class TestAssignments(Base):
    """GUI per-app assignments (rules["atama"]) win over everything."""

    def rules(self, *atama):
        r = load_shipped()
        r["atama"] = list(atama)
        return r

    def test_binary_assignment_beats_cs2(self):
        self.env.add(proc(8000, "cs2", env={"SteamAppId": "730"}))
        c = self.env.classifier(self.rules({"alan": "ikili", "deger": "CS2", "profil": "film"}))
        d = self.check(stream(app="cs2", binary="cs2", pid=8000), [], Category.MOVIE, "film", c=c)
        self.assertEqual(d.reason, "kullanıcı ataması")

    def test_app_name_assignment_case_insensitive(self):
        c = self.env.classifier(self.rules({"alan": "uygulama", "deger": "firefox", "profil": "muzik"}))
        self.check(stream(app="Firefox", binary="firefox", media_name="x - YouTube"), [],
                   Category.MUSIC, "muzik", "kullanıcı ataması", c=c)

    def test_flatpak_assignment(self):
        self.env.add(proc(50100, "spotify", env={"FLATPAK_ID": "com.spotify.Client"}), ns_pid=2)
        c = self.env.classifier(self.rules({"alan": "flatpak", "deger": "com.spotify.client", "profil": "oyun"}))
        self.check(stream(app="spotify", binary="spotify", pid=2), [], Category.GAME, "oyun", c=c)

    def test_empty_profile_means_unknown_no_eq(self):
        c = self.env.classifier(self.rules({"alan": "ikili", "deger": "spotify", "profil": ""}))
        self.check(stream(app="spotify", binary="spotify"), [], Category.UNKNOWN, None,
                   "kullanıcı ataması", c=c)

    def test_assignment_beats_user_rule_and_voice(self):
        r = self.rules({"alan": "ikili", "deger": "discord", "profil": "cs2"})
        r["kural"] = [{"ikili": "discord", "kategori": "film"}]
        c = self.env.classifier(r)
        self.check(stream(app="Discord", binary="Discord"), [], Category.CS2, "cs2", c=c)

    def test_no_match_and_invalid_entries(self):
        c = self.env.classifier(self.rules(
            {"alan": "ikili", "deger": "other", "profil": "film"},
            {"alan": "yanlis", "deger": "spotify", "profil": "film"},
            {"alan": "ikili", "deger": "", "profil": "film"},
            "bozuk", None))
        self.assertEqual(len(c.assignments), 1)
        self.check(stream(app="spotify", binary="spotify"), [], Category.MUSIC, "muzik", c=c)


class TestExplain(Base):
    def test_same_decision_and_decisive_rule(self):
        self.env.add(proc(8002, "SDLAudioP2", ancestors=[PV, REAPER730, STEAM]), ns_pid=12)
        s = stream(app="SDL Application", binary="SDLAudioP2", pid=12)
        d, trace = self.c.explain(s, [])
        self.assertEqual(d, self.c.classify(s, []))
        decisive = [t for t in trace if t.startswith("✔")]
        self.assertEqual(len(decisive), 1)
        self.assertIn("cs2", decisive[0])
        self.assertIn("AppId=730", decisive[0])
        self.assertTrue(trace[-1].startswith("→ karar: CS2 / CS2"), trace[-1])
        self.assertTrue(any("host pid 8002" in t for t in trace))

    def test_trace_lists_all_steps_until_match(self):
        self.env.add(proc(FIREFOX_PID, "firefox"))
        p = player("org.mpris.MediaPlayer2.firefox.instance_1_280", FIREFOX_PID,
                   title="Haber", url="https://www.youtube.com/watch?v=1")
        d, trace = self.c.explain(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                                         media_name="Haber - YouTube"), [p])
        self.assertEqual(d.category, Category.MOVIE)
        steps = [t.split(":")[0][2:] for t in trace if t[:1] in "✔✘"]
        self.assertEqual(steps, [step_label(s) for s in ["atama"] + self.c.steps])
        self.assertIn("kullanıcı kuralı", steps)
        self.assertIn("youtube.com", [t for t in trace if t.startswith("✔ tarayıcı")][0])

    def test_user_rule_mismatch_reported(self):
        r = load_shipped()
        r["kural"] = [{"ad": "yalnız mpv", "ikili": "mpv", "kategori": "film"}]
        c = self.env.classifier(r)
        _, trace = c.explain(stream(app="spotify", binary="spotify"), [])
        self.assertTrue(any("'yalnız mpv' ikili uymadı" in t for t in trace), trace)

    def test_assignment_in_trace(self):
        r = load_shipped()
        r["atama"] = [{"alan": "ikili", "deger": "spotify", "profil": "film"}]
        d, trace = self.env.classifier(r).explain(stream(app="spotify", binary="spotify"), [])
        self.assertEqual(d.reason, "kullanıcı ataması")
        self.assertTrue(trace[2].startswith("✔ atama"), trace)

    def test_explain_never_raises(self):
        d, trace = self.c.explain(None, [None])   # type: ignore[arg-type]
        self.assertEqual(d.category, Category.UNKNOWN)
        self.assertTrue(trace)



class TestCeviri(Base):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_reason(self):
        from oto_eq import i18n
        i18n.sifirla("de", {"kullanıcı ataması": "Benutzerzuweisung",
                            "ikili '{ad}'": "Programm '{ad}'"})
        d = self.c.classify(stream(app="cs2", binary="cs2"), [])
        self.assertEqual(d.reason, "Programm 'cs2'")
        r = load_shipped()
        r["atama"] = [{"alan": "ikili", "deger": "spotify", "profil": "film"}]
        d = self.env.classifier(r).classify(stream(app="spotify", binary="spotify"), [])
        self.assertEqual(d.reason, "Benutzerzuweisung")

    def test_trace_labels_translated(self):
        # 6 Kas hata avı: the trace showed "ikili=", "süreç=", step ids and "film / film"
        from oto_eq import i18n
        i18n.sifirla("en", {"ikili": "binary", "uygulama": "app", "süreç": "process",
                            "komut": "command", "tarayıcı": "browser", "kullanıcı kuralı": "user rule",
                            "Film": "Movie", "karar: {kategori} / {profil} ({neden})":
                            "verdict: {kategori} / {profil} ({neden})"})
        self.env.add(proc(FIREFOX_PID, "firefox"))
        _, trace = self.c.explain(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                                         media_name="Haber - YouTube"), [])
        text = "\n".join(trace)
        self.assertIn("binary=firefox, app=Firefox, process=firefox", text)
        self.assertIn("command=firefox", text)
        self.assertIn("✘ user rule:", text)
        self.assertIn("✔ browser:", text)
        self.assertNotIn("süreç=", text)
        self.assertTrue(trace[-1].startswith("→ verdict: Movie / Movie"), trace[-1])


class TestInheritedGameEnv(Base):
    """6 Kas hata avı: a browser / player first opened by a game's link keeps the game's
    environment (WINEPREFIX, SteamAppId, wine ancestors) for the whole session."""

    YTM = "https://music.youtube.com/watch?v=abc"

    def _ff(self, env=None, ancestors=()):
        self.env.add(proc(FIREFOX_PID, "firefox", env=env, ancestors=ancestors))
        return stream(app="Firefox", binary="firefox", pid=FIREFOX_PID, media_name="Lofi Girl - radio")

    def _ytm(self):
        return player("org.mpris.MediaPlayer2.firefox.instance_1_9", FIREFOX_PID,
                      title="radio", artist="Lofi Girl", url=self.YTM)

    def test_browser_with_game_env_is_not_a_game(self):
        for env in ({"WINEPREFIX": "/home/u/Games/oyun/pfx"}, {"LUTRIS_GAME_UUID": "x"},
                    {"SteamAppId": "570"}, {"SteamAppId": "730"}, {"SteamGameId": "730"}):
            with self.subTest(env=env):
                self.check(self._ff(env), [self._ytm()], Category.MUSIC, "muzik",
                           "music.youtube.com")

    def test_browser_under_wine_ancestors_is_not_a_game(self):
        wine = (5000, "wine64-preloader", "C:\\\\Games\\\\Launcher.exe")
        s = self._ff({"WINEPREFIX": "/pfx"}, ancestors=[wine])
        d = self.check(s, [], Category.MOVIE)               # browser default, not Oyun
        self.assertEqual(d.kaynak, "tarayici")

    def test_music_app_with_cs2_env(self):
        self.env.add(proc(7100, "spotify", env={"SteamAppId": "730"}))
        self.check(stream(app="spotify", binary="spotify", pid=7100), [], Category.MUSIC, "muzik")

    def test_games_still_games(self):
        self.env.add(proc(8000, "cs2", env={"SteamAppId": "730"}))
        self.check(stream(app="cs2", binary="cs2", pid=8000), [], Category.CS2)
        self.env.add(proc(8100, "game", env={"SteamAppId": "730"}))
        self.check(stream(app="game", binary="game", pid=8100), [], Category.CS2)
        self.env.add(proc(8200, "Game.exe", env={"WINEPREFIX": "/pfx"}))
        self.check(stream(app="Game.exe", binary="Game.exe", pid=8200), [], Category.GAME)

    def test_media_app_public(self):
        s = self._ff({"WINEPREFIX": "/pfx"})
        self.assertTrue(self.c.media_app(s))
        self.assertTrue(self.c.media_app(s, self.env.procs[FIREFOX_PID]))
        self.env.add(proc(8200, "Game.exe"))
        self.assertFalse(self.c.media_app(stream(app="Game.exe", binary="Game.exe", pid=8200)))
        self.assertFalse(self.c.media_app(None))               # never raises


class TestUserRuleValues(Base):
    """6 Kas hata avı: [[kural]] values as TOML lists and with a "(?i)" prefix."""

    def _c(self, **rule):
        r = load_shipped()
        r["kural"] = [{"ad": "k", "kategori": "muzik", **rule}]
        return self.env.classifier(r)

    def test_list_in_search_field_is_any_of(self):
        c = self._c(medya_adi=["Spotify", "Deezer"])
        self.assertEqual(c.user_rules[0]["medya_adi"].pattern, "(?:Spotify)|(?:Deezer)")
        self.env.add(proc(FIREFOX_PID, "firefox"))
        # str(list) was the character class "['Spotify', 'Deezer']": matched almost any title
        self.check(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                          media_name="Haber bülteni videosu"), [], Category.MOVIE, c=c)
        self.check(stream(app="Firefox", binary="firefox", pid=FIREFOX_PID,
                          media_name="Bir şarkı - Deezer"), [], Category.MUSIC,
                   reason_has="kullanıcı kuralı", c=c)

    def test_list_in_full_field(self):
        c = self._c(ikili=["foo", "bar"])
        self.check(stream(app="bar", binary="bar"), [], Category.MUSIC, reason_has="kullanıcı kuralı", c=c)
        self.check(stream(app="b", binary="b"), [], Category.UNKNOWN, c=c)
        self.check(stream(app="foobar", binary="foobar"), [], Category.UNKNOWN, c=c)   # still full match

    def test_global_flag_prefix(self):
        c = self._c(ikili="(?i)witcher3\\.exe", kategori="film")
        self.assertEqual(len(c.user_rules), 1)
        self.check(stream(app="Witcher3.exe", binary="Witcher3.exe"), [], Category.MOVIE,
                   reason_has="kullanıcı kuralı", c=c)
        c = self._c(medya_adi="(?i)podcast")
        self.check(stream(app="Firefox", binary="firefox", media_name="My PODCAST"), [],
                   Category.MUSIC, c=c)

    def test_table_value_list_and_empty_list(self):
        c = self._c(ortam={"WINEPREFIX": ["Faugus/a", "Faugus/b"]}, kategori="oyun")
        self.env.add(proc(7200, "x", env={"WINEPREFIX": "/home/u/Faugus/b"}))
        self.check(stream(app="x", binary="x", pid=7200), [], Category.GAME,
                   reason_has="kullanıcı kuralı", c=c)
        self.assertEqual(self._c(ikili=[]).user_rules, [])      # empty list: rule skipped


class TestSiteListRegex(unittest.TestCase):
    def test_regex_kept_as_written_domains_lowered(self):
        from oto_eq.classifier import _SiteList
        sl = _SiteList([r"(^|\.)radio\S+\.com", "Example.COM", "www.Foo.org"], "t")
        self.assertEqual(sl.regexes[0].pattern, r"(^|\.)radio\S+\.com")   # \S stays \S
        self.assertEqual(sl.match("radiox.com"), "radiox.com")
        self.assertIsNone(sl.match("radio x.com"))
        self.assertEqual(sl.domains, ["example.com", "foo.org"])
        self.assertEqual(sl.match("www2.example.com"), "example.com")


class TestHls(Base):
    """6 Kas hata avı: a remote .m3u8 (HLS: IPTV, Twitch via streamlink) went to Müzik."""

    def _mpv(self, args, role=""):
        self.env.add(proc(7300, "mpv", cmdline=("mpv",) + tuple(args)))
        return stream(app="mpv", binary="mpv", pid=7300, media_name="index.m3u8", role=role)

    def test_remote_manifest_is_no_audio_evidence(self):
        url = "https://cdn.example.tv/live/trt1/index.m3u8"
        p = player("org.mpris.MediaPlayer2.mpv", 7300, title="TRT 1", url=url)
        d = self.check(self._mpv([url]), [p], Category.MOVIE)
        self.assertNotIn("m3u8", d.reason)
        self.check(self._mpv([url]), [], Category.MOVIE)                     # args + title
        self.check(self._mpv([url], role="Music"), [p], Category.MUSIC,       # mpv's own role
                   reason_has="medya rolü")

    def test_local_playlist_still_music(self):
        p = player("org.mpris.MediaPlayer2.mpv", 7300, title="x",
                   url="file:///home/u/M%C3%BCzik/liste.m3u8")
        self.check(self._mpv(["/home/u/Müzik/liste.m3u8"]), [p], Category.MUSIC,
                   reason_has=".m3u8")
        self.env.add(proc(7301, "mpv", cmdline=("mpv", "/home/u/liste.m3u8")))
        self.check(stream(app="mpv", binary="mpv", pid=7301, media_name="Şarkı"), [],
                   Category.MUSIC, reason_has=".m3u8")


class TestReadOnlyLoader(unittest.TestCase):
    def test_load_classifier_offline_never_requests(self):
        from unittest import mock
        from oto_eq import classifier as C
        calls = []

        class FakeResolver:
            def known_artist(self, name, request=False):
                calls.append((name, request))
                return None
        with mock.patch("oto_eq.tur.shared_resolver", return_value=FakeResolver()):
            for online, want in ((False, False), (True, True)):
                calls.clear()
                c = C.load_classifier(online=online)
                c._known_artist("Gece - Uyku İçin Hikaye - YouTube")
                self.assertEqual(calls, [("Gece", want)])


if __name__ == "__main__":
    unittest.main()
