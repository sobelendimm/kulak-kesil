#!/bin/sh
# Launcher installed by `kulak-kesil.AppImage --setup` (used by the systemd unit and the ~/.local/bin commands).
# With FUSE the AppImage runs directly (it mounts itself: fast). Without FUSE the AppImage is extracted ONCE into
# ~/.cache/kulak-kesil/<size>-<mtime>/ and that copy is reused on every launch (the old way, APPIMAGE_EXTRACT_AND_RUN,
# unpacked the whole image on every start). Replacing the AppImage changes the key; stale copies are removed.
# Env (tests/debugging): KULAK_KESIL_APPIMAGE (path), KULAK_KESIL_FUSE=0|1 (skip detection), KULAK_KESIL_LIBDIRS.
set -u
APP="${KULAK_KESIL_APPIMAGE:-${XDG_DATA_HOME:-$HOME/.local/share}/kulak-kesil/kulak-kesil.AppImage}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/kulak-kesil"
[ -f "$APP" ] || { echo "kulak-kesil: AppImage not found: $APP (run it with --setup again)" >&2; exit 1; }

fuse_ok() {
    case "${KULAK_KESIL_FUSE:-}" in 1) return 0 ;; 0) return 1 ;; esac
    [ -r /dev/fuse ] && [ -w /dev/fuse ] || return 1
    command -v fusermount3 >/dev/null 2>&1 || command -v fusermount >/dev/null 2>&1 || return 1
    # the type-2 runtime loads libfuse.so.2 (a host with only fuse3 cannot run the AppImage directly)
    for d in ${KULAK_KESIL_LIBDIRS:-/usr/lib /usr/lib64 /lib /lib64 /usr/lib/x86_64-linux-gnu /lib/x86_64-linux-gnu}; do
        [ -e "$d/libfuse.so.2" ] && return 0
    done
    return 1
}

in_use() {  # some process runs from this extracted copy (a tray/app started before an upgrade)
    for f in /proc/[0-9]*/cmdline; do
        tr '\0' ' ' < "$f" 2>/dev/null | grep -qF "$1/" && return 0
    done
    return 1
}

if fuse_ok; then
    exec "$APP" "$@"
fi

key="$(stat -c '%s-%Y' "$APP")" || { echo "kulak-kesil: cannot stat $APP" >&2; exit 1; }
dir="$CACHE/$key"
if [ ! -x "$dir/AppRun" ]; then
    mkdir -p "$CACHE" || exit 1
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
exec "$dir/AppRun" "$@"
