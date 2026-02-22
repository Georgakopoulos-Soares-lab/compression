#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

# Default dataset: GRCm39 mouse reference genome FASTA.
FASTA_URL="${FASTA_URL:-https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/635/GCF_000001635.27_GRCm39/GCF_000001635.27_GRCm39_genomic.fna.gz}"
FASTA_GZ="${FASTA_GZ:-$DATA_DIR/$(basename "$FASTA_URL")}"
FASTA_OUT="${FASTA_OUT:-${FASTA_GZ%.gz}}"

if [ ! -f "$FASTA_GZ" ]; then
  echo "Downloading: $FASTA_URL"
  wget -O "$FASTA_GZ" "$FASTA_URL"
else
  echo "Already downloaded: $FASTA_GZ"
fi

if [ ! -f "$FASTA_OUT" ]; then
  echo "Decompressing to: $FASTA_OUT"
  gzip -dk "$FASTA_GZ"
else
  echo "Already decompressed: $FASTA_OUT"
fi

echo "FASTA: $FASTA_OUT"
