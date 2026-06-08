#!/usr/bin/env python3
"""Mine cache/llvm_headers/*.h for `name -> short description` pairs.

Each clang x86 intrinsic has a Doxygen block ahead of its declaration:

    /// Computes the absolute value of each signed byte in the 256-bit
    ///    integer vector \a __a and returns each value...
    ///
    /// \headerfile <immintrin.h>
    ///
    /// This intrinsic corresponds to the \c VPABSB instruction.
    ///
    /// \param __a ...
    /// \returns ...
    static __inline__ __m256i __DEFAULT_FN_ATTRS256_CONSTEXPR
    _mm256_abs_epi8(__m256i __a) {

We extract the first paragraph (until the first blank `///` or a `\headerfile` /
`\param` / `\returns` / `\code` directive), strip Doxygen escapes, and emit
{name: description} JSON to cache/llvm_descriptions.json.

Source: clang headers under Apache-2.0 WITH LLVM-exception.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HDR_DIR = ROOT / "cache" / "llvm_headers"
OUT = ROOT / "cache" / "llvm_descriptions.json"

# Doxygen inline command stripping. We keep argument text but drop the marker.
DOXY_INLINE = re.compile(r"\\(?:a|c|p|e|em|b|ref|link|f\$|n)\s+")
# Strip @link/@endlink balanced markers (rare).
DOXY_LINK_BLOCK = re.compile(r"\\link\s+(\S+).*?\\endlink", re.DOTALL)
# Stop directives that end the headline paragraph.
HEADLINE_STOP = re.compile(r"^\\(?:headerfile|param|returns?|code|brief|note|see|sa|throws?|warning|deprecated|since|copyright)\b")
# Names we accept (Intel intrinsic-name shape: starts with `_`).
NAME_RE = re.compile(r"^_[A-Za-z][\w]*$")


def parse_file(path: Path) -> dict[str, str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.split("\n")
    n = len(lines)
    out: dict[str, str] = {}

    for i, ln in enumerate(lines):
        # We're looking for a line that starts a declaration of intrinsic `name(`.
        # Two shapes:
        #   #define _NAME(...
        #   _NAME(args...) { OR ...DEFAULT_FN... \n_NAME(args...)
        m = re.match(r"\s*#define\s+(_\w+)\s*\(", ln)
        if not m:
            m = re.match(r"\s*(_[A-Za-z]\w*)\s*\(", ln)
        if not m:
            continue
        name = m.group(1)
        if not NAME_RE.match(name):
            continue
        if name in out:
            continue  # first occurrence wins (Doxygen lives with the canonical decl).

        # Walk back skipping decl-spec/attribute lines that may sit between doc and name.
        j = i - 1
        while j >= 0:
            s = lines[j].lstrip()
            if (
                s.startswith("static ")
                or s.startswith("static\t")
                or s.startswith("extern ")
                or s.startswith("__attribute")
                or s.startswith("__inline")
                or s.startswith("__DEFAULT_FN")
                or s == ""
            ):
                j -= 1
                continue
            break

        # Now lines[j] should be the last `///` line (if any).
        doc_lines: list[str] = []
        while j >= 0 and lines[j].lstrip().startswith("///"):
            stripped = lines[j].lstrip()[3:]
            # Drop a single leading space if present.
            if stripped.startswith(" "):
                stripped = stripped[1:]
            doc_lines.append(stripped)
            j -= 1
        if not doc_lines:
            continue
        doc_lines.reverse()

        # Take the first paragraph (until blank line or stop directive).
        para: list[str] = []
        for d in doc_lines:
            ds = d.strip()
            if ds == "":
                if para:
                    break
                else:
                    continue
            if HEADLINE_STOP.match(ds):
                break
            para.append(ds)

        if not para:
            continue
        text = " ".join(para)
        # Clean Doxygen markup.
        text = DOXY_LINK_BLOCK.sub(r"\1", text)
        text = DOXY_INLINE.sub("", text)
        # Some leftover backslash directives (e.g., trailing \endcode) — drop them.
        text = re.sub(r"\\[a-zA-Z]+", "", text)
        # Collapse whitespace.
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            out[name] = text
    return out


def main():
    merged: dict[str, str] = {}
    files = sorted(HDR_DIR.glob("*.h"))
    print(f"parsing {len(files)} headers ...")
    for fp in files:
        d = parse_file(fp)
        for k, v in d.items():
            merged.setdefault(k, v)

    OUT.write_text(json.dumps(merged, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"wrote {len(merged)} descriptions -> {OUT.relative_to(ROOT)}")
    # Quick samples
    samples = ["_mm_add_epi32", "_mm256_permutevar8x32_epi32", "_mm512_add_pd",
               "_mm_aesenc_si128", "_mm256_abs_epi8", "_pdep_u32",
               "_mm256_mpsadbw_epu8"]
    for s in samples:
        if s in merged:
            print(f"  {s}: {merged[s][:130]}")
        else:
            print(f"  {s}: MISSING")


if __name__ == "__main__":
    main()
