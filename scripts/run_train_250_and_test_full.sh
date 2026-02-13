#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- User-tunable knobs (via env) ----
THREADS="${THREADS:-16}"                 # OpenZL training threads + xargs parallelism
TARGET_MIB="${TARGET_MIB:-200}"           # training FASTA size target (~200MiB part)
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"    # ~30 minutes

# Compressing the *full* FASTA spawns multiple processes (one per chunk).
# This can be memory heavy; you can lower this independently from THREADS.
COMPRESS_JOBS="${COMPRESS_JOBS:-$THREADS}"

# Some trained successor graphs (ACE) can be brittle/large on unseen chunks.
# Default to disabling ACE successors for robustness when testing on full FASTA.
NO_ACE_SUCCESSORS="${NO_ACE_SUCCESSORS:-1}"

# Preprocessing thread counts:
# - for the ~250MiB training sample we prefer a *single* output chunk, so use 1 thread.
# - for the full FASTA we use more threads (but output is still chunked by size/boundaries).
TRAIN_PRE_THREADS="${TRAIN_PRE_THREADS:-1}"
FULL_PRE_THREADS="${FULL_PRE_THREADS:-16}"

# Validation mode for the full FASTA: "1" validates all chunks (slow, but thorough)
VALIDATE_FULL="${VALIDATE_FULL:-1}"

# ---- Paths ----
SCHEMA="$HERE/schemas/fasta_packed.sddl"
ZLI="$HERE/openzl/zli"
PRE="$HERE/tools/biocompress_preprocessor"

DATA_DIR="$HERE/data"
OUT_DIR="$HERE/out"
ART_DIR="$HERE/artifacts"
TIME_DIR="$OUT_DIR/timing"

TRAIN_FASTA="$OUT_DIR/train_${TARGET_MIB}MiB.fasta"
TRAIN_CHUNKS="$HERE/chunks_train_${TARGET_MIB}MiB"
FULL_CHUNKS="$HERE/chunks_full"

COMPRESSOR="$ART_DIR/fasta_packed_train_${TARGET_MIB}MiB_t${THREADS}.compressor"

mkdir -p "$DATA_DIR" "$OUT_DIR" "$ART_DIR" "$TIME_DIR"

time_cmd() {
  local outfile="$1"
  shift
  # macOS /usr/bin/time does not support -f.
  # We just use -p (POSIX) or default output, and redirect to file.
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
  # Sums sizes of files passed as arguments.
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
"$HERE/scripts/build_all.sh"
require_exec "$ZLI"
require_exec "$PRE"

# ---- 1) Download full FASTA ----
"$HERE/scripts/download_fasta.sh" >/dev/null
FASTA_IN="${FASTA_OUT:-$HERE/data/GCF_000001635.27_GRCm39_genomic.fna}"
if [ ! -f "$FASTA_IN" ]; then
  FASTA_IN="$(ls -1 "$HERE/data"/*.fna 2>/dev/null | head -n1 || true)"
fi
if [ -z "$FASTA_IN" ] || [ ! -f "$FASTA_IN" ]; then
  echo "Error: FASTA input not found under $HERE/data" >&2
  exit 1
fi

echo "Full FASTA: $FASTA_IN"

orig_bytes="$(get_file_size "$FASTA_IN")"
echo "Original FASTA size: $(human_mib "$orig_bytes")"

# ---- 2) Create ~TARGET_MIB training FASTA (record-safe) ----
echo "Creating training FASTA (~${TARGET_MIB} MiB): $TRAIN_FASTA"
python3 "$HERE/scripts/make_train_sample.py" --in "$FASTA_IN" --out "$TRAIN_FASTA" --target-mib "$TARGET_MIB"

# ---- 3) Preprocess training FASTA to packed .bin ----
rm -rf "$TRAIN_CHUNKS" && mkdir -p "$TRAIN_CHUNKS"
echo "Preprocessing training FASTA -> packed chunks: $TRAIN_CHUNKS (threads=$TRAIN_PRE_THREADS)"
"$PRE" "$TRAIN_FASTA" "$TRAIN_CHUNKS" "$TRAIN_PRE_THREADS" fasta_packed

TRAIN_BINS=("$TRAIN_CHUNKS"/*.fasta_packed.bin)
if [ ! -f "${TRAIN_BINS[0]}" ]; then
  echo "Error: no training .bin chunks produced in $TRAIN_CHUNKS" >&2
  exit 1
fi

train_bin_bytes="$(bytes_sum "${TRAIN_BINS[@]}")"
echo "Training packed payload size: $(human_mib "$train_bin_bytes")"

# ---- 4) Train compressor (bounded to ~30 min by default) ----
echo "Training compressor (threads=$THREADS, max_time_secs=$MAX_TIME_SECS): $COMPRESSOR"
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

# ---- 5) Validate on the training chunk(s) ----
echo "Validating compressor on training chunks (compress/decompress/cmp)"
find "$TRAIN_CHUNKS" -maxdepth 1 -type f -name '*.fasta_packed.bin' -print0 | \
  xargs -0 -P "$THREADS" -I {} bash -c '"$0" compress "$1" --compressor "$2" --output "$1.zl" --force > /dev/null' \
    "$ZLI" {} "$COMPRESSOR"

FAIL=0
for bin in "$TRAIN_CHUNKS"/*.fasta_packed.bin; do
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

# ---- 6) Preprocess full FASTA ----
rm -rf "$FULL_CHUNKS" && mkdir -p "$FULL_CHUNKS"
echo "Preprocessing FULL FASTA -> packed chunks: $FULL_CHUNKS (threads=$FULL_PRE_THREADS)"
time_cmd "$TIME_DIR/prep_full.time" \
  "$PRE" "$FASTA_IN" "$FULL_CHUNKS" "$FULL_PRE_THREADS" fasta_packed

FULL_BINS=("$FULL_CHUNKS"/*.fasta_packed.bin)
if [ ! -f "${FULL_BINS[0]}" ]; then
  echo "Error: no full .bin chunks produced in $FULL_CHUNKS" >&2
  exit 1
fi

full_in_bytes="$(bytes_sum "${FULL_BINS[@]}")"
echo "Full packed input total: $(human_mib "$full_in_bytes")"

# ---- 7) Compress full chunks ----
echo "Compressing FULL chunks with trained compressor (parallel=$THREADS)"
time_cmd "$TIME_DIR/openzl_comp_full.time" \
  bash -c 'find "$1" -maxdepth 1 -type f -name "*.fasta_packed.bin" -print0 | \
      xargs -0 -P "$2" -I {} "$3" compress "{}" --compressor "$4" --output "{}.zl" --force' \
    _ "$FULL_CHUNKS" "$COMPRESS_JOBS" "$ZLI" "$COMPRESSOR"

FULL_ZLS=("$FULL_CHUNKS"/*.fasta_packed.bin.zl)
full_out_bytes="$(bytes_sum "${FULL_ZLS[@]}")"

# OpenZL ratio vs original FASTA
python3 - <<PY
orig_b=$orig_bytes
out_b=$full_out_bytes
ratio = (orig_b / out_b) if out_b else float('inf')
print(f"OpenZL vs original FASTA: in={orig_b} bytes ({orig_b/1024/1024:.2f} MiB) out={out_b} bytes ({out_b/1024/1024:.2f} MiB) ratio={ratio:.3f}x")
PY

python3 - <<PY
in_b=$full_in_bytes
out_b=$full_out_bytes
ratio = (in_b / out_b) if out_b else float('inf')
print(f"Full compression: in={in_b} bytes ({in_b/1024/1024:.2f} MiB) out={out_b} bytes ({out_b/1024/1024:.2f} MiB) ratio={ratio:.3f}x")
PY

# Pigz baseline on ORIGINAL FASTA
if have_cmd pigz; then
  PIGZ_OUT_DIR="$OUT_DIR/pigz"
  mkdir -p "$PIGZ_OUT_DIR"
  PIGZ_OUT="$PIGZ_OUT_DIR/$(basename "$FASTA_IN").gz"
  echo "Running pigz -9 baseline on original FASTA -> $PIGZ_OUT"
  time_cmd "$TIME_DIR/pigz.time" \
    pigz -9 -p "$THREADS" -c "$FASTA_IN" > "$PIGZ_OUT"
  pigz_bytes="$(get_file_size "$PIGZ_OUT")"
  python3 - <<PY
orig_b=$orig_bytes
out_b=$pigz_bytes
ratio = (orig_b / out_b) if out_b else float('inf')
print(f"pigz -9 vs original FASTA: in={orig_b} bytes ({orig_b/1024/1024:.2f} MiB) out={out_b} bytes ({out_b/1024/1024:.2f} MiB) ratio={ratio:.3f}x")
PY
else
  echo "pigz not found; skipping pigz baseline"
fi

# ---- 8) Decompress + validate full ----
if [ "$VALIDATE_FULL" != "1" ]; then
  echo "Skipping full validation (set VALIDATE_FULL=1 to enable)"
else
  echo "Validating FULL chunks (decompress/cmp)"
  FAIL=0
  time_cmd "$TIME_DIR/full_validate.time" \
    bash -c '
      set -euo pipefail
      ZLI="$1"
      FAIL_FILE="$2"
      shopt -s nullglob
      for bin in "$0"/*.fasta_packed.bin; do
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

echo "Timing (seconds):"
for f in "$TIME_DIR"/*.time; do
  [ -f "$f" ] || continue
  echo "  $(basename "$f"): $(cat "$f")"
done

echo "Done. Outputs:"
echo "  Compressor:     $COMPRESSOR"
echo "  Train chunks:   $TRAIN_CHUNKS"
echo "  Full chunks:    $FULL_CHUNKS"
