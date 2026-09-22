#!/usr/bin/env bash
# test_vcf_roundtrip.sh — byte-exactness of tools/nyx_vcf on the edge-case corpus.
#
# Every case is run under each ablation setting as well, because a transform
# that is only correct when another one is enabled is still a bug.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NYX="${NYX:-$HERE/tools/nyx_vcf}"
DIR="$HERE/tests/vcf_edge_cases"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/nyxvcf_test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
[ -x "$NYX" ] || { echo "build first: scripts/build_all.sh" >&2; exit 1; }
HOSTILE="$HERE/tests/vcf_hostile"
[ -d "$DIR" ] || python3 "$HERE/tests/make_vcf_edge_cases.py" "$DIR" >/dev/null
[ -d "$HOSTILE" ] || python3 "$HERE/tests/make_vcf_hostile.py" "$HOSTILE" >/dev/null

MODES=(
  ""
  "--no-pbwt"
  "--no-values"
  "--no-dict"
  "--no-annot"
  "--no-info-split"
  "--raw-blocks"
  "--block-mb 1"
  "--no-pbwt --no-values --no-dict --no-annot --no-info-split"
)
pass=0 fail=0
for f in "$DIR"/*.vcf; do
  b=$(basename "$f")
  for mode in "${MODES[@]}"; do
    if "$NYX" compress --threads 2 --quiet $mode "$f" "$TMP/a.nvcf" 2>"$TMP/err" \
       && "$NYX" decompress --threads 2 --quiet "$TMP/a.nvcf" "$TMP/a.rt" 2>>"$TMP/err" \
       && cmp -s "$f" "$TMP/a.rt"; then
      pass=$((pass+1))
    else
      fail=$((fail+1))
      printf 'FAIL  %-32s %s\n' "$b" "${mode:-(default)}"
      head -2 "$TMP/err" | sed 's/^/      /'
    fi
    rm -f "$TMP/a.nvcf" "$TMP/a.rt"
  done
done
# Hostile inputs: malformed, degenerate or extreme files. A compressor must
# never refuse, crash, or -- worst of all -- round-trip them incorrectly.
for f in "$HOSTILE"/*.vcf; do
  b=$(basename "$f")
  if "$NYX" compress --threads 2 --quiet --block-mb 1 "$f" "$TMP/h.nvcf" 2>/dev/null \
     && "$NYX" decompress --threads 2 --quiet "$TMP/h.nvcf" "$TMP/h.rt" 2>/dev/null \
     && cmp -s "$f" "$TMP/h.rt"; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); printf 'FAIL  hostile: %s\n' "$b"
  fi
  rm -f "$TMP/h.nvcf" "$TMP/h.rt"
done

# gzip input must be handled from magic bytes, not the file name
gzip -c "$DIR/gt_shapes.vcf" > "$TMP/g.vcf.gz"
if "$NYX" compress --threads 2 --quiet "$TMP/g.vcf.gz" "$TMP/g.nvcf" 2>/dev/null \
   && "$NYX" decompress --threads 2 --quiet "$TMP/g.nvcf" "$TMP/g.rt" 2>/dev/null \
   && cmp -s "$DIR/gt_shapes.vcf" "$TMP/g.rt"; then
  pass=$((pass+1))
else
  fail=$((fail+1)); echo "FAIL  gzip input"
fi
echo
echo "round-trip: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
