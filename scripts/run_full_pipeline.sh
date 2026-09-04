#!/usr/bin/env bash
# run_full_pipeline.sh — verify the VCF corpus is complete, then submit all four
# benchmark jobs. (FASTQ model retraining now happens INSIDE benchmark_fastq.slurm
# so it runs on a compute node against the pinned OpenZL, not the login node.)
#
# Safe to re-run.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"
mkdir -p out
STAMP() { date -u +%H:%M:%S; }

echo "[$(STAMP)] === STEP 1/2: verify VCF corpus ==="
if pgrep -f 'scripts/vcf/get_corpus.sh' >/dev/null; then
  echo "[$(STAMP)] get_corpus.sh still running — waiting"
  while pgrep -f 'scripts/vcf/get_corpus.sh' >/dev/null; do sleep 30; done
fi
bash scripts/vcf/get_corpus.sh          # idempotent: fills anything missing
MAN="data/vcf/corpus/manifest.tsv"
missing=0
while IFS=$'\t' read -r a p s n; do [ -f "$p" ] || { echo "  MISSING: $p"; missing=1; }; done < "$MAN"
echo "[$(STAMP)] corpus: $(wc -l < "$MAN") files, per archetype:"
awk -F'\t' '{c[$1]++} END{for(a in c) printf "    %-16s %d\n", a, c[a]}' "$MAN" | sort
[ "$missing" = 0 ] || { echo "[$(STAMP)] ABORT: corpus incomplete"; exit 1; }

echo "[$(STAMP)] === STEP 2/2: submit benchmark jobs ==="
j1=$(sbatch --parsable batch_files/benchmark_fasta.slurm)      ; echo "  fasta     $j1"
j2=$(sbatch --parsable batch_files/benchmark_vcfzl.slurm)      ; echo "  vcfzl     $j2"
j3=$(sbatch --parsable batch_files/benchmark_fastq.slurm)      ; echo "  fastq     $j3"
j4=$(sbatch --parsable batch_files/benchmark_vcf_corpus.slurm) ; echo "  vcfcorpus $j4"
echo "[$(STAMP)] submitted: $j1 $j2 $j3 $j4"
squeue -u "$USER" -o '%.12i %.16j %.9T %.11M %R'
echo "[$(STAMP)] === DONE (jobs are queued) ==="
