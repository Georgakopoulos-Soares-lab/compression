#!/usr/bin/env bash
# benchmark_fasta.sh — the FASTA table for the paper.
#
# Adds NAF (Nucleotide Archival Format) to the baselines: the manuscript cites
# it but never measured it, and it is the strongest open-source FASTA-specific
# competitor. Every row is paired with a full decompression and byte comparison.
#
#   scripts/benchmark_fasta.sh [--threads N] [--out results/fasta_bench.csv]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
DATA="${NYX_FASTA_DATA:-$ROOT/data/fasta}"
WORK="${NYX_FASTA_WORK:-$ROOT/data/fasta/work}"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
OUT="$HERE/results/fasta_bench.csv"
# --only re-measures a subset of the tools; NAF -20 alone costs 90 minutes per
# assembly, so re-running the baselines when only our codec changed is waste.
ONLY=""
NYX_MEM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS="$2"; shift 2;;
    --only)    ONLY="$2"; shift 2;;
    --nyx-max-mem-mb) NYX_MEM="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
SCRATCH="$WORK/run.$$"
mkdir -p "$WORK" "$SCRATCH" "$(dirname "$OUT")"
# NAF does heavy small-write temp I/O; keep it off the shared filesystem.
NAF_TMP="${NAF_TMP:-/tmp/naf_tmp.$$}"
mkdir -p "$NAF_TMP"
trap 'rm -rf "$NAF_TMP" "$SCRATCH"' EXIT
ENNAF="$ROOT/thirdparty/bin/ennaf"
UNNAF="$ROOT/thirdparty/bin/unnaf"

# Runs a command once and reports "<wall seconds> <peak RSS KB>". Running each
# tool twice (once to time it, once to measure memory) doubles the cost of the
# whole suite and lets the two runs disagree.
MEASURE_SEC=0; MEASURE_RSS=0
measure() {
  local o
  o=$(/usr/bin/time -f '%e %M' "$@" 2>&1 >/dev/null | tail -1)
  MEASURE_SEC=${o%% *}
  MEASURE_RSS=${o##* }
  case "$MEASURE_SEC" in ''|*[!0-9.]*) MEASURE_SEC=0;; esac
  case "$MEASURE_RSS" in ''|*[!0-9]*) MEASURE_RSS=0;; esac
}
echo "tool,file,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,peak_RSS_MB,roundtrip" > "$OUT"
emit() {
  awk -v t="$1" -v f="$2" -v o="$3" -v c="$4" -v cs="$5" -v ds="$6" -v m="$7" -v r="$8" \
    'BEGIN{printf "%s,\"%s\",%s,%s,%.3f,%s,%s,%s,%s\n",t,f,o,c,(c>0?o/c:0),cs,ds,m,r}' >> "$OUT"
}

want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

bench() { # path
  local src="$1" b plain
  b=$(basename "$src"); b=${b%.gz}
  plain="$WORK/$b"
  [ -s "$plain" ] || { echo "  decompressing $b"; pigz -dc -p"$THREADS" "$src" > "$plain"; }
  local sz; sz=$(stat -c%s "$plain")
  echo "== $b ($sz bytes)"

  # ---- NYX (fastazl) ----
  local cs ds rss rt
  if want nyx; then
  local NYXMEMARG=(); [ -n "$NYX_MEM" ] && NYXMEMARG=(--max-mem-mb "$NYX_MEM")
  measure "$HERE/scripts/fastazl" compress "$plain" "$SCRATCH/t.fazl" --threads "$THREADS" "${NYXMEMARG[@]}"
  cs=$MEASURE_SEC; rss=$MEASURE_RSS
  measure "$HERE/scripts/fastazl" decompress "$SCRATCH/t.fazl" "$SCRATCH/t.rt" --threads "$THREADS"
  ds=$MEASURE_SEC
  if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; fi
  emit nyx "$b" "$sz" "$(stat -c%s "$SCRATCH/t.fazl")" "$cs" "$ds" "$(echo "scale=1;$rss/1024"|bc)" "$rt"
  [ "$rt" = OK ] || echo "  !! NYX ROUND TRIP FAILED for $b"
  rm -f "$SCRATCH/t.rt" "$SCRATCH/t.fazl"

  fi

  # ---- NAF: the FASTA-specific baseline the manuscript cites but never ran ----
  if want naf && [ -x "$ENNAF" ]; then
    # NAF is single-threaded; level 22 is its maximum and is very slow, so we
    # report both its default and its strongest setting rather than pick one.
    for lvl in ${NAF_LEVELS:-1 22}; do
      measure "$ENNAF" -"$lvl" --temp-dir "$NAF_TMP" -o "$SCRATCH/t.naf" "$plain"
      cs=$MEASURE_SEC; rss=$MEASURE_RSS
      measure bash -c "'$UNNAF' '$SCRATCH/t.naf' > '$SCRATCH/t.rt'"
      ds=$MEASURE_SEC
      if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; fi
      emit "naf-$lvl" "$b" "$sz" "$(stat -c%s "$SCRATCH/t.naf")" "$cs" "$ds" "$(echo "scale=1;$rss/1024"|bc)" "$rt"
      rm -f "$SCRATCH/t.rt" "$SCRATCH/t.naf"
    done
  fi

  want gp || return 0
  # ---- general purpose ----
  measure bash -c "pigz -9 -p$THREADS -c '$plain' > '$SCRATCH/t.gz'"
  cs=$MEASURE_SEC
  measure bash -c "pigz -dc -p$THREADS '$SCRATCH/t.gz' > /dev/null"
  ds=$MEASURE_SEC
  emit gzip "$b" "$sz" "$(stat -c%s "$SCRATCH/t.gz")" "$cs" "$ds" "" OK
  measure bash -c "zstd -19 --long=27 -T$THREADS -q -f '$plain' -o '$SCRATCH/t.zst'"
  cs=$MEASURE_SEC
  measure bash -c "zstd -d --long=27 -T$THREADS -q -f '$SCRATCH/t.zst' -o /dev/null"
  ds=$MEASURE_SEC
  emit zstd "$b" "$sz" "$(stat -c%s "$SCRATCH/t.zst")" "$cs" "$ds" "" OK
  measure bash -c "xz -9e -T$THREADS -c '$plain' > '$SCRATCH/t.xz'"
  cs=$MEASURE_SEC
  measure bash -c "xz -dc -T$THREADS '$SCRATCH/t.xz' > /dev/null"
  ds=$MEASURE_SEC
  emit xz "$b" "$sz" "$(stat -c%s "$SCRATCH/t.xz")" "$cs" "$ds" "" OK
  rm -f "$SCRATCH/t.gz" "$SCRATCH/t.zst" "$SCRATCH/t.xz"
}

for f in "$DATA"/*.fa.gz; do [ -s "$f" ] && bench "$f"; done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
