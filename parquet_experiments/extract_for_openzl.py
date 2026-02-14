#!/usr/bin/env python3
"""Extract raw column data from Parquet files into PBL binary format for OpenZL.

For each dataset, this script:
  1. Reads the uncompressed (_none.parquet) Parquet file
  2. Identifies all numeric columns (skips string columns)
  3. Extracts raw column values as contiguous little-endian numpy arrays
  4. Writes them to a flat binary file in PBL format (our trivial container)
  5. Auto-generates an SDDL schema that tells OpenZL how to parse the PBL
  6. Saves metadata as JSON for the benchmark script

PBL (Parquet Binary Layout) is NOT a standard — it's a minimal container for
this experiment. Layout:
  [4B magic "PBL1"] [4B num_rows LE uint32] [col1 values] [col2 values] ...

No padding between columns, no per-column headers, just raw typed bytes.

Usage:
  python extract_for_openzl.py
  python extract_for_openzl.py --datasets numeric_heavy,ml_features
"""

import argparse
import json
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent

ALL_DATASETS = ["numeric_heavy", "ml_features", "mixed_type"]

# Arrow type → (SDDL type name, byte width, target numpy dtype)
# Note: SDDL type names are used DIRECTLY in schemas — NO aliases allowed.
TYPE_MAP = {
    pa.int16():   ("Int16LE",   2, np.dtype("<i2")),
    pa.int32():   ("Int32LE",   4, np.dtype("<i4")),
    pa.int64():   ("Int64LE",   8, np.dtype("<i8")),
    pa.float32(): ("Float32LE", 4, np.dtype("<f4")),
    pa.float64(): ("Float64LE", 8, np.dtype("<f8")),
    pa.bool_():   ("Byte",      1, np.dtype("uint8")),
}


def map_arrow_type(arrow_type):
    """Map an Arrow type to (sddl_type, byte_width, numpy_dtype) or None."""
    if arrow_type in TYPE_MAP:
        return TYPE_MAP[arrow_type]
    if pa.types.is_timestamp(arrow_type):
        return ("Int64LE", 8, np.dtype("<i8"))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str = ""):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# Column extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_column_to_numpy(col: pa.ChunkedArray, arrow_type) -> tuple[np.ndarray, int]:
    """Convert an Arrow column to a flat, LE numpy array for PBL writing.

    Returns (numpy_array, null_count).

    Null handling:
      - Floats:     nulls become NaN (pyarrow default)
      - Integers:   nulls become 0
      - Booleans:   nulls become False (0), cast to uint8
      - Timestamps: cast to int64 microseconds, nulls become 0
    """
    null_count = col.null_count
    type_info = map_arrow_type(arrow_type)
    if type_info is None:
        raise ValueError(f"Unsupported type: {arrow_type}")
    _, _, target_dtype = type_info

    if pa.types.is_floating(arrow_type):
        # NaN fills automatically for floats
        arr = col.to_numpy(zero_copy_only=False)

    elif pa.types.is_boolean(arrow_type):
        filled = pc.fill_null(col, False)
        arr = filled.to_numpy(zero_copy_only=False).astype(np.uint8)

    elif pa.types.is_timestamp(arrow_type):
        casted = col.cast(pa.int64())
        filled = pc.fill_null(casted, 0)
        arr = filled.to_numpy(zero_copy_only=False)

    else:
        # Integer types — fill nulls with 0
        # pc.fill_null handles type matching automatically
        filled = pc.fill_null(col, pa.scalar(0, type=arrow_type))
        arr = filled.to_numpy(zero_copy_only=False)

    # Cast to target dtype if needed (e.g., float64→int16 after null fill)
    if arr.dtype != target_dtype:
        arr = arr.astype(target_dtype)

    # Guarantee little-endian byte order
    if arr.dtype.byteorder not in ("<", "=", "|"):
        arr = arr.byteswap().newbyteorder("<")

    return arr, null_count


# ─────────────────────────────────────────────────────────────────────────────
# SDDL schema generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_sddl(dataset: str, source_file: str, num_rows: int,
                  columns: list[dict]) -> str:
    """Generate an SDDL schema string for a PBL file.

    CRITICAL: Every column uses the built-in type name directly (e.g.,
    Float32LE, Int64LE). NO type aliases. Each usage creates a separate
    compression stream — this is essential for good compression.
    """
    max_name_len = max(len(c["name"]) for c in columns) if columns else 10

    lines = [
        f"# Auto-generated SDDL for: {dataset}",
        f"# Source: {source_file} ({num_rows:,} rows x {len(columns)} numeric columns)",
        f"# Layout: Full columnar (Layout A) -- all values of col 1, then col 2, etc.",
        f"# Generated by extract_for_openzl.py",
        f"#",
        f"# IMPORTANT: Each column uses the built-in type directly (no aliases)",
        f"# so that OpenZL creates independent compression streams per column.",
        f"",
        f": Byte[4]            # Magic PBL1 -- consumed, not referenced",
        f"num_rows : UInt32LE  # Row count, used as array dimension below",
        f"",
        f"# -- Column streams --------------------------------------------------",
    ]

    for c in columns:
        name = c["name"]
        sddl_type = c["sddl_type"]
        comment = f"  # {c['parquet_type']}"
        lines.append(f"{name:<{max_name_len}s} : {sddl_type}[num_rows]{comment}")

    lines.append("")
    lines.append(": Byte[_rem]   # Permissive trailer")
    lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Per-dataset extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_dataset(dataset: str, data_dir: Path, extracted_dir: Path,
                    schema_dir: Path) -> dict:
    """Extract one dataset: Parquet → PBL binary + SDDL schema + metadata."""
    source_file = data_dir / f"{dataset}_none.parquet"
    if not source_file.exists():
        log(f"ERROR: source file not found: {source_file}")
        return None

    source_size = source_file.stat().st_size
    log("═" * 60)
    log(f"Dataset: {dataset}")
    log(f"Source:  {source_file.name} ({source_size / 1024 / 1024:.1f} MiB)")
    log("═" * 60)

    start = time.monotonic()

    # ── Read Parquet ──
    log("Reading Parquet file...")
    table = pq.read_table(str(source_file))
    num_rows = table.num_rows
    log(f"Table: {num_rows:,} rows × {table.num_columns} columns")

    # ── Identify and extract columns ──
    columns_meta = []
    skipped_cols = []
    arrays = []  # parallel list of numpy arrays

    log("Extracting numeric columns:")
    for i in range(table.num_columns):
        field = table.schema.field(i)
        col_name = field.name
        arrow_type = field.type
        type_info = map_arrow_type(arrow_type)

        if type_info is None:
            skipped_cols.append(col_name)
            continue

        sddl_type, byte_width, _ = type_info
        col = table.column(col_name)
        arr, null_count = extract_column_to_numpy(col, arrow_type)

        columns_meta.append({
            "name": col_name,
            "parquet_type": str(arrow_type),
            "sddl_type": sddl_type,
            "byte_width": byte_width,
            "null_count": null_count,
        })
        arrays.append(arr)

        null_info = ""
        if null_count > 0:
            fill = "NaN" if pa.types.is_floating(arrow_type) else "0"
            null_info = f"  [{null_count:,} nulls → {fill}]"
        log(f"  {len(columns_meta):>2d}. {col_name:<20s}: "
            f"{str(arrow_type):<18s} → {sddl_type:<12s} "
            f"({byte_width} bytes/row){null_info}")

    if skipped_cols:
        log(f"Skipping {len(skipped_cols)} non-numeric column(s): "
            f"{', '.join(skipped_cols)}")

    # ── Write PBL binary ──
    out_dir = extracted_dir / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    pbl_path = out_dir / "data.pbl"

    log(f"Writing PBL binary: {pbl_path}")
    raw_col_bytes = 0
    with open(pbl_path, "wb") as f:
        f.write(b"PBL1")
        f.write(struct.pack("<I", num_rows))
        for arr in arrays:
            data = arr.tobytes()
            f.write(data)
            raw_col_bytes += len(data)

    pbl_size = pbl_path.stat().st_size
    bytes_per_row = sum(c["byte_width"] for c in columns_meta)
    log(f"  Header:  8 bytes (magic + num_rows)")
    log(f"  Columns: {raw_col_bytes:,} bytes "
        f"({bytes_per_row} bytes/row × {num_rows:,} rows)")
    log(f"  Total:   {pbl_size:,} bytes ({pbl_size / 1024 / 1024:.1f} MiB)")

    # ── Verify size ──
    expected = 8 + sum(num_rows * c["byte_width"] for c in columns_meta)
    assert pbl_size == expected, f"PBL size mismatch: {pbl_size} != {expected}"
    log(f"  ✓ Size verified: {pbl_size} == {expected}")

    # ── Generate SDDL schema ──
    schema_dir.mkdir(parents=True, exist_ok=True)
    sddl_path = schema_dir / f"{dataset}.sddl"
    sddl_content = generate_sddl(
        dataset, f"{dataset}_none.parquet", num_rows, columns_meta,
    )
    sddl_path.write_text(sddl_content)
    log(f"Writing SDDL schema: {sddl_path}")

    # ── Gather Parquet variant sizes ──
    parquet_sizes = {}
    for codec in ["none", "snappy", "gzip", "zstd"]:
        pq_path = data_dir / f"{dataset}_{codec}.parquet"
        if pq_path.exists():
            parquet_sizes[f"parquet_{codec}_bytes"] = pq_path.stat().st_size

    # ── Write metadata JSON ──
    metadata = {
        "dataset": dataset,
        "source_file": f"{dataset}_none.parquet",
        "num_rows": num_rows,
        "columns": columns_meta,
        "skipped_columns": skipped_cols,
        "pbl_size_bytes": pbl_size,
        "raw_column_bytes": raw_col_bytes,
        **parquet_sizes,
        "extracted_at": datetime.now().isoformat(),
    }

    meta_path = out_dir / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2))
    log(f"Writing metadata:    {meta_path}")

    elapsed = time.monotonic() - start
    log(f"✓ {dataset} complete ({elapsed:.1f}s)")

    # Free memory
    del arrays, table
    return metadata


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract Parquet column data into PBL binary + SDDL schemas.",
    )
    parser.add_argument(
        "--datasets", type=str, default=",".join(ALL_DATASETS),
        help=f"Comma-separated dataset names (default: {','.join(ALL_DATASETS)})",
    )
    parser.add_argument(
        "--data-dir", type=str, default=str(SCRIPT_DIR / "data"),
        help="Directory containing Parquet files.",
    )
    parser.add_argument(
        "--output-dir", type=str, default=str(SCRIPT_DIR / "extracted"),
        help="Output directory for PBL files.",
    )
    parser.add_argument(
        "--schema-dir", type=str, default=str(SCRIPT_DIR / "schemas"),
        help="Output directory for SDDL schemas.",
    )
    args = parser.parse_args()

    datasets = [d.strip() for d in args.datasets.split(",")]
    data_dir = Path(args.data_dir)
    extracted_dir = Path(args.output_dir)
    schema_dir = Path(args.schema_dir)

    log("=" * 60)
    log("Parquet → PBL Extraction for OpenZL Experiments")
    log(f"Datasets:   {', '.join(datasets)}")
    log(f"Data dir:   {data_dir}")
    log(f"Output dir: {extracted_dir}")
    log(f"Schema dir: {schema_dir}")
    log("=" * 60)

    results = []
    for ds in datasets:
        meta = extract_dataset(ds, data_dir, extracted_dir, schema_dir)
        if meta:
            results.append(meta)

    # ── Summary ──
    log("")
    log("═" * 60)
    log("Extraction Summary")
    log("═" * 60)
    for m in results:
        pbl_mib = m["pbl_size_bytes"] / (1024 * 1024)
        pq_zstd = m.get("parquet_zstd_bytes", 0) / (1024 * 1024)
        log(f"  {m['dataset']:<18s}  {m['num_rows']:>10,} rows  "
            f"{len(m['columns']):>3d} cols  "
            f"PBL={pbl_mib:>7.1f} MiB  "
            f"Parquet+zstd={pq_zstd:>7.1f} MiB")
    log("═" * 60)


if __name__ == "__main__":
    main()
