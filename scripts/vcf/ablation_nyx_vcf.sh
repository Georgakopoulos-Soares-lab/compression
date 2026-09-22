#!/usr/bin/env bash
# ablation_nyx_vcf.sh — what each transform in the VCF pipeline is worth.
#
# Stages are cumulative. Every stage is verified byte-exact; a stage that fails
# its round trip is recorded as such rather than dropped.
#
#   scripts/vcf/ablation_nyx_vcf.sh [--threads N] [--out results/vcf_ablation.csv]
# --no-calibrate: the manuscript's VCF tables were measured at OpenZL's default
# entropy level, before level calibration existed. Pinning it keeps those tables
# regenerable from the package; drop the flag to measure the calibrated default.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYX="$HERE/tools/nyx_vcf"
MODELS="$HERE/artifacts/nyx_vcf_models"
WORK="${NYX_VCF_WORK:-$HERE/data/vcf/work}"
THREADS="${SLURM_CPUS_ON_NODE:-16}"
OUT="$HERE/results/vcf_ablation.csv"
while [ $# -gt 0 ]; do
  case "$1" in
    --threads) THREADS="$2"; shift 2;;
    --out)     OUT="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done
SCRATCH="$WORK/abl.$$"
mkdir -p "$SCRATCH" "$(dirname "$OUT")"
trap 'rm -rf "$SCRATCH"' EXIT
echo "file,archetype,stage,orig_bytes,comp_bytes,ratio,roundtrip" > "$OUT"

# stage label <TAB> flags   (cumulative: each line adds one transform)
STAGES=$(cat <<'S'
OpenZL generic (no format awareness)	--raw-blocks
+ column split	--no-pbwt --no-values --no-dict --no-annot --no-info-split
+ per-INFO-key streams	--no-pbwt --no-values --no-dict --no-annot
+ value transforms	--no-pbwt --no-dict --no-annot
+ text dictionaries	--no-pbwt --no-annot
+ annotation split	--no-pbwt
+ haplotype PBWT	
+ trained models	--models MODELS
S
)

run() { # plain archetype
  local plain="$1" arch="$2" b sz
  b=$(basename "$plain"); sz=$(stat -c%s "$plain")
  echo "== $b"
  while IFS=$'\t' read -r label flags; do
    [ -n "$label" ] || continue
    flags=${flags//MODELS/$MODELS}
    if [ "${flags#*--models}" != "$flags" ] && [ ! -d "$MODELS" ]; then
      echo "   skip '$label' (no trained models at $MODELS)"; continue
    fi
    "$NYX" compress --no-calibrate --threads "$THREADS" --quiet $flags "$plain" "$SCRATCH/ab.nvcf" 2>/dev/null || {
      echo "\"$b\",$arch,\"$label\",$sz,,,ERROR" >> "$OUT"; continue; }
    "$NYX" decompress --threads "$THREADS" --quiet "$SCRATCH/ab.nvcf" "$SCRATCH/ab.rt" 2>/dev/null
    local rt=MISMATCH; cmp -s "$plain" "$SCRATCH/ab.rt" && rt=OK
    local cb; cb=$(stat -c%s "$SCRATCH/ab.nvcf")
    awk -v b="$b" -v a="$arch" -v l="$label" -v s="$sz" -v c="$cb" -v r="$rt" \
      'BEGIN{printf "\"%s\",%s,\"%s\",%s,%s,%.3f,%s\n",b,a,l,s,c,s/c,r}' >> "$OUT"
    printf "   %-38s %8.2fx  %s\n" "$label" "$(echo "scale=4;$sz/$cb"|bc)" "$rt"
    rm -f "$SCRATCH/ab.rt" "$SCRATCH/ab.nvcf"
  done <<< "$STAGES"
}

# One representative file per archetype, already decompressed by the benchmark.
[ -s "$WORK/1000G.chr22.200k.vcf" ] || cp "$HERE/data/vcf/bench/1000G.chr22.200k.vcf" "$WORK/" 2>/dev/null
for spec in \
  "1000G.chr22.200k.vcf:panel" \
  "HG002_GRCh38_v4.2.1.vcf:single-sample" \
  "clinvar_GRCh38.vcf:sites-annotated" \
  "gvcf_nfcore_test2.g.vcf:gvcf" \
  "seqc2_hcc1395_ssnv.vcf:somatic" \
  "civic_nightly.vcf:sites-annotated"; do
  f="$WORK/${spec%%:*}"
  [ -s "$f" ] && run "$f" "${spec##*:}"
done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
