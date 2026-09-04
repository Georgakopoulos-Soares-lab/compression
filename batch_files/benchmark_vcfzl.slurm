#!/bin/bash
#SBATCH --job-name=bench_vcfzl
#SBATCH --partition=pvc
#SBATCH --account=BCS25105
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --time=16:00:00
#SBATCH --output=results/bench_vcfzl_%j.out
#SBATCH --error=results/bench_vcfzl_%j.err

# =============================================================================
# Benchmark the archetype-dispatched OpenZL pipeline (scripts/vcf/vcfzl).
#
# Steps:
#   1. build tools + OpenZL
#   2. scripts/vcf/get_archetype_data.sh   (download ClinVar, derive trio/pair/gvcf)
#   3. scripts/vcf/train_vcf_library.sh    (train all 8 archetype compressors)
#   4. for each archetype's representative VCF (first SAMPLE_MB MB):
#        - vcfzl compress --archetype <id>   -> ratio / time, cmp round trip
#        - vcfzl compress  (auto)            -> does auto-detect pick <id>?
#        - gzip -9 / zstd -19 / xz -9e on the whole file  (reference)
#
# Submit:   sbatch batch_files/benchmark_vcfzl.slurm
# Override: SAMPLE_MB=0 sbatch ...        (whole files; default 200 MB)
#           SKIP_SETUP=1 sbatch ...       (skip get-data + train; models must exist)
# =============================================================================
set -uo pipefail

HERE="/scratch/11579/alexmargaris/compression"
cd "$HERE" || exit 1
export SLURM_CPUS_PER_TASK="${SLURM_CPUS_PER_TASK:-16}"
THREADS="$SLURM_CPUS_PER_TASK"
JOB="${SLURM_JOB_ID:-local}"
BENCH_DIR="${BENCH_DIR:-$HERE/out/bench_vcfzl_${JOB}}"
CSV="${CSV:-$HERE/results/benchmark_vcfzl_${JOB}.csv}"
TXT="${TXT:-$HERE/results/benchmark_vcfzl_${JOB}.txt}"
SAMPLE_MB="${SAMPLE_MB:-200}"
SKIP_SETUP="${SKIP_SETUP:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"

VCFZL="$HERE/scripts/vcf/vcfzl"
REG="$HERE/artifacts/vcf_models/archetypes.tsv"

mkdir -p "$BENCH_DIR" "$HERE/results"
timed() { local tf="$1"; shift; /usr/bin/time -f 'elapsed_sec=%e maxrss_kb=%M' -o "$tf" "$@"; }
rf()    { sed -n "s/.*$2=\([0-9.]*\).*/\1/p" "$1" 2>/dev/null | head -n1; }
ratio() { awk -v o="$1" -v c="$2" 'BEGIN{ if(c+0>0) printf "%.3f",o/c; else printf "0" }'; }
emit()  { printf '%s\n' "$1" >> "$CSV"; }

echo "file,archetype,method,orig_bytes,comp_bytes,ratio,comp_sec,decomp_sec,roundtrip,notes" > "$CSV"

# ---- 1. build ----------------------------------------------------------
if [ "$SKIP_BUILD" != 1 ]; then bash "$HERE/scripts/build_all.sh" || echo "WARN build_all"; fi
[ -x "$HERE/openzl/zli" ] || { echo "FATAL: no zli"; exit 1; }

# ---- 2 + 3. data + library ------------------------------------------
if [ "$SKIP_SETUP" != 1 ]; then
  echo "=== get_archetype_data ==="; bash "$HERE/scripts/vcf/get_archetype_data.sh" || echo "WARN get-data"
  # retrain the whole library from the current sources for a consistent paper run
  # (RETRAIN_ALL=0 -> only fill in missing models)
  echo "=== train_vcf_library ==="
  FORCE_FLAG=--force; [ "${RETRAIN_ALL:-1}" = 0 ] && FORCE_FLAG=""
  bash "$HERE/scripts/vcf/train_vcf_library.sh" $FORCE_FLAG --threads "$THREADS" || echo "WARN train"
fi
"$VCFZL" archetypes || true

# ---- 4. per-archetype benchmark ------------------------------------
while IFS=$'\t' read -r id rule src status desc; do
  [ -n "$id" ] || continue; case "$id" in \#*) continue;; esac
  case "$src" in TODO:*) echo "SKIP $id (no source)"; continue;; esac
  srcfile="$HERE/$src"; [ -f "$src" ] && srcfile="$src"
  [ -f "$srcfile" ]                 || { echo "SKIP $id (missing $src)"; continue; }
  [ -f "$HERE/artifacts/vcf_models/$id.zlc" ] || { echo "SKIP $id (no model)"; continue; }

  # materialise a working .vcf (decompress gz, optionally head to SAMPLE_MB)
  work="$BENCH_DIR/${id}.vcf"
  if [ "${srcfile##*.}" = gz ]; then pigz -dc -p "$THREADS" "$srcfile" > "$work.full" 2>/dev/null; else cp "$srcfile" "$work.full"; fi
  if [ "$SAMPLE_MB" = 0 ]; then mv "$work.full" "$work"
  else head -c "$(( SAMPLE_MB*1000*1000 ))" "$work.full" | head -n -1 > "$work"; rm -f "$work.full"; fi
  orig=$(stat -c%s "$work")
  echo "=================================================================="
  echo "=== $id  ($(basename "$src"), $orig bytes)"
  echo "=================================================================="

  # -- vcfzl with the correct archetype (compress --verify self-checks the round trip) --
  tc="$BENCH_DIR/z.c.time" td="$BENCH_DIR/z.d.time"
  : > "$BENCH_DIR/z.err"
  timed "$tc" "$VCFZL" compress --archetype "$id" --verify --threads "$THREADS" "$work" "$BENCH_DIR/z.vcfz" 2>>"$BENCH_DIR/z.err"
  cb=0; [ -s "$BENCH_DIR/z.vcfz" ] && cb=$(stat -c%s "$BENCH_DIR/z.vcfz")
  rt=FAIL
  if [ "$cb" -gt 0 ]; then
    timed "$td" "$VCFZL" decompress --threads "$THREADS" "$BENCH_DIR/z.vcfz" "$BENCH_DIR/z.rt.vcf" 2>>"$BENCH_DIR/z.err"
    cmp -s "$work" "$BENCH_DIR/z.rt.vcf" && rt=OK
  fi
  fb=$(grep -oE '[0-9]+ part\(s\) used a generic fallback' "$BENCH_DIR/z.err" | grep -oE '^[0-9]+' || echo 0)
  emit "\"$(basename "$src")\",$id,vcfzl_openzl,$orig,$cb,$(ratio "$orig" "$cb"),$(rf "$tc" elapsed_sec),$(rf "$td" elapsed_sec),$rt,fallback_parts=$fb"
  echo "  vcfzl (--archetype $id): ratio=$(ratio "$orig" "$cb")  rt=$rt  fallback_parts=$fb"
  rm -f "$BENCH_DIR/z.vcfz" "$BENCH_DIR/z.rt.vcf"

  # -- vcfzl auto-detect: does it choose $id? --
  guess="$("$VCFZL" classify "$work" 2>/dev/null || echo unknown)"
  emit "\"$(basename "$src")\",$id,vcfzl_autodetect,$orig,,,,,$( [ "$guess" = "$id" ] && echo OK || echo "MISMATCH:$guess" ),detected=$guess"
  echo "  auto-detect -> $guess $( [ "$guess" = "$id" ] && echo '(correct)' || echo '(MISMATCH)')"

  # -- reference codecs on the whole working file --
  for tool in gzip zstd xz; do
    cf="$BENCH_DIR/r.$tool"; df="$BENCH_DIR/r.dec"; ct="$BENCH_DIR/r.c.time" dt="$BENCH_DIR/r.d.time"
    case $tool in
      gzip) timed "$ct" bash -c "gzip -9 -c '$work' > '$cf'";            timed "$dt" bash -c "gzip -dc '$cf' > '$df'";;
      zstd) timed "$ct" bash -c "zstd -q -f -19 --long=27 -T$THREADS -o '$cf' '$work'"; timed "$dt" bash -c "zstd -q -f -d --long=27 -o '$df' '$cf'";;
      xz)   timed "$ct" bash -c "xz -9e -T$THREADS --block-size=192MiB -c '$work' > '$cf'"; timed "$dt" bash -c "xz -dc -T$THREADS '$cf' > '$df'";;
    esac
    rcb=0; [ -s "$cf" ] && rcb=$(stat -c%s "$cf")
    { [ -s "$df" ] && cmp -s "$work" "$df"; } && rrt=OK || rrt=FAIL
    emit "\"$(basename "$src")\",$id,$tool,$orig,$rcb,$(ratio "$orig" "$rcb"),$(rf "$ct" elapsed_sec),$(rf "$dt" elapsed_sec),$rrt,"
    echo "  $tool: ratio=$(ratio "$orig" "$rcb")  rt=$rrt"
    rm -f "$cf" "$df"
  done
  rm -f "$work"
done < <(grep -v '^#' "$REG")

# ---- summary --------------------------------------------------------
python3 - "$CSV" > "$TXT" <<'PY'
import csv, sys
rows=[r for r in csv.DictReader(open(sys.argv[1]))]
byf={}
for r in rows: byf.setdefault((r["file"],r["archetype"]),[]).append(r)
w="{:<18} {:>10} {:>10} {:>9} {:>9} {:>7}"
for (f,a),rs in byf.items():
    o=int(rs[0]["orig_bytes"])
    print(f"\n### {a}   {f}   ({o/1e6:.1f} MB)")
    print(w.format("method","ratio","comp_MB","comp_s","decomp_s","rt"))
    print("-"*68)
    for r in rs:
        cb=r["comp_bytes"]
        print(w.format(r["method"], r["ratio"] or "-",
                       f"{int(cb)/1e6:.2f}" if cb.isdigit() else "-",
                       r["comp_sec"] or "-", r["decomp_sec"] or "-", r["roundtrip"] or "-"))
print("\nCSV:", sys.argv[1])
PY
cat "$TXT"
echo; echo "Done. $CSV"
