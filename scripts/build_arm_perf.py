#!/usr/bin/env python3
"""Build a per-intrinsic ARM perf table by combining clang compilation
of a tiny wrapper (to extract the dominant asm instruction) with
llvm-mca --instruction-info across a panel of microarchs.

The output is `simd-tooltip/dist/arm-perf.json`, fetched lazily by the
front-end only when an ARM intrinsic page is opened. Format:

    {
      "microarchs": ["neoverse-n1", "neoverse-v1", ...],
      "forms": {
        "add v0.4s, v1.4s, v0.4s": [
          [1, 2, 0.5],   // [uops, latency, rThroughput] for neoverse-n1
          [1, 2, 0.25],  //                                 neoverse-v1
          ...
        ],
        ...
      },
      "intrinsics": {
        "vaddq_s32":  "add v0.4s, v1.4s, v0.4s",
        "vaddq_u32":  "add v0.4s, v1.4s, v0.4s",
        ...
      }
    }
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "simd-scribe"))
from scribe import (  # noqa: E402
    CLANG,
    LLVM_OBJDUMP,
    _macos_sdk_path,
    compile_flags_for,
    parse_signature,
)

DB_PATH = ROOT / "data" / "intrinsics.jsonl"
OUT_PATH = ROOT / "simd-tooltip" / "dist" / "arm-perf.json"

LLVM_BIN = Path(CLANG).parent
LLVM_MCA = LLVM_BIN / "llvm-mca"

# Microarchs to model. LLVM ships scheduling models for these; ones
# without a model fall back to a default that's not useful, so keep
# the list tight.
MICROARCHS = [
    "neoverse-n1",
    "neoverse-v1",
    "neoverse-v2",
    "neoverse-n2",
    "cortex-x4",
    "apple-m1",
]

# C++ wrapper we compile per-intrinsic to expose a single function whose
# body is the intrinsic call. -O2 prunes everything else; with the right
# args clang emits at most a few instructions, of which the SIMD one is
# what we care about.
WRAP_HEADERS = (
    "#include <arm_neon.h>\n"
    "#include <arm_acle.h>\n"
    "#include <arm_fp16.h>\n"
    "#include <arm_bf16.h>\n"
)


def _wrap_source(sig, imm_value: int = 1) -> str | None:
    """Build a wrapper function calling `sig.name(args)`. Returns None
    if the signature has a `const int` immediate we can't synthesize.

    `imm_value` bakes a literal at every immediate slot. Shift-by-N
    intrinsics need 1..N (0 is rejected); lane intrinsics need 0..N-1
    (>=lane count rejected). We default to 1 which is valid for the
    bulk; caller retries with 0 on compile failure."""
    args = []
    call = []
    for p in sig.params:
        t = p.type_name
        if t in ("const int", "const unsigned int"):
            call.append(str(imm_value))
            continue
        args.append(f"{t} {p.name}")
        call.append(p.name)
    ret = "void" if sig.return_type == "void" else sig.return_type
    body = f"{sig.name}({', '.join(call)})"
    if sig.return_type == "void":
        body = body + ";"
    else:
        body = f"return {body};"
    return (
        WRAP_HEADERS
        + f'extern "C" {ret} wrapped({", ".join(args) if args else "void"}) '
        + f"{{ {body} }}\n"
    )


# Mnemonics that are always ABI scaffolding regardless of operands.
_ALWAYS_BORING = {
    "ret", "br", "blr", "bl", "b", "nop",
    "stp", "ldp", "stur", "ldur",
    "adrp", "adr", "movk", "movz", "movn",
}
# Mnemonics that could be scaffolding OR a vector op: distinguish by
# operand shape. `add x29, sp, #0x20` is scaffolding; `add v0.4s, ...`
# is the intrinsic's body.
_GP_REG_RE = re.compile(r"\b(?:[wx](?:\d+|zr)|sp)\b")
_VEC_OPERAND_RE = re.compile(r"\bv\d+\.\d+[bhsdq]\b|\bs\d+\b|\bd\d+\b|\bq\d+\b|\bh\d+\b|\b[vqdsh]\d+\b")


def _is_interesting(line: str) -> bool:
    """Reject scaffolding lines so we land on the SIMD op."""
    parts = line.split(None, 1)
    if not parts:
        return False
    mnem = parts[0].lower()
    if mnem in _ALWAYS_BORING:
        return False
    operands = parts[1] if len(parts) > 1 else ""
    # If the operands are vector/FP registers (v0.4s, q1, s2, ...),
    # the line is interesting regardless of mnemonic. If they're
    # purely GP/SP registers, it's frame setup.
    has_vec = bool(_VEC_OPERAND_RE.search(operands))
    if has_vec:
        return True
    has_only_gp = bool(_GP_REG_RE.search(operands)) and not has_vec
    if has_only_gp:
        # Things like `add x29, sp, #0x20` -> scaffolding.
        return False
    return True


_LINE_RE = re.compile(r"^\s*[0-9a-f]+:\s+(.+)$", re.IGNORECASE)


def extract_asm_form(name: str, rec: dict, sdk: str | None) -> str | None:
    """Compile a wrapper for this intrinsic and pull the dominant SIMD
    line out of the disassembly. Returns the line in GNU-style
    `mnemonic op1, op2, ...` form (what llvm-mca consumes), or None
    on failure."""
    try:
        sig = parse_signature(rec["definition"])
    except Exception:
        return None
    flags = compile_flags_for(sig, rec.get("family", []))
    dis = None
    # Try imm=1 first (works for shifts + most lanes), fall back to 0
    # (works for lane=0 cases on single-lane types).
    for imm in (1, 0):
        src = _wrap_source(sig, imm_value=imm)
        if src is None:
            return None
        with tempfile.TemporaryDirectory() as td:
            s = Path(td) / "h.cc"
            o = Path(td) / "h.o"
            s.write_text(src)
            cmd = [CLANG, "-O2", "-c"]
            if sdk:
                cmd += ["-isysroot", sdk]
            cmd += flags + [str(s), "-o", str(o)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                continue
            dis = subprocess.run(
                [
                    LLVM_OBJDUMP,
                    "--triple=aarch64-linux-gnu",
                    "--no-show-raw-insn",
                    "--disassemble-symbols=_wrapped",
                    "-d",
                    str(o),
                ],
                capture_output=True,
                text=True,
            ).stdout
            break
    if dis is None:
        return None
    interesting: list[str] = []
    for line in dis.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        body = m.group(1).strip()
        body = re.sub(r"\s+", " ", body)
        if _is_interesting(body):
            interesting.append(body)
    if not interesting:
        return None
    # The dominant SIMD line is the first non-boring instruction;
    # subsequent ones are usually `ret` (already filtered) or another
    # mov shuffle. Heuristic: pick the longest, since SIMD ops tend to
    # have the most operands.
    interesting.sort(key=len, reverse=True)
    return interesting[0]


def _is_arm_simd(rec: dict) -> bool:
    """ARM intrinsic that *might* lower to a single SIMD instruction.
    We skip non-vector scalar bridges (e.g. __arm_sqshl which is an
    M-profile scalar op) by requiring a NEON/Helium family tag."""
    if rec.get("source") != "arm-acle":
        return False
    fams = set(rec.get("family") or [])
    return bool(fams & {"Neon", "Helium"})


def _run_mca(form: str) -> dict[str, list]:
    """Run llvm-mca --instruction-info for one asm form across all
    microarchs. Returns {microarch: [uops, latency, rThroughput]}."""
    out: dict[str, list] = {}
    for mcpu in MICROARCHS:
        with tempfile.NamedTemporaryFile("w", suffix=".s", delete=False) as f:
            f.write(form + "\n")
            sp = f.name
        try:
            r = subprocess.run(
                [str(LLVM_MCA), "-mtriple=aarch64",
                 f"-mcpu={mcpu}", "--instruction-info", sp],
                capture_output=True, text=True,
            )
        finally:
            Path(sp).unlink(missing_ok=True)
        if r.returncode != 0:
            continue
        # Parse the "Instruction Info" block. The data row looks like:
        #   ' 1      2     0.25                        add v0.4s, v1.4s, v2.4s'
        # uops, latency, rThroughput, [MayLoad], [MayStore], [HasSideEffects]
        for line in r.stdout.splitlines():
            m = re.match(
                r"^\s+(\d+)\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)", line
            )
            if m and "Instructions:" not in line:
                uops = int(m.group(1))
                lat = float(m.group(2))
                if lat == int(lat):
                    lat = int(lat)
                rt = float(m.group(3))
                out[mcpu] = [uops, lat, rt]
                break
    return out


def _extract_one(item):
    sdk, name, rec = item
    form = extract_asm_form(name, rec, sdk)
    return name, form


def main() -> int:
    print(f"Reading {DB_PATH}...")
    records: dict[str, dict] = {}
    with DB_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            n = r["intrinsic"]
            if _is_arm_simd(r):
                records[n] = r
    print(f"  {len(records):,} ARM SIMD intrinsics to process")

    sdk = _macos_sdk_path()
    # Phase 1: extract asm form per intrinsic (parallel compile is the
    # bottleneck; share workers across cores).
    intrinsic_to_form: dict[str, str] = {}
    items = [(sdk, n, r) for n, r in records.items()]
    done = 0
    print("Phase 1: extracting asm forms...")
    with ProcessPoolExecutor() as pool:
        futs = [pool.submit(_extract_one, it) for it in items]
        for fut in as_completed(futs):
            name, form = fut.result()
            done += 1
            if form:
                intrinsic_to_form[name] = form
            if done % 500 == 0 or done == len(items):
                print(f"  {done:,}/{len(items):,} (got {len(intrinsic_to_form):,} forms)")

    forms = sorted(set(intrinsic_to_form.values()))
    print(f"  unique asm forms: {len(forms):,}")

    # Phase 2: llvm-mca per unique form × microarch.
    print(f"Phase 2: running llvm-mca for {len(forms):,} forms × {len(MICROARCHS)} microarchs...")
    form_perf: dict[str, list] = {}
    done = 0
    with ProcessPoolExecutor() as pool:
        futs = {pool.submit(_run_mca, f): f for f in forms}
        for fut in as_completed(futs):
            form = futs[fut]
            data = fut.result()
            row = [data.get(m) for m in MICROARCHS]
            form_perf[form] = row
            done += 1
            if done % 200 == 0 or done == len(forms):
                print(f"  {done:,}/{len(forms):,}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        json.dumps(
            {
                "microarchs": MICROARCHS,
                "forms": form_perf,
                "intrinsics": intrinsic_to_form,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    size = OUT_PATH.stat().st_size
    print(f"wrote {OUT_PATH}: {size:,} bytes "
          f"({len(intrinsic_to_form):,} intrinsics, {len(forms):,} forms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
