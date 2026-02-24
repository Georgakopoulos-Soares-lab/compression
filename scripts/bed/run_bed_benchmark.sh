#!/usr/bin/env bash
# run_bed_benchmark.sh — Preprocess hg38_rmsk.txt → OpenZL compress (trained, 16-part parallel)
#                         → run all baseline tools → write results CSV → plot PNG.
#
# Usage:
#   bash scripts/bed/run_bed_benchmark.sh [input_bed] [n_parts] [n_threads]
#
# Defaults:
#   input_bed = data/bed/hg38_rmsk.txt   (download first with scripts/bed/download_bed.sh)
#   n_parts   = 16
#   n_threads = 16

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

INPUT="${1:-data/bed/hg38_rmsk.txt}"
N_PARTS="${2:-16}"
THREADS="${3:-16}"
COMPRESSOR="${COMPRESSOR:-artifacts/bed_trained.compressor}"

if [ ! -f "$INPUT" ]; then
  echo "ERROR: input not found: $INPUT" >&2
  echo "       Run: bash scripts/bed/download_bed.sh first" >&2
  exit 1
fi
if [ ! -f "$COMPRESSOR" ]; then
  echo "ERROR: compressor not found: $COMPRESSOR" >&2
  exit 1
fi

PP_PREFIX="out/bed/hg38_rmsk_pp"
PARTS_DIR="out/bed/parts"
TIMING_DIR="out/bed/timing"
BASELINES_DIR="out/bed/baselines"
mkdir -p out/bed "$PARTS_DIR" "$TIMING_DIR" "$BASELINES_DIR"

# ── 1. Preprocess ─────────────────────────────────────────────────────────────
echo "[1/4] Preprocessing $INPUT …"
./tools/bed_preprocess encode "$INPUT" "$PP_PREFIX"
# Produces: ${PP_PREFIX}.meta + ${PP_PREFIX}.tsv

PP_TSV="${PP_PREFIX}.tsv"
ORIG_BYTES=$(stat -c%s "$INPUT")

# ── 2. Split preprocessed TSV into N_PARTS ───────────────────────────────────
echo "[2/4] Splitting into $N_PARTS parts …"
LINES=$(wc -l < "$PP_TSV")
CHUNK=$(( (LINES + N_PARTS - 1) / N_PARTS ))
rm -f "$PARTS_DIR"/part_*
split -l "$CHUNK" -d "$PP_TSV" "$PARTS_DIR/part_"

# ── 3. Compress all parts in parallel, measure wall-clock time ────────────────
echo "[3/4] Compressing with OpenZL (trained, ${N_PARTS}p) …"
OZL_TIME_FILE="$TIMING_DIR/openzl.time"
{
  /usr/bin/time -f "elapsed_sec=%e maxrss_kb=%M" -o "$OZL_TIME_FILE" \
    bash -c '
      for f in '"$PARTS_DIR"'/part_*; do
        [[ "$f" == *.zl ]] && continue
        '"$HERE"'/openzl/zli compress "$f" \
          --compressor '"$COMPRESSOR"' \
          --output "$f.zl" --force &
      done
      wait
    '
}

# Sum output bytes across all parts
OZL_OUT_BYTES=0
for f in "$PARTS_DIR"/part_*.zl; do
  OZL_OUT_BYTES=$(( OZL_OUT_BYTES + $(stat -c%s "$f") ))
done

# ── 4. Run baseline tools ─────────────────────────────────────────────────────
echo "[4/4] Running baseline tools …"
FORMAT_TAG=bed \
  OUT_DIR="$BASELINES_DIR" \
  OPENZL_TIME_FILE="$TIMING_DIR/openzl.time" \
  OPENZL_OUT_FILE="" \
  OPENZL_RATIO_OVERRIDE="" \
  OPENZL_LABEL="" \
  THREADS="$THREADS" \
  RESULTS_TSV="$TIMING_DIR/baselines_bed.tsv" \
  bash scripts/benchmark_baselines.sh "$PP_TSV"

# ── 5. Assemble results CSV ───────────────────────────────────────────────────
RESULTS_CSV="artifacts/bed_benchmark.csv"
mkdir -p artifacts

python3 - "$OZL_TIME_FILE" "$OZL_OUT_BYTES" "$ORIG_BYTES" "$TIMING_DIR/baselines_bed.tsv" "$RESULTS_CSV" <<'PY'
import sys, csv
from pathlib import Path
import re

time_file, ozl_bytes_str, orig_bytes_str, baselines_tsv, out_csv = sys.argv[1:]
orig_bytes = int(orig_bytes_str)
ozl_bytes  = int(ozl_bytes_str)

def read_elapsed(p):
    m = re.search(r"elapsed_sec=([0-9.]+)", Path(p).read_text(errors="replace"))
    return float(m.group(1)) if m else None

rows = []
# OpenZL row
secs = read_elapsed(time_file)
if secs and ozl_bytes:
    rows.append({"tool": "OpenZL (trained, 16t)",
                 "ratio": f"{orig_bytes/ozl_bytes:.6f}",
                 "seconds": f"{secs:.6f}",
                 "out_bytes": str(ozl_bytes),
                 "out_file": ""})

# Baseline rows (already in TSV from benchmark_baselines.sh)
tsv_path = Path(baselines_tsv)
if tsv_path.exists():
    for row in csv.DictReader(tsv_path.open(), delimiter='\t'):
        if row.get("tool", "").strip():
            rows.append(row)

with open(out_csv, 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=["tool","ratio","seconds","out_bytes","out_file"])
    w.writeheader()
    w.writerows(rows)
print(f"Results: {out_csv}")
PY

# ── 6. Plot ───────────────────────────────────────────────────────────────────
python3 scripts/bed/plot_bed_benchmark.py \
  --csv "$RESULTS_CSV" \
  --out "artifacts/bed_benchmark_plot.png"

echo ""
echo "Done. Results: $RESULTS_CSV"
echo "Plot:          artifacts/bed_benchmark_plot.png"
