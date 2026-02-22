#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

shopt -s nullglob

# Generated outputs from all pipelines
TARGETS=(
  "$HERE"/chunks*
  "$HERE"/artifacts*
  "$HERE"/out*
)

# Locally built binaries (rebuildable)
TARGETS+=(
  "$HERE/tools/biocompress_preprocessor"
  "$HERE/tools/geojson_to_bin_universal"
  "$HERE/tools/lidar_preprocessor"
)

for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned generated outputs under: $HERE"
echo "Kept: data/ (downloads), openzl/ (checkout), schemas/, tools/ (sources), scripts/"
