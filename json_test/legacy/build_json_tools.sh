#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JSON_TEST_DIR="$HERE/json_test"
TOOLS_DIR="$JSON_TEST_DIR/tools"

mkdir -p "$TOOLS_DIR"

echo "Compiling json_to_bin_cpp..."
clang++ -std=c++17 -O3 "$TOOLS_DIR/json_to_bin.cpp" -o "$TOOLS_DIR/json_to_bin_cpp"

echo "Build complete."
