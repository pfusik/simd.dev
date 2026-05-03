# LLM-style explanations: 17 representative intrinsics

A side-by-side preview of what a Phase 3 LLM pass could produce for the
intrinsic catalog. For each entry: the upstream pseudocode (verbatim,
cleaned), then a tight `result[i] = …` formula, a plain-English summary,
and a worked example.

The "produced" content here was hand-written by Claude (acting as the
LLM) using only signature + description + upstream pseudocode for each
intrinsic. In production this would be a pinned-model batch with cache,
plus a verifier that runs the upstream pseudocode through a tiny
ASL/Intel-DSL interpreter on the example inputs to confirm the LLM's
claimed `result` agrees.

Picked to span complexity:

- simple lane-wise:   `_mm_add_epi32`, `vadd_s8`
- shuffle/permute:    `_mm256_permute_ps`, `vrev64q_u8`, `_mm256_shuffle_epi8`
- table lookup:       `vqtbl1q_u8`
- predicated/masked:  `svadd_s32_z`, `_mm512_mask_add_pd`, `svld1_u8`
- gather:             `_mm256_i32gather_epi32`
- multiply-add:       `_mm256_madd_epi16`, `_mm_fmadd_ps`
- reduction:          `vmaxvq_u32`
- bit-extract:        `_mm_movemask_epi8`
- type-widen:         `_mm512_cvtepi32_pd`
- crypto:             `_mm_aesenc_si128`
- FP rounding:        `vrndnq_f32`

---

## `_mm_add_epi32`  · SSE2 · x86_64

`__m128i _mm_add_epi32(__m128i a, __m128i b)`

**Upstream pseudocode:**
```
FOR j := 0 to 3
    i := j*32
    dst[i+31:i] := a[i+31:i] + b[i+31:i]
ENDFOR
```

**Formula:**
```
result[i] = a[i] + b[i]    for i in 0..3   (32-bit lanes, mod 2^32)
```

**Plain English:** Lane-wise 32-bit integer add. Four lanes; each output lane is the sum of the corresponding input lanes, wrapping on overflow.

**Example:**
```
a      = [   1,    2,    3,     4]
b      = [  10,   20,   30,    40]
result = [  11,   22,   33,    44]
```

---

## `vadd_s8`  · NEON · aarch32 + aarch64

`int8x8_t vadd_s8(int8x8_t a, int8x8_t b)`

**Upstream pseudocode** (ARM ASL, condensed):
```
for e = 0 to elements-1
    Elem[result, e, esize] = Elem[a, e, esize] + Elem[b, e, esize];
```
(`esize = 8`, `elements = 8`, total 64-bit D-register.)

**Formula:**
```
result[i] = a[i] + b[i]    for i in 0..7   (signed 8-bit lanes, wraps mod 2^8)
```

**Plain English:** Lane-wise signed 8-bit add over 8 lanes (one NEON D-register, 64 bits). Wraps on overflow.

**Example:**
```
a      = [  1,  2,  3,  4,  5,  6,  7,  8]
b      = [ 10, 20, 30, 40, 50, 60, 70, 80]
result = [ 11, 22, 33, 44, 55, 66, 77, 88]
```

---

## `_mm256_permute_ps`  · AVX · x86_64

`__m256 _mm256_permute_ps(__m256 a, int imm8)`

**Upstream pseudocode:** A `SELECT4` macro picks one of 4 floats per 128-bit lane based on a 2-bit field of `imm8`; the same `imm8` shuffles both 128-bit lanes.

**Formula:**
```
result[i] = a[ (i & ~3) + ((imm8 >> (2*(i & 3))) & 3) ]    for i in 0..7
```
(`i & ~3` keeps you in the right 128-bit lane; the 2-bit field at position `2*(i & 3)` picks the source within that lane.)

**Plain English:** Permute single-precision floats *within each 128-bit half* independently. The 8-bit `imm8` packs four 2-bit selectors that map output positions 0,1,2,3 to one of the 4 input floats in the same 128-bit lane. The same selectors apply to both halves.

**Example:** `imm8 = 0b00_01_10_11 = 0x1B` (reverse 4 floats per lane)
```
a      = [ 1.0, 2.0, 3.0, 4.0,    10.0, 20.0, 30.0, 40.0]
result = [ 4.0, 3.0, 2.0, 1.0,    40.0, 30.0, 20.0, 10.0]
```

---

## `vrev64q_u8`  · NEON · aarch32 + aarch64

`uint8x16_t vrev64q_u8(uint8x16_t vec)`

**Upstream pseudocode** (ASL): reverse element order within each 64-bit container of an `esize=8` vector.

**Formula:**
```
result[i] = a[ (i & ~7) + 7 - (i & 7) ]    for i in 0..15
         (= a[i ^ 7])
```

**Plain English:** Byte-reverse within each 64-bit half. The lower 8 bytes get reversed; the upper 8 bytes get reversed independently.

**Example:**
```
a      = [00 01 02 03 04 05 06 07 | 10 11 12 13 14 15 16 17]
result = [07 06 05 04 03 02 01 00 | 17 16 15 14 13 12 11 10]
```

---

## `_mm_aesenc_si128`  · AES · x86_64

`__m128i _mm_aesenc_si128(__m128i a, __m128i RoundKey)`

**Upstream pseudocode:**
```
a := ShiftRows(a)
a := SubBytes(a)
a := MixColumns(a)
dst := a XOR RoundKey
```

**Formula:**
```
result = MixColumns( SubBytes( ShiftRows(a) ) ) XOR RoundKey
```

**Plain English:** Performs one AES encryption round on the 128-bit state `a`: ShiftRows → SubBytes → MixColumns, then XORs with `RoundKey`. Used inside an AES-128/192/256 encrypt loop, one call per round (with a final `_mm_aesenclast_si128` skipping MixColumns).

*(Worked numeric example omitted — 16-byte AES state values aren't illuminating without showing the full S-box / column tables.)*

---

## `vqtbl1q_u8`  · NEON · aarch64

`uint8x16_t vqtbl1q_u8(uint8x16_t t, uint8x16_t idx)`

**Upstream pseudocode** (ASL, condensed):
```
table = V[n]                          // 16-byte table
for i = 0 to elements-1               // elements = 16
    index = UInt(Elem[idx, i, 8])
    if index < 16 then
        Elem[result, i, 8] = Elem[table, index, 8]
    // else: result lane i stays zero
```

**Formula:**
```
result[i] = (idx[i] < 16) ? t[idx[i]] : 0    for i in 0..15
```

**Plain English:** Byte-wise table lookup with bounds check. For each of 16 output lanes, take the corresponding index byte; if it's `0..15` look it up in the 16-byte table `t`; if it's `16..255` (any value with a high bit or top nibble set) the output is 0. The "Q" form handles 16-byte tables; non-Q forms exist for 8-byte tables.

**Example:**
```
t      = [00 01 02 03 04 05 06 07 08 09 0A 0B 0C 0D 0E 0F]
idx    = [05 03 1F 00 0F 80 02 0E 04 0C 09 06 11 0A 07 0B]
                  ^^                     ^^
                out of range            out of range
result = [05 03 00 00 0F 00 02 0E 04 0C 09 06 00 0A 07 0B]
```

---

## `_mm256_i32gather_epi32`  · AVX2 · x86_64

`__m256i _mm256_i32gather_epi32(int const* base_addr, __m256i vindex, const int scale)`

**Upstream pseudocode:**
```
FOR j := 0 to 7
    i := j*32
    addr := base_addr + SignExtend64(vindex[j]) * scale
    dst[i+31:i] := MEM[addr+31:addr]
ENDFOR
```
(`scale ∈ {1, 2, 4, 8}` — bytes per element.)

**Formula:**
```
result[i] = *(const int32_t *)((const char *)base_addr + vindex[i] * scale)
            for i in 0..7
```

**Plain English:** Gathers 8 32-bit integers from non-contiguous memory. Each output lane reads from `base_addr + vindex[i] * scale`. `scale` is the immediate byte-stride between consecutive elements in memory (typically 4 when the array is `int32_t[]`).

**Example:** `base_addr = arr` where `arr = [100, 101, 102, ..., 107, 108, 109]`, `scale = 4`
```
vindex = [3, 7, 1, 0, 5, 2, 4, 6]
result = [arr[3], arr[7], arr[1], arr[0], arr[5], arr[2], arr[4], arr[6]]
       = [   103,    107,    101,    100,    105,    102,    104,    106]
```

---

## `svadd_s32_z`  · SVE · aarch64

`svint32_t svadd_s32_z(svbool_t pg, svint32_t op1, svint32_t op2)`

**Upstream pseudocode** (ARM gives English here, not ASL):
```
Return a vector in which each active element i contains op1[i] + op2[i].
Set the inactive elements of the result to zero.
The operation uses modulo arithmetic.
```

**Formula:**
```
result[i] = pg[i] ? (op1[i] + op2[i]) : 0    for i in 0..(VL/32 - 1)
```
(`VL` is the runtime SVE vector length, ≥128 bits and a multiple of 128.)

**Plain English:** SVE predicated 32-bit add with **zero**-masking (`_z`). Active lanes (where `pg[i]` is true) compute `op1[i] + op2[i]`; inactive lanes are zeroed. Vector length is hardware-dependent. Sibling forms: `_m` keeps the previous value of `op1`, `_x` is "don't care".

**Example** (illustrating with `VL=128`, so 4 int32 lanes):
```
pg     = [1, 0, 1, 1]
op1    = [10, 20, 30, 40]
op2    = [ 1,  2,  3,  4]
result = [11,  0, 33, 44]
```

---

## `_mm512_mask_add_pd`  · AVX-512F · x86_64

`__m512d _mm512_mask_add_pd(__m512d src, __mmask8 k, __m512d a, __m512d b)`

**Upstream pseudocode:**
```
FOR j := 0 to 7
    i := j*64
    IF k[j]
        dst[i+63:i] := a[i+63:i] + b[i+63:i]
    ELSE
        dst[i+63:i] := src[i+63:i]
    FI
ENDFOR
```

**Formula:**
```
result[i] = k[i] ? (a[i] + b[i]) : src[i]    for i in 0..7
```

**Plain English:** AVX-512 masked double-precision add. Lanes where mask bit `k[i]` is 1 get `a[i] + b[i]`; lanes where it's 0 pass through `src[i]` unchanged. The `_mask` form preserves an existing value; the `_maskz` sibling zeroes the inactive lanes instead.

**Example:**
```
src    = [9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0]
k      = 0b1010_1100              // bits 7,5,3,2 set
a      = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
b      = [ 10,  20,  30,  40,  50,  60,  70,  80]

result = [9.0, 9.0,33.0,44.0, 9.0,66.0, 9.0,88.0]
                    ^    ^         ^         ^
            mask=1: a+b applies; otherwise src is kept
```

---

## `_mm256_madd_epi16`  · AVX2 · x86_64

`__m256i _mm256_madd_epi16(__m256i a, __m256i b)`

**Upstream pseudocode:**
```
FOR j := 0 to 7
    i := j*32
    dst[i+31:i] := SignExtend32(a[i+31:i+16]*b[i+31:i+16])
                 + SignExtend32(a[i+15:i  ]*b[i+15:i  ])
ENDFOR
```

**Formula** (treating `a`, `b` as arrays of 16 signed 16-bit lanes; `result` has 8 signed 32-bit lanes):
```
result[j] = (int32_t)a[2j]   * (int32_t)b[2j]
          + (int32_t)a[2j+1] * (int32_t)b[2j+1]    for j in 0..7
```

**Plain English:** Pairwise signed multiply-add of 16-bit lanes into 32-bit lanes. The 16 i16 lanes of `a` and `b` are multiplied lane-wise to give 16 intermediate i32s; adjacent pairs are summed to produce 8 i32 outputs. Workhorse for INT16 dot-product kernels (and 8-bit deep-learning kernels combined with `_mm256_maddubs_epi16`).

**Example:** `a = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]`, `b = [1]*16`
```
intermediate = a[0]*1, a[1]*1, ... = [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16]
result[0] = 1+2 = 3
result[1] = 3+4 = 7
...
result    = [3, 7, 11, 15, 19, 23, 27, 31]
```

---

## `vmaxvq_u32`  · NEON · aarch64

`uint32_t vmaxvq_u32(uint32x4_t a)`

**Upstream pseudocode** (ASL, condensed):
```
maxmin = a[0]
for e = 1 to 3
    maxmin = max(maxmin, a[e])
return maxmin
```

**Formula:**
```
result = max( a[0], a[1], a[2], a[3] )      // returned as scalar uint32_t
```

**Plain English:** Horizontal unsigned max-reduction across the 4 unsigned 32-bit lanes; returns a scalar. Useful for finding "largest element" or for `reduce_or`-style summary masks.

**Example:**
```
a      = [10, 50, 25, 30]
result = 50
```

---

## `_mm_movemask_epi8`  · SSE2 · x86_64

`int _mm_movemask_epi8(__m128i a)`

**Upstream pseudocode:**
```
FOR j := 0 to 15
    dst[j] := a[j*8 + 7]   // MSB of byte j
ENDFOR
dst[31:16] := 0
```

**Formula:**
```
result = OR over j in 0..15 of  ((a[j] >> 7) & 1) << j     // a 16-bit value in low int
```

**Plain English:** Take the most-significant bit of each of the 16 bytes of `a` and pack them into the low 16 bits of an integer. Common pattern: do a per-byte compare (`_mm_cmpeq_epi8`), then `movemask` to convert the all-ones-or-all-zeros lane mask into a scalar bitmask you can `bsf`/`__builtin_ctz` over.

**Example:**
```
a (16 bytes, hex):
 byte:    0   1   2   3   4   5   6   7   8   9  10  11  12  13  14  15
 value:  ff  00  ff  80  7f  ff  01  00  80  40  ff  ff  00  ff  80  00
 MSB:     1   0   1   1   0   1   0   0   1   0   1   1   0   1   1   0

result = 0b0110_1101_0010_1101 = 0x6D2D
```

---

## `_mm256_shuffle_epi8`  · AVX2 · x86_64

`__m256i _mm256_shuffle_epi8(__m256i a, __m256i b)`

**Upstream pseudocode:**
```
FOR j := 0 to 15                   // applied per 128-bit lane, twice
    IF b[i+7] == 1                 // top bit of mask byte
        dst[i+7:i] := 0
    ELSE
        index := b[i+3:i]          // low 4 bits of mask byte
        dst[i+7:i] := a[index*8+7:index*8]
    FI
ENDFOR
```

**Formula:**
```
result[i] = (b[i] & 0x80) ? 0 : a[ (i & ~15) + (b[i] & 0x0F) ]    for i in 0..31
```
(`i & ~15` keeps you in the same 128-bit lane.)

**Plain English:** Per-byte permute *within each 128-bit lane independently* (no cross-lane movement). Each output byte is taken from `a` at the index given by the low 4 bits of the corresponding shuffle byte; if that byte's top bit is set, the output is 0. Common building block for byte-wise lookup tables (where the table fits in 16 bytes), pshufb/tbl-style reorderings, etc.

**Example** (lower 128-bit lane only):
```
a (lower 16) = [10 11 12 13 14 15 16 17 18 19 1A 1B 1C 1D 1E 1F]
b (lower 16) = [00 01 02 03 FF 00 00 00 00 00 00 00 00 00 00 00]
                              ^^
                            top bit set
result(low) = [10 11 12 13 00 10 10 10 10 10 10 10 10 10 10 10]
```

---

## `svld1_u8`  · SVE · aarch64

`svuint8_t svld1_u8(svbool_t pg, const uint8_t *base)`

**Upstream pseudocode** (English):
```
Return a vector in which each active element i contains base[i] and
in which all other elements are zero. Do not access memory for inactive
elements.
```

**Formula:**
```
result[i] = pg[i] ? base[i] : 0     for i in 0..(VL/8 - 1)
```

**Plain English:** SVE predicated byte load. Active lanes load `base[i]`; inactive lanes are zeroed. Crucially, *inactive lanes do not access memory* — so you can predicate off the tail of an array without faulting on bytes past the end.

**Example** (with `VL=128`, so 16 byte lanes):
```
pg     = [1 1 0 1 0 0 1 1 1 1 0 0 0 1 1 1]
*base  = [a b c d e f g h i j k l m n o p]
result = [a b 0 d 0 0 g h i j 0 0 0 n o p]
```

---

## `_mm512_cvtepi32_pd`  · AVX-512F · x86_64

`__m512d _mm512_cvtepi32_pd(__m256i a)`

**Upstream pseudocode:**
```
FOR j := 0 to 7
    i := j*32   // input position
    m := j*64   // output position
    dst[m+63:m] := Convert_Int32_To_FP64(a[i+31:i])
ENDFOR
```

**Formula:**
```
result[i] = (double) a[i]      for i in 0..7
```
(input is `__m256i` = 8 × i32 = 256 bits; output is `__m512d` = 8 × f64 = 512 bits — the register doubles in size to hold the wider type.)

**Plain English:** Convert 8 signed 32-bit integers to 8 64-bit doubles. The output register is twice the width of the input. Exact for all int32 values (f64 has 53 bits of mantissa, which covers any int32).

**Example:**
```
a      = [        1,        -2,         3,    1000000,   -1000000,         0,         7,         8]
result = [      1.0,      -2.0,       3.0, 1000000.0, -1000000.0,       0.0,       7.0,       8.0]
```

---

## `_mm_fmadd_ps`  · FMA · x86_64

`__m128 _mm_fmadd_ps(__m128 a, __m128 b, __m128 c)`

**Upstream pseudocode:**
```
FOR j := 0 to 3
    i := j*32
    dst[i+31:i] := (a[i+31:i] * b[i+31:i]) + c[i+31:i]
ENDFOR
```

**Formula:**
```
result[i] = a[i] * b[i] + c[i]     for i in 0..3
            (single rounding step at the end -- "fused" multiply-add)
```

**Plain English:** Fused multiply-add: 4-lane single-precision `a*b + c` with a single rounding step (more accurate than separate multiply and add — the intermediate product is held at full precision before the final add). Workhorse for matmul, convolution, dot-product, polynomial evaluation.

**Example:**
```
a      = [1.0, 2.0, 3.0, 4.0]
b      = [5.0, 6.0, 7.0, 8.0]
c      = [10.0,10.0,10.0,10.0]
result = [15.0,22.0,31.0,42.0]
```

---

## `vrndnq_f32`  · NEON · aarch32 + aarch64

`float32x4_t vrndnq_f32(float32x4_t a)`

**Upstream pseudocode** (ASL): per-lane `FPRoundInt(a, FPCR, FPRounding_TIEEVEN, exact=FALSE)`.

**Formula:**
```
result[i] = round_to_nearest_even(a[i])     for i in 0..3
            (still f32; integer value held in f32 format)
```

**Plain English:** Round each of 4 single-precision floats to the nearest integer value (still as f32), with banker's rounding (ties round to the even integer). Doesn't change the type; halfway values like `±0.5, ±1.5, ±2.5` go to the nearer **even** integer.

**Example:**
```
a      = [ 1.5, 2.5, -1.5, -2.5]
result = [ 2.0, 2.0, -2.0, -2.0]
            ↑    ↑    ↑    ↑
          1.5 → 2 (even); 2.5 → 2 (already even);
          -1.5 → -2 (even); -2.5 → -2 (even).
```

---

## Reading the results

What works well:

- For element-wise ops (`add`, `sub`, `mul`, `cvt`, `fmadd`, masked variants),
  the formula form is genuinely tighter than upstream and the worked example
  cements understanding.
- For shuffles / table lookups (`vqtbl1q_u8`, `_mm256_shuffle_epi8`,
  `_mm256_permute_ps`), the worked example is doing more work than the
  formula — it's where the LLM contribution shows the most value over raw
  upstream.
- ARM ASL → plain formula is a real win. `vadd_s8`'s ASL pseudocode has
  five lines of bookkeeping; the formula has one.

Where it's still imperfect:

- `_mm_aesenc_si128`-style ops where the actual transform is internally
  defined by a standard (AES, SHA, GFNI). A worked numerical example
  isn't useful unless we expand the inner functions, which is its own
  document.
- Operations gated on hardware FPCR rounding modes can be misleading if
  the example happens to be in a regime where rounding matters and we
  don't surface it.

Verifier strategy that addresses both:

1. Run the upstream Intel `<operation>` (or ARM ASL fragment) through a
   small interpreter on the worked-example inputs.
2. Confirm the LLM's stated `result` agrees byte-exact.
3. Reject any LLM output that disagrees, and either re-prompt or fall
   back to "show upstream pseudocode only".

That makes Phase 3 reproducible *up to verifier coverage* — the LLM is
the slow path, the cache + verifier ensures correctness.
