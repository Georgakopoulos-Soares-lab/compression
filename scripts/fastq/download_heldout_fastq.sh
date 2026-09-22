#!/usr/bin/env bash
# download_heldout_fastq.sh — the FASTQ model-training corpus: the first 200,000
# reads of eight public runs, none in the benchmark and none from a benchmark
# study (see train_fastq_models.sh for why each was chosen).
#
# Uses NCBI's SRA toolkit (fastq-dump -X): ENA's file server refused connections
# from the TACC nodes in September 2026. The defline reproduces ENA's header
# layout, "@<run>.<spot> <original read name>", which is what the benchmark runs
# (downloaded from ENA) carry, so the header models see the same shape. Only
# runs that kept their original read names were chosen.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${1:-$ROOT/data/fastq/heldout}"
FQD="${FASTQ_DUMP:-fastq-dump}"
READS="${HELDOUT_READS:-200000}"
mkdir -p "$OUT"
RUNS="A:ERR12161649 A:ERR11138821 A:ERR13081220 A:ERR12246909
      B:ERR11235414 B:ERR11217152
      C:ERR10216234 C:ERR11844657 D:ERR12664699 D:ERR11175705"
for s in $RUNS; do
  k=${s%%:*}; r=${s#*:}; dst="$OUT/${k}_$r.fastq.gz"
  [ -s "$dst" ] && { echo "have $dst"; continue; }
  t="$OUT/tmp_$r"; mkdir -p "$t"
  "$FQD" -X "$READS" --split-files --defline-seq '@$ac.$si $sn' --defline-qual '+' -O "$t" "$r" >/dev/null
  f=$(ls "$t"/*_1.fastq "$t/$r.fastq" 2>/dev/null | head -1)
  gzip -c "$f" > "$dst"; rm -rf "$t"; echo "got $dst"
done
