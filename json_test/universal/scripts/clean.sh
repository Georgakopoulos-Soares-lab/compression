#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <input_geojson>"
    echo "Example: $0 json_test/experiments/citylots/citylots.json"
    exit 1
fi

INPUT_JSON="$1"
BASENAME=$(basename "$INPUT_JSON" .json)
EXP_DIR="$(cd "$(dirname "$INPUT_JSON")" && pwd)"

echo "Cleaning generated files for $BASENAME in $EXP_DIR..."

# 1. Remove Directories
rm -rf "$EXP_DIR/chunks_train"
rm -rf "$EXP_DIR/chunks_full"
rm -rf "$EXP_DIR/timing"

# 2. Remove Generated Files
rm -f "$EXP_DIR/${BASENAME}_schema.sddl"
rm -f "$EXP_DIR/${BASENAME}_mapping.json"
rm -f "$EXP_DIR/${BASENAME}_train.json"
rm -f "$EXP_DIR/${BASENAME}.compressor"
rm -f "$EXP_DIR/${BASENAME}.json.gz"

echo "Done. Kept original file: $INPUT_JSON"
