#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- User-tunable knobs ----
THREADS="${THREADS:-16}"
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
TRAIN_CHUNKS_COUNT="${TRAIN_CHUNKS_COUNT:-250}"
DNS_DATA_DIR="${DNS_DATA_DIR:-$HOME/Desktop/openzl-dns}"

# ---- Paths ----
SCHEMA="$HERE/schemas/dns_tlv.sddl"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/dns_tlv_preprocess"

RAW_TAR="$DNS_DATA_DIR/openzl-raw/dns-raw.tar.gz"
EXTRACT_SCRIPT="$HERE/extract_generic_chunks.py"

WORK_DIR="$HERE/dns_work"
RAW_DIR="$WORK_DIR/dns-raw"
EXTRACTED_DIR="$WORK_DIR/dns-extracted"
TLV_DIR="$EXTRACTED_DIR/tlv"

TRAIN_DIR="$WORK_DIR/train"
TEST_DIR="$WORK_DIR/test"
ART_DIR="$HERE/artifacts"
COMPRESSOR="$ART_DIR/dns_tlv_t${THREADS}.compressor"

mkdir -p "$WORK_DIR" "$ART_DIR"

# ---- Helpers ----
bytes_sum() {
  local total=0
  for f in "$@"; do
    [ -f "$f" ] || continue
    sz="$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)"
    total=$(( total + sz ))
  done
  echo "$total"
}

human_kb() {
  python3 -c "print(f'{$1/1024:.1f} KB')"
}

require_exec() {
  if [ ! -x "$1" ]; then
    echo "Error: not executable: $1" >&2
    exit 1
  fi
}

# ---- 0) Build ----
echo "=== Building tools ==="
"$HERE/scripts/build_all.sh"
require_exec "$ZLI"
require_exec "$PRE"

# ---- 1) Extract raw GenericChunks (if needed) ----
if [ ! -d "$RAW_DIR" ] || [ -z "$(ls -A "$RAW_DIR" 2>/dev/null)" ]; then
  echo "=== Extracting dns-raw.tar.gz ==="
  if [ ! -f "$RAW_TAR" ]; then
    echo "Error: $RAW_TAR not found" >&2
    echo "Set DNS_DATA_DIR to the directory containing openzl-raw/dns-raw.tar.gz" >&2
    exit 1
  fi
  tar -xzf "$RAW_TAR" -C "$WORK_DIR"
fi

# ---- 2) Decompose GenericChunks into TLV payloads (if needed) ----
if [ ! -d "$TLV_DIR" ] || [ -z "$(ls -A "$TLV_DIR" 2>/dev/null)" ]; then
  echo "=== Extracting TLV payloads from GenericChunks ==="
  python3 "$EXTRACT_SCRIPT" \
    --input-dir "$RAW_DIR" \
    --output-dir "$EXTRACTED_DIR"
fi

TLV_COUNT="$(ls -1 "$TLV_DIR"/*.tlv 2>/dev/null | wc -l | tr -d ' ')"
echo "TLV chunks available: $TLV_COUNT"

if [ "$TLV_COUNT" -eq 0 ]; then
  echo "Error: no .tlv files in $TLV_DIR" >&2
  exit 1
fi

# ---- 3) Analyze first few chunks ----
echo ""
echo "=== Analyzing sample TLV chunks ==="
SAMPLE_TLV="$(ls -1 "$TLV_DIR"/*.tlv | head -1)"
"$PRE" analyze "$SAMPLE_TLV"

# ---- 4) Preprocess: split into train/test and encode ----
echo ""
echo "=== Preprocessing TLV → columnar binary ==="
rm -rf "$TRAIN_DIR" "$TEST_DIR"
mkdir -p "$TRAIN_DIR" "$TEST_DIR"

# Split: first TRAIN_CHUNKS_COUNT for training, rest for testing
i=0
for tlv in "$TLV_DIR"/*.tlv; do
  base="$(basename "$tlv" .tlv)"
  if [ "$i" -lt "$TRAIN_CHUNKS_COUNT" ]; then
    "$PRE" encode "$tlv" "$TRAIN_DIR/$base" 2>/dev/null
  else
    "$PRE" encode "$tlv" "$TEST_DIR/$base" 2>/dev/null
  fi
  i=$((i + 1))
done

TRAIN_BINS=("$TRAIN_DIR"/*.dtlv.bin)
TEST_BINS=("$TEST_DIR"/*.dtlv.bin)
TRAIN_BIN_COUNT="${#TRAIN_BINS[@]}"
TEST_BIN_COUNT="${#TEST_BINS[@]}"
echo "Training chunks: $TRAIN_BIN_COUNT"
echo "Test chunks: $TEST_BIN_COUNT"

# ---- 5) Validate round-trip on a few training chunks ----
echo ""
echo "=== Validating round-trip (encode → decode → cmp) ==="
VALIDATE_COUNT=0
VALIDATE_FAIL=0
for tlv in $(ls -1 "$TLV_DIR"/*.tlv | head -10); do
  base="$(basename "$tlv" .tlv)"
  prefix="$TRAIN_DIR/$base"
  if [ ! -f "$prefix.dtlv.bin" ]; then continue; fi

  dec="$prefix.roundtrip.tlv"
  "$PRE" decode "$prefix" "$dec" 2>/dev/null

  if cmp -s "$tlv" "$dec"; then
    VALIDATE_COUNT=$((VALIDATE_COUNT + 1))
  else
    echo "  MISMATCH: $tlv vs $dec" >&2
    VALIDATE_FAIL=$((VALIDATE_FAIL + 1))
  fi
  rm -f "$dec"
done

if [ "$VALIDATE_FAIL" -gt 0 ]; then
  echo "Round-trip FAILED ($VALIDATE_FAIL/$((VALIDATE_COUNT + VALIDATE_FAIL)) mismatches)" >&2
  exit 1
fi
echo "Round-trip OK ($VALIDATE_COUNT chunks validated)"

# ---- 6) Train compressor ----
echo ""
echo "=== Training OpenZL compressor ==="

# SDDL training requires all files to have the same num_records (column dimensions).
# Different chunks have different record counts, so train on the single largest chunk.
TRAIN_BIN_DIR="$WORK_DIR/train_bins"
rm -rf "$TRAIN_BIN_DIR" && mkdir -p "$TRAIN_BIN_DIR"

LARGEST_BIN=""
LARGEST_SZ=0
for f in "$TRAIN_DIR"/*.dtlv.bin; do
  sz="$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null)"
  if [ "$sz" -gt "$LARGEST_SZ" ]; then
    LARGEST_SZ="$sz"
    LARGEST_BIN="$f"
  fi
done
cp "$LARGEST_BIN" "$TRAIN_BIN_DIR/"

echo "  Training on largest chunk ($(basename "$LARGEST_BIN"), ${LARGEST_SZ} bytes), threads=$THREADS, max_time=$MAX_TIME_SECS sec"
"$ZLI" train "$TRAIN_BIN_DIR" \
  --profile sddl --profile-arg "$SCHEMA" \
  --output "$COMPRESSOR" --force \
  --threads "$THREADS" --use-all-samples \
  --max-time-secs "$MAX_TIME_SECS"

if [ ! -f "$COMPRESSOR" ]; then
  echo "Error: training did not produce $COMPRESSOR" >&2
  exit 1
fi
echo "Compressor: $COMPRESSOR"

# ---- 7) Compress test chunks (or all if no test split) ----
echo ""
echo "=== Compressing test chunks ==="

EVAL_DIR="$TEST_DIR"
EVAL_BINS=("${TEST_BINS[@]}")
if [ "$TEST_BIN_COUNT" -eq 0 ]; then
  echo "  (No test split — evaluating on training chunks)"
  EVAL_DIR="$TRAIN_DIR"
  EVAL_BINS=("${TRAIN_BINS[@]}")
fi

find "$EVAL_DIR" -maxdepth 1 -type f -name '*.dtlv.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force > /dev/null' \
    "$ZLI" {} "$COMPRESSOR"

# ---- 8) Measure results ----
echo ""
echo "=== Results ==="

# Columnar bin sizes
EVAL_ZLS=("$EVAL_DIR"/*.dtlv.bin.zl)
eval_zl_bytes="$(bytes_sum "${EVAL_ZLS[@]}")"
eval_bin_bytes="$(bytes_sum "${EVAL_BINS[@]}")"

# Original TLV sizes (match against the chunks we tested)
orig_tlv_bytes=0
for zl in "${EVAL_ZLS[@]}"; do
  base="$(basename "$zl" .dtlv.bin.zl)"
  tlv="$TLV_DIR/${base}.tlv"
  if [ -f "$tlv" ]; then
    sz="$(stat -f%z "$tlv" 2>/dev/null || stat -c%s "$tlv" 2>/dev/null)"
    orig_tlv_bytes=$((orig_tlv_bytes + sz))
  fi
done

num_eval="${#EVAL_ZLS[@]}"

python3 - <<PY
import sys
orig_b = $orig_tlv_bytes
col_b = $eval_bin_bytes
comp_b = $eval_zl_bytes
n = $num_eval

print(f"Chunks evaluated: {n}")
print(f"")
print(f"Original TLV:     {orig_b:>12,} bytes  ({orig_b/1024:.1f} KB)  avg {orig_b/n/1024:.1f} KB/chunk")
print(f"Columnar binary:  {col_b:>12,} bytes  ({col_b/1024:.1f} KB)  avg {col_b/n/1024:.1f} KB/chunk")
print(f"OpenZL compressed:{comp_b:>12,} bytes  ({comp_b/1024:.1f} KB)  avg {comp_b/n/1024:.1f} KB/chunk")
print(f"")

if comp_b > 0:
    ratio_orig = orig_b / comp_b
    ratio_col = col_b / comp_b
    print(f"Ratio vs original TLV: {ratio_orig:.2f}x")
    print(f"Ratio vs columnar:     {ratio_col:.2f}x")
    print(f"")

snappy_avg = 35 * 1024  # 35 KB per chunk baseline
avg_comp = comp_b / n if n > 0 else 0
if avg_comp > 0:
    print(f"--- Comparison vs Snappy baseline (35 KB/chunk) ---")
    print(f"Snappy avg:            {snappy_avg:>12,} bytes  ({snappy_avg/1024:.1f} KB/chunk)")
    print(f"OpenZL avg:            {int(avg_comp):>12,} bytes  ({avg_comp/1024:.1f} KB/chunk)")
    if avg_comp < snappy_avg:
        improvement = (1 - avg_comp / snappy_avg) * 100
        print(f"OpenZL beats Snappy by {improvement:.1f}%")
    else:
        overhead = (avg_comp / snappy_avg - 1) * 100
        print(f"OpenZL is {overhead:.1f}% larger than Snappy (need more tuning)")
PY

# ---- 9) Full pipeline validation ----
echo ""
echo "=== Full pipeline validation (compress → decompress → decode → cmp) ==="
FULL_FAIL=0
FULL_OK=0

for zl in $(ls -1 "$EVAL_DIR"/*.dtlv.bin.zl | head -10); do
  bin="${zl%.zl}"
  base="$(basename "$bin" .dtlv.bin)"
  prefix="$EVAL_DIR/$base"
  orig_tlv="$TLV_DIR/${base}.tlv"

  if [ ! -f "$orig_tlv" ]; then continue; fi

  # Decompress
  "$ZLI" decompress "$zl" --output "$bin.dec" --force > /dev/null

  # Verify decompressed matches columnar
  if ! cmp -s "$bin" "$bin.dec"; then
    echo "  Decompress mismatch: $bin" >&2
    FULL_FAIL=$((FULL_FAIL + 1))
    rm -f "$bin.dec"
    continue
  fi
  rm -f "$bin.dec"

  # Decode back to TLV
  dec_tlv="$prefix.pipeline.tlv"
  "$PRE" decode "$prefix" "$dec_tlv" 2>/dev/null

  if cmp -s "$orig_tlv" "$dec_tlv"; then
    FULL_OK=$((FULL_OK + 1))
  else
    echo "  Pipeline mismatch: $orig_tlv vs $dec_tlv" >&2
    FULL_FAIL=$((FULL_FAIL + 1))
  fi
  rm -f "$dec_tlv"
done

if [ "$FULL_FAIL" -gt 0 ]; then
  echo "Full pipeline validation FAILED ($FULL_FAIL failures)" >&2
  exit 1
fi
echo "Full pipeline validation OK ($FULL_OK chunks)"
echo ""
echo "Done. Compressor: $COMPRESSOR"
