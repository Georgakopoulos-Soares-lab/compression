#!/usr/bin/env bash
# test_gzip_input.sh — gzipped input must round-trip its contents, say so, and
# never leave a wrong archive behind.
#
# A .gz file's exact bytes cannot be rebuilt in general (they depend on the gzip
# program, version and level that wrote them), so `nyx compress x.vcf.gz`
# compresses the contents and names the archive x.vcf.nyx. This checks that
# contract on all four formats, and the case that exposed the old behaviour:
#   printf 'chr1\t1\t2\n' > p.bed; gzip -kn p.bed; nyx test p.bed.gz
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NYX="$HERE/nyx"
W="$(mktemp -d "${TMPDIR:-/tmp}/gzt.XXXXXX")"; trap 'rm -rf "$W"' EXIT
python3 "$HERE/tests/make_sample_inputs.py" "$W"
printf 'chr1\t1\t2\n' > "$W/p.bed"
pass=0; fail=0
check() { if eval "$2"; then echo "  ok    $1"; pass=$((pass+1)); else echo "  FAIL  $1"; fail=$((fail+1)); fi; }

for f in p.bed sample.bed sample.vcf sample.fastq sample.fa; do
  (cd "$W" && gzip -kn "$f" && mv "$f" "$f.orig")
  out=$("$NYX" compress "$W/$f.gz" --threads 2 2>&1)
  check "$f.gz: archive named $f.nyx" '[ -s "$W/$f.nyx" ]'
  check "$f.gz: says the .gz is not what comes back" 'grep -q "not the .gz" <<<"$out"'
  "$NYX" decompress "$W/$f.nyx" --threads 2 --quiet
  check "$f.gz: decompresses to the unzipped contents" 'cmp -s "$W/$f" "$W/$f.orig"'
  check "$f.gz: nyx test passes" '"$NYX" test "$W/$f.gz" --threads 2 >/dev/null 2>&1'
  check "$f.gz: --verify passes" '"$NYX" compress "$W/$f.gz" "$W/v.nyx" --verify --force --quiet --threads 2'
  rm -f "$W/$f" "$W/$f.nyx" "$W/v.nyx"; mv "$W/$f.orig" "$W/$f"
done
echo; echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
