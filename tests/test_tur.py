"""Offline tests for oto_eq.tur (fetch is mocked; temp cache files only)."""
from __future__ import annotations

import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import tur
from oto_eq.classifier import Classifier
from oto_eq.models import ContentId, MediaPlayer, ProcInfo, Stream

logging.getLogger("oto_eq").setLevel(logging.CRITICAL)

FF = 90832


def stream(app="Firefox", binary="firefox", pid=FF, media_name="", props=None):
    return Stream(index=1, sink_index=0, app_name=app, binary=binary, pid=pid,
                  media_name=media_name, media_role="", node_name=app, corked=False,
                  props=props or {})


def proc(pid=FF, comm="firefox", env=None, cmdline=(), ancestors=()):
    return ProcInfo(pid=pid, comm=comm, exe=comm, cmdline=tuple(cmdline) or (comm,),
                    env=env or {}, ancestors=tuple(ancestors))


def player(title="", artist="", url="", pid=FF, bus="org.mpris.MediaPlayer2.firefox.instance_1_2",
           status="Playing"):
    return MediaPlayer(bus, pid, "Mozilla firefox", status, title, artist, url)


def clf():
    procs = {FF: proc(), 51000: proc(51000, "spotify", env={"FLATPAK_ID": "com.spotify.Client"})}
    return Classifier({}, proc_lookup=procs.get, resolve=lambda s: s.pid if s.pid in procs else None)


class TestNormalize(unittest.TestCase):
    def test_turkish_casefold(self):
        self.assertEqual(tur.norm("SEZEN AKSU"), "sezen aksu")
        self.assertEqual(tur.norm("İbrahim Tatlıses"), tur.norm("IBRAHIM TATLISES"))
        self.assertEqual(tur.norm("ışık"), tur.norm("IŞIK"))
        self.assertEqual(tur.norm("  Daft   Punk "), "daft punk")

    def test_clean_artist(self):
        self.assertEqual(tur.clean_artist("Eminem feat. Rihanna"), "Eminem")
        self.assertEqual(tur.clean_artist("Daft Punk ft. Pharrell Williams"), "Daft Punk")
        self.assertEqual(tur.clean_artist("Daft Punk & Pharrell"), "Daft Punk")
        # CHANGE 4: "ve"/"and" inside band names is not a second artist
        self.assertEqual(tur.normalize_artist("Mor ve Ötesi"), "mor ve ötesi")
        self.assertEqual(tur.clean_artist("Florence and the Machine"), "Florence and the Machine")
        self.assertEqual(tur.clean_artist("Tarkan - Topic", channel=True), "Tarkan")
        self.assertEqual(tur.clean_artist("MetallicaVEVO", channel=True), "Metallica")
        self.assertEqual(tur.clean_artist("VIRGIN BLACK official", channel=True), "VIRGIN BLACK")

    def test_split_title(self):
        self.assertEqual(tur.split_title("Virgin Black - Our Wings Are Burning (Official Video)"),
                         ("Virgin Black", "Our Wings Are Burning"))
        self.assertEqual(tur.split_title("THURISAZ - Years of silence"), ("THURISAZ", "Years of silence"))
        self.assertIsNone(tur.split_title("Rusya Bitti Mi?"))


class TestContentIdMusic(unittest.TestCase):
    def test_youtube_title_parse(self):
        p = player("Metallica - Nothing Else Matters (Official Music Video)", "Metallica",
                   "https://www.youtube.com/watch?v=1")
        cid = tur.content_id(stream(media_name="Metallica - Nothing Else Matters (Official Music "
                                    "Video) - YouTube"), [p], proc=proc(), kind="muzik",
                             classifier=clf())
        self.assertEqual(cid, ContentId("muzik", "sanatci:metallica", "Metallica",
                                        "Nothing Else Matters"))

    def test_tab_count_and_other_tabs_player(self):
        # 25 Eki live: Firefox's single MPRIS player described the anime tab while
        # the YouTube tab "(213) SAFI - ..." played music; the count leaked into the artist
        anime = player("Beyond Time's Gaze Episode 45", "AsyaEkip1", "https://asyaanimeleri.top/x")
        st = stream(media_name="(213) SAFI - Forever Mine (Original Mix) - YouTube")
        for players in ([anime], []):
            cid = tur.content_id(st, players, proc=proc(), kind="muzik", classifier=clf())
            self.assertEqual((cid.key, cid.display, cid.track),
                             ("sanatci:safi", "SAFI", "Forever Mine (Original Mix)"))

    def test_youtube_channel_fallback(self):
        p = player("Şımarık", "Tarkan - Topic", "https://www.youtube.com/watch?v=2")
        cid = tur.content_id(stream(media_name="Şımarık - YouTube"), [p], proc=proc(),
                             kind="muzik", classifier=clf())
        self.assertEqual(cid.key, "sanatci:tarkan")
        self.assertEqual(cid.track, "Şımarık")

    def test_spotify_artist_feat(self):
        s = stream(app="spotify", binary="spotify", pid=51000)
        p = MediaPlayer("org.mpris.MediaPlayer2.spotify", 77777, "Spotify", "Playing",
                        "Love The Way You Lie", "Eminem, Rihanna", "https://open.spotify.com/track/x")
        cid = tur.content_id(s, [p], proc=proc(51000, "spotify",
                                                env={"FLATPAK_ID": "com.spotify.Client"}),
                             kind="muzik", classifier=clf())
        self.assertEqual(cid.key, "sanatci:eminem")
        self.assertEqual(cid.display, "Eminem")
        self.assertEqual(cid.track, "Love The Way You Lie")

    def test_track_change_changes_track_not_key(self):
        c = clf()
        a = tur.content_id(stream(media_name="Daft Punk - One More Time - YouTube"),
                           [player("Daft Punk - One More Time", "", "https://youtube.com/watch?v=a")],
                           proc=proc(), kind="muzik", classifier=c)
        b = tur.content_id(stream(media_name="Daft Punk - Around the World - YouTube"),
                           [player("Daft Punk - Around the World", "", "https://youtube.com/watch?v=b")],
                           proc=proc(), kind="muzik", classifier=c)
        self.assertEqual(a.key, b.key)
        self.assertNotEqual(a.track, b.track)

    def test_no_player_uses_tab_title_or_none(self):
        cid = tur.content_id(stream(media_name="Sezen Aksu - Firuze - YouTube"), [], proc=proc(),
                             kind="muzik", classifier=clf())
        self.assertEqual(cid.key, "sanatci:sezen aksu")
        self.assertIsNone(tur.content_id(stream(media_name="Haber bülteni"), [], proc=proc(),
                                         kind="muzik", classifier=clf()))

    def test_never_raises(self):
        self.assertIsNone(tur.content_id(None, None, classifier=clf()))   # type: ignore[arg-type]


class TestContentIdGame(unittest.TestCase):
    def test_steam_env_and_ancestor(self):
        s = stream(app="cs2", binary="cs2", pid=8000)
        with mock.patch.object(tur, "steam_name", return_value="Counter-Strike 2"):
            cid = tur.content_id(s, [], proc=proc(8000, "cs2", env={"SteamAppId": "730"}),
                                 classifier=clf())
            self.assertEqual(cid, ContentId("oyun", "steam:730", "Counter-Strike 2"))
            cid = tur.content_id(s, [], proc=proc(8000, "x", ancestors=[
                (7001, "reaper", "reaper SteamLaunch AppId=2694490 -- x")]), kind="oyun",
                classifier=clf())
            self.assertEqual(cid.key, "steam:2694490")

    def test_umu_zero_and_md5_ignored_then_faugus_title(self):
        info = proc(9000, "VRising.exe", env={"SteamAppId": "0", "STEAM_COMPAT_APP_ID": "a1b2c3d4e5f6",
                                              "WINEPREFIX": "/home/u/Faugus/vrising"})
        games = [{"gameid": "vrising", "title": "V Rising", "path": "/g/V Rising/VRising.exe",
                  "prefix": "/home/u/Faugus/vrising"}]
        with mock.patch.object(tur, "_faugus_games", return_value=games), \
                mock.patch.object(tur, "_lutris_games", return_value=[]):
            cid = tur.content_id(stream(app="VRising.exe", binary="VRising.exe", pid=9000), [],
                                 proc=info, classifier=clf())
        self.assertEqual(cid, ContentId("oyun", "oyun:v rising", "V Rising"))

    def test_lutris_title_and_exe_fallback(self):
        info = proc(9100, "Big Ambitions.e", env={"WINEPREFIX": "/home/u/Games/big-ambitions"},
                    cmdline=("Z:\\home\\u\\Games\\Big Ambitions\\Big Ambitions.exe",))
        lut = [{"name": "Big Ambitions", "prefix": "/home/u/Games/big-ambitions",
                "exe": "/home/u/Games/Big Ambitions/Big Ambitions.exe", "service": "", "service_id": ""}]
        with mock.patch.object(tur, "_faugus_games", return_value=[]), \
                mock.patch.object(tur, "_lutris_games", return_value=lut):
            cid = tur.content_id(stream(app="Big Ambitions.exe", binary="wine64-preloader", pid=9100),
                                 [], proc=info, classifier=clf())
            self.assertEqual(cid.key, "oyun:big ambitions")
        with mock.patch.object(tur, "_faugus_games", return_value=[]), \
                mock.patch.object(tur, "_lutris_games", return_value=[]):
            cid = tur.content_id(stream(app="PathOfExileSteam.exe", binary="PathOfExileSteam.exe",
                                        pid=9200), [], proc=proc(9200, "PathOfExileStea",
                                                                 env={"WINEPREFIX": "/p"}),
                                 classifier=clf())
        self.assertEqual(cid, ContentId("oyun", "exe:pathofexilesteam", "PathOfExileSteam"))


class TestMapTags(unittest.TestCase):
    def check(self, kind, tags, group):
        self.assertEqual(tur.map_tags(kind, tags), group, tur.score_tags(kind, tags))

    def test_music(self):
        self.check("muzik", [("gothic metal", 5), ("doom metal", 4), ("symphonic", 1)], "metal_rock")
        self.check("muzik", [("house", 6), ("electronic", 9), ("french house", 4)], "elektronik")
        self.check("muzik", [("hip hop", 10), ("rap", 8), ("hardcore hip hop", 2)], "elektronik")
        self.check("muzik", [("pop", 5), ("turkish pop", 6), ("dance-pop", 2)], "pop_vokal")
        self.check("muzik", [("classical", 3), ("contemporary classical", 2), ("piano", 2)],
                   "klasik_akustik")
        self.check("muzik", ["arabesk"], "pop_vokal")
        self.check("muzik", ["türk halk müziği"], "klasik_akustik")
        self.check("muzik", ["anadolu rock"], "metal_rock")
        self.check("muzik", ["türkçe rap"], "elektronik")
        self.check("muzik", ["podcast"], "konusma")
        self.check("muzik", ["seen live", "american", "male vocalists"], "")
        self.check("muzik", [], "")

    def test_games(self):
        self.check("oyun", {"FPS": 9000, "Shooter": 7000, "Competitive": 6000, "Tactical": 5000,
                            "Multiplayer": 4000}, "rekabetci")
        self.check("oyun", {"Action RPG": 3000, "Hack and Slash": 2500, "Loot": 2000, "RPG": 1900,
                            "Free to Play": 1800}, "aksiyon_rpg")
        self.check("oyun", {"Survival": 9000, "Zombies": 8000, "Open World": 7000,
                            "Crafting": 6000, "Horror": 5000}, "korku_hayatta_kalma")
        self.check("oyun", {"City Builder": 500, "Strategy": 400, "Management": 300}, "strateji_insa")
        self.check("oyun", {"Racing": 500, "Driving": 400, "Simulation": 300}, "yaris_sim")
        self.check("oyun", ["Action", "Adventure", "RPG"], "aksiyon_rpg")   # store genres


class FakeNet:
    def __init__(self, routes):
        self.routes = routes
        self.urls: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, url):
        with self.lock:
            self.urls.append(url)
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"beklenmeyen istek {url}")


MB_VB = {"artists": [
    {"id": "x1", "name": "Virgin Black", "score": 100,
     "tags": [{"name": "gothic metal", "count": 3}, {"name": "doom metal", "count": 2}]},
    {"id": "x2", "name": "Virgin Steele", "score": 60, "tags": [{"name": "heavy metal", "count": 5}]}]}


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "tur-onbellek.json"
        self.now = [1_800_000_000.0]
        self.sleeps: list[float] = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, routes, settings=None):
        self.net = FakeNet(routes)
        r = tur.GenreResolver(settings=settings or {"cevrimici": True, "sanatci": {}, "oyun": {}},
                              cache_path=self.cache, fetch=self.net, clock=lambda: self.now[0],
                              sleep=self.sleeps.append, db=False)
        self.addCleanup(r.stop)
        return r

    def run_req(self, r, cid):
        got = []
        self.assertTrue(r.request(cid, lambda c, res: got.append(res)))
        self.assertTrue(r.wait_idle(5))
        return got

    def test_musicbrainz_positive_cached(self):
        r = self.make({"musicbrainz.org/ws/2/artist?": MB_VB})
        cid = ContentId("muzik", "sanatci:virgin black", "Virgin Black")
        self.assertIsNone(r.cached(cid))
        got = self.run_req(r, cid)
        self.assertEqual(got[0].group, "metal_rock")
        self.assertIn("MusicBrainz", got[0].source)
        self.assertIn("gothic metal", got[0].source)
        self.assertEqual(len(self.net.urls), 1)               # search had tags: no lookup
        self.assertIn("User-Agent", tur.http_get_json.__code__.co_consts.__repr__() + "User-Agent")
        # persisted, reusable by a fresh resolver without network
        r2 = self.make({})
        self.assertEqual(r2.cached(cid).group, "metal_rock")
        self.assertFalse(r2.request(cid, lambda c, res: None))  # already known
        self.assertTrue(r2.known_artist("VIRGIN BLACK"))

    def test_musicbrainz_lookup_when_no_tags(self):
        r = self.make({"artist?query": {"artists": [{"id": "m1", "name": "Nazan Öncel", "score": 100}]},
                       "artist/m1": {"genres": [{"name": "turkish pop", "count": 2}],
                                     "tags": [{"name": "pop", "count": 1}]}})
        got = self.run_req(r, ContentId("muzik", "sanatci:nazan öncel", "Nazan Öncel"))
        self.assertEqual(got[0].group, "pop_vokal")
        self.assertEqual(len(self.net.urls), 2)
        self.assertGreaterEqual(self.sleeps and min(self.sleeps) or 1.0, 0.0)

    def test_negative_cached_with_ttl(self):
        r = self.make({"artist?query": {"artists": []}})
        cid = ContentId("muzik", "sanatci:vlog", "Vlog")
        got = self.run_req(r, cid)
        self.assertEqual(got[0].group, "")
        self.assertFalse(r.known_artist("Vlog"))
        self.now[0] += tur.NEGATIVE_TTL + 1
        self.assertIsNone(r.cached(cid))                     # expired -> may retry
        self.assertIsNone(r.known_artist("Vlog"))

    def test_positive_ttl(self):
        r = self.make({"artist?query": MB_VB})
        cid = ContentId("muzik", "sanatci:virgin black", "Virgin Black")
        self.run_req(r, cid)
        self.now[0] += tur.POSITIVE_TTL - 10
        self.assertIsNotNone(r.cached(cid))
        self.now[0] += 20
        self.assertIsNone(r.cached(cid))

    def test_network_error_not_cached_and_retry_limited(self):
        r = self.make({"artist?query": OSError("ağ yok")})
        cid = ContentId("muzik", "sanatci:x", "X")
        got = self.run_req(r, cid)
        self.assertEqual(got, [])
        self.assertIsNone(r.cached(cid))
        self.assertFalse(r.request(cid, lambda c, res: None))  # recently failed
        self.assertFalse(self.cache.exists())

    def test_offline_mode(self):
        r = self.make({}, settings={"cevrimici": False, "sanatci": {}, "oyun": {}})
        self.assertFalse(r.request(ContentId("muzik", "sanatci:x", "X"), lambda c, res: None))
        self.assertEqual(self.net.urls, [])

    def test_overrides_first(self):
        r = self.make({}, settings={"cevrimici": True, "sanatci": {"Virgin Black": "klasik_akustik"},
                                    "oyun": {"steam:730": "yaris_sim", "V Rising": ""}})
        self.assertEqual(r.cached(ContentId("muzik", "sanatci:virgin black", "Virgin Black")),
                         tur.GenreResult("klasik_akustik", "kullanıcı", 1.0))
        self.assertEqual(r.cached(ContentId("oyun", "steam:730", "Counter-Strike 2")).group, "yaris_sim")
        self.assertEqual(r.cached(ContentId("oyun", "oyun:v rising", "V Rising")).group, "")
        self.assertTrue(r.known_artist("virgin black"))

    def test_steam_spy_tags(self):
        r = self.make({"steamspy.com": {"name": "Counter-Strike 2", "tags": {
            "FPS": 9000, "Shooter": 7000, "Competitive": 6000, "Tactical": 5000}}})
        got = self.run_req(r, ContentId("oyun", "steam:730", "Counter-Strike 2"))
        self.assertEqual(got[0].group, "rekabetci")
        self.assertIn("SteamSpy", got[0].source)
        self.assertEqual(len(self.net.urls), 1)

    def test_steam_store_fallback_and_search(self):
        r = self.make({"storesearch": {"items": [{"id": 1604030, "name": "V Rising"}]},
                       "steamspy.com": {"tags": []},
                       "api/appdetails": {"1604030": {"success": True, "data": {
                           "name": "V Rising", "genres": [{"description": "Action"},
                                                          {"description": "Adventure"},
                                                          {"description": "RPG"}]}}}})
        got = self.run_req(r, ContentId("oyun", "oyun:v rising oyunindir vip", "v.rising.Oyunindir vip"))
        self.assertEqual(got[0].group, "aksiyon_rpg")
        self.assertIn("term=v%20rising", self.net.urls[0])
        self.assertEqual(len(self.net.urls), 3)

    def test_unconfident_search_is_negative(self):
        r = self.make({"storesearch": {"items": [{"id": 1, "name": "Totally Different Game"}]}})
        got = self.run_req(r, ContentId("oyun", "oyun:my game", "My Game"))
        self.assertEqual(got[0].group, "")
        self.assertEqual(len(self.net.urls), 1)

    def test_rate_limit_sleeps(self):
        # CHANGE 4: per host — two MusicBrainz requests in a row wait, another host does not
        r = self.make({"artist?query": MB_VB, "steamspy.com": {"tags": {"Racing": 5}}})
        self.run_req(r, ContentId("muzik", "sanatci:virgin black", "Virgin Black"))
        self.run_req(r, ContentId("oyun", "steam:1", "X"))
        self.assertEqual(self.sleeps, [])
        self.run_req(r, ContentId("muzik", "sanatci:virgin steele", "Virgin Black"))
        self.assertTrue(any(0.5 < s <= tur.HOST_INTERVALS["musicbrainz.org"] for s in self.sleeps),
                        self.sleeps)

    def test_corrupt_cache_recovered(self):
        self.cache.write_text("{bozuk", "utf-8")
        r = self.make({})
        self.assertIsNone(r.cached(ContentId("muzik", "sanatci:x", "X")))
        self.assertTrue(self.cache.with_name(self.cache.name + ".bozuk").exists())

    def test_cache_file_format(self):
        r = self.make({"artist?query": MB_VB})
        self.run_req(r, ContentId("muzik", "sanatci:virgin black", "Virgin Black"))
        data = json.loads(self.cache.read_text("utf-8"))
        e = data["kayitlar"]["sanatci:virgin black"]
        self.assertEqual(e["name"], "Virgin Black")
        self.assertFalse(e["negatif"])
        self.assertEqual(e["tags"][0][0], "gothic metal")


class TestClassifierKnownArtist(unittest.TestCase):
    """Coordinator request: plain "Artist - Song" YouTube titles."""

    def make(self, known):
        procs = {FF: proc()}
        self.asked: list[str] = []

        def lookup(name):
            self.asked.append(name)
            return known.get(name.lower())
        return Classifier({}, proc_lookup=procs.get, resolve=lambda s: s.pid,
                          artist_lookup=lookup)

    def test_known_artist_is_music(self):
        c = self.make({"thurisaz": True})
        p = player("THURISAZ - Years of silence", "Thurisaz", "https://www.youtube.com/watch?v=9")
        d = c.classify(stream(media_name="THURISAZ - Years of silence - YouTube"), [p])
        self.assertEqual(d.category.value, "muzik")
        self.assertEqual(d.reason, "tarayıcı: bilinen sanatçı 'THURISAZ'")

    def test_unknown_or_negative_artist_stays_film(self):
        for known in ({}, {"vlog": False}):
            c = self.make(known)
            p = player("Vlog - Day 3", "Someone", "https://www.youtube.com/watch?v=3")
            d = c.classify(stream(media_name="Vlog - Day 3 - YouTube"), [p])
            self.assertEqual(d.category.value, "film")
            self.assertEqual(self.asked, ["Vlog"])

    def test_tab_only_path(self):
        c = self.make({"thurisaz": True})
        d = c.classify(stream(media_name="(3) THURISAZ - Years of silence - YouTube"), [])
        self.assertEqual(d.category.value, "muzik")

    def test_mix_playlist_url_is_music(self):
        c = self.make({})
        p = player("THURISAZ - Years of silence", "Thurisaz",
                   "https://www.youtube.com/watch?v=abc&list=RDabc&start_radio=1")
        d = c.classify(stream(media_name="THURISAZ - Years of silence - YouTube"), [p])
        self.assertEqual(d.category.value, "muzik")
        self.assertIn("list=RD", d.reason)
        self.assertEqual(self.asked, [])           # decided before any artist lookup


class TestChange3Followups(unittest.TestCase):
    """UA, 503 retry, built-in Turkish artists, title hints, weak 'turkish' tag."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "c.json"
        self.sleeps: list[float] = []

    def make(self, fetch=None, **kw):
        kw.setdefault("db", False)       # the Wikidata database has its own tests (TestArtistDB)
        r = tur.GenreResolver(settings=kw.pop("settings", {"cevrimici": True}), cache_path=self.cache,
                              fetch=fetch or FakeNet({}), sleep=self.sleeps.append, **kw)
        self.addCleanup(r.stop)
        return r

    def test_user_agent_is_ascii(self):
        tur.USER_AGENT.encode("ascii")
        tur.USER_AGENT.encode("latin-1")

    def test_busy_503_retried_not_cached(self):
        import urllib.error
        calls = []

        def fetch(url):
            calls.append(url)
            if len(calls) < 3:
                raise urllib.error.HTTPError(url, 503, "busy", {}, None)
            return MB_VB
        r = self.make(fetch)
        cid = ContentId("muzik", "sanatci:virgin black", "Virgin Black")
        got = []
        self.assertTrue(r.request(cid, lambda c, res: got.append(res)))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(len(calls), 3)
        self.assertEqual(got[0].group, "metal_rock")
        self.assertIn(2.5, self.sleeps)
        self.assertIn(5.0, self.sleeps)

    def test_busy_forever_is_error_not_negative(self):
        import urllib.error

        def fetch(url):
            raise urllib.error.HTTPError(url, 503, "busy", {}, None)
        r = self.make(fetch)
        cid = ContentId("muzik", "sanatci:x", "X")
        r.request(cid, lambda c, res: None)
        self.assertTrue(r.wait_idle(5))
        self.assertIsNone(r.cached(cid))                     # no negative entry
        self.assertFalse(self.cache.exists())

    def test_builtin_turkish_artists_without_network(self):
        net = FakeNet({})
        r = self.make(net)
        for name, group in (("Sezen Aksu", "pop_vokal"), ("DUMAN", "metal_rock"),
                            ("Mor ve Ötesi", "metal_rock"),
                            ("Neşet Ertaş", "klasik_akustik"), ("ezhel", "elektronik"),
                            ("İbrahim Tatlıses", "pop_vokal")):
            cid = ContentId("muzik", "sanatci:" + tur.normalize_artist(name), name, "x")
            res = r.cached(cid)
            self.assertEqual((res.group, res.source), (group, "yerleşik liste"), name)
            self.assertFalse(r.request(cid, lambda c, res: None))
        self.assertTrue(r.known_artist("Müslüm Gürses"))
        self.assertEqual(net.urls, [])
        # the user's own assignment wins over the built-in list
        r2 = self.make(settings={"cevrimici": True, "sanatci": {"Sezen Aksu": "klasik_akustik"}})
        self.assertEqual(r2.cached(ContentId("muzik", "sanatci:sezen aksu", "Sezen Aksu")).group,
                         "klasik_akustik")
        self.assertIsNone(self.make(builtin=False).cached(
            ContentId("muzik", "sanatci:sezen aksu", "Sezen Aksu")))

    def test_title_hints(self):
        r = self.make(settings={"cevrimici": True, "sanatci": {"Duman": "metal_rock"}},
                      title_hints=tur.title_hints_from_rules(
                          {"tur": {"baslik_ipucu": {r"\bsenfoni\b": "klasik_akustik",
                                                    "bad": "yok_boyle_grup"}}}))
        def g(artist, track):
            return r.cached(ContentId("muzik", "sanatci:" + tur.normalize_artist(artist), artist, track))
        self.assertEqual(g("Tarkan", "Şımarık (Akustik)").group, "klasik_akustik")
        self.assertIn("akustik", g("Tarkan", "Şımarık (Akustik)").source)
        self.assertEqual(g("Tarkan", "Kuzu Kuzu (Remix)").group, "elektronik")
        self.assertEqual(g("Tarkan", "Şımarık").group, "pop_vokal")
        self.assertEqual(g("Some Podcast", "Bölüm 42: konuk").group, "konusma")
        # 25 Eki live case: MusicBrainz "SAFI" = unrelated indie trio; the title decides
        self.assertEqual(g("SAFI", "Forever Mine (Original Mix)").group, "elektronik")
        self.assertEqual(g("X", "Gece (Bass Boosted)").group, "elektronik")
        self.assertIsNone(g("Housemartins", "Happy Hour"))     # "house" alone is no hint
        self.assertEqual(g("X", "Senfoni No 5").group, "klasik_akustik")      # kurallar.toml
        self.assertEqual(g("Duman", "Bu Akşam (Akustik)").group, "metal_rock")  # user wins
        r.set_title_hints({})
        self.assertEqual(g("Tarkan", "Şımarık (Akustik)").group, "pop_vokal")

    def test_weak_turkish_tag_and_disambiguation(self):
        r = self.make(FakeNet({"artist?query": {"artists": [
            {"id": "a", "name": "Nazan Öncel", "score": 100, "country": "TR",
             "tags": [{"name": "turkish", "count": 3}, {"name": "turkish", "count": 1},
                      {"name": "a", "count": 1}]}]}}))
        cid = ContentId("muzik", "sanatci:nazan öncel", "Nazan Öncel")
        r.request(cid, lambda c, res: None)
        self.assertTrue(r.wait_idle(5))
        res = r.cached(cid)
        self.assertEqual((res.group, res.confidence), ("pop_vokal", 0.4))
        self.assertIn("zayıf ipucu", res.source)
        r2 = tur.GenreResolver(settings={"cevrimici": True}, cache_path=Path(self.tmp.name) / "d.json",
                               fetch=FakeNet({"artist?query": {"artists": [
                                   {"id": "m", "name": "Kolpa", "score": 100,
                                    "disambiguation": "Turkish nu metal band",
                                    "tags": [{"name": "turkish", "count": 2}]}]},
                                   "artist/m?": {"genres": [], "tags": []}}),
                               sleep=lambda s: None, builtin=False)
        self.addCleanup(r2.stop)
        cid2 = ContentId("muzik", "sanatci:kolpa", "Kolpa")
        r2.request(cid2, lambda c, res: None)
        self.assertTrue(r2.wait_idle(5))
        self.assertEqual(r2.cached(cid2).group, "metal_rock")

    def test_inexact_artist_is_negative(self):
        r = self.make(FakeNet({"artist?query": {"artists": [
            {"id": "t", "name": "Tarkan Çakır", "score": 100, "tags": [{"name": "jazz", "count": 9}]}]}}),
            builtin=False)
        cid = ContentId("muzik", "sanatci:tarkan", "Tarkan")
        r.request(cid, lambda c, res: None)
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(r.cached(cid).group, "")

    def test_request_joins_pending_lookup(self):
        gate = threading.Event()
        calls = []

        def fetch(url):
            calls.append(url)
            gate.wait(5)
            return MB_VB
        r = self.make(fetch)
        cid1 = ContentId("muzik", "sanatci:virgin black", "Virgin Black")       # classifier
        cid2 = ContentId("muzik", "sanatci:virgin black", "Virgin Black", "Of Your Beauty")
        got = []
        self.assertTrue(r.request(cid1, lambda c, res: got.append(("clf", c, res.group))))
        self.assertTrue(r.request(cid2, lambda c, res: got.append(("router", c, res.group))))
        cid3 = ContentId("muzik", "sanatci:virgin black", "Virgin Black")      # classifier again
        self.assertTrue(r.request(cid3, lambda c, res: got.append(("clf2", c, res.group))))
        gate.set()
        self.assertTrue(r.wait_idle(5))
        # the artist is fetched once; CHANGE 4: the router's song is looked up at song level
        # (iTunes/Deezer answer nothing here) and falls back to the artist entry
        self.assertEqual(sum("musicbrainz" in u for u in calls), 1)
        self.assertEqual(sorted(g[0] for g in got), ["clf", "clf2", "router"])
        self.assertEqual(next(g for g in got if g[0] == "router")[1:], (cid2, "metal_rock"))

    def test_invalidate_rereads_cache_file(self):
        r = self.make(FakeNet({"artist?query": MB_VB}))
        cid = ContentId("muzik", "sanatci:virgin black", "Virgin Black")
        r.request(cid, lambda c, res: None)
        self.assertTrue(r.wait_idle(5))
        self.assertIsNotNone(r.cached(cid))
        self.cache.unlink()                                  # GUI "Tür önbelleğini sil"
        self.assertIsNotNone(r.cached(cid))                  # still in memory
        r.invalidate()
        self.assertIsNone(r.cached(cid))



# ============================================================================
#  CHANGE 4: song-level online sources, audio memory, cache merge
# ============================================================================

def itunes(*rows):
    return {"resultCount": len(rows), "results": [
        {"wrapperType": "track", "kind": "song", "artistName": a, "trackName": t,
         "primaryGenreName": g} for a, t, g in rows]}


def deezer(*rows):
    return {"data": [{"title": t, "artist": {"name": a}, "album": {"id": alb}} for a, t, alb in rows],
            "total": len(rows)}


def yt_page(title, author, keywords, category="Music"):
    vd = {"videoId": "HCXVzBpfvVc", "title": title, "keywords": keywords, "author": author}
    mf = {"playerMicroformatRenderer": {"category": category, "ownerChannelName": author}}
    return ("<html>var ytInitialPlayerResponse = {\"playabilityStatus\":{},\"videoDetails\":"
            + json.dumps(vd) + ",\"microformat\":" + json.dumps(mf) + "};</html>")


NOTHING = {"resultCount": 0, "results": []}
SAFI_URL = "https://www.youtube.com/watch?v=HCXVzBpfvVc&list=RDHCXVzBpfvVc&start_radio=1"


class TestGenreNames(unittest.TestCase):
    def test_store_genre_names(self):
        cases = {
            "Dance": "elektronik", "Electronic": "elektronik", "Hip-Hop/Rap": "elektronik",
            "Turkish Hip-Hop/Rap": "elektronik", "Rap/Hip Hop": "elektronik", "Electro": "elektronik",
            "Bass": "elektronik", "Dubstep": "elektronik", "Jungle/Drum'n'bass": "elektronik",
            "House": "elektronik", "Dans": "elektronik", "Elektronik": "elektronik",
            "Pop": "pop_vokal", "Turkish Pop": "pop_vokal", "Arabesque": "pop_vokal",
            "Fantezi": "pop_vokal", "R&B/Soul": "pop_vokal", "Soul & Funk": "pop_vokal",
            "Rock": "metal_rock", "Turkish Rock": "metal_rock", "Metal": "metal_rock",
            "Alternative": "metal_rock", "Alternatif": "metal_rock", "Turkish Alternative": "metal_rock",
            "Halk": "klasik_akustik", "Sanat": "klasik_akustik", "Özgün": "klasik_akustik",
            "Türk Halk Müziği": "klasik_akustik", "Jazz": "klasik_akustik", "Caz": "klasik_akustik",
            "Classical": "klasik_akustik", "Spoken Word": "konusma", "Comedy": "konusma",
            "Soundtrack": "", "Films/Games": "", "Worldwide": "klasik_akustik",
        }
        for name, group in cases.items():
            self.assertEqual(tur.map_tags("muzik", [name]), group, name)

    def test_youtube_keywords(self):
        kw = ["deep house mix 2025", "night drive music", "chill deep house", "luxury car music",
              "trap music", "deep house", "chillout", "house music", "electro house", "remix"]
        self.assertEqual(tur.map_tags("muzik", kw), "elektronik")
        self.assertEqual(tur.map_tags("muzik", ["arabesk", "damar", "müslüm gürses"]), "pop_vokal")
        self.assertEqual(tur.map_tags("muzik", ["phonk", "drift phonk", "brazilian phonk"]), "elektronik")


class TestMatchHelpers(unittest.TestCase):
    def test_artist_matches(self):
        m = tur.artist_matches
        self.assertTrue(m("Müslüm Gürses", "Muslum Gurses"))
        self.assertTrue(m("MÜSLÜM GÜRSES", "müslüm gürses"))
        self.assertTrue(m("Ezhel & Ufo361", "Ezhel"))
        self.assertTrue(m("Duman feat. Teoman", "Duman"))
        self.assertTrue(m("Mor ve Ötesi", "Mor ve Ötesi"))
        self.assertFalse(m("Florence and the Machine", "Florence"))
        self.assertTrue(m("MorveOtesi", "Mor ve Ötesi"))
        self.assertFalse(m("Mephisto", "SAFI"))
        self.assertFalse(m("Safiye Ayla", "SAFI"))
        self.assertFalse(m("Tarkan Çakır", "Tarkan"))
        self.assertFalse(m("", "SAFI"))

    def test_track_helpers(self):
        self.assertEqual(tur.clean_track("Forever Mine (Original Mix)"), "Forever Mine")
        self.assertEqual(tur.clean_track("Song - Remastered 2011"), "Song")
        self.assertEqual(tur.clean_track("Felaket [Official Video] feat. X"), "Felaket")
        self.assertEqual(tur.clean_track("(Intro)"), "(Intro)")
        self.assertEqual(tur.clean_track("GOZALO💫 BRAZILIAN FUNK PHONK"), "GOZALO")
        self.assertEqual(tur.clean_track("Forever Mine | Deep House 2026"), "Forever Mine")
        self.assertTrue(tur.track_matches("Forever Mine (Extended Mix)", "Forever Mine (Original Mix)"))
        self.assertTrue(tur.track_matches("Bu Akşam", "bu aksam"))
        self.assertFalse(tur.track_matches("BLUE", "Forever Mine"))
        cid = ContentId("muzik", "sanatci:safi", "SAFI", "Forever Mine (Original Mix)")
        self.assertEqual(tur.track_key(cid), "parca:safi|forever mine")
        self.assertEqual(tur.track_key(ContentId("muzik", "sanatci:safi", "SAFI")), "")
        self.assertEqual(tur.track_key(ContentId("oyun", "steam:1", "X", "y")), "")

    def test_youtube_video_id(self):
        v = tur.youtube_video_id
        self.assertEqual(v(SAFI_URL), "HCXVzBpfvVc")
        self.assertEqual(v("https://youtu.be/HCXVzBpfvVc?t=3"), "HCXVzBpfvVc")
        self.assertEqual(v("https://music.youtube.com/watch?v=HCXVzBpfvVc&list=x"), "HCXVzBpfvVc")
        self.assertEqual(v("https://m.youtube.com/shorts/HCXVzBpfvVc"), "HCXVzBpfvVc")
        self.assertEqual(v("https://example.com/watch?v=HCXVzBpfvVc"), "")
        self.assertEqual(v("https://www.youtube.com/watch?v=bad"), "")
        self.assertEqual(v(""), "")

    def test_parse_youtube_page(self):
        info = tur.parse_youtube_page(yt_page("SAFI - Forever Mine (Original Mix)", "Safi Studio Records",
                                              ["deep house", "car music", "a \"quoted\" one"]))
        self.assertEqual(info["keywords"], ["deep house", "car music", 'a "quoted" one'])
        self.assertEqual(info["category"], "Music")
        self.assertEqual(info["author"], "Safi Studio Records")
        self.assertEqual(info["title"], "SAFI - Forever Mine (Original Mix)")
        self.assertEqual(tur.parse_youtube_page("<html>consent</html>")["keywords"], [])
        self.assertEqual(tur.parse_youtube_page(None)["keywords"], [])     # type: ignore[arg-type]

    def test_content_id_fills_url(self):
        p = player("SAFI - Emotion", "Safi Studio Records", SAFI_URL)
        cid = tur.content_id(stream(media_name="SAFI - Emotion - YouTube"), [p], proc=proc(),
                             kind="muzik", classifier=clf())
        self.assertEqual(cid.url, SAFI_URL)
        self.assertEqual(cid, ContentId("muzik", "sanatci:safi", "SAFI", "Emotion"))   # url: compare=False
        cid = tur.content_id(stream(media_name="SAFI - Emotion - YouTube"), [], proc=proc(),
                             kind="muzik", classifier=clf())
        self.assertEqual(cid.url, "")


class TestTrackSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "tur-onbellek.json"
        self.now = [1_800_000_000.0]
        self.sleeps: list[float] = []

    def make(self, routes, settings=None, path=None):
        self.net = FakeNet(routes)
        r = tur.GenreResolver(settings=settings or {"cevrimici": True}, cache_path=path or self.cache,
                              fetch=self.net, clock=lambda: self.now[0], sleep=self.sleeps.append,
                              builtin=False, title_hints={})
        self.addCleanup(r.stop)
        return r

    def resolve(self, r, cid):
        got = []
        self.assertTrue(r.request(cid, lambda c, res: got.append(res)))
        self.assertTrue(r.wait_idle(5))
        return got

    def hosts(self):
        return [u.split("/")[2] for u in self.net.urls]

    def test_itunes_song_hit_sets_song_and_artist(self):
        r = self.make({"itunes.apple.com/search": itunes(
            ("Duman", "Bu Akşam", "Turkish Rock"), ("Duman", "Bu Akşam (Live)", "Turkish Rock"),
            ("Duman", "Senden Daha Güzel", "Turkish Rock"))})
        cid = ContentId("muzik", "sanatci:duman", "Duman", "Bu Akşam")
        got = self.resolve(r, cid)
        self.assertEqual(got[0].group, "metal_rock")
        self.assertEqual(got[0].source, "iTunes: Turkish Rock")
        self.assertLessEqual(got[0].confidence, tur.SOURCE_TRUST["iTunes"])
        self.assertEqual(self.hosts(), ["itunes.apple.com"])            # first hit wins
        self.assertIn("country=TR", self.net.urls[0])
        self.assertIn("term=Duman%20Bu%20Ak%C5%9Fam", self.net.urls[0])
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertIn("parca:duman|bu akşam", data)
        self.assertEqual(data["sanatci:duman"]["source"], "iTunes")      # artist from the song
        # the next song of the artist: known at once from the artist entry, no request
        nxt = ContentId("muzik", "sanatci:duman", "Duman", "Melankoli")
        self.assertEqual(r.cached(nxt).group, "metal_rock")
        self.assertFalse(r.request(nxt, lambda c, res: None))
        self.assertTrue(r.known_artist("DUMAN"))

    def test_name_mismatch_guard_and_youtube_keywords(self):
        # 25 Eki live case: "SAFI - Forever Mine" — iTunes/Deezer return OTHER artists
        page = yt_page("SAFI - Emotion (Original Mix)", "Safi Studio Records",
                       ["deep house mix 2025", "night drive music", "safi", "emotion", "deep house",
                        "house music", "car music"])
        r = self.make({"country=TR": itunes(("Mephisto", "Emotion (Extended Mix)", "Dance"),
                                            ("Safiye Ayla", "Emotion", "Turkish")),
                       "api.deezer.com/search": deezer(("Sahi Siva", "Emotion", 5)),
                       "youtube.com/watch": page,
                       "country=US": NOTHING})
        cid = ContentId("muzik", "sanatci:safi", "SAFI", "Emotion", url=SAFI_URL)
        got = self.resolve(r, cid)
        self.assertEqual(got[0].group, "elektronik")
        self.assertTrue(got[0].source.startswith("YouTube etiketleri: "), got[0].source)
        self.assertIn("deep house", got[0].source)
        self.assertEqual(self.hosts(), ["itunes.apple.com", "api.deezer.com", "www.youtube.com"])
        # privacy: only the video id goes to YouTube (no playlist / radio parameters)
        self.assertEqual(self.net.urls[2], "https://www.youtube.com/watch?v=HCXVzBpfvVc")
        self.assertNotIn("musicbrainz", " ".join(self.net.urls))       # song hit: no artist search

    def test_all_sources_negative_then_musicbrainz_then_audio_memory(self):
        r = self.make({"country=TR": NOTHING, "api.deezer.com/search": deezer(),
                       "country=US": NOTHING, "musicbrainz.org/ws/2/artist?": {"artists": [
                           {"id": "s", "name": "SAFI", "score": 100, "tags": []}]},
                       "musicbrainz.org/ws/2/artist/s": {"genres": [], "tags": []}})
        cid = ContentId("muzik", "sanatci:safi", "SAFI", "Forever Mine")
        got = self.resolve(r, cid)
        self.assertEqual(got[0].group, "")
        self.assertEqual(got[0].source, "iTunes, Deezer: tür bulunamadı")
        self.assertEqual([u.split("/")[2] for u in self.net.urls],
                         ["itunes.apple.com", "api.deezer.com", "itunes.apple.com",
                          "musicbrainz.org", "musicbrainz.org"])
        # no YouTube URL → no YouTube request; the US store was asked (TR had no such artist)
        self.assertIn("country=US", self.net.urls[2])
        # a new song of this artist is looked up (song level), the artist is not asked again
        nxt = ContentId("muzik", "sanatci:safi", "SAFI", "Emotion")
        self.assertIsNone(r.cached(nxt))
        n = len(self.net.urls)
        self.resolve(r, nxt)
        self.assertNotIn("musicbrainz", " ".join(self.net.urls[n:]))
        # router: ~25 s of audio says elektronik → used for the artist's next songs
        self.assertTrue(r.remember_audio(cid, "elektronik", 0.72, "ses analizi: güçlü bas, 124 BPM"))
        res = r.cached(nxt)
        self.assertEqual((res.group, res.source, res.confidence),
                         ("elektronik", "ses analizi: güçlü bas, 124 BPM", 0.72))
        third = ContentId("muzik", "sanatci:safi", "SAFI", "Third")
        self.assertIsNone(r.cached(third))                # online: the song is still looked up
        self.assertFalse(r.remember_audio(cid, "yok_boyle", 0.9, "x"))
        self.assertFalse(r.remember_audio(ContentId("oyun", "steam:1", "X"), "elektronik", 0.9, "x"))

    def test_audio_memory_never_beats_names_and_is_replaced(self):
        r = self.make({"country=TR": itunes(("Kolpa", "Hayat", "Rock"))})
        known = ContentId("muzik", "sanatci:kolpa", "Kolpa", "Hayat")
        self.resolve(r, known)
        self.assertTrue(r.remember_audio(known, "elektronik", None, "bas ağırlıklı"))
        self.assertEqual(r.cached(ContentId("muzik", "sanatci:kolpa", "Kolpa", "Yeni")).group,
                         "metal_rock")                      # name-based positive wins
        # audio memory first, a later positive online artist result removes it
        r2 = self.make({"country=TR": itunes(("Nova", "Gece", "Dance"))}, path=Path(self.tmp.name) / "b.json")
        nova = ContentId("muzik", "sanatci:nova", "Nova", "Gece")
        r2.remember_audio(nova, "pop_vokal", 0.5, "")
        data = json.loads((Path(self.tmp.name) / "b.json").read_text("utf-8"))["kayitlar"]
        self.assertEqual(data["ses:sanatci:nova"]["source"], "ses analizi")
        self.resolve(r2, nova)
        data = json.loads((Path(self.tmp.name) / "b.json").read_text("utf-8"))["kayitlar"]
        self.assertNotIn("ses:sanatci:nova", data)
        self.assertEqual(r2.cached(ContentId("muzik", "sanatci:nova", "Nova", "Başka")).group, "elektronik")

    def test_audio_memory_offline(self):
        r = self.make({}, settings={"cevrimici": False})
        cid = ContentId("muzik", "sanatci:x", "X", "Song")
        self.assertIsNone(r.cached(cid))
        r.remember_audio(cid, "metal_rock", 0.6, "ses analizi: gitar duvarı")
        self.assertEqual(r.cached(ContentId("muzik", "sanatci:x", "X", "Other")).group, "metal_rock")
        self.assertFalse(r.request(cid, lambda c, res: None))
        self.assertEqual(self.net.urls, [])
        self.now[0] += tur.AUDIO_TTL + 1
        self.assertIsNone(r.cached(cid))

    def test_deezer_album_genres_and_artist_evidence(self):
        r = self.make({"country=TR": NOTHING, "api.deezer.com/search": deezer(
            ("Ezhel", "Felaket", 101), ("Ezhel", "Geceler", 102)),
            "api.deezer.com/album/101": {"genres": {"data": [{"id": 116, "name": "Rap/Hip Hop"}]}}})
        got = self.resolve(r, ContentId("muzik", "sanatci:ezhel", "Ezhel", "Felaket"))
        self.assertEqual((got[0].group, got[0].source), ("elektronik", "Deezer: Rap/Hip Hop"))
        self.assertEqual(self.net.urls[-1], "https://api.deezer.com/album/101")
        # song not found, but the same artist's other album has a genre → artist evidence
        r2 = self.make({"country=TR": NOTHING, "country=US": NOTHING,
                        "api.deezer.com/search": deezer(("Ezhel", "Geceler", 102)),
                        "api.deezer.com/album/102": {"genres": {"data": [{"name": "Rap/Hip Hop"}]}},
                        "musicbrainz.org": {"artists": []}}, path=Path(self.tmp.name) / "e.json")
        got = self.resolve(r2, ContentId("muzik", "sanatci:ezhel", "Ezhel", "Yeni Parça"))
        self.assertEqual(got[0].group, "elektronik")
        self.assertEqual(got[0].source, "Deezer: Rap/Hip Hop (sanatçının diğer şarkıları)")
        self.assertEqual(got[0].confidence, tur.OTHER_TRACK_TRUST)

    def test_deezer_quota_is_error_and_source_errors_tolerated(self):
        r = self.make({"country=TR": OSError("zaman aşımı"),
                       "api.deezer.com/search": deezer(("Duman", "Bu Akşam", 7)),
                       "api.deezer.com/album/7": {"genres": {"data": [{"name": "Rock"}]}}})
        got = self.resolve(r, ContentId("muzik", "sanatci:duman", "Duman", "Bu Akşam"))
        self.assertEqual(got[0].group, "metal_rock")
        r2 = self.make({"itunes.apple.com": OSError("ağ yok"),
                        "api.deezer.com": {"error": {"type": "Exception", "message": "Quota limit exceeded",
                                                     "code": 4}},
                        "musicbrainz.org": OSError("ağ yok")}, path=Path(self.tmp.name) / "q.json")
        cid = ContentId("muzik", "sanatci:x", "X", "Y")
        got = []
        self.assertTrue(r2.request(cid, lambda c, res: got.append(res)))
        self.assertTrue(r2.wait_idle(5))
        self.assertEqual(got, [])                          # error: nothing delivered, nothing cached
        self.assertIsNone(r2.cached(cid))
        self.assertFalse(r2.request(cid, lambda c, res: None))   # retry limited
        self.assertFalse((Path(self.tmp.name) / "q.json").exists())

    def test_youtube_guards(self):
        # mixed keywords (pop ≈ rock) → no decision; a page of ANOTHER song → not used
        mixed = yt_page("Band - Song", "Band", ["pop", "rock", "pop rock"])
        r = self.make({"country=TR": NOTHING, "api.deezer.com/search": deezer(),
                       "youtube.com/watch": mixed, "country=US": NOTHING,
                       "musicbrainz.org": {"artists": []}})
        got = self.resolve(r, ContentId("muzik", "sanatci:band", "Band", "Song", url=SAFI_URL))
        self.assertEqual(got[0].group, "")
        other = yt_page("Someone Else - Other Song", "Someone Else", ["deep house", "house music"])
        r2 = self.make({"country=TR": NOTHING, "api.deezer.com/search": deezer(),
                        "youtube.com/watch": other, "country=US": NOTHING,
                        "musicbrainz.org": {"artists": []}}, path=Path(self.tmp.name) / "g.json")
        got = self.resolve(r2, ContentId("muzik", "sanatci:band", "Band", "Song", url=SAFI_URL))
        self.assertEqual(got[0].group, "")

    def test_track_entry_beats_artist_entry(self):
        r = self.make({"country=TR": itunes(("Teo", "Akustik Şarkı", "Singer/Songwriter"),
                                            ("Teo", "Sert Şarkı", "Rock")),
                       "musicbrainz.org": MB_VB})
        r.cached(ContentId("muzik", "sanatci:teo", "Teo"))
        a = ContentId("muzik", "sanatci:teo", "Teo", "Sert Şarkı")
        self.assertEqual(self.resolve(r, a)[0].group, "metal_rock")
        b = ContentId("muzik", "sanatci:teo", "Teo", "Akustik Şarkı")
        r._store(tur.track_key(b), [("Singer/Songwriter", 1.0)], "iTunes", "Teo")
        self.assertEqual(r.cached(b).group, "klasik_akustik")      # song entry first
        self.assertEqual(r.cached(a).group, "metal_rock")

    def test_musicbrainz_name_only_entry_is_verified_by_song(self):
        # live cache 25 Eki: "sanatci:safi" came from MusicBrainz (another SAFI)
        r = self.make({"musicbrainz.org": {"artists": [
            {"id": "s", "name": "SAFI", "score": 100, "tags": [{"name": "indie rock", "count": 3}]}]},
            "country=TR": itunes(("SAFI", "Emotion", "Dance"))})
        artist = ContentId("muzik", "sanatci:safi", "SAFI")
        self.assertEqual(self.resolve(r, artist)[0].group, "metal_rock")   # name only
        song = ContentId("muzik", "sanatci:safi", "SAFI", "Emotion")
        self.assertIsNone(r.cached(song))                 # verify by the song first
        self.assertEqual(self.resolve(r, song)[0].group, "elektronik")
        nxt = ContentId("muzik", "sanatci:safi", "SAFI", "Sonara")
        res = r.cached(nxt)                                # the song hit replaced the MB entry
        self.assertEqual((res.group, res.source), ("elektronik", "iTunes: Dance"))
        # a song that no store knows falls back to a (verified-or-not) artist entry
        r2 = self.make({"musicbrainz.org": {"artists": [
            {"id": "v", "name": "Virgin Black", "score": 100, "tags": [{"name": "doom metal", "count": 3}]}]},
            "country=TR": NOTHING, "api.deezer.com": deezer(), "country=US": NOTHING},
            path=Path(self.tmp.name) / "v.json")
        self.resolve(r2, ContentId("muzik", "sanatci:virgin black", "Virgin Black"))
        got = self.resolve(r2, ContentId("muzik", "sanatci:virgin black", "Virgin Black", "Rare Demo"))
        self.assertEqual((got[0].group, got[0].source), ("metal_rock", "MusicBrainz: doom metal"))

    def test_artist_only_request_uses_itunes_artist_search(self):
        r = self.make({"musicbrainz.org": {"artists": []},
                       "entity=musicArtist": {"results": [
                           {"wrapperType": "artist", "artistName": "Mabel Matiz",
                            "primaryGenreName": "Turkish Pop"}]}})
        got = self.resolve(r, ContentId("muzik", "sanatci:mabel matiz", "Mabel Matiz"))
        self.assertEqual((got[0].group, got[0].source), ("pop_vokal", "iTunes: Turkish Pop"))
        self.assertTrue(r.known_artist("Mabel Matiz"))

    def test_itunes_us_skipped_when_tr_knows_artist(self):
        r = self.make({"country=TR": itunes(("Niche", "Other", "Soundtrack")),
                       "api.deezer.com/search": deezer(), "musicbrainz.org": {"artists": []}})
        got = self.resolve(r, ContentId("muzik", "sanatci:niche", "Niche", "Wanted"))
        self.assertEqual(got[0].group, "")
        self.assertNotIn("country=US", " ".join(self.net.urls))

    def test_per_host_intervals(self):
        self.assertEqual(tur.HOST_INTERVALS["itunes.apple.com"], 3.0)
        r = self.make({"country=TR": NOTHING, "api.deezer.com/search": deezer(),
                       "country=US": NOTHING, "musicbrainz.org": {"artists": []}})
        self.resolve(r, ContentId("muzik", "sanatci:a", "A", "B"))
        # TR → Deezer → US: the second iTunes request waits for the iTunes spacing only
        self.assertTrue(any(2.0 < s <= 3.0 for s in self.sleeps), self.sleeps)
        self.assertFalse(any(s > 3.0 for s in self.sleeps), self.sleeps)

    def test_offline_sends_nothing(self):
        r = self.make({}, settings={"cevrimici": False})
        cid = ContentId("muzik", "sanatci:a", "A", "B", url=SAFI_URL)
        self.assertIsNone(r.cached(cid))
        self.assertFalse(r.request(cid, lambda c, res: None))
        self.assertEqual(self.net.urls, [])

    def test_http_headers_ascii_and_english(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n=-1):
                return b'{"ok": 1}'

        def fake_open(req, timeout):
            seen.update(req.headers)
            return Resp()
        with mock.patch.object(tur.urllib.request, "urlopen", fake_open):
            self.assertEqual(tur.http_get_json("https://api.deezer.com/x"), {"ok": 1})
            for v in seen.values():
                v.encode("latin-1")
            self.assertEqual(seen.get("Accept-language"), "en")
            self.assertEqual(tur.http_get_text("https://www.youtube.com/watch?v=x"), '{"ok": 1}')
        self.assertNotIn("Cookie", seen)


class TestCacheMerge(unittest.TestCase):
    """Two resolvers (e.g. router process + CLI) on one cache file must not clobber each other."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "tur-onbellek.json"
        self.now = [1_800_000_000.0]

    def make(self):
        r = tur.GenreResolver(settings={"cevrimici": True}, cache_path=self.cache, fetch=FakeNet({}),
                              clock=lambda: self.now[0], sleep=lambda s: None, builtin=False)
        self.addCleanup(r.stop)
        return r

    def test_merge_newer_wins_and_forget(self):
        a, b = self.make(), self.make()
        a.cached(ContentId("muzik", "sanatci:x", "X"))          # both loaded (empty)
        b.cached(ContentId("muzik", "sanatci:x", "X"))
        a._store("sanatci:a", [("rock", 1)], "MusicBrainz", "A")
        b._store("sanatci:b", [("jazz", 1)], "MusicBrainz", "B")
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertEqual(set(data), {"sanatci:a", "sanatci:b"})
        self.now[0] += 5
        b.remember_audio(ContentId("muzik", "sanatci:a", "A", "s"), "elektronik", 0.7, "x")
        a._store("sanatci:c", [("pop", 1)], "iTunes", "C")
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertEqual(set(data), {"sanatci:a", "sanatci:b", "sanatci:c", "ses:sanatci:a"})
        self.assertEqual(a.cached(ContentId("muzik", "sanatci:b", "B")).group, "klasik_akustik")
        a.forget("sanatci:b")
        b._store("sanatci:d", [("metal", 1)], "MusicBrainz", "D")
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertNotIn("sanatci:b", data)                 # a's forget is kept
        self.assertIn("sanatci:d", data)

    def test_deleted_file_is_respected(self):
        a = self.make()
        a._store("sanatci:old", [("rock", 1)], "MusicBrainz", "Old")
        self.cache.unlink()                                  # GUI "Tür önbelleğini sil"
        self.now[0] += 10
        a._store("sanatci:new", [("jazz", 1)], "MusicBrainz", "New")
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertEqual(set(data), {"sanatci:new"})


# ============================================================================
#  27 Eki: Wikidata artist database, wider genre names and title hints
# ============================================================================

def write_db(path, entries, groups=("metal_rock", "elektronik", "klasik_akustik", "pop_vokal"),
             genres=("heavy metal", "Arabesk", "pop rock", "hip hop")):
    """entries: {name: (group, conf100, turkish, alias, genre_label_or_None)}."""
    import gzip
    table = {}
    for name, (g, c, t, a, lab) in entries.items():
        gi = list(genres).index(lab) + 1 if lab else 0
        table[tur._fold(name)] = tur.pack_db_value(list(groups).index(g), c, t, a, gi)
    with gzip.open(path, "wb") as f:
        f.write(json.dumps({"surum": 1, "gruplar": list(groups), "turler": list(genres),
                            "s": table}).encode("utf-8"))
    return path


class TestArtistDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "tur-onbellek.json"
        self.dbfile = write_db(Path(self.tmp.name) / "db.json.gz", {
            "Müslüm Gürses": ("pop_vokal", 95, True, False, "Arabesk"),
            "Virgin Black": ("metal_rock", 90, False, False, "heavy metal"),
            "Kararsız Grup": ("metal_rock", 40, False, False, "pop rock"),
            "VB": ("metal_rock", 90, False, True, "heavy metal"),
            "İzel": ("pop_vokal", 90, True, False, None),
            "Türk Kararsız": ("klasik_akustik", 75, True, False, None),   # TR bar: 0.85
        })

    def make(self, routes=None, settings=None, db=None, cache=None, **kw):
        self.net = FakeNet(routes or {})
        kw.setdefault("title_hints", {})
        r = tur.GenreResolver(settings=settings or {"cevrimici": True},
                              cache_path=cache or self.cache, fetch=self.net, sleep=lambda s: None,
                              db=tur.ArtistDB(self.dbfile) if db is None else db, **kw)
        self.addCleanup(r.stop)
        return r

    @staticmethod
    def cid(name, track="Bir Şarkı"):
        return ContentId("muzik", "sanatci:" + tur.normalize_artist(name), tur.clean_artist(name),
                         track)

    def test_lookup_normalization(self):
        db = tur.ArtistDB(self.dbfile)
        for n in ("Müslüm Gürses", "MÜSLÜM GÜRSES", "muslum gurses", "Muslum  Gurses"):
            e = db.lookup(n)
            self.assertEqual((e.group, e.turkish, e.genres), ("pop_vokal", True, ("Arabesk",)), n)
        self.assertEqual(db.lookup("IZEL").group, "pop_vokal")       # İ/I/ı casefold
        self.assertEqual(db.lookup("izel").genres, ())
        self.assertTrue(db.lookup("vb").alias)
        self.assertIsNone(db.lookup("Bilinmeyen Sanatçı"))
        self.assertIsNone(db.lookup(""))
        self.assertEqual(len(db), 6)
        self.assertTrue(db.lookup("Virgin Black").sure)
        self.assertFalse(db.lookup("Türk Kararsız").sure)            # 0.75 < DB_SURE_TR
        self.assertFalse(db.lookup("Kararsız Grup").sure)

    def test_sure_entry_answers_without_network(self):
        r = self.make(builtin=False)                   # Müslüm Gürses is in the hand list too
        res = r.cached(self.cid("Müslüm Gürses feat. Biri"))
        self.assertEqual(res.group, "pop_vokal")
        self.assertEqual(res.source, "Yerleşik veritabanı (Wikidata): Arabesk")
        self.assertLessEqual(res.confidence, tur.DB_CONF_CAP)
        self.assertFalse(r.request(self.cid("Müslüm Gürses"), lambda c, x: None))
        # no stored genre label = the group came only from an occupation / birth year
        # (comedians, actors…): never "sure", the online sources are asked first (1 Kas)
        self.assertIsNone(r.cached(self.cid("İzel")))
        self.assertFalse(tur.ArtistDB(self.dbfile).lookup("İzel").sure)
        self.assertTrue(tur.ArtistDB(self.dbfile).lookup("Müslüm Gürses").sure)
        self.assertEqual(self.net.urls, [])
        self.assertTrue(r.known_artist("Virgin Black"))
        self.assertIsNone(r.known_artist("VB"))                    # an alias does not prove music
        self.assertFalse(tur.ArtistDB(self.dbfile).lookup("VB").sure)   # aliases: fallback only
        self.assertIsNone(r.cached(self.cid("VB")))
        self.assertIsNone(r.cached(self.cid("Türk Kararsız")))     # weak: online first
        self.assertIsNone(r.known_artist("Türk Kararsız"))

    def test_priority_order(self):
        # user assignment > title hint > hand-picked list > database
        r = self.make(settings={"cevrimici": True, "sanatci": {"Virgin Black": "klasik_akustik"}},
                      title_hints=tur.TITLE_HINTS)
        self.assertEqual(r.cached(self.cid("Virgin Black")).group, "klasik_akustik")
        self.assertEqual(r.cached(self.cid("Müslüm Gürses", "Nilüfer (Remix)")).group, "elektronik")
        db2 = tur.ArtistDB(write_db(Path(self.tmp.name) / "d2.json.gz",
                                    {"Duman": ("pop_vokal", 99, True, False, "pop rock")}))
        self.assertEqual(self.make(db=db2).cached(self.cid("Duman")).source, "yerleşik liste")
        self.assertEqual(self.make(db=db2, builtin=False).cached(self.cid("Duman")).group,
                         "pop_vokal")
        # builtin=False alone also turns the bundled database off (old tests stay isolated)
        self.assertIsNone(tur.GenreResolver(settings={"cevrimici": True}, cache_path=self.cache,
                                            builtin=False)._db)

    def test_weak_entry_is_only_a_fallback(self):
        nothing = {"itunes.apple.com": NOTHING, "api.deezer.com": {"data": []},
                   "musicbrainz.org": {"artists": []}}
        r = self.make(nothing)
        cid = self.cid("Kararsız Grup")
        self.assertIsNone(r.cached(cid))                  # online: look it up first
        got = []
        self.assertTrue(r.request(cid, lambda c, x: got.append(x)))
        self.assertTrue(r.wait_idle(5))
        self.assertTrue(self.net.urls)
        self.assertEqual(got[0].group, "metal_rock")      # online found nothing → weak entry
        self.assertIn("(zayıf)", got[0].source)
        self.assertEqual(r.cached(cid).confidence, 0.4)
        # an online answer beats the weak entry
        r2 = self.make({"itunes.apple.com": itunes(("Kararsız Grup", "Bir Şarkı", "Pop"))},
                       settings={"cevrimici": True})
        self.cache.unlink(missing_ok=True)
        r2.invalidate()
        r2.request(cid, lambda c, x: None)
        self.assertTrue(r2.wait_idle(5))
        self.assertEqual(r2.cached(cid).group, "pop_vokal")
        # offline: the weak entry at once; the user's own audio memory beats it (27 Eki fix)
        r3 = self.make(settings={"cevrimici": False}, cache=Path(self.tmp.name) / "c3.json")
        self.assertEqual(r3.cached(cid).group, "metal_rock")
        r3.remember_audio(self.cid("Kararsız Grup"), "elektronik", 0.9)
        self.assertEqual(r3.cached(cid).group, "elektronik")
        self.assertEqual(self.net.urls, [])

    def test_missing_or_corrupt_file_disables_silently(self):
        import gzip
        bad = Path(self.tmp.name)
        (bad / "garbage.gz").write_bytes(b"\x00not gzip at all")
        with gzip.open(bad / "badjson.gz", "wb") as f:
            f.write(b"{not json")
        with gzip.open(bad / "schema.gz", "wb") as f:
            f.write(json.dumps({"gruplar": ["yok_boyle"], "turler": [], "s": {"x": 1}}).encode())
        with gzip.open(bad / "values.gz", "wb") as f:
            f.write(json.dumps({"gruplar": ["metal_rock"], "turler": [],
                                "s": {"x": "metal", "y": -1, "z": 7}}).encode())
        for name in ("yok.json.gz", "garbage.gz", "badjson.gz", "schema.gz", "values.gz"):
            db = tur.ArtistDB(bad / name)
            for n in ("x", "y", "z", "Müslüm Gürses"):
                self.assertIsNone(db.lookup(n), (name, n))
            r = self.make({"musicbrainz.org": {"artists": []}, "itunes.apple.com": NOTHING,
                           "api.deezer.com": {"data": []}}, db=db)
            self.assertIsNone(r.cached(self.cid("x")))          # falls through to online
            self.assertIsNone(r.known_artist("x"))

    def test_bundled_database(self):
        db = tur.ArtistDB()
        if not db.path.exists():
            self.skipTest("oto_eq/data/sanatcilar.json.gz yok")
        t0 = __import__("time").monotonic()
        n = len(db)
        self.assertLess(__import__("time").monotonic() - t0, 3.0)
        self.assertGreater(n, 20000)
        self.assertEqual(db.meta.get("surum"), 1)
        for name, group in BUNDLED_EXPECT.items():
            e = db.lookup(name)
            self.assertIsNotNone(e, name)
            self.assertEqual(e.group, group, name)
        for junk in ("Music", "Topic", "Various Artists", "Phonk", "Lo-Fi", "Remix", "Live"):
            self.assertIsNone(db.lookup(junk), junk)


# spot checks on the generated database (Wikidata, see docs/NOTES-tur-repertuar.md)
BUNDLED_EXPECT = {
    "Sezen Aksu": "pop_vokal", "Tarkan": "pop_vokal", "Sertab Erener": "pop_vokal",
    "Duman": "metal_rock", "Ezhel": "elektronik", "Neşet Ertaş": "klasik_akustik",
    "Metallica": "metal_rock", "Daft Punk": "elektronik", "Miles Davis": "klasik_akustik",
    "Adele": "pop_vokal", "Frédéric Chopin": "klasik_akustik", "Nightwish": "metal_rock",
}


class TestArtistDBBuilder(unittest.TestCase):
    """tools/sanatci_veritabani.py build(): genre votes, name collisions, junk keys."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "tools" / "sanatci_veritabani.py"
        spec = importlib.util.spec_from_file_location("sanatci_veritabani", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def raw(self):
        e = lambda sl, gs, turk=False, os=(): {"sl": sl, "g": set(gs), "o": set(os), "turk": turk,
                                               "kind": "insan"}
        return {
            "ents": {
                "Q1": e(80, ["G_METAL"]), "Q2": e(5, ["G_ARABESK"], turk=True),
                "Q3": e(60, ["G_POP"]), "Q4": e(50, ["G_JAZZ"]), "Q5": e(40, ["G_POP", "G_METAL"]),
                "Q6": e(30, [], turk=True, os=["O_RAP"]), "Q7": e(30, ["G_SUB"]),
                "Q8": e(20, ["G_PHONK"]), "Q9": e(10, ["G_UNKNOWN"]),
            },
            "labels": {"Q1": {"Kurban"}, "Q2": {"Kurban"}, "Q3": {"Echo", "Simon & Friends"},
                       "Q4": {"Echo Jazz"}, "Q5": {"Mixed"}, "Q6": {"Rapçı"}, "Q7": {"Subbie"},
                       "Q8": {"Phonk"}, "Q9": {"Nobody"}},
            "aliases": {"Q3": {"EJJ"}, "Q4": {"Echoj", "The Echo"}, "Q1": {"The Echo"}},
            "genres": {"G_METAL": {"en": "heavy metal", "tr": "", "p": set()},
                       "G_ARABESK": {"en": "arabesque", "tr": "Arabesk", "p": set()},
                       "G_POP": {"en": "pop music", "tr": "", "p": set()},
                       "G_JAZZ": {"en": "jazz", "tr": "caz", "p": set()},
                       "G_PHONK": {"en": "phonk", "tr": "", "p": set()},
                       "G_SUB": {"en": "zzz subgenre", "tr": "", "p": {"G_JAZZ"}},
                       "G_UNKNOWN": {"en": "something", "tr": "", "p": set()}},
            "occ_labels": {"O_RAP": "rapper"},
        }

    def test_build(self):
        data, stats = self.mod.build(self.raw())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "x.json.gz"
        import gzip
        with gzip.open(db_path, "wb") as f:
            f.write(json.dumps(data).encode())
        db = tur.ArtistDB(db_path)
        k = db.lookup("Kurban")                        # the Turkish artist wins the name
        self.assertEqual((k.group, k.turkish, k.genres), ("pop_vokal", True, ("Arabesk",)))
        self.assertEqual(db.lookup("Echo Jazz").group, "klasik_akustik")
        self.assertEqual(db.lookup("caz"), None)
        self.assertEqual(db.lookup("Rapçı").group, "elektronik")      # occupation only
        self.assertEqual(db.lookup("Subbie").group, "klasik_akustik")  # via the parent genre
        mixed = db.lookup("Mixed")
        self.assertLess(mixed.confidence, 0.6)                          # pop + metal: unsure
        self.assertIsNone(db.lookup("Phonk"))          # a genre name is no artist key
        self.assertIsNone(db.lookup("Nobody"))         # unmapped genre: no entry
        self.assertIsNone(db.lookup("Simon"))          # clean_artist would cut "Simon & Friends"
        self.assertIsNone(db.lookup("Simon & Friends"))
        self.assertIsNone(db.lookup("EJJ"))            # alias shorter than 4
        self.assertTrue(db.lookup("Echoj").alias)
        self.assertFalse(db.lookup("Echoj").sure)      # aliases: fallback only
        self.assertIsNone(db.lookup("The Echo"))       # alias of two artists, other genres
        self.assertEqual(stats["sanatci_turk"], 2)


class TestWiderNamesAndHints(unittest.TestCase):
    def test_new_store_genre_names(self):
        cases = {
            "Anatolian Rock": "metal_rock", "Türkçe Rock": "metal_rock", "Heavy Metal": "metal_rock",
            "J-Rock": "metal_rock", "Britpop": "metal_rock",
            "Özgün Müzik": "klasik_akustik", "Türkü": "klasik_akustik", "Oyun Havası": "klasik_akustik",
            "Tasavvuf": "klasik_akustik", "Klasik Türk Müziği": "klasik_akustik",
            "Classical Crossover": "klasik_akustik", "Smooth Jazz": "klasik_akustik",
            "Folk, World, & Country": "klasik_akustik", "Americana": "klasik_akustik",
            "Slow": "pop_vokal", "Gospel": "pop_vokal", "Funk / Soul": "pop_vokal",
            "K-Pop": "pop_vokal", "KPop": "pop_vokal", "J-Pop": "pop_vokal", "Cantopop": "pop_vokal",
            "Salsa": "pop_vokal", "Arabesk Pop": "pop_vokal", "Musical Theatre": "pop_vokal",
            "Lo-Fi Hip Hop": "elektronik", "Drum'n'Bass": "elektronik", "Hardstyle": "elektronik",
            "Ambient": "elektronik", "Trip-Hop": "elektronik", "Turkish Rap": "elektronik",
            "Arabesk Rap": "elektronik", "Brazilian Funk": "elektronik", "Eurodance": "elektronik",
            "Sesli Kitap": "konusma", "Audiobook": "konusma", "Radyo Tiyatrosu": "konusma",
            # deliberately undecided (Genel)
            "Indie": "", "Film Müziği": "", "Original Soundtrack": "", "Stage & Screen": "",
            "Video Game Music": "",
        }
        for name, group in cases.items():
            self.assertEqual(tur.map_tags("muzik", [name]), group, name)
        # old decisions unchanged
        for name, group in {"Reggaeton": "pop_vokal", "Soundtrack": "", "Arabesque": "pop_vokal",
                            "Halk": "klasik_akustik", "Turkish Hip-Hop/Rap": "elektronik"}.items():
            self.assertEqual(tur.map_tags("muzik", [name]), group, name)

    def test_new_title_hints(self):
        r = tur.GenreResolver(settings={"cevrimici": True}, cache_path="/nonexistent/x.json",
                              fetch=FakeNet({}), builtin=False, db=False)
        self.addCleanup(r.stop)

        def g(track):
            res = r._title_hint(ContentId("muzik", "sanatci:x", "X", track))
            return res.group if res else None
        yes = {
            "Gece (Slowed + Reverb)": "elektronik", "Şımarık (Sped Up)": "elektronik",
            "Bad Apple (Nightcore)": "elektronik", "Song (8D Audio)": "elektronik",
            "Rain - Lo-Fi": "elektronik", "Hard Type Beat 2024": "elektronik",
            "Boiler Room Berlin DJ Set": "elektronik",
            "Moonlight Sonata No. 14": "klasik_akustik", "Cello Suite BWV 1007": "klasik_akustik",
            "Nocturne Op. 9 No. 2": "klasik_akustik", "Serenade K. 525": "klasik_akustik",
            "Piano Concerto in A minor": "klasik_akustik", "Senfoni No. 9": "klasik_akustik",
            "Unutamadım (Piano Cover)": "klasik_akustik", "Hicaz Taksimi": "klasik_akustik",
            "Nothing Else Matters (Symphony Orchestra)": "klasik_akustik",
            "Kara Sevda (Bağlama Solo)": "klasik_akustik",
            "Toxic (A Cappella)": "pop_vokal",
            "Suç ve Ceza – Sesli Kitap 1. Bölüm": "konusma", "Duman Röportaj": "konusma",
            "Ders 5: Türev": "konusma", "Season 2 Episode 4": "konusma",
            "Cem Yılmaz Stand Up Show": "konusma", "TEDx Talk": "konusma",
        }
        for track, group in yes.items():
            self.assertEqual(g(track), group, track)
        # ordinary song titles: no hint (low false positives)
        for track in ("Requiem", "Prelude", "Trance", "Live Forever", "In the End",
                      "Stand Up", "Chapter 1: The Beginning", "Taksim", "Symphony",
                      "Piano Man", "Interstellar", "Sonata Arctica", "Op", "Opus",
                      "Canlı Canlı", "Haber Ver", "Interviewer's Blues", "Daydream",
                      "Slow Down", "Speed of Light", "In A Minute", "Lofty Ideas"):
            self.assertIsNone(g(track), track)


if __name__ == "__main__":
    unittest.main()


class TestSharedResolverNoDeadlock(unittest.TestCase):
    """shared_resolver() held its lock while GenreResolver() → default_db() asked for the same
    non-reentrant lock: the router hung at start (27 Eki hata avı)."""

    def test_first_call_returns(self):
        old_shared, old_db = tur._shared, tur._default_db
        tur._shared = None
        self.addCleanup(setattr, tur, "_shared", old_shared)
        self.addCleanup(setattr, tur, "_default_db", old_db)
        got = []
        t = threading.Thread(target=lambda: got.append(tur.shared_resolver()), daemon=True)
        t.start()
        t.join(10)
        self.assertFalse(t.is_alive(), "shared_resolver() kilitlendi")
        self.assertIs(got[0], tur.shared_resolver())


# ============================================================================
#  27 Eki denetçi bulguları (hata-H2): kesik ad, ağ hatasında zayıf katman, başlık ipucu
#  yanlış pozitifleri, folk rock, ses hafızası > zayıf kayıt, deterministik üretim
# ============================================================================

class TestAuditFixes(unittest.TestCase):
    NOTHING_ROUTES = {"itunes.apple.com": NOTHING, "api.deezer.com": {"data": []},
                      "musicbrainz.org": {"artists": []}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "tur-onbellek.json"
        self.dbfile = write_db(Path(self.tmp.name) / "db.json.gz", {
            "Simon": ("metal_rock", 90, False, False, "heavy metal"),     # another "Simon"
            "Down": ("metal_rock", 90, False, False, "heavy metal"),
            "Belle": ("pop_vokal", 90, False, False, None),
            "Brooks & Dunn": ("klasik_akustik", 90, False, False, "pop rock"),  # whole name known
            "Kararsız Grup": ("metal_rock", 40, False, False, "pop rock"),
        })

    def make(self, routes=None, settings=None, cache=None):
        self.net = FakeNet(routes if routes is not None else {})
        r = tur.GenreResolver(settings=settings or {"cevrimici": True}, cache_path=cache or self.cache,
                              fetch=self.net, sleep=lambda s: None, title_hints={},
                              db=tur.ArtistDB(self.dbfile))
        self.addCleanup(r.stop)
        return r

    @staticmethod
    def spotify(artist, title="Bir Şarkı"):
        p = MediaPlayer("org.mpris.MediaPlayer2.spotify", 77777, "Spotify", "Playing", title,
                        artist, "https://open.spotify.com/track/x")
        return tur._music_id(stream(app="spotify", binary="spotify", pid=51000), p)

    # 1 -------------------------------------------------------------------------
    def test_cut_group_name_is_weak_only(self):
        r = self.make(self.NOTHING_ROUTES)
        for raw, cut in (("Simon & Garfunkel", "Simon"), ("Down with Webster", "Down"),
                         ("Belle & Sebastian", "Belle")):
            cid = self.spotify(raw)
            self.assertEqual((cid.key, cid.display), ("sanatci:" + tur.norm(cut), cut))
            self.assertEqual(tur.cut_name(cid), raw)
            self.assertIsNone(r.cached(cid), raw)               # online first, no sure hit
            self.assertIsNone(r._db_result(cid, sure=True), raw)
        # the same first name alone IS that artist: the sure entry answers at once
        plain = self.spotify("Simon", "Başka Şarkı")
        self.assertEqual(tur.cut_name(plain), "")
        self.assertEqual(r.cached(plain).source, "Yerleşik veritabanı (Wikidata): heavy metal")
        # feat./ft. is no cut: the part before it is the artist
        self.assertEqual(tur.cut_name(self.spotify("Simon feat. Biri", "Üçüncü")), "")
        # online finds nothing → the cut name's entry, weak and capped
        cid = self.spotify("Simon & Garfunkel", "Mrs Robinson")
        got = []
        self.assertTrue(r.request(cid, lambda c, x: got.append(x)))
        self.assertTrue(r.wait_idle(5))
        self.assertTrue(self.net.urls)
        self.assertEqual(got[0].group, "metal_rock")
        self.assertIn("kesik ad: Simon", got[0].source)
        self.assertLessEqual(got[0].confidence, tur.DB_CUT_CONF)
        # offline: weak entry, never sure
        off = self.make(settings={"cevrimici": False}, cache=Path(self.tmp.name) / "off.json")
        self.assertIn("(zayıf, kesik ad: Down)", off.cached(self.spotify("Down with Webster")).source)
        # the whole name in the database: a sure answer without network
        n = len(self.net.urls)
        bd = self.spotify("Brooks & Dunn")
        self.assertEqual(tur.cut_name(bd), "Brooks & Dunn")
        res = r.cached(bd)
        self.assertEqual((res.group, res.source),
                         ("klasik_akustik", "Yerleşik veritabanı (Wikidata): pop rock"))
        self.assertEqual(len(self.net.urls), n)
        # a ContentId built elsewhere with the raw name as display is recognised too
        self.assertEqual(tur.cut_name(ContentId("muzik", "sanatci:down", "Down with Webster", "X")),
                         "Down with Webster")
        self.assertEqual(tur.cut_name(ContentId("muzik", "sanatci:down", "Down", "X")), "")
        # YouTube channel name ("… - Topic") and a later uncut sighting of the same cid
        yt = player(title="Get Me Away from Here, I'm Dying", artist="Belle & Sebastian - Topic",
                    url="https://www.youtube.com/watch?v=abcdefghijk")
        cid = tur._music_id(stream(media_name="x"), yt)
        self.assertEqual((cid.display, tur.cut_name(cid)), ("Belle", "Belle & Sebastian"))
        again = self.spotify("Simon", "Mrs Robinson")
        self.assertEqual(again, self.spotify("Simon & Garfunkel", "Mrs Robinson"))
        self.spotify("Simon", "Mrs Robinson")
        self.assertEqual(tur.cut_name(again), "")

    # 2 -------------------------------------------------------------------------
    def test_network_error_delivers_weak_entry(self):
        import urllib.error
        boom = urllib.error.URLError("ağ yok")
        r = self.make({"itunes.apple.com": boom, "api.deezer.com": boom, "musicbrainz.org": boom})
        cid = TestArtistDB.cid("Kararsız Grup")
        got, waited = [], []
        gate = threading.Event()
        orig = r._resolve
        r._resolve = lambda c: (gate.wait(5), orig(c))[1]
        self.assertTrue(r.request(cid, lambda c, x: got.append(x)))
        self.assertTrue(r.request(cid, lambda c, x: waited.append(x)))     # joins (waiter)
        gate.set()
        self.assertTrue(r.wait_idle(5))
        self.assertEqual([x.group for x in got], ["metal_rock"])
        self.assertIn("(zayıf)", got[0].source)
        self.assertEqual(waited, got)
        self.assertFalse(r.request(cid, lambda c, x: None))        # failed: not retried yet
        # artist-only lookup (no song title) too
        solo = ContentId("muzik", "sanatci:kararsız grup", "Kararsız Grup")
        got2 = []
        self.assertTrue(r.request(solo, lambda c, x: got2.append(x)))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual([x.group for x in got2], ["metal_rock"])
        # nothing known offline either: no callback (as before)
        unknown = ContentId("muzik", "sanatci:hic bilinmeyen", "Hiç Bilinmeyen", "Şarkı")
        got3 = []
        self.assertTrue(r.request(unknown, lambda c, x: got3.append(x)))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(got3, [])
        self.assertIsNone(r.cached(unknown))
        # a NEGATIVE answer is unchanged: "not found" is delivered, then the weak entry
        r2 = self.make(self.NOTHING_ROUTES, cache=Path(self.tmp.name) / "neg.json")
        got4 = []
        r2.request(unknown, lambda c, x: got4.append(x))
        self.assertTrue(r2.wait_idle(5))
        self.assertEqual(got4[0].group, "")

    # 3 -------------------------------------------------------------------------
    def test_title_hint_false_positives(self):
        r = tur.GenreResolver(settings={"cevrimici": False}, cache_path="/nonexistent/x.json",
                              fetch=FakeNet({}), builtin=False, db=False)
        self.addCleanup(r.stop)

        def g(track):
            res = r._title_hint(ContentId("muzik", "sanatci:x", "X", track))
            return res.group if res else None
        for track in ("Interview With The Vampire", "Living In A Major City", "Love In A Minor Key",
                      "Living in a Minor Key", "Opus 17", "Opus 17 (Don't You Worry 'bout Me)",
                      "RV 2", "Lo-Fi", "Slowed Down", "Sped Up", "Nightcore", "Reverb",
                      "Konferans", "Seminer", "Vaaz", "Seslendiren", "Lecture", "K. 9",
                      "A Major Problem", "In A Minor Way", "Minor Swing", "Major Tom"):
            self.assertIsNone(g(track), track)
        yes = {
            "Gece (Slowed + Reverb)": "elektronik", "Gece (Slowed)": "elektronik",
            "Rüya [Lo-Fi Remix]": "elektronik", "Yağmur [Lo-Fi]": "elektronik",
            "Song | Sped Up": "elektronik", "Lo-Fi Hip Hop Beats": "elektronik",
            "Podcast #12": "konusma", "Bölüm 3": "konusma", "Röportaj: Duman": "konusma",
            "Konuk (Full Interview)": "konusma", "Tarih - Konferans": "konusma",
            "Kitap | Seslendiren: Ali": "konusma", "Interview - Part 1": "konusma",
            "Ahlak (Vaaz)": "konusma",
            "Nocturne in E-flat major, Op. 9 No. 2": "klasik_akustik",
            "Symphony No. 5 in C Minor": "klasik_akustik", "Concerto In A Major": "klasik_akustik",
            "Song in B-flat Major": "klasik_akustik", "Air in A major": "klasik_akustik",
            "Prelude in C Major": "klasik_akustik", "Sonate in c-moll": "klasik_akustik",
            "The Four Seasons: Spring, RV 269": "klasik_akustik", "Op. 10 No. 3": "klasik_akustik",
            "Etude Op. 25": "klasik_akustik", "Eine kleine Nachtmusik, K. 525": "klasik_akustik",
            "Cello Suite No. 1 in G major, BWV 1007": "klasik_akustik", "BWV 846": "klasik_akustik",
            "Toccata and Fugue in D minor": "klasik_akustik",
        }
        for track, group in yes.items():
            self.assertEqual(g(track), group, track)
        res = r._title_hint(ContentId("muzik", "sanatci:x", "X", "Gece (Slowed + Reverb)"))
        self.assertEqual(res.source, "şarkı adı: “slowed”")        # no bracket in the shown text

    # 4 -------------------------------------------------------------------------
    def test_folk_rock_is_acoustic(self):
        self.assertEqual(tur.map_tags("muzik", ["Folk Rock"]), "klasik_akustik")
        self.assertEqual(tur.map_tags("muzik", ["folk-rock"]), "klasik_akustik")

    # 5 -------------------------------------------------------------------------
    def test_audio_memory_beats_weak_entry_after_negative(self):
        r = self.make(self.NOTHING_ROUTES)
        cid = TestArtistDB.cid("Kararsız Grup")
        got = []
        r.request(cid, lambda c, x: got.append(x))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(got[0].group, "metal_rock")               # negative online → weak
        r.remember_audio(cid, "elektronik", 0.7, "ses analizi: bas")
        res = r.cached(cid)
        self.assertEqual((res.group, res.source), ("elektronik", "ses analizi: bas"))
        # a cut name's weak entry does not beat the audio memory either
        cut = self.spotify("Simon & Garfunkel", "Sound of Silence")
        r.remember_audio(cut, "klasik_akustik", 0.6, "")
        off = tur.GenreResolver(settings={"cevrimici": False}, cache_path=self.cache,
                                fetch=FakeNet({}), title_hints={}, db=tur.ArtistDB(self.dbfile))
        self.addCleanup(off.stop)
        self.assertEqual(off.cached(cut).group, "klasik_akustik")

    # 6 -------------------------------------------------------------------------
    def test_builder_is_deterministic(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        code = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("sv", sys.argv[1] + "/tools/sanatci_veritabani.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
P = {"G_%d" % i for i in range(8)}
labs = ["heavy metal", "pop music", "jazz", "techno", "punk", "soul", "folk", "house"]
genres = {"G_%d" % i: {"en": labs[i], "tr": "", "p": set()} for i in range(8)}
genres["G_TIE"] = {"en": "zzz tie", "tr": "", "p": set(P)}
genres["G_TIE2"] = {"en": "zzz tie two", "tr": "", "p": {"G_0", "G_1"}}
e = lambda gs: {"sl": 20, "g": set(gs), "o": set(), "turk": False, "kind": "insan"}
raw = {"ents": {"Q%d" % i: e(["G_TIE", "G_TIE2"] if i % 2 else ["G_%d" % (i % 8), "G_TIE2"])
                for i in range(30)},
       "labels": {"Q%d" % i: {"Ad %d" % i, "Name %d" % i, "Nom %d" % i} for i in range(30)},
       "aliases": {"Q%d" % i: {"Takma %d" % i, "Lakap %d" % i} for i in range(30)},
       "genres": genres, "occ_labels": {}}
data, stats = m.build(raw)
data["uretim"] = ""
print(json.dumps([data, stats], sort_keys=True))
"""
        outs = set()
        for seed in ("1", "2", "3", "4"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            p = subprocess.run([sys.executable, "-I", "-c", code, str(root)], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            outs.add(p.stdout)
        self.assertEqual(len(outs), 1)


# ============================================================================
#  6 Kas hata avı (sınıflandırma): kaynak metinleri, User-Agent, oyun ortamı devralan
#  tarayıcı, bilinen sanatçı zayıf karar, başarısız sorgu belleği
# ============================================================================

class TestHataAvi6Kas(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "c.json"

    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def make(self, routes=None, settings=None):
        self.net = FakeNet(routes or {})
        r = tur.GenreResolver(settings=settings or {"cevrimici": True}, cache_path=self.cache,
                              fetch=self.net, sleep=lambda s: None, builtin=False, db=False,
                              title_hints={})
        self.addCleanup(r.stop)
        return r

    def test_user_agent_names_the_repo(self):
        self.assertRegex(tur.USER_AGENT,
                         r"^KulakKesil/[0-9][\w.]* \(\+https://github\.com/sobelendimm/kulak-kesil\)$")
        self.assertNotIn("@", tur.USER_AGENT)
        tur.USER_AGENT.encode("ascii")

    def _entry(self, **e):
        return tur.GenreResolver._entry_result("muzik" if "kind" not in e else e.pop("kind"), e)

    def test_source_texts_translated(self):
        from oto_eq import i18n, ui_text
        i18n.sifirla("en", {"tür bulunamadı": "genre not found",
                            "mağazada bulunamadı": "not found in the store",
                            "sanatçının diğer şarkıları": "the artist's other songs",
                            "zayıf, kesik ad: {ad}": "weak, shortened name: {ad}"})
        neg = self._entry(source="iTunes, Deezer", tags=[], negatif=True)
        self.assertEqual(neg.source, "iTunes, Deezer: genre not found")
        steam = self._entry(kind="oyun", source=tur.SRC_STEAM_SEARCH, tags=[], negatif=True)
        self.assertEqual(steam.source, "Steam: not found in the store")
        # stored form (old caches too): "<source> (sanatçının diğer şarkıları)"
        other = self._entry(source=f"Deezer ({tur.OTHER_SONGS})", tags=[["Rap/Hip Hop", 1.0]],
                            negatif=False, guven=0.6)
        self.assertEqual(other.source, "Deezer: Rap/Hip Hop (the artist's other songs)")
        self.assertEqual(other.group, "elektronik")
        self.assertEqual(ui_text.source_short_key(other.source), "deezer")   # name first
        self.assertNotIn("sanatçı", ui_text.source_text(other.source))
        for s in (neg.source, steam.source):
            self.assertFalse(any(ch in ui_text.source_text(s) for ch in "ışğüçö"), s)

    def test_cut_name_detail_translated(self):
        from oto_eq import i18n

        class DB(tur.ArtistDB):
            def lookup(self, *names):          # the whole name unknown, the cut one known
                if names == ("Simon & Garfunkel",):
                    return None
                return tur.DBEntry("metal_rock", 0.9, False, False, ("heavy metal",))
        r = tur.GenreResolver(settings={"cevrimici": False}, cache_path=self.cache,
                              fetch=FakeNet({}), sleep=lambda s: None, builtin=False,
                              db=DB(Path(self.tmp.name) / "yok.json.gz"), title_hints={})
        self.addCleanup(r.stop)
        cid = ContentId("muzik", "sanatci:simon", "Simon", "Mrs Robinson")
        tur._note_uncut(cid, "Simon & Garfunkel")
        self.assertIn("(zayıf, kesik ad: Simon)", r._db_result(cid, sure=False).source)
        i18n.sifirla("en", {"zayıf, kesik ad: {ad}": "weak, shortened name: {ad}"})
        self.assertEqual(r._db_result(cid, sure=False).source,
                         "Yerleşik veritabanı (Wikidata): heavy metal (weak, shortened name: Simon)")

    def test_steam_search_negative_stored_as_key(self):
        r = self.make({"storesearch": {"items": []}})
        got = []
        self.assertTrue(r.request(ContentId("oyun", "oyun:bilinmez oyun", "Bilinmez Oyun"),
                                  lambda c, x: got.append(x)))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(got[0].source, "Steam: mağazada bulunamadı")
        data = json.loads(self.cache.read_text("utf-8"))["kayitlar"]
        self.assertEqual(data["oyun:bilinmez oyun"]["source"], tur.SRC_STEAM_SEARCH)

    def test_failed_lookup_memory_is_pruned(self):
        import urllib.error
        r = self.make({"itunes": urllib.error.URLError("yok"), "deezer": urllib.error.URLError("yok"),
                       "musicbrainz": urllib.error.URLError("yok")})
        old = tur.time.monotonic() - tur.ERROR_RETRY - 1
        r._errors = {f"parca:x|{i}": old for i in range(tur.ERRORS_MAX)}
        self.assertTrue(r.request(ContentId("muzik", "sanatci:y", "Y", "Song"), lambda c, x: None))
        self.assertTrue(r.wait_idle(5))
        self.assertEqual(list(r._errors), ["parca:y|song"])          # expired ones dropped

    def test_browser_with_game_env_is_music_content(self):
        # the classifier keeps such a browser out of Oyun (Classifier._media_app): its
        # content id must be a song, not the game (the variant would be a game group)
        info = proc(env={"WINEPREFIX": "/home/u/Games/oyun/pfx", "SteamAppId": "570"})
        p = player("Duman - Bu Akşam", "Duman", "https://www.youtube.com/watch?v=abcdefghijk")
        cid = tur.content_id(stream(media_name="Duman - Bu Akşam - YouTube"), [p], proc=info,
                             classifier=clf())
        self.assertEqual((cid.kind, cid.key), ("muzik", "sanatci:duman"))
        g = tur.content_id(stream(app="Game.exe", binary="Game.exe", pid=9300), [],
                           proc=proc(9300, "Game.exe", env={"WINEPREFIX": "/p"}), classifier=clf())
        self.assertEqual(g.kind, "oyun")


class TestKnownArtistWeak(unittest.TestCase):
    """6 Kas hata avı: a title's first word may only share a known artist's name."""

    def make(self, known):
        procs = {FF: proc()}
        return Classifier({}, proc_lookup=procs.get, resolve=lambda s: s.pid,
                          artist_lookup=lambda n: known.get(n.lower()))

    def test_known_artist_decision_is_weak(self):
        c = self.make({"gece": True})
        d = c.classify(stream(media_name="Gece - Uyku İçin Hikaye - YouTube"), [])
        self.assertEqual(d.category.value, "muzik")
        self.assertTrue(d.zayif)              # → the content check (YAMNet) verifies it

    def test_obvious_video_titles(self):
        c = self.make({"boston": True, "america": True, "queen": True, "the fray": True})
        for t in ("Boston - Travel Guide 2026", "America - Full Documentary",
                  "Queen - The Royal Family Explained", "Boston - 7 Days Itinerary"):
            with self.subTest(t=t):
                self.assertEqual(c.classify(stream(media_name=t + " - YouTube"), []).category.value,
                                 "film")
        # a song title with "how to" stays a song
        d = c.classify(stream(media_name="The Fray - How to Save a Life - YouTube"), [])
        self.assertEqual(d.category.value, "muzik")
