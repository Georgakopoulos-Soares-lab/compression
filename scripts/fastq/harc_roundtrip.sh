#!/usr/bin/env bash
# Validate lossless round-trip and report compression ratios for the
# nyxfqz_v2 native-clustering pipeline, on the SAME sample files
# roundtrip.sh uses by default (including ones not used for training --
# same robustness check). Writes benchmarks.txt-compatible rows (algorithm
# "harc") to harc_benchmarks.txt so they can be appended next to
# nyx_dual / spring_lossless for direct comparison.
#
# Usage: scripts/harc_roundtrip.sh [file1.fastq.gz ...]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HARC_COMPRESS="$ROOT/scripts/harc_compress.sh"
HARC_DECOMPRESS="$ROOT/scripts/harc_decompress.sh"
WORK="$ROOT/build/harc_rt"
mkdir -p "$WORK"
export NYX_CLUSTER_LOG="${NYX_CLUSTER_LOG:-1}"   # so gate decisions are visible by default

if [[ $# -gt 0 ]]; then
  FILES=("$@")
else
  FILES=(
    "$ROOT/data/fastq/ERR9539086.fastq.gz"
    "$ROOT/data/fastq/SRR8899104.part.fastq.gz"
    "$ROOT/data/fastq/SRR1770413_1.part.fastq.gz"
    "$ROOT/data/fastq/SRR062634_1.part.fastq.gz"
  )
fi

if [[ ! -x "$HARC_COMPRESS" || ! -x "$HARC_DECOMPRESS" ]]; then
  echo "error: scripts/harc_compress.sh and scripts/harc_decompress.sh must exist and be executable" >&2
  exit 1
fi

OUT_TABLE="$ROOT/harc_benchmarks.txt"
{
  echo "| file | original_bytes | original_human | algorithm | compressed_bytes | compressed_human | ratio | compress_s | decompress_s | peak_ram_mb | roundtrip |"
  echo "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---|"
} > "$OUT_TABLE"

printf "%-28s %14s %14s %8s %14s %8s %s\n" "file" "raw_bytes" "nyxz_bytes" "vs_raw" "c_s/d_s" "peak_mb" "roundtrip"

fail=0
for src in "${FILES[@]}"; do
  [[ -f "$src" ]] || { echo "skip (missing): $src"; continue; }
  base="$(basename "$src")"
  plain="$WORK/$base.plain"
  nyxz="$WORK/$base.nyxz"
  back="$WORK/$base.back"

  if [[ "$src" == *.gz ]]; then
    gzip -dc "$src" >"$plain" 2>/dev/null || true
  else
    cp "$src" "$plain"
  fi

  t0=$(date +%s.%N)
  # Run through /usr/bin/time, write the Max RSS (in KB) to a temp file
  /usr/bin/time -o "$WORK/$base.time" -f "%M" "$HARC_COMPRESS" "$plain" "$nyxz" >/dev/null || { echo "compress FAILED: $base"; fail=1; continue; }
  t1=$(date +%s.%N)
  "$HARC_DECOMPRESS" "$nyxz" "$back" >/dev/null || { echo "decompress FAILED: $base"; fail=1; continue; }
  t2=$(date +%s.%N)

  raw=$(wc -c <"$plain" | tr -d ' ')
  cz=$(wc -c <"$nyxz" | tr -d ' ')

  if cmp -s "$plain" "$back"; then rt="OK"; else rt="MISMATCH"; fail=1; fi

  vraw=$(awk "BEGIN{ if ($cz>0) printf \"%.3fx\", $raw/$cz; else print \"-\" }")
  csec=$(awk "BEGIN{ printf \"%.2f\", $t1-$t0 }")
  dsec=$(awk "BEGIN{ printf \"%.2f\", $t2-$t1 }")
  raw_h=$(awk "BEGIN{ printf \"%.2f MB\", $raw/1000000 }")
  cz_h=$(awk "BEGIN{ printf \"%.2f MB\", $cz/1000000 }")

  # Calculate peak RAM in MB
  max_kb=$(cat "$WORK/$base.time")
  peak_mb=$(awk "BEGIN{ printf \"%.0f\", $max_kb/1024 }")

  printf "%-28s %14s %14s %8s %6ss/%-5ss %8s %s\n" "$base" "$raw" "$cz" "$vraw" "$csec" "$dsec" "$peak_mb" "$rt"
  echo "| $base | $raw | $raw_h | harc | $cz | $cz_h | $vraw | $csec | $dsec | $peak_mb | $rt |" >> "$OUT_TABLE"

  rm -f "$plain" "$back"
done

if [[ "$fail" -ne 0 ]]; then
  echo "RESULT: FAILURES DETECTED" >&2
  echo ">> partial results in $OUT_TABLE"
  exit 1
fi
echo "RESULT: all round-trips lossless"
echo ">> benchmark rows written to $OUT_TABLE -- merge into benchmarks.txt to compare against nyx_dual, spring_lossless, etc."
