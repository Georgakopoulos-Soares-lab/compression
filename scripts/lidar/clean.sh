#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

shopt -s nullglob

TARGETS=(
  "$HERE"/chunks_train*
  "$HERE"/chunks_full*
  "$HERE"/out/lidar*
  "$HERE"/artifacts/lidar*
)

for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned LiDAR generated outputs under: $HERE"
