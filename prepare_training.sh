#!/usr/bin/env bash
# Prepare 5 clean DNS TSV training files (empty lines stripped)
# then run full OpenZL group training with maximum quality settings.

set -euo pipefail

REPO="/Users/theodorechronopoulos/Desktop/Compress/newrepo/compression"
SRC="$REPO/data/DNS/dns-capture-vertica"
OUT="$REPO/data/DNS/training_clean"
ZLI="$REPO/nyx/openzl/zli"
MODEL="$REPO/nyx/models/lossless_dns/dns_csv.zl_compressor"

# 5 files from different partitions for diversity
FILES=(
  "nom-dns-vertica_p0_0001.tsv"
  "nom-dns-vertica_p5_0002.tsv"
  "nom-dns-vertica_p12_0001.tsv"
  "nom-dns-vertica_p21_0002.tsv"
  "nom-dns-vertica_p29_0001.tsv"
)

echo "=== Step 1: Cleaning training files (stripping empty lines) ==="
mkdir -p "$OUT"
for f in "${FILES[@]}"; do
  echo "  Cleaning $f ..."
  # Strip empty lines, write to training dir
  grep -v '^[[:space:]]*$' "$SRC/$f" > "$OUT/$f"
  orig=$(wc -c < "$SRC/$f" | tr -d ' ')
  clean=$(wc -c < "$OUT/$f" | tr -d ' ')
  lines=$(wc -l < "$OUT/$f" | tr -d ' ')
  echo "    $orig -> $clean bytes ($lines lines)"
done

echo ""
echo "  Training dir: $OUT"
ls -lh "$OUT"

echo ""
echo "=== Step 2: Training compressor (full quality, ~30 min) ==="
mkdir -p "$(dirname "$MODEL")"

echo "  Command:"
echo "  $ZLI train $OUT \\"
echo "    --output $MODEL \\"
echo "    --profile csv --profile-arg '\\t' \\"
echo "    --use-all-samples \\"
echo "    --threads $(sysctl -n hw.ncpu) \\"
echo "    --max-time-secs 1800 \\"
echo "    --force"
echo ""

time "$ZLI" train "$OUT" \
  --output "$MODEL" \
  --profile csv \
  --profile-arg "	" \
  --use-all-samples \
  --threads "$(sysctl -n hw.ncpu)" \
  --max-time-secs 1800 \
  --force

echo ""
echo "=== Done ==="
echo "  Model: $MODEL ($(wc -c < "$MODEL" | tr -d ' ') bytes)"
echo ""
echo "  Quick test:"
echo "  $ZLI compress $OUT/${FILES[0]} --compressor $MODEL --output /tmp/dns_test.zl --force"
