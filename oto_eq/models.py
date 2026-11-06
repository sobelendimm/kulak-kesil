"""Shared data types: the contract between modules.

Do NOT change field names/types without the coordinator. If you need extra
data, read it from the raw ``props`` dict or add a helper in your own module.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum

from .i18n import N_, _


class Category(str, Enum):
    CS2 = "cs2"
    GAME = "oyun"
    MUSIC = "muzik"
    MOVIE = "film"
    VOICE = "sesli"        # Discord, TeamSpeak, calls -> never EQ'd
    SYSTEM = "sistem"      # notification/event sounds, speech-dispatcher -> never EQ'd
    UNKNOWN = "bilinmeyen"


# Each EQ profile gets its own virtual sink named SINK_PREFIX + profile.
EQ_PROFILES: tuple[str, ...] = ("cs2", "oyun", "muzik", "film")
SINK_PREFIX = "oto_eq_"

# Flat, level-matched sink: streams that get no EQ (voice, system sounds,
# mode "off") pass through it so their loudness stays in balance with the
# attenuated EQ sinks. Not a classifier profile.
FLAT_PROFILE = "duz"
ALL_SINK_PROFILES: tuple[str, ...] = EQ_PROFILES + (FLAT_PROFILE,)

# Display order / priority when several profiles are active at once.
PROFILE_PRIORITY: tuple[str, ...] = ("cs2", "oyun", "film", "muzik")

# Turkish source names (msgids): generated config files (sink node.description / media.name)
# use these so they do not change with the UI language (WirePlumber keys state by media.name).
PROFILE_NAMES: dict[str, str] = {
    "cs2": "CS2",
    "oyun": N_("Oyun"),
    "muzik": N_("Müzik"),
    "film": N_("Film"),
    "duz": N_("Düz"),
}
class Etiketler(Mapping):
    """Read-only {key: label}; each access translates into the CURRENT process language
    (a long-running router can switch language: i18n.yeniden_yukle(), IPC "dil")."""

    def __init__(self, names: dict[str, str]):
        self._names = names

    def __getitem__(self, key: str) -> str:
        return _(self._names[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


PROFILE_LABELS: Mapping[str, str] = Etiketler(PROFILE_NAMES)   # UI language, at access time


def sink_name(profile: str) -> str:
    return SINK_PREFIX + profile


_OUR_SINKS = frozenset(sink_name(p) for p in ALL_SINK_PROFILES)


def is_eq_sink(name: str) -> bool:
    """True only for our own sinks (exact names, so test sinks don't match)."""
    return name in _OUR_SINKS


# Genre variants ("alt profil"): small live deltas on top of the muzik / oyun
# sinks, switched only at content boundaries (new song / new game).
VARIANT_PROFILES: tuple[str, ...] = ("muzik", "oyun")
MUSIC_GROUPS: tuple[str, ...] = ("metal_rock", "elektronik", "klasik_akustik", "pop_vokal", "konusma")
GAME_GROUPS: tuple[str, ...] = ("rekabetci", "korku_hayatta_kalma", "aksiyon_rpg",
                                "strateji_insa", "yaris_sim")
# Turkish source names (language independent: what is WRITTEN into profiller.toml defaults)
GROUP_NAMES: dict[str, str] = {
    "": N_("Genel"),
    "metal_rock": N_("Metal / Rock"),
    "elektronik": N_("Elektronik / Hip-hop"),
    "klasik_akustik": N_("Klasik / Caz / Akustik"),
    "pop_vokal": N_("Pop / Vokal"),
    "konusma": N_("Konuşma / Podcast"),
    "rekabetci": N_("Rekabetçi / FPS"),
    "korku_hayatta_kalma": N_("Korku / Hayatta kalma"),
    "aksiyon_rpg": N_("Aksiyon / RPG"),
    "strateji_insa": N_("Strateji / İnşa"),
    "yaris_sim": N_("Yarış / Simülasyon"),
}
GROUP_LABELS: Mapping[str, str] = Etiketler(GROUP_NAMES)   # UI language, at access time


def variant_groups(profile: str) -> tuple[str, ...]:
    return MUSIC_GROUPS if profile == "muzik" else GAME_GROUPS if profile == "oyun" else ()


@dataclass(frozen=True)
class ContentId:
    """What is playing on a variant sink: one song's artist, or one game."""
    kind: str              # "muzik" | "oyun"
    key: str               # normalized: "sanatci:virgin black" / "steam:2694490" / "exe:pathofexilesteam"
    display: str           # "Virgin Black" / "Path of Exile 2"
    track: str = ""        # song title (music only); a change = track boundary
    # page/stream URL when known (YouTube watch URL → video keywords); not part of identity
    url: str = field(default="", compare=False)


@dataclass(frozen=True)
class GenreResult:
    group: str             # one of MUSIC_GROUPS/GAME_GROUPS, or "" = unknown/general
    source: str            # Turkish, e.g. "MusicBrainz: gothic metal, doom metal" / "kullanıcı"
    confidence: float = 1.0


@dataclass(frozen=True)
class Stream:
    """A playback stream (= pactl sink-input)."""
    index: int
    sink_index: int
    app_name: str          # application.name, '' if missing
    binary: str            # application.process.binary, '' if missing
    pid: int | None        # application.process.id (may be sandbox/namespace-local!)
    media_name: str        # media.name, '' if missing
    media_role: str        # media.role, '' if missing
    node_name: str         # node.name, '' if missing
    corked: bool
    props: dict[str, str] = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class Sink:
    index: int
    name: str
    description: str
    state: str             # RUNNING / IDLE / SUSPENDED
    # raw ``pactl -f json list sinks`` item (properties, ports, active_port); {} from the
    # text fallback. kulaklik.siniflandir() reads it (CHANGE 7).
    info: dict = field(default_factory=dict, hash=False, compare=False, repr=False)


@dataclass(frozen=True)
class Event:
    kind: str              # "new" | "change" | "remove"
    facility: str          # "sink-input" | "sink" | "server" | "card" | ...
    index: int


@dataclass(frozen=True)
class MediaPlayer:
    """One MPRIS player on the session bus."""
    bus_name: str          # e.g. org.mpris.MediaPlayer2.firefox.instance_1_23
    pid: int | None        # host PID from DBus GetConnectionUnixProcessID
    identity: str          # "Firefox", "mpv", "Spotify", "VLC media player"
    status: str            # "Playing" | "Paused" | "Stopped"
    title: str
    artist: str
    url: str               # xesam:url, '' if missing


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    comm: str
    exe: str               # basename of /proc/<pid>/exe target, '' if unreadable
    cmdline: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict, hash=False, compare=False)
    ancestors: tuple[tuple[int, str, str], ...] = ()   # (pid, comm, cmdline joined) up to pid 1


@dataclass(frozen=True)
class Decision:
    category: Category
    profile: str | None    # one of EQ_PROFILES, or None = no EQ (route to the real default sink)
    reason: str            # short Turkish human-readable reason, e.g. "ikili 'cs2' kuralı"
    # CHANGE 9 (içerik denetimi) — not part of equality:
    kaynak: str = field(default="", compare=False)   # classifier step ("tarayici", "atama", …)
    zayif: bool = field(default=False, compare=False)  # fallback guess (site unknown, "YouTube video")
    # streams of ONE app instance whose names cannot tell them apart (Chromium "Playback"):
    # ≥2 playing with the same grup → the metadata cannot say which tab is which
    grup: str = field(default="", compare=False)
