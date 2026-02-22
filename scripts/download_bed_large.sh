#!/usr/bin/env bash
# Download the UCSC RepeatMasker BED (hg38, ~208 MB) for compression testing.
# No replication — single file only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
mkdir -p "$DATA_DIR"

# UCSC RepeatMasker BED for hg38 (~208 MB)
BED_SOURCE_URL="${BED_LARGE_URL:-https://hgdownload.soe.ucsc.edu/hubs/RepeatBrowser2020/hg38/hg38_2020_rmsk.bed}"
BED_OUT="${BED_LARGE_OUT:-$DATA_DIR/hg38_rmsk_208MB.bed}"

if [ ! -f "$BED_OUT" ]; then
  echo "Downloading BED (~208 MB): $BED_SOURCE_URL"
  curl -L -o "$BED_OUT" "$BED_SOURCE_URL" || { echo "Download failed."; exit 1; }
else
  echo "Already have: $BED_OUT"
fi

BYTES=$(stat -f%z "$BED_OUT" 2>/dev/null || stat -c%s "$BED_OUT")
BED_LARGE="$BED_OUT"
export BED_LARGE
echo "BED_LARGE: $BED_OUT ($BYTES bytes)"
