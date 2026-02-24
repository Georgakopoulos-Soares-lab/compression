#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data/fastq}"
mkdir -p "$DATA_DIR"

URL="https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/086/ERR9539086/ERR9539086.fastq.gz"
GZ="$DATA_DIR/ERR9539086.fastq.gz"
FULL="$DATA_DIR/ERR9539086.fastq"
SAMPLE="$DATA_DIR/ERR9539086_500M.fastq"

# Number of lines to take (14M lines = 3.5M reads × 4 lines/read ≈ 500 MB)
SAMPLE_LINES="${SAMPLE_LINES:-14000000}"

if [ ! -f "$GZ" ]; then
  echo "Downloading: $URL"
  wget -O "$GZ" "$URL"
else
  echo "Already downloaded: $GZ"
fi

if [ ! -f "$FULL" ]; then
  echo "Decompressing… (this may take a while, ~7.8 GiB)"
  gunzip -k "$GZ"
else
  echo "Already decompressed: $FULL"
fi

if [ ! -f "$SAMPLE" ]; then
  echo "Creating sample: first $SAMPLE_LINES lines…"
  head -"$SAMPLE_LINES" "$FULL" > "$SAMPLE"
else
  echo "Sample already exists: $SAMPLE"
fi

echo "FASTQ sample: $SAMPLE  ($(stat -c%s "$SAMPLE") bytes)"
