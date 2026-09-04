#!/usr/bin/env bash
# Compress via nyxfqz_v2's NATIVE global read clustering. Superseded the
# external harc_reorder.py pre-process entirely -- nyxfqz_v2 already
# reorders SEQ/QUAL (headers untouched) with a calibrated benefit gate and
# writes one self-contained archive. No sidecar, no bundling.
#
# Two things from reading nyxfqz_v2.cpp directly, not guessed:
#   1. NYX_CLUSTER is only checked on the seekable-FILE compress path.
#      Piping through stdin (how compress.sh handles .gz) never reaches it.
#      -> this script always gunzips to a real temp file first.
#   2. Passing a DIRECTORY as the compressor routes to the per-chunk model
#      PICKER instead, which also never checks NYX_CLUSTER. Clustering and
#      the picker are mutually exclusive today.
#      -> this script requires a single .zc file, not compressors/dual/.
#
# Default model is fastq_fixed_cluster.zc (fixed-length Illumina). For
# variable-length or Nanopore data, override:
#   HARC_COMPRESSOR=compressors/dual/fastq_var.zc         scripts/harc_compress.sh ...
#   HARC_COMPRESSOR=compressors/dual/fastq_nano_zstdseq.zc scripts/harc_compress.sh ...
#
# Usage: scripts/harc_compress.sh <input.fastq[.gz]> [output.nyxz] [threads] [memMB]
# Env:   HARC_COMPRESSOR=<path.zc>   NYX_CLUSTER=auto|1|0 (default: auto)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYXFQZ_V2="$ROOT/openzl/nyxfqz_v2"
ZC="${HARC_COMPRESSOR:-$ROOT/artifacts/fastq_models/fastq_fixed_cluster.zc}"
CLUSTER_MODE="${NYX_CLUSTER:-auto}"

if [[ $# -lt 1 ]]; then
  echo "usage: scripts/harc_compress.sh <input.fastq[.gz]> [output.nyxz] [threads] [memMB]" >&2
  exit 2
fi
in="$1"
out="${2:-${in%.gz}.nyxz}"
threads="${3:-}"
memmb="${4:-}"

if [[ ! -x "$NYXFQZ_V2" ]]; then
  echo "error: $NYXFQZ_V2 not found -- run scripts/fastq/build_nyxfqz.sh first" >&2
  exit 1
fi
if [[ -d "$ZC" ]]; then
  echo "error: $ZC is a directory -- that routes to the model picker, which" \
       "never checks NYX_CLUSTER. Point HARC_COMPRESSOR at a single .zc file." >&2
  exit 1
fi
if [[ ! -f "$ZC" ]]; then
  echo "error: compressor $ZC not found. fastq_fixed_cluster.zc is for" \
       "fixed-length Illumina -- set HARC_COMPRESSOR for variable-length" \
       "(fastq_var.zc) or Nanopore (fastq_nano_zstdseq.zc) data." >&2
  exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# Always a real seekable file -- never stdin -- so NYX_CLUSTER is reachable.
if [[ "$in" == *.gz ]]; then
  plain="$work/plain.fastq"
  gzip -dc "$in" > "$plain"
else
  plain="$in"
fi

extra_args=()
if [[ -n "$threads" ]]; then
  extra_args+=("$threads")
  [[ -n "$memmb" ]] && extra_args+=("$memmb")
fi

NYX_CLUSTER="$CLUSTER_MODE" "$NYXFQZ_V2" compress "$ZC" "$plain" "$out" "${extra_args[@]}"
echo ">> wrote $out (NYX_CLUSTER=$CLUSTER_MODE, model=$(basename "$ZC"))"
