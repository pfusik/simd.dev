# Ideas — deferred work

Backlog of improvements that aren't worth doing yet but are worth not
forgetting. Each entry should explain *why* before *what*.

## Plain-formula explanation per intrinsic (LLM with verifier)

**Why:** upstream pseudocode is uneven — Intel's `<operation>` is mostly
readable, ARM ASL (`Elem[]`, `bits()`, `for e = 0 to elements-1`) requires
familiarity. For a tooltip audience the right form is a one-liner like
`result[i] = (idx[i] < 16) ? t[idx[i]] : 0` plus a short worked example.
Hand-writing 22k of these doesn't scale; pure-script transformers cover
~50% of the catalog before the long tail of edge cases (immediates,
predication, FP rounding, SVE scalable lengths, AES/SHA fixed transforms).

**Sketch:**
- Build prompt context per intrinsic: signature + description + upstream
  pseudocode (now in the cache via `cache/arm_operations.json`).
- LLM produces `{formula, plain_english, example_inputs, example_outputs}`.
- Pinned model + temperature 0; cache outputs by content-hash
  (`data/llm-cache/<hash>.json`).
- Commit the cached outputs to the repo so rebuild is reproducible *up to*
  cache lookup; cache miss falls through to a re-prompt only when source
  pseudocode actually changed.
- See `data/probe_examples.md` for a hand-rendered preview of the target
  shape on 17 intrinsics across the complexity spectrum.

**Caveats:**
- ~$50-100 in tokens for a frontier-model batch on the full catalog.
- Hallucination risk on the long tail. Mitigated by the verifier (next).

## Verifier interpreter for upstream pseudocode

**Why:** the LLM pass above is only as trustworthy as its outputs. A
small interpreter for Intel's `<operation>` DSL and the ARM ASL subset
that appears in `operations.json` can run the upstream pseudocode on the
LLM's claimed example inputs and confirm the LLM's claimed outputs
agree byte-exact.

**Sketch:**
- Intel DSL: `:=`, `[hi:lo]` bit slices, `FOR/IF/CASE`, `ZeroExtend*`,
  `SignExtend*`, `Convert_*_To_*`, plus a stable shortlist of helper
  functions (about 30-40 to cover all intrinsics). ~2 days to implement.
- ARM ASL subset: `Elem[]`, `bits(N)`, `for e = 0 to elements-1`,
  `UInt`, `SInt`, plus `FPRoundInt`/`Saturate`/etc. Limited to forms
  actually present in `operations.json`. ~4 days.
- Pipeline: LLM output → run upstream pseudocode in interpreter on
  `example_inputs` → compare to LLM `example_outputs`. Reject mismatch;
  re-prompt or fall back to upstream-only display.
- Verifier coverage gates which entries get the LLM treatment in the
  shipped DB; the rest fall back to upstream pseudocode verbatim.

## Cross-arch mapping table (Intel ↔ NEON via simde + sse2neon)

**Why:** "what's the NEON equivalent of `_mm_maddubs_epi16`?" is one of
the most-asked questions in any SIMD walkthrough. The mappings exist in
permissively-licensed source: simde implements every Intel intrinsic in
terms of NEON (or scalar fallback); sse2neon is single-header with a
`perf-tier.md` that ranks each mapping by efficiency (1:1 vs.
13-instruction emulation). Both MIT.

**Sketch:**
- `scripts/extract_simde.py` parses `simde/simde/x86/*.h`, builds
  `data/cross_arch.jsonl` rows: `{intel: "_mm_maddubs_epi16",
  neon_equivalent: ["vmovl_s8", "vmul_s16", "vaddl_s16"], complexity:
  "high"}`.
- Pull sse2neon's `perf-tier.md` for the complexity ranking.
- Library renders cross-arch hints in the tooltip and on per-intrinsic
  pages.

**Caveats:** simde is a header library — the mapping isn't a flat table,
it's the implementation graph. Extraction is real parsing work (~3 days),
not just a `grep`. Only Intel→NEON; the reverse direction has no equivalent
1-to-1 library.

## Per-intrinsic static pages with worked examples

**Why:** the tooltip is good for "what does this do?" — but for a full
walkthrough, readers want a dedicated page per intrinsic with: signature,
plain-formula, plain-English, worked example, Compiler Explorer link,
related-intrinsics graph, cross-arch equivalents, performance notes.

**Sketch:**
- `scripts/build_pages.py` emits `pages/<intrinsic>.html` (or `.md`) per
  record from the unified DB. ~3 days once the upstream pieces are in.
- Compiler Explorer URL: deterministic. Template a `#include` + tiny
  function that calls the intrinsic, encode-base64 into a CE
  `clientstate=` URL. ~1 day; ships for all 22k.
- Worked-example numbers from native execution: write a tiny C program
  per intrinsic with canonical inputs, compile, run (native or QEMU
  cross-arch), capture the output. Deterministic, ~1-2 weeks for full
  coverage; ~1 week if capped at the easy 70%.

## "When would I use this?" prose (LLM, second pass)

**Why:** the most-requested educational content isn't pseudocode, it's
"why does this exist? when would I reach for it instead of X?". Domain
reasoning that isn't in any upstream source.

**Sketch:** second LLM pass after the formula pass; same caching
discipline. Verification is harder (no ground-truth oracle for "is this
the right tool for this job?"); manual spot-check 1-5% of outputs.

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

## Markdown / static-site integrations (build-time subset bundling)

**Why:** the standalone form of this already exists as
[`simd-annotate/`](simd-annotate/README.md) — runs over a finished HTML
file, embeds the slice of the DB that file actually references. What's
still missing is *direct integration with the markdown / static-site
toolchains* people actually use, so the inlining happens inside the
normal build instead of as a separate post-processing pass.

**Candidate integrations (all using the same library + DB):**

- **[Pandoc](https://pandoc.org/)** + a Lua filter — single binary, the
  Lua filter scans code blocks (and prose) and either wraps tokens or
  emits a per-page subset. Most portable for academic / book authors.
- **[markdown-it](https://github.com/markdown-it/markdown-it)** plugin
  — same idea in Node, fits the JS ecosystem (Docusaurus, VuePress, etc.).
- **[Python-Markdown](https://python-markdown.github.io/)** treeprocessor
  — fits Pelican / mkdocs-material plugin authors.
- **[mdBook](https://github.com/rust-lang/mdBook)** preprocessor — Rust;
  no existing SIMD plugin, but the preprocessor API is small.
- **[remark](https://github.com/remarkjs/remark) / [rehype](https://github.com/rehypejs/rehype)**
  plugins — fits any unified.js pipeline (Astro, Next.js MDX, …).
- **[mkdocs](https://www.mkdocs.org/) hook** — for engineering-team docs sites.
- **[Eleventy](https://www.11ty.dev/)** plugin — for the static-site
  community already wiring custom Markdown pipelines.

## VS Code extension

**Why:** the database is the moat; an editor extension is the obvious
next presentation surface. clangd already covers ~15% of Intel intrinsics
with rich Doxygen (mostly SSE/AVX/FMA) and signature-only for AVX-512
and almost all of ARM NEON/SVE/SME — so a `HoverProvider` backed by our
DB fills a real gap, especially for the ~5,000 AVX-512 and ~14,000 ARM
intrinsics where clangd shows just the signature.

**Sketch:**
- `simd-vscode/` sibling to `simd-tooltip/` and `simd-annotate/`.
- `package.json` activation on `onLanguage:c` / `onLanguage:cpp`,
  optionally `markdown` for code fences in prose.
- `HoverProvider` returning a `MarkdownString`: signature in a fenced
  block, description, family/arch badges, optional pseudocode block,
  link to upstream docs.
- Vendor `simd-tooltip/dist/simd-data.json` directly (~9 MB raw / ~450 KB
  gzipped is fine for a VS Code extension package).
- VS Code stacks hover providers, so coexisting with clangd is free
  for v1 — readers see clangd's content followed by ours; we fill the
  gaps automatically.
- Settings: `simd-tooltips.pseudocode = expanded | off` (no "collapsed"
  -- VS Code hover panels can't do `<details>`).
- Stretch: a "Show full info" command opens a webview pane that
  embeds `simd-tooltips.js` directly and renders the rich tooltip
  layout for the symbol under cursor (effectively the per-intrinsic
  page from the static-pages idea, but in-IDE).

**Effort:** ~½ day for v0 (working hover, no settings); ~3 days for
a polished v1 with settings + webview + Marketplace publish. ~300-500
lines of TypeScript total. Marketplace publish has one-time friction
(publisher account, signing) but can be automated thereafter via a
GitHub Action triggered when `simd-tooltip/dist/` changes.

## Intel arch refinement

**Why:** today every Intel record is tagged `x86_64`. Some intrinsics
(those returning or accepting `__int64`/`__m64`) are 64-bit-only or
32-bit-only. Walk parameter types in `build_db.py` and split into
`x86`, `x86_64`, or both.

## SIMD support for RISC-V vector and WebAssembly SIMD

**Why:** the database covers ARM and x86 today. Both RVV and WASM-SIMD
have official, machine-readable intrinsic lists (RISC-V Vector intrinsics
in [riscv-non-isa/rvv-intrinsic-doc](https://github.com/riscv-non-isa/rvv-intrinsic-doc),
WebAssembly SIMD via the [wasm-simd-128 spec](https://github.com/WebAssembly/simd)
and clang's `wasm_simd128.h`). Adding them would broaden the audience to
embedded / RISC-V folks and to the substantial population of people doing
SIMD in JS via WASM.

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

