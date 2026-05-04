// Wrap intrinsic call in an immediately-invoked lambda. Tests whether
// clang's constant folder still lands the result in __const.
#include <arm_neon.h>

extern "C" const int8x16_t RESULT = []() {
    const int8x16_t a = {1, 2, 3, 4, 5, 6, 7, 8,
                         9, 10, 11, 12, 13, 14, 15, 16};
    const int8x16_t b = {100, -50, 30, -20, 10, -5, 0, 1,
                         -1, 2, -2, 3, -3, 4, -4, 5};
    return vaddq_s8(a, b);
}();

// Same with vcreate_s8 (uses statement-expression macros, otherwise illegal at file scope).
extern "C" const int8x8_t CREATE_RESULT = []() {
    const uint64_t a = 0x0123456789abcdefULL;
    return vcreate_s8(a);
}();
