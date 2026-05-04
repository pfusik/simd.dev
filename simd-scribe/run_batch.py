#!/usr/bin/env python3
"""Run scribe across a filter of the DB and report success/failure stats.

Usage:
    python3 simd-scribe/run_batch.py --family Neon
    python3 simd-scribe/run_batch.py --family Neon --cache-out data/verifier-cache/neon.jsonl
    python3 simd-scribe/run_batch.py --family Neon --limit 100 --show-fail
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scribe import (  # noqa: E402
    DB_PATH,
    build_inputs,
    compile_and_extract,
    decode_lanes,
    effective_return,
    emit_source,
    parse_signature,
)
from run_cluster import (  # noqa: E402
    compiler_id,
    inputs_hash,
    upsert_cache,
)


def categorize_error(e: Exception) -> str:
    msg = str(e).split("\n", 1)[0]
    # Collapse common families to keep the report readable.
    lower = msg.lower()
    if "unsupported param type" in lower or "unsupported return type" in lower:
        return f"unsupported-type: {msg}"
    if "unsupported type" in lower:
        return f"unsupported-type: {msg}"
    if "cannot parse signature" in lower or "cannot parse param" in lower:
        return f"parse: {msg[:80]}"
    if "unknown role" in lower:
        return "harness: unknown role"
    if "expected" in lower and "lanes" in lower:
        return "harness: lane-count mismatch"
    if "clang failed" in lower:
        # Look for a meaningful suffix from stderr
        return "clang: " + (msg[len("clang failed:"):].strip()[:80] or "?")
    if "could not locate result bytes" in lower:
        return "fold: result not in __const (call did not fold)"
    return f"other: {type(e).__name__}: {msg[:80]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--family", action="append",
                    help="only records whose family list contains this (repeatable)")
    ap.add_argument("--source", action="append",
                    help="only records from this source (e.g. arm-acle, intel-iguide)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many records to attempt")
    ap.add_argument("--cache-out",
                    help="merge successful examples into this verifier-cache shard")
    ap.add_argument("--show-fail", action="store_true",
                    help="print every failure (otherwise summary only)")
    ap.add_argument("--show-pass", action="store_true",
                    help="print every success (otherwise summary only)")
    args = ap.parse_args()

    fams = set(args.family) if args.family else None
    sources = set(args.source) if args.source else None

    candidates: list[dict] = []
    with DB_PATH.open() as f:
        for line in f:
            r = json.loads(line)
            if fams and not (fams & set(r.get("family", []))):
                continue
            if sources and r.get("source") not in sources:
                continue
            candidates.append(r)

    if args.limit:
        candidates = candidates[: args.limit]

    print(f"Running scribe on {len(candidates):,} candidates")

    successes: list[dict] = []
    method_counter: Counter = Counter()
    fail_counter: Counter = Counter()
    fail_examples: dict[str, str] = {}  # category -> example name

    for r in candidates:
        name = r["intrinsic"]
        try:
            sig = parse_signature(r["definition"])
            inputs = build_inputs(sig)
            source = emit_source(sig, inputs)
            ret_ti, _ = effective_return(sig)
            out_bytes, method = compile_and_extract(
                source, expected_bytes=ret_ti.total_bytes
            )
            out_lanes = decode_lanes(ret_ti, out_bytes)
        except Exception as e:
            cat = categorize_error(e)
            fail_counter[cat] += 1
            fail_examples.setdefault(cat, name)
            if args.show_fail:
                print(f"  FAIL  {name:36s}  {cat}")
            continue

        method_counter[method] += 1
        # For stores (sig.return_type == "void"), report the synthesized
        # buffer type that captures what the intrinsic actually wrote.
        out_type_name = sig.return_type
        if out_type_name == "void":
            _, ret_decl = effective_return(sig)
            out_type_name = ret_decl
        # Strip store pointer params from inputs (they're output buffers,
        # we don't generate values for them).
        shipped_inputs = [
            {"name": p.name, "type": p.type_name, "values": vals}
            for p, vals in zip(sig.params, inputs) if vals
        ]
        successes.append({
            "intrinsic": name,
            "definition": r["definition"],
            "family": r["family"],
            "arch": r["arch"],
            "description": r.get("description", ""),
            "cluster_id": r.get("pseudocode_hash") or "",
            "verified_via": method,
            "inputs": shipped_inputs,
            "output": {
                "type": out_type_name,
                "bytes_hex": out_bytes.hex(),
                "values": out_lanes,
            },
        })
        if args.show_pass:
            print(f"  OK    {name:36s}  via {method:7s}  -> {out_bytes.hex()}")

    n = len(candidates)
    n_ok = len(successes)
    n_fail = n - n_ok
    print()
    print(f"Summary: {n_ok:,}/{n:,} succeeded ({100.0 * n_ok / max(n, 1):.1f}%), "
          f"{n_fail:,} failed")
    if method_counter:
        parts = ", ".join(f"{m}={c}" for m, c in method_counter.most_common())
        print(f"  by method: {parts}")
    if fail_counter:
        print()
        print("Failure categories (top 30):")
        for cat, count in fail_counter.most_common(30):
            example = fail_examples[cat]
            print(f"  {count:5,d}  {cat}  (e.g. {example})")

    if args.cache_out and successes:
        cid = compiler_id()
        today = _dt.date.today().isoformat()
        cache_entries = []
        for ex in successes:
            ihash = inputs_hash(ex["inputs"])
            cache_entries.append({
                "intrinsic": ex["intrinsic"],
                "inputs_hash": ihash,
                "compiler_id": cid,
                "march": "",
                "cluster_id": ex.get("cluster_id", ""),
                "verified_via": ex["verified_via"],
                "inputs": ex["inputs"],
                "output": ex["output"],
                "verified_at": today,
            })
        added, updated = upsert_cache(Path(args.cache_out), cache_entries)
        print()
        print(f"Cache {args.cache_out}: +{added} new, ~{updated} updated, "
              f"total entries written = {added + updated}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
