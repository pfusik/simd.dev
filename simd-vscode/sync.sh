#!/usr/bin/env bash
# Copy the latest simd-data.json into ./data/ so the VS Code extension can
# load it. Run after `scripts/build_all.sh` regenerates the DB.
#
# We *copy* rather than symlink because vsce / VS Code do not always follow
# symlinks when packaging or loading extensions, and a copy guarantees the
# extension is self-contained when installed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/../simd-tooltip/dist/simd-data.json"
DEST="$HERE/data/simd-data.json"

if [[ ! -f "$SRC" ]]; then
    echo "error: $SRC not found. Run scripts/build_all.sh first." >&2
    exit 1
fi

mkdir -p "$HERE/data"
cp "$SRC" "$DEST"
echo "synced $(wc -c < "$DEST" | tr -d ' ') bytes -> $DEST"
