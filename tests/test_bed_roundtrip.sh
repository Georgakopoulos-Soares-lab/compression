#!/usr/bin/env bash
# Every BED case must round-trip byte-exactly, including the ones that are not
# BED. A compressor that refuses a file is a bug; a compressor that corrupts one
# is worse. Both are failures here.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CASES="${BED_CASES:-$HERE/data/bed/cases}"
TOOL="${NYX_BED:-$HERE/tools/nyx_bed}"
THREADS="${THREADS:-4}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -d "$CASES" ] || python3 "$HERE/tests/make_bed_cases.py" "$CASES"
[ -x "$TOOL" ] || { echo "nyx_bed not built"; exit 1; }

pass=0; fail=0
for f in "$CASES"/*; do
  b=$(basename "$f")
  if ! "$TOOL" compress --threads "$THREADS" --quiet "$f" "$TMP/a.nbed" 2>"$TMP/err"; then
    echo "FAIL  $b  (compress: $(tail -1 "$TMP/err"))"; fail=$((fail+1)); continue
  fi
  if ! "$TOOL" decompress --threads "$THREADS" --quiet "$TMP/a.nbed" "$TMP/a.out" 2>"$TMP/err"; then
    echo "FAIL  $b  (decompress: $(tail -1 "$TMP/err"))"; fail=$((fail+1)); continue
  fi
  if cmp -s "$f" "$TMP/a.out"; then
    pass=$((pass+1))
  else
    echo "FAIL  $b  (not byte-exact)"; fail=$((fail+1))
  fi
  rm -f "$TMP/a.nbed" "$TMP/a.out"
done
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
