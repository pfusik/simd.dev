# simd.dev

A collection of data and tools related to SIMD intrinsics.

Contains:

- **[data](data/)** unified intrinsic database in [`data/intrinsics.jsonl`](data/intrinsics.jsonl) —
21,484 records spanning ARM (NEON / Helium / SVE / SVE2 / SME) and Intel (MMX / SSE* / AVX /
AVX2 / AVX-512 / AMX / etc.).  See [`data/README.md`](data/README.md)
for schema, provenance, and rebuild instructions.
**Generated automatically from upstream sources** (ARM ACLE, Intel Intrinsics Guide XML,
LLVM clang headers); pure stdlib Python, no LLM in the loop.

- **[simd-tooltip](simd-tooltip/) - drop-in JS tooltip library in
— one `<script>` tag adds
hover (or click, or hover-with-`?`-badge) tooltips to any HTML page,
detecting intrinsic names in text without touching the DOM by default.
See [`simd-tooltip/README.md`](simd-tooltip/README.md) for install
instructions and [`simd-tooltip/examples/demo.html`](simd-tooltip/examples/demo.html) for a
working page.

- a CLI tool [`simd-annotate/`](simd-annotate/README.md) that
takes an arbitrary HTML file and emits a self-contained, **100%
offline** HTML page with the library plus only the slice of the
intrinsic database the page actually references embedded inline. Useful
for archived posts, e-book exports, intranet docs.

## Why?

I like to play with SIMD code but I tend to forget things quickly.
So having immediate access to an explanation of what an intrinsic does
is very helpful.

I looked around, but couldn't find either a library like this or a
well-defined open database. So, with the help of Claude, I built it on one
Sunday afternoon.

One motivation was the tooltips in Compiler Explorer — but those are only for
the generated assembly, not for the C/C++ source side.

Note: I also found simd.info — it's a great resource with much deeper info,
but it's not open and doesn't provide the tooling I wanted. The depth there
(especially in the paid version, from the screenshots) is impressive; if your
goal is reference-grade reading rather than embedding tooltips in your own
docs, that's a better fit.

Related projects:
- **[Compiler Explorer](https://godbolt.org/) (godbolt)** has rich hover tooltips
  on the *assembly* side (its [`asm-docs/`](https://github.com/compiler-explorer/compiler-explorer/tree/main/lib/asm-docs)
  directory ships ~2 MB of curated entries for x86 / aarch64 / RISC-V / etc.).
  On the C/C++ source side it has none of its own — and it's a full IDE-style
  app, not a static doc renderer in any case.
- **clangd** (LSP) shows hover info in your IDE — editor-only, not exportable
  to HTML. Coverage is uneven: rich Doxygen for AVX/AVX2/FMA and most SSE
  (~15% of the Intel catalog), but only the bare signature for AVX-512 and
  for nearly all of ARM NEON / SVE / SME (the auto-generated ARM headers
  ship without prose).
- **[Intel's Intrinsics Guide](https://www.intel.com/content/www/us/en/docs/intrinsics-guide/index.html)**
  — the reference everyone uses for browsing, with rich descriptions and
  pseudocode. JavaScript app though; you can't embed its tooltips on
  third-party content. Our DB pulls signatures, ISA flags, and pseudocode
  from the same XML it's built on.
- **simd.info** — seems to be a very rich and deep database with a different
  focus (reference reading rather than tooltip embedding). Not open.
- **[Highway](https://github.com/google/highway)** (Google's portable SIMD
  library) has writeups under [`g3doc/`](https://github.com/google/highway/tree/master/g3doc)
  that mention specific intrinsics inline as cross-references — useful, but
  hand-curated rather than driven by a database. Different scope (portable
  abstraction layer over Intel/NEON/RISC-V/WASM SIMD), but a kindred-spirit
  example.
- **[ARM Compute Library](https://github.com/ARM-software/ComputeLibrary)**
  has function-level / kernel-level docs but doesn't link individual
  NEON/SVE intrinsics — different abstraction layer, complementary rather
  than overlapping.
