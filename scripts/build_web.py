#!/usr/bin/env python3
"""Build web-distribution artifacts from data/intrinsics.jsonl.

Outputs (under simd-tooltip/dist/):

    simd-names.json   ~150 KB  -- compact name index for fast DOM scanning.
                                  Shape: {"version": ..., "names": ["...", ...]}
                                  All canonical names + non-ambiguous aliases.

    simd-data.json    ~7-8 MB  -- per-name record map for tooltip rendering.
                                  Shape: {"version": ..., "records": {<name>: {...}}}
                                  Includes both canonical names and aliases as keys
                                  (alias keys reuse the canonical record).

The library (simd-tooltip/dist/simd-tooltips.js) is hand-written, not generated.

The `description` field is truncated harder here (cap 200 chars, first sentence)
so the on-the-wire payload is friendlier.
"""

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "intrinsics.jsonl"
DIST = ROOT / "simd-tooltip" / "dist"

WEB_DESC_LIMIT = 220


def shorten(text: str, limit: int = WEB_DESC_LIMIT) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    m = re.search(r"[.!?](?:\s|$)", text)
    if m and m.end() <= limit + 1:
        return text[: m.end()].rstrip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# SIMD data type extraction + classification.
# We mine type tokens that appear in intrinsic signatures, then classify each
# by a regex pattern and synthesize a factual description (uncopyrightable:
# function of bit-widths, lane counts, ISA family).
# ---------------------------------------------------------------------------

# Patterns the matcher considers "definitely a type".
_INTEL_VEC = re.compile(r"^__m(64|128|256|512)(bh|h|d|i)?$")
_INTEL_MASK = re.compile(r"^__mmask(8|16|32|64)$")
_INTEL_AMX = re.compile(r"^__tile1024i$")
_INTEL_BF16 = re.compile(r"^__bf16$|^_Float16$")
_ARM_NEON = re.compile(r"^(int|uint|float|bfloat|poly|mfloat)(\d+)x(\d+)(?:x([234]))?_t$")
_ARM_SVE = re.compile(r"^sv(int|uint|float|bfloat|mfloat)(\d+)(?:x([234]))?_t$")
_ARM_SVE_PRED = re.compile(r"^sv(bool|count)(?:x([234]))?_t$")
_ARM_SCALAR = re.compile(r"^(float|bfloat|mfloat|poly)(\d+)_t$")
_MVE_PRED = re.compile(r"^mve_pred16_t$")

_INTEL_SUFFIX_DESC = {
    None: ("single-precision float", "FP32"),
    "d":  ("double-precision float", "FP64"),
    "i":  ("integer",               "int"),
    "h":  ("half-precision (FP16) float", "FP16"),
    "bh": ("bfloat16",              "BF16"),
}


def _intel_family(bits: int, suffix: str | None) -> str:
    if suffix == "h":  return "AVX-512 FP16"
    if suffix == "bh": return "AVX-512 BF16"
    if bits == 64:  return "MMX"
    if bits == 128: return "SSE / SSE2"
    if bits == 256: return "AVX / AVX2"
    if bits == 512: return "AVX-512"
    return "?"


def classify_type(name: str) -> dict | None:
    """Return a record dict for `name` if it's a recognized SIMD type, else None."""
    m = _INTEL_VEC.match(name)
    if m:
        bits = int(m.group(1)); suffix = m.group(2)
        elem, _ = _INTEL_SUFFIX_DESC[suffix]
        family = _intel_family(bits, suffix)
        return _record_type(name, ["x86_64"], [family],
            f"typedef {name};   // {bits}-bit {elem} vector",
            f"{bits}-bit packed {elem} vector. Used by {family} intrinsics.")

    m = _INTEL_MASK.match(name)
    if m:
        n = int(m.group(1))
        return _record_type(name, ["x86_64"], ["AVX-512"],
            f"typedef {name};   // {n}-bit mask register",
            f"{n}-bit AVX-512 opmask (predicate). 1 bit per lane; selects which lanes "
            "an instruction writes back. Loaded/stored as an integer of the same width.")

    if _INTEL_AMX.match(name):
        return _record_type(name, ["x86_64"], ["AMX"],
            f"typedef {name};   // 1024-byte AMX tile",
            "AMX tile (up to 16 rows × 64 bytes). Backs the matrix-extension "
            "intrinsics (_tile_loadd / _tile_dpbssd / _tile_stored / etc.).")

    if _INTEL_BF16.match(name):
        return _record_type(name, ["x86_64"], ["AVX-512 BF16" if name == "__bf16" else "AVX-512 FP16"],
            f"typedef {name};   // scalar half-width float",
            "Scalar half-width floating-point type (16-bit) used by the "
            "narrow-float Intel intrinsic surfaces.")

    m = _ARM_NEON.match(name)
    if m:
        elem_kind, bits, lanes, tup = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        total = bits * lanes
        elem_label = {
            "int": "signed integer", "uint": "unsigned integer",
            "float": "floating-point", "bfloat": "bfloat16" if bits == 16 else f"bfloat{bits}",
            "poly": "polynomial", "mfloat": "modal-8 floating-point",
        }[elem_kind]
        if tup:
            base = f"{elem_kind}{bits}x{lanes}_t"
            family = ["Neon"] if total in (64, 128) else ["Helium"]
            arch = _arm_arch_for_neon(total)
            return _record_type(name, arch, family,
                f"typedef struct {{ {base} val[{tup}]; }} {name};",
                f"{tup}-tuple of {base}. Used for de/interleaved load/store intrinsics "
                f"(e.g. vld{tup}q_*, vst{tup}q_*).")
        family = "Neon"
        arch = _arm_arch_for_neon(total)
        return _record_type(name, arch, [family],
            f"typedef {name};   // {lanes} lanes of {bits}-bit {elem_label} ({total}-bit vector)",
            f"{lanes} lanes of {bits}-bit {elem_label} packed into a {total}-bit vector. "
            f"{'NEON 64-bit (D-register)' if total == 64 else 'NEON 128-bit (Q-register)'}.")

    m = _ARM_SVE.match(name)
    if m:
        elem_kind, bits, tup = m.group(1), int(m.group(2)), m.group(3)
        elem_label = {
            "int": "signed integer", "uint": "unsigned integer",
            "float": "floating-point", "bfloat": "bfloat16" if bits == 16 else f"bfloat{bits}",
            "mfloat": "modal-8 floating-point",
        }[elem_kind]
        if tup:
            base = f"sv{elem_kind}{bits}_t"
            return _record_type(name, ["aarch64"], ["SVE"],
                f"typedef struct {{ {base} val[{tup}]; }} {name};",
                f"{tup}-tuple of {base}. Used for de/interleaved SVE load/store "
                f"(svld{tup}_*, svst{tup}_*).")
        return _record_type(name, ["aarch64"], ["SVE"],
            f"typedef {name};   // scalable vector of {bits}-bit {elem_label}",
            f"Scalable vector of {bits}-bit {elem_label}. Length is hardware-dependent "
            f"(128–2048 bits, multiple of 128). Used by SVE / SVE2 / SME intrinsics.")

    m = _ARM_SVE_PRED.match(name)
    if m:
        kind, tup = m.group(1), m.group(2)
        if tup:
            base = f"sv{kind}_t"
            return _record_type(name, ["aarch64"], ["SVE"],
                f"typedef struct {{ {base} val[{tup}]; }} {name};",
                f"{tup}-tuple of {base}.")
        if kind == "bool":
            return _record_type(name, ["aarch64"], ["SVE"],
                f"typedef {name};   // SVE predicate (governing mask)",
                "SVE predicate (mask). One bit per byte of governed data; "
                "selects active lanes for predicated instructions.")
        # svcount_t
        return _record_type(name, ["aarch64"], ["SVE2.1", "SME"],
            f"typedef {name};   // SVE2.1/SME multi-vector predicate",
            "SVE2.1/SME multi-vector predicate. Encodes loop state for "
            "predicate-pair / multi-vector intrinsics.")

    m = _ARM_SCALAR.match(name)
    if m:
        kind, bits = m.group(1), int(m.group(2))
        what = {"float": "floating-point", "bfloat": "brain floating-point (BF16)",
                "mfloat": "modal-8 floating-point", "poly": "polynomial"}[kind]
        return _record_type(name, ["aarch32", "aarch64"], ["Neon"],
            f"typedef {name};   // scalar {bits}-bit {what}",
            f"Scalar {bits}-bit {what} type used by ARM SIMD intrinsic surfaces "
            f"(NEON / SVE / scalar bridges).")

    if _MVE_PRED.match(name):
        return _record_type(name, ["armv8m"], ["Helium"],
            "typedef mve_pred16_t;   // Helium predicate (16-bit)",
            "Helium (M-profile MVE) predicate. 16-bit mask backing predicated MVE "
            "instructions; produced by vctp* and consumed by vpst.")

    return None


def _arm_arch_for_neon(total_bits: int) -> list[str]:
    # NEON 64-bit and 128-bit vectors exist on both A32 and A64.
    return ["aarch32", "aarch64"]


def _record_type(name: str, arch: list[str], family: list[str],
                 definition: str, description: str) -> dict:
    return {
        "name": name,
        "kind": "type",
        "arch": list(arch),
        "family": list(family),
        "definition": definition,
        "description": shorten(description),
        "source": "synth",
        "doc_url": "",
    }


def extract_types(records_path: Path) -> dict[str, dict]:
    """Walk all signatures in records_path and emit a {name: type-record} dict."""
    type_token = re.compile(r"\b(?:__\w+|sv\w+_t|\w+_t)\b")
    seen: set[str] = set()
    out: dict[str, dict] = {}
    with records_path.open() as f:
        for line in f:
            r = json.loads(line)
            for tok in type_token.findall(r["definition"]):
                if tok in seen:
                    continue
                seen.add(tok)
                rec = classify_type(tok)
                if rec:
                    out[tok] = rec
    return out


def doc_url(name: str, source: str) -> str:
    if source == "arm-acle":
        return f"https://developer.arm.com/architectures/instruction-sets/intrinsics/{name}"
    if source == "intel-iguide":
        return f"https://www.intel.com/content/www/us/en/docs/intrinsics-guide/index.html#text={name}"
    return ""


def main():
    DIST.mkdir(parents=True, exist_ok=True)

    by_canonical: dict[str, dict] = {}
    alias_targets: dict[str, list[str]] = {}

    with SRC.open() as f:
        for line in f:
            r = json.loads(line)
            name = r["intrinsic"]
            if name in by_canonical:
                # Duplicate canonical (e.g. Intel dual-CPUID rows). Merge the family lists
                # so the tooltip shows both ISAs; first occurrence wins for description.
                existing = by_canonical[name]
                existing["family"] = sorted(set(existing["family"]) | set(r["family"]))
                existing["arch"] = sorted(set(existing["arch"]) | set(r["arch"]))
                continue
            by_canonical[name] = {
                "name": name,
                "arch": r["arch"],
                "family": r["family"],
                "definition": r["definition"],
                "description": shorten(r.get("description", "")),
                "source": r["source"],
                "doc_url": doc_url(name, r["source"]),
            }
            for a in r.get("aliases", []):
                alias_targets.setdefault(a, []).append(name)

    # Promote non-ambiguous aliases into the lookup map.
    # Ambiguous aliases (one alias -> many canonicals) are kept in a separate dict
    # so the tooltip can render a "this overloaded form maps to N variants" choice.
    records: dict[str, dict] = dict(by_canonical)
    ambiguous_aliases: dict[str, list[str]] = {}
    for alias, targets in alias_targets.items():
        if alias in by_canonical:
            # An alias collides with a canonical name -- canonical wins.
            continue
        targets_unique = sorted(set(targets))
        if len(targets_unique) == 1:
            records[alias] = by_canonical[targets_unique[0]]
        else:
            ambiguous_aliases[alias] = targets_unique

    # Mine SIMD types from the same signatures and merge them in. Type records
    # never collide with intrinsic names (different namespaces), but if anything
    # ever does, intrinsics win.
    types = extract_types(SRC)
    type_names: list[str] = []
    for tname, trec in types.items():
        if tname in records:
            continue
        records[tname] = trec
        type_names.append(tname)
    type_count = len(type_names)

    # The names index = every key the matcher should treat as "look this up".
    # That is: canonicals + non-ambiguous aliases + ambiguous aliases (so the matcher
    # surfaces a hint), but ambiguous resolution is deferred to runtime.
    names = sorted(set(records.keys()) | set(ambiguous_aliases.keys()))

    names_doc = {
        "version": 1,
        "count": len(names),
        "names": names,
        "types": sorted(type_names),     # subset of names that are SIMD types
        "ambiguous": ambiguous_aliases,
    }
    data_doc = {
        "version": 1,
        "count": len(records),
        "records": records,
    }

    (DIST / "simd-names.json").write_text(
        json.dumps(names_doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    (DIST / "simd-data.json").write_text(
        json.dumps(data_doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    )

    names_size = (DIST / "simd-names.json").stat().st_size
    data_size = (DIST / "simd-data.json").stat().st_size

    rel = DIST.relative_to(ROOT)
    print(f"{rel}/simd-names.json  {names_size:>10,} B  ({len(names):,} names; {len(ambiguous_aliases):,} ambiguous aliases)")
    print(f"{rel}/simd-data.json   {data_size:>10,} B  ({len(records):,} records, of which {type_count} are SIMD types)")

    # tiny per-source breakdown
    by_source = Counter(r["source"] for r in by_canonical.values())
    print(f"  intrinsics by source: {dict(by_source)}")


if __name__ == "__main__":
    main()
