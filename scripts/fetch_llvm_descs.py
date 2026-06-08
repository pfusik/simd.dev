#!/usr/bin/env python3
"""Fetch LLVM clang x86 intrinsic headers into cache/llvm_headers/.

Cross-platform replacement for fetch_llvm_descs.sh. These headers are
Apache-2.0 WITH LLVM-exception (redistributable) and carry Doxygen
comments per intrinsic that we later mine for short descriptions.

Cached files are reused; missing or newly-added ones are pulled from
raw.githubusercontent.com. A per-file failure logs to stderr and
continues -- mirroring the old shell loop -- so a flaky network or a
single renamed file doesn't abort the whole fetch.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "cache" / "llvm_headers"

API = "https://api.github.com/repos/llvm/llvm-project/contents/clang/lib/Headers"
BASE = "https://raw.githubusercontent.com/llvm/llvm-project/main/clang/lib/Headers"

# x86 headers: anything containing common x86 intrinsic substrings.
# Excludes arm_*, riscv_*, cuda, hip, etc.
KEEP = re.compile(
    r"^(.*intrin\.h|.*mmintrin\.h|adcintrin\.h|adxintrin\.h|amxintrin\.h"
    r"|amx\w*intrin\.h|avx\w*\.h|bmi\w*\.h|f16cintrin\.h|fma\w*intrin\.h"
    r"|fxsrintrin\.h|gfniintrin\.h|invpcidintrin\.h|keylockerintrin\.h"
    r"|lwpintrin\.h|lzcntintrin\.h|mm3dnow\.h|mmintrin\.h|movdirintrin\.h"
    r"|mwaitxintrin\.h|pconfigintrin\.h|pkuintrin\.h|popcntintrin\.h"
    r"|prfchwintrin\.h|raoint\w*\.h|rdpruintrin\.h|rdseedintrin\.h"
    r"|rtmintrin\.h|serializeintrin\.h|sgxintrin\.h|sha\w*intrin\.h"
    r"|sm[34]intrin\.h|smmintrin\.h|tbmintrin\.h|tmmintrin\.h"
    r"|tsxldtrkintrin\.h|uintrintrin\.h|usermsrintrin\.h|vaesintrin\.h"
    r"|vpclmulqdqintrin\.h|waitpkgintrin\.h|wbnoinvdintrin\.h|wmmintrin\.h"
    r"|xmmintrin\.h|xopintrin\.h|xsave\w*intrin\.h|xtestintrin\.h"
    r"|cmpccxaddintrin\.h|enqcmdintrin\.h|hresetintrin\.h|emmintrin\.h"
    r"|cetintrin\.h|cldemoteintrin\.h|clflushoptintrin\.h|clwbintrin\.h"
    r"|clzerointrin\.h|crc32intrin\.h|fcfintrin\.h|immintrin\.h|x86intrin\.h)$"
)
EXCLUDE = re.compile(
    r"(arm|riscv|cuda|hip|hexagon|nvptx|ppc|wasm|spir|loong|amdgpu|opencl"
    r"|builtins|float|stdarg|__|altivec|htm|s390)",
    re.IGNORECASE,
)

# GitHub's API requires a User-Agent; provide an identifiable one rather
# than letting urllib's default get rejected.
USER_AGENT = "simd.dev-fetch/1.0 (+https://simd.dev)"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r:
        return r.read()


def list_x86_headers() -> list[str]:
    files = json.loads(_get(API).decode("utf-8"))
    names: list[str] = []
    for f in files:
        if f.get("type") != "file":
            continue
        n = f["name"]
        if EXCLUDE.search(n):
            continue
        if KEEP.match(n):
            names.append(n)
    return names


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)

    print("listing clang headers ...")
    names = list_x86_headers()

    count = 0
    for n in names:
        out = DEST / n
        if out.exists():
            count += 1
            continue
        try:
            data = _get(f"{BASE}/{n}")
        except Exception as e:
            print(f"  failed: {n} ({e})", file=sys.stderr)
            out.unlink(missing_ok=True)
            continue
        out.write_bytes(data)
        count += 1

    print(f"fetched/cached {count} files in {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
