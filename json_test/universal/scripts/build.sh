#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
JSON_TEST_DIR="$HERE/json_test"
TOOLS_DIR="$JSON_TEST_DIR/universal/tools"

mkdir -p "$TOOLS_DIR"

echo "Compiling geojson_to_bin_universal..."
clang++ -std=c++17 -O3 "$TOOLS_DIR/geojson_to_bin_universal.cpp" -o "$TOOLS_DIR/geojson_to_bin_universal"

echo "Build complete."
