#!/usr/bin/env bash
# download_fasta_train.sh — the FASTA model-training corpus (see train_fasta_model.sh).
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/data/fasta_train_heldout}"; mkdir -p "$OUT"
B=https://ftp.ncbi.nlm.nih.gov/genomes/all
for p in GCF/000/005/845/GCF_000005845.2_ASM584v2 GCF/000/146/045/GCF_000146045.2_R64 \
         GCF/000/001/735/GCF_000001735.4_TAIR10.1 GCF/003/339/765/GCF_003339765.1_Mmul_10 \
         GCF/015/227/675/GCF_015227675.2_mRatBN7.2 \
         GCF/904/849/725/GCF_904849725.1_MorexV3_pseudomolecules_assembly; do
  f=$(basename "$p")_genomic.fna
  [ -s "$OUT/$f" ] && { echo "have $f"; continue; }
  curl -sS --retry 3 "$B/$p/$f.gz" | gzip -dc > "$OUT/$f"; echo "got $f"
done
