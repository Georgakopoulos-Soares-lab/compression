#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Serialize concurrent runs (FASTA + VCF benchmark jobs share $HERE/openzl and
# would otherwise race on `git checkout` / `sed -i` / `make`).
exec 9>"$HERE/.build_all.lock"
flock 9

# 1) Ensure OpenZL exists (pinned commit) and apply the wide-CSV limit patch.
"$HERE/scripts/get_openzl.sh"
PATCH_OPENZL_BUILD=0 "$HERE/scripts/patch_openzl.sh"   # patch only; build happens next

# 2) Build OpenZL (produces ./openzl/zli)
cd "$HERE/openzl"

# OpenZL uses std::thread in the CLI/training code; on some Linux toolchains
# the linker won't pull in pthread unless -pthread is provided.
#
# Also: user-provided *FLAGS from the environment can override OpenZL's
# required C++ standard settings. We explicitly unset them for reproducibility.
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
  make -j"${JOBS:-${SLURM_CPUS_ON_NODE:-$(nproc)}}" MOREFLAGS="-pthread"

# nyx_vcf links against OpenZL directly rather than shelling out to zli, so it
# needs the static library as well as the CLI.
env -u CFLAGS -u CXXFLAGS -u CPPFLAGS -u LDFLAGS -u LDLIBS \
  make -j"${JOBS:-${SLURM_CPUS_ON_NODE:-$(nproc)}}" MOREFLAGS="-pthread" lib

if [ ! -x "$HERE/openzl/zli" ]; then
  echo "Error: expected executable not found: $HERE/openzl/zli"
  echo "OpenZL built, but zli was not produced where expected."
  echo "Try: ls -la $HERE/openzl | grep zli"
  exit 1
fi

# 3) Build the NYX tools. CPPFLAGS/LDFLAGS carry include and library paths when
# a packager sets them (conda puts zlib under $PREFIX); empty in a normal build.
cd "$HERE"

"${CXX:-g++}" ${CPPFLAGS:-} -O3 -std=c++17 -pthread \
  -o "$HERE/tools/biocompress_preprocessor" \
  "$HERE/tools/biocompress_preprocessor.cpp"

# 3b) FASTA decoder (inverse of the FAV5 packed format -> exact original FASTA)
"${CXX:-g++}" ${CPPFLAGS:-} -O2 -std=c++17 \
  -o "$HERE/tools/fasta_postprocess" \
  "$HERE/tools/fasta_postprocess.cpp"

# 4) Build the BED compressor (column model + OpenZL, in-process)
"${CXX:-g++}" ${CPPFLAGS:-} -O3 -std=c++17 -pthread \
  -o "$HERE/tools/nyx_bed" "$HERE/tools/nyx_bed.cpp" \
  -I"$HERE/openzl/include" -I"$HERE/openzl/src" -I"$HERE/tools" \
  "$HERE/openzl/libopenzl.a" \
  "$HERE/openzl/deps/zstd/lib/libzstd.a" \
  "$HERE/openzl/deps/lz4/lib/liblz4.a" \
  ${LDFLAGS:-} -lz


# 6) Build the VCF compressor (format-aware transform + OpenZL, in-process)
"${CXX:-g++}" ${CPPFLAGS:-} -O3 -std=c++17 -pthread \
  -o "$HERE/tools/nyx_vcf" "$HERE/tools/nyx_vcf.cpp" \
  -I"$HERE/openzl/include" -I"$HERE/openzl/src" \
  "$HERE/openzl/libopenzl.a" \
  "$HERE/openzl/deps/zstd/lib/libzstd.a" \
  "$HERE/openzl/deps/lz4/lib/liblz4.a" \
  ${LDFLAGS:-} -lz

# 7) Build the FASTQ codec. It links OpenZL's whole object set rather than the
# static library, so it has its own script; calling it here means one command
# builds everything the `nyx` entry point can dispatch to.
bash "$HERE/scripts/fastq/build_nyxfqz.sh" >/dev/null

echo "Build OK:"
echo "  $HERE/openzl/zli"
echo "  $HERE/openzl/nyxfqz_v2"
echo "  $HERE/tools/biocompress_preprocessor"
echo "  $HERE/tools/fasta_postprocess"
echo "  $HERE/tools/nyx_bed"
echo "  $HERE/tools/nyx_vcf"
