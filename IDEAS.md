# Ideas — backlog

Tracking ideas across three states:

- **[Considered](#considered)** — on the backlog, worth doing
- **[Done](#done)** — shipped; kept here so the rationale lives alongside
  the other entries
- **[Rejected](#rejected)** — looked at, decided not to pursue

Each entry should explain *why* before *what*.

## Contents

**[Considered](#considered)**
- [Plain-formula explanation per intrinsic (LLM with verifier)](#plain-formula-explanation-per-intrinsic-llm-with-verifier)
- [Verifier: compile-and-run example code](#verifier-compile-and-run-example-code)
- [LLM-as-judge for prose explanations](#llm-as-judge-for-prose-explanations)
- [Cross-arch mapping table (Intel ↔ NEON via simde + sse2neon)](#cross-arch-mapping-table-intel--neon-via-simde--sse2neon)
- [Per-intrinsic static pages with worked examples](#per-intrinsic-static-pages-with-worked-examples)
- [Live editable examples per intrinsic (CE iframe)](#live-editable-examples-per-intrinsic-ce-iframe)
- ["When would I use this?" prose (LLM, second pass)](#when-would-i-use-this-prose-llm-second-pass)
- [Per-arch sharded data files](#per-arch-sharded-data-files)
- [Alias resolution UX](#alias-resolution-ux)
- [Markdown / static-site integrations (build-time subset bundling)](#markdown--static-site-integrations-build-time-subset-bundling)
- [VS Code extension — v1 polish (post-v0)](#vs-code-extension--v1-polish-post-v0)
- [Other editor plugins (CLion, Zed, Neovim, Emacs, …)](#other-editor-plugins-clion-zed-neovim-emacs-)
- [Intel arch refinement](#intel-arch-refinement)
- [SIMD support for RISC-V vector and WebAssembly SIMD](#simd-support-for-risc-v-vector-and-webassembly-simd)
- [Refresh detection](#refresh-detection)
- [Search / lookup endpoint](#search--lookup-endpoint)

**[Done](#done)**
- [Variant detection via pseudocode hash](#variant-detection-via-pseudocode-hash)
- [VS Code extension (v0)](#vs-code-extension-v0)
- [Compiler Explorer URL per intrinsic](#compiler-explorer-url-per-intrinsic)

**[Rejected](#rejected)** — *nothing yet*

# Considered

## Plain-formula explanation per intrinsic (LLM with verifier)

**Why:** upstream pseudocode is uneven — Intel's `<operation>` is mostly
readable, ARM ASL (`Elem[]`, `bits()`, `for e = 0 to elements-1`) requires
familiarity. For a tooltip audience the right form is a one-liner like
`result[i] = (idx[i] < 16) ? t[idx[i]] : 0` plus a short worked example.
Hand-writing 22k of these doesn't scale; pure-script transformers cover
~50% of the catalog before the long tail of edge cases (immediates,
predication, FP rounding, SVE scalable lengths, AES/SHA fixed transforms).

**Sketch:**
- **Cluster first, prompt once per cluster.** Most of the 22k catalog is
  type/width variants of the same operation. Group records by *normalized
  upstream pseudocode* (not by name — names lie: `_mm_add` vs `_mm_adds`
  vs `_mm_hadd` vs `_mm_mask_add` are very different operations under
  similar prefixes). ARM's ASL is already type-generic so plain-hash
  clustering gives huge groups out of the box (`vadd_s8/s16/s32/s64/u8/...`
  all share one pseudocode); Intel's needs normalization (replace lane
  widths and counts with placeholders before hashing) to cluster across
  widths. After normalization, expect **~3,000-5,000 unique clusters
  from 22,111 records** — 4-7× LLM cost reduction and stronger internal
  consistency (every member of a cluster lands on the same template).
  Phase 1 of this is already shipped — see "Variant detection via
  pseudocode hash" in the Done section.
- Build prompt context per cluster: representative signature + description
  + upstream pseudocode + the list of cluster members (so the LLM knows
  the variation axes — lane width, signed/unsigned, masked/unmasked).
- LLM produces `{formula_template, plain_english_template,
  example_inputs_per_member, example_outputs_per_member}`. Templates have
  placeholders (`{lanes}`, `{type}`, `{bits}`) filled at render time.
- Pinned model + temperature 0; cache outputs by content-hash
  (`data/llm-cache/<hash>.json`).
- Commit the cached outputs to the repo so rebuild is reproducible *up to*
  cache lookup; cache miss falls through to a re-prompt only when source
  pseudocode actually changed.
- See `data/probe_examples.md` for a hand-rendered preview of the target
  shape on 17 intrinsics across the complexity spectrum.

**Caveats:**
- ~$15-25 in tokens for a frontier-model batch *with clustering* (vs.
  ~$50-100 without). Bigger savings for ARM where clusters tend to be
  larger.
- Hallucination risk on the long tail. Mitigated by the verifier (next).
- Within-cluster outliers exist (saturating arithmetic, FP rounding modes,
  predicated forms). Cluster members where the pseudocode normalizer
  can't justify the merge should fall out of the cluster. Manual review
  of a sample of cluster representatives catches the rest.

## Verifier: compile-and-run example code

**Why:** the LLM pass above is only as trustworthy as its outputs. The
authoritative ground truth for "what does this intrinsic produce on
input X?" is the actual compiled instruction. Local clang / gcc, with
clang's `-target` for cross-compile, is the simplest path: no network
round-trip, no rate limits, no third-party service in the critical
build path, **and no emulator** for the cases clang's constant folder
can handle (which is most of them). **Cache results in the repo** so
subsequent rebuilds (and contributors who don't have the cross-toolchain)
reuse the verified bytes offline.

**Why this beats writing an interpreter for the upstream DSL** (the
original sketch in this slot):
- **Zero DSL parsing.** No need to write Intel `<operation>` or ARM
  ASL interpreters with their long tail of helpers, predication,
  rounding, FP exceptions, etc.
- **Hardware is the source of truth.** Catches LLM errors that are
  "consistent with the pseudocode" but wrong about real semantics
  (saturation boundaries, NaN propagation, rounding edge cases). An
  interpreter only catches "didn't follow the pseudocode."
- **100% coverage.** Any intrinsic with a callable signature is
  verifiable — including SVE / SVE2 / SME, AES / SHA, AMX, bf16 /
  fp16.

**Pipeline (primary path — constant-folding):**
- LLM emits per cluster member
  `{example_inputs, example_outputs_claimed}` (already needed for the
  worked-example display).
- Codegen a tiny C++ program where the result is a `const`-initialized
  global so clang folds the call into `.rodata` at compile time —
  generated and thrown away, never committed:
  ```c++
  #include <arm_neon.h>
  alignas(16) const uint8_t RESULT[16] = []{
      const int8x16_t a = { 1, -2, 3, ... };
      const int8x16_t b = { ... };
      uint8_t buf[16];
      vst1q_u8(buf, vreinterpretq_u8_s8(vqaddq_s8(a, b)));
      // returned via std::to_array equivalent
      return *reinterpret_cast<const std::array<uint8_t,16>*>(buf);
  }();
  ```
  (Exact form depends on whether the intrinsic itself can appear in a
  constexpr context; in practice a non-constexpr `const` global with
  `-O2` is enough — clang folds the result into `.rodata` regardless.)
- Cross-compile with clang's `-target <triple>` + the same `-march` we
  already emit for the in-tooltip CE links (flags already calibrated
  by the Compiler Explorer URL work — see Done). **No emulator
  needed**: cross-compiling x86 from aarch64 (or vice versa) only
  needs a clang frontend — the bytes drop out of the asm, no execution.
- Disassemble (`llvm-objdump -s` or asm output), parse `.rodata` for
  the `RESULT:` label, read the bytes that follow, byte-compare to the
  LLM's claim. Mismatch → re-prompt, downgrade to
  upstream-pseudocode-only display, or flag for manual review.

**Fallback path — actual execution:** for the non-foldable tail (loads
from non-const memory, `_rdrand` / `_rdtsc` / `_xgetbv`, AMX tile ops,
a chunk of predicated SVE):
- Native execution where the host arch matches.
- [Intel SDE](https://www.intel.com/content/www/us/en/developer/articles/tool/software-development-emulator.html)
  for AVX-512 / AMX on a non-supporting x86 host.
- QEMU for SVE / SVE2 / SME from a non-aarch64 host.
Reserved for the cases where folding fails — most of the catalog
should never hit this.
- **Cache committed to the repo** so rebuilds are offline and
  contributors / CI don't need the cross-toolchain. Refetch only on
  cache miss (new LLM output, new toolchain version, or new example
  inputs). Layout and schema below.

**Cache layout — auto vs. hand-curated:**

```
data/verifier-cache/<family>.jsonl       # auto-generated, regen rewrites freely
data/verifier-overrides/<family>.jsonl   # hand-curated, never written by automation
```

One JSONL shard per arch family (`neon`, `sve`, `sve2`, `sme`, `sse2`,
`avx2`, `avx512f`, `amx`, …) — mirrors the existing `family` field on
each record. Lines are sorted by `(intrinsic, inputs_hash)` for stable
diffs; ~3k cluster reps × ~3 examples → ~9k lines spread over ~25
shards, ~50–500 KB per shard.

**Two-directory split is filesystem-level safety:** the regen script
literally never opens `verifier-overrides/`, so a buggy refresh can't
clobber human edits. Empty override shards are fine — most stay empty.
Reviewers get the full hand-edit history with `git log -- data/verifier-overrides/`.

**Per-line schema (JSONL):**

- `intrinsic` *(both)* — canonical intrinsic name (e.g. `vqaddq_s8`).
  Primary lookup field.
- `inputs_hash` *(both)* — short SHA1 prefix (8 hex) over the
  canonicalized `inputs`. Co-key with `intrinsic`.
- `inputs` *(both)* — list referencing the input palette by name
  (`["BOUNDARIES","IDENTITY"]`); fallback `{"raw":"<hex>"}` entries
  for one-off bytes that don't fit a palette pattern.
- `output_bytes` *(both)* — list of hex strings, one per output vector
  (most intrinsics return a single vector, but `vld2q_*` etc. return
  multiple). Verified ground truth.
- `compiler_id` *(cache only)* — godbolt-style compiler ID used to
  verify (e.g. `armv8-full-cclang-trunk`). Cache-invalidation field.
- `march` *(cache only)* — march flags string (e.g.
  `+fp16+bf16+i8mm+dotprod+crypto`). Cache-invalidation field.
- `verified_at` *(cache only)* — ISO date of verification. Telemetry /
  staleness signal.
- `note` *(override only, required)* — why this override exists. Even
  one sentence is enough; future-you will want to know what made the
  auto-generated answer wrong.
- `added_by`, `added_at` *(override only, optional)* — who hand-curated
  and when.

**Lookup semantics:**

- Cache lookup key: `(intrinsic, inputs_hash, compiler_id, march)` —
  exact match; invalidates on any toolchain change.
- Override lookup key: `(intrinsic, inputs_hash)` — broader; ignores
  compiler/march so a hand-curated truth survives toolchain upgrades
  without re-curation.
- Build-time precedence: override wins. Cache miss + no override → run
  the verifier, write the result back to the cache shard.

**Examples** (one line each in the actual file):

```jsonc
// data/verifier-cache/neon.jsonl
{"intrinsic":"vqaddq_s8","inputs_hash":"bd9f12a6","inputs":["BOUNDARIES","IDENTITY"],"output_bytes":["7f7f7f7f80808080..."],"compiler_id":"armv8-full-cclang-trunk","march":"+fp16+bf16+i8mm+dotprod+crypto","verified_at":"2026-05-03"}

// data/verifier-overrides/neon.jsonl
{"intrinsic":"vrndnq_f32","inputs_hash":"a4421f88","inputs":["FP_BOUNDARIES"],"output_bytes":["00000000..."],"note":"LLM kept claiming round-half-up; ARMARM specifies round-to-nearest-even. Hand-verified.","added_by":"marcin","added_at":"2026-05-03"}
```

**Compiler Explorer as backup:** for any architecture / extension
awkward to cover locally (e.g. a contributor on x86 without QEMU
verifying SVE2; SME on a box without M4 hardware), fall back to the
CE compile API with the same harness and the same cache shape. Polite
usage: per-contributor cache misses, not bulk refresh runs. Bulk
refresh stays on whoever has the local rig set up.

**Caveats:**
- **Compile-time-constant immediates** must be substituted at codegen
  (already done for the godbolt URL helper).
- **Predicated SVE / SVE2:** harness has to set up a governing
  predicate. Either drive it from `example_inputs` or default to
  all-true.
- **AMX:** requires SDE; tile config setup is non-trivial. ~30
  intrinsics, manageable as a special case.
- **FP rounding / FE flags:** the harness pins `MXCSR` / `FPCR` to a
  known mode so behavior is deterministic.
- **Cluster-rep only.** Verify ~3k representatives, not all 22k
  records; cluster siblings inherit via type-substitution from the
  rep. With the cache committed, steady-state rebuilds touch ~zero
  programs — only on LLM-output churn.

## LLM-as-judge for prose explanations

**Why:** the byte-level verifier above proves the *numerical* output of
each intrinsic. It cannot catch hallucinations in the *prose* — claiming
"saturating" when the op is wrap-around, mis-stating the rounding mode,
inventing semantic edge cases that don't exist, etc. Wrong bytes are
easy to spot; wrong-but-confident English is much harder, and readers
tend to trust it. A second LLM pass acting as a judge catches the
prose-level mistakes the verifier can't.

**Sketch:**
- After the formula-pass LLM emits prose for a cluster, run a critique
  pass with a *different model family* (e.g. if Claude produces,
  Gemini judges; or vice versa). Same-family judging is much more
  correlated with same-family producing — the cross-family pairing is
  the cheap insurance.
- Judge prompt context: signature + upstream pseudocode + verified
  byte-level worked example (from the verifier above) + the prose
  under review.
- Judge scores on three axes:
  - **Accuracy** — does the prose contradict the pseudocode or the
    verified bytes?
  - **Completeness** — does it mention saturation / rounding /
    predication / out-of-range behavior when those apply?
  - **Clarity** — is the one-liner formula present, correct, and
    readable?
- Below threshold → re-prompt the producer with the judge's specific
  objections; or downgrade to upstream-pseudocode-only display; or
  flag for manual review.
- Score + reasoning logged alongside the prose in the LLM cache so
  reviewers can sample and tune.

**Caveats:**
- Judges are not infallible — manual spot-check 1-3% of judge-approved
  entries.
- Cost ~equal to the producer pass; smaller / cheaper judge models
  (Haiku-tier, Gemini Flash) are usually enough for scoring.
- Judges over-flag stylistic preferences if the prompt isn't tight.
  Lock the judge prompt to objective criteria (contradicts /
  omits / unclear), not "is this well-written?"

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
plain-formula, plain-English, worked example, Compiler Explorer link
(✅ already shipped — see Done below), related-intrinsics graph,
cross-arch equivalents, performance notes.

**Sketch:**
- `scripts/build_pages.py` emits `pages/<intrinsic>.html` (or `.md`) per
  record from the unified DB. ~3 days once the upstream pieces are in.
- Worked-example numbers come from the verifier cache (see
  "Verifier: compile-and-run example code" above), not from a
  separate execution pass — same data, two display surfaces.

## Live editable examples per intrinsic (CE iframe)

**Why:** once the verifier above is producing trusted worked examples
per cluster, embedding them as an editable Compiler Explorer iframe on
each per-intrinsic page is a near-free UX win. The reader can change
inputs, swap compilers, see how the asm shifts, all without leaving
the page. Turns each per-intrinsic page from a static doc into a live
playground. The same `compilerExplorerUrl` helper that already builds
the in-tooltip "open in CE" links produces the iframe state — no new
URL plumbing.

**Sketch:**
- godbolt iframe URL form (`https://godbolt.org/e?...`, state in the
  fragment, identical encoding to the `/z/...` short links we already
  emit) is explicitly designed for embedding. Default to the verified
  worked example as the iframe's initial code.
- Lazy-mount the iframe on user click ("show editable example") rather
  than auto-loading on page open — each iframe is a real page weight.
- Keep the existing static "open in CE →" link for the tooltip and VS
  Code surfaces; iframes inside a tooltip are too heavy.

**Caveats:**
- godbolt courtesy: each iframe load is one CE compile request. The
  traffic is naturally minimal (one per user click on "show editable
  example", lazy-mounted), but ask
  [Matt Godbolt](https://github.com/mattgodbolt) for the OK before
  flipping it on. Self-host the compiler-explorer container if volume
  ever justifies it.
- Privacy / CSP: iframes embed third-party JS; the consent banner /
  CSP policy needs to allow `frame-src https://godbolt.org`.

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

## VS Code extension — v1 polish (post-v0)

**Why:** the v0 extension shipped with hover-only support (see Done
below). Stretch features that didn't make v0:

- **"Show full info" command + webview panel.** Opens a side-by-side
  pane that loads `simd-tooltips.js` and renders the rich card layout
  (with collapsible pseudocode, etc.) for the symbol under cursor.
  Effectively the per-intrinsic page from the static-pages idea, but
  in-IDE. No more being constrained by VS Code's Markdown-only hover
  panel.
- **Marketplace publish.** One-time friction (publisher account,
  signing) but can be automated thereafter via a GitHub Action that
  triggers when `simd-tooltip/dist/` changes.
- **Dedupe with clangd.** v0 stacks happily — both providers' content
  appears together. For names where clangd has rich Doxygen (most of
  AVX2 / SSE / FMA) the user sees redundant content. Detect-and-skip
  in the hover provider would clean that up.

## Other editor plugins (CLion, Zed, Neovim, Emacs, …)

**Why:** the v0 extension covers VS Code, but a lot of the SIMD audience
lives in other editors. Each has its own plugin format but the same
underlying data file (`simd-data.json`) and the same hover-card markdown
that `simd-vscode/extension.js` already emits — so each port is mostly
glue, not new logic.

**Candidates (by likely audience size for SIMD-heavy C/C++):**

- **[CLion](https://www.jetbrains.com/clion/) / IntelliJ Platform** —
  JetBrains plugin (Kotlin/Java), `DocumentationProvider` API. Largest
  paid-IDE C/C++ audience; same plugin would also work in IntelliJ IDEA
  Ultimate, Rider, Android Studio.
- **[Zed](https://zed.dev/)** — extensions are WASM (Rust → wasm32-wasi)
  with a small surface; hover hooks land via the LSP integration.
  Growing C/C++ user base, AOT-fast.
- **Neovim / Vim** — Lua plugin hooking `vim.lsp.handlers["textDocument/hover"]`
  or a standalone hover provider. Big C/C++ kernel/systems audience.
- **Emacs** — `eglot` / `lsp-mode` advice, or a standalone minor mode.
  Smaller but vocal SIMD audience (numerical / scientific computing).
- **Sublime Text / Helix** — LSP-based; lower priority but cheap once a
  language-server form exists.
- **Xcode** — Apple-silicon NEON/SVE2 audience writing in Xcode. Source
  Editor extensions are limited (no hover API), so this likely needs a
  different surface (quick-help docset or a sidebar webview).

**Shared substrate:** the cheapest path is a tiny **language server**
that wraps the existing data file and emits the same Markdown card. Then
every editor with LSP gets it free; native plugins remain optional for
editors where LSP UX is worse than native (CLion, Xcode).

**Caveats:**
- Each marketplace has its own publishing friction (JetBrains
  Marketplace review, Zed extension registry, MELPA for Emacs, etc.).
  Maintenance cost scales with the number of native ports.
- Don't write N copies of the markdown renderer. Either share via an
  LSP, or extract the card-rendering logic from `simd-vscode/extension.js`
  into a `simd-card/` package that all ports import.

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

# Done

## Variant detection via pseudocode hash

**Why:** the catalog is full of type/width siblings of the same operation
(`vadd_s8`, `vadd_s16`, … 36 in one cluster; `_mm_add_epi32` and
`_mm_add_ps` share an identical bitwise pseudocode; etc.). Showing
those siblings inline in the tooltip lets a reader pivot from "the i32
add" to "the f32 add" without losing context, and is a stepping stone
toward the LLM-cluster idea above (the same hash key tells the LLM
pass which intrinsics share an explanation template).

**Shipped:** every record now gets a `cluster` id derived from a SHA1
prefix of its upstream pseudocode (after stripping placeholders like
ARM's "No operation information."). Records that share a hash share a
cluster. simd-data.json carries a `clusters` map of `cluster_id →
[member names]`. Numbers from the current build:

- 22,111 records → **2,563 clusters covering 11,562 records** (avg ~5
  members per cluster, p90 ~8, max 795 → 664 → 144).
- The web tooltip, the simd.dev result card, and the simd-vscode hover
  each render a "variants (N): …" section under the pseudocode block,
  capped to 8 visible names with a "+N more" indicator. On the simd.dev
  card the variant chips are clickable and load the sibling's card.
- Wire-size impact: +45 KB gzipped on `simd-data.json` (including the
  cluster map + per-record cluster id).

**Known limits (future work, see "Plain-formula explanation per
intrinsic" in Considered):**
- ARM ASL is type-generic so its clusters are large and clean. Intel's
  `<operation>` mentions specific bit widths, so cross-width Intel
  clustering would require a normalization pass (replace `8/16/32/64`
  with placeholders before hashing). Not done; clusters are smaller for
  Intel as a result.
- Some clusters group operations that *look* the same at the bit level
  but differ in type interpretation (e.g. `_mm_add_epi32` and
  `_mm_add_ps` share an identical bitwise pseudocode). Acceptable for
  v0 — the user still sees the relationship.

## VS Code extension (v0)

**Why:** clangd covers ~15% of Intel intrinsics with rich Doxygen
(mostly SSE / AVX / FMA) and signature-only for AVX-512 and almost all
of ARM NEON / SVE / SME — so a `HoverProvider` backed by our DB fills a
real gap, especially for the ~5,000 AVX-512 and ~14,000 ARM intrinsics
where clangd shows just the signature.

**Shipped:** [`simd-vscode/`](simd-vscode/README.md) — sibling to
`simd-tooltip/` and `simd-annotate/`. Pure JavaScript, no TypeScript
build step. Activates on `c` / `cpp` / `objective-c` / `objective-cpp`,
returns a Markdown card with signature, family/arch badges, description,
optional pseudocode, links to upstream docs and Compiler Explorer, plus
a small attribution footer.

**Implementation notes:**
- ~150 lines of JS, no `node_modules` at runtime.
- Lazy-loads `simd-data.json` on first hover so VS Code startup is
  unaffected.
- Cheap pre-filter (skip identifiers without `_` and shorter than 4
  chars) avoids hitting the records map for ordinary variable names.
- Stacks happily with clangd: both providers' content appears in the
  hover panel.
- Settings: `simdVscode.pseudocode = expanded | off`,
  `simdVscode.languages` (defaults to C/C++/ObjC).
- Manual install only for now: `vsce package` →
  `code --install-extension simd-vscode/simd-vscode-0.0.1.vsix`.
- Marketplace publish + webview side-panel are tracked above under
  "VS Code extension — v1 polish".

## Compiler Explorer URL per intrinsic

**Why:** "what asm does this compile to?" is the natural follow-up
question after "what does this do?". Compiler Explorer (godbolt.org) is
the universal tool for that — it supports prefilled URLs that load with
a ready-to-run example.

**Shipped:** every intrinsic record now generates a godbolt clientstate
URL on demand. The web tooltip, the simd.dev landing-page result card,
and the simd-vscode hover all surface a "Compiler Explorer →" link in
the footer. Click → godbolt opens with a tiny `example()` function that
calls the intrinsic, the right include headers (`arm_neon.h` +
`arm_fp16.h` + `arm_bf16.h` for NEON, `arm_sve.h` + `arm_neon_sve_bridge.h`
for SVE, etc.), and a march that has the relevant optional extensions
enabled (`+fp16+bf16+i8mm+dotprod+crypto`).

**Implementation notes:**
- Pure function of existing record fields (signature, ISA family,
  source). No new bytes added to the data file.
- Per-arch godbolt compiler IDs: `cclang_trunk` for x86,
  `armv8-full-cclang-trunk` for aarch64, `armv7-cclang-trunk` for
  Helium / aarch32. Calibrated against the godbolt API.
- ARM family priority is least-specific first (Neon → Helium → SVE
  → SVE2 → SME) so an intrinsic available in both SVE and SME context
  compiles with the lighter SVE march.
- Intel CPUID flags are unioned (`AVX512F + AVX512VL` →
  `-mavx512f -mavx512vl`).
- `const int` parameters get a literal `0` substituted because they
  must be compile-time constants.
- Helper lives in `simd-tooltips.js` and is exposed via
  `SimdTooltips.compilerExplorerUrl(rec)`. Duplicated in
  `simd-vscode/extension.js` since it can't load the web library at
  runtime — ~80 lines of duplication; fine for now.

# Rejected

*Nothing yet — but kept here so future "considered and dropped" items
have a home.*
