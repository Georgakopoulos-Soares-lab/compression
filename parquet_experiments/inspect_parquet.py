#!/usr/bin/env python3
"""Comprehensive Parquet file structural inspection tool.

Provides a detailed analysis of a Parquet file's internal structure:
  - File-level metadata (size, row groups, created-by)
  - Schema with physical and logical types
  - Per-row-group, per-column-chunk breakdown:
      encodings, compression codec, compressed/uncompressed sizes, statistics
  - Summary table: encoding ratio vs codec ratio per column

This tool is designed to answer the key question for OpenZL integration:
  "What data reaches the compression layer, and how much does the codec
   actually contribute after Parquet's built-in encodings?"

The key metrics:
  - Encoding ratio  = raw_estimate / uncompressed_size
    How much Parquet's encodings (RLE, delta, dictionary) help BEFORE compression.
  - Codec ratio     = uncompressed_size / compressed_size
    How much the compression codec (snappy/gzip/zstd) helps on the encoded data.
  - Total ratio     = raw_estimate / compressed_size

The "Codec ratio" is what OpenZL would need to beat or improve upon.

Usage:
  python inspect_parquet.py <file.parquet>
  python inspect_parquet.py <file.parquet> --detail   # per-row-group breakdown
"""

import argparse
import sys
from pathlib import Path

import pyarrow.parquet as pq


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def fmt_bytes(n: int | float) -> str:
    """Format byte count as human-readable string."""
    if n < 0:
        return f"-{fmt_bytes(-n)}"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def fmt_ratio(a: float, b: float) -> str:
    """Format ratio a/b as 'X.XXx', handling division by zero."""
    if b <= 0:
        return "N/A"
    return f"{a / b:.2f}x"


# Byte size of Parquet physical types (for estimating raw data size).
# BYTE_ARRAY and FIXED_LEN_BYTE_ARRAY are variable — we skip the raw estimate.
PHYSICAL_TYPE_BYTES = {
    "BOOLEAN": 0.125,
    "INT32": 4,
    "INT64": 8,
    "INT96": 12,
    "FLOAT": 4,
    "DOUBLE": 8,
}


# ─────────────────────────────────────────────────────────────────────────────
# Inspection logic
# ─────────────────────────────────────────────────────────────────────────────

def inspect_file(filepath: str, show_detail: bool = False) -> None:
    """Print a comprehensive structural analysis of a Parquet file."""
    path = Path(filepath)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    pf = pq.ParquetFile(str(path))
    meta = pf.metadata
    schema = pf.schema_arrow
    file_size = path.stat().st_size

    # ── File summary ──
    print(f"\n{'═' * 78}")
    print(f" Parquet File Analysis: {path.name}")
    print(f"{'═' * 78}")
    print(f"  Path:        {path}")
    print(f"  File size:   {fmt_bytes(file_size)} ({file_size:,} bytes)")
    print(f"  Row groups:  {meta.num_row_groups}")
    print(f"  Total rows:  {meta.num_rows:,}")
    print(f"  Columns:     {meta.num_columns}")
    print(f"  Created by:  {meta.created_by}")
    fmt_ver = meta.format_version
    print(f"  Format ver:  {fmt_ver}")

    # ── Schema ──
    print(f"\n{'─' * 78}")
    print(f" Schema ({meta.num_columns} columns)")
    print(f"{'─' * 78}")
    max_name_len = max(len(schema.field(i).name) for i in range(len(schema)))
    for i in range(len(schema)):
        field = schema.field(i)
        nullable = "nullable" if field.nullable else "required"
        print(f"  {field.name:<{max_name_len}s}  {str(field.type):<20s}  ({nullable})")

    # ── Accumulate per-column stats across row groups ──
    # Keyed by column index
    col_totals = {}
    for col_idx in range(meta.num_columns):
        col_totals[col_idx] = {
            "uncompressed": 0,
            "compressed": 0,
            "num_values": 0,
            "null_count": 0,
            "has_stats": True,
            "distinct_count": 0,
            "min_val": None,
            "max_val": None,
            "encodings": set(),
            "compression": None,
            "physical_type": None,
        }

    for rg_idx in range(meta.num_row_groups):
        rg = meta.row_group(rg_idx)

        if show_detail:
            print(f"\n{'─' * 78}")
            print(f" Row Group {rg_idx} — {rg.num_rows:,} rows, "
                  f"total_byte_size={fmt_bytes(rg.total_byte_size)}")
            print(f"{'─' * 78}")

        for col_idx in range(rg.num_columns):
            col = rg.column(col_idx)
            ct = col_totals[col_idx]

            ct["uncompressed"] += col.total_uncompressed_size
            ct["compressed"] += col.total_compressed_size
            ct["num_values"] += col.num_values
            ct["physical_type"] = col.physical_type
            ct["compression"] = col.compression

            # Accumulate encodings
            if col.encodings:
                for enc in col.encodings:
                    ct["encodings"].add(enc)

            # Statistics
            if col.is_stats_set:
                stats = col.statistics
                if stats.null_count is not None:
                    ct["null_count"] += stats.null_count
                if stats.distinct_count is not None:
                    ct["distinct_count"] += stats.distinct_count
                # Track global min/max across row groups
                if stats.has_min_max:
                    if ct["min_val"] is None or stats.min < ct["min_val"]:
                        ct["min_val"] = stats.min
                    if ct["max_val"] is None or stats.max > ct["max_val"]:
                        ct["max_val"] = stats.max
            else:
                ct["has_stats"] = False

            if show_detail:
                col_name = schema.field(col_idx).name
                uncomp = col.total_uncompressed_size
                comp = col.total_compressed_size
                enc_str = ",".join(str(e) for e in col.encodings) if col.encodings else "?"
                print(f"  {col_name:<{max_name_len}s}  "
                      f"enc=[{enc_str}]  "
                      f"codec={col.compression}  "
                      f"uncomp={fmt_bytes(uncomp)}  "
                      f"comp={fmt_bytes(comp)}  "
                      f"ratio={fmt_ratio(uncomp, comp)}")

    # ── Column summary table ──
    print(f"\n{'═' * 78}")
    print(f" Column Compression Analysis (aggregated across all row groups)")
    print(f"{'═' * 78}")
    print()

    # Table header
    hdr_name = "Column"
    hdr_type = "Type"
    hdr_raw = "Raw Est."
    hdr_enc = "Encoded"
    hdr_comp = "Compressed"
    hdr_enc_r = "Enc.Ratio"
    hdr_cod_r = "Codec Rat."
    hdr_tot_r = "Total Rat."
    hdr_nulls = "Nulls"
    hdr_encs = "Encodings"

    name_w = max(max_name_len, len(hdr_name))
    print(f"  {hdr_name:<{name_w}s}  {hdr_type:<8s}  {hdr_raw:>10s}  "
          f"{hdr_enc:>10s}  {hdr_comp:>10s}  "
          f"{hdr_enc_r:>9s}  {hdr_cod_r:>10s}  {hdr_tot_r:>10s}  "
          f"{hdr_nulls:>10s}  {hdr_encs}")
    print(f"  {'─' * (name_w + 8 + 10*5 + 9 + 10 + 40)}")

    grand_raw_est = 0
    grand_uncompressed = 0
    grand_compressed = 0
    grand_nulls = 0

    for col_idx in range(meta.num_columns):
        ct = col_totals[col_idx]
        col_name = schema.field(col_idx).name
        phys_type = str(ct["physical_type"])
        uncomp = ct["uncompressed"]
        comp = ct["compressed"]
        null_count = ct["null_count"]
        enc_set = ct["encodings"]

        grand_uncompressed += uncomp
        grand_compressed += comp
        grand_nulls += null_count

        # Estimate raw data size from physical type
        type_size = PHYSICAL_TYPE_BYTES.get(phys_type)
        if type_size is not None:
            raw_est = int(ct["num_values"] * type_size)
            grand_raw_est += raw_est
            raw_str = fmt_bytes(raw_est)
            enc_ratio = fmt_ratio(raw_est, uncomp)
            total_ratio = fmt_ratio(raw_est, comp)
        else:
            raw_str = "variable"
            enc_ratio = "N/A"
            total_ratio = "N/A"

        codec_ratio = fmt_ratio(uncomp, comp)
        enc_str = ",".join(sorted(str(e) for e in enc_set)) if enc_set else "?"

        print(f"  {col_name:<{name_w}s}  {phys_type:<8s}  {raw_str:>10s}  "
              f"{fmt_bytes(uncomp):>10s}  {fmt_bytes(comp):>10s}  "
              f"{enc_ratio:>9s}  {codec_ratio:>10s}  {total_ratio:>10s}  "
              f"{null_count:>10,}  {enc_str}")

    # ── Grand totals ──
    print(f"\n{'─' * 78}")
    print(f" Summary")
    print(f"{'─' * 78}")
    if grand_raw_est > 0:
        print(f"  Raw estimate (typed columns): {fmt_bytes(grand_raw_est)}")
    print(f"  Total encoded (uncompressed):  {fmt_bytes(grand_uncompressed)}")
    print(f"  Total compressed:              {fmt_bytes(grand_compressed)}")
    print(f"  File size on disk:             {fmt_bytes(file_size)}")
    print(f"  Metadata overhead:             "
          f"{fmt_bytes(file_size - grand_compressed)}")
    print()
    print(f"  Overall codec ratio:  {fmt_ratio(grand_uncompressed, grand_compressed)}")
    if grand_raw_est > 0:
        print(f"  Overall total ratio:  {fmt_ratio(grand_raw_est, grand_compressed)}")
    print(f"  Total null values:    {grand_nulls:,}")

    # ── Column statistics (min/max) ──
    print(f"\n{'─' * 78}")
    print(f" Column Statistics")
    print(f"{'─' * 78}")
    for col_idx in range(meta.num_columns):
        ct = col_totals[col_idx]
        col_name = schema.field(col_idx).name
        if ct["has_stats"] and ct["min_val"] is not None:
            min_v = ct["min_val"]
            max_v = ct["max_val"]
            # Truncate long string values for readability
            min_s = str(min_v)[:40]
            max_s = str(max_v)[:40]
            print(f"  {col_name:<{name_w}s}  min={min_s}  max={max_s}")
        else:
            print(f"  {col_name:<{name_w}s}  (no statistics)")

    print(f"\n{'═' * 78}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Inspect a Parquet file's internal structure and compression.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python inspect_parquet.py data/numeric_heavy_zstd.parquet
  python inspect_parquet.py data/numeric_heavy_none.parquet --detail
""",
    )
    parser.add_argument("file", help="Path to Parquet file to inspect.")
    parser.add_argument(
        "--detail", action="store_true",
        help="Show per-row-group, per-column breakdown.",
    )
    args = parser.parse_args()
    inspect_file(args.file, show_detail=args.detail)


if __name__ == "__main__":
    main()
