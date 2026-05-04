#!/usr/bin/env python3
"""Render verified-examples JSON as a self-contained preview HTML page.

Usage:
    python3 simd-scribe/build_preview.py simd-scribe/preview-data.json \
        -o simd-scribe/preview.html
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>simd-scribe pilot — verified worked examples</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  :root {
    --bg: #0e1219;
    --panel: #161b25;
    --panel-2: #1d2330;
    --line: #2a3245;
    --ink: #d8dde7;
    --ink-dim: #8a93a3;
    --ink-fade: #5e6679;
    --accent: #7da3ff;
    --good: #5ec083;
    --warn: #d8a868;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--ink);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
          Roboto, sans-serif;
    padding: 24px 32px 48px;
    min-height: 100vh;
  }
  header { max-width: 1200px; margin: 0 auto 24px; }
  header h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; }
  header .sub { color: var(--ink-dim); font-size: 13px; }
  header .sub code {
    background: var(--panel-2);
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 12px;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
    gap: 14px;
    max-width: 1600px;
    margin: 0 auto;
  }
  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 14px 16px 12px;
  }
  .card-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }
  .card-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 15px;
    font-weight: 600;
    color: var(--accent);
  }
  .badge {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding: 2px 6px;
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 3px;
    color: var(--ink-dim);
  }
  .sig {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
    color: var(--ink-dim);
    margin-bottom: 8px;
    word-break: break-all;
  }
  .description {
    font-size: 12px;
    color: var(--ink-dim);
    margin-bottom: 10px;
  }
  .example {
    background: var(--panel-2);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 8px 10px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11.5px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .example .row { display: flex; gap: 6px; }
  .example .lbl {
    color: var(--ink-fade);
    flex: 0 0 auto;
    min-width: 32px;
  }
  .example .vals { color: var(--ink); }
  .example .out  { color: var(--good); }
  .example .hex  { color: var(--ink-fade); font-size: 10.5px; margin-top: 2px; }
  .example .arrow { color: var(--ink-fade); }
  .filter-box {
    max-width: 1200px;
    margin: 0 auto 16px;
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .filter-box input {
    flex: 1;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 4px;
    color: var(--ink);
    padding: 8px 10px;
    font: inherit;
  }
  .filter-box input:focus {
    outline: none;
    border-color: var(--accent);
  }
  .filter-box .count {
    color: var(--ink-dim);
    font-size: 12px;
  }
</style>
</head>
<body>
<header>
  <h1>simd-scribe — verified worked examples</h1>
  <div class="sub">
    Cluster <code id="cluster-id"></code> · <span id="member-count"></span> members ·
    inputs generated deterministically per type, outputs verified by clang
    constant-folding.
  </div>
</header>
<div class="filter-box">
  <input id="filter" type="text" placeholder="filter by name (e.g. vaddq, _s8)" />
  <span class="count" id="filter-count"></span>
</div>
<main class="grid" id="grid"></main>

<script id="preview-data" type="application/json">__DATA__</script>
<script>
(function () {
  const data = JSON.parse(document.getElementById("preview-data").textContent);
  document.getElementById("cluster-id").textContent = data.cluster_id;
  document.getElementById("member-count").textContent = data.members.length;

  const grid = document.getElementById("grid");
  const filterEl = document.getElementById("filter");
  const filterCount = document.getElementById("filter-count");

  function fmtArr(arr) {
    return "[" + arr.join(", ") + "]";
  }
  function fmtScalar(v) {
    return String(v);
  }
  function fmtVals(input) {
    return Array.isArray(input.values)
      ? (input.values.length === 1 ? fmtScalar(input.values[0]) : fmtArr(input.values))
      : fmtScalar(input.values);
  }
  function fmtHex(hex) {
    return hex.match(/.{1,2}/g).join(" ");
  }

  function renderCard(m) {
    const card = document.createElement("article");
    card.className = "card";
    card.dataset.name = m.intrinsic;

    const head = document.createElement("div");
    head.className = "card-head";
    const name = document.createElement("span");
    name.className = "card-name";
    name.textContent = m.intrinsic;
    head.appendChild(name);
    for (const fam of m.family || []) {
      const b = document.createElement("span"); b.className = "badge";
      b.textContent = fam; head.appendChild(b);
    }
    for (const a of m.arch || []) {
      const b = document.createElement("span"); b.className = "badge";
      b.textContent = a; head.appendChild(b);
    }
    card.appendChild(head);

    const sig = document.createElement("div");
    sig.className = "sig";
    sig.textContent = m.definition;
    card.appendChild(sig);

    if (m.description) {
      const desc = document.createElement("div");
      desc.className = "description";
      desc.textContent = m.description;
      card.appendChild(desc);
    }

    const ex = document.createElement("div");
    ex.className = "example";
    for (const input of m.inputs) {
      const row = document.createElement("div"); row.className = "row";
      const lbl = document.createElement("span"); lbl.className = "lbl";
      lbl.textContent = input.name + ":";
      const vals = document.createElement("span"); vals.className = "vals";
      vals.textContent = fmtVals(input);
      row.appendChild(lbl); row.appendChild(vals);
      ex.appendChild(row);
    }
    const outRow = document.createElement("div"); outRow.className = "row";
    const arrow = document.createElement("span"); arrow.className = "lbl arrow";
    arrow.textContent = "→";
    const outVals = document.createElement("span"); outVals.className = "vals out";
    outVals.textContent = fmtVals(m.output);
    outRow.appendChild(arrow); outRow.appendChild(outVals);
    ex.appendChild(outRow);

    const hexRow = document.createElement("div"); hexRow.className = "hex";
    hexRow.textContent = "bytes: " + fmtHex(m.output.bytes_hex);
    ex.appendChild(hexRow);

    card.appendChild(ex);
    return card;
  }

  const cards = data.members.map(renderCard);
  for (const c of cards) grid.appendChild(c);

  function applyFilter() {
    const q = filterEl.value.trim().toLowerCase();
    let visible = 0;
    for (const c of cards) {
      const match = !q || c.dataset.name.toLowerCase().includes(q);
      c.style.display = match ? "" : "none";
      if (match) visible++;
    }
    filterCount.textContent = q ? `${visible} / ${cards.length} match` : "";
  }
  filterEl.addEventListener("input", applyFilter);
})();
</script>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", help="path to verified-examples JSON")
    ap.add_argument("-o", "--output", required=True,
                    help="path to write the self-contained HTML preview")
    args = ap.parse_args()

    src = Path(args.data)
    payload = json.loads(src.read_text())
    # Embed inside <script type="application/json">. Neutralize `</`
    # so the script tag can't close prematurely.
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2)
        .replace("</", "<\\/")
    )
    html = HTML_TEMPLATE.replace("__DATA__", encoded)

    out = Path(args.output)
    out.write_text(html)
    print(f"Wrote {out}  ({len(html):,} bytes, {len(payload['members'])} cards)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
