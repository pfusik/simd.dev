#!/usr/bin/env bash
# End-to-end rebuild of data/intrinsics.jsonl from upstream sources.
#
# Idempotent: cached files are reused. Pass --refresh to force re-fetch.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# Pick a Python 3.9+ interpreter. We require 3.9 because build_db.py and
# extract_llvm_descs.py use PEP 585 generics (`dict[str, str]`, `list[X]`)
# at runtime without `from __future__ import annotations`. Try `python3`
# first (Unix convention), then `python` (Windows / some minimal envs).
MIN_MAJOR=3
MIN_MINOR=9
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import sys; sys.exit(0 if sys.version_info >= ($MIN_MAJOR, $MIN_MINOR) else 1)" >/dev/null 2>&1; then
      PY="$candidate"
      break
    fi
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: need Python ${MIN_MAJOR}.${MIN_MINOR}+ on PATH (tried python3, python)." >&2
  echo "       installed versions:" >&2
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      echo "         $candidate -> $("$candidate" --version 2>&1)" >&2
    fi
  done
  exit 1
fi
echo ">> using $PY ($("$PY" --version 2>&1))"

if [[ "${1:-}" == "--refresh" ]]; then
  echo ">> --refresh: clearing cache/"
  rm -rf cache/
fi

echo ">> [1/6] fetching ARM + Intel upstream data ..."
"$PY" scripts/fetch.py

echo ">> [2/6] fetching LLVM clang x86 headers ..."
"$PY" scripts/fetch_llvm_descs.py

echo ">> [3/6] extracting Doxygen descriptions from LLVM headers ..."
"$PY" scripts/extract_llvm_descs.py

echo ">> [4/6] building unified DB ..."
"$PY" scripts/build_db.py

echo ">> [5/6] building web artifacts (simd-tooltip/dist/) ..."
"$PY" scripts/build_web.py

echo ">> [6/6] building ARM per-microarch perf table via llvm-mca ..."
"$PY" scripts/build_arm_perf.py

echo
echo ">> done. outputs in data/ and simd-tooltip/dist/."
