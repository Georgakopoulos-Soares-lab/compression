#!/usr/bin/env bash
# test_corruption.sh — a damaged archive must fail to decompress, never decode
# to wrong output.
#
#   tests/test_corruption.sh [--flips N] [file ...]
#
# Compresses each file, then flips one random bit per trial and decompresses.
# Every trial must either fail (non-zero exit) or reproduce the input exactly (a
# flip in padding, e.g. of the FASTA tar container); wrong output with exit 0
# fails the test. The protection comes from the checksums OpenZL puts on every
# frame. With no files it uses deterministic synthetic inputs.
#
# The paper's figure (600 flips, 600 failed to decompress, 0 wrong output;
# results/corruption_test.txt) is 150 flips on one real file per format, all
# cut from the benchmark corpus:
#   civic_nightly.vcf                          the whole file
#   head -c 3000000  E003_15_coreMarks_dense.bed
#   first 10,000 reads of DRR206632.fastq      (head -n 40000)
#   bytes 370,000,001-400,000,000 of GRCm39.fa, headed '>slice'
#   tests/test_corruption.sh --flips 150 civic.vcf chromhmm.bed drr.fastq grcm39_slice.fa
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLIPS=40; FILES=()
while [ $# -gt 0 ]; do
  case "$1" in --flips) FLIPS="$2"; shift 2;; *) FILES+=("$1"); shift;; esac
done
W="$(mktemp -d "${TMPDIR:-/tmp}/corrupt.XXXXXX")"; trap 'rm -rf "$W"' EXIT
if [ ${#FILES[@]} -eq 0 ]; then
  python3 "$HERE/tests/make_sample_inputs.py" "$W"
  FILES=("$W/sample.vcf" "$W/sample.bed" "$W/sample.fastq" "$W/sample.fa")
fi
python3 - "$HERE/nyx" "$W" "$FLIPS" "${FILES[@]}" <<'PY'
import os, random, subprocess, sys
nyx, work, flips, files = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4:]
rng = random.Random(1); silent = 0
for f in files:
    arc = os.path.join(work, "a.nyx"); bad = os.path.join(work, "bad.nyx"); out = os.path.join(work, "out")
    subprocess.run([nyx, "compress", f, arc, "--force", "--quiet", "--threads", "4"], check=True)
    fmt = subprocess.run([nyx, "info", arc], capture_output=True, text=True).stdout.split("NYX ")[1].split()[0]
    data = open(arc, "rb").read(); orig = open(f, "rb").read(); res = {"failed": 0, "unaffected": 0, "WRONG": 0}
    for _ in range(flips):
        b = bytearray(data); b[rng.randrange(len(b))] ^= 1 << rng.randrange(8); open(bad, "wb").write(b)
        if os.path.exists(out): os.remove(out)
        r = subprocess.run([nyx, "decompress", bad, out, "--force", "--quiet", "--threads", "4", "--format", fmt],
                           capture_output=True, timeout=600)
        got = open(out, "rb").read() if os.path.exists(out) else None
        k = "failed" if r.returncode else ("unaffected" if got == orig else "WRONG")
        res[k] += 1
    silent += res["WRONG"]
    print(f"  {os.path.basename(f):28s} {fmt:5s} {len(data):>10} B archive, {flips} flips: "
          f"{res['failed']} failed to decompress, {res['unaffected']} unaffected, {res['WRONG']} wrong output")
print(f"\nwrong output: {silent}")
sys.exit(1 if silent else 0)
PY
