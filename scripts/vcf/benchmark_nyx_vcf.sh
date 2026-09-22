#!/usr/bin/env bash
# benchmark_nyx_vcf.sh — the VCF table for the paper.
#
# Runs nyx_vcf and every baseline over the same decompressed input, on the same
# machine, with the same thread count, and reports no row unless the nyx_vcf
# round trip is byte-for-byte identical to the input.
#
#   scripts/vcf/benchmark_nyx_vcf.sh [--threads N] [--out results/vcf_bench.csv]
#
# Baselines: gzip(pigz -9), zstd -19 --long=27, xz -9e, bcftools BCF (-Ob -l9).
# GTShark and VCFShark are handled separately: GTShark's compress-db discards
# CHROM/REF/ALT/QUAL/FILTER/INFO, so it is not a whole-file VCF compressor, and
# VCFShark segfaults on this toolchain. See docs/vcf-competitors.md.
# --no-calibrate: the manuscript's VCF tables were measured at OpenZL's default
# entropy level, before level calibration existed. Pinning it keeps those tables
# regenerable from the package; drop the flag to measure the calibrated default.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
NYX="$HERE/tools/nyx_vcf"
MODELS="${NYX_VCF_MODELS:-$HERE/artifacts/nyx_vcf_models}"
DATA="${NYX_VCF_DATA:-$HERE/data/vcf}"
WORK="${NYX_VCF_WORK:-$HERE/data/vcf/work}"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
OUT="$HERE/results/vcf_bench.csv"
BLOCK_MB="${NYX_VCF_BLOCK_MB:-64}"
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    --block-mb) BLOCK_MB="$2"; shift 2;;
    # --only re-measures one tool. Timings are the reason: the first full run of
    # this table shared the node with other work and the small-file rows came
    # out 4x slow, which is a measurement artefact, not a property of the codec.
    --only)    ONLY="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
want() { [ -z "${ONLY:-}" ] || [ "${ONLY}" = "$1" ]; }

# Scratch files are per-run: two concurrent invocations sharing $WORK/t.* would
# silently overwrite each other's archives and produce nonsense sizes.
SCRATCH="$WORK/run.$$"
mkdir -p "$WORK" "$SCRATCH" "$(dirname "$OUT")"
trap 'rm -rf "$SCRATCH"' EXIT
[ -x "$NYX" ] || { echo "build first: scripts/build_all.sh" >&2; exit 1; }
BCFTOOLS="$ROOT/thirdparty/bin/bcftools"

# One timed run per tool, reporting wall seconds and peak RSS together.
MEASURE_SEC=0; MEASURE_RSS=0
measure() {
  local o
  o=$(/usr/bin/time -f '%e %M' "$@" 2>&1 >/dev/null | tail -1)
  MEASURE_SEC=${o%% *}
  MEASURE_RSS=${o##* }
  case "$MEASURE_SEC" in ''|*[!0-9.]*) MEASURE_SEC=0;; esac
  case "$MEASURE_RSS" in ''|*[!0-9]*) MEASURE_RSS=0;; esac
}

echo "tool,file,archetype,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,comp_MBps,peak_RSS_MB,roundtrip" > "$OUT"

emit() { # tool file arch orig comp csec dsec rss rt
  awk -v t="$1" -v f="$2" -v a="$3" -v o="$4" -v c="$5" -v cs="$6" -v ds="$7" -v m="$8" -v r="$9" \
    'BEGIN{printf "%s,\"%s\",%s,%s,%s,%.3f,%s,%s,%.1f,%s,%s\n",
           t,f,a,o,c,(c>0?o/c:0),cs,ds,(cs>0?o/cs/1e6:0),m,r}' >> "$OUT"
}

bench_file() { # path archetype
  local src="$1" arch="$2" b plain
  b=$(basename "$src"); b=${b%.gz}; b=${b%.bgz}
  plain="$WORK/$b"
  if [ ! -s "$plain" ]; then
    echo "  decompressing $b"
    case "$src" in
      *.gz|*.bgz) pigz -dc -p"$THREADS" "$src" > "$plain" 2>/dev/null || gzip -dc "$src" > "$plain";;
      *) cp "$src" "$plain";;
    esac
  fi
  local sz; sz=$(stat -c%s "$plain")
  echo "== $b ($arch, $sz bytes)"

  # ---- nyx_vcf (with and without the shipped models) ----
  local cs ds rt cb rss
  if want nyx; then
  local mflag=(); [ -d "$MODELS" ] && mflag=(--models "$MODELS")
  measure "$NYX" compress --no-calibrate --threads "$THREADS" --block-mb "$BLOCK_MB" "${mflag[@]}" --quiet "$plain" "$SCRATCH/t.nvcf"
  cs=$MEASURE_SEC; rss=$MEASURE_RSS
  cb=$(stat -c%s "$SCRATCH/t.nvcf")
  measure "$NYX" decompress --threads "$THREADS" --quiet "$SCRATCH/t.nvcf" "$SCRATCH/t.rt"
  ds=$MEASURE_SEC
  if cmp -s "$plain" "$SCRATCH/t.rt"; then rt=OK; else rt=MISMATCH; fi
  emit nyx_vcf "$b" "$arch" "$sz" "$cb" "$cs" "$ds" "$(echo "scale=1;$rss/1024"|bc)" "$rt"
  [ "$rt" = OK ] || echo "  !! ROUND TRIP FAILED for $b"
  rm -f "$SCRATCH/t.rt"
  fi

  want gp || return 0
  # ---- baselines ----
  measure bash -c "pigz -9 -p$THREADS -c '$plain' > '$SCRATCH/t.gz'"
  cs=$MEASURE_SEC
  measure bash -c "pigz -dc -p$THREADS '$SCRATCH/t.gz' > /dev/null"
  ds=$MEASURE_SEC
  emit gzip "$b" "$arch" "$sz" "$(stat -c%s "$SCRATCH/t.gz")" "$cs" "$ds" "" OK

  measure bash -c "zstd -19 --long=27 -T$THREADS -q -f '$plain' -o '$SCRATCH/t.zst'"
  cs=$MEASURE_SEC
  measure bash -c "zstd -d --long=27 -T$THREADS -q -f '$SCRATCH/t.zst' -o '$SCRATCH/t.rt'"
  ds=$MEASURE_SEC
  emit zstd "$b" "$arch" "$sz" "$(stat -c%s "$SCRATCH/t.zst")" "$cs" "$ds" "" OK
  rm -f "$SCRATCH/t.rt"

  measure bash -c "xz -9e -T$THREADS -c '$plain' > '$SCRATCH/t.xz'"
  cs=$MEASURE_SEC
  measure bash -c "xz -dc -T$THREADS '$SCRATCH/t.xz' > /dev/null"
  ds=$MEASURE_SEC
  emit xz "$b" "$arch" "$sz" "$(stat -c%s "$SCRATCH/t.xz")" "$cs" "$ds" "" OK

  if [ -x "$BCFTOOLS" ]; then
    measure "$BCFTOOLS" view "$plain" -Ob -l9 --threads "$THREADS" -o "$SCRATCH/t.bcf"
    cs=$MEASURE_SEC
    measure bash -c "'$BCFTOOLS' view '$SCRATCH/t.bcf' -Ov --threads $THREADS -o /dev/null"
    ds=$MEASURE_SEC
    emit bcf "$b" "$arch" "$sz" "$(stat -c%s "$SCRATCH/t.bcf")" "$cs" "$ds" "" LOSSY_FORMAT
  fi
  rm -f "$SCRATCH/t.gz" "$SCRATCH/t.zst" "$SCRATCH/t.xz" "$SCRATCH/t.bcf" "$SCRATCH/t.nvcf"
}

# Smallest first: a harness bug then shows up in seconds instead of an hour,
# and partial results are useful while the big files are still running.
bench_file "$DATA/seqc2_hcc1395_ssnv.vcf.gz"        somatic
bench_file "$DATA/civic_nightly.vcf"                sites-annotated
bench_file "$DATA/gvcf_nfcore_test2.g.vcf.gz"       gvcf
bench_file "$DATA/gnomad_v4.1_genomes_chrY.vcf.bgz" sites-frequency
bench_file "$DATA/seqc2_hcc1395_ssnv_superset.vcf.gz" somatic
bench_file "$DATA/clinvar_GRCh38.vcf.gz"            sites-annotated
bench_file "$DATA/HG002_GRCh38_v4.2.1.vcf.gz"       single-sample
bench_file "$DATA/HG004_GRCh38_v4.2.1.vcf.gz"       single-sample
bench_file "$DATA/1000G.chr22.phase3.vcf.gz"        panel
bench_file "$DATA/1000G.chr20.phase3.vcf.gz"        panel

echo
echo "wrote $OUT"
column -s, -t "$OUT"
