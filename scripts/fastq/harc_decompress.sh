#!/usr/bin/env bash
# Decompress anything nyxfqz_v2 can read. v1-style, per-chunk-picker, and
# globally-clustered (v3) archives are all self-describing by magic bytes
# (confirmed in cmdDecompress) -- this doesn't need to know which mode
# produced the file, so there's no more format auto-detection to do here.
#
# Usage: scripts/harc_decompress.sh <input.nyxz> [output.fastq]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYXFQZ_V2="$ROOT/openzl/nyxfqz_v2"

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/harc_decompress.sh <input.nyxz> [output.fastq]" >&2
  exit 2
fi
in="$1"
out="${2:-${in%.nyxz}.fastq}"

if [[ ! -x "$NYXFQZ_V2" ]]; then
  echo "error: $NYXFQZ_V2 not found -- run scripts/fastq/build_nyxfqz.sh first" >&2
  exit 1
fi

"$NYXFQZ_V2" decompress "$in" "$out"
echo ">> wrote $out"
