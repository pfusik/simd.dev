# Ideas — deferred work

Backlog of improvements that aren't worth doing yet but are worth not
forgetting. Each entry should explain *why* before *what*.

## Per-arch sharded data files

**Why:** today the library lazy-fetches one ~9 MB JSON (~450 KB gzipped)
the first time a tooltip opens. That's fine for AArch+x86 mixed pages, but
wasteful when a page only ever talks about one ISA family — an aarch64
walkthrough never needs the AVX-512 records and vice versa.

**Sketch:**
- Build emits `simd-tooltip/dist/simd-data-{aarch64,aarch32,armv8m,x86_64}.json`
  alongside (or instead of) the unified `simd-data.json`.
- `simd-names.json` records which shard each name lives in
  (`{name: {shard: "x86_64"}}` or just `{name: "x86_64"}`).
- Library fetches only the shard for the first-hovered intrinsic, then
  any additional shards on demand.
- Optional consumer hint: `<script ... data-archs="aarch64">` to
  pre-fetch a known shard on init.

**Caveats:**
- A handful of names have no arch (very few), or the entry merge across
  Intel dual-CPUID rows can broaden `arch` — handle in the build script.
- Per-shard payloads also gzip well; the saving over the unified file
  is realistically 50-80% per page rather than 4×.

## Alias resolution UX

**Why:** the `ambiguous` map captures cases like `svadd_z` (overloaded
short form) → 22 typed canonicals. Today the tooltip lists the first
6 names. Better UX would let the reader pick a type from the tooltip,
or auto-pick using nearby context (e.g., look at adjacent text for a
type hint such as `s32`/`f64`).

## Per-page subset bundling at build time

**Why:** for static sites that *do* have a build step, embedding only
the intrinsics actually used on the page would shrink the payload to
a few KB. Provide an opt-in build helper (Pandoc/markdown-it/Eleventy
plugin) that scans rendered HTML, resolves names against the DB, and
inlines just the records needed as `<script type="application/json">`.

## VS Code extension

**Why:** the same library plus a webview-aware shim could power a
preview pane in VS Code. Same data, different presentation surface.

## Intel arch refinement

**Why:** today every Intel record is tagged `x86_64`. Some intrinsics
(those returning or accepting `__int64`/`__m64`) are 64-bit-only or
32-bit-only. Walk parameter types in `build_db.py` and split into
`x86`, `x86_64`, or both.

## SIMD support for RISC-V vector and WebAssembly SIMD

**Why:** the project README flags this as an open question. Both have
official intrinsic lists (RISC-V vector intrinsics in the rvv-intrinsic-doc
repo; WASM SIMD in the wasm-simd-128 spec). Coverage would broaden the
audience.

## Refresh detection

**Why:** ARM/Intel/LLVM all update their sources periodically. A weekly
cron that runs `scripts/build_all.sh --refresh` and compares stats.json
would surface coverage regressions or upstream removals before users
hit them.

## Search / lookup endpoint

**Why:** a `Cmd-K`-style search overlay for keyboard users. The names
index is already small enough to ship to every page; with a fuzzy match
(e.g., "addmask" → `_mm512_mask_add_pd`) this becomes the keyboard-first
counterpart to hover detection.
