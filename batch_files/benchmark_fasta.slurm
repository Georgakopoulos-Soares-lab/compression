#!/bin/bash
#SBATCH --job-name=bench_fasta
#SBATCH --partition=pvc
#SBATCH --account=BCS25105
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --time=36:00:00
#SBATCH --output=results/bench_fasta_%j.out
#SBATCH --error=results/bench_fasta_%j.err

# =============================================================================
# FASTA compression quality comparison
# -----------------------------------------------------------------------------
# For each input FASTA file, benchmarks:
#   * General-purpose codecs at max quality: gzip -9, pigz -9, zstd -19 --long,
#     xz -9e, 7z -mx=9
#   * "Our method" (FAV5): biocompress_preprocessor (lossless 2-bit pack + case
#     RLE + exception RLE + line layout) -> zli train (serial) -> zli compress
#     per chunk -> zli decompress -> fasta_postprocess. Verified BYTE-EXACT
#     against the original FASTA (soft-masking, IUPAC codes, line wrapping, CRLF
#     and trailing-newline all preserved).
#   * HARC ABLATION (RUN_HARC=1, default on): harc_preprocessor (minimizer read
#     reordering) -> zli train -> zli compress per chunk. This is an ABLATION,
#     NOT a competing codec, and is emitted under the tool name
#     `ablation_harc_lossy` so it cannot be read as a compression result.
#     It is sequence-lossless only: it case-folds, collapses non-ACGT to N, and
#     does not preserve line wrapping. Discarding that information is worth
#     ~4.0% of ratio (measured: 4.649x lossy vs 4.465x byte-exact on the same
#     400 MB wheat slice), so its numbers are NOT comparable to the byte-exact
#     rows and are inflated by roughly that margin. Its roundtrip column reads
#     'OK(zl-only)' (the OpenZL layer only) or 'OK(seq)' with HARC_SEQ_VALIDATE=1.
#   * SPRING: recorded as N/A (SPRING only supports FASTQ, not FASTA).
#
# Per (file, tool) it records: compressed size, ratio vs original, compression
# wall time, decompression wall time, peak RSS, and a roundtrip check.
# Output: results/benchmark_fasta_<jobid>.csv  +  ..._<jobid>.txt summary
#
# Submit:   sbatch batch_files/benchmark_fasta.slurm
# Override: sbatch --export=ALL,TARGET_MIB=200,MAX_TIME_SECS=1800 batch_files/benchmark_fasta.slurm
#           FASTA_FILES="/path/a.fna /path/b.fna" sbatch ... (space separated)
#           RUN_HARC=0 sbatch ...                 (skip the HARC variant)
#           FASTA_METHODS="zstd xz 7z" sbatch ... (trim the baseline set)
#           HARC_READ_LEN=200 HARC_MISMATCH_FRAC=0.15 sbatch ...
#           HARC_SEQ_VALIDATE=1 sbatch ...        (add per-record seq check; RAM-heavy)
# =============================================================================

set -uo pipefail

HERE="/scratch/11579/alexmargaris/compression"
cd "$HERE" || exit 1

THREADS="${SLURM_CPUS_PER_TASK:-16}"
JOB="${SLURM_JOB_ID:-local}"
BENCH_DIR="${BENCH_DIR:-$HERE/out/bench_fasta_${JOB}}"
RESULTS_CSV="${RESULTS_CSV:-$HERE/results/benchmark_fasta_${JOB}.csv}"
SUMMARY_TXT="${SUMMARY_TXT:-$HERE/results/benchmark_fasta_${JOB}.txt}"

ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/biocompress_preprocessor"
FASTA_POST="$HERE/tools/fasta_postprocess"
SCHEMA="$HERE/schemas/fasta_packed.sddl"

HARC_PRE="$HERE/tools/harc_preprocessor"
HARC_DECODE="$HERE/tools/harc_decode"
HARC_SCHEMA="$HERE/schemas/harc_packed.sddl"

# The ONE shipped universal FASTA model. Trained once by scripts/train_fasta_model.sh;
# runtime here is compress-only. FORCE_RETRAIN_FASTA=1 rebuilds it this run.
FASTA_MODEL="${FASTA_MODEL:-$HERE/artifacts/fasta_model.zlc}"
FASTA_SDDL="${FASTA_SDDL:-$HERE/schemas/fasta_packed_v5.sddl}"

# HARC reference-method knobs (HARC is the lossy comparison track; it still
# trains per file because it is not the shipped method).
TARGET_MIB="${TARGET_MIB:-200}"
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"
NO_ACE="${NO_ACE:-1}"

# HARC variant knobs
RUN_HARC="${RUN_HARC:-1}"                     # 1 => also benchmark the HARC path
HARC_READ_LEN="${HARC_READ_LEN:-200}"         # harc_preprocessor read_len arg
HARC_MISMATCH_FRAC="${HARC_MISMATCH_FRAC:-0.15}"
HARC_SEQ_VALIDATE="${HARC_SEQ_VALIDATE:-0}"   # 1 => also run validate_harc.py per-record
                                              # sequence diff. Holds ~2x genome in RAM, so
                                              # leave off for the multi-GB inputs; the
                                              # OpenZL byte-cmp on the packed .bin still runs.

# Baseline codec levels (max quality)
GZIP_LEVEL="${GZIP_LEVEL:-9}"
ZSTD_LEVEL="${ZSTD_LEVEL:-19}"
XZ_LEVEL="${XZ_LEVEL:-9e}"
XZ_BLOCK="${XZ_BLOCK:-192MiB}"

SKIP_BUILD="${SKIP_BUILD:-0}"

# Which general-purpose baselines to run. Trim it when re-testing one genome and
# the other baselines are already measured (gzip -9 alone is ~2.5 h on wheat).
FASTA_METHODS="${FASTA_METHODS:-gzip pigz zstd xz 7z}"

# ---- Input files (5 by default). Override with FASTA_FILES env var. ----------
if [ -n "${FASTA_FILES:-}" ]; then
  read -r -a FASTA_LIST <<< "$FASTA_FILES"
else
  FASTA_LIST=(
    "$HERE/data/T2T-CHM13v2.0_genomic.fna"
    "$HERE/data/GCF_000001405.40_GRCh38.p14_genomic.fna"
    "$HERE/data/GCF_000001635.27_GRCm39_genomic.fna"
    "$HERE/data/GCF_018294505.1_IWGSC_CS_RefSeq_v2.1_genomic.fna"
    "$HERE/data/GRCh38_1GB_sample.fna"
  )
fi

mkdir -p "$BENCH_DIR" "$HERE/results"

# ---- Helpers ---------------------------------------------------------------
timed() { local tf="$1"; shift; /usr/bin/time -f 'elapsed_sec=%e maxrss_kb=%M' -o "$tf" "$@"; }
read_field() { sed -n "s/.*$2=\([0-9.]*\).*/\1/p" "$1" 2>/dev/null | head -n1; }
bytes_sum() { local t=0 s f; for f in "$@"; do [ -f "$f" ] || continue; s=$(stat -c%s "$f"); t=$((t+s)); done; echo "$t"; }
ratio() { awk -v o="$1" -v c="$2" 'BEGIN{ if(c+0>0) printf "%.3f", o/c; else printf "0" }'; }
emit() { printf '%s\n' "$1" >> "$RESULTS_CSV"; }

echo "file,format,orig_bytes,tool,settings,comp_bytes,ratio,comp_sec,decomp_sec,comp_maxrss_kb,roundtrip,notes" > "$RESULTS_CSV"

# ---- One general-purpose codec: compress, decompress, verify, record --------
run_std_tool() {
  local input="$1" orig="$2" base="$3" tool="$4" fname="$5"
  local cf df settings notes="" cb ct dt mem rt
  local tfc="$BENCH_DIR/${base}.${tool}.c.time" tfd="$BENCH_DIR/${base}.${tool}.d.time"

  case "$tool" in
    gzip)
      settings="-${GZIP_LEVEL}"; cf="$BENCH_DIR/${base}.gz"; df="$BENCH_DIR/${base}.gz.dec"
      timed "$tfc" bash -c "gzip -${GZIP_LEVEL} -c '$input' > '$cf'" || notes="compress_failed;"
      timed "$tfd" bash -c "gzip -dc '$cf' > '$df'"                  || notes="${notes}decompress_failed;"
      ;;
    pigz)
      settings="-${GZIP_LEVEL} -p${THREADS}"; cf="$BENCH_DIR/${base}.pigz.gz"; df="$BENCH_DIR/${base}.pigz.dec"
      timed "$tfc" bash -c "pigz -${GZIP_LEVEL} -p ${THREADS} -c '$input' > '$cf'" || notes="compress_failed;"
      timed "$tfd" bash -c "pigz -dc '$cf' > '$df'"                                || notes="${notes}decompress_failed;"
      ;;
    zstd)
      settings="-${ZSTD_LEVEL} --long=27 -T${THREADS}"; cf="$BENCH_DIR/${base}.zst"; df="$BENCH_DIR/${base}.zst.dec"
      timed "$tfc" bash -c "zstd -q -f -${ZSTD_LEVEL} --long=27 -T${THREADS} -o '$cf' '$input'" || notes="compress_failed;"
      timed "$tfd" bash -c "zstd -q -f -d --long=27 -o '$df' '$cf'"                             || notes="${notes}decompress_failed;"
      ;;
    xz)
      settings="-${XZ_LEVEL} -T${THREADS} --block-size=${XZ_BLOCK}"; cf="$BENCH_DIR/${base}.xz"; df="$BENCH_DIR/${base}.xz.dec"
      timed "$tfc" bash -c "xz -${XZ_LEVEL} -T${THREADS} --block-size=${XZ_BLOCK} -c '$input' > '$cf'" || notes="compress_failed;"
      timed "$tfd" bash -c "xz -dc -T${THREADS} '$cf' > '$df'"                                        || notes="${notes}decompress_failed;"
      ;;
    7z)
      settings="-mx=9 -mmt=${THREADS}"; cf="$BENCH_DIR/${base}.7z"; df="$BENCH_DIR/${base}.7z.dec"
      rm -f "$cf"
      timed "$tfc" 7z a -t7z -mx=9 -mmt="${THREADS}" "$cf" "$input" >/dev/null 2>&1 || notes="compress_failed;"
      timed "$tfd" bash -c "7z e -so '$cf' > '$df' 2>/dev/null"                     || notes="${notes}decompress_failed;"
      ;;
  esac

  cb=0; [ -s "$cf" ] && cb=$(stat -c%s "$cf")
  ct=$(read_field "$tfc" elapsed_sec); dt=$(read_field "$tfd" elapsed_sec)
  mem=$(read_field "$tfc" maxrss_kb)
  if [ -s "$df" ] && cmp -s "$input" "$df"; then rt="OK"; else rt="FAIL"; fi
  emit "\"$fname\",FASTA,$orig,$tool,$settings,$cb,$(ratio "$orig" "$cb"),${ct:-NA},${dt:-NA},${mem:-NA},$rt,${notes:-}"
  echo "  $tool: ratio=$(ratio "$orig" "$cb")  comp=${ct:-NA}s  decomp=${dt:-NA}s  roundtrip=$rt"
  rm -f "$cf" "$df"
}

# ---- Our method: FAV5 packing + the ONE shipped universal FASTA compressor ---
# No per-file training. The model at $FASTA_MODEL is trained once
# (scripts/train_fasta_model.sh) on a diverse corpus and only used to COMPRESS
# here; a per-chunk `--profile serial` fallback keeps every chunk byte-exact.
run_our_method() {
  local input="$1" orig="$2" base="$3" fname="$4"
  local tdir="$BENCH_DIR/${base}.our"
  rm -rf "$tdir"; mkdir -p "$tdir/full_chunks"
  local tfp="$tdir/pre.time" tfc="$tdir/comp.time" tfd="$tdir/decomp.time"
  local notes=""

  timed "$tfp" "$PRE" "$input" "$tdir/full_chunks" "$THREADS" fasta_packed >/dev/null 2>&1

  # Resolve the shipped model: either a trained .zlc or, when the winning
  # candidate was the SDDL profile, the pseudo-model "sddl:<schema>".
  local comp_arg="-" want=""
  local prof; prof="$(cat "${FASTA_MODEL%.zlc}.profile" 2>/dev/null)"
  if [ "$prof" = sddl ] && [ -s "$FASTA_SDDL" ]; then
    want="sddl:$FASTA_SDDL"
  elif [ -s "$FASTA_MODEL" ]; then
    want="$FASTA_MODEL"
  fi
  if [ -n "$want" ]; then
    local smallest cargs=()
    smallest=$(find "$tdir/full_chunks" -maxdepth 1 -type f -name '*.fasta_packed.bin' -printf '%s %p\n' \
               | sort -n | head -n1 | cut -d' ' -f2-)
    case "$want" in
      sddl:*) cargs=(--profile sddl --profile-arg "${want#sddl:}");;
      *)      cargs=(--compressor "$want");;
    esac
    if [ -n "$smallest" ] && "$ZLI" compress "$smallest" "${cargs[@]}" \
         --output "$smallest.zltest" --force >/dev/null 2>&1; then
      comp_arg="$want"
    else
      notes="${notes}shipped_model_unusable_fell_back_to_serial;"
    fi
    rm -f "$smallest.zltest"
  else
    notes="${notes}no_shipped_model;"
  fi

  # Per-chunk compress with a hard guarantee: shipped model, else --profile serial.
  : > "$tdir/fb.count"
  timed "$tfc" bash "$HERE/scripts/fasta_compress_chunks.sh" \
      "$ZLI" "$tdir/full_chunks" "$THREADS" "$comp_arg" "$tdir/fb.count" >/dev/null 2>&1
  local nfb; nfb=$(wc -l < "$tdir/fb.count" 2>/dev/null | tr -d ' '); nfb=${nfb:-0}
  [ "$comp_arg" != "-" ] && [ "$nfb" -gt 0 ] && notes="${notes}serial_fallback_chunks=${nfb};"

  # Every FAV5 chunk must have produced a .zl; if any is missing that is a real
  # compress failure.
  local n_bin n_zl
  n_bin=$(find "$tdir/full_chunks" -maxdepth 1 -name '*.fasta_packed.bin' | wc -l)
  n_zl=$(find "$tdir/full_chunks" -maxdepth 1 -name '*.fasta_packed.bin.zl' | wc -l)
  [ "$n_zl" -eq "$n_bin" ] || notes="${notes}compress_failed(${n_zl}/${n_bin});"

  local cb; cb=$(bytes_sum "$tdir"/full_chunks/*.fasta_packed.bin.zl)

  timed "$tfd" bash -c '
      find "$1" -maxdepth 1 -type f -name "*.fasta_packed.bin.zl" -print0 \
        | xargs -0 -P "$2" -I{} "$3" decompress "{}" --output "{}.dec" --force >/dev/null 2>&1
    ' _ "$tdir/full_chunks" "$THREADS" "$ZLI" || notes="${notes}decompress_failed;"

  # End-to-end byte-exact check: reconstruct the FASTA from the decompressed
  # FAV5 chunks and compare to the original input.
  local rt="FAIL"
  if "$FASTA_POST" "$tdir/full_chunks" "$tdir/rt.fasta" --suffix .fasta_packed.bin.zl.dec >/dev/null 2>&1 \
     && cmp -s "$input" "$tdir/rt.fasta"; then rt="OK"; else notes="${notes}roundtrip_mismatch;"; fi
  [ "$cb" -gt 0 ] || rt="FAIL"
  rm -f "$tdir/rt.fasta"

  local pe ce de mem
  pe=$(read_field "$tfp" elapsed_sec)
  ce=$(read_field "$tfc" elapsed_sec); de=$(read_field "$tfd" elapsed_sec)
  mem=$(read_field "$tfc" maxrss_kb)
  local comp_sec; comp_sec=$(awk -v a="${pe:-0}" -v b="${ce:-0}" 'BEGIN{printf "%.2f", a+b}')

  emit "\"$fname\",FASTA,$orig,our_method_openzl,universal-model/t${THREADS},$cb,$(ratio "$orig" "$cb"),$comp_sec,${de:-NA},${mem:-NA},$rt,byte-exact FASTA roundtrip; preprocess_sec=${pe:-NA}; compress_sec=${ce:-NA}; ${notes}"
  echo "  our_method: ratio=$(ratio "$orig" "$cb")  encode=${comp_sec}s (prep ${pe:-NA} + comp ${ce:-NA})  decomp=${de:-NA}s  roundtrip=$rt  ${notes}"
  rm -rf "$tdir"
}

# ---- HARC method: assembly-free read reordering + trained OpenZL compressor -
# HARC packs the genome into fixed-length reads and reorders them so similar
# reads are adjacent, which can help OpenZL on large, repetitive genomes.
# It is sequence-lossless only (see header note), so roundtrip = validate_harc.py.
run_harc_method() {
  local input="$1" orig="$2" base="$3" fname="$4"
  local tdir="$BENCH_DIR/${base}.harc"
  rm -rf "$tdir"; mkdir -p "$tdir/train_chunks" "$tdir/full_chunks"
  local train_fa="$tdir/train.fasta" comp="$tdir/harc_packed.compressor"
  local tfp="$tdir/pre.time" tft="$tdir/train.time" tfc="$tdir/comp.time" tfd="$tdir/decomp.time"
  local notes="seq-lossless (non-ACGT->N)"

  python3 "$HERE/scripts/make_train_sample.py" --in "$input" --out "$train_fa" --target-mib "$TARGET_MIB" \
    || { emit "\"$fname\",FASTA,$orig,ablation_harc_lossy,rl${HARC_READ_LEN},0,0,NA,NA,NA,FAIL,make_train_sample failed"; rm -rf "$tdir"; return; }

  "$HARC_PRE" "$train_fa" "$tdir/train_chunks" 1 "$HARC_READ_LEN" "$HARC_MISMATCH_FRAC" >/dev/null 2>&1
  timed "$tfp" "$HARC_PRE" "$input" "$tdir/full_chunks" "$THREADS" "$HARC_READ_LEN" "$HARC_MISMATCH_FRAC" >/dev/null 2>&1 \
    || notes="${notes}; preprocess_failed"

  local ace_flag=(); [ "$NO_ACE" = 1 ] && ace_flag=(--no-ace-successors)
  timed "$tft" "$ZLI" train "$tdir/train_chunks" \
      --profile sddl --profile-arg "$HARC_SCHEMA" \
      --output "$comp" --force --threads "$THREADS" --use-all-samples \
      --max-time-secs "$MAX_TIME_SECS" "${ace_flag[@]}" >/dev/null 2>&1 \
    || notes="${notes}; train_failed"

  timed "$tfc" bash -c '
      find "$1" -maxdepth 1 -type f -name "*.harc_packed.bin" -print0 \
        | xargs -0 -P "$2" -I{} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force >/dev/null 2>&1
    ' _ "$tdir/full_chunks" "$THREADS" "$ZLI" "$comp" || notes="${notes}; compress_failed"

  local cb; cb=$(bytes_sum "$tdir"/full_chunks/*.harc_packed.bin.zl)

  timed "$tfd" bash -c '
      find "$1" -maxdepth 1 -type f -name "*.harc_packed.bin.zl" -print0 \
        | xargs -0 -P "$2" -I{} "$3" decompress "{}" --output "{}.dec" --force >/dev/null 2>&1
    ' _ "$tdir/full_chunks" "$THREADS" "$ZLI" || notes="${notes}; decompress_failed"

  # OpenZL layer must be byte-exact on the packed .bin ...
  local zlrt="OK" b
  for b in "$tdir"/full_chunks/*.harc_packed.bin; do
    [ -f "$b" ] || continue
    cmp -s "$b" "$b.zl.dec" || { zlrt="FAIL"; break; }
  done

  # ... and the HARC layer must reproduce every record's sequence (canonicalized).
  local rt="$zlrt"
  if [ "$HARC_SEQ_VALIDATE" = 1 ]; then
    if python3 "$HERE/scripts/validate_harc.py" "$input" "$tdir/full_chunks" "$HARC_DECODE" >/dev/null 2>&1; then
      [ "$zlrt" = OK ] && rt="OK(seq)" || rt="FAIL"
    else
      rt="FAIL"; notes="${notes}; seq_validation_failed"
    fi
    rm -rf "$tdir/full_chunks/_decoded_tmp"
  else
    rt="${zlrt}(zl-only)"; notes="${notes}; seq_validation_skipped"
  fi
  [ "$cb" -gt 0 ] || rt="FAIL"

  local pe te ce de mem
  pe=$(read_field "$tfp" elapsed_sec); te=$(read_field "$tft" elapsed_sec)
  ce=$(read_field "$tfc" elapsed_sec); de=$(read_field "$tfd" elapsed_sec)
  mem=$(read_field "$tfc" maxrss_kb)
  local comp_sec; comp_sec=$(awk -v a="${pe:-0}" -v b="${ce:-0}" 'BEGIN{printf "%.2f", a+b}')

  emit "\"$fname\",FASTA,$orig,ablation_harc_lossy,rl${HARC_READ_LEN}/mf${HARC_MISMATCH_FRAC}/t${THREADS},$cb,$(ratio "$orig" "$cb"),$comp_sec,${de:-NA},${mem:-NA},$rt,${notes}; preprocess_sec=${pe:-NA}; train_sec=${te:-NA}; compress_sec=${ce:-NA}"
  echo "  harc_method: ratio=$(ratio "$orig" "$cb")  encode=${comp_sec}s (prep ${pe:-NA} + comp ${ce:-NA})  decomp=${de:-NA}s  train=${te:-NA}s  roundtrip=$rt"
  rm -rf "$tdir"
}

# ---- Build ----------------------------------------------------------------
if [ "$SKIP_BUILD" != 1 ]; then
  bash "$HERE/scripts/build_all.sh" || echo "WARN: build_all.sh failed; falling back to existing binaries"
fi
[ -x "$ZLI" ] || { echo "FATAL: $ZLI not found/executable"; exit 1; }
[ -x "$PRE" ] || { echo "FATAL: $PRE not found/executable"; exit 1; }

# ---- The one universal FASTA model (train once, then compress-only) --------
if [ "${FORCE_RETRAIN_FASTA:-0}" = 1 ] || { [ ! -s "$FASTA_MODEL" ] && [ ! -s "${FASTA_MODEL%.zlc}.profile" ]; }; then
  echo "=== training the universal FASTA model -> $FASTA_MODEL ==="
  bash "$HERE/scripts/train_fasta_model.sh" "$FASTA_MODEL" || echo "WARN: FASTA model training failed - will use per-chunk serial fallback"
fi
echo "universal FASTA model: profile=$(cat "${FASTA_MODEL%.zlc}.profile" 2>/dev/null || echo none)$([ -s "$FASTA_MODEL" ] && echo " ($(stat -c%s "$FASTA_MODEL") bytes)")"

if [ "$RUN_HARC" = 1 ]; then
  if [ "$SKIP_BUILD" != 1 ]; then
    bash "$HERE/scripts/build_harc.sh" || echo "WARN: build_harc.sh failed"
  fi
  if [ ! -x "$HARC_PRE" ] || [ ! -x "$HARC_DECODE" ]; then
    echo "WARN: harc_preprocessor/harc_decode missing - disabling HARC variant"
    RUN_HARC=0
  fi
fi

# Generate the 1 GiB human sub-sample (5th datapoint) if referenced and missing.
SUB="$HERE/data/GRCh38_1GB_sample.fna"
SRC="$HERE/data/GCF_000001405.40_GRCh38.p14_genomic.fna"
for f in "${FASTA_LIST[@]}"; do
  if [ "$f" = "$SUB" ] && [ ! -f "$SUB" ] && [ -f "$SRC" ]; then
    echo "Creating 1 GiB sub-sample: $SUB"
    python3 "$HERE/scripts/make_train_sample.py" --in "$SRC" --out "$SUB" --target-mib 1024 || true
  fi
done

# ---- Main loop ----------------------------------------------------------
echo "THREADS=$THREADS  CSV=$RESULTS_CSV"
for input in "${FASTA_LIST[@]}"; do
  if [ ! -f "$input" ]; then echo "SKIP (missing): $input"; continue; fi
  fname="$(basename "$input")"
  base="$fname"
  orig=$(stat -c%s "$input")
  echo "=============================================================="
  echo "=== $fname   ($orig bytes)"
  echo "=============================================================="
  for t in $FASTA_METHODS; do
    run_std_tool "$input" "$orig" "$base" "$t" "$fname"
  done
  run_our_method "$input" "$orig" "$base" "$fname"
  [ "$RUN_HARC" = 1 ] && run_harc_method "$input" "$orig" "$base" "$fname"
  emit "\"$fname\",FASTA,$orig,spring,n/a,,,,,,,SPRING supports FASTQ only - not applicable to FASTA"
done

# ---- Summary ----------------------------------------------------------
python3 - "$RESULTS_CSV" > "$SUMMARY_TXT" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
byfile = {}
for r in rows:
    byfile.setdefault(r["file"], []).append(r)
w = "{:<22} {:>10} {:>9} {:>12} {:>12} {:>9}"
for f, rs in byfile.items():
    orig = int(rs[0]["orig_bytes"])
    print(f"\n### {f}   original = {orig/1e9:.3f} GB")
    print(w.format("tool", "ratio", "comp_MB", "comp_sec", "decomp_sec", "roundtrip"))
    print("-" * 78)
    for r in rs:
        cb = r["comp_bytes"]
        comp_mb = f"{int(cb)/1e6:.1f}" if cb.isdigit() else "-"
        print(w.format(r["tool"], r["ratio"] or "-", comp_mb,
                       r["comp_sec"] or "-", r["decomp_sec"] or "-", r["roundtrip"] or "-"))
print("\nCSV:", sys.argv[1])
PY

cat "$SUMMARY_TXT"
echo ""
echo "Done. Results: $RESULTS_CSV"
