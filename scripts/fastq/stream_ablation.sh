#!/usr/bin/env bash
# Which stream does the SPRING gap live in?
#
# Neither tool reports per-stream sizes, so measure by ablation: compress the
# same slice with one stream flattened to a constant, and attribute the
# difference to that stream. Doing it for both tools shows not just where our
# bytes go but where theirs go, which is the only way to know whether the gap is
# the sequence coder (fixable by reordering) or the quality coder (not).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$(cd "$HERE/.." && pwd)" || exit 1
export LD_LIBRARY_PATH="/opt/apps/gcc/9.4.0/lib64:/opt/apps/gcc/9.4.0/lib:${LD_LIBRARY_PATH:-}"
D=data/fastq/split; rm -rf $D; mkdir -p $D
IN="${1:-data/fastq/work/DRR206632.fastq}"
head -n 8000000 "$IN" > $D/full.fq          # 2M reads

# flat quality: same length, single symbol -> quality stream costs ~nothing
awk 'NR%4==0{gsub(/./,"I")} {print}' $D/full.fq > $D/flatq.fq
# flat headers: keep a counter so records stay distinguishable and legal
awk 'NR%4==1{printf "@r%d\n", ++i; next} {print}' $D/full.fq > $D/flath.fq
# flat sequence: same length, single base
awk 'NR%4==2{gsub(/./,"A")} {print}' $D/full.fq > $D/flats.fq

sz() { stat -c%s "$1"; }
nyx() { ./compression/openzl/nyxfqz_v2 compress compression/artifacts/fastq_models "$1" $D/o 16 4000 >/dev/null 2>&1; sz $D/o; rm -f $D/o; }
spr() { rm -rf $D/sw; mkdir -p $D/sw; thirdparty/bin/spring -c -i "$1" -o $D/o -t 16 -w $D/sw >/dev/null 2>&1; local s=$(sz $D/o); rm -rf $D/o $D/sw; echo "$s"; }

printf "%-10s %14s %14s %14s %14s\n" variant orig NYX SPRING note
for v in full flatq flath flats; do
  o=$(sz $D/$v.fq); n=$(nyx $D/$v.fq); s=$(spr $D/$v.fq)
  printf "%-10s %14s %14s %14s\n" "$v" "$o" "$n" "$s"
done
echo
echo "Stream cost = full - flat<stream>.  Negative means the flattening cost more"
echo "than it saved, which would mean that stream is already essentially free."
rm -rf $D
