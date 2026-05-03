# simd-vscode

VS Code extension that adds a hover tooltip for every SIMD intrinsic and
SIMD type in C/C++/Objective-C source. Backed by the same database as
the [`simd-tooltip`](../simd-tooltip) JS library — covers ARM NEON /
Helium / SVE / SVE2 / SME and Intel MMX / SSE\* / AVX / AVX2 / AVX-512 /
AMX (~22k records, 208 SIMD types).

Status: **manual install only**. Not on the Marketplace yet.

## What you get

Hover any name VS Code would treat as a C identifier — if we know it,
you'll see:

- the canonical signature (or typedef for SIMD types)
- ISA family + arch tags (`AVX-512`, `aarch64`, `Neon`, …)
- short prose description (from ARM ACLE for ARM, from LLVM
  Doxygen + Intel XML for Intel)
- upstream pseudocode (collapsible, controlled by a setting)
- a link to the upstream developer-portal page

If you have **clangd** active too, VS Code will stack both providers
(clangd's content first, ours after). For AVX-512 and ARM intrinsics
where clangd shows only the signature, we fill in the rest.

## Install (recommended — package as `.vsix`)

Requires [`@vscode/vsce`](https://github.com/microsoft/vscode-vsce)
(`brew install vsce` on macOS, or `npm install -g @vscode/vsce`):

```sh
( cd simd-vscode && ./sync.sh && vsce package )   # produces simd-vscode-0.0.1.vsix
code --install-extension simd-vscode/simd-vscode-0.0.1.vsix
```

Then reload VS Code (Cmd-Shift-P → "Developer: Reload Window", or quit
and relaunch). Open any `.c` / `.cpp` file with SIMD intrinsics and
hover an intrinsic name to confirm — `_mm256_add_epi32`, `vfmaq_f32`,
`svadd_s32_z`, etc. all should produce a tooltip card.

The packaged `.vsix` is fully self-contained (the data file is bundled
inside it), so the same file can be shared via Slack/email/etc. and
installed without cloning the repo.

## Develop / hack on the extension

For iterating on `extension.js` itself, skip packaging entirely and
launch VS Code in **extension-development mode**:

```sh
code --extensionDevelopmentPath=$(pwd)/simd-vscode
```

That opens a fresh "Extension Development Host" window with this
extension loaded directly from your working tree — edits to
`extension.js` take effect on the next reload. No registry write,
no .vsix, nothing to clean up afterwards.

> **Why not just symlink into `~/.vscode/extensions/`?**
> Recent VS Code versions only load extensions that have an entry in
> `~/.vscode/extensions/extensions.json`, which is written by
> `code --install-extension`. A bare folder (or symlink) that's not in
> that registry is silently ignored, even after a restart. Use the
> `.vsix` install path or `--extensionDevelopmentPath` instead.

## Settings

| Setting | Values | Default | Effect |
|---|---|---|---|
| `simdVscode.pseudocode` | `expanded` / `off` | `expanded` | Whether to include the upstream pseudocode block in the hover. |
| `simdVscode.languages` | `string[]` | `["c", "cpp", "objective-c", "objective-cpp"]` | Language IDs where the hover provider is registered. Add `"rust"` etc. if you want to extend it. |

## Updating the database

The vendored `data/simd-data.json` is a copy of
`simd-tooltip/dist/simd-data.json`. Whenever upstream regenerates
(via `scripts/build_all.sh --refresh`), re-sync:

```sh
( cd simd-vscode && ./sync.sh )
```

If you installed via Option A (symlink), reload the VS Code window. If
via Option B (`.vsix`), re-package and re-install.

## What this *isn't*, yet

- No Marketplace listing. Manual install only.
- No webview / "Show full info" command — just the inline hover.
- No keyboard `Cmd-K` search. The cursor has to be *on* the name.
- No cross-arch hints ("the NEON equivalent of `_mm_maddubs_epi16` is
  …") — that depends on the cross-arch table tracked in
  [IDEAS.md](../IDEAS.md).
- No dedupe with clangd's hover; both stack. For names where clangd
  has rich Doxygen (most of AVX2 / SSE / FMA) you'll see redundant
  content. For AVX-512 and ARM that's where the value is — clangd
  shows only the signature there.

## File layout

```
simd-vscode/
├── package.json        # extension manifest
├── extension.js        # ~120 lines: HoverProvider + Markdown formatter
├── data/
│   └── simd-data.json  # vendored from simd-tooltip/dist/ (gitignored)
├── sync.sh             # copy fresh simd-data.json from simd-tooltip/dist/
└── README.md           # this file
```

Pure JavaScript, no TypeScript build step, no node_modules required at
runtime — VS Code provides `vscode` and `fs`/`path` are stdlib.
