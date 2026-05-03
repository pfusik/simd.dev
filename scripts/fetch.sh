#!/usr/bin/env bash
# Fetch raw upstream intrinsic data into ./cache/.
# Re-run to refresh.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$HERE/cache"
mkdir -p "$CACHE"

ARM_URL="https://developer.arm.com/architectures/instruction-sets/intrinsics/data/intrinsics.json"
ARM_OPS_URL="https://developer.arm.com/architectures/instruction-sets/intrinsics/data/operations.json"
INTEL_URL="https://www.intel.com/content/dam/develop/public/us/en/include/intrinsics-guide/data-latest.xml"

echo "fetching ARM ACLE intrinsics JSON ..."
curl -fsSL "$ARM_URL" -o "$CACHE/arm_intrinsics.json"
echo "  $(wc -c < "$CACHE/arm_intrinsics.json") bytes"

echo "fetching ARM operations JSON (ASL pseudocode) ..."
curl -fsSL "$ARM_OPS_URL" -o "$CACHE/arm_operations.json"
echo "  $(wc -c < "$CACHE/arm_operations.json") bytes"

echo "fetching Intel intrinsics XML ..."
curl -fsSL "$INTEL_URL" -o "$CACHE/intel_intrinsics.xml"
echo "  $(wc -c < "$CACHE/intel_intrinsics.xml") bytes"

echo "done."
