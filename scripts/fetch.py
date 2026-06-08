#!/usr/bin/env python3
"""Fetch raw upstream intrinsic data into ./cache/.

Cross-platform replacement for the older fetch.sh. Always re-downloads
the three upstream files (ARM ACLE intrinsics + operations JSON, Intel
intrinsics XML) and then runs scrape_felix.py to refresh the felix slug
map. Re-run to refresh.
"""
from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "cache"

SOURCES = [
    ("ARM ACLE intrinsics JSON",
     "https://developer.arm.com/architectures/instruction-sets/intrinsics/data/intrinsics.json",
     "arm_intrinsics.json"),
    ("ARM operations JSON (ASL pseudocode)",
     "https://developer.arm.com/architectures/instruction-sets/intrinsics/data/operations.json",
     "arm_operations.json"),
    ("Intel intrinsics XML",
     "https://www.intel.com/content/dam/develop/public/us/en/include/intrinsics-guide/data-latest.xml",
     "intel_intrinsics.xml"),
]

# A polite, identifiable UA so neither side of the connection has to
# guess. urllib's default ("Python-urllib/X.Y") is sometimes rejected by
# CDNs.
USER_AGENT = "simd.dev-fetch/1.0 (+https://simd.dev)"


def fetch(label: str, url: str, dest: Path) -> None:
    print(f"fetching {label} ...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req) as r, dest.open("wb") as f:
        # Stream copy: Intel XML is a few MB, ARM JSON is larger; either
        # way, no need to buffer the whole body in memory.
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
    print(f"  {dest.stat().st_size} bytes")


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)
    for label, url, name in SOURCES:
        fetch(label, url, CACHE / name)

    print("scraping felixcloutier.com/x86/ index ...")
    rc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scrape_felix.py")],
    ).returncode
    if rc != 0:
        return rc

    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
