#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# LiDAR Compression Pipeline
# ==============================================================================

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---- User-tunable knobs (via env) ----
THREADS="${THREADS:-16}"
TARGET_MIB="${TARGET_MIB:-200}"
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
CHUNK_SIZE_MB="${CHUNK_SIZE_MB:-20}"
TOTAL_DATA_MIB="${TOTAL_DATA_MIB:-2048}"

COMPRESS_JOBS="${COMPRESS_JOBS:-$THREADS}"
NO_ACE_SUCCESSORS="${NO_ACE_SUCCESSORS:-1}"
VALIDATE_FULL="${VALIDATE_FULL:-1}"

# ---- Paths ----
SCHEMA="$HERE/schemas/lidar.sddl"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/lidar_preprocessor"

DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
TIME_DIR="$OUT_DIR/timing"

TRAIN_CHUNKS="$HERE/chunks_train_lidar"
FULL_CHUNKS="$HERE/chunks_full_lidar"

COMPRESSOR="$ART_DIR/lidar_train_${TARGET_MIB}MiB_t${THREADS}.compressor"

RAW_INPUT="$DATA_DIR/lidar_large_raw.bin"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$TIME_DIR"

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

get_file_size() {
  local f="$1"
  if [[ "$OSTYPE" == "darwin"* ]]; then
    stat -f%z "$f"
  else
    stat -c%s "$f"
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
  python3 - "$1" <<'PY'
import sys
n = int(sys.argv[1])
print(f"{n/1024/1024:.2f} MiB")
PY
}

require_exec() {
  local p="$1"
  if [ ! -x "$p" ]; then
    echo "Error: not executable: $p" >&2
    exit 1
  fi
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

# ---- 0) Build ----
echo "---- 0) Build ----"
"$HERE/scripts/common/build_all.sh"
require_exec "$ZLI"
require_exec "$PRE"

# ---- 1) Prepare full LiDAR input ----
echo "---- 1) Prepare full LiDAR input ----"
if [ ! -f "$RAW_INPUT" ]; then
    echo "Generating raw dataset from KITTI frames..."
    "$HERE/scripts/lidar/download_kitti.sh"
    RAW_DIR="$DATA_DIR/2011_09_26/2011_09_26_drive_0002_sync/velodyne_points/data"
    if [ ! -d "$RAW_DIR" ]; then
        echo "Error: KITTI data not found." >&2
        exit 1
    fi
    UNIT="$DATA_DIR/_unit.bin"
    cat "$RAW_DIR"/*.bin > "$UNIT"
    rm -f "$RAW_INPUT"
    TARGET_BYTES=$((TOTAL_DATA_MIB * 1024 * 1024))
    while [ "$(get_file_size "$RAW_INPUT" 2>/dev/null || echo 0)" -lt "$TARGET_BYTES" ]; do
        cat "$UNIT" >> "$RAW_INPUT"
    done
    rm -f "$UNIT"
fi

if [ ! -f "$RAW_INPUT" ]; then
    echo "Error: LiDAR input not found: $RAW_INPUT" >&2
    exit 1
fi

orig_bytes="$(get_file_size "$RAW_INPUT")"
echo "Full LiDAR input: $RAW_INPUT"
echo "Original LiDAR size: $(human_mib "$orig_bytes")"

# ---- 2) Preprocess training sample ----
echo "---- 2) Preprocess training sample (~${TARGET_MIB} MiB) ----"
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
TRAIN_RAW="$OUT_DIR/train_lidar_${TARGET_MIB}MiB.bin"
python3 "$HERE/scripts/lidar/make_train_sample.py" --in "$RAW_INPUT" --out "$TRAIN_RAW" --target-mib "$TARGET_MIB"

echo "Preprocessing training sample -> packed chunks: $TRAIN_CHUNKS"
"$PRE" "$TRAIN_RAW" "$TRAIN_CHUNKS" "$CHUNK_SIZE_MB"

TRAIN_BINS=("$TRAIN_CHUNKS"/*.lidar.bin)
if [ ! -f "${TRAIN_BINS[0]}" ]; then
  echo "Error: no training .bin chunks produced in $TRAIN_CHUNKS" >&2
  exit 1
fi

train_bin_bytes="$(bytes_sum "${TRAIN_BINS[@]}")"
echo "Training packed payload size: $(human_mib "$train_bin_bytes")"

# ---- 3) Train compressor ----
echo "---- 3) Train compressor (threads=$THREADS, max_time_secs=$MAX_TIME_SECS) ----"
echo "Output: $COMPRESSOR"
time_cmd "$TIME_DIR/train.time" \
  "$ZLI" train "$TRAIN_CHUNKS" \
    --profile sddl --profile-arg "$SCHEMA" \
    --output "$COMPRESSOR" --force \
    --threads "$THREADS" --use-all-samples \
    --max-time-secs "$MAX_TIME_SECS" \
    $( [ "$NO_ACE_SUCCESSORS" = "1" ] && echo "--no-ace-successors" )

if [ ! -f "$COMPRESSOR" ]; then
  echo "Error: training did not produce compressor: $COMPRESSOR" >&2
  exit 1
fi

# ---- 4) Validate on training chunks ----
echo "---- 4) Validate compressor on training chunks ----"
find "$TRAIN_CHUNKS" -maxdepth 1 -type f -name '*.lidar.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force > /dev/null' \
    "$ZLI" {} "$COMPRESSOR"

FAIL=0
for bin in "$TRAIN_CHUNKS"/*.lidar.bin; do
  [ -f "$bin" ] || continue
  "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force > /dev/null
  if ! cmp -s "$bin" "$bin.dec"; then
    echo "Mismatch (training): $bin" >&2
    FAIL=1
  fi
  rm -f "$bin.dec"
done

if [ "$FAIL" = "1" ]; then
  echo "Training-set validation FAILED" >&2
  exit 1
fi

echo "Training-set validation OK"

# ---- 5) Preprocess full LiDAR ----
echo "---- 5) Preprocess FULL LiDAR -> packed chunks ----"
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
time_cmd "$TIME_DIR/prep_full.time" \
  "$PRE" "$RAW_INPUT" "$FULL_CHUNKS" "$CHUNK_SIZE_MB"

FULL_BINS=("$FULL_CHUNKS"/*.lidar.bin)
if [ ! -f "${FULL_BINS[0]}" ]; then
  echo "Error: no full .bin chunks produced in $FULL_CHUNKS" >&2
  exit 1
fi

full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
echo "Full packed input total: $(human_mib "$full_in_bytes")"

# ---- 6) Compress full chunks ----
echo "---- 6) Compress FULL chunks (parallel=$COMPRESS_JOBS) ----"
time_cmd "$TIME_DIR/openzl_comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.lidar.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$COMPRESS_JOBS" "$ZLI" "$COMPRESSOR"

FULL_ZLS=("$FULL_CHUNKS"/*.lidar.bin.zl)
full_out_bytes="$(bytes_sum "${FULL_ZLS[@]}")"

# ---- 7) Baselines ----
echo "---- 7) Baselines on original raw LiDAR ----"

lz4_bytes=0; zstd_bytes=0; pigz_bytes=0; gzip_bytes=0

if have_cmd gzip; then
  echo "Running gzip baseline..."
  GZIP_OUT="$OUT_DIR/lidar_raw.bin.gz"
  time_cmd "$TIME_DIR/gzip.time" gzip -k -f -c "$RAW_INPUT" > "$GZIP_OUT"
  gzip_bytes="$(get_file_size "$GZIP_OUT")"
fi

if have_cmd lz4; then
  LZ4_OUT="$OUT_DIR/lidar_raw.bin.lz4"
  echo "Running lz4 baseline..."
  time_cmd "$TIME_DIR/lz4.time" lz4 -f -q "$RAW_INPUT" "$LZ4_OUT"
  lz4_bytes="$(get_file_size "$LZ4_OUT")"
fi

if have_cmd zstd; then
  ZSTD_OUT="$OUT_DIR/lidar_raw.bin.zst"
  echo "Running zstd -9 baseline..."
  time_cmd "$TIME_DIR/zstd.time" zstd -f -q -9 "$RAW_INPUT" -o "$ZSTD_OUT"
  zstd_bytes="$(get_file_size "$ZSTD_OUT")"
fi

if have_cmd pigz; then
  PIGZ_OUT="$OUT_DIR/lidar_raw.bin.pigz.gz"
  echo "Running pigz -9 baseline (threads=$THREADS)..."
  time_cmd "$TIME_DIR/pigz.time" \
    bash -c "pigz -9 -p $THREADS -c '$RAW_INPUT' > '$PIGZ_OUT'"
  pigz_bytes="$(get_file_size "$PIGZ_OUT")"
fi

# ---- 8) Decompress + validate full ----
if [ "$VALIDATE_FULL" != "1" ]; then
  echo "---- 8) Skipping full validation (set VALIDATE_FULL=1 to enable) ----"
else
  echo "---- 8) Validating FULL chunks (decompress/cmp) ----"
  FAIL=0
  time_cmd "$TIME_DIR/full_validate.time" \
    bash -c '
      set -euo pipefail
      ZLI="$1"
      FAIL_FILE="$2"
      shopt -s nullglob
      for bin in "$0"/*.lidar.bin; do
        "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force > /dev/null
        if ! cmp -s "$bin" "$bin.dec"; then
          echo "Mismatch (full): $bin" >> "$FAIL_FILE"
          rm -f "$bin.dec"
          exit 2
        fi
        rm -f "$bin.dec"
      done
    ' "$FULL_CHUNKS" "$ZLI" "$OUT_DIR/full_validate_failures.txt" || FAIL=1

  if [ "$FAIL" = "1" ]; then
    echo "Full validation FAILED. See: $OUT_DIR/full_validate_failures.txt" >&2
    exit 1
  fi

  echo "Full validation OK"
fi

# ==============================================================================
# 9) Final Benchmark Report
# ==============================================================================
echo ""
echo "================================================================================"
echo " BENCHMARK REPORT: LiDAR Point Cloud Compression"
echo " Dataset: $(human_mib "$orig_bytes") (KITTI Velodyne HDL-64E)"
echo " Training: $(human_mib "$train_bin_bytes") sample, ${MAX_TIME_SECS}s max"
echo "================================================================================"
echo ""

get_real_sec() {
  if [ -f "$1" ]; then
    grep "^real" "$1" 2>/dev/null | awk '{print $2}' || echo "N/A"
  else
    echo "N/A"
  fi
}

printf "%-25s | %12s | %8s | %10s | %12s\n" \
       "Method" "Output Size" "Ratio" "Time (s)" "Speed (MB/s)"
echo "--------------------------|--------------|----------|------------|-------------"

report_row() {
  local name="$1" out_bytes="$2" time_file="$3"
  if [ "$out_bytes" -eq 0 ] 2>/dev/null; then return; fi
  local t=$(get_real_sec "$time_file")
  local ratio speed
  ratio=$(python3 -c "print(f'{${orig_bytes}/${out_bytes}:.2f}')")
  if [ "$t" != "N/A" ] && [ "$t" != "0" ]; then
    speed=$(python3 -c "print(f'{(${orig_bytes}/1024/1024)/float(${t}):.1f}')")
  else
    speed="N/A"
  fi
  printf "%-25s | %12s | %7sx | %10s | %11s\n" \
         "$name" "$(human_mib "$out_bytes")" "$ratio" "$t" "$speed"
}

report_row "Gzip"                "$gzip_bytes" "$TIME_DIR/gzip.time"
report_row "LZ4 (ROS standard)"  "$lz4_bytes"  "$TIME_DIR/lz4.time"
report_row "Pigz -9 (parallel)"  "$pigz_bytes" "$TIME_DIR/pigz.time"
report_row "Zstd -9 (MCAP std)"  "$zstd_bytes" "$TIME_DIR/zstd.time"
report_row "OpenZL (Columnar)"   "$full_out_bytes" "$TIME_DIR/openzl_comp_full.time"

echo ""
echo "OpenZL vs Zstd improvement: $(python3 -c "print(f'{${zstd_bytes}/${full_out_bytes}:.2f}')") x smaller"
echo ""
echo "================================================================================"
echo " Timing Breakdown"
echo "================================================================================"
echo "  Preprocessing (C++):    $(get_real_sec "$TIME_DIR/prep_full.time") s"
echo "  Training (one-time):    $(get_real_sec "$TIME_DIR/train.time") s"
echo "  Compression (parallel): $(get_real_sec "$TIME_DIR/openzl_comp_full.time") s"
if [ "$VALIDATE_FULL" = "1" ]; then
  echo "  Validation:             $(get_real_sec "$TIME_DIR/full_validate.time") s"
fi
echo ""
echo "Artifacts:"
echo "  Compressor:   $COMPRESSOR"
echo "  Train chunks: $TRAIN_CHUNKS"
echo "  Full chunks:  $FULL_CHUNKS"
echo "================================================================================"
