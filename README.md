# simd.dev

A collection of data and tools related to SIMD intrinsics.

Contains:

- **[data](data/)** - A unified intrinsic database in [`data/intrinsics.jsonl`](data/intrinsics.jsonl) -
21,484 records spanning ARM (NEON / Helium / SVE / SVE2 / SME) and Intel (MMX / SSE* / AVX /
AVX2 / AVX-512 / AMX / etc.).  See [`data/README.md`](data/README.md)
for schema, provenance, and rebuild instructions.
**Generated automatically from upstream sources** (ARM ACLE, Intel Intrinsics Guide XML,
LLVM clang headers); pure stdlib Python, no LLM in the loop.

- **[simd-tooltip](simd-tooltip/)** - Drop-in JS tooltip library.
One `<script>` tag adds
hover (or click, or hover-with-`?`-badge) tooltips to any HTML page,
detecting intrinsic names in text without touching the DOM by default.
See [`simd-tooltip/README.md`](simd-tooltip/README.md) for install
instructions and [`simd-tooltip/examples/demo.html`](simd-tooltip/examples/demo.html) for a
working page.

- **[simd-annotate](simd-annotate/)** - A CLI that
takes an arbitrary HTML file and emits a self-contained, **100%
offline** HTML page with the library plus only the slice of the
intrinsic database the page actually references embedded inline. Useful
for archived posts, e-book exports, intranet docs.
See [an example with annotations](simd-annotate/examples/sample.annotated.html).

- **[simd-vscode](simd-vscode/)** - A VS Code extension that surfaces
the same data as a hover tooltip directly in your editor — covers ~22k
intrinsics + 208 SIMD types, including the AVX-512 and ARM NEON/SVE/SME
ranges where clangd typically shows only the bare signature. Manual
install via `vsce package` + `code --install-extension`. See
[`simd-vscode/README.md`](simd-vscode/README.md).

- **[simd.dev/](simd.dev/)** - Static landing page for the project's
domain. Describes the four pieces above and provides a small
intrinsic-search box that filters the names index live and renders the
full record on click. Self-contained after `sync.sh`; deployable to any
static host.

## Why?

I like to play with SIMD code but I tend to forget things quickly.
So having immediate access to an explanation of what an intrinsic does
is very helpful.

I looked around, but couldn't find either a library like this or a
well-defined open database. So, with the help of Claude, I built it on one
Sunday afternoon.

One motivation was the tooltips in Compiler Explorer — but those are only for
the generated assembly, not for the C/C++ source side.

Related projects:
- **[Compiler Explorer](https://godbolt.org/) (godbolt)** has rich hover tooltips
  on the *assembly* side (its [`asm-docs/`](https://github.com/compiler-explorer/compiler-explorer/tree/main/lib/asm-docs)
  directory ships ~2 MB of curated entries for x86 / aarch64 / RISC-V / etc.).
  On the C/C++ source side it has none of its own — and it's a full IDE-style
  app, not a static doc renderer in any case.
- **clangd** (LSP) shows hover info in your IDE — editor-only, not exportable
  to HTML, coverage is uneven.
- **[Intel's Intrinsics Guide](https://www.intel.com/content/www/us/en/docs/intrinsics-guide/index.html)**
  — the reference everyone uses for browsing, with rich descriptions and
  pseudocode. Our DB pulls signatures, ISA flags, and pseudocode
  from the same XML it's built on.
- **[simd.info](https://simd.info)** — seems to be a very rich and deep database with a different
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
