"""Genre / game-type detection for the variant sub-profiles (CONTRACT CHANGE 3 + 4).

Sync, network-free:
    content_id(stream, players, proc=None) -> ContentId | None
    GenreResolver.cached(cid) -> GenreResult | None
Background (one worker thread, per-host rate limits, offline-safe):
    GenreResolver.request(cid, callback)   # callback(cid, GenreResult) on the worker thread
Router (CHANGE 4):
    GenreResolver.remember_audio(cid, group, confidence, detail)   # artist-level audio guess

Music, track level first (CHANGE 4): iTunes Search (TR store, US as a fallback),
Deezer (track search → album genres), the YouTube watch page (uploader keywords,
only for YouTube URLs: just the video id is sent), then the artist level:
MusicBrainz artist search (+ lookup). Games: SteamSpy ``appdetails`` (user tags),
Steam store ``appdetails`` (genres) and ``storesearch`` (title → appid).
Only the artist + song title (+ YouTube video id) or the game title/appid leave
the machine, and only when ``cevrimici`` is on. See docs/NOTES-tur-cevrimici.md.

Key formats (ContentId.key / cache keys):
    "sanatci:<normalized artist>"   music (artist entry)
    "parca:<artist>|<norm track>"   cache only: one song (track entry)
    "ses:sanatci:<artist>"          cache only: audio-analysis memory for the artist
    "steam:<appid>"                 Steam game (env SteamAppId/SteamGameId/STEAM_COMPAT_APP_ID
                                    or an ancestor's ``AppId=N``)
    "oyun:<normalized title>"       Faugus / Lutris game (title from their config, read-only)
    "exe:<normalized exe stem>"     anything else
The tag → group tables are at the top of this file (TAG_TABLES, GENRE_NAMES), on purpose
in one place.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config, procinfo
from .models import (GAME_GROUPS, MUSIC_GROUPS, ContentId, GenreResult, MediaPlayer,
                     ProcInfo, Stream)

log = logging.getLogger("oto_eq.tur")

# ASCII only: urllib writes headers as latin-1 ("ş"/"ı" raise UnicodeEncodeError).
USER_AGENT = "KulakKesil/0.1 (kisisel kullanim)"
HTTP_TIMEOUT = 8.0
MIN_INTERVAL = 1.1            # seconds between two requests to the same host (default)
HOST_INTERVALS: dict[str, float] = {    # per-host minimum spacing (documented limits / courtesy)
    "musicbrainz.org": 1.1,             # 1 req/s per IP, 503 when exceeded
    "itunes.apple.com": 3.0,            # "approximately 20 calls per minute"
    "api.deezer.com": 0.3,              # 50 requests / 5 s
    "www.youtube.com": 2.0,             # an HTML page (~1.4 MB): be gentle
    "steamspy.com": 1.1,
    "store.steampowered.com": 1.5,
}
POSITIVE_TTL = 90 * 86400
NEGATIVE_TTL = 7 * 86400
AUDIO_TTL = 30 * 86400        # remember_audio entries
YOUTUBE_MAX_BYTES = 2_500_000 # the keywords sit ~0.8 MB into the watch page
ERROR_RETRY = 600.0           # after a network error, do not retry the same key for 10 min
BUSY_RETRIES = (2.5, 5.0)     # HTTP 503/429 (MusicBrainz "busy"): transient, wait and retry
MIN_SCORE = 1.0               # map_tags: below this the result is "" (unknown)

# ============================================================================
#  TAG → GROUP TABLES  (edit here; A1 refinements go here)
#  Each entry: (regex searched in the lower-cased tag, weight). A tag scores for
#  every group whose patterns match; the group with the highest total wins.
#  Decisions for Turkish genres (documented in docs/NOTES-detect.md):
#   arabesk, fantezi, türk pop        → pop_vokal (vocal-centric, strings behind)
#   türk halk müziği, türkü, THM, TSM,
#   türk sanat müziği, fasıl, tasavvuf → klasik_akustik (acoustic instruments)
#   özgün müzik                         → klasik_akustik (acoustic singer-songwriter; 0.5 pop)
#   anadolu rock / anatolian rock       → metal_rock
#   türkçe rap / turkish hip hop        → elektronik (the "Elektronik / Hip-hop" group)
# ============================================================================
TAG_TABLES: dict[str, dict[str, list[tuple[str, float]]]] = {
    "muzik": {
        "metal_rock": [
            (r"\bmetal", 2.0), (r"\brock\b", 1.5), (r"punk", 1.5), (r"grunge", 1.5),
            (r"hardcore", 1.2), (r"\bemo\b", 1.0), (r"gothic", 1.2), (r"\bdoom", 1.5),
            (r"thrash", 2.0), (r"metalcore|deathcore|djent", 2.0), (r"shoegaze|post-rock", 1.0),
            (r"alternative", 0.6), (r"indie rock", 1.5), (r"progressive", 0.8),
            (r"anadolu rock|anatolian rock|türk rock|turkish rock", 2.5), (r"stoner|sludge", 1.5),
        ],
        "elektronik": [
            (r"electronic|electronica|\bedm\b", 2.0), (r"\bhouse\b", 1.8), (r"techno", 2.0),
            (r"trance", 2.0), (r"dubstep|drum and bass|drum & bass|\bdnb\b|jungle", 2.0),
            (r"synthwave|retrowave|outrun", 2.0), (r"synth-?pop", 0.8), (r"\belectro\b", 1.5),
            (r"\bidm\b|breakbeat|uk garage|\bgarage\b|bass music", 1.5), (r"\bdance\b", 0.8),
            (r"hip[ -]?hop|\brap\b|trap\b|grime|drill\b", 2.0), (r"phonk", 2.0),
            (r"lo-?fi", 1.5), (r"\bambient\b", 0.8), (r"downtempo|trip[ -]hop|chillout", 1.2),
            (r"türkçe rap|turkish hip hop|turkish rap", 2.5), (r"french house|disco house", 2.0),
        ],
        "klasik_akustik": [
            (r"classical|klasik", 2.0), (r"baroque|romantic era|romanticism", 2.0),
            (r"orchestra|symphon|chamber|string quartet", 1.8), (r"\bopera\b", 1.5),
            (r"piano|neo-?classical|contemporary classical|minimalism|modern classical", 1.8),
            (r"soundtrack|film score|\bscore\b", 1.0), (r"\bjazz", 2.0), (r"\bblues", 1.5),
            (r"\bfolk\b", 1.5), (r"acoustic|akustik", 1.5), (r"singer-songwriter", 0.8),
            (r"country|bluegrass", 1.5), (r"bossa nova|flamenco|fado|tango", 1.8),
            (r"new age", 1.2), (r"world music", 1.0),
            (r"türk halk|turkish folk|türkü|\bthm\b", 2.5),
            (r"türk sanat|turkish classical|ottoman|\btsm\b|fasıl|tasavvuf|sufi", 2.5),
            (r"özgün", 1.5),
        ],
        "pop_vokal": [
            (r"\bpop\b", 1.5), (r"dance-?pop|electropop|teen pop", 1.2),
            (r"turkish pop|türk pop|türkçe pop", 2.5), (r"arabesk|arabesque", 2.5),
            (r"fantezi", 2.5), (r"r&b|rnb|rhythm and blues|\bsoul\b|neo soul", 1.5),
            (r"\bfunk\b", 1.0), (r"k-?pop|j-?pop|c-?pop|latin pop", 2.0), (r"ballad", 1.2),
            (r"chanson|schlager|eurovision", 1.5), (r"\bvocal", 0.8), (r"reggaeton", 1.0),
            (r"özgün", 0.5), (r"singer-songwriter", 0.6),
        ],
        "konusma": [
            (r"podcast", 3.0), (r"spoken word", 3.0), (r"audiobook|audio drama|radio drama", 3.0),
            (r"comedy|stand-?up", 2.0), (r"interview|lecture|sermon|talk show", 2.5),
        ],
    },
    "oyun": {
        "rekabetci": [
            (r"\bfps\b", 2.5), (r"shooter", 1.5), (r"competitive", 2.0), (r"\bpvp\b", 1.5),
            (r"e-?sports", 2.0), (r"tactical", 1.2), (r"battle royale", 2.0),
            (r"hero shooter|arena shooter", 2.0), (r"\bmoba\b", 2.0), (r"fighting", 1.5),
            (r"first-person", 0.6), (r"team-based", 1.0),
        ],
        "korku_hayatta_kalma": [
            (r"horror", 2.5), (r"\bsurvival\b", 2.0), (r"zombie", 2.0), (r"psychological", 1.0),
            (r"thriller", 1.0), (r"lovecraft", 1.5), (r"\bgore\b", 1.0), (r"\bdark\b", 0.5),
            (r"open world survival craft", 2.5), (r"post-apocalyptic", 1.0),
            (r"atmospheric", 0.4), (r"crafting", 0.6),
        ],
        "aksiyon_rpg": [
            (r"\brpg\b|role-?playing", 2.0), (r"action rpg|\barpg\b|action-rpg", 2.5),
            (r"souls-?like", 2.5), (r"hack and slash", 2.0), (r"\baction\b", 0.8),
            (r"adventure", 0.8), (r"open world", 0.8), (r"story rich", 0.8),
            (r"fantasy", 0.8), (r"jrpg", 2.0), (r"metroidvania", 2.0), (r"looter", 1.5),
            (r"dungeon crawler", 1.8), (r"roguelike|roguelite", 1.2), (r"third person", 0.6),
            (r"\bloot\b", 1.0), (r"platformer", 1.2),
        ],
        "strateji_insa": [
            (r"strategy", 2.0), (r"\brts\b|real-time strategy|real time strategy", 2.5),
            (r"turn-based", 1.5), (r"\b4x\b", 2.5), (r"grand strategy", 2.5),
            (r"city builder", 2.5), (r"colony sim", 2.5), (r"base building", 1.5),
            (r"automation", 2.0), (r"management", 1.5), (r"tower defense", 2.0),
            (r"\bbuilding\b", 1.0), (r"resource management", 1.5), (r"tycoon|economy", 1.5),
            (r"card game|deckbuild|board game", 1.5), (r"puzzle", 1.0), (r"sandbox", 0.5),
            (r"factory|logistics", 1.5), (r"space sim", 0.8),
        ],
        "yaris_sim": [
            (r"racing", 2.5), (r"driving", 2.0), (r"\bsimulation\b", 1.0),
            (r"flight", 2.0), (r"sports\b", 1.5), (r"automobile sim|sim racing", 2.5),
            (r"truck|farming sim|train", 1.5), (r"football|soccer|basketball", 2.0),
            (r"space sim", 1.0), (r"vehicular", 1.2),
        ],
    },
}

# ============================================================================
#  STORE GENRE NAMES  (CHANGE 4: iTunes primaryGenreName, Deezer album genres,
#  YouTube keywords, also exact MusicBrainz tags). Matched EXACTLY on the folded
#  name (Turkish-aware casefold, accents dropped, "/" "-" → space), before the
#  regex tables above; a name may score for several groups. One store genre is
#  the only evidence a track has, so a decisive name weighs ≥ MIN_SCORE; weaker
#  names (< 1.0) never decide alone. Deezer answers in the IP's language unless
#  asked in English (we send Accept-Language: en) — Turkish names are listed too.
#  Decisions: bass music / dance / electro / house / rap → elektronik (the
#  "Elektronik / Hip-hop" group); Arabesque, Fantezi, Türk Pop → pop_vokal;
#  Halk, Sanat, Özgün, Religious (Turkish store sub-genres) → klasik_akustik;
#  Turkish Rock / Alternative → metal_rock; Soundtrack alone is not decisive.
# ============================================================================
GENRE_NAMES: dict[str, dict[str, float]] = {
    "elektronik": {
        "dance": 2.0, "dans": 2.0, "electronic": 2.0, "elektronik": 2.0, "electronica": 2.0,
        "electro": 2.0, "house": 2.0, "deep house": 2.0, "tech house": 2.0, "bass house": 2.0,
        "slap house": 2.0, "afro house": 2.0, "progressive house": 2.0, "techno": 2.0,
        "trance": 2.0, "bass": 2.0, "bass music": 2.0, "dubstep": 2.0, "jungle drumnbass": 2.0,
        "drum and bass": 2.0, "drum & bass": 2.0, "dnb": 2.0, "breakbeat": 2.0, "garage": 1.5,
        "idm experimental": 1.8, "downtempo": 1.5, "edm": 2.0, "phonk": 2.0, "drift phonk": 2.0,
        "brazilian phonk": 2.0, "hip hop rap": 2.0, "rap hip hop": 2.0, "hip hop": 2.0,
        "rap": 2.0, "turkish hip hop rap": 2.5, "turkce rap": 2.5, "alternative rap": 2.0,
        "gangsta rap": 2.0, "underground rap": 2.0, "hardcore rap": 2.0, "east coast rap": 2.0,
        "west coast rap": 2.0, "dirty south": 2.0, "old school rap": 2.0, "uk hip hop": 2.0,
        "deutschrap": 2.0, "trap": 2.0, "drill": 2.0, "grime": 2.0, "fitness & workout": 1.2,
        "amapiano": 1.5, "baile funk": 1.5, "synthwave": 2.0, "lofi": 1.5, "lo fi": 1.5,
        "chillout": 1.2, "chill out": 1.2, "car music": 1.5, "remix": 0.6,
        # 27 Eki genişletme (iTunes / Deezer / Discogs / MusicBrainz stil adları)
        "electronic music": 2.0, "electronic dance music": 2.0, "elektronik muzik": 2.0,
        "dance music": 2.0, "dans muzigi": 2.0, "electro house": 2.0, "future house": 2.0,
        "future bass": 2.0, "big room": 2.0, "big room house": 2.0, "melodic techno": 2.0,
        "minimal techno": 2.0, "hard techno": 2.0, "acid house": 2.0, "chicago house": 2.0,
        "euro house": 2.0, "eurodance": 2.0, "italo disco": 1.5, "nu disco": 2.0,
        "hardstyle": 2.0, "hardcore techno": 2.0, "gabber": 2.0, "psytrance": 2.0,
        "psychedelic trance": 2.0, "goa trance": 2.0, "progressive trance": 2.0,
        "uplifting trance": 2.0, "hard trance": 2.0, "drumnbass": 2.0, "drum n bass": 2.0,
        "liquid funk": 2.0, "neurofunk": 2.0, "breakcore": 2.0, "uk garage": 2.0,
        "2 step": 1.5, "jersey club": 2.0, "footwork": 1.5, "moombahton": 2.0, "brostep": 2.0,
        "riddim": 1.5, "glitch hop": 2.0, "glitch": 1.2, "electroclash": 2.0, "ebm": 1.5,
        "electronic body music": 1.5, "witch house": 1.5, "chillwave": 1.5, "vaporwave": 1.5,
        "retrowave": 2.0, "outrun": 2.0, "darksynth": 2.0, "hyperpop": 1.5, "ambient": 1.2,
        "dark ambient": 1.2, "ambient music": 1.2, "trip hop": 1.8, "chillhop": 2.0,
        "lofi hip hop": 2.0, "lo fi hip hop": 2.0, "leftfield": 1.2, "edm trap": 2.0,
        "hip hop music": 2.0, "hip hop rap music": 2.0, "turkish hip hop": 2.5,
        "turkish rap": 2.5, "turkce hip hop": 2.5, "turk rap": 2.5, "pop rap": 2.0,
        "melodic rap": 2.0, "emo rap": 2.0, "cloud rap": 2.0, "boom bap": 2.0,
        "conscious hip hop": 2.0, "jazz rap": 1.5, "trap music": 2.0, "latin trap": 1.5,
        "afro trap": 2.0, "uk drill": 2.0, "brooklyn drill": 2.0, "plugg": 2.0, "rage": 1.2,
        "crunk": 2.0, "g funk": 2.0, "horrorcore": 2.0, "mumble rap": 2.0, "arabesk rap": 2.0,
        "mahraganat": 1.5, "brazilian funk": 2.0, "funk carioca": 2.0,
        "funk brasileiro": 2.0,
    },
    "pop_vokal": {
        "pop": 2.0, "turkish pop": 2.5, "turk pop": 2.5, "turkce pop": 2.5, "arabesque": 2.5,
        "arabesk": 2.5, "fantezi": 2.5, "fantasy pop": 1.5, "damar": 2.0, "r&b soul": 2.0,
        "r&b": 2.0, "rnb": 2.0, "soul": 2.0, "soul & funk": 2.0, "contemporary r&b": 2.0,
        "neo soul": 2.0, "k pop": 2.0, "j pop": 2.0, "c pop": 2.0, "mandopop": 2.0,
        "latin": 1.5, "latin music": 1.5, "latin muzik": 1.5, "pop in spanish": 2.0,
        "latin urban": 1.2, "vocal": 1.5, "vocal pop": 2.0, "traditional pop": 1.5,
        "adult contemporary": 1.5, "teen pop": 2.0, "french pop": 2.0, "german pop": 2.0,
        "oldies": 1.2, "easy listening": 1.2, "christian pop": 1.5, "disney": 1.2,
        "anime": 1.2, "enka": 1.5, "kayokyoku": 1.5, "arabic music": 1.5, "arap muzigi": 1.5,
        "arabic pop": 2.0, "indian music": 1.5, "hint muzigi": 1.5, "bollywood": 1.5,
        "indian pop": 2.0, "brazilian music": 1.2, "brezilya muzigi": 1.2, "mpb": 1.2,
        "afrobeats": 1.2, "afro pop": 1.5, "reggae": 1.2, "reggaeton": 1.5, "kids": 1.0,
        "children s music": 1.0, "childrens music": 1.0, "dance pop": 1.5,
        # 27 Eki genişletme
        "pop music": 2.0, "pop muzik": 2.0, "turkish pop music": 2.5, "turk pop muzigi": 2.5,
        "turk popu": 2.5, "turkce pop muzik": 2.5, "anadolu pop": 2.0, "arabesk pop": 2.5,
        "arabesque music": 2.5, "arabesk muzik": 2.5, "fantezi muzik": 2.5,
        "fantezi muzigi": 2.5, "slow": 1.5, "slow pop": 2.0, "turkce slow": 2.0,
        "taverna": 1.5, "kpop": 2.0, "jpop": 2.0, "cpop": 2.0, "korean pop": 2.0,
        "cantopop": 2.0, "city pop": 1.5, "trot": 1.5, "europop": 2.0, "euro pop": 2.0,
        "indie pop": 1.5, "art pop": 1.5, "bedroom pop": 1.5, "sophisti pop": 1.5,
        "electropop": 1.5, "synth pop": 1.0, "synthpop": 1.0, "ballad": 1.2, "pop ballad": 2.0,
        "chanson": 1.5, "french chanson": 1.5, "schlager": 1.5, "eurovision": 1.2,
        "musicals": 1.5, "musical theatre": 1.5, "show tunes": 1.5,
        "cabaret": 1.2, "gospel": 1.5, "christian & gospel": 1.2, "contemporary christian": 1.5,
        "praise & worship": 1.2, "worship": 1.2, "funk": 1.0, "funk soul": 2.0, "motown": 2.0,
        "new jack swing": 2.0, "quiet storm": 2.0, "doo wop": 2.0, "rhythm and blues": 2.0,
        "r&b soul music": 2.0, "urban contemporary": 1.5, "disco": 1.2,
        "latin pop": 2.0, "salsa": 1.5, "bachata": 1.5, "merengue": 1.5, "cumbia": 1.5,
        "bolero": 1.5, "ranchera": 1.5, "mariachi": 1.5, "regional mexicano": 1.5,
        "musica mexicana": 1.5, "corridos": 1.5, "norteno": 1.5,
        "sertanejo": 1.5, "pagode": 1.2, "forro": 1.2, "samba": 1.2,
        "afrobeat": 1.2, "afropop": 1.5, "highlife": 1.2, "zouk": 1.2, "kizomba": 1.2,
        "dancehall": 1.2, "ska": 1.2, "filmi": 1.5, "bhangra": 1.5, "laiko": 1.5,
        "turbo folk": 1.5, "chalga": 1.5, "manele": 1.5, "rai": 1.5, "mizrahi": 1.5,
        "persian pop": 2.0, "khaleeji": 1.5, "dabke": 1.2, "a cappella": 1.2,
    },
    "metal_rock": {
        "rock": 2.0, "metal": 2.0, "alternative": 1.5, "alternatif": 1.5, "turkish rock": 2.5,
        "turk rock": 2.5, "turkish alternative": 1.5, "anadolu rock": 2.5, "hard rock": 2.0,
        "punk": 2.0, "pop punk": 2.0, "grunge": 2.0, "indie rock": 2.0, "goth rock": 2.0,
        "emo": 1.5, "death metal black metal": 2.0, "hair metal": 2.0, "prog rock art rock": 2.0,
        "psychedelic": 1.2, "soft rock": 1.0, "blues rock": 1.5, "christian rock": 1.5,
        "christian metal": 2.0, "college rock": 1.5, "new wave": 1.0, "metalcore": 2.0,
        "alternative and latin rock": 1.5, "rock & roll": 1.5, "arena rock": 2.0,
        # 27 Eki genişletme
        "rock music": 2.0, "rock muzik": 2.0, "anatolian rock": 2.5, "anadolu rock muzik": 2.5,
        "turkce rock": 2.5, "turk rock muzigi": 2.5, "turkish metal": 2.5, "turk metal": 2.5,
        "alternatif rock": 2.0, "alternative rock": 2.0, "alternative metal": 2.0,
        "heavy metal": 2.0, "thrash metal": 2.0, "death metal": 2.0, "black metal": 2.0,
        "doom metal": 2.0, "power metal": 2.0, "gothic metal": 2.0, "gothic rock": 2.0,
        "symphonic metal": 2.0, "folk metal": 2.0, "nu metal": 2.0, "industrial metal": 2.0,
        "progressive metal": 2.0, "progressive rock": 2.0, "prog rock": 2.0, "art rock": 1.5,
        "classic rock": 2.0, "psychedelic rock": 2.0, "garage rock": 2.0, "stoner rock": 2.0,
        "southern rock": 2.0, "post rock": 1.5, "post punk": 1.5, "post hardcore": 2.0,
        "hardcore punk": 2.0, "punk rock": 2.0, "ska punk": 2.0, "deathcore": 2.0, "djent": 2.0,
        "grindcore": 2.0, "sludge metal": 2.0, "math rock": 1.5, "shoegaze": 1.5,
        "britpop": 1.5, "j rock": 2.0, "visual kei": 1.5, "rockabilly": 1.5, "surf rock": 1.5,
        "rock n roll": 1.5, "rock and roll": 1.5, "industrial": 1.0, "industrial rock": 2.0,
        "darkwave": 1.0, "noise rock": 1.5, "indie": 0.6,
    },
    "klasik_akustik": {
        "classical": 2.0, "klasik": 2.0, "jazz": 2.0, "caz": 2.0, "blues": 1.5, "folk": 1.5,
        "halk": 2.5, "turk halk muzigi": 2.5, "turkish folk": 2.5, "sanat": 2.5,
        "turk sanat muzigi": 2.5, "ozgun": 1.5, "religious": 1.0, "singer songwriter": 1.2,
        "contemporary folk": 1.5, "new acoustic": 1.5, "traditional folk": 2.0, "country": 1.5,
        "bluegrass": 1.5, "original score": 1.5, "film music": 1.5, "soundtrack": 0.8,
        "films games": 0.8, "film oyun": 0.8, "video game": 0.8, "new age": 1.2,
        "instrumental": 1.0, "orchestral": 2.0, "opera": 1.5, "piano": 1.5,
        "chamber music": 2.0, "worldwide": 1.0, "world": 1.0, "tarab": 1.2, "flamenco": 1.8,
        "fado": 1.8, "tango": 1.8, "bossa nova": 1.8, "celtic": 1.2, "sufi & ghazals": 1.5,
        "indian classical": 2.0, "easy listening jazz": 1.5, "vocal jazz": 2.0,
        # 27 Eki genişletme. Türk: halk/türkü/oyun havası/sanat/tasavvuf/mehter → burada
        "turk halk muzik": 2.5, "halk muzigi": 2.5, "turku": 2.5, "turkuler": 2.5,
        "turkish folk music": 2.5, "oyun havasi": 1.5, "oyun havalari": 1.5,
        "uzun hava": 2.0, "bozlak": 2.0, "halay": 1.5, "horon": 1.5, "zeybek": 1.5,
        "karadeniz": 1.0, "roman havasi": 1.2, "sanat muzigi": 2.5, "turk sanat muzik": 2.5,
        "klasik turk muzigi": 2.5, "turk musikisi": 2.5, "turkish classical music": 2.5,
        "ottoman classical music": 2.5, "fasil": 2.0, "tasavvuf": 2.0, "tasavvuf muzigi": 2.0,
        "sufi music": 2.0, "ilahi": 1.5, "ilahiler": 1.5, "mehter": 2.0,
        "ozgun muzik": 1.5, "ozgun muzigi": 1.5, "classical music": 2.0,
        "klasik muzik": 2.0, "classical crossover": 1.5, "baroque": 2.0, "baroque music": 2.0,
        "contemporary classical": 2.0, "modern classical": 2.0,
        "neoclassical": 1.5, "neo classical": 1.5, "minimalism": 1.5, "early music": 2.0,
        "medieval": 1.2, "renaissance music": 1.8, "operetta": 1.5, "art song": 1.8,
        "choral": 1.8, "choir": 1.5, "sacred music": 1.5, "church music": 1.5,
        "gregorian chant": 2.0, "symphony": 1.5, "symphonic": 1.2, "orchestra": 1.8,
        "string quartet": 2.0, "solo piano": 1.5, "brass & military": 1.5, "marching band": 1.5,
        "smooth jazz": 2.0, "jazz fusion": 1.8, "bebop": 2.0, "hard bop": 2.0,
        "cool jazz": 2.0, "free jazz": 2.0, "swing": 1.5, "big band": 2.0, "latin jazz": 2.0,
        "gypsy jazz": 2.0, "jazz funk": 1.5, "nu jazz": 1.2, "ragtime": 2.0, "dixieland": 2.0,
        "delta blues": 2.0, "chicago blues": 2.0, "electric blues": 1.5, "acoustic": 1.5,
        "akustik": 1.5, "folk music": 1.5, "indie folk": 1.5, "folk rock": 1.0,
        "americana": 1.5, "country music": 1.5, "honky tonk": 1.5, "outlaw country": 1.5,
        "folk world & country": 1.5, "world music": 1.0, "klezmer": 1.5, "rebetiko": 1.5,
        "qawwali": 1.5, "ghazal": 1.5, "carnatic": 2.0, "hindustani classical": 2.0,
        "persian classical": 2.0, "arabic classical": 2.0, "maqam": 1.5,
        # Soundtrack / Film müziği bilerek < 1,0: tek başına karar vermez (eski kural)
        "film muzigi": 0.8, "original soundtrack": 0.8, "ost": 0.8, "video game music": 0.8,
        "game music": 0.8, "stage & screen": 0.8,
    },
    "konusma": {
        "spoken word": 3.0, "comedy": 2.0, "standup comedy": 2.5, "stand up comedy": 2.5,
        "podcast": 3.0, "podcasts": 3.0, "audiobooks": 3.0, "horspiele": 3.0, "stories": 1.5,
        "news & politics": 1.5, "education": 1.2,
        # 27 Eki genişletme
        "audiobook": 3.0, "audio book": 3.0, "sesli kitap": 3.0, "sesli kitaplar": 3.0,
        "audio drama": 3.0, "radio drama": 3.0, "radyo tiyatrosu": 3.0, "poetry": 2.0,
        "siir": 2.0, "talk show": 2.5, "interview": 2.0, "roportaj": 2.0,
        "news": 1.5, "haber": 1.5, "haberler": 1.5, "lecture": 2.0, "sermon": 2.0,
        "vaaz": 2.0, "stand up": 2.5, "non music": 1.2, "asmr": 1.5,
    },
}

# ============================================================================
#  BUILT-IN TURKISH ARTISTS
#  MusicBrainz coverage of Turkish artists is sparse (Sezen Aksu has no genre
#  tag at all, see docs/NOTES-tur-arastirma.md). These well-known names skip
#  the network; the user's own assignments (tur-atamalari.json) still win.
#  Same Turkish-genre decisions as above (arabesk → pop_vokal, THM/TSM →
#  klasik_akustik, Anadolu rock → metal_rock, rap → elektronik).
# ============================================================================
BUILTIN_ARTISTS: dict[str, tuple[str, ...]] = {
    "metal_rock": (
        "Duman", "maNga", "Mor ve Ötesi", "Pentagram", "Mezarkabul", "Kurban", "Athena",
        "Şebnem Ferah", "Teoman", "Cem Karaca", "Barış Manço", "Erkin Koray", "Moğollar",
        "Haluk Levent", "Gripin", "Model", "Pinhani", "Kargo", "Yüksek Sadakat", "Hayko Cepkin",
        "Redd", "Kesmeşeker", "Adamlar", "Son Feci Bisiklet", "Ogün Sanlısoy", "Feridun Düzağaç",
        "Vega", "Replikas", "Altın Gün", "Fikret Kızılok", "3 Hürel", "Bulutsuzluk Özlemi",
        "Dolu Kadehi Ters Tut", "Almora", "Badem", "Gece", "Kazım Koyuncu",
    ),
    "elektronik": (
        "Ezhel", "Ceza", "Sagopa Kajmer", "Ben Fero", "Uzi", "Şanışer", "Hidra", "Norm Ender",
        "Contra", "Lvbel C5", "Motive", "Reckol", "Cakal", "Gazapizm", "Khontkar", "Allame",
        "Server Uraz", "Patron", "No.1", "Anıl Piyancı", "Killa Hakan", "Fuat Ergin", "Massaka",
        "Ati242", "Mavi", "Batuflex", "Heijan", "Murda", "Mahmut Orhan", "Burak Yeter",
        "Mercan Dede", "Joker", "Organize", "Aspova",
    ),
    "klasik_akustik": (
        "Zeki Müren", "Müzeyyen Senar", "Emel Sayın", "Bülent Ersoy", "Neşet Ertaş", "Aşık Veysel",
        "Arif Sağ", "Musa Eroğlu", "Erkan Oğur", "Zülfü Livaneli", "Fazıl Say", "İdil Biret",
        "Ezginin Günlüğü", "Grup Yorum", "Kardeş Türküler", "Ahmet Kaya", "Selda Bağcan",
        "Kalben", "Münir Nurettin Selçuk", "Safiye Ayla", "Belkıs Akkale", "Hüseyin Turan",
        "Mahsun Kırmızıgül", "Sabahat Akkiraz", "Tülay German", "Kubat",
    ),
    "pop_vokal": (
        "Tarkan", "Sezen Aksu", "Sertab Erener", "Ajda Pekkan", "Hande Yener", "Hadise", "Gülşen",
        "Mustafa Sandal", "Kenan Doğulu", "Murat Boz", "Edis", "Aleyna Tilki", "Simge",
        "Zeynep Bastık", "Mabel Matiz", "Sıla", "Yıldız Tilbe", "Demet Akalın", "Ebru Gündeş",
        "Bengü", "Funda Arar", "Nil Karaibrahimgil", "Müslüm Gürses", "Ferdi Tayfur",
        "Orhan Gencebay", "İbrahim Tatlıses", "Bergen", "Hakan Altun", "Ümit Besen",
        "Cengiz Kurtoğlu", "Azer Bülbül", "Emrah", "MFÖ", "Erol Evgin", "Nilüfer", "Gökhan Türkmen",
        "Cem Adrian", "Melike Şahin", "Can Bonomo", "Yalın", "Göksel", "Candan Erçetin",
        "Manuş Baba", "Sezen Cumhur Önal", "Emre Aydın", "Ebru Yaşar", "Hakan Peker", "Serdar Ortaç",
        "Levent Yüksel", "Kıraç", "Ferhat Göçer", "Ümit Sayın", "Bülent Serttaş",
    ),
}

# ============================================================================
#  TITLE HINTS  (per track: "Song (Acoustic)" of a rock artist → klasik_akustik)
#  regex searched in the track title (lower-cased) → group. Extended/overridden
#  by kurallar.toml [tur] baslik_ipucu = { "<regex>" = "<grup>" }. A hint beats
#  the artist's genre (the user asked for "şarkı adına göre"), but not a group
#  the user assigned to that artist by hand.
# ============================================================================

def _deco(words: str) -> str:
    """Regex: one of ``words`` as a DECORATION of the title, not as the song's own name:
    inside (…) / […] / {…}, in a segment after " - " / " | ", or a leading label
    ("Röportaj: …", "Interview - …"). So "Gece (Slowed + Reverb)", "Rain - Lo-Fi" match,
    "Slowed Down", "Lo-Fi", "Interview With The Vampire" do not (27 Eki hata avı)."""
    w = f"(?:{words})"
    return (rf"[\(\[\{{][^\)\]\}}]*?\b{w}\b"
            rf"|(?:^|\s)[-–—|]\s*(?:[^-–—|\(\[]*?\s)?{w}\b"
            rf"|^\s*{w}\s*[:\-–—|]")


# classical title vocabulary (hint context only: none of these decides alone)
_CL_FORMS = (r"symphon(?:y|ie)|sinfonie|sinfonia|senfoni|sonata|sonatina|sonat|concerto|"
             r"konçerto|nocturne|noktürn|étude|etude|[ée]tudes|prelude|prélude|prelüd|fugue|füg|"
             r"suite|süit|partita|toccata|fantasia|fantaisie|impromptu|mazurka|polonaise|waltz|"
             r"valse|ballade|scherzo|rondo|serenade|serenad|requiem|cantata|kantat|overture|"
             r"uvertür|variations|varyasyon|aria|adagio|allegro|andante|largo|minuet|menuet|"
             r"gavotte|sarabande|gigue|bagatelle|rhapsody|rapsodi|quartet|kuartet|quintet|"
             r"caprice|capriccio|divertimento|barcarolle|berceuse|chaconne|passacaglia|"
             r"invention|lieder")
# Op. / Opus / K. / RV + number: only in a classical context ("Opus 17", "RV 2" are songs)
_CL_CAT_WEAK = r"(?:op\.?\s*\d+|opus\s*\d+|k\.\s*\d{2,3}\b|rv\s*\d+)"
_CL_ACC = r"(?:-flat|-sharp| flat| sharp|♭|♯|b|#)"

TITLE_HINTS: dict[str, str] = {
    r"\b(acoustic|akustik|unplugged|piano version|piyano versiyon|orchestral version|"
    r"senfonik versiyon)\b": "klasik_akustik",
    # DJ/EDM title conventions and genre words: small producers on YouTube Mix
    # often share a name with an unrelated MusicBrainz artist ("SAFI - Forever
    # Mine (Original Mix)" → a Leipzig indie trio), so the title decides
    r"\b(remix|club mix|extended mix|original mix|dub mix|vip mix|bootleg|mashup|"
    r"bass boosted|car music|deep house|bass house|slap house|tech house|house music|"
    r"afro house|melodic techno|techno|phonk|edm|dnb|drum (and|&|n) bass|dubstep)\b": "elektronik",
    r"\bpodcast\b|\b(episode|bölüm|ep\.)\s*\d+": "konusma",
    # 27 Eki genişletme. Yanlış pozitif düşük kalsın diye: tek başına şarkı adı olabilen
    # sözcükler ("Requiem", "Prelude", "Trance", "Live") ya hiç yok ya da kalıba bağlı.
    # Canlı / Live / Cover / Enstrümantal / Karaoke bilerek YOK: türü değiştirmez
    # (bir rock grubunun canlı kaydı yine rock).
    # internet düzenlemeleri: slowed/reverb, sped up, nightcore, 8D, lo-fi, DJ set.
    # Tek sözcükler (Slowed, Sped Up, Lo-Fi, Nightcore…) yalnız süs eki olarak: parantez,
    # " - " / " | " sonrası ("Slowed Down", "Lo-Fi" adlı şarkılar tetiklemez)
    _deco(r"slowed|sped up|speed up|nightcore|daycore|reverb|8d|lo-?fi"): "elektronik",
    r"\b(slowed ?(\+|&|and|n) ?reverb|reverb ?(\+|&) ?slowed|8d audio|"
    r"8d (version|müzik|muzik|music)|lo-?fi (hip ?hop|beats?|remix|mix|version|edit|cover)|"
    r"dj set|dj mix|boiler room|type beat|synthwave|retrowave|hardstyle|psytrance|"
    r"future bass|trap (remix|beat|music)|drill (beat|remix)|club edit|festival mix|"
    r"bass music|bas müzik)\b": "elektronik",
    # klasik eser kalıpları ve piyano/orkestra sürümleri. Katalog: BWV/HWV/KV/Hob. + numara
    # tek başına (şarkı adı olmaz); Op./Opus/K./RV + numara yalnız klasik bağlamda (virgül,
    # parantez, ayraç sonrası; önünde form adı; ardından "No. N"). Ton: "in C minor",
    # "in B-flat Major" (harf BÜYÜK: B–G), "in A major" (A ile major/minor küçük harf:
    # "Living In A Major City" tetiklemez), "in c-moll"; ya da form adı + ton (her yazım).
    r"\b(bwv|kv|hwv)\s*\d+|\bhob\.\s*[ivx]+|"
    rf"(,|:|[\(\[]|\s[-–—|])\s*(no\.?\s*\d+\s*,?\s*)?\b{_CL_CAT_WEAK}|"
    rf"\b({_CL_FORMS})\b[^\(\[|]*?\b{_CL_CAT_WEAK}|"
    rf"\b{_CL_CAT_WEAK}\s*,?\s*no\.?\s*\d+|"
    rf"\bin (?-i:[B-G]){_CL_ACC}? (major|minor)\b|"
    rf"\bin (?-i:A)({_CL_ACC} (major|minor)|(?-i: (major|minor)))\b|"
    r"\bin [a-h](is|es|s)?-(dur|moll)\b|"
    rf"\b({_CL_FORMS})\b[^\(\[|]*?\bin [a-g]{_CL_ACC}?[ -](major|minor|dur|moll)\b|"
    r"\b(symphony|senfoni|sonata|sonat|concerto|konçerto|nocturne|noktürn|étude|etude|"
    r"prelude and fugue|string quartet|yaylı(lar)? dörtlüsü) no\.?\s*\d+|"
    r"\b(piano|piyano) (cover|solo|versiyon|version|instrumental|arrangement|düzenleme)\b|"
    r"\b(solo piano|piyano ile|orchestral (cover|arrangement)|orkestra (versiyon|eşliğinde)|"
    r"symphonic version|senfoni orkestrası|symphony orchestra|philharmonic|filarmoni(k)?|"
    r"string quartet (cover|version)|bağlama (solo|ile)|saz (solo|ile)|ney (taksimi|ile)|"
    r"taksimi|uzun hava)\b": "klasik_akustik",
    r"\b(a ?cappella|acapella)\b": "pop_vokal",
    # konuşma: sesli kitap, röportaj, söyleşi, haber, ders/konferans, stand-up, şiir dinletisi.
    # Interview / Konferans / Seminer / Lecture / Vaaz / Seslendiren yalnız süs eki olarak
    # ("Interview With The Vampire" bir şarkı; "(Full Interview)", "Röportaj: …" tetikler)
    r"\b(sesli kitap|audiobook|audio book|röportaj|roportaj|söyleşi|soylesi|"
    r"haber bülteni|ana haber|gündem özeti|radyo tiyatrosu|radio drama|audio drama|"
    r"stand[ -]?up (comedy|show|gösterisi)|ted ?x? talk|tedx|full interview|"
    r"hutbe|şiir dinletisi|siir dinletisi)\b|"
    r"\b(ders \d+|sezon \d+ bölüm|season \d+ episode)": "konusma",
    _deco(r"interview|konferans|seminer|lecture|vaaz|seslendiren|seslendirme"): "konusma",
}

_COMPILED: dict[str, dict[str, list[tuple[re.Pattern, float]]]] = {
    kind: {g: [(re.compile(p, re.I), w) for p, w in pats] for g, pats in groups.items()}
    for kind, groups in TAG_TABLES.items()
}


def _tag_items(tags) -> list[tuple[str, float]]:
    """Accept list[str] | list[(tag, count)] | dict{tag: count} → [(tag, count)] sorted."""
    items: list[tuple[str, float]] = []
    if isinstance(tags, dict):
        items = [(str(k), float(v) if isinstance(v, (int, float)) else 1.0) for k, v in tags.items()]
    elif isinstance(tags, (list, tuple)):
        for t in tags:
            if isinstance(t, (list, tuple)) and t:
                c = t[1] if len(t) > 1 and isinstance(t[1], (int, float)) else 1.0
                items.append((str(t[0]), float(c)))
            elif isinstance(t, str):
                items.append((t, 1.0))
    items.sort(key=lambda x: -x[1])
    return items


def score_tags(kind: str, tags) -> dict[str, float]:
    """Group scores. Tags are weighted by rank (most-voted first) and vote count."""
    table = _COMPILED.get(kind, {})
    items = _tag_items(tags)[:25]
    top = items[0][1] if items else 1.0
    scores = {g: 0.0 for g in table}
    names = _GENRE_INDEX if kind == "muzik" else {}
    for rank, (tag, count) in enumerate(items):
        w_tag = (1.0 / (1.0 + 0.15 * rank)) * (0.5 + 0.5 * (count / top if top > 0 else 1.0))
        exact = names.get(_fold(tag)) if names else None
        if exact is not None:                     # a known store genre name
            for g, w in exact.items():
                scores[g] += w * w_tag
            continue
        t = tag.lower()
        for g, pats in table.items():
            best = max((w for rx, w in pats if rx.search(t)), default=0.0)
            scores[g] += best * w_tag
    return scores


_WEAK_TURKISH = re.compile(r"\b(turkish|türk|türkçe|turkey|türkiye)\b", re.I)


def map_tags(kind: str, tags) -> str:
    """Tags (MusicBrainz genres/tags, Steam/SteamSpy tags) → group name or ''."""
    scores = score_tags(kind, tags)
    if not scores:
        return ""
    g, s = max(scores.items(), key=lambda kv: kv[1])
    return g if s >= MIN_SCORE else ""


def _confidence(kind: str, tags, group: str) -> float:
    scores = score_tags(kind, tags)
    tot = sum(scores.values())
    return round(scores.get(group, 0.0) / tot, 2) if tot > 0 else 0.0


# ============================================================================
#  normalisation
# ============================================================================

def norm(s: str) -> str:
    """Matching key: Turkish-aware casefold (İ/I/ı → i), accents kept, spaces collapsed."""
    s = (s or "").replace("İ", "i").replace("I", "i").replace("ı", "i")
    s = unicodedata.normalize("NFC", s.casefold()).replace("̇", "")
    s = re.sub(r"[\"'`´’]", "", s)
    s = re.sub(r"[^\w&+]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _fold(s: str) -> str:
    """norm() without accents (ü→u, ş→s, ğ→g): "Türk Halk Müziği" == "turk halk muzigi"."""
    s = unicodedata.normalize("NFKD", norm(s))
    return "".join(ch for ch in s if not unicodedata.combining(ch))


_GENRE_INDEX: dict[str, dict[str, float]] = {}
for _g, _names in GENRE_NAMES.items():
    for _n, _w in _names.items():
        _GENRE_INDEX.setdefault(_fold(_n), {})[_g] = _w


_FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(feat\.?|ft\.?|featuring|with|vs\.?)\s+.*$", re.I)
# no "and"/"ve": band names ("Mor ve Ötesi", "Florence and the Machine") keep their name
_MULTI_RE = re.compile(r"\s+(?:&|x|,|/)\s+|\s*,\s*|\s*;\s*", re.I)
_CHANNEL_JUNK = re.compile(
    r"(\s*-\s*topic$|\s*vevo$|\s*official$|\s*\bofficial\b|\s*records?$|\s*music$|"
    r"\s*müzik$|\s*tv$|\s*channel$|\s*resmi$)", re.I)
_TITLE_JUNK = re.compile(
    r"\s*[\(\[][^)\]]*(official|video|audio|lyric|klip|hd|4k|remaster|live|canlı|"
    r"visuali[sz]er|sözleri|music|müzik)[^)\]]*[\)\]]", re.I)


def normalize_artist(name: str) -> str:
    """Key form of an artist name (tur-atamalari.json, ContentId "sanatci:<key>")."""
    return norm(clean_artist(name))


def clean_artist(name: str, channel: bool = False) -> str:
    """Display form: first credited artist, without feat./ft. (and channel junk)."""
    a = (name or "").strip()
    a = _FEAT_RE.sub("", a).strip()
    a = _MULTI_RE.split(a)[0].strip() if a else a
    if channel:
        for _ in range(3):
            a2 = _CHANNEL_JUNK.sub("", a).strip(" -|")
            if a2 == a:
                break
            a = a2
    return a.strip(" -–—|")


# feat./ft./featuring credit only: the part before it IS the artist (unlike "&", ",", "x",
# "with", "vs", which may be inside a group's own name)
_FEAT_ONLY_RE = re.compile(r"\s*[\(\[]?\s*\b(feat\.?|ft\.?|featuring)\s+.*$", re.I)


def uncut_artist(name: str, channel: bool = False) -> str:
    """clean_artist() without the cut at "&", ",", "x", "/", ";", "with", "vs": feat./ft.
    credits and channel junk go, a possible group name stays whole ("Simon & Garfunkel",
    "Down with Webster", "TOMORROW X TOGETHER"). Equals clean_artist() when nothing was cut."""
    a = _FEAT_ONLY_RE.sub("", (name or "").strip()).strip()
    if channel:
        for _ in range(3):
            a2 = _CHANNEL_JUNK.sub("", a).strip(" -|")
            if a2 == a:
                break
            a = a2
    return a.strip(" -–—|")


# ContentId → the uncut name, for the songs content_id() cut ("Simon & Garfunkel" → key
# "simon"). ContentId has no raw-name field; the resolver needs it so that a cut first
# name never counts as a SURE database hit for another artist (Simon, Belle, Down…).
_UNCUT_MAX = 512
_uncut: dict[ContentId, str] = {}
_uncut_lock = threading.Lock()


def _note_uncut(cid: ContentId, full: str) -> None:
    with _uncut_lock:
        _uncut.pop(cid, None)
        if full:
            _uncut[cid] = full
            while len(_uncut) > _UNCUT_MAX:
                _uncut.pop(next(iter(_uncut)))


def cut_name(cid: ContentId | None) -> str:
    """The whole credited name if ``cid``'s artist key is a CUT of it ("Simon & Garfunkel"
    for "sanatci:simon"), else ''. Known from content_id(), or from a ``display`` that
    still holds the raw name (callers that build ContentId themselves)."""
    if cid is None or cid.kind != "muzik":
        return ""
    with _uncut_lock:
        full = _uncut.get(cid, "")
    if full:
        return full
    bare = cid.key.split(":", 1)[-1]
    d = cid.display or ""
    if d and normalize_artist(d) == bare:
        full = uncut_artist(d)
        if norm(full) != bare:
            return full
    return ""


def split_title(title: str) -> tuple[str, str] | None:
    """"Artist - Song (Official Video)" → ("Artist", "Song"); None if no dash."""
    t = _TITLE_JUNK.sub("", title or "").strip()
    m = re.match(r"^(.+?)\s+[-–—|]\s+(.+)$", t)
    if not m:
        return None
    artist, song = m.group(1).strip(), m.group(2).strip()
    if not artist or not song or len(artist) > 60:
        return None
    return artist, song


_BRACKETS = re.compile(r"\s*[\(\[\{][^)\]\}]*[\)\]\}]")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|\+|/|\bfeat\.?|\bft\.?|\bfeaturing\b|\bx\b|\bvs\.?|"
                           r"\bwith\b)\s*", re.I)


_DECOR = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF|•·]")


def clean_track(track: str) -> str:
    """Search/match form of a song title: no "(Original Mix)", "[NCS]", "feat. X",
    " - Remastered 2011"; the original if nothing is left."""
    t = _BRACKETS.sub(" ", track or "")
    t = _DECOR.split(t)[0] if _DECOR.split(t)[0].strip() else t   # "GOZALO💫 BRAZILIAN FUNK"
    t = _FEAT_RE.sub("", t)
    t = re.sub(r"\s+[-–—|]\s+.*$", "", t)
    t = re.sub(r"\s+", " ", t).strip(" -–—|")
    return t or (track or "").strip()


def track_key(cid: ContentId) -> str:
    """Cache key of one song: "parca:<artist>|<normalized track>" ('' without a track)."""
    if cid.kind != "muzik" or not cid.track:
        return ""
    t = norm(clean_track(cid.track))
    return f"parca:{cid.key.split(':', 1)[-1]}|{t}" if t else ""


def artist_matches(theirs: str, ours: str) -> bool:
    """Name-mismatch guard: ``ours`` is (one of) the credited artist(s) of ``theirs``.
    Turkish-aware, accents ignored ("Muslum Gurses" == "Müslüm Gürses"), spaces optional."""
    want = _fold(ours)
    if not want or not theirs:
        return False
    parts = {_fold(theirs), _fold(clean_artist(theirs))}
    parts |= {_fold(x) for x in _ARTIST_SPLIT.split(theirs) if x and x.strip()}
    parts.discard("")
    return want in parts or want.replace(" ", "") in {x.replace(" ", "") for x in parts}


def track_matches(theirs: str, ours: str) -> bool:
    """Same song title (ignoring mix/version brackets, feat., accents)."""
    a, b = _fold(clean_track(theirs)), _fold(clean_track(ours))
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short) and len(short) / len(long_) >= 0.6:
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / len(ta | tb) >= 0.75


# ============================================================================
#  content_id
# ============================================================================

_clf_lock = threading.Lock()
_clf = None


def _classifier():
    global _clf
    with _clf_lock:
        if _clf is None:
            from .classifier import Classifier
            _clf = Classifier({})
        return _clf


_YT_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com", "youtube-nocookie.com")


def _host(url: str) -> str:
    try:
        h = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h[2:] if h.startswith("m.") else h


def _tab_title(media_name: str) -> str:
    """Browser tab title without the "(213) " notification count and " - YouTube"."""
    t = re.sub(r"^\(\d+\)\s*", "", media_name or "")
    return re.sub(r"\s+[-–—|]\s+YouTube( Music)?$", "", t).strip()


def _music_id(stream: Stream, player: MediaPlayer | None) -> ContentId | None:
    artist, track = "", ""
    ch_raw: str | None = None             # raw channel name the artist was cleaned from
    props = stream.props or {}
    tab = _tab_title(stream.media_name)
    if player is not None and player.title and split_title(tab) \
            and norm(player.title) not in norm(tab):
        # Firefox has ONE MPRIS player for all tabs: it is describing another tab
        player = None
    if player is not None:
        host = _host(player.url)
        is_yt = any(host == h or host.endswith("." + h) for h in _YT_HOSTS)
        if is_yt and host != "music.youtube.com":
            parsed = split_title(player.title)
            if parsed:
                artist, track = parsed
            else:
                artist, track = clean_artist(player.artist, channel=True), player.title
                ch_raw = player.artist
        else:
            artist, track = player.artist, player.title
            if not artist:
                parsed = split_title(player.title)
                if parsed:
                    artist, track = parsed
            if host == "music.youtube.com":
                ch_raw, artist = artist, clean_artist(artist, channel=True)
    if not artist:
        ch_raw = None
        artist = props.get("media.artist", "")
        track = track or props.get("media.title", "") or stream.media_name
    if not artist and tab:
        parsed = split_title(tab)
        if parsed:
            artist, track = parsed
    raw = artist
    artist = clean_artist(artist)
    key = norm(artist)
    if not key:
        return None
    url = (player.url or "") if player is not None else ""
    cid = ContentId(kind="muzik", key=f"sanatci:{key}", display=artist, track=(track or "").strip(),
                    url=url)
    full = uncut_artist(ch_raw, channel=True) if ch_raw is not None else uncut_artist(raw)
    _note_uncut(cid, full if norm(full) != key else "")
    return cid


# ---- local game titles (read-only) -----------------------------------------

def _steam_roots() -> list[Path]:
    home = Path.home()
    roots = [home / ".local/share/Steam", home / ".steam/steam"]
    out = []
    for r in roots:
        lf = r / "steamapps/libraryfolders.vdf"
        if lf.exists():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', lf.read_text("utf-8", "replace")):
                    out.append(Path(m.group(1)))
            except OSError:
                pass
        out.append(r)
    seen, res = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            res.append(p)
    return res


def steam_name(appid: str) -> str:
    for root in _steam_roots():
        acf = root / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            m = re.search(r'"name"\s+"([^"]+)"', acf.read_text("utf-8", "replace"))
        except OSError:
            continue
        if m:
            return m.group(1)
    return ""


def _faugus_games() -> list[dict]:
    home = Path.home()
    for p in (home / ".local/share/faugus-launcher/games.json",
              home / ".config/faugus-launcher/games.json"):
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, list):
            return [g for g in data if isinstance(g, dict)]
    return []


def _lutris_games() -> list[dict]:
    """[{name, prefix, exe, service, service_id}] from pga.db + game yml (read-only)."""
    home = Path.home()
    db = home / ".local/share/lutris/pga.db"
    out = []
    if not db.exists():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute("select name, configpath, service, service_id, directory "
                               "from games").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        log.debug("Lutris veritabanı okunamadı: %s", e)
        return out
    for name, cfg, service, sid, directory in rows:
        g = {"name": name or "", "service": service or "", "service_id": str(sid or ""),
             "prefix": directory or "", "exe": ""}
        for base in (home / ".local/share/lutris/games", home / ".config/lutris/games"):
            y = base / f"{cfg}.yml"
            try:
                text = y.read_text("utf-8", "replace")
            except OSError:
                continue
            for key in ("exe", "prefix"):
                m = re.search(rf"^\s+{key}:\s*(.+?)\s*$", text, re.M)
                if m:
                    g[key] = m.group(1).strip().strip("'\"")
            break
        out.append(g)
    return out


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a).rstrip("/") == os.path.realpath(b).rstrip("/")
    except OSError:
        return a.rstrip("/") == b.rstrip("/")


def _steam_appid(info: ProcInfo | None) -> str:
    if info is None:
        return ""
    for k in ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID"):
        v = (info.env.get(k) or "").strip()
        if v.isdigit() and v != "0" and len(v) <= 9:
            return v
    for _pid, _comm, cmd in info.ancestors:
        m = re.search(r"\bAppId=(\d+)", cmd or "")
        if m and m.group(1) != "0":
            return m.group(1)
    return ""


def _exe_names(stream: Stream, info: ProcInfo | None) -> list[str]:
    names = [stream.binary, stream.app_name]
    if info is not None:
        names += [info.comm, procinfo.basename_any(info.cmdline[0]) if info.cmdline else ""]
    return [n for n in names if n]


def _game_id(stream: Stream, info: ProcInfo | None) -> ContentId | None:
    appid = _steam_appid(info)
    if appid:
        return ContentId("oyun", f"steam:{appid}", steam_name(appid) or f"Steam {appid}")
    env = info.env if info else {}
    prefix = env.get("WINEPREFIX", "")
    exes = {n.lower() for n in _exe_names(stream, info) if n.lower().endswith(".exe")}
    cmd = " ".join(info.cmdline).lower() if info else ""
    for g in _faugus_games():
        gexe = procinfo.basename_any(str(g.get("path", ""))).lower()
        if _same_path(prefix, str(g.get("prefix", ""))) or (gexe and (gexe in exes or gexe in cmd)):
            title = str(g.get("title") or g.get("gameid") or "").strip()
            if title:
                return ContentId("oyun", f"oyun:{norm(title)}", title)
    for g in _lutris_games():
        gexe = procinfo.basename_any(g.get("exe", "")).lower()
        if (_same_path(prefix, g.get("prefix", "")) or (gexe and (gexe in exes or gexe in cmd))) \
                and g.get("name"):
            if g.get("service") == "steam" and g.get("service_id", "").isdigit():
                return ContentId("oyun", f"steam:{g['service_id']}", g["name"])
            return ContentId("oyun", f"oyun:{norm(g['name'])}", g["name"])
    for n in _exe_names(stream, info):
        if n.lower() in ("wine64-preloader", "wine-preloader", "wine", "wine64"):
            continue
        stem = re.sub(r"\.exe$", "", n, flags=re.I)
        k = norm(stem).replace(" ", "")
        if k:
            return ContentId("oyun", f"exe:{k}", stem)
    return None


def _looks_like_game(stream: Stream, info: ProcInfo | None) -> bool:
    if _steam_appid(info):
        return True
    env = info.env if info else {}
    if any(env.get(k) for k in ("WINEPREFIX", "GAMEID", "UMU_ID", "LUTRIS_GAME_UUID",
                                "STEAM_COMPAT_DATA_PATH")):
        return True
    return any(n.lower().endswith(".exe") for n in _exe_names(stream, info))


def content_id(stream: Stream, players: list[MediaPlayer] | None, proc: ProcInfo | None = None,
               *, kind: str | None = None, classifier=None) -> ContentId | None:
    """What is playing on ``stream`` (sync, no network). Never raises.

    ``kind``: "muzik" / "oyun" if the caller knows the target profile; otherwise
    game signals (Steam/wine/umu env, .exe) mean "oyun", else "muzik".
    ``proc``: ProcInfo of the stream's process (resolved via procinfo if None).
    """
    try:
        clf = classifier or _classifier()
        info = proc
        if info is None:
            _pid, info, _fp = clf.process_of(stream)
        if kind is None:
            kind = "oyun" if _looks_like_game(stream, info) else "muzik"
        if kind == "oyun":
            return _game_id(stream, info)
        player = clf.player_for(stream, players, proc=info)
        return _music_id(stream, player)
    except Exception:
        log.exception("içerik kimliği hesaplanamadı")
        return None


# ============================================================================
#  network
# ============================================================================

def http_get_json(url: str) -> Any:
    """Default fetch: GET JSON with our User-Agent (no cookies, no e-mail).
    ``Accept-Language: en``: Deezer otherwise names genres in the IP's language."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json",
                                               "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def http_get_text(url: str, limit: int = YOUTUBE_MAX_BYTES) -> str:
    """Default page fetch (YouTube watch page): at most ``limit`` bytes, no cookies."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read(limit).decode("utf-8", "replace")


MB_SEARCH = "https://musicbrainz.org/ws/2/artist?query={q}&fmt=json&limit=5"
MB_LOOKUP = "https://musicbrainz.org/ws/2/artist/{mbid}?inc=genres+tags&fmt=json"
ITUNES_SONG = "https://itunes.apple.com/search?term={q}&media=music&entity=song&country={cc}&limit=15"
ITUNES_ARTIST = ("https://itunes.apple.com/search?term={q}&media=music&entity=musicArtist"
                 "&attribute=artistTerm&country={cc}&limit=5")
DEEZER_SEARCH = "https://api.deezer.com/search/track?q={q}&limit=15"
DEEZER_ALBUM = "https://api.deezer.com/album/{id}"
YOUTUBE_WATCH = "https://www.youtube.com/watch?v={vid}"
SPY_APP = "https://steamspy.com/api.php?request=appdetails&appid={appid}"
STORE_APP = "https://store.steampowered.com/api/appdetails?appids={appid}&l=english&filters=genres,basic"
STORE_SEARCH = "https://store.steampowered.com/api/storesearch/?term={q}&cc=us&l=english"

# source names (shown to the user: "iTunes: Turkish Rock") and how far one hit is trusted
SRC_ITUNES, SRC_DEEZER, SRC_YOUTUBE, SRC_MB = "iTunes", "Deezer", "YouTube etiketleri", "MusicBrainz"
SOURCE_TRUST = {SRC_ITUNES: 0.85, SRC_DEEZER: 0.8, SRC_YOUTUBE: 0.65}
OTHER_TRACK_TRUST = 0.6       # artist entry from the artist's OTHER songs in a store

# YouTube categories that say something about the content (others: no evidence)
_YT_CATEGORIES = {"Comedy": "comedy", "News & Politics": "news & politics",
                  "Education": "education"}

_SEARCH_JUNK = re.compile(r"\b(oyunindir|vip|repack|fitgirl|dodi|elamigos|crack(ed)?|"
                          r"www|com|net|org|torrent|full|tr|türkçe)\b", re.I)


def youtube_video_id(url: str) -> str:
    """Video id of a YouTube / YouTube Music URL ('' if none). Only this id is sent."""
    host = _host(url or "")
    if not any(host == h or host.endswith("." + h) for h in _YT_HOSTS):
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    vid = ""
    if host == "youtu.be":
        vid = parts.path.strip("/").split("/")[0]
    else:
        vid = (urllib.parse.parse_qs(parts.query).get("v") or [""])[0]
        if not vid:
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", parts.path)
            vid = m.group(1) if m else ""
    return vid if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or "") else ""


def _json_str(page: str, key: str) -> str:
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % re.escape(key), page)
    if not m:
        return ""
    try:
        return json.loads(f'"{m.group(1)}"')
    except ValueError:
        return m.group(1)


def parse_youtube_page(page: str) -> dict:
    """{"keywords": [...], "category", "author", "title"} from a watch page ('' / [] if absent)."""
    out = {"keywords": [], "category": "", "author": "", "title": ""}
    if not isinstance(page, str):
        return out
    i = page.find('"videoDetails"')
    seg = page[i:i + 200_000] if i >= 0 else page
    m = re.search(r'"keywords"\s*:\s*(\[(?:[^\[\]"]|"(?:[^"\\]|\\.)*")*\])', seg)
    if m:
        try:
            kw = json.loads(m.group(1))
            out["keywords"] = [str(k) for k in kw if isinstance(k, str)][:40]
        except ValueError:
            pass
    out["author"] = _json_str(seg, "author") or _json_str(page, "ownerChannelName")
    out["title"] = _json_str(seg, "title")
    out["category"] = _json_str(page, "category")
    return out


class _Negative(Exception):
    """Lookup worked, but nothing usable was found (cache as negative)."""


# ============================================================================
#  GenreResolver
# ============================================================================

_HINT_STRIP = " \t()[]{}-–—|:,"     # context chars around a decoration hint (shown text)


def _compile_hints(hints: dict[str, str] | None) -> list[tuple[re.Pattern, str]]:
    out = []
    for pat, g in (hints or {}).items():
        if g not in MUSIC_GROUPS:
            log.warning("başlık ipucu atlandı: '%s' → bilinmeyen grup '%s'", pat, g)
            continue
        try:
            out.append((re.compile(pat, re.I), g))
        except re.error as e:
            log.warning("başlık ipucu atlandı: '%s' (%s)", pat, e)
    return out


def title_hints_from_rules(rules: dict | None) -> dict[str, str]:
    """TITLE_HINTS + kurallar.toml ``[tur] baslik_ipucu`` (user entries win)."""
    user = ((rules or {}).get("tur") or {}).get("baslik_ipucu")
    return {**TITLE_HINTS, **({str(k): str(v) for k, v in user.items()} if isinstance(user, dict) else {})}


_BUILTIN_INDEX: dict[str, str] = {norm(a): g for g, names in BUILTIN_ARTISTS.items() for a in names}


# ============================================================================
#  BUILT-IN ARTIST DATABASE  (Wikidata, CC0; generated by tools/sanatci_veritabani.py)
#  oto_eq/data/sanatcilar.json.gz: {"surum": 1, "gruplar": [...], "turler": [...],
#  "s": {<_fold(artist name)>: packed int}}. Loaded lazily on the first lookup; a
#  missing or corrupt file silently disables it (lookups answer None).
#  Two tiers (measured, docs/NOTES-tur-repertuar.md): "sure" entries answer at once like
#  the hand-picked list (no network); the others are only a fallback when the online
#  sources found nothing, failed, or are switched off. Turkish artists need a higher bar:
#  Wikidata often files arabesk singers under "Turkish folk music", while the iTunes TR
#  store names Arabesque / Halk / Sanat / Turkish Rock reliably.
# ============================================================================
DB_PATH = Path(__file__).resolve().parent / "data" / "sanatcilar.json.gz"
SRC_DB = "Yerleşik veritabanı (Wikidata)"
DB_SURE = 0.6                 # non-Turkish: confidence ≥ this → before the online sources
DB_SURE_TR = 0.85             # Turkish artists: the same, with this bar
DB_CONF_CAP = 0.85            # GenreResult.confidence of a sure entry (hand list: 0.9)
DB_CUT_CONF = 0.4             # cap for a cut first name's entry ("Simon" of "Simon & Garfunkel")


def pack_db_value(group_index: int, conf100: int, turkish: bool, alias: bool,
                  genre1: int = 0, genre2: int = 0) -> int:
    """One "s" value: group | conf<<3 | turkish<<10 | alias<<11 | genre1<<12 | genre2<<28
    (genre = index into "turler" + 1, 0 = none). Shared with tools/sanatci_veritabani.py."""
    return ((group_index & 7) | (max(0, min(100, int(conf100))) << 3) | (int(bool(turkish)) << 10)
            | (int(bool(alias)) << 11) | ((genre1 & 0xFFFF) << 12) | ((genre2 & 0xFFFF) << 28))


class DBEntry(tuple):
    """(group, confidence 0..1, turkish, alias, genre labels)."""
    __slots__ = ()

    def __new__(cls, group: str, confidence: float, turkish: bool, alias: bool,
                genres: tuple[str, ...]):
        return tuple.__new__(cls, (group, confidence, turkish, alias, genres))

    group = property(lambda self: self[0])
    confidence = property(lambda self: self[1])
    turkish = property(lambda self: self[2])
    alias = property(lambda self: self[3])
    genres = property(lambda self: self[4])

    @property
    def sure(self) -> bool:
        """Answered before the online sources. A key that is only an alias (pseudonym,
        alternative name: "Chopin", "Sen") never is: aliases are often ordinary words.
        Nor is an entry without any stored genre label: its group came only from an
        occupation / birth year (Wikidata P136 held a NON-music genre: comedians, actors…
        "Trevor Noah" → elektronik). Those stay a weak, offline-only answer."""
        return (not self[3] and bool(self[4])
                and self[1] >= (DB_SURE_TR if self[2] else DB_SURE))


class ArtistDB:
    """Offline artist → music group table (lazy, thread-safe, never raises)."""

    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._table: dict[str, int] | None = None
        self._groups: tuple[str, ...] = ()
        self._genres: tuple[str, ...] = ()
        self.meta: dict = {}
        self.load_seconds = 0.0

    def _ensure(self) -> dict[str, int]:
        t = self._table
        if t is not None:
            return t
        with self._lock:
            if self._table is None:
                self._table = self._read()
            return self._table

    def _read(self) -> dict[str, int]:
        t0 = time.monotonic()
        try:
            import gzip
            with gzip.open(self.path, "rb") as f:
                data = json.loads(f.read().decode("utf-8"))
            groups = data.get("gruplar")
            genres = data.get("turler")
            table = data.get("s")
            if not (isinstance(groups, list) and all(g in MUSIC_GROUPS for g in groups)
                    and isinstance(genres, list) and isinstance(table, dict)):
                raise ValueError("beklenmeyen biçim")
            self._groups = tuple(groups)
            self._genres = tuple(str(g) for g in genres)
            self.meta = {k: data.get(k) for k in ("surum", "kaynak", "uretim")}
            self.meta["anahtar"] = len(table)
        except FileNotFoundError:
            log.info("Sanatçı veritabanı yok (%s): devre dışı", self.path)
            return {}
        except Exception as e:                   # corrupt gzip/JSON: disabled, never fatal
            log.warning("Sanatçı veritabanı okunamadı (%s): %s", self.path, e)
            return {}
        self.load_seconds = time.monotonic() - t0
        log.debug("Sanatçı veritabanı: %d ad, %.0f ms", len(table), self.load_seconds * 1000)
        return table

    def preload(self) -> None:
        """Load in a background thread (optional; the first lookup loads it otherwise)."""
        threading.Thread(target=self._ensure, name="oto-eq-sanatci-db", daemon=True).start()

    def __len__(self) -> int:
        return len(self._ensure())

    def lookup(self, *names: str) -> DBEntry | None:
        table = self._ensure()
        if not table:
            return None
        for n in names:
            k = _fold(n) if n else ""
            v = table.get(k) if k else None
            if not isinstance(v, int) or v < 0:
                continue
            gi = v & 7
            if gi >= len(self._groups):
                continue
            gs = tuple(self._genres[i - 1] for i in ((v >> 12) & 0xFFFF, (v >> 28) & 0xFFFF)
                       if 0 < i <= len(self._genres))
            return DBEntry(self._groups[gi], min(100, (v >> 3) & 127) / 100.0,
                           bool((v >> 10) & 1), bool((v >> 11) & 1), gs)
        return None


_default_db: ArtistDB | None = None
# own lock: shared_resolver() holds _shared_lock while GenreResolver() calls default_db()
_db_lock = threading.Lock()


def default_db() -> ArtistDB:
    """The bundled database (one per process, loaded on first use)."""
    global _default_db
    with _db_lock:
        if _default_db is None:
            _default_db = ArtistDB(DB_PATH)
        return _default_db

_shared: "GenreResolver | None" = None
_shared_lock = threading.Lock()


def shared_resolver() -> "GenreResolver":
    """Process-wide resolver (router + classifier share one cache and worker)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = GenreResolver()
        return _shared


def _t(e: dict) -> float:
    try:
        return float(e.get("t", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


class GenreResolver:
    """override → title hint → built-in list → sure database entry → cache (song, artist)
    → background fetch → audio memory → weak database entry. Thread-safe."""

    def __init__(self, settings: dict | None = None, cache_path: Path | str | None = None,
                 fetch: Callable[[str], Any] | None = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 title_hints: dict[str, str] | None = None, builtin: bool = True,
                 fetch_text: Callable[[str], str] | None = None,
                 db: "ArtistDB | bool | None" = None):
        self.settings = settings if settings is not None else config.load_genre_settings()
        self._hints = _compile_hints(TITLE_HINTS if title_hints is None else title_hints)
        self._builtin = _BUILTIN_INDEX if builtin else {}
        # db: None = the bundled database when ``builtin`` is on; False = none
        self._db: ArtistDB | None = (db if isinstance(db, ArtistDB) else
                                     default_db() if (db is True or (db is None and builtin))
                                     else None)
        self.cache_path = Path(cache_path) if cache_path is not None else Path(config.GENRE_CACHE)
        self._fetch = fetch or http_get_json
        # pages (YouTube): an injected ``fetch`` serves them too unless ``fetch_text`` is given
        self._fetch_text = fetch_text or (fetch if fetch is not None else http_get_text)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.RLock()
        self._cache: dict[str, dict] | None = None
        self._dropped: set[str] = set()           # keys forgotten since the last save
        self._dirty: set[str] = set()             # keys written since the last save
        self._pending: set[str] = set()           # lookup keys (track key, else cid.key)
        self._waiters: dict[str, list] = {}       # lookup key -> [(cid, callback)] asked while pending
        self._errors: dict[str, float] = {}
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_req: dict[str, float] = {}     # host -> monotonic time of the last request
        self.requests = 0                          # network requests made (diagnostics)

    # ------------------------------------------------------------ settings
    @property
    def online(self) -> bool:
        return bool(self.settings.get("cevrimici", True))

    def set_settings(self, settings: dict) -> None:
        with self._lock:
            self.settings = settings

    def set_title_hints(self, hints: dict[str, str]) -> None:
        compiled = _compile_hints(hints)
        with self._lock:
            self._hints = compiled

    def _title_hint(self, cid: ContentId) -> GenreResult | None:
        if cid.kind != "muzik" or not cid.track:
            return None
        t = cid.track.lower()
        for rx, g in self._hints:
            # lower-cased first (shown text as before); the original too, for the few
            # case-sensitive parts ("in A major" vs "Living In A Major City")
            m = rx.search(t) or rx.search(cid.track)
            if m:
                return GenreResult(g, f"şarkı adı: “{m.group(0).strip(_HINT_STRIP)}”", 0.8)
        return None

    def _builtin_result(self, cid: ContentId) -> GenreResult | None:
        if cid.kind != "muzik":
            return None
        g = self._builtin.get(cid.key.split(":", 1)[-1]) or self._builtin.get(norm(cid.display))
        return GenreResult(g, "yerleşik liste", 0.9) if g else None

    def _db_entry(self, cid: ContentId) -> DBEntry | None:
        if cid.kind != "muzik" or self._db is None:
            return None
        try:
            return self._db.lookup(cid.key.split(":", 1)[-1], cid.display)
        except Exception:
            log.exception("sanatçı veritabanı sorgusu başarısız")
            return None

    def _db_result(self, cid: ContentId, sure: bool) -> GenreResult | None:
        """``sure``: only sure entries (answered before the network); otherwise any entry
        (fallback after the online sources), the weak ones marked as such.
        A CUT name (content_id() kept "Simon" of "Simon & Garfunkel", "Down" of "Down with
        Webster") is often another artist's sure entry: the whole name is tried first (a
        sure entry of it counts), the cut first name only ever in the weak tier."""
        full = cut_name(cid)
        cut = False
        e = None
        if full and self._db is not None:
            try:
                e = self._db.lookup(full)
            except Exception:
                log.exception("sanatçı veritabanı sorgusu başarısız")
        if e is None:
            e = self._db_entry(cid)
            cut = bool(full)
        if e is None:
            return None
        is_sure = e.sure and not cut
        if sure and not is_sure:
            return None
        detail = ", ".join(e.genres)
        conf = min(DB_CONF_CAP, e.confidence)
        if cut:
            detail = (f"{detail} (zayıf, kesik ad: {cid.display})" if detail
                      else f"zayıf, kesik ad: {cid.display}")
            conf = min(conf, DB_CUT_CONF)
        elif not is_sure:
            detail = f"{detail} (zayıf)" if detail else "zayıf"
        src = f"{SRC_DB}: {detail}" if detail else SRC_DB
        return GenreResult(e.group, src, round(conf, 2))

    def _override(self, cid: ContentId) -> GenreResult | None:
        table = self.settings.get("sanatci" if cid.kind == "muzik" else "oyun") or {}
        valid = MUSIC_GROUPS if cid.kind == "muzik" else GAME_GROUPS
        bare = cid.key.split(":", 1)[-1]
        for k, g in table.items():
            if k == cid.key or norm(k) == bare or norm(k) == norm(cid.display):
                g = str(g)
                if g == "" or g in valid:
                    return GenreResult(g, "kullanıcı", 1.0)
        return None

    # ------------------------------------------------------------ cache file
    def _read_file(self) -> dict[str, dict] | None:
        """Entries on disk; None if there is no file. A corrupt file is moved aside."""
        try:
            data = json.loads(self.cache_path.read_text("utf-8"))
            items = data.get("kayitlar", {}) if isinstance(data, dict) else None
            if not isinstance(items, dict):
                raise ValueError("kayitlar bir nesne değil")
            return {str(k): v for k, v in items.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("Tür önbelleği bozuk, sıfırlanıyor: %s", e)
            try:
                self.cache_path.replace(self.cache_path.with_name(self.cache_path.name + ".bozuk"))
            except OSError:
                pass
            return {}

    def _load(self) -> dict[str, dict]:
        with self._lock:
            if self._cache is None:
                self._cache = self._read_file() or {}
            return self._cache

    def _save(self) -> None:
        """Merge with the file under an flock (router, classifier, GUI or CLI processes may
        each hold a resolver): the file's state + only OUR changes since the last save
        (newer entry wins per key, forgotten keys removed). A stale in-memory copy never
        clobbers another process' results, and a file deleted meanwhile ("Tür önbelleğini
        sil") stays cleared except for what we stored after that."""
        with self._lock:
            mine = self._load()
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                lock_path = self.cache_path.with_name(f".{self.cache_path.name}.lock")
                with open(lock_path, "a+") as lf:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                    try:
                        merged = self._read_file() or {}
                        for k in self._dropped:
                            merged.pop(k, None)
                        for k in self._dirty:
                            v, o = mine.get(k), merged.get(k)
                            if v is not None and (o is None or _t(v) >= _t(o)):
                                merged[k] = v
                        tmp = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.tmp")
                        tmp.write_text(json.dumps({"surum": 1, "kayitlar": merged},
                                                  ensure_ascii=False, indent=1), "utf-8")
                        tmp.replace(self.cache_path)
                    finally:
                        fcntl.flock(lf, fcntl.LOCK_UN)
            except OSError as e:
                log.warning("Tür önbelleği yazılamadı: %s", e)
                return
            self._cache = merged
            self._dropped.clear()
            self._dirty.clear()

    def _store(self, key: str, tags, source: str, name: str = "", trust: float | None = None,
               extra: dict | None = None) -> None:
        e = {"tags": _tag_items(tags)[:25], "source": source, "t": self._clock(),
             "name": name, "negatif": not tags}
        if trust is not None:
            e["guven"] = trust
        if extra:
            e.update(extra)
        with self._lock:
            c = self._load()
            c[key] = e
            self._dropped.discard(key)
            self._dirty.add(key)
            if key.startswith("sanatci:") and self._entry_result("muzik", e).group:
                # a name-based answer for the artist replaces the audio-analysis memory
                if c.pop("ses:" + key, None) is not None:
                    self._dropped.add("ses:" + key)
                    self._dirty.discard("ses:" + key)
        self._save()

    def _fresh(self, key: str) -> dict | None:
        """Cache entry if present and not expired."""
        if not key:
            return None
        with self._lock:
            e = self._load().get(key)
        if not e:
            return None
        ttl = AUDIO_TTL if key.startswith("ses:") else NEGATIVE_TTL if e.get("negatif") else POSITIVE_TTL
        return e if self._clock() - _t(e) <= ttl else None

    @staticmethod
    def _entry_result(kind: str, e: dict) -> GenreResult:
        src = str(e.get("source") or "çevrimiçi")
        tags = e.get("tags") or []
        if e.get("negatif") or not tags:
            return GenreResult("", f"{src}: tür bulunamadı", 0.0)
        group = map_tags(kind, tags)
        names = [t[0] if isinstance(t, (list, tuple)) else str(t) for t in tags]
        if not group and kind == "muzik" and any(_WEAK_TURKISH.search(n) for n in names):
            # only "turkish" and no genre tag (e.g. Sezen Aksu): weak pop guess
            return GenreResult("pop_vokal", f"{src}: {', '.join(names[:3])} (zayıf ipucu)", 0.4)
        shown = [n for n in names if group and score_tags(kind, [n]).get(group, 0.0) > 0][:3] \
            or names[:3]
        conf = _confidence(kind, tags, group)
        if group and isinstance(e.get("guven"), (int, float)):
            conf = round(min(conf, float(e["guven"])), 2)
        return GenreResult(group, f"{src}: {', '.join(shown)}" if shown else src, conf)

    def cached(self, cid: ContentId | None) -> GenreResult | None:
        """Sync, no network. Order: user override → title hint → built-in list → sure
        database entry → song entry → artist entry → audio memory → weak database entry
        → negative. None = unknown yet (a song that was not looked up yet counts as
        unknown while online, even if the artist was)."""
        if cid is None:
            return None
        try:
            for r in (self._override(cid), self._title_hint(cid), self._builtin_result(cid)):
                if r is not None:
                    return r
            if cid.kind == "muzik":
                r = self._db_result(cid, sure=True)
                if r is not None:
                    return r
            if cid.kind != "muzik":
                e = self._fresh(cid.key)
                return self._entry_result(cid.kind, e) if e else None
            tkey = track_key(cid)
            te = self._fresh(tkey)
            ae = self._fresh(cid.key)
            negative = None
            if te is not None:
                r = self._entry_result("muzik", te)
                if r.group:
                    return r
                negative = r
            song_unknown = bool(self.online and tkey and te is None and not self._failed(tkey))
            if ae is not None:
                r = self._entry_result("muzik", ae)
                if r.group:
                    if song_unknown and ae.get("source") == SRC_MB:
                        # MusicBrainz matched the NAME only (live 25 Eki: "SAFI" → a Leipzig
                        # indie trio): verify once by this song; a song hit replaces it
                        return None
                    return r
                negative = negative or r
            if song_unknown or (self.online and not tkey and ae is None
                                and not self._failed(cid.key)):
                return None                     # not looked up yet
            # the user's own audio memory for this artist (remember_audio) beats a weak
            # database entry; the weak entry only when neither online nor memory knows
            am = self._fresh("ses:" + cid.key)
            if am and am.get("group") in MUSIC_GROUPS:
                return GenreResult(am["group"], str(am.get("source") or "ses analizi"),
                                   float(am.get("confidence", 0.5) or 0.5))
            weak = self._db_result(cid, sure=False)
            if weak is not None:
                return weak
            return negative
        except Exception:
            log.exception("tür önbelleği okunamadı")
            return None

    def remember_audio(self, cid: ContentId | None, group: str, confidence: float | None = None,
                       detail: str = "") -> bool:
        """Router (CHANGE 4): an audio-analysis guess for this ARTIST (~25 s into a song
        without a name-based genre), used for the artist's next songs only when no
        name-based result exists; a later positive online artist result replaces it.
        Local only: nothing is sent anywhere. Returns True if stored."""
        if cid is None or cid.kind != "muzik" or group not in MUSIC_GROUPS:
            return False
        try:
            conf = min(1.0, max(0.0, float(confidence))) if confidence is not None else 0.5
        except (TypeError, ValueError):
            conf = 0.5
        d = str(detail or "").strip()
        if not d.startswith("ses analizi"):
            d = f"ses analizi: {d}" if d else "ses analizi"
        key = "ses:" + cid.key
        with self._lock:
            self._load()[key] = {"group": group, "confidence": round(conf, 2), "source": d,
                                 "t": self._clock(), "name": cid.display, "parca": cid.track}
            self._dropped.discard(key)
            self._dirty.add(key)
        self._save()
        log.info("Ses analizi hatırlandı: %s → %s (%s)", cid.display, group, d)
        return True

    def known_artist(self, name: str, request: bool = False) -> bool | None:
        """True = an online source knows this exact artist (or the user assigned a music
        group), False = looked up, not found, None = unknown yet. Cache only; with
        ``request`` an unknown name is queued for a background lookup."""
        key = norm(clean_artist(name))
        if not key:
            return None
        cid = ContentId("muzik", f"sanatci:{key}", clean_artist(name))
        ov = self._override(cid) or self._builtin_result(cid)
        if ov is None:
            e = self._db_entry(cid)            # a sure, non-alias database name is music
            if e is not None and not e.alias and e.sure:
                return True
        if ov is not None:
            return ov.group != "konusma"
        e = self._fresh(cid.key)
        if e is not None:
            if e.get("negatif"):
                return False
            n = str(e.get("name", ""))
            return norm(n) == key or artist_matches(n, cid.display)
        if request:
            self.request(cid, lambda c, r: None)
        return None

    def invalidate(self) -> None:
        """Re-read the cache file on next use (the GUI may have deleted it)."""
        with self._lock:
            self._cache = None
            self._dropped.clear()
            self._dirty.clear()

    def forget(self, key: str) -> None:
        with self._lock:
            if self._load().pop(key, None) is None:
                return
            self._dropped.add(key)
            self._dirty.discard(key)
        self._save()

    # ------------------------------------------------------------ background
    @staticmethod
    def _lookup_key(cid: ContentId) -> str:
        return track_key(cid) or cid.key

    def _failed(self, lookup_key: str) -> bool:
        """The lookup failed (network) within ERROR_RETRY: it will not be retried yet."""
        with self._lock:
            t = self._errors.get(lookup_key)
        return t is not None and time.monotonic() - t < ERROR_RETRY

    def request(self, cid: ContentId | None, callback: Callable[[ContentId, GenreResult], None]
                ) -> bool:
        """Resolve in the background; ``callback(cid, result)`` on the worker thread.

        Returns False (and does nothing) if offline mode, already known, or
        recently failed. A known result is NOT re-delivered (use cached()).
        The same song/artist already being looked up is not fetched twice:
        ``callback`` joins that lookup."""
        if cid is None or not self.online:
            return False
        if self.cached(cid) is not None:
            return False
        lk = self._lookup_key(cid)
        with self._lock:
            if lk in self._pending:
                self._waiters.setdefault(lk, []).append((cid, callback))
                return True
            if self._failed(lk):
                return False
            self._pending.add(lk)
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="oto-eq-tur", daemon=True)
                self._thread.start()
        self._q.put((cid, callback))
        return True

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(3.0)
        self._thread = None

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Tests/tools: wait until no request is pending."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                if not self._pending:
                    return True
            time.sleep(0.01)
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._q.get()
            if item is None:
                break
            cid, callback = item
            lk = self._lookup_key(cid)
            try:
                if not self.online:
                    continue
                # answered meanwhile (e.g. by the artist lookup queued just before)
                res = self.cached(cid) or self._resolve(cid)
            except Exception as e:
                log.warning("Tür sorgusu başarısız (%s): %s", cid.display, e)
                with self._lock:
                    self._errors[lk] = time.monotonic()
                # network error: still deliver what is known offline (audio memory, weak
                # database entry); cached() no longer waits for this failed lookup
                res = self.cached(cid)
            finally:
                with self._lock:
                    self._pending.discard(lk)
                    waiters = self._waiters.pop(lk, [])
            if res is None:
                continue
            for c, cb in [(cid, callback)] + waiters:
                try:
                    cb(c, res if c == cid else (self.cached(c) or res))
                except Exception:
                    log.exception("tür geri çağrısı hata verdi")

    def _get(self, url: str, text: bool = False) -> Any:
        """Per-host rate-limited fetch; HTTP 503/429 ("server busy") is retried, never cached."""
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        interval = HOST_INTERVALS.get(host, MIN_INTERVAL)
        for attempt in range(len(BUSY_RETRIES) + 1):
            wait = interval - (time.monotonic() - self._last_req.get(host, -1e9))
            if wait > 0:
                self._sleep(wait)
            self._last_req[host] = time.monotonic()
            self.requests += 1
            try:
                return self._fetch_text(url) if text else self._fetch(url)
            except urllib.error.HTTPError as e:
                if e.code not in (429, 503) or attempt == len(BUSY_RETRIES) or self._stop.is_set():
                    raise
                log.debug("sunucu meşgul (%s %s), %.1f sn sonra tekrar", host, e.code,
                          BUSY_RETRIES[attempt])
                self._sleep(BUSY_RETRIES[attempt])
        raise RuntimeError("unreachable")

    def _resolve(self, cid: ContentId) -> GenreResult | None:
        if cid.kind == "muzik":
            return self._resolve_music(cid)
        try:
            tags, source, name = self._steam(cid)
        except _Negative as e:
            self._store(cid.key, [], str(e) or "çevrimiçi", cid.display)
            log.info("Tür bulunamadı: %s (%s)", cid.display, e)
            return self.cached(cid)
        self._store(cid.key, tags, source, name or cid.display)
        res = self.cached(cid)
        if res is not None:
            log.info("Tür: %s → %s (%s)", cid.display, res.group or "genel", res.source)
        return res

    # ---- music: song level first, artist level second
    def _resolve_music(self, cid: ContentId) -> GenreResult | None:
        tkey = track_key(cid)
        hit = None
        evidence: list[tuple[list, str, str]] = []   # same-name artist's OTHER songs
        tried: list[str] = []
        errors: list[Exception] = []
        if tkey and self._fresh(tkey) is None:
            for label, fn in self._track_sources(cid):
                if self._stop.is_set():
                    break
                if label == "iTunes US" and any(src == SRC_ITUNES for _t, src, _n in evidence):
                    continue                     # the TR store knows the artist already
                try:
                    h, ev = fn(cid)
                except Exception as e:           # one source failing must not stop the others
                    log.debug("%s sorgusu başarısız (%s): %s", label, cid.display, e)
                    errors.append(e)
                    continue
                name = SRC_ITUNES if label == "iTunes US" else label
                if name not in tried:
                    tried.append(name)
                evidence.extend(ev)
                if h is not None:
                    hit = h
                    break
            if hit is not None:
                tags, src, nm = hit
                self._store(tkey, tags, src, nm, trust=SOURCE_TRUST.get(src),
                            extra={"parca": cid.track})
            elif tried:
                self._store(tkey, [], ", ".join(tried), cid.display, extra={"parca": cid.track})
        ae = self._fresh(cid.key)
        if ae is None or not self._entry_result("muzik", ae).group or (
                hit is not None and ae.get("source") == SRC_MB):
            if hit is not None:                  # the song's genre also stands for the artist
                tags, src, nm = hit
                self._store(cid.key, tags, src, nm, trust=SOURCE_TRUST.get(src),
                            extra={"parca": cid.track})
            elif ae is None:
                self._resolve_artist(cid, evidence)
        res = self.cached(cid)
        if res is None and errors:
            raise errors[-1]
        if res is not None:
            log.info("Tür: %s – %s → %s (%s)", cid.display, cid.track or "-", res.group or "genel",
                     res.source)
        return res

    def _track_sources(self, cid: ContentId) -> list[tuple[str, Callable]]:
        out: list[tuple[str, Callable]] = [(SRC_ITUNES, lambda c: self._itunes_track(c, "TR")),
                                           (SRC_DEEZER, self._deezer_track)]
        if youtube_video_id(cid.url):
            out.append((SRC_YOUTUBE, self._youtube))
        out.append(("iTunes US", lambda c: self._itunes_track(c, "US")))
        return out

    def _resolve_artist(self, cid: ContentId, evidence: list) -> None:
        """MusicBrainz artist; without genre there: the artist's other songs in a store,
        then (no song title known) the iTunes artist search. Network errors propagate."""
        negative = ""
        try:
            tags, source, name = self._musicbrainz(cid)
            self._store(cid.key, tags, source, name)
            if map_tags("muzik", tags):
                return
        except _Negative as e:
            negative = str(e) or SRC_MB
        for tags, src, name in evidence:
            if map_tags("muzik", tags):
                self._store(cid.key, tags, f"{src} (sanatçının diğer şarkıları)", name,
                            trust=OTHER_TRACK_TRUST)
                return
        if not cid.track:
            try:
                found = self._itunes_artist(cid)
            except Exception as e:
                log.debug("iTunes sanatçı araması başarısız (%s): %s", cid.display, e)
                found = None
            if found is not None:
                tags, src, name = found
                self._store(cid.key, tags, src, name, trust=SOURCE_TRUST.get(src))
                return
        if negative:
            self._store(cid.key, [], negative, cid.display)

    # ---- iTunes Search API (no key; ~20 requests/min)
    def _itunes_track(self, cid: ContentId, country: str) -> tuple[tuple | None, list]:
        term = f"{cid.display} {clean_track(cid.track)}"
        data = self._get(ITUNES_SONG.format(q=urllib.parse.quote(term), cc=country))
        results = data.get("results") if isinstance(data, dict) else None
        same: dict[str, float] = {}
        other: dict[str, float] = {}
        name = other_name = ""
        for r in results or []:
            if not isinstance(r, dict) or r.get("kind") not in (None, "song"):
                continue
            artist, genre = str(r.get("artistName") or ""), str(r.get("primaryGenreName") or "")
            if not genre or not artist_matches(artist, cid.display):
                continue                         # another artist (name-mismatch guard)
            if track_matches(str(r.get("trackName") or ""), cid.track):
                same[genre] = same.get(genre, 0.0) + 1.0
                name = name or artist
            else:
                other[genre] = other.get(genre, 0.0) + 1.0
                other_name = other_name or artist
        hit = None
        if same and map_tags("muzik", same):
            hit = (sorted(same.items(), key=lambda kv: -kv[1]), SRC_ITUNES, name)
        ev = [(sorted(other.items(), key=lambda kv: -kv[1]), SRC_ITUNES, other_name)] if other else []
        return hit, ev

    def _itunes_artist(self, cid: ContentId) -> tuple | None:
        data = self._get(ITUNES_ARTIST.format(q=urllib.parse.quote(cid.display), cc="TR"))
        for r in (data.get("results") if isinstance(data, dict) else None) or []:
            if not isinstance(r, dict):
                continue
            artist, genre = str(r.get("artistName") or ""), str(r.get("primaryGenreName") or "")
            if genre and _fold(artist) == _fold(cid.display) and map_tags("muzik", [genre]):
                return [(genre, 1.0)], SRC_ITUNES, artist
        return None

    # ---- Deezer API (no key; 50 requests / 5 s): track search → album genres
    def _deezer_track(self, cid: ContentId) -> tuple[tuple | None, list]:
        term = f"{cid.display} {clean_track(cid.track)}"
        data = self._get(DEEZER_SEARCH.format(q=urllib.parse.quote(term)))
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Deezer: {data['error']}")      # quota etc.: error, not negative
        album = other = None
        for t in (data.get("data") if isinstance(data, dict) else None) or []:
            if not isinstance(t, dict):
                continue
            artist = str((t.get("artist") or {}).get("name") or "")
            alb = (t.get("album") or {}).get("id")
            if not alb or not artist_matches(artist, cid.display):
                continue
            if track_matches(str(t.get("title") or ""), cid.track):
                album = (alb, artist)
                break
            other = other or (alb, artist)
        target = album or other
        if target is None:
            return None, []
        det = self._get(DEEZER_ALBUM.format(id=urllib.parse.quote(str(target[0]))))
        if isinstance(det, dict) and det.get("error"):
            raise RuntimeError(f"Deezer: {det['error']}")
        genres = [str(g.get("name")) for g in
                  (((det.get("genres") or {}).get("data") if isinstance(det, dict) else None) or [])
                  if isinstance(g, dict) and g.get("name")]
        tags = [(g, 1.0) for g in genres]
        if not tags or not map_tags("muzik", tags):
            return None, []
        if album is not None:
            return (tags, SRC_DEEZER, target[1]), []
        return None, [(tags, SRC_DEEZER, target[1])]

    # ---- YouTube watch page (only the video id is sent; uploader keywords + category)
    def _youtube(self, cid: ContentId) -> tuple[tuple | None, list]:
        vid = youtube_video_id(cid.url)
        if not vid:
            return None, []
        info = parse_youtube_page(self._get(YOUTUBE_WATCH.format(vid=vid), text=True))
        title, author = info["title"], info["author"]
        song, who = _fold(clean_track(cid.track)), _fold(cid.display)
        # guard: the page must be this song by this artist (the URL may belong to another tab)
        if not song or song not in _fold(title) or not (
                re.search(rf"(^|\s){re.escape(who)}(\s|$)", _fold(title))
                or artist_matches(author, cid.display)
                or artist_matches(clean_artist(author, channel=True), cid.display)):
            log.debug("YouTube sayfası bu şarkı değil (%s / %s)", title, author)
            return None, []
        skip = {who, song, _fold(cid.track)}
        tags = [(k, 1.0) for k in info["keywords"] if _fold(k) not in skip]
        if info["category"] in _YT_CATEGORIES:
            tags.append((_YT_CATEGORIES[info["category"]], 1.0))
        group = map_tags("muzik", tags)
        if not group:
            return None, []
        ranked = sorted(score_tags("muzik", tags).values(), reverse=True)
        if len(ranked) > 1 and ranked[1] >= 0.75 * ranked[0]:
            log.debug("YouTube etiketleri kararsız (%s): %s", cid.display, ranked[:2])
            return None, []
        return (tags, SRC_YOUTUBE, author or cid.display), []

    # ---- MusicBrainz
    def _musicbrainz(self, cid: ContentId) -> tuple[list, str, str]:
        name = cid.display or cid.key.split(":", 1)[-1]
        q = urllib.parse.quote(f'artist:"{name}"')
        data = self._get(MB_SEARCH.format(q=q))
        artists = data.get("artists") if isinstance(data, dict) else None
        if not artists:
            raise _Negative(SRC_MB)
        want = norm(name)

        def rank(a):
            exact = norm(a.get("name", "")) == want or any(
                norm(x.get("name", "")) == want for x in a.get("aliases", []) or [])
            return (exact, int(a.get("score", 0) or 0),
                    sum(t.get("count", 0) for t in a.get("tags", []) or []), a.get("country") == "TR")
        best = max(artists, key=rank)
        # an inexact hit ("Tarkan" → "Tarkan Çakır", score 76) is another artist
        if not rank(best)[0] or int(best.get("score", 0) or 0) < 80:
            raise _Negative(SRC_MB)
        tags = [(t.get("name", ""), t.get("count", 1)) for t in best.get("tags", []) or []]
        if best.get("disambiguation"):          # "Turkish nu metal band": weak extra tag
            tags.append((str(best["disambiguation"]), 0.7))
        genres: list = []
        if sum(c for _, c in tags) < 3 and best.get("id"):
            det = self._get(MB_LOOKUP.format(mbid=urllib.parse.quote(best["id"])))
            if isinstance(det, dict):
                genres = [(g.get("name", ""), g.get("count", 1) * 1.5) for g in det.get("genres", []) or []]
                tags = tags or [(t.get("name", ""), t.get("count", 1)) for t in det.get("tags", []) or []]
        merged: dict[str, float] = {}
        for t, c in genres + tags:
            if t:
                merged[t] = max(merged.get(t, 0.0), float(c or 1))
        if not merged:
            raise _Negative(SRC_MB)
        return sorted(merged.items(), key=lambda kv: -kv[1]), SRC_MB, best.get("name", name)

    # ---- Steam / SteamSpy
    def _steam(self, cid: ContentId) -> tuple[list, str, str]:
        appid = cid.key.split(":", 1)[1] if cid.key.startswith("steam:") else ""
        if not appid:
            appid = self._store_search(cid)
            if not appid:
                raise _Negative("Steam araması")
        tags: dict[str, float] = {}
        name = ""
        try:
            spy = self._get(SPY_APP.format(appid=appid))
        except Exception as e:
            log.debug("SteamSpy hatası: %s", e)
            spy = None
        if isinstance(spy, dict):
            name = str(spy.get("name") or "")
            t = spy.get("tags")
            if isinstance(t, dict):
                tags = {str(k): float(v) for k, v in t.items() if isinstance(v, (int, float))}
            if not tags and spy.get("genre"):
                tags = {g.strip(): 1.0 for g in str(spy["genre"]).split(",") if g.strip()}
        source = "SteamSpy"
        if not tags:
            store = self._get(STORE_APP.format(appid=appid))
            entry = store.get(str(appid)) if isinstance(store, dict) else None
            d = entry.get("data") if isinstance(entry, dict) and entry.get("success") else None
            if isinstance(d, dict):
                name = name or str(d.get("name") or "")
                tags = {g.get("description", ""): 1.0 for g in d.get("genres", []) or []
                        if g.get("description")}
            source = "Steam"
        if not tags:
            raise _Negative("Steam")
        return sorted(tags.items(), key=lambda kv: -kv[1]), source, name

    def _store_search(self, cid: ContentId) -> str:
        title = _SEARCH_JUNK.sub(" ", re.sub(r"[._]+", " ", cid.display or ""))
        title = re.sub(r"\s+", " ", title).strip()
        if len(title) < 3:
            return ""
        data = self._get(STORE_SEARCH.format(q=urllib.parse.quote(title)))
        items = data.get("items") if isinstance(data, dict) else None
        want = norm(title)
        for it in items or []:
            n = norm(str(it.get("name", "")))
            if n and (n == want or (len(want) >= 6 and (n.startswith(want) or want.startswith(n))
                                    and min(len(n), len(want)) / max(len(n), len(want)) >= 0.7)):
                return str(it.get("id", ""))
        return ""
