#!/usr/bin/env bash
# Kulak Kesil (oto-eq) kurulumu. Tekrar çalıştırmak güvenlidir (idempotent).
#   ./install.sh                 kur / güncelle
#   ./install.sh --dry-run       hiçbir şey yapmadan yapılacakları yazdır
#   ./install.sh --force-config  ~/.config/oto-eq içindeki kurallar/profiller
#                                dosyalarını varsayılanla değiştir (önce zaman damgalı yedek)
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
Kulak Kesil (oto-eq) kurulumu. Tekrar çalıştırmak güvenlidir (idempotent).
  ./install.sh                 kur / güncelle
  ./install.sh --dry-run       hiçbir şey yapmadan yapılacakları yazdır
  ./install.sh --force-config  ~/.config/oto-eq içindeki kurallar/profiller
                               dosyalarını varsayılanla değiştir (önce zaman damgalı yedek)
  ./install.sh --help          bu yardımı göster
EOF
    else
        cat <<'EOF'
Kulak Kesil (oto-eq) installer. Safe to run again (idempotent).
  ./install.sh                 install / update
  ./install.sh --dry-run       print what would be done, change nothing
  ./install.sh --force-config  replace the rules/profiles files in ~/.config/oto-eq
                               with the defaults (timestamped backup first)
  ./install.sh --help          show this help
EOF
    fi
}

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CFG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF_DIR="$CFG_HOME/oto-eq"
UNIT_DIR="$CFG_HOME/systemd/user"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_FILE="$CFG_HOME/autostart/oto-eq-tray.desktop"
APP_FILE="$DATA_HOME/applications/oto-eq.desktop"
ICON_FILE="$CONF_DIR/kulak-kesil.png"
UNITS=(oto-eq-sinks.service oto-eq.service)
MIC_UNIT=oto-eq-mic.service
MIC_CONF="$CONF_DIR/mikrofon.conf"
MARK="# oto-eq wrapper (install.sh)"
PY=/usr/bin/python3

DRY=0
FORCE_CONFIG=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY=1 ;;
        --force-config) FORCE_CONFIG=1 ;;
        -h|--help) usage; exit 0 ;;
        *) printf '%s\n' "$(L "Bilinmeyen seçenek: $arg" "Unknown option: $arg")" >&2; exit 2 ;;
    esac
done

quiet() { "$@" >/dev/null 2>&1; }
say()  { printf '==> %s\n' "$*"; }
warn() { printf '%s %s\n' "$(L 'UYARI:' 'WARNING:')" "$*" >&2; }
die()  { printf '%s %s\n' "$(L 'HATA:' 'ERROR:')" "$*" >&2; exit 1; }

# run CMD...: execute, or print it in --dry-run mode
run() {
    if (( DRY )); then
        printf '%s' "$DRYTAG"; printf ' %q' "$@"; printf '\n'
    else
        "$@"
    fi
}

# write_file DEST MODE  (content on stdin)
write_file() {
    local dest="$1" mode="$2" content
    content="$(cat)"
    if (( DRY )); then
        printf '%s %s\n' "$DRYTAG" "$(L "yaz $dest (izin $mode):" "write $dest (mode $mode):")"
        printf '%s\n' "$content" | sed 's/^/           | /'
        return 0
    fi
    mkdir -p "$(dirname "$dest")"
    printf '%s\n' "$content" > "$dest.tmp.$$"
    chmod "$mode" "$dest.tmp.$$"
    mv -f "$dest.tmp.$$" "$dest"
}

desktop_dir() {
    local d=""
    if command -v xdg-user-dir >/dev/null 2>&1; then
        d="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
    fi
    # xdg-user-dir answers $HOME when no desktop dir is configured
    if [[ -n "$d" && "$d" != "$HOME" && -d "$d" ]]; then
        printf '%s\n' "$d"
    fi
}

# ---------------------------------------------------------------- checks
say "$(L "Kaynak: $SRC_DIR" "Source: $SRC_DIR")"
if (( DRY )); then say "$(L 'DENEME MODU: hiçbir değişiklik yapılmayacak' 'DRY-RUN MODE: nothing will be changed')"; fi
[[ "$SRC_DIR" == "$HOME/.local/src/oto-eq" ]] || \
    warn "$(L "systemd servisi %h/.local/src/oto-eq yolunu kullanır; kaynak farklı yerde: $SRC_DIR" \
         "the systemd services use the path %h/.local/src/oto-eq; the source is elsewhere: $SRC_DIR")"
[[ "$CFG_HOME" == "$HOME/.config" ]] || \
    warn "$(L "XDG_CONFIG_HOME=$CFG_HOME; servisler ~/.config/oto-eq yolunu kullanır" \
         "XDG_CONFIG_HOME=$CFG_HOME; the services use ~/.config/oto-eq")"
for tool in "$PY" /usr/bin/pipewire systemctl; do
    command -v "$tool" >/dev/null 2>&1 || die "$(L "gerekli program yok: $tool" "required program not found: $tool")"
done
for f in config/kurallar.toml config/profiller.toml oto_eq/router.py oto_eq/eqgen.py oto_eq/ipc.py \
         systemd/oto-eq-sinks.service systemd/oto-eq.service systemd/oto-eq-mic.service oto-eq-tray.desktop oto-eq.desktop; do
    if [[ ! -e "$SRC_DIR/$f" ]]; then
        if (( DRY )); then warn "$(L "eksik dosya (gerçek kurulumda hata olur): $f" "missing file (a real install would fail): $f")"
        else die "$(L "eksik dosya: $SRC_DIR/$f" "missing file: $SRC_DIR/$f")"; fi
    fi
done

# ---------------------------------------------------------------- dependency pre-check
# Required: pactl, Python >= 3.11, LSP LV2 plugins (a real install dies if one is
# missing; --dry-run only warns). Recommended: PyQt6, numpy, RNNoise LADSPA (warn).
# Optional: onnxruntime (info line only).
LV2_DIRS=(/usr/lib/lv2 /usr/lib64/lv2 /usr/local/lib/lv2 /usr/lib/x86_64-linux-gnu/lv2 "$HOME/.lv2")
LADSPA_DIRS=(/usr/lib/ladspa /usr/lib64/ladspa /usr/local/lib/ladspa /usr/lib/x86_64-linux-gnu/ladspa)
DEP_REQ_FAIL=0
DEP_ANY=0
MISSING=()

# dep_find GLOB EXTRA_PATH DIR...: success if GLOB matches in one of the DIRs or in
# EXTRA_PATH (a colon-separated list such as $LV2_PATH)
dep_find() {
    local pat="$1" extra="$2" d
    shift 2
    local -a dirs=("$@") more=()
    if [[ -n "$extra" ]]; then
        IFS=: read -ra more <<<"$extra"
        dirs+=("${more[@]}")
    fi
    for d in "${dirs[@]}"; do
        [[ -n "$d" && -d "$d" ]] || continue
        if compgen -G "$d/$pat" >/dev/null; then return 0; fi
    done
    return 1
}

# dep_miss req|rec KEY TURKISH ENGLISH: report a missing dependency (KEY feeds the package hint)
dep_miss() {
    local kind="$1" key="$2" tag
    DEP_ANY=1
    if [[ -n "$key" ]]; then MISSING+=("$key"); fi
    if [[ "$kind" == req ]]; then
        DEP_REQ_FAIL=1
        tag="$(L 'zorunlu bağımlılık eksik' 'required dependency missing')"
        if (( DRY )); then tag+=" $(L '(gerçek kurulumda hata olur)' '(a real install would fail)')"; fi
    else
        tag="$(L 'önerilen bağımlılık eksik' 'recommended dependency missing')"
    fi
    warn "$tag: $(L "$3" "$4")"
}

# dep_hint: one summary line per matching distro family with the missing package names
dep_hint() {
    local ids fam key pk pkgs extra cmd label line
    local -a fams=() lines=()
    # shellcheck disable=SC1091
    ids=" $([[ -r /etc/os-release ]] && . /etc/os-release; printf '%s %s' "${ID:-}" "${ID_LIKE:-}") "
    case "$ids" in *" arch "*) fams+=(arch) ;; esac
    case "$ids" in *" fedora "*) fams+=(fedora) ;; esac
    case "$ids" in *" debian "*|*" ubuntu "*) fams+=(debian) ;; esac
    if (( ${#fams[@]} == 0 )); then fams=(arch fedora debian); fi
    for fam in "${fams[@]}"; do
        pkgs=""; extra=""
        for key in "${MISSING[@]}"; do
            pk=""
            case "$fam:$key" in
                arch:pactl|debian:pactl) pk=pipewire-pulse ;;
                fedora:pactl) pk=pipewire-pulseaudio ;;
                *:lv2) pk=lsp-plugins-lv2 ;;
                arch:qt) pk=python-pyqt6 ;;
                fedora:qt|debian:qt) pk=python3-pyqt6 ;;
                arch:numpy) pk=python-numpy ;;
                fedora:numpy|debian:numpy) pk=python3-numpy ;;
                arch:rnnoise) pk=noise-suppression-for-voice ;;
                fedora:rnnoise) extra="$(L 'noise-suppression-for-voice (COPR/elle)' 'noise-suppression-for-voice (COPR / manual)')" ;;
                debian:rnnoise) extra="$(L 'noise-suppression-for-voice: elle / GitHub sürümü' 'noise-suppression-for-voice: manual / GitHub release')" ;;
                arch:onnx) pk=python-onnxruntime ;;
            esac
            if [[ -n "$pk" ]]; then pkgs+=" $pk"; fi
        done
        case "$fam" in
            arch)   label="Arch";          cmd="sudo pacman -S --needed" ;;
            fedora) label="Fedora";        cmd="sudo dnf install" ;;
            *)      label="Debian/Ubuntu"; cmd="sudo apt install" ;;
        esac
        line=""
        if [[ -n "$pkgs" ]]; then line="$cmd$pkgs"; fi
        if [[ -n "$extra" ]]; then line+="${line:+ + }$extra"; fi
        if [[ -n "$line" ]]; then lines+=("$(printf '    %-14s %s' "$label:" "$line")"); fi
    done
    if (( ${#lines[@]} )); then
        say "$(L 'İpucu: eksik bağımlılıklar için paketler' 'Hint: packages for the missing dependencies')"
        printf '%s\n' "${lines[@]}"
    fi
}

if ! command -v pactl >/dev/null 2>&1; then
    dep_miss req pactl "pactl bulunamadı (pipewire-pulse paketi); ses yönetimi için gerekli" \
                       "pactl not found (provided by pipewire-pulse); needed to manage audio"
fi
if ! quiet "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    pyver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    dep_miss req "" "Python >= 3.11 gerekli (bulunan: ${pyver:-?})" \
                    "Python >= 3.11 is required (found: ${pyver:-?})"
fi
if ! dep_find 'lsp-plugins*.lv2' "${LV2_PATH:-}" "${LV2_DIRS[@]}"; then
    dep_miss req lv2 "LSP LV2 eklentileri (lsp-plugins*.lv2) bulunamadı; EQ zinciri bunları kullanır, yoksa sanal EQ çıkışları açılmaz" \
                     "LSP LV2 plugins (lsp-plugins*.lv2) not found; the EQ chain needs them, without them the virtual EQ outputs will not open"
fi
if ! quiet "$PY" -c 'import PyQt6.QtCore'; then
    dep_miss rec qt "Python modülü PyQt6 yok; pencere ve tepsi çalışmaz" \
                    "Python module PyQt6 not found; the window and tray will not work"
fi
if ! quiet "$PY" -c 'import numpy'; then
    dep_miss rec numpy "Python modülü numpy yok; ses analizi ve tür tahmini çalışmaz" \
                       "Python module numpy not found; audio analysis and genre detection will not work"
fi
if ! dep_find 'librnnoise_ladspa.so' "${LADSPA_PATH:-}" "${LADSPA_DIRS[@]}"; then
    dep_miss rec rnnoise "librnnoise_ladspa.so bulunamadı; mikrofon gürültü bastırma çalışmaz" \
                         "librnnoise_ladspa.so not found; microphone noise suppression will not work"
fi
if ! quiet "$PY" -c 'import onnxruntime'; then
    MISSING+=(onnx)
    say "$(L 'İsteğe bağlı: onnxruntime yok (ses analizi modelleri çalışmaz; uygulama yine de çalışır)' \
             'Optional: onnxruntime not found (the audio analysis models will not run; the app still works)')"
fi
if (( ${#MISSING[@]} )); then dep_hint; fi
if (( ! DEP_ANY && ${#MISSING[@]} == 0 )); then
    say "$(L 'Bağımlılıklar tamam' 'Dependencies OK')"
fi
if (( DEP_REQ_FAIL && ! DRY )); then
    die "$(L 'zorunlu bağımlılıklar eksik; yukarıdaki uyarılara bak (hiçbir şey kurulmadı)' \
             'required dependencies are missing; see the warnings above (nothing was installed)')"
fi

# ---------------------------------------------------------------- config
say "$(L "Ayar dosyaları: $CONF_DIR" "Config files: $CONF_DIR")"
run mkdir -p "$CONF_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
for name in kurallar.toml profiller.toml; do
    dst="$CONF_DIR/$name"
    if [[ -e "$dst" ]]; then
        if (( FORCE_CONFIG )); then
            run cp -a "$dst" "$dst.yedek-$stamp"
            run cp "$SRC_DIR/config/$name" "$dst"
            say "$(L "$name varsayılanla değiştirildi (yedek: $name.yedek-$stamp)" "$name replaced with the default (backup: $name.yedek-$stamp)")"
        else
            say "$(L "$name zaten var, dokunulmadı" "$name already exists, left untouched")"
        fi
    else
        run cp "$SRC_DIR/config/$name" "$dst"
    fi
done

say "$(L 'EQ ayarı üretiliyor (oto-eq-sinks.conf)' 'Generating the EQ config (oto-eq-sinks.conf)')"
run env -C "$SRC_DIR" PYTHONPATH="$SRC_DIR" "$PY" -m oto_eq.eqgen generate

# microphone (optional feature): config from config/mikrofon.toml via micgen
MIC_OK=0
if [[ -e "$SRC_DIR/oto_eq/micgen.py" ]]; then
    say "$(L 'Mikrofon ayarı üretiliyor (mikrofon.conf)' 'Generating the microphone config (mikrofon.conf)')"
    run env -C "$SRC_DIR" PYTHONPATH="$SRC_DIR" "$PY" -m oto_eq.micgen generate
    MIC_OK=1
elif [[ -e "$MIC_CONF" ]]; then
    say "$(L 'micgen yok; mevcut mikrofon.conf kullanılacak' 'micgen not found; using the existing mikrofon.conf')"
    MIC_OK=1
else
    warn "$(L "mikrofon özelliği atlandı (oto_eq/micgen.py ve $MIC_CONF yok)" "microphone feature skipped (neither oto_eq/micgen.py nor $MIC_CONF exists)")"
fi

say "$(L 'Uygulama simgesi çiziliyor' 'Drawing the application icon')"
run env -C "$SRC_DIR" PYTHONPATH="$SRC_DIR" QT_QPA_PLATFORM=offscreen \
    "$PY" -m oto_eq.ui_common icon "$ICON_FILE"

# ---------------------------------------------------------------- systemd
say "$(L "systemd kullanıcı servisleri: $UNIT_DIR" "systemd user services: $UNIT_DIR")"
run mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
    run install -m 0644 "$SRC_DIR/systemd/$u" "$UNIT_DIR/$u"
done
run systemctl --user daemon-reload
run systemctl --user enable "${UNITS[@]}"
# restart the router (not just start) so a re-install picks up new code/config;
# oto-eq-sinks has BindsTo=oto-eq.service, so the restart propagates to it
# (one audio gap instead of two). `start` covers a sinks unit that was down.
run systemctl --user restart oto-eq.service
run systemctl --user start oto-eq-sinks.service
if (( MIC_OK )); then
    # the default microphone is NOT touched here (the app / `kulak mikrofon varsayilan` does that)
    run install -m 0644 "$SRC_DIR/systemd/$MIC_UNIT" "$UNIT_DIR/$MIC_UNIT"
    run systemctl --user daemon-reload
    run systemctl --user enable "$MIC_UNIT"
    run systemctl --user restart "$MIC_UNIT"
fi

# ---------------------------------------------------------------- wrappers
say "$(L "Komutlar: $BIN_DIR/{kulak,kulak-tepsi,kulak-app}" "Commands: $BIN_DIR/{kulak,kulak-tepsi,kulak-app}")"
for pair in "kulak:oto_eq.cli" "kulak-tepsi:oto_eq.tray" "kulak-app:oto_eq.app"; do
    name="${pair%%:*}"; mod="${pair#*:}"; dst="$BIN_DIR/$name"
    if [[ -e "$dst" ]] && ! grep -qF "$MARK" "$dst" 2>/dev/null; then
        die "$(L "$dst var ve bizim değil; elle kaldırıp tekrar dene" "$dst exists and is not ours; remove it by hand and try again")"
    fi
    write_file "$dst" 0755 <<EOF
#!/bin/sh
$MARK
exec env PYTHONPATH="$SRC_DIR" $PY -m $mod "\$@"
EOF
done
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$(L "$BIN_DIR PATH içinde değil (fish: fish_add_path $BIN_DIR)" "$BIN_DIR is not in PATH (fish: fish_add_path $BIN_DIR)")" ;;
esac

# ---------------------------------------------------------------- desktop entries
# installed copies: absolute Exec/Icon + a marker key so uninstall knows they are ours
desktop_entry() {  # desktop_entry TEMPLATE WRAPPER
    sed -e "s|^Exec=.*|Exec=$BIN_DIR/$2|" -e "s|^Icon=.*|Icon=$ICON_FILE|" "$SRC_DIR/$1"
    printf 'X-OtoEQ-Installed=true\n'
}

say "$(L "Tepsi otomatik başlatma: $AUTOSTART_FILE" "Tray autostart: $AUTOSTART_FILE")"
desktop_entry oto-eq-tray.desktop kulak-tepsi | write_file "$AUTOSTART_FILE" 0644

say "$(L "Uygulama menüsü: $APP_FILE" "Application menu entry: $APP_FILE")"
desktop_entry oto-eq.desktop kulak-app | write_file "$APP_FILE" 0644

DESK="$(desktop_dir)"
if [[ -n "$DESK" ]]; then
    SHORTCUT="$DESK/Kulak Kesil.desktop"
    say "$(L "Masaüstü kısayolu: $SHORTCUT" "Desktop shortcut: $SHORTCUT")"
    desktop_entry oto-eq.desktop kulak-app | write_file "$SHORTCUT" 0755
    if command -v gio >/dev/null 2>&1; then
        run gio set "$SHORTCUT" metadata::trusted true || warn "$(L 'gio ile güvenilir işaretlenemedi' 'could not mark the shortcut as trusted with gio')"
    fi
else
    warn "$(L 'masaüstü klasörü bulunamadı, kısayol atlandı' 'desktop folder not found, shortcut skipped')"
fi
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    run quiet kbuildsycoca6 --noincremental || true
fi

# ---------------------------------------------------------------- tray
if [[ -z "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]]; then
    warn "$(L 'grafik oturum yok; tepsi bir sonraki girişte otomatik başlayacak' 'no graphical session; the tray will start automatically at the next login')"
elif pgrep -u "$(id -u)" -f 'oto_eq[.]tray' >/dev/null 2>&1; then
    say "$(L 'Tepsi zaten çalışıyor (yeni kodu almak için: Çıkış, sonra kulak-tepsi)' \
           'The tray is already running (to pick up new code: quit it from its menu, then run kulak-tepsi)')"
else
    say "$(L 'Tepsi başlatılıyor' 'Starting the tray')"
    if (( DRY )); then
        printf '%s setsid -f %q >/dev/null 2>&1 </dev/null\n' "$DRYTAG" "$BIN_DIR/kulak-tepsi"
    else
        setsid -f "$BIN_DIR/kulak-tepsi" >/dev/null 2>&1 </dev/null
    fi
fi

say "$(L 'Bitti. Durum için: kulak durum' 'Done. For status run: kulak durum')"
