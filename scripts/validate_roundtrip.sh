#!/usr/bin/env bash
# validate_roundtrip.sh — byte-exact round-trip check for the FASTA (FAV5) and
# VCF (nyx_vcf) codecs over a diverse corpus. Prints a pass/fail table and exits
# non-zero if anything is not byte-identical.
#
#   scripts/validate_roundtrip.sh [--corpus DIR] [--threads N]
#
# FASTA path : biocompress_preprocessor (FAV5) -> fasta_postprocess -> cmp
#              plus a full pipeline check (+ zli serial compress/decompress) on
#              the larger inputs.
# VCF path   : nyx_vcf compress --verify  (self-checks the round
#              trip internally) -> decompress -> cmp
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CORPUS="$HERE/data/validation"
THREADS="${SLURM_CPUS_PER_TASK:-8}"
while [ $# -gt 0 ]; do case "$1" in
  --corpus) CORPUS="$2"; shift 2;; --threads) THREADS="$2"; shift 2;;
  *) echo "unknown arg $1" >&2; exit 2;; esac; done

PRE="$HERE/tools/biocompress_preprocessor"
FPOST="$HERE/tools/fasta_postprocess"
ZLI="$HERE/openzl/zli"
NYXVCF="$HERE/tools/nyx_vcf"
W="$(mktemp -d "${TMPDIR:-/tmp}/vrt.XXXXXX")"; trap 'rm -rf "$W"' EXIT

pass=0; fail=0
row() { printf '  %-6s %-46s %s\n' "$1" "$2" "$3"; }
check() { if [ "$1" = OK ]; then pass=$((pass+1)); else fail=$((fail+1)); fi; }

echo "=================== FASTA (FAV5) round trip ==================="
for f in "$CORPUS"/fasta/*; do
  case "$f" in *.gz) continue;; esac
  [ -f "$f" ] || continue
  n="$(basename "$f")"; rm -rf "$W/c"; mkdir -p "$W/c"
  if ! "$PRE" "$f" "$W/c" 4 fasta_packed >/dev/null 2>&1; then row FAIL "$n" "encode error"; check FAIL; continue; fi
  if ! "$FPOST" "$W/c" "$W/rt" >/dev/null 2>&1;               then row FAIL "$n" "decode error"; check FAIL; continue; fi
  if cmp -s "$f" "$W/rt"; then r=OK; else r=FAIL; fi
  row "$r" "$n" "pack/unpack ($(stat -c%s "$f") B)"; check "$r"

  # full pipeline (+OpenZL) on inputs >1 MB
  if [ "$(stat -c%s "$f")" -gt 1000000 ]; then
    find "$W/c" -name '*.bin' -print0 | xargs -0 -P"$THREADS" -I{} "$ZLI" compress {} --profile serial --output {}.zl --force >/dev/null 2>&1
    find "$W/c" -name '*.bin.zl' -print0 | xargs -0 -P"$THREADS" -I{} "$ZLI" decompress {} --output {}.dec --force >/dev/null 2>&1
    "$FPOST" "$W/c" "$W/rt2" --suffix .fasta_packed.bin.zl.dec >/dev/null 2>&1
    if cmp -s "$f" "$W/rt2"; then r=OK; else r=FAIL; fi
    row "$r" "$n" "+ OpenZL serial round trip"; check "$r"
  fi
done

echo
echo "=================== VCF (nyx_vcf) round trip ==================="
for f in "$CORPUS"/vcf/* "$HERE"/data/vcf/derived_trio3.vcf "$HERE"/data/vcf/seqc2_hcc1395_ssnv.vcf.gz; do
  case "$f" in *.gz) continue;; esac
  [ -f "$f" ] || continue
  n="$(basename "$f")"
  if "$NYXVCF" compress --verify --threads "$THREADS" --models "$HERE/artifacts/nyx_vcf_models" "$f" "$W/a.nvcf" >/dev/null 2>&1 \
     && "$NYXVCF" decompress --threads "$THREADS" "$W/a.nvcf" "$W/rt.vcf" >/dev/null 2>&1 \
     && cmp -s "$f" "$W/rt.vcf"; then r=OK; else r=FAIL; fi
  row "$r" "$n" "compress --verify / decompress / cmp"; check "$r"
  rm -f "$W/a.nvcf" "$W/rt.vcf"
done

echo
echo "================================================================"
echo "byte-exact: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
