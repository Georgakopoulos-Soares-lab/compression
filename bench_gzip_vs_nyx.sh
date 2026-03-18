#!/usr/bin/env bash
set -euo pipefail

cd /Users/theodorechronopoulos/Desktop/Compress/newrepo/compression

FILE="data/openZL-telemetru/nom-telegraf/part-001.jsonl"
SIZE_BYTES=$(stat -f%z "$FILE")
SIZE_MB=$(echo "scale=2; $SIZE_BYTES / 1048576" | bc)
CPUS=8

echo "=== Input: $FILE ($SIZE_MB MB) ==="
echo ""

# ── GZIP level 9 ──
echo "── GZIP-9 ──"

START=$(python3 -c "import time; print(time.time())")
gzip -9 -k -c "$FILE" > /tmp/part-001.jsonl.gz
END=$(python3 -c "import time; print(time.time())")
GZ_COMP_TIME=$(echo "$END - $START" | bc)
GZ_COMP_SIZE=$(stat -f%z /tmp/part-001.jsonl.gz)
GZ_COMP_MB=$(echo "scale=2; $GZ_COMP_SIZE / 1048576" | bc)
GZ_RATIO=$(echo "scale=2; $SIZE_BYTES / $GZ_COMP_SIZE" | bc)
GZ_COMP_SPEED=$(echo "scale=2; $SIZE_MB / $GZ_COMP_TIME" | bc)
echo "  Compress:   ${GZ_COMP_TIME}s | ${GZ_COMP_SPEED} MB/s | ${GZ_COMP_MB} MB | ${GZ_RATIO}x"

START=$(python3 -c "import time; print(time.time())")
gzip -d -k -c /tmp/part-001.jsonl.gz > /tmp/part-001.gzip.decoded.jsonl
END=$(python3 -c "import time; print(time.time())")
GZ_DEC_TIME=$(echo "$END - $START" | bc)
GZ_DEC_SPEED=$(echo "scale=2; $SIZE_MB / $GZ_DEC_TIME" | bc)
echo "  Decompress: ${GZ_DEC_TIME}s | ${GZ_DEC_SPEED} MB/s"

GZ_MD5_ORIG=$(md5 -q "$FILE")
GZ_MD5_DEC=$(md5 -q /tmp/part-001.gzip.decoded.jsonl)
[ "$GZ_MD5_ORIG" = "$GZ_MD5_DEC" ] && echo "  Round-trip:  PASS" || echo "  Round-trip:  FAIL"

rm -f /tmp/part-001.jsonl.gz /tmp/part-001.gzip.decoded.jsonl
echo ""

# ── NYX ──
echo "── NYX (lossless JSONL, train-threads=$CPUS, compress-jobs=$CPUS) ──"

NYX_OUT="${FILE}.zljsonl"
rm -f "$NYX_OUT"

START=$(python3 -c "import time; print(time.time())")
python3 -c "
import sys
sys.argv = ['nyx', 'compress', '$FILE', '--train', '--train-threads', '$CPUS', '--compress-jobs', '$CPUS', '-v']
from nyx.nyx.cli import main
main()
"
END=$(python3 -c "import time; print(time.time())")
NYX_COMP_TIME=$(echo "$END - $START" | bc)
NYX_COMP_SIZE=$(stat -f%z "$NYX_OUT")
NYX_COMP_MB=$(echo "scale=2; $NYX_COMP_SIZE / 1048576" | bc)
NYX_RATIO=$(echo "scale=2; $SIZE_BYTES / $NYX_COMP_SIZE" | bc)
NYX_COMP_SPEED=$(echo "scale=2; $SIZE_MB / $NYX_COMP_TIME" | bc)
echo "  Compress:   ${NYX_COMP_TIME}s | ${NYX_COMP_SPEED} MB/s | ${NYX_COMP_MB} MB | ${NYX_RATIO}x"

# Decompress
NYX_DEC_OUT="data/openZL-telemetru/nom-telegraf/part-001.decoded.jsonl"

START=$(python3 -c "import time; print(time.time())")
python3 -c "
import sys
sys.argv = ['nyx', 'decompress', '$NYX_OUT', '-v']
from nyx.nyx.cli import main
main()
"
END=$(python3 -c "import time; print(time.time())")
NYX_DEC_TIME=$(echo "$END - $START" | bc)
NYX_DEC_SPEED=$(echo "scale=2; $SIZE_MB / $NYX_DEC_TIME" | bc)
echo "  Decompress: ${NYX_DEC_TIME}s | ${NYX_DEC_SPEED} MB/s"

NYX_MD5_DEC=$(md5 -q "$NYX_DEC_OUT")
[ "$GZ_MD5_ORIG" = "$NYX_MD5_DEC" ] && echo "  Round-trip:  PASS" || echo "  Round-trip:  FAIL"

echo ""
echo "=== SUMMARY ==="
printf "%-12s %8s %10s %10s %10s %10s\n" "" "Ratio" "CompMB/s" "DecMB/s" "CompTime" "DecTime"
printf "%-12s %8sx %10s %10s %10ss %10ss\n" "GZIP-9" "$GZ_RATIO" "$GZ_COMP_SPEED" "$GZ_DEC_SPEED" "$GZ_COMP_TIME" "$GZ_DEC_TIME"
printf "%-12s %8sx %10s %10s %10ss %10ss\n" "NYX" "$NYX_RATIO" "$NYX_COMP_SPEED" "$NYX_DEC_SPEED" "$NYX_COMP_TIME" "$NYX_DEC_TIME"
