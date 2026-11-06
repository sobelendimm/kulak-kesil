"""Paths and config loading shared by all modules."""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent
DEFAULTS_DIR = SRC_DIR / "config"

_home = Path.home()


def _xdg(var: str, default: Path | str) -> Path:
    """XDG base directory: an unset, empty or relative value means the default (spec);
    ``XDG_CONFIG_HOME=""`` must not turn our paths into ./oto-eq of the current dir."""
    v = os.environ.get(var, "")
    return Path(v) if v and os.path.isabs(v) else Path(default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", _home / ".config") / "oto-eq"
STATE_DIR = _xdg("XDG_STATE_HOME", _home / ".local/state") / "oto-eq"
RUNTIME_DIR = _xdg("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

RULES_FILE = "kurallar.toml"
PROFILES_FILE = "profiller.toml"

SOCKET_PATH = RUNTIME_DIR / "oto-eq.sock"
SINKS_CONF = CONFIG_DIR / "oto-eq-sinks.conf"
PLOT_FILE = CONFIG_DIR / "egriler.png"
MODE_FILE = STATE_DIR / "mod.json"
TRAY_SETTINGS_FILE = STATE_DIR / "tepsi.json"

SINKS_SERVICE = "oto-eq-sinks.service"
DAEMON_SERVICE = "oto-eq.service"

# Every subprocess (pactl, pw-cli, ...) MUST run with this env: the user's
# locale is Turkish and pactl/pw-* output is translated otherwise.
C_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C"}


def config_path(name: str) -> Path:
    """User copy in ~/.config/oto-eq if present, otherwise the shipped default."""
    user = CONFIG_DIR / name
    return user if user.exists() else DEFAULTS_DIR / name


def load_toml(name: str) -> dict:
    with open(config_path(name), "rb") as f:
        return tomllib.load(f)


# User per-app assignments written by the GUI ("Bu uygulamayı hep ... ile çal").
# Format: {"atamalar": [{"alan": "ikili"|"uygulama"|"flatpak", "deger": str,
#                        "profil": "cs2"|"oyun"|"muzik"|"film"|""}]}
# "deger" matches case-insensitively and exactly; profil "" = EQ'suz.
# The classifier evaluates these BEFORE every other rule.
ASSIGNMENTS_FILE = CONFIG_DIR / "atamalar.json"
ASSIGNMENT_FIELDS = ("ikili", "uygulama", "flatpak")


def load_assignments() -> list[dict]:
    try:
        with open(ASSIGNMENTS_FILE, encoding="utf-8") as f:
            items = json.load(f).get("atamalar", [])
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(items, list):
        # {"atamalar": null} (hand edit) used to raise TypeError in load_rules() →
        # the router started with no rules at all (every stream without EQ)
        return []
    return [a for a in items
            if isinstance(a, dict) and a.get("alan") in ASSIGNMENT_FIELDS
            and isinstance(a.get("deger"), str) and isinstance(a.get("profil"), str)]


def save_assignments(items: list[dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ASSIGNMENTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"atamalar": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(ASSIGNMENTS_FILE)


# Genre detection settings + user overrides (written by the GUI):
# {"cevrimici": bool, "ses_analizi": bool, "sanatci": {"virgin black": "metal_rock"},
#  "oyun": {"steam:2694490": "aksiyon_rpg"}}   group "" = force general (no variant)
GENRE_FILE = CONFIG_DIR / "tur-atamalari.json"
GENRE_CACHE = STATE_DIR / "tur-onbellek.json"


def load_genre_settings() -> dict:
    out = {"cevrimici": True, "ses_analizi": True, "sanatci": {}, "oyun": {}}
    try:
        with open(GENRE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for flag in ("cevrimici", "ses_analizi"):
            if isinstance(data.get(flag), bool):
                out[flag] = data[flag]
        for k in ("sanatci", "oyun"):
            if isinstance(data.get(k), dict):
                out[k] = {str(a): str(g) for a, g in data[k].items()}
    return out


def save_genre_settings(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = GENRE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(GENRE_FILE)


def load_rules() -> dict:
    """kurallar.toml + GUI assignments under the key "atama"."""
    rules = load_toml(RULES_FILE)
    rules["atama"] = load_assignments()
    return rules
