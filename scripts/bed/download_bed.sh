#!/usr/bin/env bash
# A real BED corpus, sampled across dialects, tissues and sources.
#
# Two earlier mistakes this script exists to avoid:
#
# 1. The original version downloaded UCSC's rmsk.txt, a 17-column MySQL table
#    dump that no BED tool accepts. Everything measured against it was a
#    measurement of a generic text codec on a non-BED file.
# 2. The replacement was six files, four of them from one project. Six files is
#    the same "you only tested a little" objection that a Pareto count invites,
#    and BED files are small and free -- Roadmap alone publishes ~4,100 of them
#    -- so there is no excuse for a thin corpus here the way there is for FASTQ,
#    where one library is 28 GB.
#
# Files are chosen deterministically (every Kth entry of the sorted listing) so
# the corpus is reproducible from the paper, and span the widths that occur in
# practice: 4 (ChromHMM segments), 6 (cCRE, TFBS clusters), 9 (broadPeak,
# ChromHMM dense), 10 (narrowPeak), 15 (gappedPeak).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA="${DATA_DIR:-$HERE/data/bed}"
JOBS="${JOBS:-4}"
mkdir -p "$DATA"

R=https://egg2.wustl.edu/roadmap/data/byFileType

# Single named files: the large ones, and the non-Roadmap sources that keep the
# corpus from being one project's output.
SINGLES=(
  "screen_ccre_GRCh38.bed|https://downloads.wenglab.org/Registry-V4/GRCh38-cCREs.bed"
  "encode_tfbs_clustered_hg38.bed|https://hgdownload.soe.ucsc.edu/goldenPath/hg38/encRegTfbsClustered/encRegTfbsClusteredWithCells.hg38.bed.gz"
  "fantom_cage_peaks_hg38.bed|https://fantom.gsc.riken.jp/5/datafiles/reprocessed/hg38_latest/extra/CAGE_peaks/hg38_fair+new_CAGE_peaks_phase1and2.bed.gz"
)

# dir | extension | how many to take | local prefix
# Not named GROUPS: that is a bash built-in array of the caller's group IDs,
# assignment to it is ignored, and the loop silently iterates over gids.
BED_GROUPS=(
  "$R/peaks/consolidated/narrowPeak/|.narrowPeak.gz|8|np"
  "$R/peaks/consolidated/broadPeak/|.broadPeak.gz|6|bp"
  "$R/peaks/consolidated/gappedPeak/|.gappedPeak.gz|5|gp"
  "$R/chromhmmSegmentations/ChmmModels/coreMarks/jointModel/final/|_dense.bed.gz|4|cd"
  "$R/chromhmmSegmentations/ChmmModels/coreMarks/jointModel/final/|_segments.bed.gz|4|cs"
)

fetch() {
  local name="${1%%|*}"
  local url="${1#*|}"
  local out="$DATA/$name"
  [ -s "$out" ] && { echo "have  $name"; return 0; }
  if [[ "$url" == *.gz ]]; then
    curl -sL --retry 3 --max-time 3600 "$url" | gunzip -c > "$out" \
      || { rm -f "$out"; echo "FAIL  $name"; return 1; }
  else
    curl -sL --retry 3 --max-time 3600 -o "$out" "$url" \
      || { rm -f "$out"; echo "FAIL  $name"; return 1; }
  fi
  [ -s "$out" ] || { rm -f "$out"; echo "FAIL  $name (empty)"; return 1; }
  echo "got   $name  $(stat -c%s "$out") bytes"
}

WANT=()
for s in "${SINGLES[@]}"; do WANT+=("$s"); done
for g in "${BED_GROUPS[@]}"; do
  IFS='|' read -r dir ext n pre <<< "$g"
  mapfile -t all < <(curl -sL --max-time 120 "$dir" \
      | grep -oE "href=\"[^\"]*${ext//./\\.}\"" | sed 's/href="//;s/"$//' \
      | grep -v '^/' | sort -u)
  [ "${#all[@]}" -gt 0 ] || { echo "WARN  no listing for $dir"; continue; }
  step=$(( ${#all[@]} / n )); [ "$step" -lt 1 ] && step=1
  for ((i=0, k=0; i < ${#all[@]} && k < n; i+=step, k++)); do
    f="${all[$i]}"
    WANT+=("${pre}_${f%.gz}|${dir}${f}")
  done
done

printf '%s fetching %d files with %s parallel connections\n' "--" "${#WANT[@]}" "$JOBS"
i=0
for s in "${WANT[@]}"; do
  fetch "$s" &
  i=$((i+1)); [ $((i % JOBS)) -eq 0 ] && wait
done
wait

echo
printf '%-44s %12s %6s %s\n' file bytes cols shape
total=0; n=0
for f in "$DATA"/*.bed "$DATA"/*Peak; do
  [ -s "$f" ] || continue
  sz=$(stat -c%s "$f"); total=$((total+sz)); n=$((n+1))
  read -r c shape < <(awk -F'\t' '!/^(track|browser|#)/ && NF {h[NF]++} NR>200000{exit}
      END{m=0;b=0;u=0; for(k in h){u++; if(h[k]>m){m=h[k];b=k}}
          printf "%d %s\n", b, (u>1 ? "ragged" : "uniform")}' "$f")
  printf '%-44s %12d %6d %s\n' "$(basename "$f")" "$sz" "$c" "$shape"
done
printf '\n%d files, %.2f GB\n' "$n" "$(awk -v t=$total 'BEGIN{print t/1e9}')"
