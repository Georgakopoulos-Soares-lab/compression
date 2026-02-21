#!/usr/bin/env bash
# Build OpenZL and the genomic preprocessor from source.
# All artifacts are placed inside the nyx/ directory tree.
set -euo pipefail

NYX_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENZL_DIR="$NYX_ROOT/openzl"
OPENZL_REPO="${OPENZL_REPO:-https://github.com/facebook/openzl}"
OPENZL_COMMIT="${OPENZL_COMMIT:-e40fe9f314283047147d573d113fa7d17eabf7ac}"

# --- Portable thread count ------------------------------------------------
ncpu() {
  nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4
}

# --- 1) Clone / update OpenZL ---------------------------------------------
if [ -d "$OPENZL_DIR/.git" ]; then
  echo "OpenZL already present: $OPENZL_DIR"
else
  echo "Cloning OpenZL into: $OPENZL_DIR"
  git clone "$OPENZL_REPO" "$OPENZL_DIR"
fi

cd "$OPENZL_DIR"
git fetch --all --tags -q
echo "Checking out pinned commit: $OPENZL_COMMIT"
git checkout -q "$OPENZL_COMMIT"
echo "OpenZL HEAD: $(git rev-parse HEAD)"

# --- 2) Build OpenZL (produces zli) ----------------------------------------
echo ""
echo "Building OpenZL..."
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
  make -j"$(ncpu)" MOREFLAGS="-pthread"

if [ ! -x "$OPENZL_DIR/zli" ]; then
  echo "Error: zli binary not found after build."
  echo "Try: ls -la $OPENZL_DIR | grep zli"
  exit 1
fi
echo "  Built: $OPENZL_DIR/zli"

# --- 3) Build the genomic preprocessor ------------------------------------
echo ""
echo "Building genomic_preprocessor..."
cd "$NYX_ROOT"

mkdir -p "$NYX_ROOT/bin"
g++ -O3 -std=c++17 -pthread \
  -o "$NYX_ROOT/bin/genomic_preprocessor" \
  "$NYX_ROOT/tools/genomic_preprocessor.cpp"

echo "  Built: $NYX_ROOT/bin/genomic_preprocessor"

# --- 4) Build the fasta codec ------------------------------------------------
echo ""
echo "Building fasta_codec..."
g++ -O3 -std=c++17 -pthread \
  -o "$NYX_ROOT/bin/fasta_codec" \
  "$NYX_ROOT/tools/fasta_codec.cpp"

echo "  Built: $NYX_ROOT/bin/fasta_codec"

# --- 5) Build the fastq codec ------------------------------------------------
echo ""
echo "Building fastq_codec..."
g++ -O3 -std=c++17 -pthread \
  -o "$NYX_ROOT/bin/fastq_codec" \
  "$NYX_ROOT/tools/fastq_codec.cpp"

echo "  Built: $NYX_ROOT/bin/fastq_codec"

# --- Done ------------------------------------------------------------------
echo ""
echo "Build complete. Binaries:"
echo "  $OPENZL_DIR/zli"
echo "  $NYX_ROOT/bin/genomic_preprocessor"
echo "  $NYX_ROOT/bin/fasta_codec"
echo "  $NYX_ROOT/bin/fastq_codec"
