#!/usr/bin/env bash
# build_nyxfqz.sh — build the NYX FASTQ codec (nyxfqz_v2) against the single
# OpenZL checkout shared by all three format pipelines.
#
# The codec source lives in tools/nyx/ so that it is version-controlled with the
# rest of the project. OpenZL's make rules resolve object paths relative to the
# OpenZL tree, so the sources and the makefile are staged into openzl/ here
# rather than being kept there.
#
#   scripts/fastq/build_nyxfqz.sh          -> openzl/nyxfqz_v2
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OZL="$ROOT/openzl"
SRC="$ROOT/tools/nyx"

[ -d "$OZL" ] || { echo "OpenZL missing — run scripts/get_openzl.sh first" >&2; exit 1; }
bash "$ROOT/scripts/patch_openzl.sh" >/dev/null 2>&1 || true

# stage the codec into the OpenZL tree
mkdir -p "$OZL/nyx"
cp -f "$SRC"/nyxfqz_v2.cpp "$OZL/nyx/"
cp -f "$SRC"/nyxfqz_v2.make "$OZL/"

# OpenZL only compiles the C++ dirs it knows about; add ours (idempotent)
ZLDEFS="$OZL/build-scripts/make/zldefs.make"
if ! grep -qE '^[[:space:]]*nyx[[:space:]]*$' "$ZLDEFS"; then
  # append `nyx` as the last entry of the CXX_SRCDIRS list
  awk '
    /^CXX_SRCDIRS :=/ { inlist = 1 }
    inlist && !/\\$/ { print $0 " \\"; print "\tnyx"; inlist = 0; next }
    { print }
  ' "$ZLDEFS" > "$ZLDEFS.tmp" && mv "$ZLDEFS.tmp" "$ZLDEFS"
  grep -qE '^[[:space:]]*nyx[[:space:]]*$' "$ZLDEFS" \
    || { echo "FATAL: could not add nyx to CXX_SRCDIRS in $ZLDEFS" >&2; exit 1; }
fi

LOCAL_CMAKE_BIN="$ROOT/.tools/cmake-3.30.5-linux-x86_64/bin"
[ -d "$LOCAL_CMAKE_BIN" ] && export PATH="$LOCAL_CMAKE_BIN:$PATH"
# nproc reports 1 on these nodes because OMP_NUM_THREADS=1 is exported, which
# silently made this a single-threaded build (minutes instead of seconds, and
# long enough to perturb a benchmark running beside it). Prefer the allocation's
# real core count.
JOBS="${SLURM_CPUS_ON_NODE:-$(nproc 2>/dev/null || echo 8)}"

cd "$OZL"
make -j"$JOBS" MOREFLAGS="-pthread" zli >/dev/null 2>&1 || make -j"$JOBS" MOREFLAGS="-pthread" zli
make -j"$JOBS" MOREFLAGS="-pthread" -f nyxfqz_v2.make nyxfqz_v2

[ -x "$OZL/nyxfqz_v2" ] || { echo "FATAL: nyxfqz_v2 build failed" >&2; exit 1; }
echo ">> Done. Binary: $OZL/nyxfqz_v2"
