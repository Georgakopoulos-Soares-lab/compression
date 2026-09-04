#!/usr/bin/env bash
#-----------------------------------------------------------------------
# paper_bench.sh
#
# Whole-file, highest-compression benchmark of general-purpose compressors
# (gzip, pigz, zstd, 7-zip, xz) + SPRING vs. our method (nyxfqz_v2 with
# NYX_CLUSTER=auto, aka the "HARC" clustering pipeline -- native minimizer
# read reordering, no external HARC tool involved).
#
# For every input the raw (decompressed) FASTQ is the reference: ratio =
# raw_bytes / compressed_bytes. Each method is timed for compress AND
# decompress under /usr/bin/time -v (wall + Max RSS) and verified for a
# byte-exact lossless round-trip (cmp).
#
# Exact commands (highest-ratio settings):
#   gzip   : gzip -9
#   pigz   : pigz -9 -p $T                 (ratio == gzip -9, parallel)
#   zstd   : zstd -19 --long=27 -T$T          (matches FASTA/VCF benchmark)
#   7zip   : 7z a -mx=9 -mmt=$T            (LZMA2)
#   xz     : xz -9e -T$T --block-size=192MiB  (matches FASTA/VCF benchmark)
#   spring : spring -c -t $T   (adds -l automatically for variable-length)
#   ours   : NYX_CLUSTER=auto NYX_CLEVEL=19 nyxfqz_v2 compress <model> ... $T 500
#            model = fastq_var.zc for ERR9539079/086/093, else fastq_fixed_cluster.zc
#
# Usage: scripts/paper_bench.sh [file1.fastq.gz ...]
#   (no args -> the 8 files in data/fastq/)
# Env: T=<threads>  FQBENCH=<scratch dir>  METHODS="gzip pigz zstd 7zip xz spring ours"
#-----------------------------------------------------------------------
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

T="${T:-48}"
FQBENCH="${FQBENCH:-/scratch/11579/alexmargaris/paper_bench}"
NYXFQZ="$ROOT/openzl/nyxfqz_v2"
SPRING="${SPRING:-/scratch/11579/alexmargaris/SPRING/build/spring}"
METHODS="${METHODS:-gzip pigz zstd 7zip xz spring ours}"
TIME=/usr/bin/time
mkdir -p "$FQBENCH"

export NYX_SEQ_ROUTE=zstd
export NYX_CLEVEL=19
export NYX_CLUSTER_LOG=1

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  FILES=(
    data/fastq/DRR206632.fastq.gz
    data/fastq/ERR4186945_1.fastq.gz
    data/fastq/ERR9539079.fastq.gz
    data/fastq/ERR9539086.fastq.gz
    data/fastq/ERR9539093.fastq.gz
    data/fastq/SRR062634_1.fastq.gz
    data/fastq/SRR1770413_1.fastq.gz
    data/fastq/SRR8899104.fastq.gz
  )
fi

TABLE="${PAPER_BENCH_TABLE:-$ROOT/results/paper_benchmarks.txt}"
if [[ ! -f "$TABLE" ]]; then
  {
    echo "# Whole-file highest-compression benchmark  (T=$T threads, node=$(hostname), $(date -u +%Y-%m-%dT%H:%MZ))"
    echo "# ratio = raw_FASTQ_bytes / compressed_bytes"
    echo "| file | raw_bytes | method | settings | compressed_bytes | ratio | compress_s | decompress_s | compress_peak_mb | roundtrip |"
    echo "|---|---:|---|---|---:|---:|---:|---:|---:|---|"
  } > "$TABLE"
fi

# nyxfqz model per dataset
# Model library. Defaults to the mirror in the parent repo's artifacts/ so the
# models are discoverable alongside the VCF models; falls back to the in-tree
# copy. Override with NYX_MODEL_DIR.
NYX_MODEL_DIR="${NYX_MODEL_DIR:-$ROOT/artifacts/fastq_models}"
# ONE universal Illumina model for every dataset (compress-only; no per-file
# training). Falls back to the old dual names if the universal one is absent.
nyx_model() {
  if [ -s "$NYX_MODEL_DIR/fastq_illumina.zc" ]; then
    echo "$NYX_MODEL_DIR/fastq_illumina.zc"
  else
    case "$1" in
      ERR9539079*|ERR9539086*|ERR9539093*) echo "$NYX_MODEL_DIR/fastq_var.zc" ;;
      *) echo "$NYX_MODEL_DIR/fastq_fixed_cluster.zc" ;;
    esac
  fi
}

# run_method <label> <ccmd> <dcmd-to-stdout> <cfile> <ref>
#   ccmd     : shell string, writes the compressed archive to <cfile>
#   dcmd     : shell string, writes the RECONSTRUCTED plaintext to stdout
# Verification streams dcmd's stdout straight into `cmp` -- no intermediate
# file on Lustre (a reused+deleted path there intermittently returns
# "Stale file handle"). Sets C_S D_S PEAK_MB CBYTES RT.
run_method() {
  local label="$1" ccmd="$2" dcmd="$3" cfile="$4" ref="$5"
  local tf="$FQBENCH/.time"
  rm -f "$cfile"

  local t0 t1 t2 drc
  t0=$(date +%s.%N)
  if ! $TIME -v -o "$tf" bash -c "$ccmd" >/dev/null 2>"$FQBENCH/.err"; then
    echo "  $label: COMPRESS FAILED"; cat "$FQBENCH/.err" >&2; RT="COMPRESS_FAIL"; return 1
  fi
  t1=$(date +%s.%N)
  PEAK_MB=$(awk '/Maximum resident/{printf "%.0f", $NF/1024}' "$tf")
  CBYTES=$(stat -c%s "$cfile" 2>/dev/null || echo 0)

  set -o pipefail
  bash -c "$dcmd" 2>"$FQBENCH/.err" | cmp -s - "$ref"
  drc=$?
  set +o pipefail
  t2=$(date +%s.%N)
  C_S=$(awk "BEGIN{printf \"%.1f\", $t1-$t0}")
  D_S=$(awk "BEGIN{printf \"%.1f\", $t2-$t1}")
  case $drc in
    0) RT="OK" ;;
    1) RT="MISMATCH"; echo "  $label: ROUND-TRIP MISMATCH" ;;
    *) RT="DECOMPRESS_FAIL"; echo "  $label: DECOMPRESS FAILED"; cat "$FQBENCH/.err" >&2 ;;
  esac
}

emit() {
  local file="$1" raw="$2" method="$3" settings="$4"
  local ratio; ratio=$(awk "BEGIN{ if ($CBYTES>0) printf \"%.3f\", $raw/$CBYTES; else print \"-\" }")
  printf "| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |\n" \
    "$file" "$raw" "$method" "$settings" "$CBYTES" "$ratio" "$C_S" "$D_S" "$PEAK_MB" "$RT" >> "$TABLE"
  printf "  %-8s ratio=%-7s c=%-7ss d=%-7ss peak=%-7sMB %s\n" "$method" "$ratio" "$C_S" "$D_S" "$PEAK_MB" "$RT"
}

for src in "${FILES[@]}"; do
  [[ -f "$src" ]] || { echo "skip (missing): $src"; continue; }
  base="$(basename "$src" .fastq.gz)"; base="${base%.fastq}"
  echo "==================== $base ===================="
  fq="$FQBENCH/$base.fastq"
  if [[ ! -f "$fq" ]]; then
    echo "  decompressing $src ..."
    pigz -dc -p "$T" "$src" > "$fq" || gzip -dc "$src" > "$fq"
  fi
  raw=$(stat -c%s "$fq")
  echo "  raw_bytes=$raw ($(awk "BEGIN{printf \"%.2f GB\", $raw/1e9}"))"

  for m in $METHODS; do
    case "$m" in
      gzip)
        run_method gzip \
          "gzip -9 -c '$fq' > '$FQBENCH/o.gz'" \
          "gzip -dc '$FQBENCH/o.gz'" \
          "$FQBENCH/o.gz" "$fq"
        emit "$base" "$raw" gzip "gzip -9" ;;
      pigz)
        run_method pigz \
          "pigz -9 -p $T -c '$fq' > '$FQBENCH/o.pgz'" \
          "pigz -dc -p $T '$FQBENCH/o.pgz'" \
          "$FQBENCH/o.pgz" "$fq"
        emit "$base" "$raw" pigz "pigz -9 -p$T" ;;
      zstd)
        run_method zstd \
          "zstd -q -f -19 --long=27 -T$T -o '$FQBENCH/o.zst' '$fq'" \
          "zstd -q -dc --long=27 '$FQBENCH/o.zst'" \
          "$FQBENCH/o.zst" "$fq"
        emit "$base" "$raw" zstd "zstd -19 --long=27 -T$T" ;;
      7zip)
        rm -f "$FQBENCH/o.7z"
        run_method 7zip \
          "7z a -mx=9 -mmt=$T -bso0 -bsp0 '$FQBENCH/o.7z' '$fq'" \
          "7z e -mmt=$T -bso0 -bsp0 -bd -so '$FQBENCH/o.7z'" \
          "$FQBENCH/o.7z" "$fq"
        emit "$base" "$raw" 7zip "7z a -mx=9 -mmt=$T" ;;
      xz)
        run_method xz \
          "xz -9e -T$T --block-size=192MiB -c '$fq' > '$FQBENCH/o.xz'" \
          "xz -dc -T$T '$FQBENCH/o.xz'" \
          "$FQBENCH/o.xz" "$fq"
        emit "$base" "$raw" xz "xz -9e -T$T --block-size=192MiB" ;;
      spring)
        spring_setting="spring -c -t$T"
        rm -f "$FQBENCH/o.spring"
        s0=$(date +%s.%N)
        if $TIME -v -o "$FQBENCH/.time" "$SPRING" -c -i "$fq" -o "$FQBENCH/o.spring" -t "$T" -w "$FQBENCH" >/dev/null 2>"$FQBENCH/.err"; then
          :
        elif $TIME -v -o "$FQBENCH/.time" "$SPRING" -c -l -i "$fq" -o "$FQBENCH/o.spring" -t "$T" -w "$FQBENCH" >/dev/null 2>"$FQBENCH/.err"; then
          spring_setting="spring -c -l -t$T"
        else
          echo "  spring: COMPRESS FAILED"; cat "$FQBENCH/.err" >&2
          CBYTES=0; C_S="-"; D_S="-"; PEAK_MB="-"; RT="COMPRESS_FAIL"; emit "$base" "$raw" spring "$spring_setting"; continue
        fi
        s1=$(date +%s.%N)
        PEAK_MB=$(awk '/Maximum resident/{printf "%.0f", $NF/1024}' "$FQBENCH/.time")
        CBYTES=$(stat -c%s "$FQBENCH/o.spring")
        C_S=$(awk "BEGIN{printf \"%.1f\", $s1-$s0}")
        sback="$FQBENCH/sback.$$.$RANDOM"
        if "$SPRING" -d -i "$FQBENCH/o.spring" -o "$sback" -t "$T" -w "$FQBENCH" >/dev/null 2>"$FQBENCH/.err"; then
          s2=$(date +%s.%N); D_S=$(awk "BEGIN{printf \"%.1f\", $s2-$s1}")
          cmp -s "$fq" "$sback" && RT="OK" || RT="MISMATCH"
        else
          echo "  spring: DECOMPRESS FAILED"; cat "$FQBENCH/.err" >&2; D_S="-"; RT="DECOMPRESS_FAIL"
        fi
        rm -f "$sback" "$FQBENCH/o.spring"
        emit "$base" "$raw" spring "$spring_setting" ;;
      ours)
        model=$(nyx_model "$base")
        nback="$FQBENCH/nback.$$.$RANDOM"
        run_method ours \
          "NYX_CLUSTER=auto '$NYXFQZ' compress '$model' '$fq' '$FQBENCH/o.nyxz' $T 500" \
          "'$NYXFQZ' decompress '$FQBENCH/o.nyxz' '$nback' >/dev/null 2>&1 && cat '$nback' && rm -f '$nback'" \
          "$FQBENCH/o.nyxz" "$fq"
        emit "$base" "$raw" ours "NYX_CLUSTER=auto L19 $(basename "$model")" ;;
    esac
  done

  rm -f "$fq"
  echo "  (removed $fq)"
done

echo "DONE -> $TABLE"
