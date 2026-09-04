#!/usr/bin/env bash
# verify_and_queue_fastq.sh — wait for the nyxfqz_v2 build, prove the new
# streaming/shared-compressor compress path is still byte-exact on both the
# clustered and non-clustered routes, then submit benchmark_fastq.slurm.
#
# Run detached:  setsid nohup bash scripts/verify_and_queue_fastq.sh > out/verify_fastq.log 2>&1 &
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FQ="$HERE/fastq"
NYX="$FQ/openzl/nyxfqz_v2"
MODEL="$HERE/artifacts/fastq_models/fastq_fixed_cluster.zc"   # any existing model works for the codec test
mkdir -p "$HERE/out"
S() { date -u +%H:%M:%S; }

echo "[$(S)] waiting for nyxfqz_v2 build..."
while pgrep -f 'nyxfqz_v2.make' >/dev/null; do sleep 20; done
[ -x "$NYX" ] || { echo "[$(S)] FATAL: no nyxfqz_v2 binary"; exit 1; }
echo "[$(S)] build done: $(readlink -f "$NYX")"

T="$(mktemp -d "${TMPDIR:-/tmp}/nyxverify.XXXXXX")"
trap 'rm -rf "$T"' EXIT
fail=0

# Two datasets x two cluster modes = both archive paths (NYXZCHK1 and NYXZCHK3).
for ds in SRR8899104 ERR9539079; do
  src="$FQ/data/fastq/${ds}.fastq.gz"
  [ -f "$src" ] || { echo "[$(S)] skip (missing) $ds"; continue; }
  gzip -dc "$src" | head -n 4000000 > "$T/$ds.fastq"
  for mode in 0 1; do
    NYX_MEM_LOG=1 NYX_MAX_MEM_MB=8000 NYX_CLUSTER=$( [ "$mode" = 1 ] && echo 1 || echo 0 ) \
      /usr/bin/time -v -o "$T/c.time" \
      "$NYX" compress "$MODEL" "$T/$ds.fastq" "$T/$ds.$mode.nyxz" 32 500 > "$T/$ds.$mode.err" 2>&1
    /usr/bin/time -v -o "$T/d.time" \
      "$NYX" decompress "$T/$ds.$mode.nyxz" "$T/$ds.$mode.back" >>"$T/$ds.$mode.err" 2>&1
    if cmp -s "$T/$ds.fastq" "$T/$ds.$mode.back"; then
      raw=$(stat -c%s "$T/$ds.fastq"); cz=$(stat -c%s "$T/$ds.$mode.nyxz")
      cpk=$(awk '/Maximum resident/{printf "%.2f", $NF/1024/1024}' "$T/c.time")
      dpk=$(awk '/Maximum resident/{printf "%.2f", $NF/1024/1024}' "$T/d.time")
      cs=$(awk -F'[ :]' '/Elapsed .wall/{print $(NF-1)"m"$NF}' "$T/c.time")
      ds_=$(awk -F'[ :]' '/Elapsed .wall/{print $(NF-1)"m"$NF}' "$T/d.time")
      echo "[$(S)] OK   $ds cluster=$mode  ratio=$(awk -v a=$raw -v b=$cz 'BEGIN{printf "%.2f",a/b}')  cpeak=${cpk}GB dpeak=${dpk}GB  ctime=$cs dtime=$ds_  BYTE-EXACT"
    else
      echo "[$(S)] FAIL $ds cluster=$mode  round-trip MISMATCH"; tail -5 "$T/$ds.$mode.err"; fail=1
    fi
    rm -f "$T/$ds.$mode.nyxz" "$T/$ds.$mode.back"
  done
  rm -f "$T/$ds.fastq"
done

if [ "$fail" != 0 ]; then
  echo "[$(S)] NOT queueing benchmark_fastq.slurm - the codec changes broke losslessness"
  exit 1
fi
echo "[$(S)] all round trips byte-exact -> submitting benchmark_fastq.slurm"
cd "$HERE" && sbatch --parsable batch_files/benchmark_fastq.slurm
squeue -u "$USER" -o '%.12i %.16j %.9T %.11L'
echo "[$(S)] done"
