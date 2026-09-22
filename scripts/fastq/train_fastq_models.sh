#!/usr/bin/env bash
# train_fastq_models.sh — train every Illumina FASTQ model NYX ships, from runs
# that are NOT in the benchmark.
#
#   scripts/fastq/train_fastq_models.sh [out_dir]      (default artifacts/fastq_models)
#
# Why this replaced retrain_paper_models.sh: that script trained on the first
# reads of all six benchmark runs, so every FASTQ result in the paper was
# measured with models that had seen the file being compressed. It also only
# produced one of the three Illumina models the codec's calibration chooses
# between (fastq_fixed.zc and fastq_illumina_zstdseq.zc had no recorded recipe),
# and it asked for clustered training samples with NYX_CLUSTER=1, which `pack`
# ignores -- so the HARC model never saw a clustered container.
#
# Training corpus: data/fastq/heldout/, ten runs fetched by
# scripts/fastq/download_heldout_fastq.sh, covering each kind of data the
# benchmark contains, each from a study with no run in the benchmark:
#   A  NovaSeq human ancient-DNA WGS, short trimmed  benchmark: ERR9539079/86/93
#      (four runs: two at 65-77 bp and two at 40-45 bp, since the benchmark
#       runs average 43 bp and length decides what the header/quality streams look like)
#   B  HiSeq 2000 mouse RNA-seq, 51 bp (fixed)       benchmark: SRR8899104
#   C  NovaSeq human WGS, ~150 bp                    benchmark: ERR4186945
#   D  NovaSeq mouse RNA-seq, ~50 bp (fixed)         benchmark: DRR206632
#
# One model per candidate the codec's calibrateModel() prices, each trained on
# the stream shape it is used with. The SEQ route is read live during the
# trainer's internal trial compressions (see resolveSeqRoute), so it is set here
# to the route the model runs under at compress time.
#   fastq_var.zc               variable-length reads, SEQ -> big-window LZ
#   fastq_fixed.zc             fixed-length reads,    SEQ -> big-window LZ
#   fastq_illumina_zstdseq.zc  all reads,             SEQ -> zstd
#   fastq_fixed_cluster.zc     all reads, packed with per-chunk read clustering
#                              (the HARC-style reorder path, NYX_CLUSTER)
# The Nanopore models are not retrained: no Nanopore data is benchmarked.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYX="$ROOT/openzl/nyxfqz_v2"
SRC="${HELDOUT_DIR:-$ROOT/data/fastq/heldout}"
OUT="${1:-$ROOT/artifacts/fastq_models}"
READS="${NYX_TRAIN_READS:-7000}"          # per run, as the previous models used
TMP="$(mktemp -d "${TMPDIR:-/tmp}/nyx_fqtrain.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
[ -x "$NYX" ] || { echo "build nyxfqz_v2 first (scripts/fastq/build_nyxfqz.sh)"; exit 1; }
mkdir -p "$OUT"

# Refuse to train on a benchmark run, whatever the directory holds.
BENCH="ERR9539079 ERR9539086 ERR9539093 SRR8899104 ERR4186945 DRR206632"
for f in "$SRC"/*.fastq.gz; do
  for b in $BENCH; do
    case "$(basename "$f")" in *"$b"*) echo "FATAL: $f is a benchmark run"; exit 1;; esac
  done
done

sample() { # file -> plain FASTQ of the first $READS reads
  gzip -dc "$1" 2>/dev/null | head -n "$(( READS * 4 ))" > "$2" || true
}

# model : sample-kinds : pack env : train env
train_one() {
  local model="$1" kinds="$2" packenv="$3" trainenv="$4" d="$TMP/pk_$1" i=0
  mkdir -p "$d"
  for k in $kinds; do
    for f in "$SRC/${k}"_*.fastq.gz; do
      [ -s "$f" ] || continue
      sample "$f" "$TMP/s.fq"
      env $packenv "$NYX" pack "$TMP/s.fq" "$d/s$i.fqzc" >/dev/null 2>&1
      i=$((i+1))
    done
  done
  [ "$i" -gt 0 ] || { echo "FATAL: no samples for $model"; exit 1; }
  env $trainenv "$NYX" train "$d" "$OUT/$model" >/dev/null 2>"$TMP/train_$model.log" \
    || { echo "FATAL: training $model"; tail -3 "$TMP/train_$model.log"; exit 1; }
  printf "  %-28s %2d runs (%s)  %6d bytes\n" "$model" "$i" "$kinds" "$(stat -c%s "$OUT/$model")"
}

echo "== training from $SRC ($READS reads per run) =="
rm -f "$OUT/fastq_var.zc" "$OUT/fastq_fixed.zc" "$OUT/fastq_illumina_zstdseq.zc" \
      "$OUT/fastq_fixed_cluster.zc" "$OUT/fastq_illumina.zc"
train_one fastq_var.zc              "A C"     "X=0"                    "NYX_SEQ_ROUTE=biglz"
train_one fastq_fixed.zc            "B D"     "X=0"                    "NYX_SEQ_ROUTE=biglz"
train_one fastq_illumina_zstdseq.zc "A B C D" "X=0"                    "NYX_SEQ_ROUTE=zstd"
train_one fastq_fixed_cluster.zc    "A B C D" "NYX_CLUSTER_PERCHUNK=1" "X=0"
# fastq_illumina.zc is the name older scripts and docs use for the variable model.
ln -sf fastq_var.zc "$OUT/fastq_illumina.zc"

echo "== byte-exact round trip on held-out reads not used for training =="
for f in "$SRC"/*.fastq.gz; do
  gzip -dc "$f" 2>/dev/null | tail -n +"$(( READS * 4 + 1 ))" | head -n 80000 > "$TMP/chk.fq" || true
  "$NYX" compress "$OUT" "$TMP/chk.fq" "$TMP/chk.nyxz" 4 400 >/dev/null 2>&1
  "$NYX" decompress "$TMP/chk.nyxz" "$TMP/chk.back" >/dev/null 2>&1
  cmp -s "$TMP/chk.fq" "$TMP/chk.back" || { echo "  FAIL $(basename "$f")"; exit 1; }
done
echo "  all runs byte-exact"
