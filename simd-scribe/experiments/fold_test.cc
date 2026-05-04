// Pilot: can clang fold a NEON intrinsic call into .rodata bytes
// when the inputs are compile-time constants?
//
// Compile: clang++ -O2 -c fold_test.cc -o fold_test.o
// Inspect: llvm-objdump -s -j __const fold_test.o   (Mach-O)
//          llvm-objdump -s -j .rodata fold_test.o   (ELF)
//
// Expected RESULT bytes (vaddq_s8 of the two arrays below):
//   65 d0 21 f0 0f 01 07 09 08 0c 09 0f 0a 12 0b 15

#include <arm_neon.h>

const int8x16_t A = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16 };
const int8x16_t B = { 100, -50, 30, -20, 10, -5, 0, 1, -1, 2, -2, 3, -3, 4, -4, 5 };

extern "C" const int8x16_t RESULT = vaddq_s8(A, B);
