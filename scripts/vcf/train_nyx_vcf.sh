#!/usr/bin/env bash
# train_nyx_vcf.sh — rebuild the shipped per-stream-class OpenZL models.
#
# Maintainer-side only. Compression never trains on user data: `nyx_vcf
# compress --models artifacts/nyx_vcf_models` loads whatever this produced.
#
# Streams are grouped into a dozen classes (see streamClass() in
# tools/nyx_vcf.cpp) and one compressor is trained per class over a corpus that
# spans every archetype, so a class model is not tuned to any one file shape.
#
#   scripts/vcf/train_nyx_vcf.sh [--out DIR] [--threads N]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${NYX_VCF_DATA:-$HERE/data/vcf}"
OUT="$HERE/artifacts/nyx_vcf_models"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
while [ $# -gt 0 ]; do
  case "$1" in
    --out)     OUT="$2"; shift 2;;
    --threads) THREADS="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
ZLI="$HERE/openzl/zli"
NYX="$HERE/tools/nyx_vcf"
[ -x "$ZLI" ] || { echo "build OpenZL first: scripts/build_all.sh" >&2; exit 1; }
[ -x "$NYX" ] || { echo "build tools first: scripts/build_all.sh" >&2; exit 1; }

WORK="${NYX_VCF_TRAIN_WORK:-$HERE/data/vcf/trainwork}"
STREAMS="$WORK/streams"
mkdir -p "$WORK" "$OUT"
[ "${NYX_VCF_REDUMP:-0}" = 1 ] && rm -rf "$STREAMS"

# Training corpus. Deliberately disjoint from the files the paper reports:
# chr21 (not chr22), HG003 (not HG002), gnomAD exomes (not genomes),
# gVCF test1 (not test2), the sINDEL set (not the sSNV set).
TRAIN_SRC=(
  "$DATA/1000G.chr21.phase3.vcf.gz"
  "$DATA/HG003_GRCh38_v4.2.1.vcf.gz"
  "$DATA/gnomad_v4.1_exomes_chrY.vcf.bgz"
  "$DATA/gvcf_nfcore_test1.g.vcf.gz"
  "$DATA/seqc2_hcc1395_sindel.vcf.gz"
  "$DATA/civic_nightly.vcf"
)
# Cap each source so one big file cannot dominate the trained models.
MAX_VARIANTS="${NYX_VCF_TRAIN_VARIANTS:-120000}"
# Samples per class handed to the trainer. Classes like `text` and `num4`
# collect thousands of small per-key streams; training on all of them costs
# hours and buys nothing over a representative subset.
MAX_SAMPLES="${NYX_VCF_TRAIN_SAMPLES:-64}"
# Total bytes per class handed to the trainer. The ACE search cost grows with
# the corpus, and classes differ hugely in size (a few hundred KB to >100 MB);
# without a budget the big ones dominate the wall clock for no ratio gain.
MAX_BYTES="${NYX_VCF_TRAIN_BYTES:-24000000}"

if [ -d "$STREAMS" ] && [ "${NYX_VCF_REDUMP:-0}" != 1 ]; then
  echo "reusing streams already dumped under $STREAMS (NYX_VCF_REDUMP=1 to rebuild)"
fi
for src in "${TRAIN_SRC[@]}"; do
  [ -d "$STREAMS" ] && [ "${NYX_VCF_REDUMP:-0}" != 1 ] && break
  [ -s "$src" ] || { echo "!! missing training source: $src (skipped)"; continue; }
  b=$(basename "$src"); b=${b%.gz}; b=${b%.bgz}
  slice="$WORK/train_$b"
  if [ ! -s "$slice" ]; then
    echo ">> slicing $b (<= $MAX_VARIANTS variants)"
    case "$src" in
      *.gz|*.bgz) zcat "$src";;
      *) cat "$src";;
    esac | awk -v m="$MAX_VARIANTS" '/^#/{print;next}{print;n++;if(n>=m)exit}' > "$slice"
  fi
  echo ">> dumping streams from $b"
  "$NYX" stats --block-mb 64 --dump-streams "$STREAMS" "$slice" >/dev/null 2>&1 \
    || echo "!! stream dump failed for $b"
done

[ -d "$STREAMS" ] || { echo "no streams dumped — nothing to train" >&2; exit 1; }

echo
echo "== training one compressor per stream class =="
ready=0; failed=0
for cdir in "$STREAMS"/*; do
  [ -d "$cdir" ] || continue
  cls=$(basename "$cdir")
  case "$cls" in dir|raw) continue;; esac   # tiny / verbatim: not worth a model
  if [ -s "$OUT/$cls.zlc" ] && [ "${NYX_VCF_RETRAIN:-0}" != 1 ]; then
    echo "= $cls: already trained ($(stat -c%s "$OUT/$cls.zlc") B) — NYX_VCF_RETRAIN=1 to rebuild"
    ready=$((ready+1)); continue
  fi
  n=$(find "$cdir" -name '*.bin' | wc -l)
  bytes=$(find "$cdir" -name '*.bin' -printf '%s\n' | awk '{s+=$1} END{print s+0}')
  [ "$n" -gt 0 ] || continue
  # Prefer the largest samples: they carry the most signal per unit of training
  # time and match the block sizes seen at compression time.
  sel="$WORK/sel_$cls"; rm -rf "$sel"; mkdir -p "$sel"
  find "$cdir" -name '*.bin' -printf '%s\t%p\n' | sort -rn | head -n "$MAX_SAMPLES" \
    | awk -v cap="$MAX_BYTES" -F'\t' '{ if (t + $1 > cap && n > 0) exit; t += $1; n++; print $2 }' \
    | while read -r f; do cp "$f" "$sel/"; done
  nsel=$(ls "$sel" | wc -l)
  printf '%-12s %4s/%s samples %10s bytes ... ' "$cls" "$nsel" "$n" "$bytes"
  if "$ZLI" train "$sel" --profile serial --output "$OUT/$cls.zlc" --force \
        --threads "$THREADS" --use-all-samples > "$WORK/train_$cls.log" 2>&1 \
     && [ -s "$OUT/$cls.zlc" ]; then
    echo "ok ($(stat -c%s "$OUT/$cls.zlc") B)"; ready=$((ready+1))
  else
    echo "FAILED (see $WORK/train_$cls.log)"; rm -f "$OUT/$cls.zlc"; failed=$((failed+1))
  fi
done
echo
echo "models: $ready trained, $failed failed -> $OUT"
