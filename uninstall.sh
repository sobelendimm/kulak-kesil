#!/usr/bin/env bash
# Kulak Kesil (oto-eq) kaldırma: install.sh'in yaptığı her şeyi geri alır.
#   ./uninstall.sh            kaldır (~/.config/oto-eq ayarların KORUNUR)
#   ./uninstall.sh --purge    ayarları ve durum dosyalarını da sil
#   ./uninstall.sh --dry-run  hiçbir şey yapmadan yapılacakları yazdır
set -euo pipefail

# UI language: the first non-empty of LC_ALL / LC_MESSAGES / LANG decides;
# a value starting with "tr" gives Turkish, anything else English.
TR=0
for _loc in "${LC_ALL:-}" "${LC_MESSAGES:-}" "${LANG:-}"; do
    if [[ -n "$_loc" ]]; then
        if [[ "$_loc" == tr* ]]; then TR=1; fi
        break
    fi
done
unset _loc
# L TURKISH ENGLISH: print the text in the UI language
L() { if (( TR )); then printf '%s' "$1"; else printf '%s' "$2"; fi; }
DRYTAG="$(L '[deneme]' '[dry-run]')"

usage() {
    if (( TR )); then
        cat <<'EOF'
Kulak Kesil (oto-eq) kaldırma: install.sh'in yaptığı her şeyi geri alır.
  ./uninstall.sh            kaldır (~/.config/oto-eq ayarların KORUNUR)
  ./uninstall.sh --purge    ayarları ve durum dosyalarını da sil
  ./uninstall.sh --dry-run  hiçbir şey yapmadan yapılacakları yazdır
  ./uninstall.sh --help     bu yardımı göster
EOF
    else
        cat <<'EOF'
Kulak Kesil (oto-eq) uninstaller: undoes everything install.sh did.
  ./uninstall.sh            uninstall (your settings in ~/.config/oto-eq are KEPT)
  ./uninstall.sh --purge    also delete the settings and state files
  ./uninstall.sh --dry-run  print what would be done, change nothing
  ./uninstall.sh --help     show this help
EOF
    fi
}

CFG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
CONF_DIR="$CFG_HOME/oto-eq"
STATE_DIR="$STATE_HOME/oto-eq"
UNIT_DIR="$CFG_HOME/systemd/user"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_FILE="$CFG_HOME/autostart/oto-eq-tray.desktop"
APP_FILE="$DATA_HOME/applications/oto-eq.desktop"
ICON_FILE="$CONF_DIR/kulak-kesil.png"
# ORDER MATTERS: the router first (on a clean stop it moves streams back to the
# default sink, which also clears WirePlumber's remembered targets), then the
# EQ sinks and the microphone.
UNITS=(oto-eq.service oto-eq-sinks.service oto-eq-mic.service)
MIC_SOURCE=oto_eq_mikrofon
MARK="# oto-eq wrapper (install.sh)"
DESKTOP_MARK="X-OtoEQ-Installed=true"

DRY=0
PURGE=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf '%s\n' "$(L "Bilinmeyen seçenek: $arg" "Unknown option: $arg")" >&2; exit 2 ;;
    esac
done

quiet() { "$@" >/dev/null 2>&1; }
say()  { printf '==> %s\n' "$*"; }
warn() { printf '%s %s\n' "$(L 'UYARI:' 'WARNING:')" "$*" >&2; }
run() {
    if (( DRY )); then
        printf '%s' "$DRYTAG"; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}
# remove FILE only if it contains MARKER (never delete someone else's file)
rm_marked() {
    local f="$1" marker="$2"
    if [[ -e "$f" ]]; then
        if grep -qF "$marker" "$f" 2>/dev/null; then
            run rm -f "$f"
        else
            warn "$(L "$f bizim değil, dokunulmadı" "$f is not ours, left untouched")"
        fi
    fi
}

if (( DRY )); then say "$(L 'DENEME MODU: hiçbir değişiklik yapılmayacak' 'DRY-RUN MODE: nothing will be changed')"; fi

say "$(L 'Tepsi ve pencere kapatılıyor' 'Closing the tray and window')"
for pat in 'oto_eq[.]tray' 'oto_eq[.]app'; do
    if pgrep -u "$(id -u)" -f "$pat" >/dev/null 2>&1; then
        run pkill -u "$(id -u)" -f "$pat" || true
    fi
done

# never leave the user with a vanished default microphone
if command -v pactl >/dev/null 2>&1 && \
   [[ "$(LC_ALL=C pactl get-default-source 2>/dev/null || true)" == "$MIC_SOURCE" ]]; then
    sources="$(LC_ALL=C pactl list short sources 2>/dev/null | cut -f2 | grep -v '\.monitor$' | grep -v '^oto_eq_' || true)"
    real="$(grep -i 'hs80' <<<"$sources" | grep '^alsa_input' | head -n1 || true)"
    [[ -n "$real" ]] || real="$(grep '^alsa_input' <<<"$sources" | head -n1 || true)"
    [[ -n "$real" ]] || real="$(head -n1 <<<"$sources" || true)"
    if [[ -n "$real" ]]; then
        say "$(L "Varsayılan mikrofon geri alınıyor: $real" "Restoring the default microphone: $real")"
        run pactl set-default-source "$real"
    else
        warn "$(L 'varsayılan mikrofon Kulak Kesil · Mikrofon ama başka mikrofon bulunamadı' \
             'the default microphone is Kulak Kesil · Mikrofon but no other microphone was found')"
    fi
fi

say "$(L 'systemd servisleri durduruluyor ve kaldırılıyor' 'Stopping and removing the systemd services')"
for u in "${UNITS[@]}"; do
    if [[ -e "$UNIT_DIR/$u" ]] || systemctl --user cat "$u" >/dev/null 2>&1; then
        run systemctl --user disable --now "$u" || true
    fi
done
for u in "${UNITS[@]}"; do
    if [[ -e "$UNIT_DIR/$u" ]]; then run rm -f "$UNIT_DIR/$u"; fi
done
run systemctl --user daemon-reload
run systemctl --user reset-failed "${UNITS[@]}" 2>/dev/null || true

say "$(L 'Komutlar kaldırılıyor' 'Removing the commands')"
for name in kulak kulak-tepsi kulak-app; do
    rm_marked "$BIN_DIR/$name" "$MARK"
done

say "$(L 'Masaüstü girdileri kaldırılıyor' 'Removing the desktop entries')"
rm_marked "$AUTOSTART_FILE" "$DESKTOP_MARK"
rm_marked "$APP_FILE" "$DESKTOP_MARK"
if command -v xdg-user-dir >/dev/null 2>&1; then
    DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    if [[ -n "$DESK" && "$DESK" != "$HOME" ]]; then
        rm_marked "$DESK/Kulak Kesil.desktop" "$DESKTOP_MARK"
    fi
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    run quiet kbuildsycoca6 --noincremental || true
fi

if [[ -e "$ICON_FILE" ]]; then run rm -f "$ICON_FILE"; fi
SOCK="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/oto-eq.sock"
if [[ -S "$SOCK" ]]; then run rm -f "$SOCK"; fi

if (( PURGE )); then
    say "$(L 'Ayarlar ve durum siliniyor (--purge)' 'Deleting settings and state (--purge)')"
    if [[ -d "$CONF_DIR" ]]; then run rm -rf "$CONF_DIR"; fi
    if [[ -d "$STATE_DIR" ]]; then run rm -rf "$STATE_DIR"; fi
else
    say "$(L "Ayarlar korundu: $CONF_DIR (tamamen silmek için --purge)" "Settings kept: $CONF_DIR (use --purge to delete them completely)")"
fi
say "$(L 'Kaldırıldı. Kaynak kod yerinde duruyor.' 'Removed. The source code is left in place.')"
