#!/usr/bin/env bash
# patch_openzl.sh — Raise OpenZL limits for wide CSV/TSV files (>256 columns).
#
# These patches increase internal array limits from 512–2048 to 4096,
# and fix the frame-header bound calculation so it doesn't underflow
# when the number of inputs is large.
#
# Applied on top of OpenZL commit e40fe9f.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OZL="$HERE/openzl"

if [ ! -d "$OZL/src" ]; then
  echo "ERROR: $OZL/src not found — run get_openzl.sh first" >&2
  exit 1
fi

echo "Patching OpenZL limits for wide-CSV support…"

# 1) dispatch_string: 2048 → 4096
sed -i 's/#define ZL_DISPATCH_STRING_MAX_DISPATCHES 2048/#define ZL_DISPATCH_STRING_MAX_DISPATCHES 4096/' \
  "$OZL/src/openzl/codecs/dispatch_string/common_dispatch_string.h"

# 2) encoder input limit: 2048 → 4096
sed -i 's/#define ZL_ENCODER_INPUT_LIMIT 2048/#define ZL_ENCODER_INPUT_LIMIT 4096/' \
  "$OZL/src/openzl/common/limits.h"

# 3) OZL_maxNumInputs() return: 2048 → 4096
sed -i 's/return 2048;/return 4096;/' \
  "$OZL/src/openzl/common/limits.c"

# 4) Frame header bound: numInputs*5 → numInputs*22, nbRegens*4 → nbRegens*8
sed -i 's/(numInputs \* 5)/(numInputs * 22)/' \
  "$OZL/src/openzl/compress/encode_frameheader.c"
sed -i 's/(nbRegens \* 4)/(nbRegens * 8)/' \
  "$OZL/src/openzl/compress/encode_frameheader.c"

# 5) interleave: 512 → 4096
sed -i 's/#define ZL_INTERLEAVE_MAX_INPUTS 512/#define ZL_INTERLEAVE_MAX_INPUTS 4096/' \
  "$OZL/src/openzl/codecs/interleave/common_interleave.h"

echo "Patches applied.  Rebuilding zli…"
cd "$OZL" && make -j"$(nproc)" 2>&1 | tail -3
echo "Done."
