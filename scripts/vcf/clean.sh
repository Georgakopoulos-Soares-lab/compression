#!/usr/bin/env bash
# Remove all generated VCF pipeline outputs.
# Keeps source files, schema, and downloaded data.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

shopt -s nullglob

TARGETS=(
  "$HERE"/chunks_train_vcf*
  "$HERE"/chunks_full_vcf*
  "$HERE/out/train_vcf_"*".vcf"
  "$HERE/out/gzip_vcf"
  "$HERE/out/baselines_vcf_large"
  "$HERE/out/timing/vcf_"*".time"
  "$HERE/out/vcf_full_validate_failures.txt"
  "$HERE/out/vcf_large_validate_failures.txt"
  "$HERE/artifacts/vcf_"*".compressor"
)

for t in "${TARGETS[@]}"; do
  rm -rf "$t"
done

echo "Cleaned VCF pipeline outputs under: $HERE"
echo "Kept: data/ (downloads), schemas/vcf_columnar.sddl, tools/vcf_preprocessor.cpp"
