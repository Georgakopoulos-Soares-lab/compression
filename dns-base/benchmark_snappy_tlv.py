#!/usr/bin/env python3
"""Benchmark Snappy compression on raw TLV binary payloads.

Reads .tlv files (extracted GenericChunk TLV payloads) and benchmarks
Snappy compression/decompression — matching what librdkafka does in production.
"""

import glob
import os
import sys
import time

import snappy


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <tlv-directory> [iterations]")
        sys.exit(1)

    tlv_dir = sys.argv[1]
    iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    files = sorted(glob.glob(os.path.join(tlv_dir, "*.tlv")))
    if not files:
        print(f"No .tlv files found in {tlv_dir}")
        sys.exit(1)

    # Load all TLV payloads into memory
    payloads = []
    total_raw = 0
    for f in files:
        data = open(f, "rb").read()
        payloads.append(data)
        total_raw += len(data)

    print(f"Files:          {len(payloads)}")
    print(f"Total raw:      {total_raw / 1e6:.1f} MB")
    print(f"Avg chunk size: {total_raw / len(payloads) / 1e3:.1f} KB")
    print(f"Iterations:     {iterations}")
    print()

    # Compress all payloads once to measure ratio
    compressed = []
    total_compressed = 0
    for p in payloads:
        c = snappy.compress(p)
        compressed.append(c)
        total_compressed += len(c)

    ratio = total_raw / total_compressed
    print(f"Compressed:     {total_compressed / 1e6:.1f} MB")
    print(f"Ratio:          {ratio:.2f}x")
    print()

    # Benchmark compression throughput
    best_compress = float("inf")
    for i in range(iterations):
        t0 = time.monotonic()
        for p in payloads:
            snappy.compress(p)
        elapsed = time.monotonic() - t0
        best_compress = min(best_compress, elapsed)

    compress_mbps = (total_raw / 1e6) / best_compress
    print(f"Compress:       {compress_mbps:.0f} MB/s  (best of {iterations}: {best_compress:.3f}s)")

    # Benchmark decompression throughput
    best_decompress = float("inf")
    for i in range(iterations):
        t0 = time.monotonic()
        for c in compressed:
            snappy.decompress(c)
        elapsed = time.monotonic() - t0
        best_decompress = min(best_decompress, elapsed)

    decompress_mbps = (total_raw / 1e6) / best_decompress
    print(f"Decompress:     {decompress_mbps:.0f} MB/s  (best of {iterations}: {best_decompress:.3f}s)")

    # Per-chunk stats
    sizes = [len(c) for c in compressed]
    min_c = min(sizes)
    max_c = max(sizes)
    raw_sizes = [len(p) for p in payloads]
    ratios = [r / c for r, c in zip(raw_sizes, sizes)]
    print()
    print(f"Per-chunk ratio: min={min(ratios):.2f}x  max={max(ratios):.2f}x  avg={sum(ratios)/len(ratios):.2f}x")
    print(f"Per-chunk compressed: min={min_c/1e3:.1f} KB  max={max_c/1e3:.1f} KB  avg={sum(sizes)/len(sizes)/1e3:.1f} KB")


if __name__ == "__main__":
    main()
