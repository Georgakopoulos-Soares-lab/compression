#!/usr/bin/env bash
# memory_sweep.sh — is nyx_vcf's peak memory actually a budget, and what does it cost?
#
# Peak RSS is (threads x block bytes x per-block overhead) and is independent of
# file size; --max-mem-mb sizes the blocks from a budget. This measures whether
# the budget holds and what ratio it costs, on files of very different shape.
# --no-calibrate: the manuscript's VCF tables were measured at OpenZL's default
# entropy level, before level calibration existed. Pinning it keeps those tables
# regenerable from the package; drop the flag to measure the calibrated default.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NYX="$HERE/tools/nyx_vcf"; MODELS="$HERE/artifacts/nyx_vcf_models"
WORK="${NYX_VCF_WORK:-$HERE/data/vcf/work}"; THREADS="${SLURM_CPUS_ON_NODE:-16}"
OUT="$HERE/results/vcf_memory_sweep.csv"
[ "${1:-}" = --out ] && OUT="$2"
S="$WORK/mem.$$"; mkdir -p "$S" "$(dirname "$OUT")"; trap 'rm -rf "$S"' EXIT
echo "file,archetype,orig_bytes,budget_mb,block_mb,comp_bytes,ratio,peak_RSS_MB,comp_sec,roundtrip" > "$OUT"
run() {
  local f="$1" arch="$2" budget="$3" b sz
  b=$(basename "$f"); sz=$(stat -c%s "$f")
  local flag=(--block-mb 64) lbl=0
  [ "$budget" != none ] && { flag=(--max-mem-mb "$budget"); lbl="$budget"; }
  local o; o=$(/usr/bin/time -f '%e %M' "$NYX" compress --no-calibrate --threads "$THREADS" "${flag[@]}" \
        --models "$MODELS" --quiet "$f" "$S/a.nvcf" 2>&1 >/dev/null | tail -1)
  local secs=${o%% *} rss=${o##* }
  "$NYX" decompress --threads "$THREADS" --quiet "$S/a.nvcf" "$S/a.rt" >/dev/null 2>&1
  local rt=MISMATCH; cmp -s "$f" "$S/a.rt" && rt=OK
  local cb; cb=$(stat -c%s "$S/a.nvcf")
  local blk; blk=$(awk -v bud="$lbl" -v t="$THREADS" 'BEGIN{printf "%.0f", bud? (bud*1000000/(t*8))/1e6 : 64}')
  awk -v f="$b" -v a="$arch" -v s="$sz" -v bu="$lbl" -v bl="$blk" -v c="$cb" -v r="$rss" -v se="$secs" -v rt="$rt" \
    'BEGIN{printf "\"%s\",%s,%s,%s,%s,%s,%.3f,%s,%s,%s\n",f,a,s,bu,bl,c,s/c,r/1024,se,rt}' >> "$OUT"
  printf "  %-30s budget=%-6s ratio=%7.2fx peak=%6.2f GB %s\n" "$b" "$lbl" \
    "$(awk -v s=$sz -v c=$cb 'BEGIN{print s/c}')" "$(awk -v r=$rss 'BEGIN{print r/1048576}')" "$rt"
  rm -f "$S/a.nvcf" "$S/a.rt"
}
for spec in "1000G.chr22.phase3.vcf:panel" \
            "seqc2_hcc1395_ssnv_superset.vcf:somatic" \
            "gnomad_v4.1_genomes_chrY.vcf:sites-frequency"; do
  f="$WORK/${spec%%:*}"; [ -s "$f" ] || continue
  echo "== $(basename "$f")"
  for bud in none 1000 2000 4000; do run "$f" "${spec##*:}" "$bud"; done
done
echo; echo "wrote $OUT"; column -s, -t "$OUT"
