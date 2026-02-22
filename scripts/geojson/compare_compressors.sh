#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/geojson_to_bin_universal"

# ---- Arguments ----
if [ "$#" -lt 2 ]; then
    echo "Usage: $0 <input_geojson> <compressor_path> [threads] [chunk_mb]"
    echo "Example: $0 data/citylots.json artifacts/citylots.compressor"
    exit 1
fi

INPUT_JSON="$1"
COMPRESSOR="$2"
THREADS="${3:-8}"
CHUNK_MB="${4:-100}"

if [ ! -f "$INPUT_JSON" ]; then
    echo "Error: Input file '$INPUT_JSON' not found."
    exit 1
fi

if [ ! -f "$COMPRESSOR" ]; then
    echo "Error: Compressor file '$COMPRESSOR' not found."
    exit 1
fi

BASENAME=$(basename "$INPUT_JSON" .json)
EXP_DIR="$(cd "$(dirname "$INPUT_JSON")" && pwd)"
MAPPING="$EXP_DIR/${BASENAME}_mapping.json"
FULL_CHUNKS="$EXP_DIR/chunks_full"
TIME_DIR="$EXP_DIR/timing_compare"

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
    if [ -f "$1" ]; then
        grep "real" "$1" | awk '{print $2}'
    else
        echo "0"
    fi
}

get_file_size() {
  if [ -f "$1" ]; then
      if [[ "$OSTYPE" == "darwin"* ]]; then
        stat -f%z "$1"
      else
        stat -c%s "$1"
      fi
  else
      echo "0"
  fi
}

human_mib() {
  python3 -c "print(f'{int($1)/1024/1024:.2f} MiB')"
}

calc_speed() {
    local bytes=$1
    local sec=$2
    if [ "$bytes" -eq 0 ] || [ "$sec" = "0" ] || [ -z "$sec" ]; then
        echo "N/A"
    else
        python3 -c "print(f'{($bytes/1024/1024)/float($sec):.2f} MB/s')"
    fi
}

echo "================================================================"
echo "COMPARING COMPRESSORS: $BASENAME"
echo "================================================================"
echo "Input:      $INPUT_JSON"
echo "Compressor: $COMPRESSOR"
echo "================================================================"

orig_bytes="$(get_file_size "$INPUT_JSON")"
echo "Original Size: $(human_mib "$orig_bytes")"

# ---------------------------------------------------------
# 1. Zstd Baseline
# ---------------------------------------------------------
echo ""
echo "[1/2] Running Zstd (Level 9)..."
ZSTD_OUT="$EXP_DIR/${BASENAME}.json.zst"
time_cmd "$TIME_DIR/zstd.time" zstd -9 -f -c "$INPUT_JSON" > "$ZSTD_OUT"
zstd_bytes="$(get_file_size "$ZSTD_OUT")"
zstd_time=$(get_time_sec "$TIME_DIR/zstd.time")
zstd_speed=$(calc_speed "$orig_bytes" "$zstd_time")

# ---------------------------------------------------------
# 2. OpenZL (Using existing compressor)
# ---------------------------------------------------------
echo ""
echo "[2/2] Running OpenZL..."

if [ ! -d "$FULL_CHUNKS" ] || [ -z "$(ls -A "$FULL_CHUNKS"/*.bin 2>/dev/null)" ]; then
    echo "  Preprocessing JSON to binary chunks..."
    if [ ! -f "$MAPPING" ]; then
        echo "  Error: Mapping file '$MAPPING' not found. Cannot preprocess."
        echo "  Please run the full pipeline first to generate the mapping."
        exit 1
    fi

    rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
    time_cmd "$TIME_DIR/prep.time" \
      "$PRE" "$INPUT_JSON" "$MAPPING" "$FULL_CHUNKS" "$CHUNK_MB" > /dev/null
    prep_time=$(get_time_sec "$TIME_DIR/prep.time")
else
    echo "  Using existing binary chunks in $FULL_CHUNKS"
    prep_time=0
fi

FULL_BINS=("$FULL_CHUNKS"/*.bin)
full_in_bytes=0
for f in "${FULL_BINS[@]}"; do
    if [ -f "$f" ]; then
        sz=$(get_file_size "$f")
        full_in_bytes=$((full_in_bytes + sz))
    fi
done

echo "  Compressing chunks..."
time_cmd "$TIME_DIR/comp.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$THREADS" "$ZLI" "$COMPRESSOR"

comp_time=$(get_time_sec "$TIME_DIR/comp.time")

openzl_bytes=0
for f in "$FULL_CHUNKS"/*.zl; do
    if [ -f "$f" ]; then
        sz=$(get_file_size "$f")
        openzl_bytes=$((openzl_bytes + sz))
    fi
done

# ---------------------------------------------------------
# Report
# ---------------------------------------------------------
echo ""
echo "================================================================================"
echo "COMPARISON REPORT: $BASENAME"
echo "================================================================================"
echo ""
printf "%-20s | %-12s | %-10s | %-15s\n" "Method" "Size" "Ratio" "Speed (End-to-End)"
echo "---------------------|--------------|------------|---------------------"
printf "%-20s | %-12s | %-10s | %-15s\n" "Original JSON" "$(human_mib "$orig_bytes")" "1.00x" "-"

if [ "$zstd_bytes" -gt 0 ]; then
    ratio=$(python3 -c "print(f'{${orig_bytes}/${zstd_bytes}:.2f}')")
    printf "%-20s | %-12s | %-10sx | %-15s\n" "Zstd -9" "$(human_mib "$zstd_bytes")" "$ratio" "$zstd_speed"
fi

if [ "$openzl_bytes" -gt 0 ]; then
    ratio=$(python3 -c "print(f'{${orig_bytes}/${openzl_bytes}:.2f}')")
    total_time=$(python3 -c "print(f'{float($prep_time) + float($comp_time)}')")
    speed=$(calc_speed "$orig_bytes" "$total_time")
    printf "%-20s | %-12s | %-10sx | %-15s\n" "OpenZL (Offline)" "$(human_mib "$openzl_bytes")" "$ratio" "$speed"
fi

echo ""
echo "Notes:"
if [ "$prep_time" == "0" ]; then
    echo " - OpenZL speed is compression ONLY (chunks were pre-calculated)."
else
    echo " - OpenZL speed includes JSON->Binary preprocessing time."
fi
echo "================================================================================"
