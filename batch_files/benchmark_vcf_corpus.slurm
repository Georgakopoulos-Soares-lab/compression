#!/bin/bash
#SBATCH --job-name=vcf_corpus
#SBATCH --partition=pvc
#SBATCH --account=BCS25105
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --time=16:00:00
#SBATCH --output=results/vcf_corpus_%j.out
#SBATCH --error=results/vcf_corpus_%j.err

# =============================================================================
# VCF auto-detect accuracy + compression over the validation corpus
# (>=3 files per archetype, built by scripts/vcf/get_corpus.sh).
#
# For every corpus file:
#   * vcfzl classify          -> predicted archetype vs the manifest label
#   * vcfzl compress --verify  -> archetype used, ratio, byte-exact round trip
#   * gzip -9 / zstd -19 --long=27 / xz -9e --block-size=192MiB  (same args as
#     the FASTA/FASTQ benchmarks)
#
# SAMPLE_MB caps each input (whole-record head) so the sweep is affordable;
# SAMPLE_MB=0 uses whole files.
#
#   sbatch batch_files/benchmark_vcf_corpus.slurm
#   SAMPLE_MB=0 sbatch batch_files/benchmark_vcf_corpus.slurm
# =============================================================================
set -uo pipefail
HERE="/scratch/11579/alexmargaris/compression"
cd "$HERE" || exit 1
THREADS="${SLURM_CPUS_PER_TASK:-16}"
JOB="${SLURM_JOB_ID:-local}"
SAMPLE_MB="${SAMPLE_MB:-300}"
BENCH_DIR="$HERE/out/vcf_corpus_${JOB}"
CSV="$HERE/results/vcf_corpus_${JOB}.csv"
MAN="$HERE/data/vcf/corpus/manifest.tsv"
VCFZL="$HERE/scripts/vcf/vcfzl"
export ZLI="$HERE/openzl/zli"
mkdir -p "$BENCH_DIR" "$HERE/results"

[ -s "$MAN" ] || { echo "FATAL: no corpus manifest ($MAN) — run scripts/vcf/get_corpus.sh"; exit 1; }
[ "${SKIP_BUILD:-0}" = 1 ] || bash "$HERE/scripts/build_all.sh" || echo "WARN build_all"

timed() { local tf="$1"; shift; /usr/bin/time -f 'elapsed_sec=%e maxrss_kb=%M' -o "$tf" "$@"; }
rf()    { sed -n "s/.*$2=\([0-9.]*\).*/\1/p" "$1" 2>/dev/null | head -n1; }
ratio() { awk -v o="$1" -v c="$2" 'BEGIN{if(c+0>0)printf"%.3f",o/c;else printf"0"}'; }

echo "file,expected_archetype,predicted_archetype,classify_ok,method,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,roundtrip,notes" > "$CSV"

n_ok=0; n_tot=0
while IFS=$'\t' read -r expect path status note; do
  [ -n "${path:-}" ] && [ -f "$path" ] || { echo "skip (missing): ${path:-?}"; continue; }
  base="$(basename "$path")"
  echo "==================== $base   (expect: $expect) ===================="

  # materialise a plain-text sample (whole records)
  s="$BENCH_DIR/${base%.gz}.sample.vcf"; s="${s%.bgz}"
  if [ "$SAMPLE_MB" = 0 ]; then
    case "$path" in *.gz|*.bgz) gzip -dc "$path" > "$s";; *) cp "$path" "$s";; esac
  else
    case "$path" in
      *.gz|*.bgz) gzip -dc "$path" | head -c "$((SAMPLE_MB*1024*1024))" > "$s";;
      *)          head -c "$((SAMPLE_MB*1024*1024))" "$path" > "$s";;
    esac
    # trim trailing partial line
    head -n -1 "$s" > "$s.t" && mv "$s.t" "$s"
  fi
  orig=$(stat -c%s "$s")

  pred="$("$VCFZL" classify "$s" 2>/dev/null | head -n1)"
  ok=0; [ "$pred" = "$expect" ] && ok=1
  n_tot=$((n_tot+1)); n_ok=$((n_ok+ok))
  echo "  classify: predicted=$pred  expected=$expect  $([ $ok = 1 ] && echo MATCH || echo MISS)"

  # our method (auto archetype) with round-trip verify
  cz="$BENCH_DIR/${base}.vcfz"; tfc="$BENCH_DIR/${base}.our.time"
  rm -f "$cz"
  used=""; rt="FAIL"; cb=0
  if timed "$tfc" "$VCFZL" compress "$s" "$cz" --archetype auto --verify --threads "$THREADS" > "$BENCH_DIR/${base}.our.log" 2>&1; then
    cb=$(stat -c%s "$cz" 2>/dev/null || echo 0)
    used=$(sed -n 's/.*archetype=\([a-z-]*\).*/\1/p' "$BENCH_DIR/${base}.our.log" | tail -n1)
    grep -q 'verify OK' "$BENCH_DIR/${base}.our.log" && rt="OK"
  fi
  cs=$(rf "$tfc" elapsed_sec)
  printf '"%s",%s,%s,%d,our_method_vcfzl,%s,%s,%s,%s,,%s,archetype_used=%s\n' \
    "$base" "$expect" "$pred" "$ok" "$orig" "$cb" "$(ratio "$orig" "$cb")" "${cs:-NA}" "$rt" "${used:-?}" >> "$CSV"
  echo "  our_method: archetype=$used ratio=$(ratio "$orig" "$cb") roundtrip=$rt"

  # baselines (identical args to the FASTA / FASTQ / main-VCF benchmarks)
  for m in gzip pigz zstd 7z xz; do
    cf="$BENCH_DIR/${base}.$m"; tf="$BENCH_DIR/${base}.$m.time"; rm -f "$cf"
    case "$m" in
      gzip) timed "$tf" bash -c "gzip -9 -c '$s' > '$cf'";;
      pigz) timed "$tf" bash -c "pigz -9 -p $THREADS -c '$s' > '$cf'";;
      zstd) timed "$tf" bash -c "zstd -q -f -19 --long=27 -T$THREADS -o '$cf' '$s'";;
      7z)   timed "$tf" 7z a -t7z -mx=9 -mmt="$THREADS" -bso0 -bsp0 "$cf" "$s";;
      xz)   timed "$tf" bash -c "xz -9e -T$THREADS --block-size=192MiB -c '$s' > '$cf'";;
    esac
    cb=$(stat -c%s "$cf" 2>/dev/null || echo 0); cs=$(rf "$tf" elapsed_sec)
    printf '"%s",%s,%s,%d,%s,%s,%s,%s,%s,,OK,\n' \
      "$base" "$expect" "$pred" "$ok" "$m" "$orig" "$cb" "$(ratio "$orig" "$cb")" "${cs:-NA}" >> "$CSV"
    echo "  $m: ratio=$(ratio "$orig" "$cb")"
    rm -f "$cf"
  done
  rm -f "$s" "$cz"
done < "$MAN"

echo
echo "==================================================================="
echo "auto-detect accuracy: $n_ok / $n_tot"
awk -F, 'NR>1 && $5=="our_method_vcfzl"{t++; if($4==1)o++} END{printf "  (our_method rows: %d, classify match %d)\n",t,o}' "$CSV"
echo "CSV: $CSV"
