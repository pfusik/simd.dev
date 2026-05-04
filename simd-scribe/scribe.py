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

# Map ACLE vector type name -> (lane C type, lane count, total bytes)
NEON_VEC_TYPES: dict[str, tuple[str, int, int]] = {
    "int8x8_t":    ("int8_t",   8,  8),
    "int8x16_t":   ("int8_t",  16, 16),
    "int16x4_t":   ("int16_t",  4,  8),
    "int16x8_t":   ("int16_t",  8, 16),
    "int32x2_t":   ("int32_t",  2,  8),
    "int32x4_t":   ("int32_t",  4, 16),
    "int64x1_t":   ("int64_t",  1,  8),
    "int64x2_t":   ("int64_t",  2, 16),
    "uint8x8_t":   ("uint8_t",  8,  8),
    "uint8x16_t":  ("uint8_t", 16, 16),
    "uint16x4_t":  ("uint16_t", 4,  8),
    "uint16x8_t":  ("uint16_t", 8, 16),
    "uint32x2_t":  ("uint32_t", 2,  8),
    "uint32x4_t":  ("uint32_t", 4, 16),
    "uint64x1_t":  ("uint64_t", 1,  8),
    "uint64x2_t":  ("uint64_t", 2, 16),
}

# Scalar return / param types (e.g. vaddd_s64 takes/returns int64_t)
SCALAR_TYPES = {"int8_t", "int16_t", "int32_t", "int64_t",
                "uint8_t", "uint16_t", "uint32_t", "uint64_t"}


@dataclass
class Param:
    type_name: str  # "int8x16_t" or "int64_t"
    name: str       # "a", "b", ...


@dataclass
class Signature:
    return_type: str
    name: str
    params: list[Param]


# Signature parser: "int8x16_t vaddq_s8(int8x16_t a, int8x16_t b)"
_SIG_RE = re.compile(r"^\s*([\w\s\*]+?)\s+(\w+)\s*\((.*?)\)\s*$")


def parse_signature(definition: str) -> Signature:
    m = _SIG_RE.match(definition.strip())
    if not m:
        raise ValueError(f"Cannot parse signature: {definition!r}")
    ret, name, params_blob = m.group(1).strip(), m.group(2), m.group(3)
    params: list[Param] = []
    for raw in params_blob.split(","):
        raw = raw.strip()
        if not raw:
            continue
        # Split last word as the param name
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


def render_init_list(values: list[int]) -> str:
    return "{ " + ", ".join(str(v) for v in values) + " }"


def emit_source(sig: Signature, inputs: list[list[int]]) -> str:
    """Emit a tiny C++ source whose RESULT global is the folded intrinsic call."""
    # Pick the right include for now (NEON only).
    includes = "#include <arm_neon.h>\n"

    if len(inputs) != len(sig.params):
        raise ValueError(
            f"intrinsic {sig.name} expects {len(sig.params)} inputs, "
            f"got {len(inputs)}"
        )

    # Declare each input as a const vector (or scalar for d_-suffixed forms).
    decls = []
    arg_names = []
    for p, vals in zip(sig.params, inputs):
        if p.type_name in NEON_VEC_TYPES:
            lane, count, _ = NEON_VEC_TYPES[p.type_name]
            if len(vals) != count:
                raise ValueError(
                    f"param {p.name}: expected {count} lanes "
                    f"({p.type_name}), got {len(vals)}"
                )
            decls.append(
                f"const {p.type_name} {p.name} = {render_init_list(vals)};"
            )
        elif p.type_name in SCALAR_TYPES:
            if len(vals) != 1:
                raise ValueError(
                    f"param {p.name}: expected 1 scalar value, got {len(vals)}"
                )
            decls.append(f"const {p.type_name} {p.name} = {vals[0]};")
        else:
            raise ValueError(f"unsupported param type: {p.type_name}")
        arg_names.append(p.name)

    call = f"{sig.name}({', '.join(arg_names)})"
    result_decl = f'extern "C" const {sig.return_type} RESULT = {call};'

    return (
        includes
        + "\n"
        + "\n".join(decls)
        + "\n\n"
        + result_decl
        + "\n"
    )


def compile_and_extract(source: str, *, target_triple: str | None = None) -> bytes:
    """Compile source -> object, return RESULT bytes from __const section."""
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src = td_path / "harness.cc"
        obj = td_path / "harness.o"
        src.write_text(source)

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

        # Mach-O on Darwin -> __const section. ELF (cross-compile) -> .rodata.
        # llvm-objdump's -j flag spells it the same regardless of OS.
        for section in ("__const", ".rodata"):
            proc = subprocess.run(
                [LLVM_OBJDUMP, "-s", "-j", section, str(obj)],
                capture_output=True, text=True,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                bytes_out = parse_objdump_bytes(proc.stdout)
                if bytes_out:
                    return bytes_out
        raise RuntimeError(
            "Could not locate RESULT bytes in __const or .rodata; "
            "the call may not have folded. Object dump:\n"
            f"{subprocess.run([LLVM_OBJDUMP, '-h', str(obj)], capture_output=True, text=True).stdout}"
        )


_OBJDUMP_DATA_RE = re.compile(r"^\s*[0-9a-f]+\s+([0-9a-f ]+?)(?:\s{2,}.*)?$",
                              re.IGNORECASE)


def parse_objdump_bytes(dump: str) -> bytes:
    """Parse `llvm-objdump -s` output into raw bytes."""
    out = bytearray()
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
        hex_blob = m.group(1).replace(" ", "")
        try:
            out.extend(bytes.fromhex(hex_blob))
        except ValueError:
            continue
    return bytes(out)


def decode_lanes(type_name: str, raw: bytes) -> list[int]:
    """Reverse of an init list: turn raw little-endian bytes into lane values."""
    if type_name in NEON_VEC_TYPES:
        lane, count, total = NEON_VEC_TYPES[type_name]
    elif type_name in SCALAR_TYPES:
        lane = type_name
        count = 1
        total = int(lane.replace("uint", "").replace("int", "").replace("_t", "")) // 8
    else:
        raise ValueError(f"unsupported return type: {type_name}")

    if len(raw) < total:
        raise ValueError(f"got {len(raw)} bytes for {type_name}, need {total}")
    raw = raw[:total]

    bytes_per_lane = total // count
    signed = lane.startswith("int")
    return [
        int.from_bytes(raw[i * bytes_per_lane:(i + 1) * bytes_per_lane],
                       byteorder="little", signed=signed)
        for i in range(count)
    ]


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

    inputs = [[int(v) for v in s.split(",")] for s in args.input]
    source = emit_source(sig, inputs)

    if args.print_source:
        print(source)
        return 0

    result_bytes = compile_and_extract(source)
    print(f"intrinsic:  {args.intrinsic}")
    print(f"signature:  {record['definition']}")
    for p, vals in zip(sig.params, inputs):
        print(f"  {p.name}: {vals}")
    print(f"output:     {result_bytes.hex()}")
    print(f"            {' '.join(f'{b:02x}' for b in result_bytes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
