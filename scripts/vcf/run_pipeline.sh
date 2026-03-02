#!/usr/bin/env bash
# VCF Compression Pipeline
#
# Downloads a public VCF, converts it to columnar binary chunks, trains an
# OpenZL compressor, compresses the full dataset, and validates round-trip
# fidelity.
#
# Usage:
#   bash scripts/vcf/run_pipeline.sh [<input.vcf>]
#
# If no argument is given the pipeline downloads the 1000 Genomes WGS sites
# VCF (~1.1 GB gz -> ~11 GiB uncompressed, ~84 M variants).
# Pass a pre-existing (uncompressed) VCF to skip the download step:
#   bash scripts/vcf/run_pipeline.sh data/my_variants.vcf
#
# Environment overrides (export or prefix):
#   THREADS=16  TARGET_MIB=200  MAX_TIME_SECS=1800  VALIDATE_FULL=1
#   SKIP_BASELINES=1       Skip all baseline compressors
#   CLEANUP_INSTALLED=1    Remove brew packages auto-installed for baselines

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---- User-tunable knobs ----
THREADS="${THREADS:-16}"
TARGET_MIB="${TARGET_MIB:-200}"
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
TRAIN_PRE_THREADS="${TRAIN_PRE_THREADS:-1}"
FULL_PRE_THREADS="${FULL_PRE_THREADS:-$THREADS}"
COMPRESS_JOBS="${COMPRESS_JOBS:-$THREADS}"
NO_ACE_SUCCESSORS="${NO_ACE_SUCCESSORS:-1}"
VALIDATE_FULL="${VALIDATE_FULL:-1}"

# ---- Paths ----
SCHEMA="$HERE/schemas/vcf_columnar.sddl"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/vcf_preprocessor"

DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
TIME_DIR="$OUT_DIR/timing"

TRAIN_VCF="$OUT_DIR/train_vcf_${TARGET_MIB}MiB.vcf"
TRAIN_CHUNKS="$HERE/chunks_train_vcf_${TARGET_MIB}MiB"
FULL_CHUNKS="$HERE/chunks_full_vcf"

COMPRESSOR="$ART_DIR/vcf_columnar_train_${TARGET_MIB}MiB_t${THREADS}.compressor"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$TIME_DIR"

# ---- Helpers ----

time_cmd() {
  local outfile="$1"; shift
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
  local total=0 f sz
  for f in "$@"; do
    [ -f "$f" ] || continue
    sz="$(get_file_size "$f")"
    total=$(( total + sz ))
  done
  echo "$total"
}

human_mib() {
  python3 - "$1" <<'PY'
import sys; n=int(sys.argv[1]); print(f"{n/1024/1024:.2f} MiB")
PY
}

require_exec() {
  local p="$1"
  if [ ! -x "$p" ]; then
    echo "Error: not executable: $p" >&2; exit 1
  fi
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

# Auto-install helper: installs a brew package if the command is missing.
# Tracks what was installed so it can be cleaned up at the end.
BREW_INSTALLED=()   # packages we installed during this run

ensure_cmd() {
  # ensure_cmd <command> <brew_package>
  local cmd="$1" pkg="${2:-$1}"
  if have_cmd "$cmd"; then return 0; fi
  if ! have_cmd brew; then
    echo "  brew not available; cannot auto-install $pkg" >&2
    return 1
  fi
  echo "  Auto-installing $pkg via brew..."
  if brew install "$pkg" 2>/dev/null; then
    BREW_INSTALLED+=("$pkg")
    return 0
  else
    echo "  Failed to install $pkg" >&2
    return 1
  fi
}

cleanup_installed() {
  if [ "${#BREW_INSTALLED[@]}" -eq 0 ]; then return; fi
  if [ "${CLEANUP_INSTALLED:-1}" != "1" ]; then
    echo "Keeping auto-installed packages: ${BREW_INSTALLED[*]}"
    echo "  (set CLEANUP_INSTALLED=1 to remove them after the run)"
    return
  fi
  echo "Removing auto-installed packages: ${BREW_INSTALLED[*]}"
  for pkg in "${BREW_INSTALLED[@]}"; do
    brew uninstall "$pkg" 2>/dev/null || true
  done
}

# ---- 0) Build ----
echo "---- 0) Build ----"
"$HERE/scripts/common/build_all.sh"
require_exec "$ZLI"
require_exec "$PRE"

# ---- 1) Locate / download VCF ----
echo "---- 1) Locate VCF input ----"

VCF_IN="${1:-}"   # optional first argument

if [ -z "$VCF_IN" ] || [ ! -f "$VCF_IN" ]; then
  # Default: 1000 Genomes WGS sites-only (~84 M variants, ~11 GiB)
  DEFAULT_VCF="$DATA_DIR/ALL.wgs.phase3_shapeit2_mvncall_integrated_v5c.20130502.sites.vcf"
  if [ ! -f "$DEFAULT_VCF" ]; then
    echo "No VCF found; downloading 1000G WGS sites..."
    DATASET=1000g_wgs_sites "$HERE/scripts/vcf/download_vcf.sh"
  else
    echo "Using cached: $DEFAULT_VCF"
  fi
  VCF_IN="$DEFAULT_VCF"
fi

if [ ! -f "$VCF_IN" ]; then
  # Fallback: any .vcf in data/
  VCF_IN="$(ls -1 "$DATA_DIR"/*.vcf 2>/dev/null | head -n1 || true)"
fi

if [ -z "$VCF_IN" ] || [ ! -f "$VCF_IN" ]; then
  echo "Error: no VCF input found under $DATA_DIR" >&2
  exit 1
fi

echo "VCF input: $VCF_IN"
orig_bytes="$(get_file_size "$VCF_IN")"
echo "Original VCF size: $(human_mib "$orig_bytes")"

# ---- 2) Create training VCF (~TARGET_MIB) ----
echo "---- 2) Create training VCF (~${TARGET_MIB} MiB) ----"
python3 "$HERE/scripts/vcf/make_train_sample.py" \
  --in "$VCF_IN" --out "$TRAIN_VCF" --target-mib "$TARGET_MIB"

# ---- 3) Preprocess training VCF → columnar .bin chunks ----
echo "---- 3) Preprocess training VCF -> columnar chunks (threads=$TRAIN_PRE_THREADS) ----"
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
"$PRE" "$TRAIN_VCF" "$TRAIN_CHUNKS" "$TRAIN_PRE_THREADS"

shopt -s nullglob
TRAIN_BINS=("$TRAIN_CHUNKS"/*.vcf_columnar.bin)
shopt -u nullglob
if [ "${#TRAIN_BINS[@]}" -eq 0 ] || [ ! -f "${TRAIN_BINS[0]}" ]; then
  echo "Error: no training .bin chunks produced in $TRAIN_CHUNKS" >&2; exit 1
fi
train_bin_bytes="$(bytes_sum "${TRAIN_BINS[@]}")"
echo "Training packed payload size: $(human_mib "$train_bin_bytes")"

# ---- 4) Train compressor ----
echo "---- 4) Train compressor (threads=$THREADS, max_time_secs=$MAX_TIME_SECS) ----"
echo "Output: $COMPRESSOR"
time_cmd "$TIME_DIR/vcf_train.time" \
  "$ZLI" train "$TRAIN_CHUNKS" \
    --profile sddl --profile-arg "$SCHEMA" \
    --output "$COMPRESSOR" --force \
    --threads "$THREADS" --use-all-samples \
    --max-time-secs "$MAX_TIME_SECS" \
    $( [ "$NO_ACE_SUCCESSORS" = "1" ] && echo "--no-ace-successors" )

if [ ! -f "$COMPRESSOR" ]; then
  echo "Error: training did not produce compressor: $COMPRESSOR" >&2; exit 1
fi

# ---- 5) Validate on training chunks ----
echo "---- 5) Validate compressor on training chunks ----"
find "$TRAIN_CHUNKS" -maxdepth 1 -type f -name '*.vcf_columnar.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c \
    '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force > /dev/null' \
    "$ZLI" {} "$COMPRESSOR"

FAIL=0
for bin in "$TRAIN_CHUNKS"/*.vcf_columnar.bin; do
  [ -f "$bin" ] || continue
  "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force > /dev/null
  if ! cmp -s "$bin" "$bin.dec"; then
    echo "Mismatch (training): $bin" >&2; FAIL=1
  fi
  rm -f "$bin.dec"
done

if [ "$FAIL" = "1" ]; then
  echo "Training-set validation FAILED" >&2; exit 1
fi
echo "Training-set validation OK"

# ---- 6) Preprocess full VCF ----
echo "---- 6) Preprocess full VCF -> columnar chunks (threads=$FULL_PRE_THREADS) ----"
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
time_cmd "$TIME_DIR/vcf_prep_full.time" \
  "$PRE" "$VCF_IN" "$FULL_CHUNKS" "$FULL_PRE_THREADS"

shopt -s nullglob
FULL_BINS=("$FULL_CHUNKS"/*.vcf_columnar.bin)
shopt -u nullglob
if [ "${#FULL_BINS[@]}" -eq 0 ] || [ ! -f "${FULL_BINS[0]}" ]; then
  echo "Error: no full .bin chunks produced in $FULL_CHUNKS" >&2; exit 1
fi
full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
echo "Full packed input total: $(human_mib "$full_in_bytes")"

# ---- 7) Compress full chunks ----
echo "---- 7) Compress full chunks (parallel=$THREADS) ----"
time_cmd "$TIME_DIR/vcf_comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.vcf_columnar.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$COMPRESS_JOBS" "$ZLI" "$COMPRESSOR"

shopt -s nullglob
FULL_ZLS=("$FULL_CHUNKS"/*.vcf_columnar.bin.zl)
shopt -u nullglob
full_out_bytes="$(bytes_sum "${FULL_ZLS[@]}")"

openzl_bytes="$full_out_bytes"

# ---- 8) Run baselines ----
SKIP_BASELINES="${SKIP_BASELINES:-0}"
if [ "$SKIP_BASELINES" = "1" ]; then
  echo "---- 8) Baselines SKIPPED (SKIP_BASELINES=1) ----"
  RESULTS_TSV="$OUT_DIR/vcf_results.tsv"
  printf "Raw VCF (text)\t%d\t\n"            "$orig_bytes"    >  "$RESULTS_TSV"
  printf "Packed binary (schema)\t%d\t\n"    "$full_in_bytes" >> "$RESULTS_TSV"
  printf "OpenZL (schema-aware)\t%d\t%s\n"   "$openzl_bytes"  "$TIME_DIR/vcf_comp_full.time" >> "$RESULTS_TSV"
else
echo "---- 8) Baseline compressors ----"
BASELINE_DIR="$OUT_DIR/baselines_vcf"
RESULTS_TSV="$OUT_DIR/vcf_results.tsv"
mkdir -p "$BASELINE_DIR"

# Seed TSV with raw and packed sizes
printf "Raw VCF (text)\t%d\t\n"            "$orig_bytes"    >  "$RESULTS_TSV"
printf "Packed binary (schema)\t%d\t\n"    "$full_in_bytes" >> "$RESULTS_TSV"
printf "OpenZL (schema-aware)\t%d\t%s\n"   "$openzl_bytes"  "$TIME_DIR/vcf_comp_full.time" >> "$RESULTS_TSV"

run_baseline() {
  local label="$1" outfile="$2" tfile="$3"; shift 3
  echo "  -> $label"
  time_cmd "$tfile" "$@" > "$outfile"
  local sz; sz="$(get_file_size "$outfile")"
  printf "%s\t%d\t%s\n" "$label" "$sz" "$tfile" >> "$RESULTS_TSV"
}

# All baselines below use multi-threading where possible so they finish
# in minutes rather than hours on large (10+ GiB) inputs.

# 1. pigz -9 (parallel gzip — fast)
echo "[1/5] pigz -9"
if have_cmd pigz; then
  run_baseline "pigz -9" "$BASELINE_DIR/vcf.gz" "$TIME_DIR/vcf_pigz.time" \
    pigz -9 -p "$THREADS" -c "$VCF_IN"
elif have_cmd gzip; then
  run_baseline "gzip -9" "$BASELINE_DIR/vcf.gz" "$TIME_DIR/vcf_gzip.time" \
    gzip -9 -c "$VCF_IN"
else
  echo "  SKIP: gzip/pigz not found"
fi

# 2. zstd -9 (fast, parallel)
echo "[2/5] zstd -9"
if have_cmd zstd; then
  run_baseline "zstd -9" "$BASELINE_DIR/vcf.zst" "$TIME_DIR/vcf_zstd.time" \
    zstd --threads="$THREADS" -9 -q -c "$VCF_IN"
else
  echo "  SKIP: zstd not found (brew install zstd)"
fi

# 3. xz -3 (LZMA, threaded — level 3 is fast; higher levels are too slow on 10+ GiB)
echo "[3/5] xz -3 (LZMA)"
if have_cmd xz; then
  run_baseline "xz -3" "$BASELINE_DIR/vcf.xz" "$TIME_DIR/vcf_xz.time" \
    xz -3 --threads="$THREADS" -c "$VCF_IN"
else
  echo "  SKIP: xz not found (brew install xz)"
fi

# 4. bcftools BCF (binary VCF — genomics standard)
echo "[4/5] bcftools BCF"
if ! have_cmd bcftools; then ensure_cmd bcftools bcftools || true; fi
if have_cmd bcftools; then
  run_baseline "bcftools BCF" "$BASELINE_DIR/vcf.bcf" "$TIME_DIR/vcf_bcf.time" \
    bcftools view -Ob -o /dev/stdout "$VCF_IN"
else
  echo "  SKIP: bcftools not available"
fi

# 5. genozip (purpose-built genomics compressor)
echo "[5/5] genozip"
if ! have_cmd genozip; then ensure_cmd genozip genozip || true; fi
if have_cmd genozip; then
  GENOZIP_OUT="$BASELINE_DIR/vcf.genozip"
  time_cmd "$TIME_DIR/vcf_genozip.time" \
    genozip --threads "$THREADS" -fo "$GENOZIP_OUT" "$VCF_IN"
  sz="$(get_file_size "$GENOZIP_OUT")"
  printf "genozip\t%d\t%s\n" "$sz" "$TIME_DIR/vcf_genozip.time" >> "$RESULTS_TSV"
else
  echo "  SKIP: genozip not available"
fi

fi  # end SKIP_BASELINES

# ---- 9) Validate full ----
if [ "$VALIDATE_FULL" != "1" ]; then
  echo "Skipping full validation (set VALIDATE_FULL=1 to enable)"
else
  echo "---- 9) Validate full chunks (decompress/cmp) ----"
  FAIL_FILE="$OUT_DIR/vcf_full_validate_failures.txt"
  FAIL=0
  time_cmd "$TIME_DIR/vcf_full_validate.time" \
    bash -c '
      set -euo pipefail
      ZLI="$1"; FAIL_FILE="$2"
      shopt -s nullglob
      for bin in "$0"/*.vcf_columnar.bin; do
        "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force > /dev/null
        if ! cmp -s "$bin" "$bin.dec"; then
          echo "Mismatch (full): $bin" >> "$FAIL_FILE"
          rm -f "$bin.dec"; exit 2
        fi
        rm -f "$bin.dec"
      done
    ' "$FULL_CHUNKS" "$ZLI" "$FAIL_FILE" || FAIL=1

  if [ "$FAIL" = "1" ]; then
    echo "Full validation FAILED. See: $FAIL_FILE" >&2; exit 1
  fi
  echo "Full validation OK"
fi

# ---- 10) Summary table ----
echo ""
echo "============================================================"
echo "  RESULTS SUMMARY"
echo "============================================================"

python3 - "$RESULTS_TSV" <<'PYTABLE'
import sys, os, re

tsv_path = sys.argv[1]

rows = []
with open(tsv_path) as f:
    for line in f:
        line = line.rstrip("\n")
        if not line:
            continue
        parts = line.split("\t")
        label = parts[0]
        size  = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        tfile = parts[2] if len(parts) > 2 else ""
        rows.append((label, size, tfile))

orig_b   = next((sz for lbl, sz, _ in rows if "Raw VCF" in lbl), 0)
packed_b = next((sz for lbl, sz, _ in rows if "Packed binary" in lbl), 0)
MiB = 1024**2
GiB = 1024**3

def elapsed(tfile):
    if not tfile or not os.path.exists(tfile):
        return ""
    txt = open(tfile).read()
    m = re.search(r'elapsed_sec=([0-9.]+)', txt)
    if m:
        return f"{float(m.group(1)):.1f}s"
    m = re.search(r'real\s+([0-9.]+)', txt)
    if m:
        return f"{float(m.group(1)):.1f}s"
    for tok in txt.split():
        try:
            return f"{float(tok):.1f}s"
        except ValueError:
            pass
    return "?"

def fmt_size(b):
    if b == 0:
        return "N/A"
    if b >= GiB:
        return f"{b/GiB:.3f} GiB"
    return f"{b/MiB:.2f} MiB"

cw = [36, 14, 12, 8]
hdr = f"{'Method':<{cw[0]}}  {'Size':>{cw[1]}}  {'vs raw VCF':>{cw[2]}}  {'Time':>{cw[3]}}"
sep = "-" * len(hdr)

print(f"\n{hdr}")
print(sep)

openzl_b = 0
for label, sz, tfile in rows:
    size_s  = fmt_size(sz) if sz else "N/A"
    ratio_s = f"{orig_b/sz:.2f}x" if orig_b and sz else "N/A"
    time_s  = elapsed(tfile) if tfile else ""
    marker  = ""

    if "OpenZL" in label:
        openzl_b = sz
        marker = "  <--"
    elif "Raw VCF" in label:
        ratio_s = "1.00x (baseline)"

    print(f"{label:<{cw[0]}}  {size_s:>{cw[1]}}  {ratio_s:>{cw[2]}}  {time_s:>{cw[3]}}{marker}")

print(sep)

if openzl_b and packed_b:
    print(f"\nOpenZL vs packed binary input: {packed_b/openzl_b:.2f}x")

best_lbl, best_b = None, float('inf')
for label, sz, _ in rows:
    if sz and "OpenZL" not in label and "Raw VCF" not in label and "Packed" not in label:
        if sz < best_b:
            best_b, best_lbl = sz, label
if best_lbl and openzl_b:
    delta = (best_b - openzl_b) / best_b * 100
    sign  = "smaller" if delta > 0 else "larger"
    print(f"OpenZL vs best baseline ({best_lbl}): {abs(delta):.1f}% {sign}")
PYTABLE

echo ""
echo "Results TSV : $RESULTS_TSV"
echo "Baselines   : $BASELINE_DIR/"
echo "Compressor  : $COMPRESSOR"
echo "Train chunks: $TRAIN_CHUNKS"
echo "Full chunks : $FULL_CHUNKS"

# ---- Cleanup auto-installed packages ----
cleanup_installed
