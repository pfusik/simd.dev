# simd.dev

IMPORTANT: only use resources with permissible copyright.
DO NOT USE simd.info for anything.

Static-site renderer for SIMD-rich technical writing: takes Markdown
(or annotated source) and produces HTML where every SIMD intrinsic is
**hover-to-explain** with a link to the official ARM/Intel docs.

Goal: make educational walkthroughs of SIMD code (NEON, SSE, AVX2,
AVX-512, SVE2) self-contained — readers don't need to keep a
separate browser tab open to look up `vld1q_u16` or `_mm256_permutevar8x32_epi32`.

## Status

Step 1: unified intrinsic database in
[`data/intrinsics.jsonl`](data/intrinsics.jsonl) — 21,484 records spanning
ARM (NEON / Helium / SVE / SVE2 / SME) and Intel (MMX / SSE* / AVX /
AVX2 / AVX-512 / AMX / etc.).  See [`data/README.md`](data/README.md)
for schema, provenance, and rebuild instructions.

Step 2: drop-in JS tooltip library in
[`simd-tooltip/`](simd-tooltip/README.md) — one `<script>` tag adds
hover (or click, or hover-with-`?`-badge) tooltips to any HTML page,
detecting intrinsic names in text without touching the DOM by default.
See [`simd-tooltip/README.md`](simd-tooltip/README.md) for install
instructions and [`examples/demo.html`](examples/demo.html) for a
working page.

## Why this doesn't already exist

Surveyed the landscape (see [Existing landscape](#existing-landscape)
below) — there's no off-the-shelf tool that does this specifically.
The closest things are:

- **Compiler Explorer (godbolt)** shows tooltips on hover when
  you're inside its Monaco editor — but it's a full IDE-style app,
  not a static doc renderer.
- **clangd** (LSP) shows hover info in your IDE — editor-only, not
  exportable to HTML.
- **Intel's Intrinsics Guide** is itself a JS app with rich search
  and detail panels, but you can't embed its tooltips on third-party
  content.

It's not hard to build, just nobody has — yet.

## Existing landscape

### Intrinsic databases (the "what does it do" content)

- **ARM**: machine-readable JSON/XML of all NEON / SVE / SVE2
  intrinsics on the developer portal.  Each entry has signature,
  prose semantic description, behavior pseudocode, instruction
  mapping.
- **Intel**: ships an XML data file with their Intrinsics Guide
  ([download][intel-xml-data]).  Same shape — name, description,
  parameters, return, equivalent instruction, pseudocode.
- Each intrinsic has a stable canonical doc URL:
  - `https://developer.arm.com/architectures/instruction-sets/intrinsics/<name>`
  - `https://www.intel.com/content/www/us/en/docs/intrinsics-guide/index.html#text=<name>`

[intel-xml-data]: https://www.intel.com/content/www/us/en/docs/intrinsics-guide/index.html

### Tooltip / popover libraries (the hover UX)

- **Tippy.js** — most popular, ~10 KB, supports keyboard a11y,
  configurable.  Easy to attach to elements: `tippy('.intrinsic',
  { content: ... })`.
- **Popper.js** (Tippy's positioning engine) — lower-level if you
  want fully custom rendering.
- **Floating UI** — modern successor to Popper.

### Markdown → HTML with extensibility hooks

- **Pandoc** + a Lua filter — single binary, the Lua filter scans
  code blocks and wraps tokens.  Most portable.
- **markdown-it** (JS) + a custom plugin — same idea in Node.
- **Python-Markdown** + treeprocessor — same idea in Python.
- **mdBook** (Rust) — has a preprocessor API; no existing SIMD plugin.

## Architecture

Three pieces, ~300 lines total.

### 1. Build-time markdown processor (~80 lines)

For each fenced code block, scan tokens against a precompiled
intrinsic-name regex, replace matches with `<span class="intrinsic"
data-name="vld1q_u16">vld1q_u16</span>`.  Output HTML.

Regex patterns (forgiving):

- NEON: `v[a-zA-Z]+q?_[suf]?\d+(_[suf]\d+)?`
- x86:  `_mm\d*_[a-zA-Z0-9_]+`

Implementation choices, in order of preference:

1. **Pandoc + Lua filter** (single binary, portable).
2. **Python-Markdown + treeprocessor** (easy to extend).
3. **markdown-it + plugin** (Node ecosystem if site is JS-heavy).

### 2. Intrinsic database (~10 MB JSON)

Fetched once from ARM + Intel's official sources, normalized to
something like:

```json
{
  "vld1q_u16": {
    "isa": "neon",
    "signature": "uint16x8_t vld1q_u16(uint16_t const *ptr)",
    "brief": "Load 8 unsigned 16-bit elements from memory.",
    "description": "...",
    "pseudocode": "for i in 0..7: out[i] = mem[ptr + 2*i]",
    "instruction": "LD1 {Vt.8H}, [Xn]",
    "doc_url": "https://developer.arm.com/.../vld1q_u16"
  },
  ...
}
```

Embed in the page or load lazily via fetch.

A nice extra: include the **pseudocode** (which both ARM and
Intel publish), so the tooltip shows the SIMD operation as code,
not just prose.  More educational than English.

### 3. Frontend script (~30 lines + Tippy.js)

On page load, find `.intrinsic` spans, attach Tippy tooltips that
show: signature + brief description + pseudocode (if present),
with a "Read more" link to ARM/Intel docs.

```js
import tippy from 'tippy.js';
const db = await fetch('intrinsics.json').then(r => r.json());
document.querySelectorAll('.intrinsic').forEach(el => {
  const info = db[el.dataset.name];
  if (!info) return;
  tippy(el, {
    content: renderTooltip(info),
    interactive: true,
    allowHTML: true,
  });
});
```

## Cheaper-but-uglier alternatives

If full hover-with-content is too much:

1. **Just link, don't tooltip** — the markdown processor wraps
   intrinsic names in `<a href="...">` to ARM/Intel.  Click goes
   to docs.  No hover content.  ~30 lines, no JS, no DB.
2. **Static HTML with `title=""` attributes** instead of Tippy —
   browser native tooltip, no JS, ugly styling.  ~50 lines, zero
   deps.  Good as a v0.

## Use cases

- The driving use case: educational walkthroughs of SIMD kernels in
  the canasort project (e.g., `extras/fused_oct/walkthroughs/oct_contains.md`).
- Generalizes to any SIMD-heavy doc:
  - Algorithm tutorials (Lemire's blog posts, ARM/Intel optimization
    guides, etc.)
  - Code review comments where reviewer drops in a code snippet
  - Self-documenting library headers (with a build step that
    produces an HTML reference)

## Open questions

- **Source-of-truth for the database**: ARM's downloadable JSON has
  some quirks (multiple entries per name with different
  architecture variants).  Need to pick one canonical entry per
  name and pre-flatten.
- **Maintenance**: ARM and Intel update their lists periodically
  (new ISA extensions, e.g., AVX10).  Refresh cadence?
- **Coverage**: the regex catches NEON / SSE / AVX naming; what
  about SVE (`sv*`)?  RISC-V vector intrinsics?  WebAssembly SIMD?
- **Disambiguation**: some names appear in multiple ISAs (rare but
  possible); how to render?  Maybe a per-doc `defaultIsa: neon`
  hint, or just show both with separator.
- **Accessibility**: tooltips on hover-only is bad for keyboard /
  touch users.  Tippy supports click-to-open; should we make that
  the default?
- **Performance**: a 10 MB JSON on every page load is heavy for
  blog-style use.  Could split per-ISA, or generate per-page
  subsets at build time (only intrinsics actually mentioned in the
  doc).

## Naming / framing

- **simd.dev** — domain available?  Domain itself unclear; pick
  something else if not.
- Project as a **library** (drop-in MD processor) vs **service**
  (paste markdown, get HTML)?  Library first; service is just
  a wrapper.
- A **VS Code extension** that previews `.md` files with this
  treatment would be a natural follow-on.

## Prior art / similar projects worth checking

- [`docusaurus`](https://docusaurus.io/) — has remark/rehype
  plugin slots; a remark plugin could implement the intrinsic
  scanning.
- [`mkdocs-material`](https://squidfunk.github.io/mkdocs-material/)
  — mkdocs hook system could host the same logic.
- The Highway library has `g3doc/` writeups that link to specific
  intrinsics inline; manual labor today.
- ARM Compute Library has function-level docs but doesn't link
  intrinsics.

## Initial milestones

1. **v0**: Pandoc Lua filter that wraps intrinsic names in `<a
   href="...">` (no hover, just links).  Validates the regex and
   the URL templates.  ~30 lines.
2. **v0.1**: Same, but with `title="..."` static tooltips
   populated from a tiny hand-curated JSON of the ~50 intrinsics
   used in the canasort docs.
3. **v1**: Full pipeline — auto-built JSON DB from official
   sources, Tippy.js tooltips with pseudocode, deployed as a
   reusable Pandoc/markdown-it/python-markdown filter.
4. **v1.1**: VS Code extension wrapping the same renderer for
   live preview.
