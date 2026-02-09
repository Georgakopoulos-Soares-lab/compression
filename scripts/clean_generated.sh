#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

shopt -s nullglob

# Generated outputs from demos/benchmarks
TARGETS=(
  "$HERE"/chunks*
  "$HERE"/artifacts*
  "$HERE"/out*
)

# Locally built binaries (rebuildable)
TARGETS+=(
  "$HERE/tools/biocompress_preprocessor"
)

# Remove
for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned generated outputs under: $HERE"
echo "Kept: $HERE/data (downloads), $HERE/openzl (checkout), schemas/tools/scripts/docs"
