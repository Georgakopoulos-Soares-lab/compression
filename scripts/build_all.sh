#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 1) Ensure OpenZL exists
"$HERE/scripts/get_openzl.sh"

# 2) Build OpenZL (produces ./openzl/zli)
cd "$HERE/openzl"

# OpenZL uses std::thread in the CLI/training code; on some Linux toolchains
# the linker won't pull in pthread unless -pthread is provided.
#
# Also: user-provided *FLAGS from the environment can override OpenZL's
# required C++ standard settings. We explicitly unset them for reproducibility.
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
  make -j"${JOBS:-$(nproc)}" MOREFLAGS="-pthread"

if [ ! -x "$HERE/openzl/zli" ]; then
  echo "Error: expected executable not found: $HERE/openzl/zli"
  echo "OpenZL built, but zli was not produced where expected."
  echo "Try: ls -la $HERE/openzl | grep zli"
  exit 1
fi

# 3) Build the biocompress preprocessor tool
cd "$HERE"

g++ -O3 -std=c++17 -pthread \
  -o "$HERE/tools/biocompress_preprocessor" \
  "$HERE/tools/biocompress_preprocessor.cpp"

# 4) Build the BED preprocessor
g++ -O2 -std=c++17 \
  -o "$HERE/tools/bed_preprocess" \
  "$HERE/tools/bed_preprocess.cpp"

# 5) Build the FASTQ preprocessor (parallel, uses std::thread)
g++ -O2 -std=c++17 -pthread \
  -o "$HERE/tools/fastq_preprocess" \
  "$HERE/tools/fastq_preprocess.cpp"

# 6) Build the tabular chunker (used by VCF pipeline)
g++ -O2 -std=c++17 \
  -o "$HERE/tools/tabular_chunker" \
  "$HERE/tools/tabular_chunker.cpp"

echo "Build OK:"
echo "  $HERE/openzl/zli"
echo "  $HERE/tools/biocompress_preprocessor"
echo "  $HERE/tools/bed_preprocess"
echo "  $HERE/tools/fastq_preprocess"
echo "  $HERE/tools/tabular_chunker"
