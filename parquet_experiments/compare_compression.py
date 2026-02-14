#!/usr/bin/env python3
"""Compare Parquet compression codecs and levels on a generated dataset.

Loads the uncompressed variant of a dataset, re-writes it with various
compression codecs and levels, and measures:
  - File size on disk
  - Compression ratio (vs uncompressed)
  - Write throughput (MB/s based on uncompressed size)
  - Read throughput (MB/s based on uncompressed size)

This establishes the baseline that OpenZL needs to beat: how well do
Parquet's existing codec options compress each dataset type?

Usage:
  python compare_compression.py numeric_heavy
  python compare_compression.py string_heavy
  python compare_compression.py mixed_type
  python compare_compression.py ml_features
  python compare_compression.py all          # run all datasets
"""

import argparse
import sys
import tempfile
import time
from pathlib import Path

import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parent / "data"

# (label, write_table kwargs)
# We test a range of codecs and levels to see the full trade-off landscape.
VARIANTS = [
    ("uncompressed", {"compression": "NONE"}),
    ("snappy",       {"compression": "snappy"}),
    ("gzip-1",       {"compression": "gzip", "compression_level": 1}),
    ("gzip-6",       {"compression": "gzip", "compression_level": 6}),
    ("gzip-9",       {"compression": "gzip", "compression_level": 9}),
    ("zstd-1",       {"compression": "zstd", "compression_level": 1}),
    ("zstd-3",       {"compression": "zstd", "compression_level": 3}),
    ("zstd-9",       {"compression": "zstd", "compression_level": 9}),
    ("zstd-19",      {"compression": "zstd", "compression_level": 19}),
]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def fmt_bytes(n: int | float) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


# ─────────────────────────────────────────────────────────────────────────────
# Comparison logic
# ─────────────────────────────────────────────────────────────────────────────

def compare_dataset(dataset_name: str) -> None:
    """Run compression comparison for a single dataset."""
    src_path = DATA_DIR / f"{dataset_name}_none.parquet"
    if not src_path.exists():
        print(f"Error: uncompressed file not found: {src_path}", file=sys.stderr)
        print("  Run generate_samples.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"\n{'═' * 90}")
    print(f" Compression Comparison: {dataset_name}")
    print(f"{'═' * 90}")

    # Load the full table into memory (so I/O doesn't vary between runs)
    print(f"  Loading {src_path.name}...")
    load_start = time.monotonic()
    table = pq.read_table(str(src_path))
    load_time = time.monotonic() - load_start
    print(f"  Loaded {table.num_rows:,} rows × {table.num_columns} columns "
          f"in {load_time:.2f}s")

    base_size = src_path.stat().st_size

    results = []

    for label, kwargs in VARIANTS:
        # Write to a temp file, measure size and time, then delete
        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            # Measure write
            write_start = time.monotonic()
            pq.write_table(table, str(tmp_path), **kwargs)
            write_time = time.monotonic() - write_start

            file_size = tmp_path.stat().st_size

            # Measure read
            read_start = time.monotonic()
            _ = pq.read_table(str(tmp_path))
            read_time = time.monotonic() - read_start
        finally:
            tmp_path.unlink(missing_ok=True)

        ratio = base_size / file_size if file_size > 0 else 0
        write_mbps = (base_size / (1024 * 1024)) / write_time if write_time > 0 else 0
        read_mbps = (base_size / (1024 * 1024)) / read_time if read_time > 0 else 0

        results.append({
            "label": label,
            "file_size": file_size,
            "ratio": ratio,
            "write_time": write_time,
            "write_mbps": write_mbps,
            "read_time": read_time,
            "read_mbps": read_mbps,
        })

    # ── Print results table ──
    print()
    hdr = (f"  {'Codec':<15s}  {'File Size':>12s}  {'Ratio':>7s}  "
           f"{'Write':>8s}  {'Write MB/s':>11s}  "
           f"{'Read':>8s}  {'Read MB/s':>10s}")
    print(hdr)
    print(f"  {'─' * (len(hdr) - 2)}")

    for r in results:
        size_str = fmt_bytes(r["file_size"])
        ratio_str = f"{r['ratio']:.2f}x"
        write_str = f"{r['write_time']:.2f}s"
        wmbps_str = f"{r['write_mbps']:.1f}"
        read_str = f"{r['read_time']:.2f}s"
        rmbps_str = f"{r['read_mbps']:.1f}"

        print(f"  {r['label']:<15s}  {size_str:>12s}  {ratio_str:>7s}  "
              f"{write_str:>8s}  {wmbps_str:>11s}  "
              f"{read_str:>8s}  {rmbps_str:>10s}")

    # ── Key observations ──
    best = min(results, key=lambda r: r["file_size"])
    fastest_write = max(results[1:], key=lambda r: r["write_mbps"])  # skip uncompressed
    fastest_read = max(results[1:], key=lambda r: r["read_mbps"])

    print()
    print(f"  Best ratio:       {best['label']} "
          f"({fmt_bytes(best['file_size'])}, {best['ratio']:.2f}x)")
    print(f"  Fastest write:    {fastest_write['label']} "
          f"({fastest_write['write_mbps']:.1f} MB/s)")
    print(f"  Fastest read:     {fastest_read['label']} "
          f"({fastest_read['read_mbps']:.1f} MB/s)")

    # Highlight the target for OpenZL
    zstd3 = next((r for r in results if r["label"] == "zstd-3"), None)
    if zstd3:
        print()
        print(f"  ┌─────────────────────────────────────────────────────┐")
        print(f"  │ OpenZL target: beat zstd-3 ({zstd3['ratio']:.2f}x, "
              f"{fmt_bytes(zstd3['file_size'])})       │")
        print(f"  │ while maintaining comparable speed.                 │")
        print(f"  └─────────────────────────────────────────────────────┘")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

ALL_DATASETS = ["numeric_heavy", "string_heavy", "mixed_type", "ml_features"]


def main():
    parser = argparse.ArgumentParser(
        description="Compare Parquet compression codecs and levels.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Available datasets:
  numeric_heavy   IoT sensor readings (3M rows)
  string_heavy    Application event logs (1M rows)
  mixed_type      Business order analytics (2M rows)
  ml_features     ML feature store (500K rows, 50 features)
  all             Run comparison on all datasets

Examples:
  python compare_compression.py numeric_heavy
  python compare_compression.py all
""",
    )
    parser.add_argument(
        "dataset",
        help="Dataset name (or 'all' to run all).",
    )
    args = parser.parse_args()

    if args.dataset == "all":
        for ds in ALL_DATASETS:
            compare_dataset(ds)
    elif args.dataset in ALL_DATASETS:
        compare_dataset(args.dataset)
    else:
        print(f"Error: unknown dataset '{args.dataset}'", file=sys.stderr)
        print(f"Available: {', '.join(ALL_DATASETS)}, all", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
