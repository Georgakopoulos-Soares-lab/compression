#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data/bed}"
mkdir -p "$DATA_DIR"

URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/rmsk.txt.gz"
GZ="$DATA_DIR/rmsk.txt.gz"
OUT="$DATA_DIR/hg38_rmsk.txt"

if [ ! -f "$GZ" ]; then
  echo "Downloading: $URL"
  wget -O "$GZ" "$URL"
else
  echo "Already downloaded: $GZ"
fi

if [ ! -f "$OUT" ]; then
  echo "Decompressing…"
  gunzip -k "$GZ"
  mv "$DATA_DIR/rmsk.txt" "$OUT"
else
  echo "Already decompressed: $OUT"
fi

echo "BED file: $OUT  ($(stat -c%s "$OUT") bytes)"
