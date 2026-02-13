#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
JSON_TEST_DIR="$HERE/json_test"
DATA_DIR="$JSON_TEST_DIR/data"
SCRIPTS_DIR="$JSON_TEST_DIR/scripts"
TOOLS_DIR="$JSON_TEST_DIR/tools"
SCHEMA="$JSON_TEST_DIR/schemas/citylots_packed.sddl"
ZLI="$HERE/openzl/zli"
PRE="$TOOLS_DIR/json_to_bin_cpp"

# ---- User-tunable knobs ----
THREADS="${THREADS:-8}"
TARGET_MIB="${TARGET_MIB:-20}"  # Small training sample (20MB)
MAX_TIME_SECS="${MAX_TIME_SECS:-300}"
CHUNK_MB="100" # Chunk size for binary output

# ---- Paths ----
JSON_IN="$DATA_DIR/citylots.json"
TRAIN_JSON="$DATA_DIR/train_${TARGET_MIB}MiB.json"
TRAIN_CHUNKS="$JSON_TEST_DIR/chunks_train"
FULL_CHUNKS="$JSON_TEST_DIR/chunks_full"
COMPRESSOR="$DATA_DIR/citylots_train_${TARGET_MIB}MiB.compressor"
TIME_DIR="$JSON_TEST_DIR/timing"

mkdir -p "$DATA_DIR" "$TIME_DIR"

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
    # Extract 'real 1.23' from time output
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
bash "$SCRIPTS_DIR/build_json_tools.sh" > /dev/null 2>&1

# ---- 1) Check Data ----
"$SCRIPTS_DIR/download_json.sh" > /dev/null
orig_bytes="$(get_file_size "$JSON_IN")"
echo "----------------------------------------------------------------"
echo "Dataset: $(basename "$JSON_IN")"
echo "Size:    $(human_mib "$orig_bytes")"
echo "----------------------------------------------------------------"

# ---- 2) Create Training Sample ----
if [ ! -f "$TRAIN_JSON" ]; then
    echo "[1/6] Creating training sample (~${TARGET_MIB} MiB)..."
    python3 "$SCRIPTS_DIR/make_json_train_sample.py" --in "$JSON_IN" --out "$TRAIN_JSON" --target-mib "$TARGET_MIB" > /dev/null
else
    echo "[1/6] Using existing training sample."
fi

# ---- 3) Preprocess Training Sample ----
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
echo "[2/6] Preprocessing training sample..."
"$PRE" "$TRAIN_JSON" "$TRAIN_CHUNKS" "$CHUNK_MB" > /dev/null

TRAIN_BINS=("$TRAIN_CHUNKS"/*.bin)
train_bin_bytes="$(bytes_sum "${TRAIN_BINS[@]}")"

# ---- 4) Train Compressor ----
echo "[3/6] Training OpenZL model..."
time_cmd "$TIME_DIR/train.time" \
  "$ZLI" train "$TRAIN_CHUNKS" \
    --profile sddl --profile-arg "$SCHEMA" \
    --output "$COMPRESSOR" --force \
    --threads "$THREADS" --use-all-samples \
    --max-time-secs "$MAX_TIME_SECS" > "$TIME_DIR/train.log" 2>&1

# ---- 5) Preprocess Full JSON ----
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
echo "[4/6] Preprocessing FULL JSON to Binary Columns..."
time_cmd "$TIME_DIR/prep_full.time" \
  "$PRE" "$JSON_IN" "$FULL_CHUNKS" "$CHUNK_MB" > /dev/null

FULL_BINS=("$FULL_CHUNKS"/*.bin)
full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
prep_time=$(get_time_sec "$TIME_DIR/prep_full.time")
prep_speed=$(calc_speed "$orig_bytes" "$prep_time")

# ---- 6) Compress Full Chunks ----
echo "[5/6] Compressing Binary Columns with OpenZL..."
time_cmd "$TIME_DIR/comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$THREADS" "$ZLI" "$COMPRESSOR"

FULL_ZLS=("$FULL_CHUNKS"/*.bin.zl)
full_out_bytes="$(bytes_sum "${FULL_ZLS[@]}")"
comp_time=$(get_time_sec "$TIME_DIR/comp_full.time")
comp_speed=$(calc_speed "$full_in_bytes" "$comp_time")

# ---- 7) Pigz Baseline ----
echo "[6/6] Running Pigz Baseline..."
if command -v pigz >/dev/null; then
    PIGZ_OUT="$DATA_DIR/citylots.json.gz"
    time_cmd "$TIME_DIR/pigz.time" pigz -9 -k -f -c "$JSON_IN" > "$PIGZ_OUT"
    pigz_bytes="$(get_file_size "$PIGZ_OUT")"
    pigz_time=$(get_time_sec "$TIME_DIR/pigz.time")
    pigz_speed=$(calc_speed "$orig_bytes" "$pigz_time")
else
    echo "pigz not found, skipping."
    pigz_bytes=0
    pigz_time=0
    pigz_speed="N/A"
fi

# ---- 8) Report ----
echo ""
echo "================================================================"
echo "FINAL RESULTS REPORT"
echo "================================================================"
echo ""
echo "1. COMPRESSION RATIO (Higher is Better)"
echo "---------------------------------------"
printf "%-15s | %-12s | %-10s\n" "Method" "Size" "Ratio"
echo "----------------|--------------|-----------"
printf "%-15s | %-12s | %-10s\n" "Original JSON" "$(human_mib "$orig_bytes")" "1.00x"
if [ "$pigz_bytes" -gt 0 ]; then
    ratio=$(python3 -c "print(f'{${orig_bytes}/${pigz_bytes}:.2f}')")
    printf "%-15s | %-12s | %-10sx\n" "Pigz -9" "$(human_mib "$pigz_bytes")" "$ratio"
fi
ratio=$(python3 -c "print(f'{${orig_bytes}/${full_out_bytes}:.2f}')")
printf "%-15s | %-12s | %-10sx\n" "OpenZL (Total)" "$(human_mib "$full_out_bytes")" "$ratio"
echo ""

echo "2. SPEED (MB/s processed)"
echo "-------------------------"
printf "%-25s | %-10s | %-10s\n" "Stage" "Time" "Speed"
echo "--------------------------|------------|-----------"
if [ "$pigz_bytes" -gt 0 ]; then
    printf "%-25s | %-10ss | %-10s\n" "Pigz (End-to-End)" "$pigz_time" "$pigz_speed"
fi
printf "%-25s | %-10ss | %-10s\n" "OpenZL: Preprocess" "$prep_time" "$prep_speed"
printf "%-25s | %-10ss | %-10s\n" "OpenZL: Compress" "$comp_time" "$comp_speed"
total_time=$(python3 -c "print(f'{float($prep_time) + float($comp_time):.2f}')")
total_speed=$(calc_speed "$orig_bytes" "$total_time")
printf "%-25s | %-10ss | %-10s\n" "OpenZL (End-to-End)" "$total_time" "$total_speed"
echo ""
echo "================================================================"
