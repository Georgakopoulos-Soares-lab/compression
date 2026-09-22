#!/usr/bin/env bash
# thread_scaling.sh — how NYX's throughput and memory move with the thread count.
#
#   scripts/thread_scaling.sh [--threads "1 2 4 8 16"] [--out results/thread_scaling.csv]
#
# Every speed claim in the paper is measured at 16 threads, which invites the
# obvious objection: is NYX faster than xz -9e, or just more parallel? The
# answer has to be measured, not asserted, so this runs one representative file
# per format across thread counts. The single-thread row is the one that matters
# -- if NYX still beats xz -9e there, the speed argument is about the transform
# and not about the core count.
#
# NYX only: the baselines' scaling is their own business and re-running xz -9e
# on 18 GB at one thread would cost hours to say something already known.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/results/thread_scaling.csv"
THREADS_LIST="1 2 4 8 16"
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS_LIST="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

SCRATCH="$ROOT/data/scale.$$"
mkdir -p "$SCRATCH" "$(dirname "$OUT")"
trap 'rm -rf "$SCRATCH"' EXIT

MEASURE_SEC=0; MEASURE_RSS=0
measure() {
  local o; o=$(/usr/bin/time -f '%e %M' "$@" 2>&1 >/dev/null | tail -1)
  MEASURE_SEC=${o%% *}; MEASURE_RSS=${o##* }
  case "$MEASURE_SEC" in ''|*[!0-9.]*) MEASURE_SEC=0;; esac
  case "$MEASURE_RSS" in ''|*[!0-9]*) MEASURE_RSS=0;; esac
}

echo "format,file,orig_bytes,threads,comp_bytes,ratio,comp_sec,decomp_sec,comp_MBps,peak_RSS_MB,roundtrip" > "$OUT"

# One representative file per format, matching plot_paper_figures.REPRESENTATIVE.
run() { # <format> <path>
  local fmt="$1" f="$2" b sz t z r cs ds rss rt
  [ -s "$f" ] || { echo "skip $fmt: no $f"; return; }
  b=$(basename "$f"); sz=$(stat -c%s "$f")
  echo "== $fmt $b ($sz bytes)"
  for t in $THREADS_LIST; do
    z="$SCRATCH/t.z"; r="$SCRATCH/t.rt"; rm -f "$z" "$r"
    measure "$HERE/nyx" compress "$f" "$z" --threads "$t" --quiet
    cs=$MEASURE_SEC; rss=$MEASURE_RSS
    if [ ! -s "$z" ]; then echo "  !! $t threads produced nothing"; continue; fi
    measure "$HERE/nyx" decompress "$z" "$r" --threads "$t"
    ds=$MEASURE_SEC
    if cmp -s "$f" "$r"; then rt=OK; else rt=MISMATCH; echo "  !! ROUND TRIP FAILED at $t threads"; fi
    awk -v fm="$fmt" -v fi="$b" -v o="$sz" -v th="$t" -v c="$(stat -c%s "$z")" \
        -v cs="$cs" -v ds="$ds" -v m="$rss" -v rt="$rt" \
      'BEGIN{printf "%s,\"%s\",%s,%s,%s,%.3f,%s,%s,%.1f,%.1f,%s\n",
             fm,fi,o,th,c,(c>0?o/c:0),cs,ds,(cs>0?o/cs/1e6:0),m/1024,rt}' >> "$OUT"
    printf '  %2s threads: %6.2fx  %7.1fs  %7.1f MB/s  %6.2f GB  %s\n' \
      "$t" "$(awk -v o=$sz -v c=$(stat -c%s "$z") 'BEGIN{print o/c}')" \
      "$cs" "$(awk -v o=$sz -v s=$cs 'BEGIN{print (s>0?o/s/1e6:0)}')" \
      "$(awk -v m=$rss 'BEGIN{print m/1048576}')" "$rt"
    rm -f "$z" "$r"
  done
}

# Mid-sized inputs rather than the largest: the point is the shape of the curve,
# and a single-threaded pass over an 11 GB panel costs an hour to make the same
# point a 2.8 GB call set makes in a minute. All are whole files, not prefixes.
run VCF   "${NYX_VCF_WORK:-$ROOT/data/vcf/work}/HG002_GRCh38_v4.2.1.vcf"
run FASTA "${NYX_FASTA_WORK:-$ROOT/data/fasta/work}/GRCm39.fa"
run FASTQ "${FASTQ_WORK:-$ROOT/data/fastq/work}/ERR9539086.fastq"

echo; echo "wrote $OUT"; column -s, -t "$OUT"
