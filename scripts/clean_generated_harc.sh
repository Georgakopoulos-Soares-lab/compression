#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

shopt -s nullglob

# Generated outputs specific to the HARC comparison pipeline.
TARGETS=(
  "$HERE"/chunks_train_harc*
  "$HERE"/chunks_full_harc
  "$HERE"/artifacts/harc_packed_*.compressor
  "$HERE"/out/harc_alone_*
  "$HERE"/out/plain_*
  "$HERE"/out/timing/harc_*.time
  "$HERE"/out/timing/plain_*.time
)

# Locally built binaries (rebuildable via scripts/build_harc.sh)
TARGETS+=(
  "$HERE/tools/harc_preprocessor"
  "$HERE/tools/harc_decode"
)

for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned HARC-generated outputs under: $HERE"
echo "Kept: everything clean_generated.sh already keeps, plus schemas/harc_packed.sddl,"
echo "      tools/harc_preprocessor.cpp, tools/harc_decode.cpp, tools/harc_common.h"
echo
echo "Note: this does NOT clean the shared FAV4/baseline outputs (data/, out/train_*.fasta,"
echo "chunks_full/, artifacts/fasta_packed_*.compressor) since HARC's training/full FASTA"
echo "inputs are reused from them. Run scripts/clean_generated.sh separately for those,"
echo "or 'bash scripts/clean_generated.sh && bash scripts/clean_generated_harc.sh' for both."
