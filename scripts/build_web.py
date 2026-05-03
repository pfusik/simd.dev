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

    # The names index = every key the matcher should treat as "look this up".
    # That is: canonicals + non-ambiguous aliases + ambiguous aliases (so the matcher
    # surfaces a hint), but ambiguous resolution is deferred to runtime.
    names = sorted(set(records.keys()) | set(ambiguous_aliases.keys()))

    names_doc = {
        "version": 1,
        "count": len(names),
        "names": names,
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
    print(f"{rel}/simd-data.json   {data_size:>10,} B  ({len(records):,} records)")

    # tiny per-source breakdown
    by_source = Counter(r["source"] for r in by_canonical.values())
    print(f"  by source: {dict(by_source)}")


if __name__ == "__main__":
    main()
