#!/usr/bin/env bash
# Build kulak-kesil-<version>-x86_64.AppImage.
#   packaging/appimage/build.sh [OUT_DIR]      (default: ./dist)
# Env: VERSION (default: git describe or 0.0.0), WORK (download/work dir, default OUT_DIR/work).
# Bundles a relocatable CPython (python-build-standalone) with PyQt6 + numpy + jeepney.
# matplotlib and onnxruntime are NOT bundled (optional features: EQ plot, audio models).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
OUT="$(mkdir -p "${1:-$SRC/dist}" && cd "${1:-$SRC/dist}" && pwd -P)"
WORK="${WORK:-$OUT/work}"
VERSION="${VERSION:-$(git -C "$SRC" describe --tags --always 2>/dev/null | sed 's/^v//' || true)}"
VERSION="${VERSION:-0.0.0}"

# pinned bundled interpreter (sha256 of the release asset)
PBS_TAG=20261124
PBS_FILE="cpython-3.13.18+${PBS_TAG}-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${PBS_FILE//+/%2B}"
PBS_SHA256="b56ceb4fc1d8f0c49b7bd4dc91cd176bad7b86a88b9fc509bae6b10b3ee36692"
TOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

mkdir -p "$WORK"
fetch() {  # fetch URL DEST (skipped when DEST already exists)
    if [[ ! -s "$2" ]]; then
        curl -fsSL --retry 3 -o "$2.part" "$1" || { echo "download failed: $1" >&2; exit 1; }
        mv -f "$2.part" "$2"
    fi
}
fetch "$PBS_URL" "$WORK/$PBS_FILE"
echo "$PBS_SHA256  $WORK/$PBS_FILE" | sha256sum -c - >/dev/null || { echo "checksum mismatch: $PBS_FILE" >&2; exit 1; }
fetch "$TOOL_URL" "$WORK/appimagetool"
chmod +x "$WORK/appimagetool"

APPDIR="$WORK/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/share/kulak-kesil" "$APPDIR/usr/python"
tar -xzf "$WORK/$PBS_FILE" -C "$APPDIR/usr/python" --strip-components=1
"$APPDIR/usr/python/bin/python3" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    -r "$SRC/packaging/appimage/requirements.txt"

# program tree (same layout as the AUR package)
cp -r "$SRC/oto_eq" "$SRC/config" "$SRC/systemd" "$APPDIR/usr/share/kulak-kesil/"
find "$APPDIR/usr/share/kulak-kesil" \( -name '__pycache__' -o -name '*.yedek*' \) -prune -exec rm -rf {} +
install -m755 "$SRC/packaging/appimage/setup.sh" "$APPDIR/usr/share/kulak-kesil/appimage-setup.sh"
install -m755 "$SRC/packaging/appimage/AppRun" "$APPDIR/AppRun"
install -m644 "$SRC/packaging/appimage/kulak-kesil.desktop" "$APPDIR/kulak-kesil.desktop"
echo "$VERSION" > "$APPDIR/usr/share/kulak-kesil/VERSION"
QT_QPA_PLATFORM=offscreen PYTHONPATH="$APPDIR/usr/share/kulak-kesil" PYTHONNOUSERSITE=1 \
    "$APPDIR/usr/python/bin/python3" -m oto_eq.ui_common icon "$APPDIR/kulak-kesil.png"
cp "$APPDIR/kulak-kesil.png" "$APPDIR/.DirIcon"

ARCH=x86_64 "$WORK/appimagetool" --appimage-extract-and-run --no-appstream "$APPDIR" \
    "$OUT/kulak-kesil-$VERSION-x86_64.AppImage"
echo "built: $OUT/kulak-kesil-$VERSION-x86_64.AppImage"
