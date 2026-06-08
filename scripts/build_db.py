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

import hashlib
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


# Strings ARM/Intel use as placeholders when no real pseudocode is
# available. We must not cluster these as if they were a real shared
# operation (would group ~800 unrelated intrinsics under one "cluster").
_PSEUDOCODE_PLACEHOLDERS = {
    "no operation information.",
    "no operation information",
}

# Normalization passes applied to pseudocode *before* hashing. The intent is
# to strip variation that's purely a function of the intrinsic's type/width
# parameter, so e.g. svabalt_n_u{16,32,64} (which differ only by the cast in
# the prose) cluster together.
#
# Be conservative: only normalize patterns that are clearly type/width
# substitutions. Wider normalization (collapsing signed vs unsigned,
# stripping all-numeric tokens, …) risks merging genuinely different ops.

# `int{8,16,32,64,128}_t` / `uint*_t` / `float*_t` / `bfloat*_t` /
# `mfloat*_t` / `poly*_t`  →  `<TYPE>_t`. Collapses ALL type kinds (signed,
# unsigned, float, bfloat, poly, mfloat) into one placeholder so siblings
# whose pseudocode is character-identical except for the type cast cluster
# together. Ops where signed/unsigned have genuinely different pseudocode
# bodies (e.g. arithmetic vs. logical shift) will still split because their
# bodies differ outside the cast.
_NORM_TYPE_TOK = re.compile(r"\b(?:u?int|b?float|mfloat|poly)(?:8|16|32|64|128)(_t)\b")

# ARM SVE prose appends footnotes (`[1] This is true if …`, `[2] If instead
# result is in a different register …`) that are pure documentation noise
# and vary across siblings. Drop everything from the first `[N]` marker.
_NORM_FOOTNOTES = re.compile(r"\[\d+\].*$", re.DOTALL)

# ARM SVE prose for signed integer ops also appends "The operation uses
# modulo arithmetic. … no undefined behavior for signed overflow." which is
# a fixed phrase that doesn't appear on unsigned siblings. Drop it so a
# signed-op cluster can join its unsigned counterpart when nothing else
# differs in the prose.
_NORM_MODULO = re.compile(
    r"The operation uses modulo arithmetic\.[^.]*?signed overflow\.\s*",
    re.IGNORECASE | re.DOTALL,
)


def _normalize_pseudocode(pc: str) -> str:
    pc = _NORM_FOOTNOTES.sub("", pc)
    pc = _NORM_MODULO.sub("", pc)
    pc = _NORM_TYPE_TOK.sub(r"<TYPE>\1", pc)
    return pc.strip()


def pseudocode_hash(pc: str) -> str:
    """Stable cluster key for an intrinsic's upstream pseudocode.

    ARM ASL is heavily abstract (uses `Elem[...]`, `esize`, `elements`)
    so type/width variants of the same operation share an identical
    pseudocode string and naturally land in the same cluster. ARM SVE
    English prose embeds explicit type tokens (`(uint16_t)`) and footnote
    markers — _normalize_pseudocode strips those before hashing so
    type/width siblings cluster.

    Intel's `<operation>` mentions specific bit widths and lane counts;
    cross-width Intel clustering would need a more aggressive normalization
    pass and is left for the LLM-cluster idea in IDEAS.md.

    A short SHA1 prefix is plenty for ~22k records.
    """
    if not pc:
        return ""
    cleaned = pc.strip()
    if cleaned.lower() in _PSEUDOCODE_PLACEHOLDERS:
        return ""
    cleaned = _normalize_pseudocode(cleaned)
    return hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:12]


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


# Tag matcher requires `</?[a-zA-Z]` so literal `<` in the prose (e.g. the
# `<` in `0 ≤ indices[i] < n` for svtbx) isn't mistaken for an opening tag —
# the previous `<[^>]+>` would happily eat from a literal `<` through to the
# next real `>`, swallowing intervening text along with the closing tag.
_ARM_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_ARM_HTML_LINK = re.compile(r'<a [^>]*>([^<]*)</a>', re.IGNORECASE)
_ARM_HEADING = re.compile(r'<h\d>[^<]*</h\d>', re.IGNORECASE)
# Block boundaries we want to surface as whitespace so list items / paragraphs
# don't concatenate into one run-on string. Order doesn't matter; each match
# is replaced with a single newline before generic tag stripping happens.
_ARM_BLOCK_BREAK = re.compile(
    r'</(?:p|li|ul|ol|div|tr|table|h\d)>|<(?:br|li|p)\b[^>]*>',
    re.IGNORECASE,
)


def _arm_strip_html(s: str) -> str:
    """Strip HTML tags from ARM operations content, keeping link text. The
    upstream is HTML-wrapped ASL pseudocode with a leading <h4>Operation</h4>
    heading and cross-references to the ARM ARM. Drop the heading entirely
    (it's an artifact of the rendered docs page) and keep the readable text."""
    if not s:
        return ""
    s = _ARM_HEADING.sub("", s)              # drop <h4>Operation</h4>
    s = _ARM_BLOCK_BREAK.sub("\n", s)        # surface paragraph / list breaks
    s = _ARM_HTML_LINK.sub(r"\1", s)         # keep link text
    s = _ARM_HTML_TAG.sub("", s)             # drop remaining inline tags
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
    raw = json.loads(src.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for entry in raw:
        item = entry.get("item") if isinstance(entry, dict) else None
        if not item:
            continue
        out[item["id"]] = _arm_strip_html(item.get("content", ""))
    return out


def arm_records():
    src = CACHE / "arm_intrinsics.json"
    with src.open(encoding="utf-8") as f:
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
            # The raw ACLE-spec name with bracket markers preserved
            # (e.g. "[__arm_]vaddq[_u16]"). ARM's developer-portal URLs
            # are keyed on this exact form, so we keep it for URL
            # generation downstream.
            "acle_name": e["name"],
            "arch": archs,
            "family": family,
            "definition": arm_signature(e, name),
            "description": desc,
            "desc_source": "arm-acle" if desc else "",
            "pseudocode": pseudocode,
            "pseudocode_hash": pseudocode_hash(pseudocode),
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


FELIX_URL_BASE = "https://www.felixcloutier.com/x86/"


_FELIX_SUFFIXES = ("SS", "SD", "PS", "PD", "PH", "BH", "PI", "SI", "BR")


def felix_lookup(felix_map: dict[str, str], mnemonic: str) -> str | None:
    """Map an Intel iguide instruction mnemonic to its Felix Cloutier slug.

    Felix's index lists each base mnemonic with its (possibly grouped)
    page slug, but doesn't enumerate every encoded variant. Several
    naming patterns we have to strip to land on the index:

      VPADDD    -> PADDD     (VEX/EVEX V-prefix on a non-VEX op)
      VPORD     -> POR       (V-prefix + EVEX D/Q suffix)
      VBROADCASTSS -> VBROADCAST  (FP suffix SS/SD/PS/PD/...)
      VPMOVZXBQ -> PMOVZX    (source-dest pair suffix BD/BQ/BW/DQ/...)

    Try the candidates in priority order; first hit wins. Verbatim
    match is always tried first so EVEX-only ops with their own page
    (VPGATHERDD, VPTERNLOGD, VPOPCNTQ → vpopcnt) get their own page.
    """
    if not mnemonic:
        return None

    def derived(m: str) -> list[str]:
        out = [m]
        if len(m) > 3 and m[-1] in "DQBW":
            out.append(m[:-1])
        for sfx in _FELIX_SUFFIXES:
            if m.endswith(sfx) and len(m) > len(sfx) + 2:
                out.append(m[: -len(sfx)])
        if len(m) > 5 and m[-1] in "BWDQ" and m[-2] in "BWDQ":
            out.append(m[:-2])
        return out

    cands = list(derived(mnemonic))
    if mnemonic.startswith("V") and len(mnemonic) > 1:
        for c in derived(mnemonic[1:]):
            if c not in cands:
                cands.append(c)
    for c in cands:
        if c in felix_map:
            return felix_map[c]
    # Last-resort fallback: progressively chop trailing chars until we
    # land on an indexed prefix. Catches VBROADCASTI32X2 → VBROADCAST,
    # VEXTRACTF64X2 → VEXTRACT, etc. Cap at 6 chars min so we don't
    # mis-resolve a short mnemonic to an unrelated longer one.
    for L in range(len(mnemonic) - 1, 5, -1):
        if mnemonic[:L] in felix_map:
            return felix_map[mnemonic[:L]]
    return None


def felix_url_for_intrinsic(intr, felix_map: dict[str, str]) -> str | None:
    """If the intrinsic compiles to exactly one Felix-known mnemonic
    (across all its <instruction> entries -- typical for a single
    intrinsic, since width/EVEX variants share a base page), return
    the absolute Felix URL. Otherwise None.
    """
    slugs: set[str] = set()
    for ins in intr.findall("instruction"):
        m = (ins.get("name") or "").strip().upper()
        slug = felix_lookup(felix_map, m)
        if slug is None:
            return None
        slugs.add(slug)
    if len(slugs) != 1:
        return None
    return FELIX_URL_BASE + slugs.pop()


def asm_mnemonic_for_intrinsic(intr) -> str | None:
    """Return the single raw asm mnemonic this intrinsic lowers to, or
    None if there's zero or more than one distinct mnemonic. Used to
    build the uops.info search URL (which distinguishes operand-form
    variants under one mnemonic)."""
    names = {(ins.get("name") or "").strip().upper()
             for ins in intr.findall("instruction")}
    names.discard("")
    if len(names) != 1:
        return None
    return next(iter(names))


def xed_for_intrinsic(intr) -> str | None:
    """The iguide's <instruction> tag carries an XED operand-form
    encoding (e.g. PADDD_XMMdq_XMMdq). Intel's own perf2.js is keyed
    by these, so we ship the xed alongside the intrinsic record and
    let the front-end look up per-microarch latency/throughput at
    runtime. Skip if zero or multiple distinct xeds (rare)."""
    xeds = {(ins.get("xed") or "").strip()
            for ins in intr.findall("instruction")}
    xeds.discard("")
    if len(xeds) != 1:
        return None
    return next(iter(xeds))


def intel_records():
    src = CACHE / "intel_intrinsics.xml"
    tree = ET.parse(src)
    root = tree.getroot()

    llvm_path = CACHE / "llvm_descriptions.json"
    llvm = json.loads(llvm_path.read_text(encoding="utf-8")) if llvm_path.exists() else {}
    felix_path = CACHE / "felix_x86.json"
    felix_map = json.loads(felix_path.read_text(encoding="utf-8")) if felix_path.exists() else {}

    for intr in root.findall("intrinsic"):
        name = intr.get("name", "")
        if not name:
            continue
        cpuids = [c.text.strip() for c in intr.findall("CPUID") if c.text]
        # If no CPUID is given, fall back to the "tech" attribute (e.g. "Other", "AVX-512").
        family = sorted(set(cpuids)) if cpuids else [intr.get("tech", "Other")]

        # Prefer the Intel iguide's prose over clang's header comments:
        # the iguide uses public parameter names (e.g. `a`, `mask`),
        # whereas clang's headers refer to its internal `__X` / `__Y`
        # placeholders which don't match our exposed signatures. LLVM
        # text stays as the fallback for the rare case the iguide
        # ships an empty <description>.
        xml_desc = (intr.findtext("description") or "").strip()
        if xml_desc:
            desc = shorten(xml_desc)
            desc_source = "intel-iguide"
        elif name in llvm:
            desc = shorten(llvm[name])
            desc_source = "llvm"
        else:
            desc = shorten(synth_intel_description(intr))
            desc_source = "synth" if desc else ""

        pseudocode = (intr.findtext("operation") or "").strip()
        felix_url = felix_url_for_intrinsic(intr, felix_map)
        asm_mnemonic = asm_mnemonic_for_intrinsic(intr)
        xed = xed_for_intrinsic(intr)

        rec = {
            "intrinsic": name,
            "aliases": [],
            "arch": list(INTEL_DEFAULT_ARCH),
            "family": family,
            "definition": intel_signature(intr),
            "description": desc,
            "desc_source": desc_source,
            "pseudocode": pseudocode,
            "pseudocode_hash": pseudocode_hash(pseudocode),
            "source": "intel-iguide",
        }
        if felix_url:
            rec["felix_url"] = felix_url
        if asm_mnemonic:
            rec["asm_mnemonic"] = asm_mnemonic
        if xed:
            rec["xed"] = xed
        yield rec


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

    with out_path.open("w", encoding="utf-8") as out:
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
    with stats_path.open("w", encoding="utf-8") as f:
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
