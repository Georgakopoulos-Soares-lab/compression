#!/usr/bin/env bash
# retrain_paper_models.sh — train the ONE universal Illumina FASTQ model that
# ships with the method. Runtime is compress-only; the end user never trains.
#
#   fastq_illumina.zc   variable- AND fixed-length Illumina, clustered or not
#
# The training corpus mixes variable-length and fixed-length reads, half packed
# WITH global clustering (NYX_CLUSTER=1) and half without, so the single model
# sees every stream shape paper_bench.sh will feed it at runtime.
#
# Output -> ../artifacts/fastq_models/ (the dir paper_bench.sh reads by default).
#
#   scripts/retrain_paper_models.sh
# NOTE: no `pipefail` -- `gzip -dc | head` makes gzip exit non-zero on SIGPIPE.
set -eu

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYX="$ROOT/openzl/nyxfqz_v2"
FQ="$ROOT/data/fastq"
OUT="$ROOT/artifacts/fastq_models"
MODEL="$OUT/fastq_illumina.zc"
TMP="${TMPDIR:-/tmp}/nyx_retrain.$$"
mkdir -p "$OUT" "$TMP/pk"
trap 'rm -rf "$TMP"' EXIT
[ -x "$NYX" ] || { echo "build nyxfqz_v2 first"; exit 1; }

echo "== toolchain: $(cd "$ROOT/openzl" && git rev-parse --short HEAD)  $("$ROOT/openzl/zli" --version 2>/dev/null | tail -1) =="

READS="${NYX_TRAIN_READS:-7000}"   # per sample; keep total corpus in the OpenZL arena (~15-25 MB)

sl() { # src off n dest
  gzip -dc "$1" | tail -n +"$(( $2 * 4 + 1 ))" | head -n "$(( $3 * 4 ))" > "$4"
}

# name : dataset : read-offset : cluster(0/1)
SAMPLES=(
  "v0:ERR9539079:0:0"
  "v1:ERR9539086:0:0"
  "v2:ERR9539093:0:0"
  "f0:SRR8899104:0:0"
  "f1:ERR4186945_1:0:1"
  "f2:DRR206632:0:1"
  "f3:SRR8899104:$READS:1"
)

i=0
for spec in "${SAMPLES[@]}"; do
  IFS=: read -r nm ds off cl <<<"$spec"
  src="$FQ/${ds}.fastq.gz"
  [ -f "$src" ] || { echo "  skip (missing) $src"; continue; }
  sl "$src" "$off" "$READS" "$TMP/$nm.fastq"
  if [ "$cl" = 1 ]; then
    NYX_CLUSTER=1 "$NYX" pack "$TMP/$nm.fastq" "$TMP/pk/s$i.fqzc" 2>/dev/null
  else
    "$NYX" pack "$TMP/$nm.fastq" "$TMP/pk/s$i.fqzc" 2>/dev/null
  fi
  echo "  + $nm  ($ds off=$off cluster=$cl)"
  i=$((i+1))
done
[ "$i" -gt 0 ] || { echo "no samples packed"; exit 1; }

echo "== training fastq_illumina.zc on $i samples =="
NYX_SEQ_ROUTE=zstd "$NYX" train "$TMP/pk" "$MODEL"
[ -s "$MODEL" ] || { echo "FATAL: training produced no model"; exit 1; }
echo "  -> $MODEL  ($(stat -c%s "$MODEL") bytes)"

# keep the old dual names as symlinks so an un-updated paper_bench.sh still works
ln -sf fastq_illumina.zc "$OUT/fastq_var.zc"
ln -sf fastq_illumina.zc "$OUT/fastq_fixed_cluster.zc"

echo "== verify (byte-exact round trip on fresh slices) =="
for ds in ERR9539086 SRR062634_1 DRR206632; do
  src="$FQ/${ds}.fastq.gz"; [ -f "$src" ] || continue
  sl "$src" 60000 20000 "$TMP/chk.fastq"
  NYX_CLUSTER=auto "$NYX" compress "$MODEL" "$TMP/chk.fastq" "$TMP/chk.nyxz" 8 500 >/dev/null 2>&1
  "$NYX" decompress "$TMP/chk.nyxz" "$TMP/chk.back" >/dev/null 2>&1
  if cmp -s "$TMP/chk.fastq" "$TMP/chk.back"; then
    r=$(awk -v a="$(stat -c%s "$TMP/chk.fastq")" -v b="$(stat -c%s "$TMP/chk.nyxz")" 'BEGIN{printf "%.2f", a/b}')
    echo "  OK  $ds  ratio=$r  BYTE-EXACT"
  else
    echo "  FAIL $ds  round-trip mismatch"; exit 1
  fi
done
echo "done."
