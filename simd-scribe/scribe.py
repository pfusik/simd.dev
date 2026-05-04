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
import json
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "intrinsics.jsonl"

LLVM_OBJDUMP = (
    "/Applications/Xcode.app/Contents/Developer/Toolchains/"
    "XcodeDefault.xctoolchain/usr/bin/llvm-objdump"
)


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
            zero_by_name = p.name in ("lane", "lane1", "lane2", "i", "index", "idx")
            zero_by_op = sig.name.startswith(("vext_", "vextq_"))
            out.append([0 if (zero_by_name or zero_by_op) else 1])
            continue

        # `<elem> const *` -- load. Allocate a buffer of `load_count`
        # elements, sourced from the same per-bits pattern we use for
        # vector params.
        elem = is_load_pointer(p.type_name)
        if elem is not None:
            elem_ti = type_info(elem)
            if elem_ti is None:
                raise ValueError(f"unknown element type for pointer: {p.type_name}")
            out.append(_values_for(elem_ti, role, load_count))
            continue

        # `<elem> *` -- store. The pointer points at the *output buffer*
        # we synthesize in the harness; no input values for it.
        if is_store_pointer(p.type_name) is not None:
            out.append([])
            continue

        ti = type_info(p.type_name)
        if ti is None:
            raise ValueError(f"unsupported type: {p.type_name}")
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


def emit_source(sig: Signature, inputs: list[list]) -> str:
    """Emit a tiny C++ source whose RESULT global is the folded intrinsic call."""
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


def compile_and_extract(
    source: str, *, target_triple: str | None = None, expected_bytes: int | None = None
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
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "harness.cc"
        obj = td_path / "harness.o"
        exe = td_path / "harness"
        src.write_text(source)

        # Phase 1: compile to .o.
        cmd = ["clang++", "-O2", "-c", str(src), "-o", str(obj)]
        if target_triple:
            cmd[1:1] = ["-target", target_triple]
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
        if target_triple:
            raise RuntimeError(
                "Constant-fold path failed and execute fallback is "
                "disabled when cross-compiling (would need an emulator)."
            )
        proc = subprocess.run(
            ["clang++", "-O2", str(src), "-o", str(exe)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "link failed:\n"
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
    # vma flags  section          name
    r"^([0-9a-f]+)\s+\S+\s+\S+\s+([\w,.]+)\s+(\S+)\s*$",
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
