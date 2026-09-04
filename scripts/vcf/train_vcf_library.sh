#!/usr/bin/env bash
# train_vcf_library.sh — build the fixed set of OpenZL compressors that
# `vcfzl compress --archetype <id>` dispatches to.
#
# For every row in artifacts/vcf_models/archetypes.tsv whose training source
# exists on disk, this preprocesses that VCF and trains one CSV/TAB compressor
# into artifacts/vcf_models/<id>.zlc. Rows whose source is a "TODO:" note are
# reported as missing — download a representative file and point the registry
# at it, then re-run.
#
#   scripts/vcf/train_vcf_library.sh [--force] [--threads N] [--train-parts K]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ZLI="$HERE/openzl/zli"
VCF_PRE="$HERE/tools/vcf_preprocessing"
GTC="$HERE/tools/vcf_gtcodec"
MODEL_DIR="$HERE/artifacts/vcf_models"
REGISTRY="$MODEL_DIR/archetypes.tsv"

FORCE=0
THREADS="${SLURM_CPUS_PER_TASK:-16}"
TRAIN_PARTS=5
MAX_TIME_SECS="${MAX_TIME_SECS:-1800}"

while [ $# -gt 0 ]; do
  case "$1" in
    --force)       FORCE=1; shift;;
    --threads)     THREADS="$2"; shift 2;;
    --train-parts) TRAIN_PARTS="$2"; shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[ -x "$ZLI" ]     || { echo "build OpenZL first: scripts/build_all.sh" >&2; exit 1; }
[ -x "$VCF_PRE" ] || { echo "build tools first: scripts/build_all.sh" >&2; exit 1; }
[ -f "$REGISTRY" ] || { echo "missing registry: $REGISTRY" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/vcflib.XXXXXX")"
trap 'rm -rf "$work"' EXIT

ready=0 missing=0 skipped=0
while IFS=$'\t' read -r id rule src status desc; do
  [ -n "$id" ] || continue
  case "$id" in \#*) continue;; esac
  out="$MODEL_DIR/$id.zlc"

  if [ -f "$out" ] && [ "$FORCE" != 1 ]; then
    echo "= $id: already trained ($(stat -c%s "$out") B) — use --force to rebuild"
    ready=$((ready+1)); continue
  fi

  case "$src" in TODO:*) echo "! $id: $src — MISSING"; missing=$((missing+1)); continue;; esac
  srcfile="$HERE/$src"; [ -f "$src" ] && srcfile="$src"
  if [ ! -f "$srcfile" ]; then
    echo "! $id: no training source ($src) — MISSING"
    missing=$((missing+1)); continue
  fi

  # decompress .gz sources into scratch
  if [ "${srcfile##*.}" = gz ]; then
    plain="$work/$(basename "${srcfile%.gz}")"
    echo ">> $id: decompressing $(basename "$srcfile")"
    pigz -dc -p "$THREADS" "$srcfile" > "$plain" 2>/dev/null || gzip -dc "$srcfile" > "$plain"
    srcfile="$plain"
  fi

  echo ">> $id: preprocessing $(basename "$srcfile")"
  pk="$work/$id"; rm -rf "$pk"
  # bound part count so tiny sources don't fragment into unusable pieces
  pp=$(( $(stat -c%s "$srcfile") / (16*1000*1000) )); [ "$pp" -lt 1 ] && pp=1
  [ "$pp" -gt "$THREADS" ] && pp="$THREADS"
  "$VCF_PRE" "$srcfile" "$pk" --threads "$pp" --max-chunk-mib 40 --force >/dev/null

  # Train on GT-CODED parts: that is what vcfzl feeds the model at runtime.
  td="$pk/train"; mkdir -p "$td"; n=0
  for c in "$pk"/body_parts/*.vcfbody; do
    [ -f "$c" ] || continue
    if [ "${VCFZL_GT:-0}" = 1 ] && [ -x "$GTC" ]; then
      "$GTC" pack "$c" "$td/$(basename "$c").vgt" >/dev/null 2>&1 || cp "$c" "$td/"
    else cp "$c" "$td/"; fi
    n=$((n+1)); [ "$n" -ge "$TRAIN_PARTS" ] && break
  done
  if [ "$n" -eq 0 ]; then
    echo "! $id: source produced no body parts — SKIPPED"
    skipped=$((skipped+1)); continue
  fi

  # Train one candidate per profile and keep the one that actually compresses
  # this archetype's chunks smallest (csv splits VCF columns; lz is OpenZL
  # 0.2.4's trainable LZ). VCF_PROFILES overrides the candidate list.
  echo ">> $id: training on $n chunks (<= ${MAX_TIME_SECS}s), candidates: ${VCF_PROFILES:-csv u8 lz}"
  # a candidate failing to train is expected; errexit must be off in this loop
  set +e
  best=""; bestSize=""; bestProfile=""
  for p in ${VCF_PROFILES:-csv u8 lz}; do
    args=(--profile "$p"); [ "$p" = csv ] && args+=(--profile-arg $'\t')
    cand="$pk/model_$p.zlc"
    "$ZLI" train "$td" "${args[@]}" \
      --output "$cand" --force --threads "$THREADS" --use-all-samples \
      --max-time-secs "$MAX_TIME_SECS" >/dev/null 2>&1 || true
    [ -s "$cand" ] || { echo "   $p: no model"; continue; }
    tot=0; ok=1
    for c in "$td"/*; do
      "$ZLI" compress "$c" --compressor "$cand" --output "$pk/v.zl" --force >/dev/null 2>&1 || { ok=0; break; }
      tot=$(( tot + $(stat -c%s "$pk/v.zl") ))
    done
    [ "$ok" = 1 ] || { echo "   $p: unusable"; continue; }
    echo "   $p: $tot bytes"
    if [ -z "$bestSize" ] || [ "$tot" -lt "$bestSize" ]; then
      bestSize="$tot"; best="$cand"; bestProfile="$p"
    fi
  done
  set -e
  if [ -n "$best" ]; then
    cp "$best" "$out"
    printf '%s\n' "$bestProfile" > "${out%.zlc}.profile"
    echo "OK $id -> $out ($(stat -c%s "$out") B, profile=$bestProfile)"; ready=$((ready+1))
  else
    echo "! $id: training FAILED"; skipped=$((skipped+1))
  fi
  rm -rf "$pk"
done < <(grep -v '^#' "$REGISTRY")

echo
echo "library: $ready ready, $missing missing (need source files), $skipped failed"
echo "check with:  scripts/vcf/vcfzl archetypes"
