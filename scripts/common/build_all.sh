#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -L)"
JOBS="${JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"

# ---- 1) Ensure OpenZL exists ----
"$HERE/scripts/common/get_openzl.sh"

# ---- 2) Build OpenZL (produces ./openzl/zli) ----
cd "$HERE/openzl"
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
  make -j"$JOBS" MOREFLAGS="-pthread"

if [ ! -x "$HERE/openzl/zli" ]; then
  echo "Error: expected executable not found: $HERE/openzl/zli"
  exit 1
fi

# ---- 3) Build FASTA preprocessor ----
cd "$HERE"
g++ -O3 -std=c++17 -pthread \
  -o "$HERE/tools/biocompress_preprocessor" \
  "$HERE/tools/biocompress_preprocessor.cpp"

# ---- 4) Build GeoJSON preprocessor ----
clang++ -std=c++17 -O3 \
  -o "$HERE/tools/geojson_to_bin_universal" \
  "$HERE/tools/geojson_to_bin_universal.cpp"

# ---- 5) Build LiDAR preprocessor ----
g++ -O3 -std=c++17 \
  -o "$HERE/tools/lidar_preprocessor" \
  "$HERE/tools/lidar_preprocessor.cpp"

# ---- 6) Build VCF preprocessor ----
g++ -O3 -std=c++17 \
  -o "$HERE/tools/vcf_preprocessor" \
  "$HERE/tools/vcf_preprocessor.cpp"

echo "Build OK:"
echo "  $HERE/openzl/zli"
echo "  $HERE/tools/biocompress_preprocessor"
echo "  $HERE/tools/geojson_to_bin_universal"
echo "  $HERE/tools/lidar_preprocessor"
echo "  $HERE/tools/vcf_preprocessor"
