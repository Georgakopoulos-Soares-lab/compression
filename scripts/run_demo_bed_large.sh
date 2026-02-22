#!/usr/bin/env bash
# Run the BED pipeline on the UCSC RepeatMasker BED (~208 MB). Train on a ~50 MiB sample, then compress the full file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THREADS="${THREADS:-8}"
TRAIN_MIB="${TRAIN_MIB:-50}"

SCHEMA="$HERE/schemas/bed.sddl"
DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
CHUNKS_TRAIN_DIR="$HERE/chunks_bed_large_train"
CHUNKS_DIR="$HERE/chunks_bed_large"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$CHUNKS_TRAIN_DIR" "$CHUNKS_DIR"

# 0) Build
"$HERE/scripts/build_all.sh"

ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/biocompress_preprocessor"

# 1) Get full BED (~208 MB via download_bed_large.sh)
. "$HERE/scripts/download_bed_large.sh"
BED_FULL="${BED_LARGE:-}"
if [ -z "${BED_FULL:-}" ] || [ ! -f "$BED_FULL" ]; then
  echo "Error: BED not found. Run download_bed_large.sh first or set BED_LARGE."
  exit 1
fi
ORIG_BYTES=$(stat -f%z "$BED_FULL" 2>/dev/null || stat -c%s "$BED_FULL")
echo "Full BED: $BED_FULL ($ORIG_BYTES bytes)"

# 2) Create ~50 MiB training sample (line-safe)
TRAIN_BED="$OUT_DIR/train_${TRAIN_MIB}MiB_rmsk.bed"
python3 "$HERE/scripts/make_train_sample_bed.py" --in "$BED_FULL" --out "$TRAIN_BED" --target-mib "$TRAIN_MIB"
TRAIN_BYTES=$(stat -f%z "$TRAIN_BED" 2>/dev/null || stat -c%s "$TRAIN_BED")
echo "Training sample: $TRAIN_BED ($TRAIN_BYTES bytes)"

# 3) Preprocess training sample to chunks, then train on them only
rm -rf "$CHUNKS_TRAIN_DIR" && mkdir -p "$CHUNKS_TRAIN_DIR"
"$PRE" "$TRAIN_BED" "$CHUNKS_TRAIN_DIR" "$THREADS" bed
COMPRESSOR="$ART_DIR/bed_rmsk_208MB.compressor"
echo "Training compressor on ~${TRAIN_MIB} MiB of chunks..."
"$ZLI" train "$CHUNKS_TRAIN_DIR" --profile sddl --profile-arg "$SCHEMA" --output "$COMPRESSOR" --force --threads "$THREADS" --use-all-samples

# 4) Preprocess full 208 MB BED to chunks
rm -rf "$CHUNKS_DIR" && mkdir -p "$CHUNKS_DIR"
"$PRE" "$BED_FULL" "$CHUNKS_DIR" "$THREADS" bed

CHUNKS_BYTES=0
for f in "$CHUNKS_DIR"/*.bed.bin; do
  [ -f "$f" ] || continue
  CHUNKS_BYTES=$((CHUNKS_BYTES + $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")))
done

# 5) Compress full-file chunks with the trained compressor
for bin in "$CHUNKS_DIR"/*.bed.bin; do
  [ -f "$bin" ] || continue
  "$ZLI" compress "$bin" --compressor "$COMPRESSOR" --output "$bin.zl" --force > /dev/null
done

# 6) Decompress and validate
FAIL=0
for bin in "$CHUNKS_DIR"/*.bed.bin; do
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

# 7) Report sizes
COMPRESSED_BYTES=0
for f in "$CHUNKS_DIR"/*.bed.bin.zl; do
  [ -f "$f" ] || continue
  COMPRESSED_BYTES=$((COMPRESSED_BYTES + $(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")))
done

echo ""
echo "=== BED compression results (trained on ~${TRAIN_MIB} MiB, compressed full ~208 MB) ==="
echo "  Original BED:      $ORIG_BYTES bytes"
echo "  Preprocessed:      $CHUNKS_BYTES bytes (chunks)"
echo "  Compressed (.zl):  $COMPRESSED_BYTES bytes"
if [ "$CHUNKS_BYTES" -gt 0 ] && [ "$COMPRESSED_BYTES" -gt 0 ]; then
  RATIO=$(echo "scale=2; $CHUNKS_BYTES / $COMPRESSED_BYTES" | bc 2>/dev/null || echo "N/A")
  echo "  Chunk -> .zl:      ${RATIO}x compression"
fi
if [ "$ORIG_BYTES" -gt 0 ] && [ "$COMPRESSED_BYTES" -gt 0 ]; then
  RATIO_ORIG=$(echo "scale=2; $ORIG_BYTES / $COMPRESSED_BYTES" | bc 2>/dev/null || echo "N/A")
  echo "  Original BED -> .zl: ${RATIO_ORIG}x compression"
fi
echo ""
echo "  Training sample: $TRAIN_BED (~${TRAIN_MIB} MiB)"
echo "  Compressor:      $COMPRESSOR"
echo "  Full-file chunks: $CHUNKS_DIR"
