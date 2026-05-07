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
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scribe import (  # noqa: E402
    DB_PATH,
    HINTS_PATH,
    _check_clang_version,
    _load_hints,
    build_inputs,
    compile_and_extract,
    compile_flags_for,
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


def _verify_one(r: dict) -> dict:
    """Worker entry point. Returns a dict with kind='ok' or kind='fail'.

    Lives at module level so ProcessPoolExecutor can pickle it.
    """
    name = r["intrinsic"]
    try:
        sig = parse_signature(r["definition"])
        inputs = build_inputs(sig)
        source = emit_source(sig, inputs)
        ret_ti, ret_decl = effective_return(sig)
        flags = compile_flags_for(sig, r.get("family", []))
        out_bytes, method = compile_and_extract(
            source,
            extra_flags=flags,
            expected_bytes=ret_ti.total_bytes,
        )
        out_lanes = decode_lanes(ret_ti, out_bytes)
    except Exception as e:
        return {
            "kind": "fail",
            "name": name,
            "error_type": type(e).__name__,
            "error_msg": str(e),
        }

    out_type_name = sig.return_type
    if out_type_name == "void":
        out_type_name = ret_decl
    shipped_inputs = [
        {"name": p.name, "type": p.type_name, "values": vals}
        for p, vals in zip(sig.params, inputs) if vals
    ]
    return {
        "kind": "ok",
        "method": method,
        "out_hex": out_bytes.hex(),
        "record": {
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
        },
    }


def categorize_error(error_type: str, error_msg: str) -> str:
    msg = error_msg.split("\n", 1)[0]
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
    return f"other: {error_type}: {msg[:80]}"


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
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                    help="parallel worker count (default: # of CPUs)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the existing cache shard; recompute everything")
    args = ap.parse_args()

    # Fail fast on a malformed hints.json or stale toolchain so we don't
    # fan it out across thousands of worker subprocesses each hitting the
    # same error.
    try:
        _load_hints()
    except json.JSONDecodeError as e:
        print(f"hints: {HINTS_PATH} is not valid JSON: {e}", file=sys.stderr)
        return 2
    try:
        _check_clang_version()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2

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

    # Load any existing cache so we can skip recomputing entries whose
    # (intrinsic, inputs_hash, compiler_id) matches what we'd produce now.
    existing_cache: dict[tuple[str, str], dict] = {}
    cache_path = Path(args.cache_out) if args.cache_out else None
    if cache_path and cache_path.exists() and not args.refresh:
        with cache_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = json.loads(line)
                existing_cache[(e["intrinsic"], e["inputs_hash"])] = e
    current_cid = compiler_id()

    successes: list[dict] = []  # in *output record* shape, ready for upsert_cache
    method_counter: Counter = Counter()
    fail_counter: Counter = Counter()
    fail_examples: dict[str, str] = {}

    # Partition candidates: cache hits skip the compile entirely, misses
    # go to the worker pool.
    misses: list[dict] = []
    hits = 0
    for r in candidates:
        if not existing_cache:
            misses.append(r)
            continue
        # Build the same inputs we'd feed to the harness now and hash
        # them. If a cached entry matches that hash *and* the compiler is
        # unchanged, it stays valid.
        try:
            sig = parse_signature(r["definition"])
            inputs = build_inputs(sig)
            shipped_inputs = [
                {"name": p.name, "type": p.type_name, "values": vals}
                for p, vals in zip(sig.params, inputs) if vals
            ]
            ihash = inputs_hash(shipped_inputs)
        except Exception:
            misses.append(r)
            continue
        cached = existing_cache.get((r["intrinsic"], ihash))
        if cached and cached.get("compiler_id") == current_cid:
            successes.append({
                "intrinsic": r["intrinsic"],
                "definition": r["definition"],
                "family": r["family"],
                "arch": r["arch"],
                "description": r.get("description", ""),
                "cluster_id": r.get("pseudocode_hash") or "",
                "verified_via": cached.get("verified_via", ""),
                "inputs": cached["inputs"],
                "output": cached["output"],
                # Keep the original verified_at so cache-hit reruns
                # don't churn git diffs every time.
                "verified_at": cached.get("verified_at"),
            })
            method_counter[cached.get("verified_via", "?")] += 1
            hits += 1
        else:
            misses.append(r)

    print(f"Running scribe on {len(candidates):,} candidates "
          f"(cache hits: {hits:,}, to compile: {len(misses):,}, "
          f"parallel: {args.jobs} workers)")

    done = 0
    total = len(misses)
    last_print = 0

    if misses:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futs = {pool.submit(_verify_one, r): r["intrinsic"] for r in misses}
            for fut in as_completed(futs):
                res = fut.result()
                done += 1
                if res["kind"] == "fail":
                    cat = categorize_error(res["error_type"], res["error_msg"])
                    fail_counter[cat] += 1
                    fail_examples.setdefault(cat, res["name"])
                    if args.show_fail:
                        print(f"  FAIL  {res['name']:36s}  {cat}")
                else:
                    method_counter[res["method"]] += 1
                    successes.append(res["record"])
                    if args.show_pass:
                        print(f"  OK    {res['record']['intrinsic']:36s}  "
                              f"via {res['method']:7s}  -> {res['out_hex']}")
                if done - last_print >= 200 or done == total:
                    last_print = done
                    print(f"  progress: {done:,}/{total:,} "
                          f"({100.0 * done / max(total, 1):.0f}%)")

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
                "verified_at": ex.get("verified_at") or today,
            })
        # Every candidate we examined this run gets its cache rows
        # rewritten to match current inputs; stale (different-hash) rows
        # for the same intrinsic are evicted.
        replace = {r["intrinsic"] for r in candidates}
        added, updated = upsert_cache(
            Path(args.cache_out), cache_entries, replace_intrinsics=replace,
        )
        print()
        print(f"Cache {args.cache_out}: +{added} new, ~{updated} updated, "
              f"total entries written = {added + updated}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
