#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  vcf_compress_parts.sh --pack <pack_dir> --compressor <.zlc> [--zli <path>] [--jobs <n>|--threads <n>]

Compresses all *.vcfbody files in <pack_dir>/body_parts into <pack_dir>/zl/*.zl.

Defaults:
  --zli  ./openzl/zli
  --jobs 1   (recommended; higher may hit OpenZL allocation errors)
USAGE
}

pack=""
compressor=""
zli="./openzl/zli"
jobs=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pack)
      pack="$2"; shift 2;;
    --compressor)
      compressor="$2"; shift 2;;
    --zli)
      zli="$2"; shift 2;;
    --jobs)
      jobs="$2"; shift 2;;
    --threads)
      jobs="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

if [[ -z "$pack" || -z "$compressor" ]]; then
  usage
  exit 2
fi

body_dir="$pack/body_parts"
out_dir="$pack/zl"

if [[ ! -d "$body_dir" ]]; then
  echo "Missing body parts dir: $body_dir" >&2
  exit 2
fi

rm -rf "$out_dir"
mkdir -p "$out_dir"

if [[ "$jobs" -le 1 ]]; then
  i=0
  for f in "$body_dir"/*.vcfbody; do
    i=$((i+1))
    base="$(basename "$f")"
    out="$out_dir/${base}.zl"
    echo "[$i] compress $base"
    "$zli" compress "$f" --compressor "$compressor" --output "$out" --force > /dev/null
  done
else
  mapfile -d '' PARTS < <(find "$body_dir" -maxdepth 1 -type f -name '*.vcfbody' -print0 | sort -z)
  part_count="${#PARTS[@]}"
  if [[ "$part_count" -eq 0 ]]; then
    echo "No .vcfbody files found in $body_dir" >&2
    exit 2
  fi

  if [[ "$jobs" -gt "$part_count" ]]; then
    jobs="$part_count"
  fi

  echo "Parallel compression: $jobs workers for $part_count parts"
  export zli compressor out_dir
  set +e
  printf '%s\0' "${PARTS[@]}" \
    | xargs -0 -P"$jobs" -I{} bash -lc '
      f="$1"
      base="$(basename "$f")"
      out="$out_dir/${base}.zl"
      "$zli" compress "$f" --compressor "$compressor" --output "$out" --force > /dev/null
    ' _ {}
  xrc=$?
  set -e
  if [[ "$xrc" -ne 0 ]]; then
    echo "Parallel workers reported failures (exit $xrc). Retrying missing parts serially..." >&2
  fi

  # Retry any failed outputs serially (helps when a worker hits temporary allocation errors).
  missing=0
  for f in "${PARTS[@]}"; do
    base="$(basename "$f")"
    out="$out_dir/${base}.zl"
    if [[ ! -s "$out" ]]; then
      missing=$((missing+1))
      echo "Retry serial: $base"
      "$zli" compress "$f" --compressor "$compressor" --output "$out" --force > /dev/null
    fi
  done
  if [[ "$missing" -gt 0 ]]; then
    echo "Retried $missing failed/missing parts serially"
  fi
fi

PACK="$pack" python3 - <<'PY'
import os, glob

pack = os.environ['PACK']
raw = sorted(glob.glob(pack + '/body_parts/*.vcfbody'))
zl = sorted(glob.glob(pack + '/zl/*.zl'))
raw_bytes = sum(os.path.getsize(p) for p in raw)
zl_bytes = sum(os.path.getsize(p) for p in zl)

print('parts', len(raw), 'compressed', len(zl))
print('raw_bytes', raw_bytes)
print('zl_bytes', zl_bytes)
print('ratio', round(raw_bytes / zl_bytes, 2) if zl_bytes else None)
PY
