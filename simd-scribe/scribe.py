#!/usr/bin/env python3
"""simd-scribe: verify SIMD intrinsic outputs by constant-folding through clang.

Pilot scope: 2-input NEON intrinsics with same-typed vector inputs/output
(vaddq_s8, vsubq_u16, etc.). Handles the vadd/vsub cluster (36 members).
Will broaden as needed for further pilot waves.

Pipeline per (intrinsic, [input_vectors]):
  1. Look up record in data/intrinsics.jsonl for the signature.
  2. Generate a tiny C++ source declaring inputs as const vectors and
     RESULT = intrinsic(...) as an extern "C" const global.
  3. Compile with -O2 -c to a Mach-O object. Clang folds the call;
     RESULT lands in __const with no code in __text.
  4. Read the bytes back via llvm-objdump.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@functools.lru_cache(maxsize=1)
def _rosetta_available() -> bool:
    """True iff Apple's Rosetta 2 can run x86_64 binaries on this host.

    We probe with `arch -x86_64 /usr/bin/true`. If exit 0, Rosetta is up
    and we can use the Mach-O cross-compile + execute path for Intel
    intrinsics. Otherwise we fall back to Linux/ELF freestanding cross-
    compile, which only supports the fold path.
    """
    try:
        proc = subprocess.run(
            ["arch", "-x86_64", "/usr/bin/true"],
            capture_output=True, timeout=2,
        )
        return proc.returncode == 0
    except Exception:
        return False

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "intrinsics.jsonl"
HINTS_PATH = Path(__file__).resolve().parent / "hints.json"


@functools.lru_cache(maxsize=1)
def _load_hints() -> list[tuple["re.Pattern[str]", dict]]:
    """Compiled (pattern, params) pairs from hints.json. Empty if absent.

    Format: top-level dict whose keys are regex patterns matched against
    the intrinsic name (re.search), and whose values are param-name → hint
    dicts. Hint values are scalars (immediates) or lists (vector / pointer
    buffers); lists are truncated to the per-variant lane count and cycled
    if shorter. Multiple patterns may match the same name; matches merge
    in declaration order with later entries overriding earlier ones on
    the same param-name.
    """
    if not HINTS_PATH.exists():
        return []
    raw = json.loads(HINTS_PATH.read_text())
    # Underscore-prefixed keys (like `_comment`) are reserved for inline
    # documentation. Skip them so they don't get compiled as regexes.
    return [
        (re.compile(pat), params)
        for pat, params in raw.items()
        if not pat.startswith("_")
    ]


@functools.lru_cache(maxsize=4096)
def _hints_for(intrinsic_name: str) -> dict:
    merged: dict = {}
    for pat, params in _load_hints():
        if pat.search(intrinsic_name):
            merged.update(params)
    return merged


def _apply_hint_list(hint, count: int) -> list:
    """Stretch a hint value to `count` lanes: truncate if too long,
    cycle if too short. A scalar repeats across all lanes."""
    if not isinstance(hint, list):
        return [hint] * count
    if not hint:
        raise ValueError("hint list must not be empty")
    if len(hint) >= count:
        return list(hint[:count])
    out: list = []
    while len(out) < count:
        out.extend(hint)
    return out[:count]

# Use Homebrew LLVM (currently 22.x) so our fold behavior matches the
# clang-trunk that godbolt's CE iframe uses, not the older Apple Clang
# that lags by a release or two. Override with $SIMD_SCRIBE_LLVM_BIN if
# you have a different toolchain installed.
LLVM_BIN = os.environ.get(
    "SIMD_SCRIBE_LLVM_BIN", "/opt/homebrew/Cellar/llvm/22.1.4/bin"
)
CLANG = f"{LLVM_BIN}/clang++"
LLVM_OBJDUMP = f"{LLVM_BIN}/llvm-objdump"

# Minimum clang major required to reproduce the cached fold/execute
# classifications. v22 is where table-lookup IR folding, FRINT (v8.5a)
# and MMLA (i8mm) intrinsics all work; older versions silently produce
# fewer fold entries and drop ~150 NEON intrinsics from the cache.
_MIN_CLANG_MAJOR = 22


@functools.lru_cache(maxsize=1)
def _check_clang_version() -> None:
    """Fail fast if CLANG is older than what the cache was built with."""
    proc = subprocess.run(
        [CLANG, "--version"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"scribe: cannot run {CLANG!r}: {proc.stderr.strip() or 'no stderr'}"
        )
    m = re.search(r"clang version (\d+)", proc.stdout)
    if not m:
        raise RuntimeError(
            f"scribe: cannot parse clang version from {proc.stdout!r}"
        )
    major = int(m.group(1))
    if major < _MIN_CLANG_MAJOR:
        raise RuntimeError(
            f"scribe needs clang >= {_MIN_CLANG_MAJOR} (got {major}). "
            f"Older clangs lose ~150 NEON intrinsics (FRINT/MMLA/...) and "
            f"miss table-lookup fold opportunities. Install Homebrew LLVM "
            f"and point $SIMD_SCRIBE_LLVM_BIN at it (currently {CLANG})."
        )


@functools.lru_cache(maxsize=1)
def _macos_sdk_path() -> str | None:
    """Resolve the active macOS SDK so Homebrew clang sees system libc/libc++.

    Homebrew's clang doesn't ship an Apple sysroot; without -isysroot it
    falls back to a stub path and the libc++ headers blow up looking for
    `mbstate_t`. We ask `xcrun` once and reuse the answer.
    """
    try:
        out = subprocess.run(
            ["xcrun", "--sdk", "macosx", "--show-sdk-path"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or None
    except Exception:
        return None


@dataclass(frozen=True)
class TypeInfo:
    """Lane shape for a NEON vector type, scalar, or tuple-of-vectors.

    kind ∈ {"int", "uint", "float", "bfloat", "poly"}.
    bits = lane element width.
    count = lane count of one sub-vector (1 for scalars).
    tuple_size = how many such sub-vectors (e.g. int8x16x2_t -> 2).
    Most types have tuple_size = 1.
    """
    kind: str
    bits: int
    count: int
    tuple_size: int = 1

    @property
    def total_lanes(self) -> int:
        return self.count * self.tuple_size

    @property
    def total_bytes(self) -> int:
        return (self.bits * self.total_lanes) // 8


# Vector type table -- ACLE name -> TypeInfo.
NEON_VEC_TYPES: dict[str, TypeInfo] = {
    # Signed integer
    "int8x8_t":   TypeInfo("int",  8,  8),
    "int8x16_t":  TypeInfo("int",  8, 16),
    "int16x4_t":  TypeInfo("int", 16,  4),
    "int16x8_t":  TypeInfo("int", 16,  8),
    "int32x2_t":  TypeInfo("int", 32,  2),
    "int32x4_t":  TypeInfo("int", 32,  4),
    "int64x1_t":  TypeInfo("int", 64,  1),
    "int64x2_t":  TypeInfo("int", 64,  2),
    # Unsigned integer
    "uint8x8_t":   TypeInfo("uint",  8,  8),
    "uint8x16_t":  TypeInfo("uint",  8, 16),
    "uint16x4_t":  TypeInfo("uint", 16,  4),
    "uint16x8_t":  TypeInfo("uint", 16,  8),
    "uint32x2_t":  TypeInfo("uint", 32,  2),
    "uint32x4_t":  TypeInfo("uint", 32,  4),
    "uint64x1_t":  TypeInfo("uint", 64,  1),
    "uint64x2_t":  TypeInfo("uint", 64,  2),
    # Floating-point
    "float16x4_t": TypeInfo("float", 16, 4),
    "float16x8_t": TypeInfo("float", 16, 8),
    "float32x2_t": TypeInfo("float", 32, 2),
    "float32x4_t": TypeInfo("float", 32, 4),
    "float64x1_t": TypeInfo("float", 64, 1),
    "float64x2_t": TypeInfo("float", 64, 2),
    # BFloat16
    "bfloat16x4_t": TypeInfo("bfloat", 16, 4),
    "bfloat16x8_t": TypeInfo("bfloat", 16, 8),
    # Polynomial (treated as unsigned integer for input/output bytes)
    "poly8x8_t":   TypeInfo("poly",  8,  8),
    "poly8x16_t":  TypeInfo("poly",  8, 16),
    "poly16x4_t":  TypeInfo("poly", 16,  4),
    "poly16x8_t":  TypeInfo("poly", 16,  8),
    "poly64x1_t":  TypeInfo("poly", 64,  1),
    "poly64x2_t":  TypeInfo("poly", 64,  2),
}

# Scalar param / return types (e.g. vaddd_s64 takes/returns int64_t).
SCALAR_TYPES: dict[str, TypeInfo] = {
    "int8_t":   TypeInfo("int",  8, 1),
    "int16_t":  TypeInfo("int", 16, 1),
    "int32_t":  TypeInfo("int", 32, 1),
    "int64_t":  TypeInfo("int", 64, 1),
    "uint8_t":  TypeInfo("uint",  8, 1),
    "uint16_t": TypeInfo("uint", 16, 1),
    "uint32_t": TypeInfo("uint", 32, 1),
    "uint64_t": TypeInfo("uint", 64, 1),
    "float16_t": TypeInfo("float", 16, 1),
    "float32_t": TypeInfo("float", 32, 1),
    "float64_t": TypeInfo("float", 64, 1),
    "bfloat16_t": TypeInfo("bfloat", 16, 1),
    "poly8_t":  TypeInfo("poly",  8, 1),
    "poly16_t": TypeInfo("poly", 16, 1),
    "poly64_t": TypeInfo("poly", 64, 1),
    "poly128_t": TypeInfo("poly", 128, 1),
    # Plain C int (Intel uses these for extract / insert / status returns).
    "int":      TypeInfo("int", 32, 1),
    "unsigned": TypeInfo("uint", 32, 1),
    "unsigned int": TypeInfo("uint", 32, 1),
    "char":     TypeInfo("int",  8, 1),
    "short":    TypeInfo("int", 16, 1),
    "long":     TypeInfo("int", 64, 1),
    "long long": TypeInfo("int", 64, 1),
    "__int64":  TypeInfo("int", 64, 1),
    "__int32":  TypeInfo("int", 32, 1),
    # AVX-512 bitmask scalars: 1 bit per lane, N lanes wide. Stored as
    # a 1/2/4/8-byte little-endian unsigned integer. We decode them into
    # a per-bit value list so the worked-example UI can render each lane
    # as 0 or 1 with the existing lane-cell renderer.
    "__mmask8":  TypeInfo("mask", 1,  8),
    "__mmask16": TypeInfo("mask", 1, 16),
    "__mmask32": TypeInfo("mask", 1, 32),
    "__mmask64": TypeInfo("mask", 1, 64),
}

_INTEL_MASK_WIDTHS = {
    "__mmask8": 8, "__mmask16": 16, "__mmask32": 32, "__mmask64": 64,
}


def _mask_synth_bits(role: str, bits: int) -> list[int]:
    """Default mask pattern: alternating-ish bits truncated to `bits`.
    Different roles get distinct patterns so kmask ops (kand/kxor/...)
    show non-trivial results in the worked example."""
    pattern = 0xA5A5A5A5A5A5A5A5 if role == "a" else 0x5A5A5A5A5A5A5A5A
    val = pattern & ((1 << bits) - 1)
    return [(val >> i) & 1 for i in range(bits)]


# --------------------------------------------------------------------------
# Intel (x86) SIMD support.
#
# Unlike ARM ACLE, Intel's `__m128i` / `__m128` / `__m128d` are lane-agnostic
# C types. The lane shape of `__m128i` -- whether it's 16 int8s, 8 int16s,
# etc. -- comes from the *intrinsic name* suffix (`_epi8`, `_epi16`, ...).
# So we can't fill in TypeInfo from the type alone for `__m128i`; we need
# the intrinsic name. The two helper layers below do that.
# --------------------------------------------------------------------------

# Type tables: known sizes for the C type itself. Lane shape comes from the
# intrinsic name (see intel_lane_info_for).
INTEL_VEC_BYTES: dict[str, int] = {
    "__m64":       8,
    "__m128":     16,
    "__m128d":    16,
    "__m128i":    16,
    "__m128h":    16,
    "__m128bh":   16,
    "__m256":     32,
    "__m256d":    32,
    "__m256i":    32,
    "__m256h":    32,
    "__m256bh":   32,
    "__m512":     64,
    "__m512d":    64,
    "__m512i":    64,
    "__m512h":    64,
    "__m512bh":   64,
}


def is_intel_intrinsic(name: str) -> bool:
    # `_mm*` covers SSE/AVX, `_tile*` is AMX, `_k*` is the AVX-512 kmask
    # algebra (_kand_mask16, _kxor_mask32, _kunpackb, ...). The `_k`
    # prefix doesn't collide with any arm-acle intrinsic.
    return bool(re.match(r"^_(?:mm|tile|k)\w*$", name))


# `_mm_<op>_<suffix>` -> lane info derived from the suffix.
#  - epiNN / piNN  -> signed integer, NN-bit lanes
#  - epuNN / puNN  -> unsigned integer, NN-bit lanes
#  - siNN          -> "untyped" 128/256/512-bit -- treat as uint8 lanes
#  - ps / ss       -> 32-bit float (vector / scalar)
#  - pd / sd       -> 64-bit float
#  - ph            -> 16-bit float (FP16)
#  - pbh           -> bf16
def intel_lane_info_for(
    intrinsic_name: str, c_type: str, context: str = "output"
) -> TypeInfo:
    """Lane shape of `c_type` in the call to `intrinsic_name`.

    `context` ∈ {"input", "output"}: for convert intrinsics like
    `_mm_cvtepi8_epi64`, the source suffix governs input lanes, the
    destination suffix governs output lanes. For ordinary intrinsics
    with a single suffix, the choice doesn't matter.
    """
    bytes_total = INTEL_VEC_BYTES.get(c_type)
    if bytes_total is None:
        raise ValueError(f"unknown intel C type: {c_type}")

    def _pick(pattern: str) -> "re.Match | None":
        if context == "output":
            ms = list(re.finditer(pattern, intrinsic_name))
            return ms[-1] if ms else None
        return re.search(pattern, intrinsic_name)

    # `_e?p[iu]NN` followed by either end-of-string or another `_` token
    # (e.g. `_mask`, `_mask3`, `_round`, ...). `\b` doesn't work here:
    # between `2` and `_` both sides are word chars, no boundary.
    m = _pick(r"_e?p[iu](8|16|32|64)(?=_|$)")
    if m:
        bits = int(m.group(1))
        kind = "uint" if "u" in m.group(0) else "int"
        return TypeInfo(kind, bits, bytes_total * 8 // bits)
    m = _pick(r"_si(\d+)(?=_|$)")
    if m:
        return TypeInfo("uint", 8, bytes_total)
    if re.search(r"_(?:ps|ss)\b", intrinsic_name):
        return TypeInfo("float", 32, bytes_total // 4)
    if re.search(r"_(?:pd|sd)\b", intrinsic_name):
        return TypeInfo("float", 64, bytes_total // 8)
    if re.search(r"_(?:ph|sh)\b", intrinsic_name):
        return TypeInfo("float", 16, bytes_total // 2)
    if re.search(r"_pbh\b", intrinsic_name):
        return TypeInfo("bfloat", 16, bytes_total // 2)
    if c_type.endswith("bh"):
        return TypeInfo("bfloat", 16, bytes_total // 2)
    if c_type.endswith("h"):
        return TypeInfo("float", 16, bytes_total // 2)
    if c_type.endswith("d"):
        return TypeInfo("float", 64, bytes_total // 8)
    if c_type in ("__m128", "__m256", "__m512"):
        return TypeInfo("float", 32, bytes_total // 4)
    return TypeInfo("uint", 8, bytes_total)


def intel_setr_call(values: list, info: TypeInfo) -> str:
    """C source for `_mm{,256,512}_setr_<suffix>(values...)` matching the
    lane shape of `info`. `_setr_*` puts values in *memory order* (low
    addr -> first arg), which makes the harness much easier to read.

    Special-case for 128-bit 64-lane: `_mm_setr_epi64x` does not exist
    (only `_mm_set_epi64x` does), so we use `_set` with reversed args.
    """
    width = info.bits * info.count    # total bits, e.g. 128
    prefix = {128: "_mm", 256: "_mm256", 512: "_mm512"}.get(width)
    if prefix is None:
        raise ValueError(f"can't pick set-builder for width {width}")
    suffix_map = {
        ("int", 8):  "epi8",
        ("int", 16): "epi16",
        ("int", 32): "epi32",
        ("int", 64): "epi64x",
        ("uint", 8):  "epi8",
        ("uint", 16): "epi16",
        ("uint", 32): "epi32",
        ("uint", 64): "epi64x",
        ("float", 16): "ph",
        ("float", 32): "ps",
        ("float", 64): "pd",
        ("bfloat", 16): "pbh",
    }
    suffix = suffix_map.get((info.kind, info.bits))
    if suffix is None:
        raise ValueError(f"no setr builder for {info.kind}{info.bits}")
    # 128-bit + 64-bit lanes: no _mm_setr_epi64x. Use _set with reversed args.
    if width == 128 and info.bits == 64:
        rev = list(reversed(values))
        args = ", ".join(_intel_lane_literal(v, info) for v in rev)
        return f"{prefix}_set_{suffix}({args})"
    args = ", ".join(_intel_lane_literal(v, info) for v in values)
    return f"{prefix}_setr_{suffix}({args})"


def _intel_lane_literal(v, info: TypeInfo) -> str:
    if info.kind in ("int", "uint", "poly"):
        # Suffix the constant for 64-bit lanes so the compiler doesn't
        # complain about the literal type.
        if info.bits == 64:
            return f"{v}LL" if info.kind == "int" else f"{v}ULL"
        return str(v)
    if info.kind == "float":
        f = float(v)
        if info.bits == 32:
            return f"{f!r}f"
        if info.bits == 16:
            return f"((__fp16){f!r}f)"
        return repr(f)
    if info.kind == "bfloat":
        return f"((__bf16){float(v)!r}f)"
    return str(v)


def intel_compile_flags(family_list: list[str]) -> list[str]:
    """Cross-compile flags for an x86 SIMD family.

    Two modes:
    - With Rosetta: target `x86_64-apple-macos` so the linked binary
      runs natively under Rosetta 2 -- unlocks the execute fallback for
      intrinsics clang doesn't IR-fold (saturating, table lookups, ...).
      The Apple SDK is available on disk so <cstdio> etc. work.
    - Without Rosetta: target `x86_64-linux-gnu -ffreestanding` so we
      can read .rodata bytes from the .o without needing a Linux sysroot.
      Fold-only.
    """
    if _rosetta_available():
        flags = ["-target", "x86_64-apple-macos"]
    else:
        flags = ["-target", "x86_64-linux-gnu", "-ffreestanding"]
    seen = set()
    for f in family_list:
        for out in INTEL_FAMILY_FLAGS.get(f, []):
            if out not in seen:
                seen.add(out)
                flags.append(out)
    if not seen:
        flags.append("-mavx2")
    return flags


# Map of Intel family-name -> march flags. Subsumes most of CE_INTEL_FLAGS
# from simd-tooltips.js. Add more as we extend coverage.
INTEL_FAMILY_FLAGS: dict[str, list[str]] = {
    "MMX":       ["-mmmx"],
    "SSE":       ["-msse"],
    "SSE2":      ["-msse2"],
    "SSE3":      ["-msse3"],
    "SSSE3":     ["-mssse3"],
    "SSE4.1":    ["-msse4.1"],
    "SSE4.2":    ["-msse4.2"],
    "AVX":       ["-mavx"],
    "AVX2":      ["-mavx2"],
    "FMA":       ["-mfma"],
    "BMI1":      ["-mbmi"],
    "BMI2":      ["-mbmi2"],
    "AES":       ["-maes"],
    "PCLMULQDQ": ["-mpclmul"],
    "GFNI":      ["-mgfni"],
    "VAES":      ["-mvaes"],
    "VPCLMULQDQ":["-mvpclmulqdq"],
    # AVX-512 base + extensions. The iguide tags families as `AVX512F`,
    # `AVX512BW`, etc. (no dash/slash); each AVX-512 ext implicitly needs
    # `-mavx512f` first or clang refuses the builtins.
    "AVX512F":   ["-mavx512f"],
    "AVX512CD":  ["-mavx512f", "-mavx512cd"],
    "AVX512BW":  ["-mavx512f", "-mavx512bw"],
    "AVX512DQ":  ["-mavx512f", "-mavx512dq"],
    "AVX512VL":  ["-mavx512f", "-mavx512vl"],
    "AVX512_FP16":          ["-mavx512f", "-mavx512fp16"],
    "AVX512_BF16":          ["-mavx512f", "-mavx512bf16"],
    "AVX512_VBMI":          ["-mavx512f", "-mavx512vbmi"],
    "AVX512_VBMI2":         ["-mavx512f", "-mavx512vbmi2"],
    "AVX512_VNNI":          ["-mavx512f", "-mavx512vnni"],
    "AVX512_BITALG":        ["-mavx512f", "-mavx512bitalg"],
    "AVX512VPOPCNTDQ":      ["-mavx512f", "-mavx512vpopcntdq"],
    "AVX512IFMA52":         ["-mavx512f", "-mavx512ifma"],
    "AVX512_VP2INTERSECT":  ["-mavx512f", "-mavx512vp2intersect"],
    # AVX-VNNI / AVX-IFMA / etc. (post-AVX2, pre-AVX-512 extensions).
    "AVX_VNNI":             ["-mavxvnni"],
    "AVX_VNNI_INT8":        ["-mavxvnniint8"],
    "AVX_VNNI_INT16":       ["-mavxvnniint16"],
    "AVX_IFMA":             ["-mavxifma"],
    "AVX_NE_CONVERT":       ["-mavxneconvert"],
}


def _make_tuple_return_types() -> dict[str, TypeInfo]:
    """Synthesize int8x16x2_t / int8x16x3_t / int8x16x4_t / ... from each
    base vector type. These appear as return types for vld2 / vld3 / vld4
    (de-interleaved loads). Inputs of these types come from vst2/vst3/vst4
    -- not yet supported.
    """
    out: dict[str, TypeInfo] = {}
    for base_name, base in NEON_VEC_TYPES.items():
        if base.tuple_size != 1:
            continue
        stem = base_name[:-2]  # strip trailing "_t"
        for n in (2, 3, 4):
            out[f"{stem}x{n}_t"] = TypeInfo(base.kind, base.bits, base.count, tuple_size=n)
    return out


TUPLE_RETURN_TYPES: dict[str, TypeInfo] = _make_tuple_return_types()


def type_info(type_name: str) -> TypeInfo | None:
    return (
        NEON_VEC_TYPES.get(type_name)
        or SCALAR_TYPES.get(type_name)
        or TUPLE_RETURN_TYPES.get(type_name)
    )


# `<elem> const *` or `const <elem> *` -- the ACLE convention for "load N
# elements of type <elem> through this pointer".
_PTR_LOAD_RE = re.compile(r"^(\w+)\s+const\s*\*$|^const\s+(\w+)\s*\*$")
# Plain `<elem> *` -- the ACLE convention for "store N elements through
# this pointer". Excluded from `_PTR_LOAD_RE` deliberately.
_PTR_STORE_RE = re.compile(r"^(\w+)\s*\*$")


def is_load_pointer(type_name: str) -> str | None:
    """Return the element type if `type_name` is a const pointer (load), else None."""
    m = _PTR_LOAD_RE.match(type_name)
    if m:
        return m.group(1) or m.group(2)
    return None


def is_store_pointer(type_name: str) -> str | None:
    """Return the element type if `type_name` is a writable pointer (store), else None."""
    if is_load_pointer(type_name) is not None:
        return None
    m = _PTR_STORE_RE.match(type_name)
    if m:
        return m.group(1)
    return None


_LANE_DUP_RE = re.compile(r"^v(?:ld|st)(\d+)q?_(?:lane|dup)_")


def effective_return(sig: "Signature") -> tuple[TypeInfo, str]:
    """The shape we're verifying.

    For non-void returns, that's just the declared return type.
    For void-returning stores, it's the *output buffer* the store writes
    to: a synthetic `std::array<elem, N>` whose element type matches the
    pointer parameter and whose length comes from the data parameter (or
    from the `vst<N>_lane_` / `vst<N>_dup_` rules).

    Returns (TypeInfo describing the buffer shape, C++ type string).
    """
    if sig.return_type != "void":
        # Intel `__m128i` / `__m256i` / `__m512i` are lane-agnostic at the
        # C-type level; the lane shape comes from the intrinsic name.
        if is_intel_intrinsic(sig.name) and sig.return_type in INTEL_VEC_BYTES:
            return intel_lane_info_for(sig.name, sig.return_type), sig.return_type
        ti = type_info(sig.return_type)
        if ti is None:
            raise ValueError(f"unsupported return type: {sig.return_type}")
        return ti, sig.return_type

    # void return: must be a store. Find the writable pointer + the data
    # parameter (the vector or tuple being stored).
    ptr_idx = None
    elem = None
    for i, p in enumerate(sig.params):
        e = is_store_pointer(p.type_name)
        if e is not None:
            ptr_idx, elem = i, e
            break
    if elem is None:
        raise ValueError(f"void-return intrinsic with no store pointer: {sig.name}")
    elem_ti = type_info(elem)
    if elem_ti is None:
        raise ValueError(f"unknown store element type: {elem}")

    m = _LANE_DUP_RE.match(sig.name)
    if m:
        count = int(m.group(1))
    else:
        # Take the first vector / tuple param other than the pointer.
        count = 0
        for i, p in enumerate(sig.params):
            if i == ptr_idx:
                continue
            ti = type_info(p.type_name)
            if ti is not None and (
                p.type_name in NEON_VEC_TYPES or p.type_name in TUPLE_RETURN_TYPES
            ):
                count = ti.total_lanes
                break
        if count == 0:
            raise ValueError(f"can't determine store buffer size for {sig.name}")

    return TypeInfo(elem_ti.kind, elem_ti.bits, count), f"std::array<{elem}, {count}>"


# ---------------------------------------------------------------------------
# Input generation: deterministic per-bits patterns. Centralized here (and
# not in run_cluster / run_batch) so any caller gets the same canonical
# inputs.
# ---------------------------------------------------------------------------

A_INT: dict[int, list[int]] = {
    8:  [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    16: [100, 200, 300, 400, 500, 600, 700, 800],
    32: [10, 20, 30, 40],
    64: [1000, 2000],
    128: [1_000_000],
}
B_INT_SIGNED: dict[int, list[int]] = {
    8:  [-50, 30, -20, 10, -5, 15, -25, 35, -40, 20, -10, 5, -45, 25, -15, 50],
    16: [-5000, 3000, -2000, 1000, -500, 1500, -2500, 3500],
    32: [-1_000_000_000, 1_500_000_000, -500_000_000, 750_000_000],
    64: [-9_000_000_000, 4_500_000_000],
    128: [12345],
}
B_INT_UNSIGNED: dict[int, list[int]] = {
    8:  [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160],
    16: [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000],
    32: [1_000_000_000, 2_000_000_000, 3_000_000_000, 4_000_000_000],
    64: [9_000_000_000, 18_000_000_000],
    128: [12345],
}
A_FLOAT: list[float] = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0,
                        9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
B_FLOAT: list[float] = [0.5, -0.25, 0.125, -0.0625, 1.5, -2.5, 0.75, -1.25,
                        0.5,  0.25, -0.5,  -0.25, 1.0, -1.0, 2.0, -2.0]


def _values_for(ti: TypeInfo, role: str, count: int) -> list:
    """Pull `count` values from the per-bits pattern, cycling if needed.

    Cycling matters for tuple types: int16x8x4_t wants 32 lanes but our
    16-bit pattern is only 8 long, so we wrap.
    """
    if ti.kind in ("int", "uint", "poly"):
        if role == "a":
            base = A_INT[ti.bits]
        else:
            unsigned = ti.kind in ("uint", "poly")
            base = (B_INT_UNSIGNED if unsigned else B_INT_SIGNED)[ti.bits]
    else:
        base = A_FLOAT if role == "a" else B_FLOAT
    if not base:
        return []
    out: list = []
    while len(out) < count:
        out.extend(base)
    return out[:count]


def build_inputs(sig: "Signature") -> list[list]:
    """Generate deterministic input vectors / scalars for every parameter
    in `sig`. Centralizes the per-type, per-name, per-pointer logic so
    every caller gets the same canonical inputs.
    """
    if is_intel_intrinsic(sig.name):
        return _build_inputs_intel(sig)

    hints = _hints_for(sig.name)
    out: list[list] = []
    # How many elements the pointer-loaded buffer should hold:
    #   vld1   -> total lanes of the (sub-)vector return
    #   vld2/3/4 (regular) -> total lanes including the tuple multiplier
    #   vld<N>_lane_* / vld<N>_dup_* -> just N (one per sub-vector)
    ret_ti = type_info(sig.return_type)
    m = _LANE_DUP_RE.match(sig.name)
    if m:
        load_count = int(m.group(1))
    else:
        load_count = ret_ti.total_lanes if ret_ti else 1

    for i, p in enumerate(sig.params):
        role = "a" if i == 0 else "b"

        # const int: 0 for lane indices and extract intrinsics, 1 otherwise
        # (works for most shift counts and lane indices on multi-lane
        # vectors). vext_*/vextq_* are extracts where the immediate is the
        # element index -- 0 is always valid; non-zero would fail on
        # 1-lane vectors like int64x1_t.
        if p.type_name in ("const int", "const unsigned int"):
            if p.name in hints:
                hv = hints[p.name]
                out.append([int(hv[0] if isinstance(hv, list) else hv)])
                continue
            zero_by_name = p.name in ("lane", "lane1", "lane2", "i", "index", "idx")
            zero_by_op = sig.name.startswith(("vext_", "vextq_"))
            out.append([0 if (zero_by_name or zero_by_op) else 1])
            continue

        # `<elem> const *` -- load. Allocate a buffer of `load_count`
        # elements. Use a strictly *sequential* 1..N integer (or float)
        # pattern -- not the per-bits A/B pattern -- so vld2/vld3/vld4
        # readers can see the deinterleave clearly (otherwise a 32-lane
        # buffer would cycle 1..16,1..16 which looks like duplicated
        # data, not interleaved pairs).
        elem = is_load_pointer(p.type_name)
        if elem is not None:
            elem_ti = type_info(elem)
            if elem_ti is None:
                raise ValueError(f"unknown element type for pointer: {p.type_name}")
            if p.name in hints:
                vals = _apply_hint_list(hints[p.name], load_count)
                if elem_ti.kind in ("float", "bfloat"):
                    vals = [float(v) for v in vals]
                out.append(vals)
            elif elem_ti.kind in ("float", "bfloat"):
                out.append([float(i) for i in range(1, load_count + 1)])
            else:
                out.append(list(range(1, load_count + 1)))
            continue

        # `<elem> *` -- store. The pointer points at the *output buffer*
        # we synthesize in the harness; no input values for it.
        if is_store_pointer(p.type_name) is not None:
            out.append([])
            continue

        ti = type_info(p.type_name)
        if ti is None:
            raise ValueError(f"unsupported type: {p.type_name}")
        if p.name in hints:
            vals = _apply_hint_list(hints[p.name], ti.total_lanes)
            if ti.kind in ("float", "bfloat"):
                vals = [float(v) for v in vals]
            out.append(vals)
        else:
            out.append(_values_for(ti, role, ti.total_lanes))

    return out


_INTEL_IMM_TYPES = {
    "const int", "const unsigned int",
    "int", "unsigned int", "unsigned",
    # Intel's guide also writes immediates as `__int32 imm8` etc.
    "__int32", "__int8",
}


def _build_inputs_intel(sig: "Signature") -> list[list]:
    """Intel variant of build_inputs. `__m128i` etc. need the intrinsic
    name to know lane shape; pointers and immediates have the same
    handling as ARM."""
    hints = _hints_for(sig.name)
    out: list[list] = []
    for i, p in enumerate(sig.params):
        role = "a" if i == 0 else "b"
        # AVX-512 mask params (__mmask8/16/32/64) -- synthesize an
        # alternating bit pattern so masked variants visibly differ from
        # their unmasked counterparts in the worked example.
        if p.type_name in _INTEL_MASK_WIDTHS:
            bits = _INTEL_MASK_WIDTHS[p.type_name]
            if p.name in hints:
                hv = hints[p.name]
                val = int(hv[0] if isinstance(hv, list) else hv)
                out.append([(val >> j) & 1 for j in range(bits)])
            else:
                out.append(_mask_synth_bits(role, bits))
            continue
        # In the Intel intrinsic guide, ANY non-vector int param is an
        # immediate (mask, lane index, rounding mode, ...). 0 is a safe
        # default for all of them.
        if p.type_name in _INTEL_IMM_TYPES:
            if p.name in hints:
                hv = hints[p.name]
                out.append([int(hv[0] if isinstance(hv, list) else hv)])
            else:
                out.append([0])
            continue
        # Skip pointers / non-vector tricky params for v0.
        if "*" in p.type_name:
            raise ValueError(f"intel pointer param not supported yet: {p.type_name}")
        if p.type_name in INTEL_VEC_BYTES:
            ti = intel_lane_info_for(sig.name, p.type_name, context="input")
            if p.name in hints:
                vals = _apply_hint_list(hints[p.name], ti.count)
                if ti.kind in ("float", "bfloat"):
                    vals = [float(v) for v in vals]
                out.append(vals)
            else:
                out.append(_values_for(ti, role, ti.count))
            continue
        ti = type_info(p.type_name)
        if ti is None:
            raise ValueError(f"unsupported intel param type: {p.type_name}")
        if p.name in hints:
            vals = _apply_hint_list(hints[p.name], ti.total_lanes)
            if ti.kind in ("float", "bfloat"):
                vals = [float(v) for v in vals]
            out.append(vals)
        else:
            out.append(_values_for(ti, role, ti.total_lanes))
    return out


@dataclass
class Param:
    type_name: str  # "int8x16_t" or "int64_t"
    name: str       # "a", "b", ...


@dataclass
class Signature:
    return_type: str
    name: str
    params: list[Param]


# Signature parser: "int8x16_t vaddq_s8(int8x16_t a, int8x16_t b)".
# Some ARM ACLE definitions wrap parameters across multiple lines, so we
# normalize whitespace first.
_SIG_RE = re.compile(r"^\s*([\w\s\*]+?)\s+(\w+)\s*\((.*?)\)\s*$", re.DOTALL)


def parse_signature(definition: str) -> Signature:
    flat = re.sub(r"\s+", " ", definition).strip()
    m = _SIG_RE.match(flat)
    if not m:
        raise ValueError(f"Cannot parse signature: {flat!r}")
    ret, name, params_blob = m.group(1).strip(), m.group(2), m.group(3)
    params: list[Param] = []
    for raw in params_blob.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.rsplit(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"Cannot parse param {raw!r}")
        params.append(Param(type_name=parts[0].strip(), name=parts[1].strip()))
    return Signature(return_type=ret, name=name, params=params)


def lookup_record(intrinsic: str) -> dict:
    with DB_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            if r["intrinsic"] == intrinsic:
                return r
    raise KeyError(f"intrinsic {intrinsic!r} not found in DB")


def render_lane_literal(value, ti: TypeInfo) -> str:
    """C literal for a single lane value, kind- and width-aware."""
    if ti.kind in ("int", "uint", "poly"):
        return str(value)
    if ti.kind == "float":
        # bf16 is handled separately. For float / double / __fp16, a plain
        # decimal literal that is exactly representable is fine. We tag
        # 32-bit with `f` and 16-bit with the (__fp16) cast for clarity;
        # 64-bit gets no suffix (default `double`).
        if ti.bits == 32:
            return f"{float(value)!r}f"
        if ti.bits == 16:
            return f"((__fp16){float(value)!r}f)"
        return repr(float(value))
    if ti.kind == "bfloat":
        # __bf16 cannot be initialized from a literal directly; cast a
        # 32-bit float (which clang then narrows at compile time).
        return f"((__bf16){float(value)!r}f)"
    raise ValueError(f"unhandled kind {ti.kind!r}")


def render_init_list(values: list, ti: TypeInfo) -> str:
    """Render `values` as a C init list. For tuple types (tuple_size > 1)
    the list is triply-nested:
        struct { Vec val[N]; }   ->   { { {sub_0}, {sub_1}, ... } }
    Three levels: outer = struct, middle = val[N] array, inner = each
    sub-vector. C++ doesn't do brace-elision through both the struct and
    the array, so the explicit middle layer is required.
    """
    if ti.tuple_size > 1:
        if len(values) != ti.total_lanes:
            raise ValueError(
                f"tuple init expected {ti.total_lanes} values, got {len(values)}"
            )
        subs = []
        for k in range(ti.tuple_size):
            chunk = values[k * ti.count:(k + 1) * ti.count]
            subs.append(
                "{ " + ", ".join(render_lane_literal(v, ti) for v in chunk) + " }"
            )
        return "{ { " + ", ".join(subs) + " } }"
    return "{ " + ", ".join(render_lane_literal(v, ti) for v in values) + " }"


# aarch64 march that enables every NEON-relevant feature we care about
# (i8mm/bf16/fp16/dotprod for matmul + dot, sm4 for SM3/SM4, sha3 for
# vsha512* / vrax1*, v8.5a for vrnd32*/vrnd64*, crypto for AES). This
# mirrors the godbolt CE config so local fold/execute classifications
# match what the user sees in the live iframe. Apple Clang 17 had most
# of these on by default on the M-series host, but trunk LLVM is stricter.
_AARCH64_MARCH = (
    "-march=armv8.6-a+fp16+bf16+i8mm+dotprod+crypto+sha3+sm4"
)


def compile_flags_for(sig: Signature, family_list: list[str] | None = None) -> list[str]:
    """Extra clang flags needed to compile an intrinsic of this signature.

    For ARM, supply an aarch64 -march that activates every feature any
    NEON intrinsic can require (FRINT/MMLA/SM3/SHA3/...). For Intel,
    returns -target + -m<feature> based on the family list from the DB
    record (e.g. ['SSE4.1'] -> ['-target', ..., '-msse4.1']).
    """
    if not is_intel_intrinsic(sig.name):
        return [_AARCH64_MARCH]
    return intel_compile_flags(family_list or [])


def emit_source(sig: Signature, inputs: list[list]) -> str:
    """Emit a tiny C++ source whose RESULT global is the folded intrinsic call."""
    if is_intel_intrinsic(sig.name):
        return _emit_source_intel(sig, inputs)

    # Pick the right includes for the families we support today (NEON +
    # the small ARM scalar bridges + ACLE crc32/aes/sha helpers).
    includes = (
        "#include <arm_neon.h>\n"
        "#include <arm_acle.h>\n"
        "#include <arm_fp16.h>\n"
        "#include <arm_bf16.h>\n"
        "#include <array>\n"
    )

    if len(inputs) != len(sig.params):
        raise ValueError(
            f"intrinsic {sig.name} expects {len(sig.params)} inputs, "
            f"got {len(inputs)}"
        )

    # Spot the store pointer (if any). Stores are void-returning; the
    # output buffer the store writes to becomes RESULT, wrapped in
    # std::array so it has a well-defined C++ type.
    store_idx = next(
        (i for i, p in enumerate(sig.params)
         if is_store_pointer(p.type_name) is not None),
        None,
    )
    is_store = store_idx is not None
    eff_ti, ret_decl = effective_return(sig)

    decls: list[str] = []
    arg_names: list[str] = []
    for i, (p, vals) in enumerate(zip(sig.params, inputs)):
        # `const int` immediates: inline as a literal at the call site.
        if p.type_name in ("const int", "const unsigned int"):
            if len(vals) != 1:
                raise ValueError(f"param {p.name}: expected 1 immediate value")
            arg_names.append(str(vals[0]))
            continue

        # Store pointer: pass __buf.data() (declared by the harness below).
        if i == store_idx:
            arg_names.append("__buf.data()")
            continue

        # `<elem> const *` (load): emit a static const buffer of the right
        # length and pass its address.
        elem = is_load_pointer(p.type_name)
        if elem is not None:
            elem_ti = type_info(elem)
            if elem_ti is None:
                raise ValueError(f"unknown element for pointer param: {p.type_name}")
            buf = f"{p.name}_buf"
            decls.append(
                f"static constexpr {elem} {buf}[{len(vals)}] = "
                f"{render_init_list(vals, elem_ti)};"
            )
            arg_names.append(buf)
            continue

        ti = type_info(p.type_name)
        if ti is None:
            raise ValueError(f"unsupported param type: {p.type_name}")
        is_vector_or_tuple = (
            p.type_name in NEON_VEC_TYPES or p.type_name in TUPLE_RETURN_TYPES
        )
        if is_vector_or_tuple:
            expected = ti.total_lanes
            if len(vals) != expected:
                raise ValueError(
                    f"param {p.name}: expected {expected} lanes "
                    f"({p.type_name}), got {len(vals)}"
                )
            decls.append(
                f"const {p.type_name} {p.name} = {render_init_list(vals, ti)};"
            )
        else:
            if len(vals) != 1:
                raise ValueError(
                    f"param {p.name}: expected 1 scalar value, got {len(vals)}"
                )
            decls.append(
                f"const {p.type_name} {p.name} = {render_lane_literal(vals[0], ti)};"
            )
        arg_names.append(p.name)

    call_args = ", ".join(arg_names)
    # Two harness shapes:
    #   (a) regular: lambda returns the intrinsic's value directly.
    #   (b) store:   lambda allocates a std::array buffer, calls the void
    #               intrinsic on its .data(), and returns the buffer.
    # In both cases we wrap in an immediately-invoked lambda so GCC
    # statement-expression macros (vcreate_*, vshl_n_*, ...) are legal.
    if is_store:
        body = "\n    ".join(
            [f"{ret_decl} __buf{{}};"] + decls
            + [f"{sig.name}({call_args});", "return __buf;"]
        )
    else:
        body = "\n    ".join(decls + [f"return {sig.name}({call_args});"])

    return f"""{includes}#include <cstdio>
#include <cstddef>

extern "C" const {ret_decl} RESULT = []() -> {ret_decl} {{
    {body}
}}();

int main() {{
    const unsigned char* p = reinterpret_cast<const unsigned char*>(&RESULT);
    for (size_t i = 0; i < sizeof(RESULT); i++) std::printf("%02x", p[i]);
    return 0;
}}
"""


def _emit_source_intel(sig: Signature, inputs: list[list]) -> str:
    """Intel emit_source: includes <immintrin.h>, builds inputs via
    `_mm{,256,512}_setr_<suffix>(...)` (no compound literals -- the
    underlying vec_long layout is finicky and `_setr_*` is universally
    supported), and lands RESULT in .rodata via constant-folding.
    """
    if len(inputs) != len(sig.params):
        raise ValueError(
            f"intrinsic {sig.name} expects {len(sig.params)} inputs, "
            f"got {len(inputs)}"
        )
    eff_ti, ret_decl = effective_return(sig)

    decls: list[str] = []
    arg_names: list[str] = []
    for i, (p, vals) in enumerate(zip(sig.params, inputs)):
        if p.type_name in _INTEL_MASK_WIDTHS:
            bits = _INTEL_MASK_WIDTHS[p.type_name]
            if len(vals) != bits:
                raise ValueError(
                    f"param {p.name}: expected {bits} mask bits, got {len(vals)}"
                )
            val = sum((int(b) & 1) << j for j, b in enumerate(vals))
            decls.append(f"const {p.type_name} {p.name} = {hex(val)};")
            arg_names.append(p.name)
            continue
        if p.type_name in _INTEL_IMM_TYPES:
            arg_names.append(str(vals[0]))
            continue
        if p.type_name in INTEL_VEC_BYTES:
            ti = intel_lane_info_for(sig.name, p.type_name, context="input")
            if len(vals) != ti.count:
                raise ValueError(
                    f"param {p.name}: expected {ti.count} lanes "
                    f"({p.type_name} via {sig.name} suffix), got {len(vals)}"
                )
            decls.append(f"const {p.type_name} {p.name} = {intel_setr_call(vals, ti)};")
            arg_names.append(p.name)
            continue
        # Scalars (int, float, etc.) - reuse the regular path.
        ti = type_info(p.type_name)
        if ti is None:
            raise ValueError(f"unsupported intel param type: {p.type_name}")
        if len(vals) != 1:
            raise ValueError(
                f"param {p.name}: expected 1 scalar value, got {len(vals)}"
            )
        decls.append(f"const {p.type_name} {p.name} = {render_lane_literal(vals[0], ti)};")
        arg_names.append(p.name)

    call_args = ", ".join(arg_names)
    body = "\n    ".join(decls + [f"return {sig.name}({call_args});"])

    # When Rosetta is available we target x86_64-apple-macos and link
    # against the Apple SDK -- so cstdio + main() work and we can fall
    # back to the execute path for intrinsics clang's IR folder doesn't
    # model. Otherwise we're freestanding (fold-only).
    has_main = _rosetta_available()
    main_block = (
        "#include <cstdio>\n#include <cstddef>\n"
        if has_main else ""
    ) + ""
    main_fn = """
int main() {
    const unsigned char* p = reinterpret_cast<const unsigned char*>(&RESULT);
    for (size_t i = 0; i < sizeof(RESULT); i++) std::printf("%02x", p[i]);
    return 0;
}
""" if has_main else ""

    return f"""#include <immintrin.h>
{main_block}
// Intel's intrinsic guide uses MSVC-style fixed-width typedefs.
// clang in -target x86_64-linux-gnu doesn't ship them; supply our own.
#if !defined(_MSC_VER)
typedef long long  __int64;
typedef int        __int32;
typedef short      __int16;
typedef signed char __int8;
#endif

extern "C" const {ret_decl} RESULT = []() -> {ret_decl} {{
    {body}
}}();
{main_fn}"""


def compile_and_extract(
    source: str,
    *,
    target_triple: str | None = None,
    extra_flags: list[str] | None = None,
    expected_bytes: int | None = None,
) -> tuple[bytes, str]:
    """Compile source and return (RESULT bytes, method).

    Two-phase: first try the constant-folding path (compile -c, find the
    `RESULT` symbol via the symbol table, slice the right bytes out of
    its section). method == "fold" if that succeeds. If RESULT lands in
    __common / .bss (clang couldn't fold the call), fall back to linking
    the harness's main() and running it; main() prints the bytes via
    printf, and method == "execute".

    "fold" intrinsics are CE-iframe-friendly because their semantics are
    visible in static asm without execution. "execute" intrinsics still
    work in CE iframes via CE's executor mode but need an actual run.
    """
    if expected_bytes is None:
        raise ValueError("compile_and_extract requires expected_bytes")
    _check_clang_version()
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "harness.cc"
        obj = td_path / "harness.o"
        exe = td_path / "harness"
        src.write_text(source)

        # Phase 1: compile to .o.
        cmd = [CLANG, "-O2", "-c", str(src), "-o", str(obj)]
        if target_triple:
            cmd[1:1] = ["-target", target_triple]
        if extra_flags:
            cmd[1:1] = list(extra_flags)
        # Homebrew clang has no built-in sysroot: point it at Xcode's SDK.
        sdk = _macos_sdk_path()
        if sdk:
            cmd[1:1] = ["-isysroot", sdk]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "clang failed:\n"
                f"  cmd: {' '.join(cmd)}\n"
                f"  stderr:\n{proc.stderr}\n"
                f"  source:\n{source}"
            )

        # Locate RESULT in the symbol table and slice its bytes if it
        # landed in a folded section (__const / .rodata).
        sym = _find_result_symbol(obj)
        if sym is not None:
            section = sym.section
            if section in _FOLDED_SECTIONS:
                proc = subprocess.run(
                    [LLVM_OBJDUMP, "-s", "-j", section, str(obj)],
                    capture_output=True, text=True,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    sliced = _slice_section_bytes(
                        proc.stdout, sym.vma, expected_bytes
                    )
                    if len(sliced) == expected_bytes:
                        return sliced, "fold"

        # Phase 2: link + run. The harness's static initializer computes
        # RESULT at module-load time, then main() prints it. Used for
        # intrinsics clang's IR constant folder doesn't model (saturating,
        # halving, table-lookup, etc.) but where the backend can lower the
        # call cleanly at -O2.
        target = target_triple
        if not target and extra_flags:
            for i, f in enumerate(extra_flags):
                if f == "-target" and i + 1 < len(extra_flags):
                    target = extra_flags[i + 1]
                    break
        # Native or Apple Mach-O on Apple Silicon (Rosetta) -- can run.
        # Linux ELF cross-compile -- can't run, no emulator.
        can_run = (
            not target
            or target.startswith("x86_64-apple")
            and _rosetta_available()
        )
        if not can_run:
            raise RuntimeError(
                "Constant-fold path failed and execute fallback is "
                "disabled when cross-compiling (would need an emulator)."
            )
        link_cmd = [CLANG, "-O2", str(src), "-o", str(exe)]
        if target_triple:
            link_cmd[1:1] = ["-target", target_triple]
        if extra_flags:
            link_cmd[1:1] = list(extra_flags)
        sdk = _macos_sdk_path()
        if sdk:
            link_cmd[1:1] = ["-isysroot", sdk]
        proc = subprocess.run(link_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "link failed:\n"
                f"  cmd: {' '.join(link_cmd)}\n"
                f"  stderr:\n{proc.stderr}\n"
                f"  source:\n{source}"
            )
        proc = subprocess.run([str(exe)], capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            raise RuntimeError(
                f"run failed (exit {proc.returncode}):\n"
                f"  stderr: {proc.stderr}\n  stdout: {proc.stdout}"
            )
        out = proc.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]+", out):
            raise RuntimeError(
                f"unexpected stdout from harness: {out[:120]!r}"
            )
        return bytes.fromhex(out), "execute"


_OBJDUMP_DATA_RE = re.compile(
    r"^\s*([0-9a-f]+)\s+([0-9a-f ]+?)(?:\s{2,}.*)?$",
    re.IGNORECASE,
)

# Sections where clang lands constant-folded results we can read without
# running the program. Mach-O on Darwin -> __const; ELF (cross-compile or
# Linux native) -> .rodata.
_FOLDED_SECTIONS = ("__const", ".rodata")


@dataclass
class _Symbol:
    name: str
    vma: int
    section: str


_SYMTAB_RE = re.compile(
    # ELF rows include a size column between section and name; Mach-O
    # doesn't. Match either with an optional `<hex>` token before the name.
    r"^([0-9a-f]+)\s+\S+\s+\S+\s+([\w,.]+)(?:\s+[0-9a-f]+)?\s+(\S+)\s*$",
    re.IGNORECASE,
)


def _find_result_symbol(obj_path: Path) -> _Symbol | None:
    """Run llvm-objdump --syms and return the RESULT symbol, or None."""
    proc = subprocess.run(
        [LLVM_OBJDUMP, "--syms", str(obj_path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        m = _SYMTAB_RE.match(line)
        if not m:
            continue
        vma = int(m.group(1), 16)
        section = m.group(2)
        name = m.group(3)
        # Mach-O leading-underscore + plain ELF naming.
        if name in ("_RESULT", "RESULT"):
            # `section` looks like "__TEXT,__const" or ".rodata"
            short = section.split(",")[-1] if "," in section else section
            return _Symbol(name=name, vma=vma, section=short)
    return None


def _slice_section_bytes(dump: str, start_vma: int, length: int) -> bytes:
    """Pull `length` bytes out of `llvm-objdump -s -j <section>` output
    starting at virtual address `start_vma`. Rows look like:
        001c 01020304 05060708 090a0b0c 0d0e0f10  ................
    """
    out = bytearray()
    end_vma = start_vma + length
    in_section = False
    for line in dump.splitlines():
        if line.startswith("Contents of section"):
            in_section = True
            continue
        if not in_section:
            continue
        m = _OBJDUMP_DATA_RE.match(line)
        if not m:
            continue
        row_addr = int(m.group(1), 16)
        try:
            row_bytes = bytes.fromhex(m.group(2).replace(" ", ""))
        except ValueError:
            continue
        row_end = row_addr + len(row_bytes)
        s = max(start_vma, row_addr)
        e = min(end_vma, row_end)
        if e > s:
            out.extend(row_bytes[s - row_addr:e - row_addr])
        if row_end >= end_vma:
            break
    return bytes(out)


def _bf16_bytes_to_float(raw: bytes) -> float:
    """Decode 2 bytes (little-endian bfloat16) into a Python float."""
    # bfloat16 is the upper 16 bits of an IEEE-754 binary32. Pad with two
    # zero bytes on the bottom and unpack as float32.
    return struct.unpack("<f", b"\x00\x00" + raw)[0]


def decode_lanes(type_or_info, raw: bytes):
    """Reverse of an init list: turn raw little-endian bytes into lane values.

    Accepts either a type name (`"int8x16_t"`) or a `TypeInfo` directly
    (for synthesized buffer types from store harnesses). Returns ints for
    int/uint/poly lanes and floats for float/bfloat lanes.
    """
    if isinstance(type_or_info, TypeInfo):
        ti = type_or_info
    else:
        ti = type_info(type_or_info)
        if ti is None:
            raise ValueError(f"unsupported return type: {type_or_info}")
    total = ti.total_bytes
    if len(raw) < total:
        raise ValueError(f"got {len(raw)} bytes for {type_name}, need {total}")
    raw = raw[:total]
    # AVX-512 masks: pack 1 bit per lane in a 1/2/4/8-byte LE integer,
    # so the per-lane loop below (which assumes >=1 byte per lane) doesn't
    # apply -- unpack bits directly.
    if ti.kind == "mask":
        val = int.from_bytes(raw, byteorder="little", signed=False)
        return [(val >> i) & 1 for i in range(ti.count)]
    # For tuple types (int8x16x2_t etc.) we just decode `count * tuple_size`
    # lanes back-to-back in memory order. Callers can split into sub-vectors
    # afterwards if they want to display them stacked.
    n_lanes = ti.total_lanes
    bytes_per_lane = total // n_lanes

    out: list = []
    for i in range(n_lanes):
        chunk = raw[i * bytes_per_lane:(i + 1) * bytes_per_lane]
        if ti.kind in ("int", "uint", "poly"):
            signed = (ti.kind == "int")
            out.append(int.from_bytes(chunk, byteorder="little", signed=signed))
        elif ti.kind == "float":
            if ti.bits == 16:
                out.append(struct.unpack("<e", chunk)[0])
            elif ti.bits == 32:
                out.append(struct.unpack("<f", chunk)[0])
            elif ti.bits == 64:
                out.append(struct.unpack("<d", chunk)[0])
            else:
                raise ValueError(f"unsupported float width {ti.bits}")
        elif ti.kind == "bfloat":
            out.append(_bf16_bytes_to_float(chunk))
        else:
            raise ValueError(f"unhandled kind {ti.kind!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--intrinsic", required=True,
                    help="canonical intrinsic name (e.g. vaddq_s8)")
    ap.add_argument(
        "--input", "-i", action="append", required=True,
        help="comma-separated lane values, repeat per parameter "
             "(e.g. -i 1,2,3,...,16 -i 100,-50,...)",
    )
    ap.add_argument("--print-source", action="store_true",
                    help="print the generated C++ source and exit")
    args = ap.parse_args()

    record = lookup_record(args.intrinsic)
    sig = parse_signature(record["definition"])

    def _parse_val(s: str):
        try:
            return int(s, 0)  # auto-detect base (0x..., 0o..., 0b...)
        except ValueError:
            return float(s)
    inputs = [[_parse_val(v.strip()) for v in s.split(",")] for s in args.input]
    source = emit_source(sig, inputs)

    if args.print_source:
        print(source)
        return 0

    ret_ti, _ = effective_return(sig)
    result_bytes, method = compile_and_extract(
        source, expected_bytes=ret_ti.total_bytes
    )
    print(f"intrinsic:  {args.intrinsic}")
    print(f"signature:  {record['definition']}")
    for p, vals in zip(sig.params, inputs):
        print(f"  {p.name}: {vals}")
    print(f"output:     {result_bytes.hex()}  (via {method})")
    print(f"            {' '.join(f'{b:02x}' for b in result_bytes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
