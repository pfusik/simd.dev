#!/usr/bin/env python3
"""Pilot driver: run the verifier over every member of a cluster.

Inputs are generated deterministically per parameter type (LLM is *not*
involved in input/output generation — it'll only contribute prose later).
Outputs come from clang's constant folder via simd-scribe.scribe.

Usage:
    python3 simd-scribe/run_cluster.py --cluster <pseudocode_hash>
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scribe import (  # noqa: E402
    DB_PATH,
    NEON_VEC_TYPES,
    SCALAR_TYPES,
    Signature,
    compile_and_extract,
    decode_lanes,
    emit_source,
    parse_signature,
)


def _bits_of(lane_type: str) -> int:
    return int(lane_type.replace("uint", "").replace("int", "").replace("_t", ""))


def _is_unsigned(lane_type: str) -> bool:
    return lane_type.startswith("uint")


def _shape_of(param_type: str) -> tuple[int, int, bool]:
    """Return (lane_count, lane_bits, unsigned)."""
    if param_type in NEON_VEC_TYPES:
        lane_type, count, _ = NEON_VEC_TYPES[param_type]
    elif param_type in SCALAR_TYPES:
        lane_type, count = param_type, 1
    else:
        raise ValueError(f"unsupported type {param_type}")
    return count, _bits_of(lane_type), _is_unsigned(lane_type)


# Pre-baked patterns per bit width. Picks values that fit comfortably in
# both signed and unsigned lanes of that width, with a little variety so
# overflow / sign behavior is visible in the output bytes.
A_PATTERNS: dict[int, list[int]] = {
    8:  [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16],
    16: [100, 200, 300, 400, 500, 600, 700, 800],
    32: [10, 20, 30, 40],
    64: [1000, 2000],
}
B_SIGNED: dict[int, list[int]] = {
    8:  [-50, 30, -20, 10, -5, 15, -25, 35, -40, 20, -10, 5, -45, 25, -15, 50],
    16: [-5000, 3000, -2000, 1000, -500, 1500, -2500, 3500],
    32: [-1_000_000_000, 1_500_000_000, -500_000_000, 750_000_000],
    64: [-9_000_000_000, 4_500_000_000],
}
B_UNSIGNED: dict[int, list[int]] = {
    8:  [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150, 160],
    16: [1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000],
    32: [1_000_000_000, 2_000_000_000, 3_000_000_000, 4_000_000_000],
    64: [9_000_000_000, 18_000_000_000],
}


def make_input(param_type: str, role: str) -> list[int]:
    count, bits, unsigned = _shape_of(param_type)
    if role == "a":
        return A_PATTERNS[bits][:count]
    if role == "b":
        src = B_UNSIGNED if unsigned else B_SIGNED
        return src[bits][:count]
    raise ValueError(f"unknown role {role!r}")


def compiler_id() -> str:
    """A short, stable identifier for the local toolchain."""
    try:
        out = subprocess.run(
            ["clang++", "--version"], capture_output=True, text=True, check=True
        ).stdout
        first_line = out.splitlines()[0] if out else "clang"
        # e.g. "Apple clang version 17.0.0 (clang-1700.4.4.1)"
        return first_line.strip()
    except Exception:
        return "clang (unknown version)"


def inputs_hash(inputs: list[dict]) -> str:
    """SHA1[:8] over the canonical inputs payload — cache co-key with intrinsic."""
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def upsert_cache(cache_path: Path, new_entries: list[dict]) -> tuple[int, int]:
    """Merge new_entries into cache_path keyed by (intrinsic, inputs_hash).

    Returns (added, updated). Existing entries with no key match are kept.
    Output is sorted by (intrinsic, inputs_hash) for stable diffs.
    """
    existing: dict[tuple[str, str], dict] = {}
    if cache_path.exists():
        with cache_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                key = (e["intrinsic"], e["inputs_hash"])
                existing[key] = e

    added = updated = 0
    for entry in new_entries:
        key = (entry["intrinsic"], entry["inputs_hash"])
        if key in existing:
            updated += 1
        else:
            added += 1
        existing[key] = entry

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("w") as f:
        for key in sorted(existing):
            f.write(json.dumps(existing[key], ensure_ascii=False) + "\n")

    return added, updated


def fmt_inputs(sig: Signature, inputs: list[list[int]]) -> str:
    parts = []
    for p, vals in zip(sig.params, inputs):
        if len(vals) == 1:
            parts.append(f"{p.name}={vals[0]}")
        else:
            head = ",".join(str(v) for v in vals[:4])
            tail = "..." if len(vals) > 4 else ""
            parts.append(f"{p.name}=[{head}{tail}]")
    return "  ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cluster", required=True,
                    help="pseudocode_hash of the cluster to run")
    ap.add_argument("--full-inputs", action="store_true",
                    help="print full input vectors instead of truncated")
    ap.add_argument("--json-out",
                    help="write verified examples to this JSON path")
    ap.add_argument("--cache-out",
                    help="merge into a verifier-cache JSONL shard (e.g. "
                         "data/verifier-cache/neon.jsonl)")
    args = ap.parse_args()

    members = []
    with DB_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            if r.get("pseudocode_hash") == args.cluster:
                members.append(r)

    if not members:
        print(f"No members found for cluster {args.cluster!r}", file=sys.stderr)
        return 1

    print(f"Cluster {args.cluster}: {len(members)} members\n")

    failures: list[str] = []
    examples: list[dict] = []
    for r in members:
        name = r["intrinsic"]
        try:
            sig = parse_signature(r["definition"])
            inputs = [
                make_input(p.type_name, "a" if i == 0 else "b")
                for i, p in enumerate(sig.params)
            ]
            source = emit_source(sig, inputs)
            out_bytes = compile_and_extract(source)
            out_lanes = decode_lanes(sig.return_type, out_bytes)
        except Exception as e:
            failures.append(name)
            print(f"  {name:24s}  FAIL  {e!s}".replace("\n", "\n    "))
            continue

        in_repr = (
            "  ".join(f"{p.name}={vals}" for p, vals in zip(sig.params, inputs))
            if args.full_inputs else fmt_inputs(sig, inputs)
        )
        print(f"  {name:24s}  {in_repr}")
        print(f"    -> {out_bytes.hex()}  ({out_lanes})")

        examples.append({
            "intrinsic": name,
            "definition": r["definition"],
            "family": r["family"],
            "arch": r["arch"],
            "description": r.get("description", ""),
            "inputs": [
                {"name": p.name, "type": p.type_name, "values": vals}
                for p, vals in zip(sig.params, inputs)
            ],
            "output": {
                "type": sig.return_type,
                "bytes_hex": out_bytes.hex(),
                "values": out_lanes,
            },
        })

    print()
    print(f"Done: {len(members) - len(failures)} ok, {len(failures)} failed")
    if failures:
        print("Failures:", ", ".join(failures))

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w") as f:
            json.dump({
                "cluster_id": args.cluster,
                "members": examples,
            }, f, indent=2)
        print(f"Wrote {len(examples)} examples to {out_path}")

    if args.cache_out:
        cid = compiler_id()
        today = _dt.date.today().isoformat()
        cache_entries = []
        for ex in examples:
            ihash = inputs_hash(ex["inputs"])
            cache_entries.append({
                "intrinsic": ex["intrinsic"],
                "inputs_hash": ihash,
                "compiler_id": cid,
                "march": "",  # native build; cross-compile flags go here
                "cluster_id": args.cluster,
                "inputs": ex["inputs"],
                "output": ex["output"],
                "verified_at": today,
            })
        added, updated = upsert_cache(Path(args.cache_out), cache_entries)
        print(f"Cache {args.cache_out}: +{added} new, ~{updated} updated, "
              f"total entries = {added + updated}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
