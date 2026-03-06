"""Read OpenZL-compressed Parquet files (codec 100).

Reads the Parquet footer, identifies OpenZL columns via key-value metadata,
decompresses and decodes each column chunk, and returns an Arrow table.
"""

from __future__ import annotations

import json
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .chunk_format import deserialize_column_chunk
from .column_decoder import (
    decode_raw, decode_delta, decode_binary_convert,
    decode_dictionary, INDEX_DTYPE_MAP,
)
from .null_bitmap import apply_null_bitmap_numeric, apply_null_bitmap_strings
from .parquet_thrift import read_page_header


PARQUET_MAGIC = b"PAR1"


def is_openzl_parquet(path: Path | str) -> bool:
    """Check if a file is an OpenZL-compressed Parquet file."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        schema = pq.read_schema(str(path))
        if schema.metadata and b"openzl:version" in schema.metadata:
            return True
    except Exception:
        pass
    return False


def read_openzl_parquet(
    input_path: Path | str,
    zli_path: str,
    columns: list[str] | None = None,
    verbose: bool = False,
) -> pa.Table:
    """Read an OpenZL-compressed Parquet file and return an Arrow table.

    Args:
        input_path: Path to the .parquet file with codec 100.
        zli_path: Path to the zli binary for decompression.
        columns: Optional list of column names to read (column pruning).
        verbose: Print progress info.

    Returns:
        Arrow table with the requested columns.
    """
    input_path = Path(input_path)

    meta = pq.read_metadata(str(input_path))
    schema_obj = pq.read_schema(str(input_path))

    raw_metadata = schema_obj.metadata or {}
    if b"openzl:version" not in raw_metadata:
        raise ValueError(f"Not an OpenZL Parquet file: {input_path}")

    columns_meta = json.loads(raw_metadata[b"openzl:columns"].decode("utf-8"))

    if meta.num_row_groups == 0:
        raise ValueError(f"No row groups in {input_path}")
    all_col_names = [meta.row_group(0).column(i).path_in_schema
                     for i in range(meta.row_group(0).num_columns)]

    if columns is None:
        requested = all_col_names
    else:
        requested = [c for c in columns if c in all_col_names]

    rg = meta.row_group(0)

    col_offsets = {}
    col_sizes = {}
    for i in range(rg.num_columns):
        col_meta = rg.column(i)
        name = col_meta.path_in_schema
        col_offsets[name] = col_meta.data_page_offset
        col_sizes[name] = col_meta.total_compressed_size

    schema_fields = {f.name: f for f in schema_obj}
    num_rows = meta.num_rows

    arrays = {}
    with open(input_path, "rb") as f:
        for col_name in requested:
            if col_name not in col_offsets:
                continue

            offset = col_offsets[col_name]
            size = col_sizes[col_name]

            f.seek(offset)
            raw_bytes = f.read(size)

            page_hdr, hdr_len = read_page_header(raw_bytes)
            blob = raw_bytes[hdr_len:]

            col_header = columns_meta.get(col_name, {})
            arrow_type = _resolve_arrow_type(col_header, schema_fields[col_name].type)

            if verbose:
                print(f"  Reading {col_name}: encoding={col_header.get('encoding')}, "
                      f"blob={len(blob)} bytes")

            arr = _decompress_and_decode(
                blob, col_header, arrow_type, num_rows, zli_path, verbose,
            )
            arrays[col_name] = arr

    result_fields = []
    for c in requested:
        if c in arrays:
            col_header = columns_meta.get(c, {})
            arrow_type = _resolve_arrow_type(col_header, schema_fields[c].type)
            result_fields.append(pa.field(c, arrow_type))
    result_schema = pa.schema(result_fields)
    return pa.table(arrays, schema=result_schema)


_ARROW_TYPE_MAP = {
    "int8": pa.int8(),
    "int16": pa.int16(),
    "int32": pa.int32(),
    "int64": pa.int64(),
    "uint8": pa.uint8(),
    "uint16": pa.uint16(),
    "uint32": pa.uint32(),
    "uint64": pa.uint64(),
    "float": pa.float32(),
    "float16": pa.float16(),
    "double": pa.float64(),
    "bool": pa.bool_(),
    "string": pa.string(),
    "large_string": pa.large_string(),
}


def _resolve_arrow_type(col_header: dict, fallback: pa.DataType) -> pa.DataType:
    """Get the original Arrow type from our stored metadata, falling back to the schema."""
    type_str = col_header.get("arrow_type", "")
    if type_str in _ARROW_TYPE_MAP:
        return _ARROW_TYPE_MAP[type_str]
    if type_str.startswith("timestamp["):
        unit = type_str.split("[")[1].rstrip("]").split(",")[0]
        tz = None
        if "tz=" in type_str:
            tz = type_str.split("tz=")[1].rstrip("]")
        return pa.timestamp(unit, tz=tz)
    return fallback


def _decompress_and_decode(
    blob: bytes,
    col_header: dict[str, Any],
    arrow_type: pa.DataType,
    num_rows: int,
    zli_path: str,
    verbose: bool = False,
) -> pa.Array:
    """Decompress a column chunk blob and return an Arrow array."""
    chunk = deserialize_column_chunk(blob)
    encoding = chunk.header["encoding"]
    profile = chunk.header["profile"]
    metadata = chunk.header.get("metadata", {})

    primary_decompressed = _zli_decompress(chunk.primary_zl_data, zli_path)

    dict_decompressed_parts = []
    for dchunk in chunk.dict_chunk_zl_data:
        dict_decompressed_parts.append(_zli_decompress(dchunk, zli_path))

    if encoding == "raw":
        np_dtype, width = _profile_to_np(profile, arrow_type)
        values = decode_raw(primary_decompressed, np_dtype, width)
        return apply_null_bitmap_numeric(
            values, chunk.null_bitmap, chunk.null_count, arrow_type,
        )

    elif encoding == "delta":
        np_dtype, width = _profile_to_np(profile, arrow_type)
        values = decode_delta(primary_decompressed, np_dtype, width)
        return apply_null_bitmap_numeric(
            values, chunk.null_bitmap, chunk.null_count, arrow_type,
        )

    elif encoding == "binary_convert":
        binary_type = metadata["binary_type"]
        element_size = metadata["element_size"]
        values = decode_binary_convert(primary_decompressed, binary_type, element_size)
        return apply_null_bitmap_strings(
            values, chunk.null_bitmap, chunk.null_count,
        )

    elif encoding == "dictionary":
        index_dtype = INDEX_DTYPE_MAP[metadata["index_dtype"]]
        num_unique = metadata["num_unique"]
        dict_blob = b"".join(dict_decompressed_parts)
        values = decode_dictionary(primary_decompressed, dict_blob, index_dtype, num_unique)
        return apply_null_bitmap_strings(
            values, chunk.null_bitmap, chunk.null_count,
        )

    else:
        raise ValueError(f"Unknown encoding: {encoding}")


def _zli_decompress(data: bytes, zli_path: str) -> bytes:
    """Decompress a .zl frame using zli."""
    with tempfile.NamedTemporaryFile(suffix=".zl", delete=False) as zl_f:
        zl_f.write(data)
        zl_path_tmp = Path(zl_f.name)

    out_path = zl_path_tmp.with_suffix(".bin")
    try:
        result = subprocess.run(
            [zli_path, "decompress", str(zl_path_tmp),
             "--output", str(out_path), "--force"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"zli decompress failed: {result.stderr[:300]}")
        return out_path.read_bytes()
    finally:
        zl_path_tmp.unlink(missing_ok=True)
        out_path.unlink(missing_ok=True)


def _profile_to_np(profile: str, arrow_type: pa.DataType):
    """Map OpenZL profile + Arrow type to numpy dtype and width."""
    if pa.types.is_boolean(arrow_type):
        return np.dtype("uint8"), 1
    if profile == "le-i16":
        if pa.types.is_unsigned_integer(arrow_type):
            return np.dtype("<u2"), 2
        return np.dtype("<i2"), 2
    if profile == "le-i32":
        if pa.types.is_floating(arrow_type):
            return np.dtype("<f4"), 4
        if pa.types.is_unsigned_integer(arrow_type):
            return np.dtype("<u4"), 4
        return np.dtype("<i4"), 4
    if profile == "le-i64":
        if pa.types.is_floating(arrow_type):
            return np.dtype("<f8"), 8
        if pa.types.is_timestamp(arrow_type):
            return np.dtype("<i8"), 8
        if pa.types.is_unsigned_integer(arrow_type):
            return np.dtype("<u8"), 8
        return np.dtype("<i8"), 8
    if profile == "serial":
        return np.dtype("uint8"), 1
    raise ValueError(f"Unknown profile: {profile}")
