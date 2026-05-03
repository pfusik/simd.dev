#!/usr/bin/env bash
# End-to-end rebuild of data/intrinsics.jsonl from upstream sources.
#
# Idempotent: cached files are reused. Pass --refresh to force re-fetch.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

if [[ "${1:-}" == "--refresh" ]]; then
  echo ">> --refresh: clearing cache/"
  rm -rf cache/
fi

echo ">> [1/5] fetching ARM + Intel upstream data ..."
scripts/fetch.sh

echo ">> [2/5] fetching LLVM clang x86 headers ..."
scripts/fetch_llvm_descs.sh

echo ">> [3/5] extracting Doxygen descriptions from LLVM headers ..."
python3 scripts/extract_llvm_descs.py

echo ">> [4/5] building unified DB ..."
python3 scripts/build_db.py

echo ">> [5/5] building web artifacts (simd-tooltip/dist/) ..."
python3 scripts/build_web.py

echo
echo ">> done. outputs in data/ and simd-tooltip/dist/."
