#!/usr/bin/env bash
# test_seq_roundtrip.sh — every FASTA/FASTQ case must come back byte-identical.
#
#   tests/test_seq_roundtrip.sh [--via nyx|native] [--keep]
#
# --via nyx     drive everything through the single `nyx` entry point, which is
#               what a user actually runs (content-based format detection)
# --via native  drive scripts/fastazl and openzl/nyxfqz_v2 directly
#
# Cases that the tool legitimately declines (a file that is not FASTA/FASTQ at
# all) are reported as SKIP, not PASS: a compressor is allowed to refuse, and it
# is allowed to store verbatim, but it is never allowed to return different
# bytes. That distinction is the whole point -- the VCF raw fallback once
# "succeeded" while silently dropping the probed header bytes.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CASES="${SEQ_CASES:-$HERE/tests/seq_cases}"
VIA=nyx
KEEP=0
THREADS="${SLURM_CPUS_ON_NODE:-4}"
while [ $# -gt 0 ]; do
  case "$1" in
    --via)  VIA="$2"; shift 2;;
    --keep) KEEP=1; shift;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# Fail loudly on a missing tool. Without this every case reports "declined" and
# the suite passes while testing nothing -- which is exactly how a CI job that
# never built the FASTQ codec would have gone green.
need() { [ -x "$1" ] || { echo "missing: $1 (run scripts/build_all.sh)" >&2; exit 2; }; }
if [ "$VIA" = nyx ]; then
  need "$HERE/nyx"
fi
need "$HERE/scripts/fastazl"
need "$HERE/openzl/nyxfqz_v2"
need "$HERE/tools/biocompress_preprocessor"

[ -d "$CASES" ] || python3 "$HERE/tests/make_seq_cases.py" "$CASES" >/dev/null
WORK="$(mktemp -d "${TMPDIR:-/tmp}/seqrt.XXXXXX")"
[ "$KEEP" = 1 ] || trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0; skip=0
failed=()

run_case() { # <kind> <file>
  local kind="$1" f="$2" b z r
  b=$(basename "$f"); z="$WORK/$b.z"; r="$WORK/$b.back"
  rm -f "$z" "$r"
  if [ "$VIA" = nyx ]; then
    "$HERE/nyx" compress "$f" "$z" --threads "$THREADS" --quiet --force \
        --format "$kind" >/dev/null 2>&1 || return 2
    "$HERE/nyx" decompress "$z" "$r" --force >/dev/null 2>&1 || return 2
  elif [ "$kind" = fasta ]; then
    "$HERE/scripts/fastazl" compress "$f" "$z" --threads "$THREADS" >/dev/null 2>&1 || return 2
    "$HERE/scripts/fastazl" decompress "$z" "$r" --threads "$THREADS" >/dev/null 2>&1 || return 2
  else
    "$HERE/openzl/nyxfqz_v2" compress "$HERE/artifacts/fastq_models" "$f" "$z" \
        "$THREADS" 500 >/dev/null 2>&1 || return 2
    "$HERE/openzl/nyxfqz_v2" decompress "$z" "$r" >/dev/null 2>&1 || return 2
  fi
  cmp -s "$f" "$r"
}

for kind in fasta fastq; do
  d="$CASES/$kind"
  [ -d "$d" ] || continue
  echo "== $kind (via $VIA)"
  for f in "$d"/*; do
    [ -f "$f" ] || continue
    run_case "$kind" "$f"
    case $? in
      0) pass=$((pass+1)); printf '  ok      %s\n' "$(basename "$f")";;
      2) skip=$((skip+1)); printf '  SKIP    %s (declined)\n' "$(basename "$f")";;
      *) fail=$((fail+1)); failed+=("$(basename "$f")")
         printf '  FAIL    %s (round trip differs)\n' "$(basename "$f")";;
    esac
  done
done

echo
echo "pass=$pass  fail=$fail  declined=$skip"
if [ "$fail" -gt 0 ]; then
  echo "failed: ${failed[*]}"
  exit 1
fi
