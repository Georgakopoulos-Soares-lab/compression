#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data/benchmark}"
mkdir -p "$DATA_DIR"

# ---------- ERR9539086 (Variable Length, NovaSeq) ----------
# FIXED: The ENA subfolder path is 006, not 086
ERR_URL="https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/006/ERR9539086/ERR9539086.fastq.gz"
ERR_GZ="$DATA_DIR/ERR9539086.fastq.gz"
ERR_FULL="$DATA_DIR/ERR9539086.fastq"
ERR_SAMPLE="$DATA_DIR/ERR9539086_500M.fastq"

# ---------- SRR8899104 (Fixed Length 51bp, HiSeq) ----------
SRR_URL="https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR889/004/SRR8899104/SRR8899104.fastq.gz"
SRR_GZ="$DATA_DIR/SRR8899104.fastq.gz"
SRR_FULL="$DATA_DIR/SRR8899104.fastq"
SRR_SAMPLE="$DATA_DIR/SRR8899104_500M.fastq"

# Number of lines to take for training samples (~500 MB)
ERR_LINES="${ERR_LINES:-14000000}"
SRR_LINES="${SRR_LINES:-13000000}"

# Download and Extract ERR9539086
if [ ! -f "$ERR_GZ" ]; then
  echo "Downloading ERR9539086: $ERR_URL"
  wget -O "$ERR_GZ" "$ERR_URL"
else
  echo "Already downloaded: $ERR_GZ"
fi

if [ ! -f "$ERR_FULL" ]; then
  echo "Decompressing ERR9539086… (~7.8 GiB)"
  gunzip -k "$ERR_GZ"
else
  echo "Already decompressed: $ERR_FULL"
fi

if [ ! -f "$ERR_SAMPLE" ]; then
  echo "Creating ERR sample: first $ERR_LINES lines…"
  head -"$ERR_LINES" "$ERR_FULL" > "$ERR_SAMPLE"
else
  echo "Sample already exists: $ERR_SAMPLE"
fi

# Download and Extract SRR8899104
if [ ! -f "$SRR_GZ" ]; then
  echo "Downloading SRR8899104: $SRR_URL"
  wget -O "$SRR_GZ" "$SRR_URL"
else
  echo "Already downloaded: $SRR_GZ"
fi

if [ ! -f "$SRR_FULL" ]; then
  echo "Decompressing SRR8899104… (~24 GiB)"
  gunzip -k "$SRR_GZ"
else
  echo "Already decompressed: $SRR_FULL"
fi

if [ ! -f "$SRR_SAMPLE" ]; then
  echo "Creating SRR sample: first $SRR_LINES lines…"
  head -"$SRR_LINES" "$SRR_FULL" > "$SRR_SAMPLE"
else
  echo "Sample already exists: $SRR_SAMPLE"
fi

echo ""
echo "FASTQ Download Complete."
echo "ERR9539086 Sample: $ERR_SAMPLE ($(stat -c%s "$ERR_SAMPLE") bytes)"
echo "SRR8899104 Sample: $SRR_SAMPLE ($(stat -c%s "$SRR_SAMPLE") bytes)"
echo ""
echo "Next: preprocess with fastq_preprocess using Base-5 ASCII packing, e.g.:"
echo "  ./tools/fastq_preprocess encode $ERR_SAMPLE $DATA_DIR/ERR_pp 16 --pack-4bit"
