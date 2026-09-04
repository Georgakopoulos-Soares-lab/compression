#!/usr/bin/env bash
# patch_openzl.sh — Raise OpenZL limits for wide CSV/TSV files (>256 columns).
#
# These patches increase internal array limits from 512–2048 to 4096, and fix
# the frame-header bound calculation so it doesn't underflow when the number of
# inputs is large. Needed for the VCF `panel` archetype (1000G phase 3 has 2504
# sample columns -> 2513 inputs).
#
# Applied on top of OpenZL commit d262127 (0.2.5). The sed targets are plain
# string literals that have been stable across 0.1.0 -> 0.2.5; each substitution
# is verified below so a silent upstream rename fails the build loudly.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OZL="$HERE/openzl"

if [ ! -d "$OZL/src" ]; then
  echo "ERROR: $OZL/src not found — run get_openzl.sh first" >&2
  exit 1
fi

# apply <file> <sed-expr> <string-that-must-be-present-afterwards>
apply() {
  local file="$1" expr="$2" want="$3"
  [ -f "$file" ] || { echo "ERROR: missing $file" >&2; exit 1; }
  sed -i "$expr" "$file"
  if ! grep -qF "$want" "$file"; then
    echo "ERROR: patch did not take in $file (expected '$want' — upstream may have renamed it)" >&2
    exit 1
  fi
}

echo "Patching OpenZL limits for wide-CSV support…"

apply "$OZL/src/openzl/codecs/dispatch_string/common_dispatch_string.h" \
  's/#define ZL_DISPATCH_STRING_MAX_DISPATCHES 2048/#define ZL_DISPATCH_STRING_MAX_DISPATCHES 4096/' \
  '#define ZL_DISPATCH_STRING_MAX_DISPATCHES 4096'

apply "$OZL/src/openzl/common/limits.h" \
  's/#define ZL_ENCODER_INPUT_LIMIT 2048/#define ZL_ENCODER_INPUT_LIMIT 4096/' \
  '#define ZL_ENCODER_INPUT_LIMIT 4096'

apply "$OZL/src/openzl/common/limits.c" \
  's/return 2048;/return 4096;/' \
  'return 4096;'

apply "$OZL/src/openzl/compress/encode_frameheader.c" \
  's/(numInputs \* 5)/(numInputs * 22)/' \
  '(numInputs * 22)'
apply "$OZL/src/openzl/compress/encode_frameheader.c" \
  's/(nbRegens \* 4)/(nbRegens * 8)/' \
  '(nbRegens * 8)'

apply "$OZL/src/openzl/codecs/interleave/common_interleave.h" \
  's/#define ZL_INTERLEAVE_MAX_INPUTS 512/#define ZL_INTERLEAVE_MAX_INPUTS 4096/' \
  '#define ZL_INTERLEAVE_MAX_INPUTS 4096'

echo "Patches applied and verified."
if [ "${PATCH_OPENZL_BUILD:-1}" = 1 ]; then
  echo "Rebuilding zli…"
  cd "$OZL" && env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
    make -j"$(nproc)" MOREFLAGS="-pthread" zli 2>&1 | tail -3
fi
echo "Done."
