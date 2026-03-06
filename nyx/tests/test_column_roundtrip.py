#!/usr/bin/env python3
"""Step 3: Full encode → compress → decompress → decode round-trip test.

Tests every encoding type with and without nulls.
Uses small arrays (1000 rows) so each zli invocation finishes in seconds.
"""

import shutil
import subprocess
import sys
import tempfile
import uuid as _uuid
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nyx.core.column_encoder import ColumnEncoder, _serialise_dict, _arrow_type_to_profile
from nyx.core.column_decoder import (
    decode_raw, decode_delta, decode_binary_convert,
    decode_dictionary, deserialize_dict, INDEX_DTYPE_MAP,
)
from nyx.core.null_bitmap import (
    extract_null_bitmap, apply_null_bitmap_numeric, apply_null_bitmap_strings,
)

ZLI = str(sorted(Path("nyx/openzl/cachedObjs").glob("*/zli"))[-1].resolve())
N = 1000


def zli_decompress(zl_path: Path, out_path: Path):
    result = subprocess.run(
        [ZLI, "decompress", str(zl_path), "--output", str(out_path), "--force"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"zli decompress failed: {result.stderr[:300]}")


def run_test(name, arrow_array, arrow_type, expected_encoding):
    tmpdir = Path(tempfile.mkdtemp(prefix=f"rt_{name}_"))
    try:
        col = arrow_array
        if isinstance(col, pa.ChunkedArray):
            col = col.combine_chunks()

        bitmap, null_count = extract_null_bitmap(col)

        if null_count > 0:
            if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
                filled = pa.array(
                    [(v.as_py() or "") for v in col], type=pa.string()
                )
            else:
                sentinel = pa.scalar(0, type=arrow_type) if not pa.types.is_boolean(arrow_type) else False
                filled = pc.fill_null(col, sentinel)
        else:
            filled = col

        encoder = ColumnEncoder(ZLI)
        result = encoder.encode_and_compress(
            filled, name, arrow_type, "auto", str(tmpdir), compress_timeout=120,
        )

        assert result.encoding == expected_encoding, (
            f"Expected encoding '{expected_encoding}', got '{result.encoding}'"
        )

        # Decompress primary .zl file
        zl_path = Path(result.zl_path)
        bin_path = tmpdir / f"{name}_decompressed.bin"
        zli_decompress(zl_path, bin_path)
        primary_data = bin_path.read_bytes()

        # Decompress sidecar .zl files (dictionary chunks)
        sidecar_data_list = []
        for sp in result.sidecar_paths:
            sp_path = Path(sp)
            sp_out = tmpdir / f"{sp_path.stem}_dec.bin"
            zli_decompress(sp_path, sp_out)
            sidecar_data_list.append(sp_out.read_bytes())

        # Decode
        encoding = result.encoding
        if encoding == "raw":
            profile, np_dtype, width = _arrow_type_to_profile(arrow_type)
            values = decode_raw(primary_data, np_dtype, width)
            restored = apply_null_bitmap_numeric(values, bitmap, null_count, arrow_type)
        elif encoding == "delta":
            profile, np_dtype, width = _arrow_type_to_profile(arrow_type)
            values = decode_delta(primary_data, np_dtype, width)
            restored = apply_null_bitmap_numeric(values, bitmap, null_count, arrow_type)
        elif encoding == "binary_convert":
            binary_type = result.metadata["binary_type"]
            element_size = result.metadata["element_size"]
            values = decode_binary_convert(primary_data, binary_type, element_size)
            restored = apply_null_bitmap_strings(values, bitmap, null_count)
        elif encoding == "dictionary":
            index_dtype = INDEX_DTYPE_MAP[result.metadata["index_dtype"]]
            num_unique = result.metadata["num_unique"]
            dict_blob = b"".join(sidecar_data_list)
            values = decode_dictionary(primary_data, dict_blob, index_dtype, num_unique)
            restored = apply_null_bitmap_strings(values, bitmap, null_count)
        else:
            raise ValueError(f"Unknown encoding: {encoding}")

        # Compare
        if not col.equals(restored):
            for i in range(min(len(col), 20)):
                o = col[i].as_py()
                r = restored[i].as_py()
                if o != r:
                    print(f"  Mismatch at index {i}: original={o!r}, restored={r!r}")
            raise AssertionError(f"Round-trip failed for {name}")

        ratio = len(primary_data) / result.compressed_bytes if result.compressed_bytes > 0 else 0
        print(f"  PASS: {name:20s} encoding={encoding:16s} ratio={ratio:.1f}x null_count={null_count}")
        return True
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    rng = np.random.default_rng(42)
    passed = 0
    failed = 0
    tests = []

    # A1: INT64 sorted → delta
    tests.append(("int64_sorted", pa.array(np.arange(1, N + 1, dtype=np.int64)), pa.int64(), "delta"))

    # A2: INT64 unsorted → raw
    tests.append(("int64_unsorted", pa.array(rng.integers(0, 10**9, N, dtype=np.int64)), pa.int64(), "raw"))

    # A3: INT32 → raw
    tests.append(("int32", pa.array(rng.integers(-1000, 1000, N, dtype=np.int32)), pa.int32(), "raw"))

    # A4: FLOAT32 → raw
    tests.append(("float32", pa.array(rng.standard_normal(N).astype(np.float32)), pa.float32(), "raw"))

    # A5: FLOAT64 → raw
    tests.append(("float64", pa.array(rng.standard_normal(N).astype(np.float64)), pa.float64(), "raw"))

    # A6: BOOL → raw
    tests.append(("bool", pa.array(rng.choice([True, False], N)), pa.bool_(), "raw"))

    # A7: TIMESTAMP → raw
    base_ts = 1700000000000000
    ts_vals = base_ts + np.cumsum(rng.integers(1000, 100000, N))
    tests.append(("timestamp", pa.array(ts_vals, type=pa.timestamp("us")), pa.timestamp("us"), "delta"))

    # A8: STRING UUID → binary_convert
    uuids = [str(_uuid.uuid4()) for _ in range(N)]
    tests.append(("uuid_str", pa.array(uuids), pa.string(), "binary_convert"))

    # A9: STRING IPv4 → binary_convert
    ips = [f"{rng.integers(1,255)}.{rng.integers(0,255)}.{rng.integers(0,255)}.{rng.integers(0,255)}" for _ in range(N)]
    tests.append(("ipv4_str", pa.array(ips), pa.string(), "binary_convert"))

    # A10: STRING hex hash → binary_convert
    hashes = [rng.bytes(32).hex() for _ in range(N)]
    tests.append(("hex_hash", pa.array(hashes), pa.string(), "binary_convert"))

    # A11: STRING low-cardinality → dictionary
    statuses = ["active", "inactive", "pending", "error", "complete"]
    status_vals = [statuses[i % 5] for i in range(N)]
    tests.append(("low_card_str", pa.array(status_vals), pa.string(), "dictionary"))

    # A12: STRING high-cardinality → dictionary
    emails = [f"user{i}@domain{i % 10}.com" for i in range(N)]
    tests.append(("high_card_str", pa.array(emails), pa.string(), "dictionary"))

    # A13: INT64 with nulls → raw
    int_with_nulls = [int(rng.integers(0, 1000)) if rng.random() > 0.1 else None for _ in range(N)]
    tests.append(("int64_nulls", pa.array(int_with_nulls, type=pa.int64()), pa.int64(), "raw"))

    # A14: STRING with nulls → dictionary
    str_with_nulls = [f"val_{i}" if rng.random() > 0.1 else None for i in range(N)]
    tests.append(("str_nulls", pa.array(str_with_nulls, type=pa.string()), pa.string(), "dictionary"))

    print(f"Running {len(tests)} round-trip tests (N={N} rows each)...\n")

    for name, arr, dtype, expected_enc in tests:
        try:
            run_test(name, arr, dtype, expected_enc)
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name} — {e}")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Results: {passed} passed, {failed} failed, {len(tests)} total")
    if failed == 0:
        print("ALL TESTS PASSED")
    else:
        print("SOME TESTS FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
