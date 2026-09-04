#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CXX="${CXX:-g++}"
CXXFLAGS="${CXXFLAGS:--O2 -std=c++17 -Wall}"

echo "Building tools/harc_preprocessor and tools/harc_decode"
"$CXX" $CXXFLAGS -o "$HERE/tools/harc_preprocessor" "$HERE/tools/harc_preprocessor.cpp" -pthread
"$CXX" $CXXFLAGS -o "$HERE/tools/harc_decode" "$HERE/tools/harc_decode.cpp"

echo "Built:"
echo "  $HERE/tools/harc_preprocessor"
echo "  $HERE/tools/harc_decode"
