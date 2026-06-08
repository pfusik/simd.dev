#!/usr/bin/env python3
"""Scrape the felixcloutier.com/x86/ index for a {mnemonic: slug} map.

Felix Cloutier's site mirrors the Intel SDM with a stable URL per
instruction page. Grouped pages (e.g. PADDB+PADDW+PADDD+PADDQ on one
page) use a colon-joined slug; we record each mnemonic mapping to its
group's slug.

Output: cache/felix_x86.json = {"PADDD": "paddb:paddw:paddd:paddq", ...}

Run this rarely; the index is stable. Committing the cache file lets
the build reproduce without a live HTTP fetch.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cache" / "felix_x86.json"

INDEX_URL = "https://www.felixcloutier.com/x86/"

LINK_RE = re.compile(r"href='/x86/([^']+)'>([A-Z0-9_]+)<")


def main() -> int:
    req = urllib.request.Request(
        INDEX_URL,
        headers={"User-Agent": "simd.dev-build-script/1.0"},
    )
    with urllib.request.urlopen(req) as r:
        html = r.read().decode("utf-8", errors="replace")

    out: dict[str, str] = {}
    for slug, mnem in LINK_RE.findall(html):
        # Skip non-instruction entries (the index sometimes has anchor
        # links to itself, but those don't match this regex anyway).
        if not mnem:
            continue
        # Same mnemonic may appear in multiple groups (rare); the first
        # wins. Index is sorted alphabetically by mnemonic so this is
        # deterministic.
        out.setdefault(mnem, slug)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {OUT}: {len(out):,} mnemonics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
