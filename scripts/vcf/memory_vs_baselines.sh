#!/usr/bin/env bash
# memory_vs_baselines.sh — peak RSS of nyx_vcf against the baselines.
#
# The main VCF benchmark records peak RSS for nyx_vcf only. Memory is one of the
# paper's claims, so it has to be measured for the competition too, on the same
# machine and inputs.
# --no-calibrate: the manuscript's VCF tables were measured at OpenZL's default
# entropy level, before level calibration existed. Pinning it keeps those tables
# regenerable from the package; drop the flag to measure the calibrated default.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYX="$HERE/tools/nyx_vcf"; MODELS="$HERE/artifacts/nyx_vcf_models"
WORK="${NYX_VCF_WORK:-$HERE/data/vcf/work}"; THREADS="${SLURM_CPUS_ON_NODE:-16}"
OUT="$HERE/results/vcf_memory_vs_baselines.csv"
S="$WORK/memcmp.$$"; mkdir -p "$S"; trap 'rm -rf "$S"' EXIT
echo "tool,file,orig_bytes,comp_bytes,ratio,comp_sec,peak_RSS_MB" > "$OUT"
m() { /usr/bin/time -f '%e %M' "$@" 2>&1 >/dev/null | tail -1; }
row() { awk -v t="$1" -v f="$2" -v o="$3" -v c="$4" -v s="$5" -v r="$6" \
  'BEGIN{printf "%s,\"%s\",%s,%s,%.3f,%s,%.1f\n",t,f,o,c,(c?o/c:0),s,r/1024}' >> "$OUT"; }
for spec in "1000G.chr22.phase3.vcf:panel" "clinvar_GRCh38.vcf:sites" "HG002_GRCh38_v4.2.1.vcf:single"; do
  f="$WORK/${spec%%:*}"; [ -s "$f" ] || continue
  b=$(basename "$f"); o=$(stat -c%s "$f"); echo "== $b"
  # NYX at its default and under a 2 GB budget
  x=$(m "$NYX" compress --no-calibrate --threads "$THREADS" --models "$MODELS" --quiet "$f" "$S/a.nvcf")
  row nyx_vcf "$b" "$o" "$(stat -c%s "$S/a.nvcf")" "${x%% *}" "${x##* }"; rm -f "$S/a.nvcf"
  x=$(m "$NYX" compress --no-calibrate --threads "$THREADS" --models "$MODELS" --max-mem-mb 2000 --quiet "$f" "$S/a.nvcf")
  row nyx_vcf_2GBbudget "$b" "$o" "$(stat -c%s "$S/a.nvcf")" "${x%% *}" "${x##* }"; rm -f "$S/a.nvcf"
  for t in "gzip:pigz -9 -p$THREADS -c" "zstd:zstd -19 --long=27 -T$THREADS -q -c" "xz:xz -9e -T$THREADS -c"; do
    n=${t%%:*}; cmd=${t#*:}
    x=$(m bash -c "$cmd '$f' > '$S/a.out'")
    row "$n" "$b" "$o" "$(stat -c%s "$S/a.out")" "${x%% *}" "${x##* }"; rm -f "$S/a.out"
  done
done
echo; column -s, -t "$OUT"
