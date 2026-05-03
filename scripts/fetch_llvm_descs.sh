#!/usr/bin/env bash
# Fetch LLVM clang x86 intrinsic headers into cache/llvm_headers/.
# These headers are Apache-2.0 WITH LLVM-exception (redistributable) and
# carry Doxygen comments per intrinsic that we can mine for short descriptions.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HERE/cache/llvm_headers"
mkdir -p "$DEST"

API="https://api.github.com/repos/llvm/llvm-project/contents/clang/lib/Headers"
BASE="https://raw.githubusercontent.com/llvm/llvm-project/main/clang/lib/Headers"

echo "listing clang headers ..."
NAMES=$(curl -fsSL "$API" | python3 -c "
import json, sys, re
files = json.load(sys.stdin)
# x86 headers: anything containing common x86 intrinsic substrings.
# Excludes arm_*, riscv_*, cuda, hip, etc.
KEEP = re.compile(r'^(.*intrin\.h|.*mmintrin\.h|adcintrin\.h|adxintrin\.h|amxintrin\.h|amx\w*intrin\.h|avx\w*\.h|bmi\w*\.h|f16cintrin\.h|fma\w*intrin\.h|fxsrintrin\.h|gfniintrin\.h|invpcidintrin\.h|keylockerintrin\.h|lwpintrin\.h|lzcntintrin\.h|mm3dnow\.h|mmintrin\.h|movdirintrin\.h|mwaitxintrin\.h|pconfigintrin\.h|pkuintrin\.h|popcntintrin\.h|prfchwintrin\.h|raoint\w*\.h|rdpruintrin\.h|rdseedintrin\.h|rtmintrin\.h|serializeintrin\.h|sgxintrin\.h|sha\w*intrin\.h|sm[34]intrin\.h|smmintrin\.h|tbmintrin\.h|tmmintrin\.h|tsxldtrkintrin\.h|uintrintrin\.h|usermsrintrin\.h|vaesintrin\.h|vpclmulqdqintrin\.h|waitpkgintrin\.h|wbnoinvdintrin\.h|wmmintrin\.h|xmmintrin\.h|xopintrin\.h|xsave\w*intrin\.h|xtestintrin\.h|cmpccxaddintrin\.h|enqcmdintrin\.h|hresetintrin\.h|emmintrin\.h|cetintrin\.h|cldemoteintrin\.h|clflushoptintrin\.h|clwbintrin\.h|clzerointrin\.h|crc32intrin\.h|fcfintrin\.h|immintrin\.h|x86intrin\.h)$')
EXCLUDE = re.compile(r'(arm|riscv|cuda|hip|hexagon|nvptx|ppc|wasm|spir|loong|amdgpu|opencl|builtins|float|stdarg|__|altivec|htm|s390)', re.I)
for f in files:
    if f.get('type') != 'file':
        continue
    n = f['name']
    if EXCLUDE.search(n):
        continue
    if KEEP.match(n):
        print(n)
")

count=0
for n in $NAMES; do
  if [ -f "$DEST/$n" ]; then
    count=$((count+1))
    continue
  fi
  curl -fsSL "$BASE/$n" -o "$DEST/$n" || { echo "  failed: $n" >&2; rm -f "$DEST/$n"; continue; }
  count=$((count+1))
done
echo "fetched/cached $count files in $DEST"
