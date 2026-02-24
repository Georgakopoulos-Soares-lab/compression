#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data/vcf}"
mkdir -p "$DATA_DIR"

# ---------- ClinVar ----------
CLINVAR_URL="https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"
CLINVAR_GZ="$DATA_DIR/clinvar.vcf.gz"

if [ ! -f "$CLINVAR_GZ" ]; then
  echo "Downloading ClinVar: $CLINVAR_URL"
  wget -O "$CLINVAR_GZ" "$CLINVAR_URL"
else
  echo "Already downloaded: $CLINVAR_GZ"
fi

# ---------- 1000 Genomes chr22 ----------
KG_URL="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz"
KG_GZ="$DATA_DIR/ALL.chr22.vcf.gz"

if [ ! -f "$KG_GZ" ]; then
  echo "Downloading 1000G chr22: $KG_URL"
  wget -O "$KG_GZ" "$KG_URL"
else
  echo "Already downloaded: $KG_GZ"
fi

echo ""
echo "Downloaded VCF files:"
echo "  ClinVar:  $CLINVAR_GZ"
echo "  1000G:    $KG_GZ"
echo ""
echo "Next: create benchmark samples with make_vcf_sample.py, e.g.:"
echo "  python3 $HERE/scripts/vcf/make_vcf_sample.py \\"
echo "      --input $CLINVAR_GZ --out-prefix $DATA_DIR/clinvar_bench_500MiB --target-mib 500"
