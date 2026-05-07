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
    Signature,
    build_inputs,
    compile_and_extract,
    decode_lanes,
    emit_source,
    parse_signature,
)


def compiler_id() -> str:
    """A short, stable identifier for the local toolchain."""
    from scribe import CLANG  # local import to avoid cycles at module load
    try:
        out = subprocess.run(
            [CLANG, "--version"], capture_output=True, text=True, check=True
        ).stdout
        first_line = out.splitlines()[0] if out else "clang"
        # e.g. "Homebrew clang version 22.1.4"
        return first_line.strip()
    except Exception:
        return "clang (unknown version)"


def inputs_hash(inputs: list[dict]) -> str:
    """SHA1[:8] over the canonical inputs payload — cache co-key with intrinsic."""
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def upsert_cache(
    cache_path: Path,
    new_entries: list[dict],
    replace_intrinsics: set[str] | None = None,
) -> tuple[int, int]:
    """Merge new_entries into cache_path keyed by (intrinsic, inputs_hash).

    Returns (added, updated). Existing entries with no key match are kept,
    *except* for intrinsics in `replace_intrinsics`: any pre-existing
    entry whose intrinsic is in that set and whose key isn't matched by
    `new_entries` gets evicted (used to drop stale rows when canonical
    inputs change, e.g. via hints.json).
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

    new_keys = {(e["intrinsic"], e["inputs_hash"]) for e in new_entries}
    if replace_intrinsics:
        existing = {
            k: v for k, v in existing.items()
            if k[0] not in replace_intrinsics or k in new_keys
        }

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
            inputs = build_inputs(sig)
            source = emit_source(sig, inputs)
            from scribe import effective_return  # local import
            ret_ti, _ = effective_return(sig)
            out_bytes, method = compile_and_extract(
                source, expected_bytes=ret_ti.total_bytes
            )
            out_lanes = decode_lanes(ret_ti, out_bytes)
        except Exception as e:
            failures.append(name)
            print(f"  {name:24s}  FAIL  {e!s}".replace("\n", "\n    "))
            continue

        in_repr = (
            "  ".join(f"{p.name}={vals}" for p, vals in zip(sig.params, inputs))
            if args.full_inputs else fmt_inputs(sig, inputs)
        )
        print(f"  {name:24s}  {in_repr}")
        print(f"    -> {out_bytes.hex()}  ({out_lanes})  via {method}")

        examples.append({
            "intrinsic": name,
            "definition": r["definition"],
            "family": r["family"],
            "arch": r["arch"],
            "description": r.get("description", ""),
            "verified_via": method,
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
                "verified_via": ex["verified_via"],
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
