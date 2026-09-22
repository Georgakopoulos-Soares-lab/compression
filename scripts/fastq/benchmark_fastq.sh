#!/usr/bin/env bash
# benchmark_fastq.sh — the FASTQ table for the paper, on the same harness as
# the FASTA, VCF and BED tables. Every row is paired with a full decompression
# and a byte comparison against the input.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
NYXFQZ="$HERE/openzl/nyxfqz_v2"
MODELS="${NYX_FASTQ_MODELS:-$HERE/artifacts/fastq_models}"
DATA="${FASTQ_DATA:-$ROOT/data/fastq}"
WORK="${FASTQ_WORK:-$ROOT/data/fastq/work}"
SPRING="$ROOT/thirdparty/bin/spring"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
MEMMB="${NYX_MAX_MEM_MB:-500}"
OUT="$HERE/results/fastq_bench.csv"
# --only re-measures a subset of the tools. The baselines take hours on a 28 GB
# library and do not change when only our codec does, so re-running them would
# burn a node to reproduce numbers we already have on this same harness.
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    --mem-mb)  MEMMB="$2"; shift 2;;
    --only)    ONLY="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
SCRATCH="$WORK/run.$$"
mkdir -p "$WORK" "$SCRATCH" "$(dirname "$OUT")"
trap 'rm -rf "$SCRATCH"' EXIT
[ -x "$NYXFQZ" ] || { echo "build first: scripts/fastq/build_nyxfqz.sh" >&2; exit 1; }

want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

MEASURE_SEC=0; MEASURE_RSS=0
measure() {
  local o; o=$(/usr/bin/time -f '%e %M' "$@" 2>&1 >/dev/null | tail -1)
  MEASURE_SEC=${o%% *}; MEASURE_RSS=${o##* }
  case "$MEASURE_SEC" in ''|*[!0-9.]*) MEASURE_SEC=0;; esac
  case "$MEASURE_RSS" in ''|*[!0-9]*) MEASURE_RSS=0;; esac
}
echo "tool,file,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,peak_RSS_MB,roundtrip" > "$OUT"
emit() { awk -v t="$1" -v f="$2" -v o="$3" -v c="$4" -v cs="$5" -v ds="$6" -v m="$7" -v r="$8" \
  'BEGIN{printf "%s,\"%s\",%s,%s,%.3f,%s,%s,%s,%s\n",t,f,o,c,(c>0?o/c:0),cs,ds,m,r}' >> "$OUT"; }

bench() {
  local gz="$1" b plain
  b=$(basename "$gz" .gz); plain="$WORK/$b"
  [ -s "$plain" ] || { echo "  decompressing $b"; pigz -dc -p"$THREADS" "$gz" > "$plain"; }
  local sz; sz=$(stat -c%s "$plain")
  echo "== $b ($sz bytes)"
  local cs ds rss rt

  # ---- NYX ----
  if want nyx; then
  measure "$NYXFQZ" compress "$MODELS" "$plain" "$SCRATCH/t.nyxz" "$THREADS" "$MEMMB"
  cs=$MEASURE_SEC; rss=$MEASURE_RSS
  if [ -s "$SCRATCH/t.nyxz" ]; then
    measure "$NYXFQZ" decompress "$SCRATCH/t.nyxz" "$SCRATCH/t.rt"
    ds=$MEASURE_SEC
    if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; echo "  !! NYX ROUND TRIP FAILED"; fi
    emit nyx "$b" "$sz" "$(stat -c%s "$SCRATCH/t.nyxz")" "$cs" "$ds" "$(echo "scale=1;$rss/1024"|bc)" "$rt"
  else
    emit nyx "$b" "$sz" 0 "$cs" "" "" ERROR; echo "  !! NYX produced no output"
  fi
  rm -f "$SCRATCH/t.nyxz" "$SCRATCH/t.rt"
  fi

  # ---- SPRING (format-specific) ----
  if want spring && [ -x "$SPRING" ]; then
    measure "$SPRING" -c -i "$plain" -o "$SCRATCH/t.spring" -t "$THREADS" --working-dir "$SCRATCH"
    cs=$MEASURE_SEC; rss=$MEASURE_RSS
    if [ -s "$SCRATCH/t.spring" ]; then
      measure "$SPRING" -d -i "$SCRATCH/t.spring" -o "$SCRATCH/t.rt" -t "$THREADS" --working-dir "$SCRATCH"
      ds=$MEASURE_SEC
      if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=DIFFERS; fi
      emit spring "$b" "$sz" "$(stat -c%s "$SCRATCH/t.spring")" "$cs" "$ds" "$(echo "scale=1;$rss/1024"|bc)" "$rt"
    else
      emit spring "$b" "$sz" 0 "$cs" "" "" ERROR
    fi
    rm -f "$SCRATCH/t.spring" "$SCRATCH/t.rt"
  fi

  # ---- general purpose ----
  want gp || return 0
  # Decompression is timed too: without it Figure 1's decompression panel has
  # only NYX and SPRING in it, which makes a two-point plot out of a six-tool
  # comparison. Decompression is cheap relative to -19/-9e compression.
  for spec in "gzip:pigz -9 -p$THREADS -c:pigz -dc -p$THREADS" \
              "zstd:zstd -19 --long=27 -T$THREADS -q -c:zstd -dc --long=27 -T$THREADS -q" \
              "xz:xz -9e -T$THREADS -c:xz -dc -T$THREADS"; do
    local n=${spec%%:*} rest=${spec#*:} c d
    c=${rest%%:*}; d=${rest#*:}
    measure bash -c "$c '$plain' > '$SCRATCH/t.out'"; cs=$MEASURE_SEC
    measure bash -c "$d '$SCRATCH/t.out' > '$SCRATCH/t.rt'"; ds=$MEASURE_SEC
    if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; fi
    emit "$n" "$b" "$sz" "$(stat -c%s "$SCRATCH/t.out")" "$cs" "$ds" "" "$rt"
    rm -f "$SCRATCH/t.out" "$SCRATCH/t.rt"
  done
}

for f in "$DATA"/*.fastq.gz; do [ -s "$f" ] && bench "$f"; done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
