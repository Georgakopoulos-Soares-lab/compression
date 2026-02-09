#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THREADS="${THREADS:-16}"

SCHEMA="$HERE/schemas/fasta_packed.sddl"

DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
CHUNKS_DIR="$HERE/chunks"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$CHUNKS_DIR"

# 0) Build everything
"$HERE/scripts/build_all.sh"

ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/biocompress_preprocessor"

# 1) Download a big FASTA (or user override FASTA_OUT via env)
"$HERE/scripts/download_fasta.sh"
FASTA_IN="${FASTA_OUT:-$HERE/data/GCF_000001635.27_GRCm39_genomic.fna}"
if [ ! -f "$FASTA_IN" ]; then
  # fallback: use whatever download_fasta.sh printed to disk
  FASTA_IN="$(ls -1 "$HERE/data"/*.fna 2>/dev/null | head -n1 || true)"
fi
if [ -z "$FASTA_IN" ] || [ ! -f "$FASTA_IN" ]; then
  echo "Error: FASTA input not found under $HERE/data"
  exit 1
fi

# 2) Create a ~250MiB FASTA training sample (record-safe)
TRAIN_FASTA="$OUT_DIR/train_250MiB.fasta"
python3 "$HERE/scripts/make_train_sample.py" --in "$FASTA_IN" --out "$TRAIN_FASTA" --target-mib "${TARGET_MIB:-250}"

# 3) Preprocess to FAV4 chunk .bin files that match the schema
rm -rf "$CHUNKS_DIR" && mkdir -p "$CHUNKS_DIR"
"$PRE" "$TRAIN_FASTA" "$CHUNKS_DIR" "$THREADS" fasta_packed

# 4) Train an OpenZL compressor on the preprocessed chunks
COMPRESSOR="$ART_DIR/fasta_packed_250MiB.compressor"
"$ZLI" train "$CHUNKS_DIR" --profile sddl --profile-arg "$SCHEMA" --output "$COMPRESSOR" --force --threads "$THREADS" --use-all-samples

# 5) Compress each chunk in parallel
find "$CHUNKS_DIR" -maxdepth 1 -type f -name '*.fasta_packed.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force > /dev/null' \
    "$ZLI" {} "$COMPRESSOR"

# 6) Decompress + validate: decompressed .bin must match original .bin
FAIL=0
for bin in "$CHUNKS_DIR"/*.fasta_packed.bin; do
  [ -f "$bin" ] || continue
  "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force > /dev/null
  if ! cmp -s "$bin" "$bin.dec"; then
    echo "Mismatch: $bin"
    FAIL=1
  fi
  rm -f "$bin.dec"
done

if [ "$FAIL" = "1" ]; then
  echo "Validation FAILED"
  exit 1
fi

echo "Validation OK: decompressed .bin matches original .bin"

echo "Outputs:"
echo "  Training FASTA: $TRAIN_FASTA"
echo "  Chunks dir:     $CHUNKS_DIR"
echo "  Compressor:     $COMPRESSOR"
