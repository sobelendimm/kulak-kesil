#!/bin/sh
# One-time per-user setup for the AppImage (run as: kulak-kesil.AppImage --setup).
# Copies the AppImage to a stable place, writes the config and the systemd user
# units, and enables them. Safe to run again. Needs $APPIMAGE (set by the runtime).
set -eu
HERE="$(dirname "$(readlink -f "$0")")"          # .../usr/share/kulak-kesil
APPDIR="$(cd "$HERE/../../.." && pwd -P)"
PY="$APPDIR/usr/python/bin/python3"
[ -n "${APPIMAGE:-}" ] || { echo "ERROR: run this through the AppImage file (APPIMAGE is not set)" >&2; exit 1; }

CFG="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA/kulak-kesil/kulak-kesil.AppImage"
LAUNCH="$DATA/kulak-kesil/run.sh"
UNIT_DIR="$CFG/systemd/user"
CONF_DIR="$CFG/oto-eq"
case "$DEST$HOME" in *[[:space:]\"\\%\$\|\&]*) echo "ERROR: paths with spaces or special characters are not supported" >&2; exit 1 ;; esac
[ "$CFG" = "$HOME/.config" ] || { echo "ERROR: a custom XDG_CONFIG_HOME is not supported by the AppImage setup; use the AUR package or install.sh" >&2; exit 1; }

mkdir -p "$(dirname "$DEST")" "$CONF_DIR" "$UNIT_DIR"
if [ "$(readlink -f "$APPIMAGE")" != "$(readlink -f "$DEST")" ]; then
    cp "$APPIMAGE" "$DEST.new" && chmod 755 "$DEST.new" && mv -f "$DEST.new" "$DEST"
fi
# launcher: FUSE -> run the AppImage directly; no FUSE -> extract once into ~/.cache/kulak-kesil and reuse
cp "$HERE/appimage-run.sh" "$LAUNCH.new" && chmod 755 "$LAUNCH.new" && mv -f "$LAUNCH.new" "$LAUNCH"
for f in kurallar.toml profiller.toml; do
    [ -e "$CONF_DIR/$f" ] || cp "$HERE/config/$f" "$CONF_DIR/$f"
done
PYTHONPATH="$HERE" PYTHONNOUSERSITE=1 "$PY" -m oto_eq.eqgen generate
PYTHONPATH="$HERE" PYTHONNOUSERSITE=1 "$PY" -m oto_eq.micgen generate

cp "$HERE/systemd/oto-eq-sinks.service" "$HERE/systemd/oto-eq-mic.service" "$UNIT_DIR/"
# the router unit runs the AppImage itself instead of a source folder
grep -v '^WorkingDirectory=\|^Environment=PYTHONPATH=' "$HERE/systemd/oto-eq.service" \
    | sed "s|^ExecStart=.*|ExecStart=/usr/bin/env KULAK_KESIL_APPIMAGE=$DEST $LAUNCH --router|" > "$UNIT_DIR/oto-eq.service"

# commands in ~/.local/bin (the tray autostart entry and the app look for these)
BIN="$HOME/.local/bin"; MARK="# kulak-kesil AppImage wrapper"
mkdir -p "$BIN"
for pair in kulak:--cli kulak-app:--app kulak-tepsi:--tray; do
    name="${pair%%:*}"; flag="${pair#*:}"
    if [ -e "$BIN/$name" ] && ! grep -qF "$MARK" "$BIN/$name"; then
        echo "WARNING: $BIN/$name exists and is not ours; left untouched" >&2; continue
    fi
    printf '#!/bin/sh\n%s\nexec env KULAK_KESIL_APPIMAGE=%s %s %s "$@"\n' "$MARK" "$DEST" "$LAUNCH" "$flag" > "$BIN/$name.new"
    chmod 755 "$BIN/$name.new" && mv -f "$BIN/$name.new" "$BIN/$name"
done

# warms the extraction cache (no FUSE) so a failure shows here, not in systemd; it must not abort the setup
WARMUP=0
"$LAUNCH" --help >/dev/null || { WARMUP=1; echo "WARNING: the first launch test failed (see the message above); the services may not start until that is fixed" >&2; }
systemctl --user daemon-reload
systemctl --user enable oto-eq-sinks.service oto-eq.service oto-eq-mic.service
systemctl --user restart oto-eq.service
systemctl --user start oto-eq-sinks.service oto-eq-mic.service
echo "Done. Commands: kulak, kulak-app, kulak-tepsi (in $BIN; add it to PATH if needed)"
[ "$WARMUP" -eq 0 ] || { echo "ERROR: setup finished, but the launcher test failed (exit status 1)" >&2; exit 1; }
