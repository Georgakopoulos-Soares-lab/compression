#!/usr/bin/env bash
# get_archetype_data.sh — obtain / derive a representative VCF for the archetypes
# that don't ship one, and point artifacts/vcf_models/archetypes.tsv at it.
#
#   scripts/vcf/get_archetype_data.sh [--force]
#
#   sites-annotated : download ClinVar GRCh38                              (real)
#   somatic         : download SEQC2 HCC1395 high-confidence sSNV set      (real)
#   cohort          : bcftools subset 100 samples from the local 1000G panel  (derived)
#   family-trio     : bcftools subset 3 samples from the local 1000G panel    (derived)
#   gvcf            : real GATK HaplotypeCaller gVCF (nf-core test-datasets)          (real)
#
# sites-frequency / single-sample / panel already have real local sources.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BCF="$HERE/bcftools-1.19/bcftools"
REG="$HERE/artifacts/vcf_models/archetypes.tsv"
DATA="$HERE/data/vcf"
FORCE=0
[ "${1:-}" = --force ] && FORCE=1
mkdir -p "$DATA"

set_source() {  # id  path
  awk -F'\t' -v OFS='\t' -v id="$1" -v p="$2" '
    /^#/ { print; next } $1==id { $3=p } { print }' "$REG" > "$REG.tmp" && mv "$REG.tmp" "$REG"
  echo "  registry: $1 -> $2"
}
dl() {  # url  dest
  [ "$FORCE" = 1 ] || [ ! -s "$2" ] || { echo "  have $(basename "$2")"; return; }
  echo "  downloading $(basename "$2")"
  curl -fL --retry 3 --retry-delay 5 -o "$2" "$1"
}

PANEL="$DATA/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf"
HG002="$HERE/data/test/HG002_GRCh38_1_22_v4.2.1_benchmark.vcf"

echo "== sites-annotated : ClinVar =="
dl "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz" "$DATA/clinvar.vcf.gz"
[ -s "$DATA/clinvar.vcf.gz" ] && set_source sites-annotated "data/vcf/clinvar.vcf.gz"

echo "== somatic : SEQC2 HCC1395 high-confidence sSNV =="
dl "https://ftp.ncbi.nlm.nih.gov/ReferenceSamples/seqc/Somatic_Mutation_WG/release/latest/high-confidence_sSNV_in_HC_regions_v1.2.1.vcf.gz" \
   "$DATA/seqc2_hcc1395_ssnv.vcf.gz"
[ -s "$DATA/seqc2_hcc1395_ssnv.vcf.gz" ] && set_source somatic "data/vcf/seqc2_hcc1395_ssnv.vcf.gz"

echo "== cohort / family-trio : subset the local 1000G panel =="
if [ -s "$PANEL" ] && [ -x "$BCF" ]; then
  mapfile -t S < <("$BCF" query -l "$PANEL" 2>/dev/null)
  if [ "${#S[@]}" -ge 100 ]; then
    COH="$DATA/derived_cohort100.vcf"; TRIO="$DATA/derived_trio3.vcf"
    if [ "$FORCE" = 1 ] || [ ! -s "$COH" ]; then
      s100=$(printf '%s,' "${S[@]:0:100}"); s100=${s100%,}
      echo "  deriving cohort (100 samples)"
      "$BCF" view -s "$s100" "$PANEL" -Ov -o "$COH" 2>/dev/null
    fi
    [ -s "$COH" ] && set_source cohort "data/vcf/derived_cohort100.vcf"
    if [ "$FORCE" = 1 ] || [ ! -s "$TRIO" ]; then
      echo "  deriving family-trio (${S[0]},${S[1]},${S[2]})"
      "$BCF" view -s "${S[0]},${S[1]},${S[2]}" "$PANEL" -Ov -o "$TRIO" 2>/dev/null
    fi
    [ -s "$TRIO" ] && set_source family-trio "data/vcf/derived_trio3.vcf"
  else
    echo "  !! panel has <100 samples — skipping"
  fi
else
  echo "  !! panel VCF or bcftools missing — skipping cohort / family-trio"
fi

echo "== gvcf : real GATK HaplotypeCaller -ERC GVCF output =="
# No synthetic data. Real gVCFs of meaningful size aren't downloadable over plain
# HTTP (public ones are in gs:// requester-pays / auth-gated buckets); the
# nf-core test-datasets copy is a genuine `gatk HaplotypeCaller -ERC GVCF`
# output (real <NON_REF>, END= blocks, MIN_DP/PL), just cut to a test region.
GVCF="$DATA/gvcf_gatk_hc_real.g.vcf.gz"
dl "https://raw.githubusercontent.com/nf-core/test-datasets/modules/data/genomics/homo_sapiens/illumina/gatk/haplotypecaller_calls/test2.g.vcf.gz" "$GVCF"
[ -s "$GVCF" ] && set_source gvcf "data/vcf/gvcf_gatk_hc_real.g.vcf.gz"

echo
grep -v '^#' "$REG" | awk -F'\t' '{printf "  %-16s %-9s %s\n",$1,$4,$3}'
echo "next:  scripts/vcf/train_vcf_library.sh   then   scripts/vcf/vcfzl archetypes"
