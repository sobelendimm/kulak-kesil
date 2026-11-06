#!/usr/bin/env bash
# Download the optional audio-analysis models (see docs/MODELS.md) and verify them.
# Safe to re-run: files that already match their checksum are skipped.
set -euo pipefail

DEST="${XDG_DATA_HOME:-$HOME/.local/share}/oto-eq/models"
BASE="https://essentia.upf.edu/models"

# path under $BASE | sha256 of the .onnx
MODELS=(
    "feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1|a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c"
    "audio-event-recognition/yamnet/audioset-yamnet-1|9bc15ac91426e431527196ee6663de78dffcc7db53ac002d5afbda61429b456f"
)

command -v curl >/dev/null 2>&1 || { echo "error: curl is required" >&2; exit 1; }

cat <<'EOF'
These models are published by the Music Technology Group (UPF) under a
Creative Commons NON-COMMERCIAL license. They are for personal use only and
are not covered by Kulak Kesil's GPL-3.0 license. Details: docs/MODELS.md
EOF

mkdir -p "$DEST"
for entry in "${MODELS[@]}"; do
    path="${entry%%|*}"
    sum="${entry##*|}"
    name="${path##*/}"
    onnx="$DEST/$name.onnx"
    if [[ -f "$onnx" ]] && echo "$sum  $onnx" | sha256sum -c --quiet - 2>/dev/null \
            && [[ -s "$DEST/$name.json" ]]; then
        echo "ok      $name (already present)"
        continue
    fi
    echo "fetch   $name"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --retry 3 -o "$tmp/$name.onnx" "$BASE/$path.onnx"
    curl -fL --retry 3 -o "$tmp/$name.json" "$BASE/$path.json"
    if ! echo "$sum  $tmp/$name.onnx" | sha256sum -c --quiet -; then
        echo "error: checksum mismatch for $name.onnx — nothing installed" >&2
        exit 1
    fi
    mv "$tmp/$name.onnx" "$tmp/$name.json" "$DEST/"
    rm -rf "$tmp"
    trap - EXIT
    echo "ok      $name"
done

echo
echo "Models are in $DEST"
echo "Apply with: systemctl --user restart oto-eq   (then check: kulak saglik)"
