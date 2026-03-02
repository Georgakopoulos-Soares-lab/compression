#!/usr/bin/env bash
# ============================================================================
# VCF Large-Scale Benchmark
# ============================================================================
#
# Downloads the 1000 Genomes phase 3 whole-genome sites-only VCF
# (~1.1 GB gz  →  ~14 GB uncompressed  →  ~84 million variants) and runs
# the full OpenZL compression pipeline with settings appropriate for a
# large, realistic evaluation.
#
# Also measures against 7 state-of-the-art compressors:
#   1. pigz -9          parallel gzip (same algorithm as gzip, but fast)
#   2. bzip2 -9         Burrows-Wheeler, widely used in bioinformatics
#   3. bgzip            BGZF blocked gzip — the genomics random-access standard
#   4. xz -9            LZMA2 — typically the best ratio of all general-purpose tools
#   5. zstd -19         Facebook Zstandard — best ratio + fast with threads
#   6. bcftools BCF     Binary VCF (bit-packed genotypes + integer field IDs)
#                       + BCF piped through bgzip (the compressed binary standard)
#   7. genozip          Purpose-built VCF/genomics compressor (best-in-class)
#
# Usage:
#   bash scripts/vcf/run_large_benchmark.sh [<already_decompressed.vcf>]
#
# Disk requirements: ~30–40 GB free (download + uncompressed + chunks).
# Expected wall time: 2–4 hours depending on CPU count.
#
# Key environment overrides:
#   THREADS         parallelism (default: all logical cores)
#   TARGET_MIB      training sample size in MiB (default: 1000)
#   MAX_TIME_SECS   max training duration in seconds (default: 1800 = 30 min)
#   CHUNK_SIZE_MB   chunk size for preprocessing (default: 50)
#   VALIDATE_FULL   set to 1 to do full round-trip validation (adds ~1 h)
#   KEEP_CHUNKS     set to 1 to keep chunk directories after the run
#
# Estimated wall time (breakdown):
#   Download + decompress  : 10–40 min  (network / disk speed)
#   Preprocessing          :  5–15 min  (I/O bound, parallel)
#   Training               : up to MAX_TIME_SECS (default 30 min)
#   OpenZL compression     :  5–20 min  (parallel, THREADS workers)
#   pigz/gzip baseline     :  2–20 min  (pigz is parallel; plain gzip is slow)
#   zstd baseline          :  2–10 min  (parallel)
#   ─────────────────────────────────────
#   Total (typical)        : ~1–2 hours

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ---- Knobs ----
THREADS="${THREADS:-$(nproc 2>/dev/null || sysctl -n hw.logicalcpu 2>/dev/null || echo 8)}"
TARGET_MIB="${TARGET_MIB:-1000}"
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
TRAIN_PRE_THREADS="${TRAIN_PRE_THREADS:-1}"
FULL_PRE_THREADS="${FULL_PRE_THREADS:-$THREADS}"
COMPRESS_JOBS="${COMPRESS_JOBS:-$THREADS}"
NO_ACE_SUCCESSORS="${NO_ACE_SUCCESSORS:-1}"
VALIDATE_FULL="${VALIDATE_FULL:-0}"
KEEP_CHUNKS="${KEEP_CHUNKS:-0}"

# ---- Paths ----
SCHEMA="$HERE/schemas/vcf_columnar.sddl"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/vcf_preprocessor"

DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
TIME_DIR="$OUT_DIR/timing"
BASELINE_DIR="$OUT_DIR/baselines_vcf_large"

TRAIN_VCF="$OUT_DIR/train_vcf_large_${TARGET_MIB}MiB.vcf"
TRAIN_CHUNKS="$HERE/chunks_train_vcf_large_${TARGET_MIB}MiB"
FULL_CHUNKS="$HERE/chunks_full_vcf_large"

COMPRESSOR="$ART_DIR/vcf_large_train_${TARGET_MIB}MiB_t${THREADS}.compressor"

LARGE_VCF_GZ="$DATA_DIR/ALL.wgs.phase3_shapeit2_mvncall_integrated_v5c.20130502.sites.vcf.gz"
LARGE_VCF="$DATA_DIR/ALL.wgs.phase3_shapeit2_mvncall_integrated_v5c.20130502.sites.vcf"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$TIME_DIR" "$BASELINE_DIR"

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

require_exec() {
  local p="$1"
  if [ ! -x "$p" ]; then echo "Error: not executable: $p" >&2; exit 1; fi
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

elapsed_from_time_file() {
  # Extract the elapsed real time from a /usr/bin/time output file.
  local f="$1"
  [ -f "$f" ] || { echo "N/A"; return; }
  # GNU time: "elapsed_sec=X"  |  BSD time: "real X.XX"
  local secs
  secs="$(grep -oE 'elapsed_sec=[0-9.]+' "$f" | cut -d= -f2 || true)"
  if [ -z "$secs" ]; then
    secs="$(grep -oE 'real\s+[0-9.]+' "$f" | awk '{print $2}' || true)"
  fi
  if [ -n "$secs" ]; then
    printf "%.1f s" "$secs"
  else
    echo "?"
  fi
}

print_banner() {
  echo ""
  echo "============================================================"
  echo "  $*"
  echo "============================================================"
}

# ---- 0) Build ----
print_banner "0) Build"
"$HERE/scripts/common/build_all.sh"
require_exec "$ZLI"
require_exec "$PRE"

# ---- 1) Locate / download large VCF ----
print_banner "1) Locate / download 1000G WGS sites VCF"

VCF_IN="${1:-}"

if [ -n "$VCF_IN" ] && [ -f "$VCF_IN" ]; then
  echo "Using provided VCF: $VCF_IN"
else
  if [ ! -f "$LARGE_VCF" ]; then
    if [ ! -f "$LARGE_VCF_GZ" ]; then
      echo "Downloading 1000 Genomes phase 3 WGS sites VCF (~1.1 GB)..."
      echo "  Estimated download time: 5–30 min depending on connection."
      DATASET=1000g_wgs_sites \
        VCF_GZ="$LARGE_VCF_GZ" \
        VCF_OUT="$LARGE_VCF" \
        "$HERE/scripts/vcf/download_vcf.sh"
    else
      echo "gz already present: $LARGE_VCF_GZ"
      if [ ! -f "$LARGE_VCF" ]; then
        echo "Decompressing (~14 GB, this may take several minutes)..."
        gzip -dk "$LARGE_VCF_GZ"
      fi
    fi
  else
    echo "Using cached: $LARGE_VCF"
  fi
  VCF_IN="$LARGE_VCF"
fi

if [ ! -f "$VCF_IN" ]; then
  echo "Error: VCF not found: $VCF_IN" >&2; exit 1
fi

orig_bytes="$(get_file_size "$VCF_IN")"
echo ""
echo "VCF input : $VCF_IN"
printf "Input size: %.2f GiB (%d bytes)\n" \
  "$(python3 -c "print($orig_bytes/1024**3)")" "$orig_bytes"

# Rough variant count (exclude header lines, count data lines)
echo "Counting variants (may take 30–60 s for 14 GB)..."
variant_count="$(grep -vc '^#' "$VCF_IN" || true)"
echo "Variants  : $variant_count"

# ---- Disk space warning ----
python3 - "$orig_bytes" <<'PY'
import sys
ob = int(sys.argv[1])
needed = ob * 2.5  # uncompressed + chunks + compressed + baselines
print(f"\nEstimated disk required: ~{needed/1024**3:.0f} GiB free space.")
print("If disk is tight, set KEEP_CHUNKS=0 (default) to remove chunks after use.")
PY

# ---- 2) Create training VCF ----
print_banner "2) Create training sample (~${TARGET_MIB} MiB)"
python3 "$HERE/scripts/vcf/make_train_sample.py" \
  --in "$VCF_IN" --out "$TRAIN_VCF" --target-mib "$TARGET_MIB"
train_orig_bytes="$(get_file_size "$TRAIN_VCF")"
printf "Training VCF size: %.2f MiB\n" "$(python3 -c "print($train_orig_bytes/1024**2)")"

# ---- 3) Preprocess training chunks ----
print_banner "3) Preprocess training VCF -> columnar chunks (threads=$TRAIN_PRE_THREADS)"
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
"$PRE" "$TRAIN_VCF" "$TRAIN_CHUNKS" "$TRAIN_PRE_THREADS"

shopt -s nullglob
TRAIN_BINS=("$TRAIN_CHUNKS"/*.vcf_columnar.bin)
shopt -u nullglob
if [ "${#TRAIN_BINS[@]}" -eq 0 ]; then
  echo "Error: no training chunks produced" >&2; exit 1
fi
train_bin_bytes="$(bytes_sum "${TRAIN_BINS[@]}")"
printf "Training binary size: %.2f MiB (%d chunks)\n" \
  "$(python3 -c "print($train_bin_bytes/1024**2)")" "${#TRAIN_BINS[@]}"

# ---- 4) Train compressor ----
print_banner "4) Train compressor  threads=$THREADS  max_time=${MAX_TIME_SECS}s"
echo "Output: $COMPRESSOR"
time_cmd "$TIME_DIR/vcf_large_train.time" \
  "$ZLI" train "$TRAIN_CHUNKS" \
    --profile sddl --profile-arg "$SCHEMA" \
    --output "$COMPRESSOR" --force \
    --threads "$THREADS" --use-all-samples \
    --max-time-secs "$MAX_TIME_SECS" \
    $( [ "$NO_ACE_SUCCESSORS" = "1" ] && echo "--no-ace-successors" )

if [ ! -f "$COMPRESSOR" ]; then
  echo "Error: training did not produce compressor" >&2; exit 1
fi
echo "Training time: $(elapsed_from_time_file "$TIME_DIR/vcf_large_train.time")"

# ---- 5) Validate on training chunks ----
print_banner "5) Validate compressor on training chunks"
find "$TRAIN_CHUNKS" -maxdepth 1 -type f -name '*.vcf_columnar.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c \
    '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force >/dev/null' \
    "$ZLI" {} "$COMPRESSOR"
FAIL=0
for bin in "$TRAIN_CHUNKS"/*.vcf_columnar.bin; do
  [ -f "$bin" ] || continue
  "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force >/dev/null
  if ! cmp -s "$bin" "$bin.dec"; then
    echo "Mismatch (training): $bin" >&2; FAIL=1
  fi
  rm -f "$bin.dec" "$bin.zl"
done
[ "$FAIL" = "1" ] && { echo "Training-set validation FAILED" >&2; exit 1; }
echo "Training-set validation OK"

# ---- 6) Preprocess full VCF ----
print_banner "6) Preprocess full VCF (~$(python3 -c "print(f'{$orig_bytes/1024**3:.1f}')") GiB)"
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
time_cmd "$TIME_DIR/vcf_large_prep_full.time" \
  "$PRE" "$VCF_IN" "$FULL_CHUNKS" "$FULL_PRE_THREADS"
echo "Preprocessing time: $(elapsed_from_time_file "$TIME_DIR/vcf_large_prep_full.time")"

shopt -s nullglob
FULL_BINS=("$FULL_CHUNKS"/*.vcf_columnar.bin)
shopt -u nullglob
if [ "${#FULL_BINS[@]}" -eq 0 ]; then
  echo "Error: no full chunks produced" >&2; exit 1
fi
full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
printf "Full binary size : %.2f GiB (%d chunks)\n" \
  "$(python3 -c "print($full_in_bytes/1024**3)")" "${#FULL_BINS[@]}"

# ---- 7) Compress full chunks with OpenZL ----
print_banner "7) Compress full dataset with OpenZL (${THREADS} workers)"
time_cmd "$TIME_DIR/vcf_large_comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.vcf_columnar.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$COMPRESS_JOBS" "$ZLI" "$COMPRESSOR"
echo "Compression time: $(elapsed_from_time_file "$TIME_DIR/vcf_large_comp_full.time")"

shopt -s nullglob
FULL_ZLS=("$FULL_CHUNKS"/*.vcf_columnar.bin.zl)
shopt -u nullglob
openzl_bytes="$(bytes_sum "${FULL_ZLS[@]}")"

# ---- 8) Baseline compressors ----
print_banner "8) Baseline compressors"

# Results accumulate into a TSV: label<TAB>bytes<TAB>time_file
RESULTS_TSV="$OUT_DIR/vcf_large_results.tsv"
# Seed with the raw sizes we already know
printf "Raw VCF (text)\t%d\t\n"    "$orig_bytes"   >  "$RESULTS_TSV"
printf "Packed binary (schema)\t%d\t\n" "$full_in_bytes" >> "$RESULTS_TSV"

run_baseline() {
  # run_baseline <label> <output_file> <time_file> <cmd...>
  local label="$1" outfile="$2" tfile="$3"
  shift 3
  echo "  → $label"
  time_cmd "$tfile" "$@" > "$outfile"
  local sz; sz="$(get_file_size "$outfile")"
  printf "%s\t%d\t%s\n" "$label" "$sz" "$tfile" >> "$RESULTS_TSV"
  printf "    size: %.3f GiB   time: %s\n" \
    "$(python3 -c "print($sz/1024**3)")" \
    "$(elapsed_from_time_file "$tfile")"
}

# 1. pigz / gzip -9
echo "[1/7] pigz / gzip -9 (parallel gzip)"
if have_cmd pigz; then
  run_baseline "pigz -9" "$BASELINE_DIR/large.vcf.gz" \
    "$TIME_DIR/vcf_large_pigz.time" \
    pigz -9 -p "$THREADS" -c "$VCF_IN"
elif have_cmd gzip; then
  echo "  (pigz not found; falling back to single-threaded gzip)"
  run_baseline "gzip -9" "$BASELINE_DIR/large.vcf.gz" \
    "$TIME_DIR/vcf_large_pigz.time" \
    gzip -9 -c "$VCF_IN"
else
  echo "  SKIP: gzip/pigz not found"
fi

# 2. bzip2 -9  (widely used in bioinformatics pipelines)
echo "[2/7] bzip2 -9"
if have_cmd bzip2; then
  run_baseline "bzip2 -9" "$BASELINE_DIR/large.vcf.bz2" \
    "$TIME_DIR/vcf_large_bzip2.time" \
    bzip2 -9 -c "$VCF_IN"
else
  echo "  SKIP: bzip2 not found"
fi

# 3. bgzip  (BGZF — the genomics random-access standard, used by tabix/htslib)
echo "[3/7] bgzip (BGZF)"
if have_cmd bgzip; then
  run_baseline "bgzip" "$BASELINE_DIR/large.vcf.bgz" \
    "$TIME_DIR/vcf_large_bgzip.time" \
    bgzip -@ "$THREADS" -l 9 -c "$VCF_IN"
else
  echo "  SKIP: bgzip not found (install: brew install htslib / apt install tabix)"
fi

# 4. xz -9 (LZMA — typically the best ratio among general-purpose compressors)
echo "[4/7] xz -9 (LZMA)"
if have_cmd xz; then
  run_baseline "xz -9" "$BASELINE_DIR/large.vcf.xz" \
    "$TIME_DIR/vcf_large_xz.time" \
    xz -9 --threads="$THREADS" -c "$VCF_IN"
else
  echo "  SKIP: xz not found (brew install xz / apt install xz-utils)"
fi

# 5. zstd -19 (best-ratio mode, very fast with threads)
echo "[5/7] zstd -19"
if have_cmd zstd; then
  run_baseline "zstd -19" "$BASELINE_DIR/large.vcf.zst" \
    "$TIME_DIR/vcf_large_zstd.time" \
    zstd --threads="$THREADS" -19 -q -c "$VCF_IN"
else
  echo "  SKIP: zstd not found (brew install zstd / apt install zstd)"
fi

# 6. bcftools BCF -Ob  (VCF → binary BCF format — the genomics standard binary encoding)
#    Note: BCF stores genotypes as bit-packed integers and field IDs as integers,
#    giving 3-6x size reduction vs text VCF *before* any compression.
#    For sites-only VCFs the gain is smaller (no genotype columns to pack).
echo "[6/7] bcftools BCF (binary VCF)"
if have_cmd bcftools; then
  run_baseline "bcftools BCF (uncompressed)" "$BASELINE_DIR/large.bcf" \
    "$TIME_DIR/vcf_large_bcf.time" \
    bcftools view -Ob -o /dev/stdout "$VCF_IN"
  # Also BCF + bgzip = the compressed binary standard
  if have_cmd bgzip; then
    echo "  → bcftools BCF + bgzip"
    BCF_GZ="$BASELINE_DIR/large.bcf.bgz"
    BCF_TMP="$BASELINE_DIR/large.bcf"
    time_cmd "$TIME_DIR/vcf_large_bcf_bgzip.time" \
      bash -c 'bcftools view -Ob "$0" | bgzip -@ "$1" -l 9 -c > "$2"' \
        "$VCF_IN" "$THREADS" "$BCF_GZ"
    sz="$(get_file_size "$BCF_GZ")"
    printf "bcftools BCF + bgzip\t%d\t%s\n" "$sz" \
      "$TIME_DIR/vcf_large_bcf_bgzip.time" >> "$RESULTS_TSV"
    printf "    bcftools BCF + bgzip: %.3f GiB   time: %s\n" \
      "$(python3 -c "print($sz/1024**3)")" \
      "$(elapsed_from_time_file "$TIME_DIR/vcf_large_bcf_bgzip.time")"
  fi
else
  echo "  SKIP: bcftools not found (brew install bcftools / apt install bcftools)"
fi

# 7. genozip  (purpose-built VCF/genomics compressor — typically best ratio for VCF)
#    https://www.genozip.com  |  free for academic use
echo "[7/7] genozip (genomics-specific)"
if have_cmd genozip; then
  GENOZIP_OUT="$BASELINE_DIR/large.vcf.genozip"
  time_cmd "$TIME_DIR/vcf_large_genozip.time" \
    genozip --threads "$THREADS" -fo "$GENOZIP_OUT" "$VCF_IN"
  sz="$(get_file_size "$GENOZIP_OUT")"
  printf "genozip\t%d\t%s\n" "$sz" "$TIME_DIR/vcf_large_genozip.time" >> "$RESULTS_TSV"
  printf "    genozip: %.3f GiB   time: %s\n" \
    "$(python3 -c "print($sz/1024**3)")" \
    "$(elapsed_from_time_file "$TIME_DIR/vcf_large_genozip.time")"
else
  echo "  SKIP: genozip not found"
  echo "         Academic/non-commercial: https://github.com/divonlan/genozip"
  echo "         conda install -c conda-forge -c bioconda genozip"
fi

# Add OpenZL result to TSV
printf "OpenZL (schema-aware)\t%d\t%s\n" \
  "$openzl_bytes" "$TIME_DIR/vcf_large_comp_full.time" >> "$RESULTS_TSV"

# ---- 9) Optional full round-trip validation ----
if [ "$VALIDATE_FULL" = "1" ]; then
  print_banner "9) Full round-trip validation (VALIDATE_FULL=1)"
  FAIL=0
  FAIL_FILE="$OUT_DIR/vcf_large_validate_failures.txt"
  time_cmd "$TIME_DIR/vcf_large_validate.time" \
    bash -c '
      set -euo pipefail
      ZLI="$1"; FF="$2"
      shopt -s nullglob
      for bin in "$0"/*.vcf_columnar.bin; do
        "$ZLI" decompress "$bin.zl" --output "$bin.dec" --force >/dev/null
        if ! cmp -s "$bin" "$bin.dec"; then
          echo "Mismatch: $bin" >> "$FF"; rm -f "$bin.dec"; exit 2
        fi
        rm -f "$bin.dec"
      done
    ' "$FULL_CHUNKS" "$ZLI" "$FAIL_FILE" || FAIL=1
  echo "Validation time: $(elapsed_from_time_file "$TIME_DIR/vcf_large_validate.time")"
  [ "$FAIL" = "1" ] && { echo "Full validation FAILED. See $FAIL_FILE" >&2; exit 1; }
  echo "Full validation OK"
else
  echo "Full validation skipped (set VALIDATE_FULL=1 to enable)"
fi

# ---- 10) Cleanup chunks (optional) ----
if [ "$KEEP_CHUNKS" != "1" ]; then
  print_banner "Cleaning up chunks (set KEEP_CHUNKS=1 to retain)"
  rm -rf "$TRAIN_CHUNKS" "$FULL_CHUNKS" "$TRAIN_VCF"
  echo "Chunks removed."
fi

# ---- 11) Summary table ----
print_banner "RESULTS SUMMARY"

python3 - "$RESULTS_TSV" "$variant_count" <<'PY'
import sys, os

tsv_path   = sys.argv[1]
variants   = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 0

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

# Extract raw VCF size as reference baseline
orig_b   = next((sz for lbl, sz, _ in rows if "Raw VCF" in lbl), 0)
packed_b = next((sz for lbl, sz, _ in rows if "Packed binary" in lbl), 0)

GiB = 1024**3

def elapsed(tfile):
    if not tfile or not os.path.exists(tfile):
        return "N/A"
    txt = open(tfile).read()
    import re
    m = re.search(r'elapsed_sec=([0-9.]+)', txt)
    if m:
        return f"{float(m.group(1)):.1f}s"
    m = re.search(r'real\s+([0-9.]+)', txt)
    if m:
        return f"{float(m.group(1)):.1f}s"
    # BSD time -l -p: "real X.XX" on its own line
    for tok in txt.split():
        try:
            v = float(tok)
            return f"{v:.1f}s"
        except ValueError:
            pass
    return "?"

col_w = [36, 12, 12, 8]
hdr   = f"{'Method':<{col_w[0]}}  {'Size':>{col_w[1]}}  {'vs raw VCF':>{col_w[2]}}  {'Time':>{col_w[3]}}"
sep   = "-" * len(hdr)

print(f"\n{hdr}")
print(sep)

openzl_b = 0
for label, sz, tfile in rows:
    if sz == 0:
        size_s = "N/A"
        ratio_s = "N/A"
    else:
        size_s  = f"{sz/GiB:.3f} GiB"
        ratio_s = f"{orig_b/sz:.2f}x" if orig_b and sz else "N/A"

    time_s = elapsed(tfile) if tfile else ""

    marker = ""
    if "OpenZL" in label:
        openzl_b = sz
        marker = "  ◄"
    elif "Raw VCF" in label:
        ratio_s = "1.00x (baseline)"

    print(f"{label:<{col_w[0]}}  {size_s:>{col_w[1]}}  {ratio_s:>{col_w[2]}}  {time_s:>{col_w[3]}}{marker}")

print(sep)

# Extra stats
if variants > 0 and openzl_b > 0:
    bpv = openzl_b * 8 / variants
    print(f"\nOpenZL: {bpv:.2f} bits / variant  ({variants:,} variants)")

if openzl_b and packed_b:
    r = packed_b / openzl_b
    print(f"OpenZL vs packed binary input: {r:.2f}x")

# best baseline
best_lbl, best_b = None, float('inf')
for label, sz, _ in rows:
    if sz and "OpenZL" not in label and "Raw VCF" not in label and "Packed" not in label:
        if sz < best_b:
            best_b, best_lbl = sz, label
if best_lbl and openzl_b:
    delta = (best_b - openzl_b) / best_b * 100
    sign  = "smaller" if delta > 0 else "larger"
    print(f"OpenZL vs best baseline ({best_lbl}): {abs(delta):.1f}% {sign}")
PY

echo ""
echo "Timing log files: $TIME_DIR/vcf_large_*.time"
echo "Baselines stored: $BASELINE_DIR/"
echo "Results TSV     : $RESULTS_TSV"
echo "Compressor      : $COMPRESSOR"
echo ""
echo "Done."
