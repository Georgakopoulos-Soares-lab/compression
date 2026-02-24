#!/usr/bin/env bash
# run_vcf_benchmark.sh — make_vcf_sample → OpenZL compress (trained, 16-part parallel)
#                         → run all baseline tools → write results CSV → plot PNG.
#
# Benchmarks the VCF *table* (data rows only; ## meta lines are not included).
#
# Usage:
#   bash scripts/vcf/run_vcf_benchmark.sh [input.vcf.gz] [n_parts] [n_threads]
#
# Defaults:
#   input.vcf.gz = data/vcf/clinvar.vcf.gz   (download with scripts/vcf/download_vcf.sh)
#   n_parts      = 16
#   n_threads    = 16
#
# Override compressor:
#   COMPRESSOR=artifacts/clinvar_csv_train_200MiB_t16.compressor \
#     bash scripts/vcf/run_vcf_benchmark.sh

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$HERE"

INPUT="${1:-data/vcf/clinvar.vcf.gz}"
N_PARTS="${2:-16}"
THREADS="${3:-16}"
TARGET_MIB="${TARGET_MIB:-500}"
COMPRESSOR="${COMPRESSOR:-artifacts/clinvar_csv_train_200MiB_t16.compressor}"
LABEL="${LABEL:-OpenZL csv (chunks, 16t)}"

if [ ! -f "$INPUT" ]; then
  echo "ERROR: input not found: $INPUT" >&2
  echo "       Run: bash scripts/vcf/download_vcf.sh first" >&2
  exit 1
fi

TAG=$(basename "$INPUT" .vcf.gz | sed 's/\.vcf$//')
SAMPLE_PREFIX="out/vcf/${TAG}_bench_${TARGET_MIB}MiB"
PARTS_DIR="out/vcf/parts"
TIMING_DIR="out/vcf/timing"
BASELINES_DIR="out/vcf/baselines"
mkdir -p out/vcf "$PARTS_DIR" "$TIMING_DIR" "$BASELINES_DIR"

# ── 1. Extract table sample ───────────────────────────────────────────────────
echo "[1/4] Extracting ${TARGET_MIB} MiB table from $INPUT …"
python3 scripts/vcf/make_vcf_sample.py \
  --input "$INPUT" \
  --out-prefix "$SAMPLE_PREFIX" \
  --target-mib "$TARGET_MIB"
# Produces: ${SAMPLE_PREFIX}.meta.txt + ${SAMPLE_PREFIX}.table.tsv

TABLE_TSV="${SAMPLE_PREFIX}.table.tsv"
ORIG_BYTES=$(stat -c%s "$TABLE_TSV")

# ── 2. Split table into N_PARTS ───────────────────────────────────────────────
echo "[2/4] Splitting into $N_PARTS parts …"
LINES=$(wc -l < "$TABLE_TSV")
CHUNK=$(( (LINES + N_PARTS - 1) / N_PARTS ))
rm -f "$PARTS_DIR"/part_*
split -l "$CHUNK" -d "$TABLE_TSV" "$PARTS_DIR/part_"

# ── 3. Compress all parts in parallel ─────────────────────────────────────────
echo "[3/4] Compressing with OpenZL …"
OZL_TIME_FILE="$TIMING_DIR/openzl.time"

if [ -f "$COMPRESSOR" ]; then
  COMPRESS_CMD="$HERE/openzl/zli compress \"\$f\" --compressor $COMPRESSOR --output \"\$f.zl\" --force"
else
  echo "  (no trained compressor found — using untrained CSV profiler)"
  COMPRESS_CMD="$HERE/openzl/zli compress \"\$f\" --profile csv --profile-arg \$'\\t' --output \"\$f.zl\" --force"
  LABEL="OpenZL csv untrained (${N_PARTS}p)"
fi

/usr/bin/time -f "elapsed_sec=%e maxrss_kb=%M" -o "$OZL_TIME_FILE" \
  bash -c "
    for f in $PARTS_DIR/part_*; do
      [[ \"\$f\" == *.zl ]] && continue
      $COMPRESS_CMD &
    done
    wait
  "

OZL_OUT_BYTES=0
for f in "$PARTS_DIR"/part_*.zl; do
  OZL_OUT_BYTES=$(( OZL_OUT_BYTES + $(stat -c%s "$f") ))
done

# ── 4. Run baseline tools ─────────────────────────────────────────────────────
echo "[4/4] Running baseline tools …"
FORMAT_TAG=vcf_${TAG} \
  OUT_DIR="$BASELINES_DIR" \
  OPENZL_TIME_FILE="$TIMING_DIR/openzl.time" \
  OPENZL_OUT_FILE="" \
  OPENZL_RATIO_OVERRIDE="" \
  OPENZL_LABEL="" \
  THREADS="$THREADS" \
  RESULTS_TSV="$TIMING_DIR/baselines_vcf.tsv" \
  bash scripts/benchmark_baselines.sh "$TABLE_TSV"

# ── 5. Assemble results CSV ───────────────────────────────────────────────────
RESULTS_CSV="artifacts/results_vcf_${TAG}.csv"
mkdir -p artifacts

python3 - "$OZL_TIME_FILE" "$OZL_OUT_BYTES" "$ORIG_BYTES" \
          "$TIMING_DIR/baselines_vcf.tsv" "$RESULTS_CSV" "$LABEL" <<'PY'
import sys, csv, re
from pathlib import Path

time_file, ozl_bytes_str, orig_bytes_str, baselines_tsv, out_csv, label = sys.argv[1:]
orig_bytes = int(orig_bytes_str)
ozl_bytes  = int(ozl_bytes_str)

def read_elapsed(p):
    m = re.search(r"elapsed_sec=([0-9.]+)", Path(p).read_text(errors="replace"))
    return float(m.group(1)) if m else None

rows = []
secs = read_elapsed(time_file)
if secs and ozl_bytes:
    rows.append({"tool": label,
                 "ratio": f"{orig_bytes/ozl_bytes:.6f}",
                 "seconds": f"{secs:.6f}",
                 "out_bytes": str(ozl_bytes),
                 "out_file": ""})

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
python3 scripts/vcf/plot_vcf_benchmark.py \
  --csv "$RESULTS_CSV" \
  --out "artifacts/vcf_${TAG}_benchmark_plot.png" \
  --title "VCF Compression Benchmark — ${TAG} (${TARGET_MIB} MiB table)"

echo ""
echo "Done. Results: $RESULTS_CSV"
echo "Plot:          artifacts/vcf_${TAG}_benchmark_plot.png"
