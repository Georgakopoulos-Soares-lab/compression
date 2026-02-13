#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

shopt -s nullglob

# Generated outputs
TARGETS=(
  "$HERE"/chunks*
  "$HERE"/timing
  "$HERE"/out
)

# Locally built binaries
TARGETS+=(
  "$HERE/tools/json_to_bin_cpp"
)

# Generated data files (keep original JSON)
TARGETS+=(
  "$HERE"/data/*.bin
  "$HERE"/data/*.zl
  "$HERE"/data/*.dec
  "$HERE"/data/*.compressor
  "$HERE"/data/train_*.json
  "$HERE"/data/*.gz
)

# Remove
for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned generated outputs under: $HERE"
echo "Kept: $HERE/data/citylots.json (original download)"
