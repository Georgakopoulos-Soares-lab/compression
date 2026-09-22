#!/usr/bin/env bash
# benchmark_bed.sh — the BED table for the paper.
#
# Every row, ours and the baselines', is paired with a full decompression and a
# byte comparison. A tool that does not reproduce its input does not get a
# number.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${BED_DATA:-$HERE/data/bed}"
# Every tool's archive and round-trip output goes to node-local disk, not to
# /scratch. /scratch is BeeGFS, and on files this small its write and metadata
# stalls dominate: measured, bgzip "took" 24.9 s to decompress a 25 MB file it
# decompresses in 0.2 s, and 7-Zip 29.5 s for one it extracts in 0.15 s. The
# stalls landed on baselines at random and happened to miss NYX, which would
# have biased the table in our favour. Same disk for every tool.
WORK="${BED_WORK:-${TMPDIR:-/tmp}/nyx_bed_bench}"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
SEVENZ="${SEVENZ:-$HERE/../thirdparty/bin/7zz}"
BGZIP="${BGZIP:-$HERE/../thirdparty/htslib-install/bin/bgzip}"
OUT="$HERE/results/bed_bench.csv"
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    --only)    ONLY="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
# A second copy of this script running concurrently does not just double the
# wall time -- both append to the same CSV and both contend for the node, so
# every timing in the file is wrong and the ratios appear twice. That happened
# once; the lock is so it cannot happen again.
LOCK="$WORK/.bench.lock"
mkdir -p "$WORK"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another benchmark_bed.sh is already running (lock: $LOCK)" >&2
  exit 3
fi

SCRATCH="$WORK/run.$$"

# Every executable, and the libraries it loads from network filesystems, is
# staged onto the same local disk before anything is timed. Process start-up is
# otherwise part of every measurement, and here it is not small or stable: the
# same 2.8 MB decompression took 0.62 s, 0.41 s and 0.05 s back to back with the
# binary on /scratch, and a steady 0.10 s from /tmp. On BED-sized files that is
# most of the number, for every tool. conda's zstd and xz find their libraries
# through RPATH $ORIGIN/../lib, which the stage layout matches.
STAGE="$WORK/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE/bin" "$STAGE/lib"
stage() {   # stage <name> <path>
  cp -f "$2" "$STAGE/bin/$1"
  ldd "$2" | awk '$3 ~ /^\// && $3 !~ /^\/(usr\/)?lib64\// {print $3}' | while read -r l; do
    cp -Lf "$l" "$STAGE/lib/"
  done
}
stage nyx_bed "$HERE/tools/nyx_bed"
stage pigz    "$(command -v pigz)"
stage zstd    "$(command -v zstd)"
stage xz      "$(command -v xz)"
stage brotli  "$(command -v brotli)"
stage bgzip   "$BGZIP"
stage 7zz     "$SEVENZ"
export LD_LIBRARY_PATH="$STAGE/lib"
B="$STAGE/bin"
for t in nyx_bed pigz zstd xz brotli bgzip 7zz; do cat "$B/$t" > /dev/null; done
mkdir -p "$WORK" "$SCRATCH" "$(dirname "$OUT")"
trap 'rc=$?; echo "-- benchmark_bed.sh exiting rc=$rc at line $LINENO" >&2; rm -rf "$SCRATCH"' EXIT

MEASURE_SEC=0; MEASURE_RSS=0
# The command's own stderr is kept in $SCRATCH/last.err rather than discarded,
# so a failing row explains itself. Parquet decompression once failed on 23 of
# 34 files in the harness while passing on every one of them by hand, and with
# stderr thrown away there was nothing to go on.
measure() {
  local o; o=$(/usr/bin/time -o "$SCRATCH/last.time" -f '%e %M' "$@" 2>"$SCRATCH/last.err" >/dev/null; tail -1 "$SCRATCH/last.time")
  MEASURE_SEC=${o%% *}; MEASURE_RSS=${o##* }
  case "$MEASURE_SEC" in ''|*[!0-9.]*) MEASURE_SEC=0;; esac
  case "$MEASURE_RSS" in ''|*[!0-9]*) MEASURE_RSS=0;; esac
}
want() { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

if [ -z "$ONLY" ]; then
  echo "tool,file,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,peak_RSS_MB,roundtrip" > "$OUT"
fi
emit() { awk -v t="$1" -v f="$2" -v o="$3" -v c="$4" -v cs="$5" -v ds="$6" -v m="$7" -v r="$8" \
  'BEGIN{printf "%s,\"%s\",%s,%s,%.3f,%s,%s,%s,%s\n",t,f,o,c,(c>0?o/c:0),cs,ds,m,r}' >> "$OUT"; }

bench() {
  local src="$1" b sz; b=$(basename "$src"); sz=$(stat -c%s "$src")
  # Warm the page cache once before any tool runs: NYX goes first on every
  # file, so without this it alone would pay the cold read from /scratch.
  cat "$src" > /dev/null
  echo "== $b ($sz bytes)"
  # xz -9e defaults to a 192 MiB block, so it can only use ceil(size/192MiB)
  # threads: on a 200 MB file that is one core at 100% CPU, measured, while
  # 12 MiB blocks give 1333%. Giving it -T16 and then reporting a single-core
  # wall time would be a rigged comparison, so xz is run twice -- once at its
  # default (its best ratio) and once with the block size cut so it can actually
  # use the threads (its best speed). Both rows are kept; NYX has to beat both.
  local XZBLK
  XZBLK=$(( sz / THREADS / 1048576 )); [ "$XZBLK" -lt 1 ] && XZBLK=1
  XZBLK="${XZBLK}MiB"
  local cs ds rss dr rt

  if want nyx_bed; then
    echo "   -> nyx_bed" >&2
    measure "$B/nyx_bed" compress --threads "$THREADS" --quiet "$src" "$SCRATCH/t.nbed"
    cs=$MEASURE_SEC; rss=$MEASURE_RSS
    measure "$B/nyx_bed" decompress --threads "$THREADS" --quiet "$SCRATCH/t.nbed" "$SCRATCH/t.rt"
    ds=$MEASURE_SEC; dr=$MEASURE_RSS
    if cmp -s "$src" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; echo "  !! ROUND TRIP FAILED"; fi
    emit nyx_bed "$b" "$sz" "$(stat -c%s "$SCRATCH/t.nbed")" "$cs" "$ds" \
         "$(awk -v a="$rss" -v c="$dr" 'BEGIN{printf "%.1f",(a>c?a:c)/1024}')" "$rt"
    rm -f "$SCRATCH/t.rt" "$SCRATCH/t.nbed"
  fi

  # Baselines get the input as a *file path*, not on stdin, and that is not
  # cosmetic: xz will not split a non-seekable stream into blocks, so `xz -T16`
  # reading a pipe compresses on one core. Measured here at 100% CPU where the
  # file form uses sixteen. The other benchmark scripts already pass paths;
  # matching them is what makes the thread count equal across every table.
  local specs=(
    "gzip|$B/pigz -9 -p$THREADS -c SRC|$B/pigz -d -p$THREADS -c"
    "zstd|$B/zstd -19 --long=27 -T$THREADS -q -c SRC|$B/zstd -d --long=27 -T$THREADS -q -c"
    "xz|$B/xz -9e -T$THREADS -c SRC|$B/xz -d -T$THREADS -c"
    "xz_mt|$B/xz -9e -T$THREADS --block-size=$XZBLK -c SRC|$B/xz -d -T$THREADS -c"
    "brotli|$B/brotli -q 11 -c SRC|$B/brotli -d -c"
    # bgzip is how the community actually stores BED -- .bed.gz beside a tabix
    # index -- so leaving it out would mean never comparing against what people
    # really use. It is deflate, so the ratio is gzip's; the point of the row is
    # that this is the incumbent, not that it is strong.
    "bgzip|$B/bgzip -@ $THREADS -l 9 -c SRC|$B/bgzip -d -@ $THREADS -c"
  )
  for spec in "${specs[@]}"; do
    local n="${spec%%|*}" rest="${spec#*|}"
    local cc="${rest%%|*}" dc="${rest#*|}"
    want "$n" || continue
    echo "   -> $n" >&2
    cc="${cc/SRC/\'$src\'}"
    measure bash -c "$cc > '$SCRATCH/t.out' 2>/dev/null"; cs=$MEASURE_SEC; rss=$MEASURE_RSS
    measure bash -c "$dc < '$SCRATCH/t.out' > '$SCRATCH/t.rt' 2>/dev/null"; ds=$MEASURE_SEC; dr=$MEASURE_RSS
    if cmp -s "$src" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; echo "  !! $n ROUND TRIP FAILED"; fi
    emit "$n" "$b" "$sz" "$(stat -c%s "$SCRATCH/t.out")" "$cs" "$ds" \
         "$(awk -v a="$rss" -v c="$dr" 'BEGIN{printf "%.1f",(a>c?a:c)/1024}')" "$rt"
    rm -f "$SCRATCH/t.out" "$SCRATCH/t.rt"
  done

  # Parquet + zstd is the control for "isn't a column split just columnar
  # storage?" -- a mature columnar format given the same split, with dictionary
  # and delta encoding and a zstd back end. Byte-exact like every other row.
  if want parquet; then
    measure python3 "$HERE/src/parquet_baseline.py" compress "$src" "$SCRATCH/t.parquet"
    cs=$MEASURE_SEC; rss=$MEASURE_RSS
    measure python3 "$HERE/src/parquet_baseline.py" decompress "$SCRATCH/t.parquet" "$SCRATCH/t.rt"
    ds=$MEASURE_SEC; dr=$MEASURE_RSS
    if cmp -s "$src" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; echo "  !! parquet ROUND TRIP FAILED"; sed 's/^/     /' "$SCRATCH/last.err" | tail -15; fi
    emit parquet "$b" "$sz" "$(stat -c%s "$SCRATCH/t.parquet")" "$cs" "$ds" \
         "$(awk -v a="$rss" -v c="$dr" 'BEGIN{printf "%.1f",(a>c?a:c)/1024}')" "$rt"
    rm -f "$SCRATCH/t.parquet" "$SCRATCH/t.rt"
  fi

  # 7-Zip will not build a 7z archive on a pipe, so it gets real paths.
  if want 7z && [ -x "$B/7zz" ]; then
    echo "   -> 7z" >&2
    rm -f "$SCRATCH/a.7z"
    measure "$B/7zz" a -t7z -mx=9 -mmt="$THREADS" "$SCRATCH/a.7z" "$src"
    cs=$MEASURE_SEC; rss=$MEASURE_RSS
    # Extract to stdout, like every other tool's decompression. Extracting into
    # a new directory is a metadata operation, and on BeeGFS (/scratch) those stall: one
    # extraction measured 29.5 s in the harness against 0.15 s on /tmp and
    # 0.61 s by hand on the same filesystem.
    measure bash -c "'$B/7zz' e -so '$SCRATCH/a.7z' > '$SCRATCH/t.rt'"
    ds=$MEASURE_SEC; dr=$MEASURE_RSS
    if cmp -s "$src" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; echo "  !! 7z ROUND TRIP FAILED"; sed 's/^/     /' "$SCRATCH/last.err" | tail -5; fi
    emit 7z "$b" "$sz" "$(stat -c%s "$SCRATCH/a.7z")" "$cs" "$ds" \
         "$(awk -v a="$rss" -v c="$dr" 'BEGIN{printf "%.1f",(a>c?a:c)/1024}')" "$rt"
    rm -f "$SCRATCH/a.7z" "$SCRATCH/t.rt"
  fi
}

# The whole corpus, largest first so a failure shows up on the expensive files
# early rather than three hours in. Six hand-named files invited exactly the
# "you only tested a little" objection that a small corpus always invites, and
# BED files are cheap: scripts/bed/download_bed.sh fetches ~34 spanning widths
# 4, 6, 9, 10 and 15 from four independent sources.
mapfile -t CORPUS < <(find "$DATA" -maxdepth 1 -type f \
  \( -name '*.bed' -o -name '*Peak' \) -printf '%s\t%p\n' | sort -rn | cut -f2)
echo "corpus: ${#CORPUS[@]} files"
for f in "${CORPUS[@]}"; do bench "$f"; done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
