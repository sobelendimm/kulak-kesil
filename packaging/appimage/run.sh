#!/bin/sh
# Launcher installed by `kulak-kesil.AppImage --setup` (used by the systemd unit and the ~/.local/bin commands).
# With FUSE the AppImage runs directly (it mounts itself: fast). Without FUSE the AppImage is extracted ONCE into
# ~/.cache/kulak-kesil/<size>-<mtime>/ and that copy is reused on every launch (the old way, APPIMAGE_EXTRACT_AND_RUN,
# unpacked the whole image on every start). Replacing the AppImage changes the key; stale copies are removed.
# FUSE is decided by a cheap static check (device, fusermount) AND a real short mount probe
# (`--appimage-mount`); an "ok" probe is remembered per AppImage build. A failed probe falls back to extraction.
# Extraction and pruning run under flock (two simultaneous starts extract once).
# Env (tests/debugging): KULAK_KESIL_APPIMAGE (path), KULAK_KESIL_FUSE=0|1|probe (1 = trust, 0 = never,
# probe = skip the static check, run only the mount probe).
set -u
APP="${KULAK_KESIL_APPIMAGE:-${XDG_DATA_HOME:-$HOME/.local/share}/kulak-kesil/kulak-kesil.AppImage}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/kulak-kesil"
[ -f "$APP" ] || { echo "kulak-kesil: AppImage not found: $APP (run it with --setup again)" >&2; exit 1; }
key="$(stat -c '%s-%Y' "$APP")" || { echo "kulak-kesil: cannot stat $APP" >&2; exit 1; }
dir="$CACHE/$key"

fuse_static() {  # cheap pre-filter; the (static) type-2 runtime needs no libfuse.so.2, only the device and fusermount
    [ -r /dev/fuse ] && [ -w /dev/fuse ] || return 1
    command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1
}

fuse_probe() {  # `--appimage-mount` prints the mount point BEFORE it mounts: success = AppRun visible inside it
    out="$(mktemp)" || return 1
    "$APP" --appimage-mount >"$out" 2>/dev/null &
    pid=$!
    i=0; res=1
    while [ "$i" -lt 50 ]; do
        mnt="$(head -n 1 "$out" 2>/dev/null)"
        if [ -n "$mnt" ] && [ -e "$mnt/AppRun" ]; then res=0; break; fi
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1; i=$((i + 1))
    done
    kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    if [ -n "${mnt:-}" ] && [ -e "$mnt" ]; then   # the runtime may leave its FUSE mount behind when killed
        if command -v fusermount3 >/dev/null 2>&1; then fusermount3 -u -z "$mnt" 2>/dev/null
        elif command -v fusermount >/dev/null 2>&1; then fusermount -u -z "$mnt" 2>/dev/null; fi
    fi
    rm -f "$out"
    return "$res"
}

fuse_ok() {
    case "${KULAK_KESIL_FUSE:-}" in 1) return 0 ;; 0) return 1 ;; probe) ;; *) fuse_static || return 1 ;; esac
    [ -e "$CACHE/.fuse-ok-$key" ] && return 0
    # a failed probe is remembered for an hour (every start would otherwise wait for it again)
    [ -n "$(find "$CACHE/.fuse-no-$key" -mmin -60 2>/dev/null)" ] && return 1
    if fuse_probe; then
        mkdir -p "$CACHE" 2>/dev/null || return 0
        find "$CACHE" -maxdepth 1 -name '.fuse-*' -exec rm -f {} + 2>/dev/null   # markers of replaced builds
        : > "$CACHE/.fuse-ok-$key"
        return 0
    fi
    mkdir -p "$CACHE" 2>/dev/null && : > "$CACHE/.fuse-no-$key"
    return 1
}

in_use() {  # some process runs from, or has mapped files of, this extracted copy (a tray/app started before an upgrade)
    for p in /proc/[0-9]*; do
        case "$(readlink "$p/exe" 2>/dev/null)" in "$1"/*) return 0 ;; esac
        case "$(readlink "$p/cwd" 2>/dev/null)" in "$1"|"$1"/*) return 0 ;; esac
        grep -qF "$1/" "$p/maps" 2>/dev/null && return 0
        tr '\0' ' ' < "$p/cmdline" 2>/dev/null | grep -qF "$1/" && return 0
    done
    return 1
}

if fuse_ok; then
    exec "$APP" "$@"
fi

mkdir -p "$CACHE" || exit 1
if command -v flock >/dev/null 2>&1; then
    exec 9>"$CACHE/.lock" && flock 9
fi
if [ ! -x "$dir/AppRun" ]; then
    tmp="$(mktemp -d "$CACHE/.extract.XXXXXX")" || exit 1
    if ! (cd "$tmp" && "$APP" --appimage-extract >/dev/null 2>&1) || [ ! -x "$tmp/squashfs-root/AppRun" ]; then
        rm -rf "$tmp"
        echo "kulak-kesil: could not extract $APP" >&2
        exit 1
    fi
    if [ -e "$dir" ]; then rm -rf "$tmp"; else   # else: a parallel start did not win the race
        if ! mv "$tmp/squashfs-root" "$dir"; then
            rm -rf "$tmp"
            echo "kulak-kesil: could not move the extracted copy into $dir" >&2
            exit 1
        fi
        rmdir "$tmp"
    fi
fi
# half-extracted leftovers of a killed start (a running extraction is younger than an hour)
find "$CACHE" -maxdepth 1 -name '.extract.*' -mmin +60 -exec rm -rf {} + 2>/dev/null
for old in "$CACHE"/*; do   # stale copies of replaced AppImages, unless still running
    [ -d "$old" ] && [ "$old" != "$dir" ] && ! in_use "$old" && rm -rf "$old"
done
exec 9>&-   # release the lock: the app must not inherit it
exec "$dir/AppRun" "$@"
