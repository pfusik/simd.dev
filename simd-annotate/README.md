# simd-annotate

Take any HTML file, find the SIMD intrinsic and type names that appear in
its visible text, and emit a self-contained HTML file with the
[`simd-tooltips`](../simd-tooltip) library and *only the relevant slice*
of the intrinsic database embedded inline.

The output is **100% offline-ready** — no network fetches, no
`<script src>`, no `<link href>`. Useful for:

- archived blog posts / lecture notes that should keep working forever,
- intranets and offline dev environments,
- CI artifacts (PDF-print of an annotated page, e-book exports),
- email-as-attachment "here's the annotated walkthrough" workflows,
- any page where you want zero runtime dependence on a CDN.

## Quick start

```sh
# Annotate a static HTML file with hover-+-? tooltips:
simd-annotate/simd-annotate page.html -o page.annotated.html --on hover+?

# Pipe-friendly (default output is stdout):
simd-annotate/simd-annotate page.html | tee out.html >/dev/null

# Click-mode tooltips, walk-and-wrap (visible underline + Tab navigation):
simd-annotate/simd-annotate page.html -o out.html --on click --wrap
```

Open `out.html` in a browser, *air-gapped if you like*. Tooltips work.

## Options

```
simd-annotate INPUT [-o OUTPUT] [--on TRIGGER] [--wrap] [--data DIR]

  INPUT              HTML file (or '-' for stdin)
  -o, --output       output file (default: stdout)
  --on TRIGGER       'hover' (default) | 'click' | 'hover+?'
  --wrap             walk-and-wrap mode (visible underline + Tab focus)
  --data DIR         simd-tooltip/dist/ location
                     (default: ../simd-tooltip/dist relative to this script)
  --quiet            suppress the stderr summary line
```

The trigger and wrap flags map directly to the runtime library options
documented in [`../simd-tooltip/README.md`](../simd-tooltip/README.md).

## Sample

```sh
simd-annotate/simd-annotate \
    simd-annotate/examples/sample.html \
    -o simd-annotate/examples/sample.annotated.html \
    --on hover+?
```

…produces something like:

```
simd-annotate: in=…/sample.html (2,831 B)  matched=16 intrinsics + 4 types
   embedded library=32,385 B + data=12,219 B   out=47,435 B
```

For this short article the embedded subset is **12 KB raw** — versus
the full `simd-data.json` of ~9 MB raw. (Pages with more intrinsics
will scale up roughly linearly with how many they reference.)

## How it works

1. **Visible-text extraction** — the input HTML is parsed with the
   stdlib `html.parser` and text inside `<script>`, `<style>`,
   `<textarea>`, `<input>`, `<select>`, `<option>`, and `<noscript>` is
   skipped. Same skip-set the runtime library uses.
2. **Tokenize and match** — text is split into C-identifier tokens and
   matched against the names index from `simd-tooltip/dist/simd-names.json`.
3. **Subset** — for every name found, the corresponding record from
   `simd-data.json` is pulled into a fresh tiny database. Ambiguous-alias
   entries are preserved so the runtime can render the "resolves to N
   typed variants" hint inline.
4. **Inject** — the library is inlined verbatim with a `data-no-auto`
   marker (so its auto-init doesn't run), and a second small `<script>`
   block calls `SimdTooltips.init({ on, wrap, names, data })` with the
   subset and the chosen options. The block is inserted just before
   `</body>` (or `</html>`, or appended).

Pure stdlib Python — no pip, no node. Reproducible: same input + same
`simd-tooltip/dist` snapshot ⇒ same output bytes.

## Limitations

- The annotator looks at the *static* HTML you give it. If your page
  generates content via JS at runtime, names that only appear after
  hydration won't be picked up at annotation time. (For SPA / dynamic
  content, ship `simd-tooltips.js` the normal way and let it run live.)
- Wrap mode is still a runtime walk inside the embedded library; the
  annotator does not pre-wrap spans into the HTML at build time. That
  could be a future enhancement (smaller browser-side cost) but adds
  complexity and inflates output size.
