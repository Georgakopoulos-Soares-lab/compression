#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
JSON_TEST_DIR="$HERE/json_test"
UNIVERSAL_DIR="$JSON_TEST_DIR/universal"
SCRIPTS_DIR="$UNIVERSAL_DIR/scripts"
TOOLS_DIR="$UNIVERSAL_DIR/tools"
ZLI="$HERE/openzl/zli"
PRE="$TOOLS_DIR/geojson_to_bin_universal"

# ---- Arguments ----
if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <input_geojson> [threads] [chunk_mb]"
    exit 1
fi

INPUT_JSON="$1"
THREADS="${2:-8}"
CHUNK_MB="${3:-100}"
BASENAME=$(basename "$INPUT_JSON" .json)

# Experiment Directory is where the input JSON lives
EXP_DIR="$(cd "$(dirname "$INPUT_JSON")" && pwd)"

# ---- Derived Paths ----
OUT_SDDL="$EXP_DIR/${BASENAME}_schema.sddl"
OUT_MAP="$EXP_DIR/${BASENAME}_mapping.json"
TRAIN_SAMPLE="$EXP_DIR/${BASENAME}_train.json"
TRAIN_CHUNKS="$EXP_DIR/chunks_train"
FULL_CHUNKS="$EXP_DIR/chunks_full"
COMPRESSOR="$EXP_DIR/${BASENAME}.compressor"
TIME_DIR="$EXP_DIR/timing"

mkdir -p "$TIME_DIR"

# ---- Helpers ----
time_cmd() {
  local outfile="$1"
  shift
  if [[ "$OSTYPE" == "darwin"* ]]; then
    /usr/bin/time -l -p "$@" 2> "$outfile"
  else
    /usr/bin/time -f "elapsed_sec=%e maxrss_kb=%M" -o "$outfile" "$@"
  fi
}

get_time_sec() {
    grep "real" "$1" | awk '{print $2}'
}

get_file_size() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    stat -f%z "$1"
  else
    stat -c%s "$1"
  fi
}

bytes_sum() {
  local total=0
  local f sz
  for f in "$@"; do
    [ -f "$f" ] || continue
    sz="$(get_file_size "$f")"
    total=$(( total + sz ))
  done
  echo "$total"
}

human_mib() {
  python3 -c "print(f'{int($1)/1024/1024:.2f} MiB')"
}

calc_speed() {
    local bytes=$1
    local sec=$2
    python3 -c "print(f'{($bytes/1024/1024)/$sec:.2f} MB/s')"
}

# ---- 0) Build ----
bash "$SCRIPTS_DIR/build.sh" > /dev/null 2>&1

echo "================================================================"
echo "Dataset: $BASENAME"
orig_bytes="$(get_file_size "$INPUT_JSON")"
echo "Size:    $(human_mib "$orig_bytes")"
echo "================================================================"

# ---- 1) Scan Schema ----
echo "[1/7] Scanning schema..."
python3 "$TOOLS_DIR/scan_geojson_schema.py" \
    --in "$INPUT_JSON" \
    --out-sddl "$OUT_SDDL" \
    --out-map "$OUT_MAP" \
    --sample 5000

# ---- 2) Create Training Sample ----
if [ ! -f "$TRAIN_SAMPLE" ]; then
    echo "[2/7] Creating training sample..."
    python3 "$SCRIPTS_DIR/make_json_train_sample.py" --in "$INPUT_JSON" --out "$TRAIN_SAMPLE" --target-mib 20 > /dev/null
fi

# ---- 3) Preprocess Training ----
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
echo "[3/7] Preprocessing training sample..."
"$PRE" "$TRAIN_SAMPLE" "$OUT_MAP" "$TRAIN_CHUNKS" "$CHUNK_MB" > /dev/null

# ---- 4) Train ----
echo "[4/7] Training OpenZL model..."
time_cmd "$TIME_DIR/train.time" \
  "$ZLI" train "$TRAIN_CHUNKS" \
    --profile sddl --profile-arg "$OUT_SDDL" \
    --output "$COMPRESSOR" --force \
    --threads "$THREADS" --use-all-samples \
    --max-time-secs 300 > "$TIME_DIR/train.log" 2>&1

# ---- 5) Preprocess Full ----
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
echo "[5/7] Preprocessing FULL JSON..."
time_cmd "$TIME_DIR/prep_full.time" \
  "$PRE" "$INPUT_JSON" "$OUT_MAP" "$FULL_CHUNKS" "$CHUNK_MB" > /dev/null

FULL_BINS=("$FULL_CHUNKS"/*.bin)
full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
prep_time=$(get_time_sec "$TIME_DIR/prep_full.time")
prep_speed=$(calc_speed "$orig_bytes" "$prep_time")

# ---- 6) Compress Full ----
echo "[6/7] Compressing with OpenZL..."
time_cmd "$TIME_DIR/comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$THREADS" "$ZLI" "$COMPRESSOR"

FULL_ZLS=("$FULL_CHUNKS"/*.bin.zl)
full_out_bytes="$(bytes_sum "${FULL_ZLS[@]}")"
comp_time=$(get_time_sec "$TIME_DIR/comp_full.time")
comp_speed=$(calc_speed "$full_in_bytes" "$comp_time")

# ---- 7) Zstd Baseline ----
echo "[7/7] Running Zstd..."
if command -v zstd >/dev/null; then
    ZSTD_OUT="$EXP_DIR/${BASENAME}.json.zst"
    time_cmd "$TIME_DIR/zstd.time" zstd -9 -f -c "$INPUT_JSON" > "$ZSTD_OUT"
    zstd_bytes="$(get_file_size "$ZSTD_OUT")"
    zstd_time=$(get_time_sec "$TIME_DIR/zstd.time")
    zstd_speed=$(calc_speed "$orig_bytes" "$zstd_time")
else
    zstd_bytes=0; zstd_time=0; zstd_speed="N/A"
fi

# ---- 8) Validation ----
echo "[8/8] Validating Decompression..."
# Decompress all .zl files
bash -c 'find "$1" -maxdepth 1 -type f -name "*.zl" -print0 | \
    xargs -0 -P "$2" -I {} "$3" decompress "{}" --output "{}.dec" --force' \
    _ "$FULL_CHUNKS" "$THREADS" "$ZLI" > /dev/null

# Compare original bins with decompressed bins
echo "Verifying integrity..."
VALIDATION_FAIL=0
for orig in "$FULL_CHUNKS"/*.bin; do
    dec="$orig.zl.dec"
    if [ ! -f "$dec" ]; then
        echo "ERROR: Decompressed file missing for $orig"
        VALIDATION_FAIL=1
        break
    fi
    if ! cmp -s "$orig" "$dec"; then
        echo "ERROR: Validation failed for $orig"
        VALIDATION_FAIL=1
        break
    fi
done

if [ "$VALIDATION_FAIL" -eq 0 ]; then
    echo "Validation OK: All files match."
    # Clean up decompressed files to save space
    rm -f "$FULL_CHUNKS"/*.dec
else
    echo "Validation FAILED."
    exit 1
fi

# ---- Report ----
echo ""
echo "================================================================"
echo "FINAL RESULTS REPORT: $BASENAME"
echo "================================================================"
echo ""
echo "1. COMPRESSION RATIO"
echo "--------------------"
printf "%-15s | %-12s | %-10s\n" "Method" "Size" "Ratio"
echo "----------------|--------------|-----------"
printf "%-15s | %-12s | %-10s\n" "Original JSON" "$(human_mib "$orig_bytes")" "1.00x"
if [ "$zstd_bytes" -gt 0 ]; then
    ratio=$(python3 -c "print(f'{${orig_bytes}/${zstd_bytes}:.2f}')")
    printf "%-15s | %-12s | %-10sx\n" "Zstd -9" "$(human_mib "$zstd_bytes")" "$ratio"
fi
ratio=$(python3 -c "print(f'{${orig_bytes}/${full_out_bytes}:.2f}')")
printf "%-15s | %-12s | %-10sx\n" "OpenZL (Total)" "$(human_mib "$full_out_bytes")" "$ratio"
echo ""
echo "2. SPEED"
echo "--------"
printf "%-25s | %-10s | %-10s\n" "Stage" "Time" "Speed"
echo "--------------------------|------------|-----------"
if [ "$zstd_bytes" -gt 0 ]; then
    printf "%-25s | %-10ss | %-10s\n" "Zstd (End-to-End)" "$zstd_time" "$zstd_speed"
fi
printf "%-25s | %-10ss | %-10s\n" "OpenZL: Preprocess" "$prep_time" "$prep_speed"
printf "%-25s | %-10ss | %-10s\n" "OpenZL: Compress" "$comp_time" "$comp_speed"
total_time=$(python3 -c "print(f'{float($prep_time) + float($comp_time):.2f}')")
total_speed=$(calc_speed "$orig_bytes" "$total_time")
printf "%-25s | %-10ss | %-10s\n" "OpenZL (End-to-End)" "$total_time" "$total_speed"
echo ""
echo "================================================================"
