#!/usr/bin/env bash
# duplication_profile.sh — exact-duplicate rate of each FASTQ library.
#
#   scripts/fastq/duplication_profile.sh [--reads N] [--out results/fastq_duplication.csv]
#
# Why this is a result and not a diagnostic. SPRING's ratio advantage over NYX
# on this corpus is not uniform: it ranges from 1.06x to 2.46x. The whole of
# that spread is explained by one property of the library that costs seconds to
# measure -- the fraction of reads that are byte-identical to another read.
# SPRING reorders reads and encodes each as a difference from its neighbour, so
# it converts that redundancy directly; a per-record transform cannot. Measuring
# it lets the paper say *which* libraries each tool suits, instead of reporting
# an average that describes neither.
#
# Sampling is a contiguous prefix, not a stride: a strided sample dilutes
# duplicate pairs by the stride factor and would understate the rate severely.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WORK="${FASTQ_WORK:-$ROOT/data/fastq/work}"
OUT="$HERE/results/fastq_duplication.csv"
READS=4000000
while [ $# -gt 0 ]; do
  case "$1" in
    --reads) READS="$2"; shift 2;;
    --out)   OUT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
TMP="$(mktemp -d "${TMPDIR:-/tmp}/dup.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$(dirname "$OUT")"
# Exact duplicates are only the extreme case. A 150 bp library can be 4.2%
# exact-duplicate and still highly redundant because its reads *overlap* -- and
# a reordering codec converts that too. `sort_gain` measures the whole of it:
# how much smaller the sequence stream gets when reads are sorted, which is
# precisely the redundancy an ordering-based codec can reach and a per-record
# transform cannot.
echo "file,reads_sampled,read_len,distinct_reads,dup_rate_pct,zstd_bytes,sorted_bytes,sort_gain" > "$OUT"

for f in "$WORK"/*.fastq; do
  [ -s "$f" ] || continue
  b=$(basename "$f")
  head -n $((READS * 4)) "$f" | awk 'NR%4==2' > "$TMP/s.txt" 2>/dev/null || true
  n=$(wc -l < "$TMP/s.txt")
  [ "$n" -gt 0 ] || continue
  u=$(sort -S2G --parallel=4 -u "$TMP/s.txt" | wc -l)
  len=$(awk 'NR==1{print length($0); exit}' "$TMP/s.txt")
  z=$(zstd -19 --long=27 -T4 -q -c "$TMP/s.txt" | wc -c)
  t=$(sort -S2G --parallel=4 "$TMP/s.txt" | zstd -19 --long=27 -T4 -q -c | wc -c)
  awk -v f="$b" -v n="$n" -v l="$len" -v u="$u" -v z="$z" -v t="$t" \
    'BEGIN{printf "\"%s\",%d,%d,%d,%.1f,%d,%d,%.2f\n", f, n, l, u, 100*(1-u/n), z, t, z/t}' >> "$OUT"
  printf '  %-24s %3d bp  %5.1f%% duplicate  sort_gain %.2fx\n' "$b" "$len" \
    "$(awk -v n=$n -v u=$u 'BEGIN{print 100*(1-u/n)}')" \
    "$(awk -v z=$z -v t=$t 'BEGIN{print z/t}')"
  rm -f "$TMP/s.txt"
done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
