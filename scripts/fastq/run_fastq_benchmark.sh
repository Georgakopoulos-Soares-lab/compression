#!/usr/bin/env bash
# run_fastq_benchmark.sh — Preprocess FASTQ → OpenZL compress (trained, 16-part parallel)
#                           → run all baseline tools → write results CSV → plot PNG.
#
# Usage:
#   bash scripts/fastq/run_fastq_benchmark.sh [input_fastq] [n_parts] [n_threads]
#
# Defaults:
#   input_fastq = data/fastq/ERR9539086_500M.fastq   (download+slice with download_fastq.sh)
#   n_parts     = 16
#   n_threads   = 16

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

INPUT="${1:-data/fastq/ERR9539086_500M.fastq}"
N_PARTS="${2:-16}"
THREADS="${3:-16}"
PREPROCESS_THREADS="${PREPROCESS_THREADS:-32}"
COMPRESSOR="${COMPRESSOR:-artifacts/fastq_trained.compressor}"

if [ ! -f "$INPUT" ]; then
  echo "ERROR: input not found: $INPUT" >&2
  echo "       Run: bash scripts/fastq/download_fastq.sh first" >&2
  exit 1
fi
if [ ! -f "$COMPRESSOR" ]; then
  echo "ERROR: compressor not found: $COMPRESSOR" >&2
  exit 1
fi

PP_PREFIX="out/fastq/ERR9539086_500M_pp"
PARTS_DIR="out/fastq/parts"
TIMING_DIR="out/fastq/timing"
BASELINES_DIR="out/fastq/baselines"
mkdir -p out/fastq "$PARTS_DIR" "$TIMING_DIR" "$BASELINES_DIR"

# ── 1. Preprocess ─────────────────────────────────────────────────────────────
echo "[1/4] Preprocessing $INPUT (${PREPROCESS_THREADS} threads) …"
PP_TIME_FILE="$TIMING_DIR/preprocess.time"
/usr/bin/time -f "elapsed_sec=%e maxrss_kb=%M" -o "$PP_TIME_FILE" \
  ./tools/fastq_preprocess encode "$INPUT" "$PP_PREFIX" "$PREPROCESS_THREADS"
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

# Sum output bytes
OZL_OUT_BYTES=0
for f in "$PARTS_DIR"/part_*.zl; do
  OZL_OUT_BYTES=$(( OZL_OUT_BYTES + $(stat -c%s "$f") ))
done

# Read preprocessing time to compute end-to-end wall time
PP_SECS=$(python3 -c "
import re; m=re.search(r'elapsed_sec=([0-9.]+)', open('$PP_TIME_FILE').read()); print(m.group(1) if m else '0')
")
OZL_SECS=$(python3 -c "
import re; m=re.search(r'elapsed_sec=([0-9.]+)', open('$OZL_TIME_FILE').read()); print(m.group(1) if m else '0')
")
E2E_SECS=$(python3 -c "print(round($PP_SECS + $OZL_SECS, 6))")
echo "  Preprocessing: ${PP_SECS}s  |  Compression: ${OZL_SECS}s  |  E2E: ${E2E_SECS}s"

# ── 4. Run baseline tools (on the original FASTQ for fair comparison) ─────────
echo "[4/4] Running baseline tools on original FASTQ …"
FORMAT_TAG=fastq \
  OUT_DIR="$BASELINES_DIR" \
  OPENZL_TIME_FILE="$TIMING_DIR/openzl.time" \
  OPENZL_OUT_FILE="" \
  OPENZL_RATIO_OVERRIDE="" \
  OPENZL_LABEL="" \
  THREADS="$THREADS" \
  RESULTS_TSV="$TIMING_DIR/baselines_fastq.tsv" \
  bash scripts/benchmark_baselines.sh "$INPUT"

# ── 5. Assemble results CSV ───────────────────────────────────────────────────
RESULTS_CSV="artifacts/fastq_benchmark.csv"
mkdir -p artifacts

python3 - "$E2E_SECS" "$OZL_OUT_BYTES" "$ORIG_BYTES" "$TIMING_DIR/baselines_fastq.tsv" "$RESULTS_CSV" <<'PY'
import sys, csv
from pathlib import Path

e2e_secs, ozl_bytes_str, orig_bytes_str, baselines_tsv, out_csv = sys.argv[1:]
orig_bytes = int(orig_bytes_str)
ozl_bytes  = int(ozl_bytes_str)

rows = []
# OpenZL row (ratio vs original FASTQ, time includes preprocessing)
if ozl_bytes:
    rows.append({"tool": "OpenZL trained (16p)",
                 "ratio": f"{orig_bytes/ozl_bytes:.6f}",
                 "seconds": e2e_secs,
                 "out_bytes": str(ozl_bytes),
                 "out_file": ""})

# Baseline rows
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
python3 scripts/fastq/plot_fastq_benchmark.py \
  --csv "$RESULTS_CSV" \
  --out "artifacts/fastq_benchmark_plot.png"

echo ""
echo "Done. Results: $RESULTS_CSV"
echo "Plot:          artifacts/fastq_benchmark_plot.png"
