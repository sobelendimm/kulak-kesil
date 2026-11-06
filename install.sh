#!/usr/bin/env bash
# Kulak Kesil (oto-eq) kurulumu. Tekrar çalıştırmak güvenlidir (idempotent).
# Kaynak klasörü herhangi bir yerde olabilir; systemd servisleri gerçek yollarla kurulur.
#   ./install.sh                 kur / güncelle
#   ./install.sh --dry-run       hiçbir şey yapmadan yapılacakları yazdır
#   ./install.sh --force-config  ~/.config/oto-eq içindeki kurallar/profiller
#                                dosyalarını varsayılanla değiştir (önce zaman damgalı yedek)
set -euo pipefail
# bash 5.2: '&' in a ${var//pat/repl} replacement means "the match"; we want it literal
shopt -u patsub_replacement 2>/dev/null || true

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
Kaynak klasörü herhangi bir yerde olabilir; taşırsan ./install.sh'i yeniden çalıştır.
EOF
    else
        cat <<'EOF'
Kulak Kesil (oto-eq) installer. Safe to run again (idempotent).
  ./install.sh                 install / update
  ./install.sh --dry-run       print what would be done, change nothing
  ./install.sh --force-config  replace the rules/profiles files in ~/.config/oto-eq
                               with the defaults (timestamped backup first)
  ./install.sh --help          show this help
The source folder can live anywhere; if you move it, run ./install.sh again.
EOF
    fi
}

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CFG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
CONF_DIR="$CFG_HOME/oto-eq"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_FILE="$CFG_HOME/autostart/oto-eq-tray.desktop"
APP_FILE="$DATA_HOME/applications/oto-eq.desktop"
ICON_FILE="$CONF_DIR/kulak-kesil.png"
UNITS=(oto-eq-sinks.service oto-eq.service)
MIC_UNIT=oto-eq-mic.service
MIC_CONF="$CONF_DIR/mikrofon.conf"
MARK="# oto-eq wrapper (install.sh)"
DESKTOP_MARK="X-OtoEQ-Installed=true"
WRAPPERS=(kulak kulak-tepsi kulak-app)
PY=/usr/bin/python3
OS_RELEASE="${OTO_EQ_OS_RELEASE:-/etc/os-release}"   # override: tests only
# the paths the unit templates in systemd/ are written for
TPL_SRC="%h/.local/src/oto-eq"
TPL_CONF="%h/.config/oto-eq"

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

# is_ours FILE MARKER: FILE exists and carries our marker
is_ours() { [[ -e "$1" ]] && grep -qF "$2" "$1" 2>/dev/null; }

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

# --- quoting for the files we generate (paths may contain spaces, quotes, % or $)
# sh_quote S: one single-quoted POSIX sh word
sh_quote() { local s="${1//\'/\'\\\'\'}"; printf "'%s'" "$s"; }
# sd_path PATH: a whole-value systemd path setting (WorkingDirectory=, Condition*=)
sd_path() { printf '%s' "${1//%/%%}"; }
# sd_quote S [1]: one double-quoted systemd word (Environment= assignment; with 1 also an
# ExecStart= argument, where "$" would start a variable)
sd_quote() {
    local s="$1"
    s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//%/%%}"
    if [[ "${2:-0}" == 1 ]]; then s="${s//\$/\$\$}"; fi
    printf '"%s"' "$s"
}
# desktop_exec PATH: the program of a desktop-entry Exec= key (quoted when it has a
# reserved character; ASCII ranges like A-Z are locale-dependent, hence [:alnum:])
DESKTOP_SAFE_RE='[^-[:alnum:]_./+,:=@%]'
desktop_exec() {
    local s="${1//%/%%}"
    if [[ "$s" =~ $DESKTOP_SAFE_RE ]]; then
        s="${s//\\/\\\\\\\\}"; s="${s//\"/\\\\\"}"; s="${s//\`/\\\\\`}"; s="${s//\$/\\\\\$}"
        s="\"$s\""
    fi
    printf '%s' "$s"
}

# ---------------------------------------------------------------- checks
if (( EUID == 0 )); then
    die "$(L 'root olarak çalıştırma: Kulak Kesil kullanıcı başına kurulur (sudo olmadan ./install.sh)' \
             'do not run as root: Kulak Kesil installs per user (run ./install.sh without sudo)')"
fi
for p in "$HOME" "$SRC_DIR" "$CFG_HOME" "$DATA_HOME" "$STATE_HOME"; do
    [[ "$p" != *$'\n'* ]] || die "$(L 'yol satır sonu içeriyor, desteklenmiyor' 'a path contains a newline; not supported')"
done
say "$(L "Kaynak: $SRC_DIR" "Source: $SRC_DIR")"
if (( DRY )); then say "$(L 'DENEME MODU: hiçbir değişiklik yapılmayacak' 'DRY-RUN MODE: nothing will be changed')"; fi
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
# our commands must not replace someone else's: checked BEFORE anything is changed
for name in "${WRAPPERS[@]}"; do
    if [[ -e "$BIN_DIR/$name" ]] && ! is_ours "$BIN_DIR/$name" "$MARK"; then
        msg="$(L "$BIN_DIR/$name var ve bizim değil; elle kaldırıp tekrar dene" \
                 "$BIN_DIR/$name exists and is not ours; remove it by hand and try again")"
        if (( DRY )); then warn "$msg $(L '(gerçek kurulumda hata olur)' '(a real install would fail)')"
        else die "$msg"; fi
    fi
done
# an earlier install (our tray command exists): user choices made since then
# (tray autostart off, desktop shortcut deleted) are respected
PREV_INSTALL=0
if is_ours "$BIN_DIR/kulak-tepsi" "$MARK"; then PREV_INSTALL=1; fi

# ---------------------------------------------------------------- systemd paths
# The services run in the systemd user manager's environment, not in this shell's:
# its XDG_CONFIG_HOME decides where it looks for units, and the router must be told
# about every XDG directory this shell uses differently.
MGR_ENV="$(systemctl --user show-environment 2>/dev/null || true)"
mgr_env() {  # mgr_env NAME: value in the manager's environment ("" = unset / unreadable)
    local line
    while IFS= read -r line; do
        if [[ "$line" == "$1="* ]]; then
            line="${line#*=}"
            [[ "$line" == "\$'"* ]] || printf '%s' "$line"   # escaped value: treat as unset
            return 0
        fi
    done <<<"$MGR_ENV"
}
MGR_CFG="$(mgr_env XDG_CONFIG_HOME)"; MGR_CFG="${MGR_CFG:-$HOME/.config}"
MGR_STATE="$(mgr_env XDG_STATE_HOME)"; MGR_STATE="${MGR_STATE:-$HOME/.local/state}"
MGR_DATA="$(mgr_env XDG_DATA_HOME)"; MGR_DATA="${MGR_DATA:-$HOME/.local/share}"
UNIT_DIR="$MGR_CFG/systemd/user"
SVC_ENV=()
[[ "$CFG_HOME" == "$MGR_CFG" ]] || SVC_ENV+=("XDG_CONFIG_HOME=$CFG_HOME")
[[ "$STATE_HOME" == "$MGR_STATE" ]] || SVC_ENV+=("XDG_STATE_HOME=$STATE_HOME")
[[ "$DATA_HOME" == "$MGR_DATA" ]] || SVC_ENV+=("XDG_DATA_HOME=$DATA_HOME")

# The unit templates use %h/.local/src/oto-eq and %h/.config/oto-eq. They are installed
# unchanged for that layout; otherwise (source elsewhere, other config dir, extra
# environment, or a $HOME with characters that would need quoting) the real paths are
# filled in, quoted for systemd.
DEFAULT_SRC="$(cd "$HOME/.local/src/oto-eq" 2>/dev/null && pwd -P || true)"
HOME_UNSAFE_RE='[[:space:]"\\%$]'
RENDER=0
if [[ "$SRC_DIR" != "$DEFAULT_SRC" || "$CONF_DIR" != "$HOME/.config/oto-eq" || ${#SVC_ENV[@]} -gt 0 \
      || "$HOME" =~ $HOME_UNSAFE_RE ]]; then
    RENDER=1
fi

# render_unit NAME: systemd/NAME with the real paths (unit text on stdout)
render_unit() {
    local line e
    while IFS= read -r line || [[ -n "$line" ]]; do
        if (( RENDER )); then
            case "$line" in
                "#"*) ;;
                "WorkingDirectory=$TPL_SRC")
                    line="WorkingDirectory=$(sd_path "$SRC_DIR")" ;;
                "Environment=PYTHONPATH=$TPL_SRC")
                    line="Environment=$(sd_quote "PYTHONPATH=$SRC_DIR")"
                    for e in "${SVC_ENV[@]}"; do line+=$'\n'"Environment=$(sd_quote "$e")"; done ;;
                "ConditionPathExists=$TPL_CONF/"*)
                    line="ConditionPathExists=$(sd_path "$CONF_DIR/${line#"ConditionPathExists=$TPL_CONF/"}")" ;;
                "ExecStart=/usr/bin/pipewire -c $TPL_CONF/"*)
                    line="ExecStart=/usr/bin/pipewire -c $(sd_quote "$CONF_DIR/${line#"ExecStart=/usr/bin/pipewire -c $TPL_CONF/"}" 1)" ;;
                *"$TPL_SRC"*|*"$TPL_CONF"*)
                    die "$(L "systemd/$1: bu satırdaki yol doldurulamıyor: $line" \
                             "systemd/$1: cannot fill in the path on this line: $line")" ;;
            esac
        fi
        printf '%s\n' "$line"
    done <"$SRC_DIR/systemd/$1"
}
# validate every template now: render_unit dies inside a pipeline later otherwise
for u in "${UNITS[@]}" "$MIC_UNIT"; do
    if [[ -e "$SRC_DIR/systemd/$u" ]]; then render_unit "$u" >/dev/null; fi
done
if (( RENDER )); then
    say "$(L "systemd servisleri bu yolları kullanacak: kaynak $SRC_DIR, ayarlar $CONF_DIR (kaynak klasörünü taşırsan ./install.sh'i yeniden çalıştır)" \
             "the systemd services will use: source $SRC_DIR, config $CONF_DIR (if you move the source folder, run ./install.sh again)")"
fi

# ---------------------------------------------------------------- dependency pre-check
# Required (a real install dies if one is missing; --dry-run only warns): pactl, a
# PipeWire sound server, the pw-* tools, Python >= 3.11, numpy, PyQt6, LSP LV2 plugins.
# Recommended (warn): matplotlib, jeepney, RNNoise LADSPA. Optional: onnxruntime (info).
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
    # shellcheck disable=SC1090,SC1091
    ids=" $([[ -r "$OS_RELEASE" ]] && . "$OS_RELEASE"; printf '%s %s' "${ID:-}" "${ID_LIKE:-}") "
    case "$ids" in *" arch "*) fams+=(arch) ;; esac
    case "$ids" in *" fedora "*) fams+=(fedora) ;; esac
    case "$ids" in *" debian "*|*" ubuntu "*) fams+=(debian) ;; esac
    if (( ${#fams[@]} == 0 )); then fams=(arch fedora debian); fi
    for fam in "${fams[@]}"; do
        pkgs=""; extra=""
        for key in "${MISSING[@]}"; do
            pk=""
            case "$fam:$key" in
                arch:pactl|arch:pwpulse) pk=pipewire-pulse ;;      # pulls in libpulse (pactl, parec)
                fedora:pactl) pk="pipewire-pulseaudio pulseaudio-utils" ;;
                debian:pactl) pk="pipewire-pulse pulseaudio-utils" ;;
                fedora:pwpulse) pk=pipewire-pulseaudio ;;
                debian:pwpulse) pk="pipewire-pulse wireplumber" ;;
                arch:pwtools) pk=pipewire ;;
                fedora:pwtools) pk=pipewire-utils ;;
                debian:pwtools) pk=pipewire-bin ;;
                *:lv2) pk=lsp-plugins-lv2 ;;
                arch:qt) pk=python-pyqt6 ;;
                fedora:qt|debian:qt) pk=python3-pyqt6 ;;
                arch:numpy) pk=python-numpy ;;
                fedora:numpy|debian:numpy) pk=python3-numpy ;;
                arch:mpl) pk=python-matplotlib ;;
                fedora:mpl|debian:mpl) pk=python3-matplotlib ;;
                arch:jeepney) pk=python-jeepney ;;
                fedora:jeepney|debian:jeepney) pk=python3-jeepney ;;
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
    dep_miss req pactl "pactl bulunamadı (Arch: libpulse, Fedora/Debian: pulseaudio-utils); ses yönetimi için gerekli" \
                       "pactl not found (Arch: libpulse, Fedora/Debian: pulseaudio-utils); needed to manage audio"
else
    # the sound server must be PipeWire (pipewire-pulse), not PulseAudio itself
    server="$(LC_ALL=C pactl info 2>/dev/null | sed -n 's/^Server Name: //p' || true)"
    if [[ -z "$server" ]]; then
        warn "$(L 'ses sunucusuna sorulamadı (pactl info); PipeWire + pipewire-pulse çalışıyor olmalı' \
                  'could not query the sound server (pactl info); PipeWire + pipewire-pulse must be running')"
    elif [[ "$server" != *"on PipeWire"* ]]; then
        dep_miss req pwpulse "ses sunucusu PipeWire değil ($server); Kulak Kesil PipeWire + pipewire-pulse ile çalışır" \
                             "the sound server is not PipeWire ($server); Kulak Kesil needs PipeWire + pipewire-pulse"
    fi
fi
pw_missing=()
for t in pw-cli pw-dump pw-metadata; do
    command -v "$t" >/dev/null 2>&1 || pw_missing+=("$t")
done
if (( ${#pw_missing[@]} )); then
    dep_miss req pwtools "PipeWire araçları yok: ${pw_missing[*]} (yönlendirme ve canlı EQ için gerekli)" \
                         "PipeWire tools not found: ${pw_missing[*]} (needed for routing and live EQ)"
fi
if ! quiet "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    pyver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    dep_miss req "" "Python >= 3.11 gerekli (bulunan: ${pyver:-?})" \
                    "Python >= 3.11 is required (found: ${pyver:-?})"
fi
if ! quiet "$PY" -c 'import numpy'; then
    dep_miss req numpy "Python modülü numpy yok; EQ ayarı üretilemez" \
                       "Python module numpy not found; needed to generate the EQ config"
fi
if ! quiet "$PY" -c 'import PyQt6.QtCore'; then
    dep_miss req qt "Python modülü PyQt6 yok; pencere, tepsi ve uygulama simgesi için gerekli" \
                    "Python module PyQt6 not found; needed for the window, the tray and the app icon"
fi
if ! dep_find 'lsp-plugins*.lv2' "${LV2_PATH:-}" "${LV2_DIRS[@]}"; then
    dep_miss req lv2 "LSP LV2 eklentileri (lsp-plugins*.lv2) bulunamadı; EQ zinciri bunları kullanır, yoksa sanal EQ çıkışları açılmaz" \
                     "LSP LV2 plugins (lsp-plugins*.lv2) not found; the EQ chain needs them, without them the virtual EQ outputs will not open"
fi
# has_module NAME: importable without importing it (no side effects such as
# ~/.config/matplotlib, and fast)
has_module() { quiet "$PY" -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec(sys.argv[1]) is None)' "$1"; }
if ! has_module matplotlib; then
    dep_miss rec mpl "Python modülü matplotlib yok; EQ eğrileri grafiği (kulak eq-ciz) çizilemez" \
                     "Python module matplotlib not found; the EQ curves plot (kulak eq-ciz) will not work"
fi
if ! has_module jeepney; then
    dep_miss rec jeepney "Python modülü jeepney yok; müzik çalarlar ve tarayıcı (MPRIS) tanınamaz" \
                         "Python module jeepney not found; music players and the browser (MPRIS) cannot be recognised"
fi
if ! dep_find 'librnnoise_ladspa.so' "${LADSPA_PATH:-}" "${LADSPA_DIRS[@]}"; then
    dep_miss rec rnnoise "librnnoise_ladspa.so bulunamadı; mikrofon gürültü bastırma çalışmaz" \
                         "librnnoise_ladspa.so not found; microphone noise suppression will not work"
fi
if ! has_module onnxruntime; then
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
    render_unit "$u" | write_file "$UNIT_DIR/$u" 0644
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
    render_unit "$MIC_UNIT" | write_file "$UNIT_DIR/$MIC_UNIT" 0644
    run systemctl --user daemon-reload
    run systemctl --user enable "$MIC_UNIT"
    run systemctl --user restart "$MIC_UNIT"
fi

# ---------------------------------------------------------------- wrappers
say "$(L "Komutlar: $BIN_DIR/{kulak,kulak-tepsi,kulak-app}" "Commands: $BIN_DIR/{kulak,kulak-tepsi,kulak-app}")"
for pair in "kulak:oto_eq.cli" "kulak-tepsi:oto_eq.tray" "kulak-app:oto_eq.app"; do
    name="${pair%%:*}"; mod="${pair#*:}"; dst="$BIN_DIR/$name"
    write_file "$dst" 0755 <<EOF
#!/bin/sh
$MARK
exec env PYTHONPATH=$(sh_quote "$SRC_DIR") $PY -m $mod "\$@"
EOF
done
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$(L "$BIN_DIR PATH içinde değil: oturumu kapatıp açınca çoğu dağıtımda eklenir; ya da bash/zsh: ~/.profile içine export PATH=\"\$HOME/.local/bin:\$PATH\", fish: fish_add_path $BIN_DIR" \
                 "$BIN_DIR is not in PATH: on most distros it is added after logging out and back in; otherwise bash/zsh: add export PATH=\"\$HOME/.local/bin:\$PATH\" to ~/.profile, fish: fish_add_path $BIN_DIR")" ;;
esac

# ---------------------------------------------------------------- desktop entries
# installed copies: absolute Exec/Icon + a marker key so uninstall knows they are ours
desktop_entry() {  # desktop_entry TEMPLATE WRAPPER
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
        case "$line" in
            Exec=*) line="Exec=$(desktop_exec "$BIN_DIR/$2")" ;;
            Icon=*) line="Icon=${ICON_FILE//\\/\\\\}" ;;
        esac
        printf '%s\n' "$line"
    done <"$SRC_DIR/$1"
    printf '%s\n' "$DESKTOP_MARK"
}

# install_entry TEMPLATE WRAPPER DEST MODE KEEP_OFF: write DEST unless it is someone
# else's file; with KEEP_OFF=1 a missing DEST after an earlier install means the user
# removed it (or turned it off in Settings): leave it that way
install_entry() {
    local tpl="$1" wrapper="$2" dest="$3" mode="$4" keep_off="$5"
    if [[ -e "$dest" ]] && ! is_ours "$dest" "$DESKTOP_MARK"; then
        warn "$(L "$dest bizim değil, dokunulmadı" "$dest is not ours, left untouched")"
        return 1
    fi
    if (( keep_off && PREV_INSTALL )) && [[ ! -e "$dest" ]]; then
        say "$(L "Yok (önceden kapatılmış/silinmiş), yeniden eklenmedi: $dest" \
                 "Absent (turned off or deleted earlier), not added again: $dest")"
        return 1
    fi
    desktop_entry "$tpl" "$wrapper" | write_file "$dest" "$mode"
}

say "$(L "Tepsi otomatik başlatma: $AUTOSTART_FILE" "Tray autostart: $AUTOSTART_FILE")"
AUTOSTART_ON=0
if install_entry oto-eq-tray.desktop kulak-tepsi "$AUTOSTART_FILE" 0644 1; then AUTOSTART_ON=1; fi

say "$(L "Uygulama menüsü: $APP_FILE" "Application menu entry: $APP_FILE")"
install_entry oto-eq.desktop kulak-app "$APP_FILE" 0644 0 || true

DESK="$(desktop_dir)"
if [[ -n "$DESK" ]]; then
    SHORTCUT="$DESK/Kulak Kesil.desktop"
    say "$(L "Masaüstü kısayolu: $SHORTCUT" "Desktop shortcut: $SHORTCUT")"
    if install_entry oto-eq.desktop kulak-app "$SHORTCUT" 0755 1 && command -v gio >/dev/null 2>&1; then
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
    if (( AUTOSTART_ON )); then
        warn "$(L 'grafik oturum yok; tepsi bir sonraki girişte otomatik başlayacak' 'no graphical session; the tray will start automatically at the next login')"
    else
        warn "$(L 'grafik oturum yok; tepsiyi açmak için: kulak-tepsi' 'no graphical session; to open the tray run: kulak-tepsi')"
    fi
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
