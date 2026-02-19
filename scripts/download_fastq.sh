#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

# Default: one Illumina WGS run (R1) from ENA. Override with FASTQ_URL / FASTQ_GZ / FASTQ_OUT.
FASTQ_URL="${FASTQ_URL:-ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR292/SRR292678/SRR292678_1.fastq.gz}"
FASTQ_GZ="${FASTQ_GZ:-$DATA_DIR/$(basename "$FASTQ_URL")}"
FASTQ_OUT="${FASTQ_OUT:-${FASTQ_GZ%.gz}}"

if [ ! -f "$FASTQ_GZ" ]; then
  echo "Downloading: $FASTQ_URL"
  wget -O "$FASTQ_GZ" "$FASTQ_URL"
else
  echo "Already downloaded: $FASTQ_GZ"
fi

if [ ! -f "$FASTQ_OUT" ]; then
  echo "Decompressing to: $FASTQ_OUT"
  gzip -dk "$FASTQ_GZ"
else
  echo "Already decompressed: $FASTQ_OUT"
fi

echo "FASTQ: $FASTQ_OUT"
