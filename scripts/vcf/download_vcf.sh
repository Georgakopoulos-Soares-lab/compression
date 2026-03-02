#!/usr/bin/env bash
# Download a public VCF for the compression pipeline.
#
# Select a dataset via DATASET= (default: clinvar):
#
#   clinvar          NCBI ClinVar GRCh38 (current release)
#                    ~6 MB gz  →  ~30 MB uncompressed  |  ~1 M variants
#                    Good for quick smoke-tests.
#
#   1000g_chr22      1000 Genomes phase 3 chr22 with genotypes
#                    ~500 MB gz → ~3–4 GB uncompressed  |  ~1.1 M variants × 2504 samples
#                    Medium-sized realistic benchmark with lots of genotype INFO.
#
#   1000g_wgs_sites  1000 Genomes phase 3 whole-genome sites-only (no sample columns)
#                    ~1.1 GB gz → ~14 GB uncompressed   |  ~84 M variants
#                    Large-scale realistic benchmark; best for compression ratio study.
#
# Examples:
#   DATASET=clinvar          bash scripts/vcf/download_vcf.sh
#   DATASET=1000g_chr22      bash scripts/vcf/download_vcf.sh
#   DATASET=1000g_wgs_sites  bash scripts/vcf/download_vcf.sh
#
# You can still override the URL entirely:
#   VCF_URL=<url> bash scripts/vcf/download_vcf.sh

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

# ---- Dataset presets ----
DATASET="${DATASET:-clinvar}"

case "$DATASET" in
  clinvar)
    _DEFAULT_URL="https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz"
    ;;
  1000g_chr22)
    _DEFAULT_URL="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz"
    ;;
  1000g_wgs_sites)
    _DEFAULT_URL="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.wgs.phase3_shapeit2_mvncall_integrated_v5c.20130502.sites.vcf.gz"
    ;;
  *)
    echo "Error: unknown DATASET '$DATASET'." >&2
    echo "  Valid values: clinvar  1000g_chr22  1000g_wgs_sites" >&2
    exit 1
    ;;
esac

VCF_URL="${VCF_URL:-$_DEFAULT_URL}"
VCF_GZ="${VCF_GZ:-$DATA_DIR/$(basename "$VCF_URL")}"
VCF_OUT="${VCF_OUT:-${VCF_GZ%.gz}}"

echo "Dataset  : $DATASET"
echo "URL      : $VCF_URL"
echo "Local gz : $VCF_GZ"
echo "Local vcf: $VCF_OUT"
echo ""

# ---- Download ----
if [ ! -f "$VCF_GZ" ]; then
  echo "Downloading..."
  if command -v wget >/dev/null 2>&1; then
    wget --progress=dot:giga -O "$VCF_GZ" "$VCF_URL"
  else
    curl -L --progress-bar -o "$VCF_GZ" "$VCF_URL"
  fi
else
  echo "Already downloaded: $VCF_GZ"
fi

# ---- Decompress ----
if [ ! -f "$VCF_OUT" ]; then
  echo "Decompressing (this may take several minutes for large files)..."
  gzip -dk "$VCF_GZ"
else
  echo "Already decompressed: $VCF_OUT"
fi

echo "VCF ready: $VCF_OUT"
