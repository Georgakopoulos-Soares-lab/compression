#!/bin/bash
#SBATCH --job-name=bench_fastq
#SBATCH --partition=pvc
#SBATCH --account=BCS25105
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=48
#SBATCH --time=48:00:00
#SBATCH --output=results/bench_fastq_%j.out
#SBATCH --error=results/bench_fastq_%j.err

# =============================================================================
# FASTQ compression benchmark — NYX (tools/nyx/nyxfqz_v2.cpp) vs baselines.
#
# Baseline arguments are IDENTICAL to batch_files/benchmark_fasta.slurm and
# batch_files/benchmark_vcfzl.slurm (whole file, highest quality):
#   gzip  -9
#   pigz  -9 -pT
#   zstd  -19 --long=27 -TT
#   7z    a -mx=9 -mmt=T
#   xz    -9e -TT --block-size=192MiB
# plus SPRING (FASTQ-specific state of the art) and NYX.
#
# NYX memory is clamped to a workstation budget via NYX_MAX_MEM_MB (see
# docs/fastq-codec-notes.md) so the reported peak is what a normal machine can run.
#
# The FASTQ codec is a self-contained sub-project with its own OpenZL fork; this
# job builds it (scripts/fastq/build_nyxfqz.sh) and drives scripts/fastq/paper_bench.sh.
# Results are copied to results/benchmark_fastq_<jobid>.txt.
#
# Submit:   sbatch batch_files/benchmark_fastq.slurm
# Override: NYX_MAX_MEM_MB=8000 sbatch ...        METHODS="xz spring ours" sbatch ...
# =============================================================================
set -uo pipefail

HERE="/scratch/11579/alexmargaris/compression"
cd "$HERE" || exit 1
mkdir -p "$HERE/results" "$HERE/out/fastq_bench"

export T="${T:-48}"
export FQBENCH="${FQBENCH:-$HERE/out/fastq_bench}"   # NOT $WORK: Stampede3 exports WORK=/work2/... which we cannot write
export NYX_MAX_MEM_MB="${NYX_MAX_MEM_MB:-8000}"    # total RAM ceiling; archive now streamed + compressor shared, so peak tracks this
export NYX_MEM_LOG=1
export NYX_MODEL_DIR="$HERE/artifacts/fastq_models"
export PATH="/home1/apps/cmake/4.1.1/bin:$PATH"

echo "== node $(hostname)  cores $(nproc)  mem $(free -g | awk '/Mem:/{print $2}')G  T=$T  NYX_MAX_MEM_MB=$NYX_MAX_MEM_MB =="
echo "== start $(date -u) =="

# ---- one OpenZL checkout for every format ----------------------------------
# scripts/get_openzl.sh pins the commit (0.2.5, d262127) and scripts/patch_openzl.sh
# applies the wide-CSV limit patch; build_nyxfqz.sh runs the patcher and stages
# tools/nyx/ into the OpenZL tree before building.
bash scripts/get_openzl.sh >/dev/null 2>&1 || true
bash scripts/fastq/build_nyxfqz.sh || { echo "FATAL: nyxfqz_v2 build failed"; exit 1; }
echo "== OpenZL: $(cd openzl && git rev-parse --short HEAD) =="

# ---- retrain the shipped Illumina model on THIS OpenZL ----------------------
# Falls back to the existing model if the retrain fails.
if bash scripts/fastq/retrain_paper_models.sh; then
  echo "== FASTQ model retrained on $(cd openzl && git rev-parse --short HEAD) =="
else
  echo "WARN: retrain failed -- benchmarking with the existing model"
fi

export PAPER_BENCH_TABLE="$HERE/results/benchmark_fastq_${SLURM_JOB_ID:-local}.txt"
rm -f "$PAPER_BENCH_TABLE"
bash scripts/fastq/paper_bench.sh

echo "== end $(date -u) =="
cat "$PAPER_BENCH_TABLE"
