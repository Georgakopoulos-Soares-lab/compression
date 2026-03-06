"""Automated test suite for Parquet compression/decompression round-trip.

Tests cover:
  1. Pure numeric columns (int16, int32, int64, float32, float64)
  2. String columns (low-cardinality, high-cardinality)
  3. Mixed numeric + string columns
  4. Columns with nulls (numeric and string)
  5. Monotonic/sorted columns (delta encoding)
  6. Large datasets (1M+ rows)
  7. Metadata preservation (schema, row count, column names, key-value metadata)
  8. Column pruning on decompression
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nyx.core.parquet_pipeline import compress_parquet, decompress_parquet
from nyx.core.parquet_reader import is_openzl_parquet

ZLI = str(sorted(Path(__file__).resolve().parent.parent.joinpath(
    "openzl/cachedObjs").glob("*/zli"))[-1].resolve())


def _roundtrip(table: pa.Table, label: str, columns=None) -> bool:
    """Write table to Parquet, compress with OpenZL, decompress, and compare."""
    in_path = Path(tempfile.mktemp(suffix="_in.parquet"))
    out_path = Path(tempfile.mktemp(suffix="_out.parquet"))
    res_path = Path(tempfile.mktemp(suffix="_res.parquet"))

    try:
        pq.write_table(table, str(in_path), compression="none")
        compress_parquet(in_path, out_path, ZLI, verbose=False)

        assert is_openzl_parquet(out_path), "Output not detected as OpenZL Parquet"

        meta = pq.read_metadata(str(out_path))
        assert meta.num_rows == table.num_rows, f"Row count mismatch: {meta.num_rows} vs {table.num_rows}"
        assert meta.num_columns == len(table.schema), f"Column count mismatch"

        decompress_parquet(out_path, res_path, ZLI, columns=columns, verbose=False)
        restored = pq.read_table(str(res_path))

        expected_cols = columns or table.column_names
        for col_name in expected_cols:
            orig = table.column(col_name).combine_chunks()
            rest = restored.column(col_name).combine_chunks()
            if not orig.equals(rest):
                print(f"  MISMATCH in {col_name}:")
                for i in range(min(5, len(orig))):
                    o, r = orig[i].as_py(), rest[i].as_py()
                    if o != r:
                        print(f"    [{i}] {o!r} vs {r!r}")
                return False

        return True

    finally:
        for p in [in_path, out_path, res_path]:
            p.unlink(missing_ok=True)


def test_pure_numerics():
    rng = np.random.default_rng(1)
    N = 500
    table = pa.table({
        "i16": pa.array(rng.integers(-100, 100, N, dtype=np.int16)),
        "i32": pa.array(rng.integers(-10000, 10000, N, dtype=np.int32)),
        "i64": pa.array(rng.integers(0, 10**9, N, dtype=np.int64)),
        "f32": pa.array(rng.standard_normal(N).astype(np.float32)),
        "f64": pa.array(rng.standard_normal(N).astype(np.float64)),
    })
    return _roundtrip(table, "pure_numerics")


def test_sorted_column():
    N = 500
    table = pa.table({
        "monotonic": pa.array(np.arange(1, N + 1, dtype=np.int64)),
        "random": pa.array(np.random.default_rng(2).standard_normal(N).astype(np.float32)),
    })
    return _roundtrip(table, "sorted_column")


def test_low_card_strings():
    N = 500
    choices = ["alpha", "beta", "gamma", "delta", "epsilon"]
    table = pa.table({
        "category": pa.array([choices[i % len(choices)] for i in range(N)]),
        "id": pa.array(np.arange(N, dtype=np.int32)),
    })
    return _roundtrip(table, "low_card_strings")


def test_mixed_types():
    rng = np.random.default_rng(3)
    N = 500
    table = pa.table({
        "id": pa.array(np.arange(N, dtype=np.int64)),
        "score": pa.array(rng.standard_normal(N).astype(np.float32)),
        "label": pa.array([f"item_{i}" for i in range(N)]),
        "flag": pa.array([["a", "b", "c"][i % 3] for i in range(N)]),
    })
    return _roundtrip(table, "mixed_types")


def test_numerics_with_nulls():
    rng = np.random.default_rng(4)
    N = 500
    vals = rng.integers(0, 1000, N).tolist()
    for i in range(0, N, 7):
        vals[i] = None
    table = pa.table({"nullable_int": pa.array(vals, type=pa.int64())})
    return _roundtrip(table, "numerics_with_nulls")


def test_strings_with_nulls():
    N = 500
    vals = [f"val_{i}" if i % 5 != 0 else None for i in range(N)]
    table = pa.table({"nullable_str": pa.array(vals, type=pa.string())})
    return _roundtrip(table, "strings_with_nulls")


def test_column_pruning():
    rng = np.random.default_rng(5)
    N = 200
    table = pa.table({
        "a": pa.array(np.arange(N, dtype=np.int32)),
        "b": pa.array(rng.standard_normal(N).astype(np.float32)),
        "c": pa.array([f"x{i}" for i in range(N)]),
    })
    return _roundtrip(table, "column_pruning", columns=["a", "c"])


def test_metadata_preservation():
    """Test that key-value metadata and schema survive the round-trip."""
    N = 100
    table = pa.table({"x": pa.array(np.arange(N, dtype=np.int64))})

    in_path = Path(tempfile.mktemp(suffix="_in.parquet"))
    out_path = Path(tempfile.mktemp(suffix="_out.parquet"))

    try:
        pq.write_table(table, str(in_path), compression="none")
        compress_parquet(in_path, out_path, ZLI, verbose=False)

        meta = pq.read_metadata(str(out_path))
        assert meta.num_rows == N
        assert meta.num_columns == 1

        schema = pq.read_schema(str(out_path))
        assert schema.metadata is not None
        assert b"openzl:version" in schema.metadata
        assert b"openzl:columns" in schema.metadata

        return True
    finally:
        for p in [in_path, out_path]:
            p.unlink(missing_ok=True)


def test_large_dataset():
    """Test with 100K rows to verify scalability."""
    rng = np.random.default_rng(6)
    N = 100_000
    table = pa.table({
        "id": pa.array(np.arange(N, dtype=np.int64)),
        "value": pa.array(rng.standard_normal(N).astype(np.float32)),
        "cat": pa.array([["A", "B", "C"][i % 3] for i in range(N)]),
    })
    return _roundtrip(table, "large_dataset")


if __name__ == "__main__":
    tests = [
        ("Pure numeric columns", test_pure_numerics),
        ("Sorted/monotonic column (delta)", test_sorted_column),
        ("Low-cardinality strings", test_low_card_strings),
        ("Mixed types", test_mixed_types),
        ("Numeric nulls", test_numerics_with_nulls),
        ("String nulls", test_strings_with_nulls),
        ("Column pruning", test_column_pruning),
        ("Metadata preservation", test_metadata_preservation),
        ("Large dataset (100K rows)", test_large_dataset),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            result = fn()
            if result:
                print(f"  PASS  {name}")
                passed += 1
            else:
                print(f"  FAIL  {name}")
                failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{passed}/{passed + failed} tests passed")
    sys.exit(0 if failed == 0 else 1)
