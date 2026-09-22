#!/usr/bin/env bash
# download_fastq_corpus.sh — fetch the six Illumina runs the FASTQ table reports.
#
# The manuscript's FASTQ table was measured on six runs on a different machine at
# a different thread count. To report them on the same harness as everything else
# they have to be re-measured here, which means having them here. URLs come from
# the ENA file report so accession-path rules do not have to be guessed.
#
#   scripts/fastq/download_fastq_corpus.sh [accession ...]
#
# Downloads are resumable (wget -c); an accession already present is skipped.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${FASTQ_DATA:-$(cd "$HERE/.." && pwd)/data/fastq}"
mkdir -p "$DATA"

ACCS=("$@")
[ ${#ACCS[@]} -eq 0 ] && ACCS=(DRR206632 ERR4186945 ERR9539079 ERR9539093)

for acc in "${ACCS[@]}"; do
  report=$(curl -s "https://www.ebi.ac.uk/ena/portal/api/filereport?accession=$acc&result=read_run&fields=fastq_ftp&format=tsv" | tail -n +2)
  urls=$(echo "$report" | cut -f2 | tr ';' '\n' | grep -v '^$')
  [ -n "$urls" ] || { echo "!! no ENA record for $acc" >&2; continue; }
  # Paired runs list _1 and _2; the table reports the _1 file, so take the first.
  url=$(echo "$urls" | head -1)
  out="$DATA/$(basename "$url")"
  if [ -s "$out" ]; then echo "have $(basename "$out")"; continue; fi
  echo "fetching $(basename "$out")"
  wget -q -c -O "$out" "https://$url" || { echo "!! failed: $acc" >&2; rm -f "$out"; }
done
ls -la "$DATA"/*.fastq.gz 2>/dev/null
