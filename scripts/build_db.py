#!/usr/bin/env python3
"""Build a unified SIMD intrinsics database from cached upstream sources.

Inputs (from cache/):
  - arm_intrinsics.json     (ARM ACLE, CC-BY-SA-4.0 + patent grant)
  - arm_operations.json     (ARM ASL pseudocode, same license; optional)
  - intel_intrinsics.xml    (Intel Intrinsics Guide; only factual fields are kept)
  - llvm_descriptions.json  (mined from clang headers, Apache-2.0-WITH-LLVM-exception)

Output:
  - data/intrinsics.jsonl  -- one record per line
  - data/stats.json        -- summary counts

Record schema:
  intrinsic    str          canonical C name (e.g. "vadd_s8", "svadd_s32_z")
  aliases      list[str]    other callable names (e.g. ["svadd_z"] -- the ACLE-overloaded short form)
  arch         list[str]    target architectures, normalized: aarch32 | aarch64 | armv8m | x86_64
  family       list[str]    SIMD family/version tags (NEON, SVE, SVE2, SME, MVE, SSE2, AVX2, AVX512F, ...)
  definition   str          C signature, e.g. "int8x8_t vadd_s8(int8x8_t a, int8x8_t b)"
  description  str          short prose headline (may be empty)
  desc_source  str          where the description came from: "arm-acle" | "llvm" | "intel-iguide" | "synth" | ""
  pseudocode   str          upstream pseudocode (Intel <operation> or ARM ASL); may be empty
  source       str          upstream provenance: "arm-acle" | "intel-iguide"

Description sources (asymmetric due to licensing):
  ARM   -> upstream JSON's `description` field (CC-BY-SA, attribute Arm Limited).
  Intel -> in priority order:
             1. LLVM clang headers' Doxygen comments (Apache-2.0).
             2. Intel Intrinsics Guide XML `<description>` (no open license; we
                use the first sentence only and attribute Intel in NOTICE).
             3. Synthetic factual headline from category + instruction
                (uncopyrightable facts).

ARM ACLE encodes overload variance with bracketed segments in the name:
  - "[__arm_]vcreateq_f16"  -> vcreateq_f16 (canonical), __arm_vcreateq_f16 (alias)
  - "svadd[_s32]_z"         -> svadd_s32_z (canonical), svadd_z (overloaded alias)
"""

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"
DATA = ROOT / "data"

DESC_MAX_LEN = 280  # tooltip-friendly cap


def shorten(text: str, limit: int = DESC_MAX_LEN) -> str:
    """Collapse whitespace and clamp to ~one sentence / `limit` chars."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    # Cut at first sentence terminator if it fits comfortably.
    m = re.search(r"[.!?](?:\s|$)", text)
    if m and m.end() <= limit + 1:
        text = text[: m.end()].rstrip()
    elif len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


# ---------------------------------------------------------------------------
# ARM
# ---------------------------------------------------------------------------

ARM_ARCH_MAP = {
    "A64": "aarch64",
    "A32": "aarch32",
    "v7": "aarch32",
    "MVE": "armv8m",
}


_BRACKET_RE = re.compile(r"\[([^\]]*)\]")


def arm_names(raw: str) -> tuple[str, list[str]]:
    """Return (canonical_name, [aliases]).

    Canonical = brackets removed, *contents kept* (the always-callable form).
    Aliases   = brackets and their contents removed (overloaded form), if different.
    """
    canonical = _BRACKET_RE.sub(lambda m: m.group(1), raw)
    overloaded = _BRACKET_RE.sub("", raw)
    aliases = [overloaded] if overloaded != canonical else []
    return canonical, aliases


def arm_signature(entry: dict, name: str) -> str:
    rt = entry.get("return_type") or {}
    rt_value = rt.get("value", "void") if isinstance(rt, dict) else str(rt)
    args = entry.get("arguments") or []
    if not args:
        return f"{rt_value} {name}(void)"
    if len(args) <= 2:
        joined = ", ".join(args)
        return f"{rt_value} {name}({joined})"
    # multi-arg: pretty-print like ACLE does
    inner = ",\n    ".join(args)
    return f"{rt_value} {name}(\n    {inner})"


_ARM_HTML_TAG = re.compile(r"<[^>]+>")
_ARM_HTML_LINK = re.compile(r'<a [^>]*>([^<]*)</a>')
_ARM_HEADING = re.compile(r'<h\d>[^<]*</h\d>')


def _arm_strip_html(s: str) -> str:
    """Strip HTML tags from ARM operations content, keeping link text. The
    upstream is HTML-wrapped ASL pseudocode with a leading <h4>Operation</h4>
    heading and cross-references to the ARM ARM. Drop the heading entirely
    (it's an artifact of the rendered docs page) and keep the readable text."""
    if not s:
        return ""
    s = _ARM_HEADING.sub("", s)              # drop <h4>Operation</h4>
    s = _ARM_HTML_LINK.sub(r"\1", s)         # keep link text
    s = _ARM_HTML_TAG.sub("", s)             # drop everything else
    # Decode the few entities ARM actually uses.
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&nbsp;", " ")
    return s.strip()


def _arm_load_operations() -> dict[str, str]:
    """Load cache/arm_operations.json (when present) and return a map
    {operation_id: stripped_text}. Missing file is OK -- pseudocode is
    optional, the rest of the DB still builds."""
    src = CACHE / "arm_operations.json"
    if not src.exists():
        return {}
    raw = json.loads(src.read_text())
    out: dict[str, str] = {}
    for entry in raw:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not item:
            continue
        out[item["id"]] = _arm_strip_html(item.get("content", ""))
    return out


def arm_records():
    src = CACHE / "arm_intrinsics.json"
    with src.open() as f:
        entries = json.load(f)
    op_text = _arm_load_operations()

    for e in entries:
        archs_raw = e.get("Architectures") or []
        archs = sorted({ARM_ARCH_MAP.get(a, a) for a in archs_raw})
        family = sorted(set(e.get("SIMD_ISA") or []))
        name, aliases = arm_names(e["name"])
        desc = shorten(e.get("description") or "")
        op_id = e.get("Operation")
        pseudocode = op_text.get(op_id, "") if op_id else ""
        yield {
            "intrinsic": name,
            "aliases": aliases,
            "arch": archs,
            "family": family,
            "definition": arm_signature(e, name),
            "description": desc,
            "desc_source": "arm-acle" if desc else "",
            "pseudocode": pseudocode,
            "source": "arm-acle",
        }


# ---------------------------------------------------------------------------
# Intel
# ---------------------------------------------------------------------------

# Intel intrinsics that take or return 64-bit-only scalar types are x86_64-only;
# everything else compiles for both 32- and 64-bit. We approximate coarsely and
# treat all entries as x86_64 (the dominant modern target). A future refinement
# could split arch by inspecting parameter types.
INTEL_DEFAULT_ARCH = ["x86_64"]


def intel_signature(intr: ET.Element) -> str:
    rt = intr.find("return")
    rt_type = rt.get("type", "void") if rt is not None else "void"
    name = intr.get("name", "")
    params = intr.findall("parameter")
    if not params or (len(params) == 1 and params[0].get("type", "").strip() == "void"):
        return f"{rt_type} {name}(void)"
    parts = []
    for p in params:
        ptype = p.get("type", "").strip()
        pname = p.get("varname", "").strip()
        parts.append(f"{ptype} {pname}".strip())
    if len(parts) <= 2:
        return f"{rt_type} {name}({', '.join(parts)})"
    inner = ",\n    ".join(parts)
    return f"{rt_type} {name}(\n    {inner})"


def synth_intel_description(intr: ET.Element) -> str:
    """Factual headline derived from category + underlying instruction(s).

    Both fields are facts (function category + CPU instruction mnemonic),
    so this fallback avoids any prose-copyright concern when LLVM has no
    Doxygen for the intrinsic.
    """
    category = (intr.get("category") or "").strip() or None
    if category is None:
        c_el = intr.find("category")
        if c_el is not None and c_el.text:
            category = c_el.text.strip()
    insns = []
    for ins in intr.findall("instruction"):
        nm = ins.get("name")
        if nm and nm not in insns:
            insns.append(nm)
    bits = []
    if category:
        bits.append(f"[{category}]")
    if insns:
        bits.append("compiles to " + "/".join(insns[:3]) + ".")
    return " ".join(bits).strip()


def intel_records():
    src = CACHE / "intel_intrinsics.xml"
    tree = ET.parse(src)
    root = tree.getroot()

    llvm_path = CACHE / "llvm_descriptions.json"
    llvm = json.loads(llvm_path.read_text()) if llvm_path.exists() else {}

    for intr in root.findall("intrinsic"):
        name = intr.get("name", "")
        if not name:
            continue
        cpuids = [c.text.strip() for c in intr.findall("CPUID") if c.text]
        # If no CPUID is given, fall back to the "tech" attribute (e.g. "Other", "AVX-512").
        family = sorted(set(cpuids)) if cpuids else [intr.get("tech", "Other")]

        if name in llvm:
            desc = shorten(llvm[name])
            desc_source = "llvm"
        else:
            xml_desc = (intr.findtext("description") or "").strip()
            if xml_desc:
                desc = shorten(xml_desc)
                desc_source = "intel-iguide"
            else:
                desc = shorten(synth_intel_description(intr))
                desc_source = "synth" if desc else ""

        pseudocode = (intr.findtext("operation") or "").strip()

        yield {
            "intrinsic": name,
            "aliases": [],
            "arch": list(INTEL_DEFAULT_ARCH),
            "family": family,
            "definition": intel_signature(intr),
            "description": desc,
            "desc_source": desc_source,
            "pseudocode": pseudocode,
            "source": "intel-iguide",
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    DATA.mkdir(parents=True, exist_ok=True)
    out_path = DATA / "intrinsics.jsonl"
    stats_path = DATA / "stats.json"

    n = 0
    family_counter = Counter()
    arch_counter = Counter()
    source_counter = Counter()
    desc_source_counter = Counter()
    pseudocode_counter = Counter()
    by_source_family = {"arm-acle": Counter(), "intel-iguide": Counter()}

    with out_path.open("w") as out:
        for src_iter in (arm_records, intel_records):
            for rec in src_iter():
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                n += 1
                source_counter[rec["source"]] += 1
                desc_source_counter[rec.get("desc_source") or "(none)"] += 1
                if rec.get("pseudocode"):
                    pseudocode_counter[rec["source"]] += 1
                for f in rec["family"]:
                    family_counter[f] += 1
                    by_source_family[rec["source"]][f] += 1
                for a in rec["arch"]:
                    arch_counter[a] += 1

    stats = {
        "total": n,
        "by_source": dict(source_counter),
        "by_arch": dict(arch_counter),
        "by_desc_source": dict(desc_source_counter),
        "with_pseudocode": dict(pseudocode_counter),
        "by_family": dict(family_counter.most_common()),
        "by_source_family": {k: dict(v.most_common()) for k, v in by_source_family.items()},
    }
    with stats_path.open("w") as f:
        json.dump(stats, f, indent=2)

    print(f"wrote {n} records -> {out_path.relative_to(ROOT)}")
    print(f"stats             -> {stats_path.relative_to(ROOT)}")
    print()
    print("by_source:     ", dict(source_counter))
    print("by_arch:       ", dict(arch_counter))
    print("by_desc_source:", dict(desc_source_counter))
    print("with_pseudocode:", dict(pseudocode_counter))
    print("top families:")
    for f, c in family_counter.most_common(15):
        print(f"  {c:6d}  {f}")


if __name__ == "__main__":
    main()
