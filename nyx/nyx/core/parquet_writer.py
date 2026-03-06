"""Write Parquet files with OpenZL compression (codec 100).

Produces a valid Parquet file: PAR1 magic, row groups, Thrift footer.
Standard readers can parse the metadata but cannot decompress the data
(unknown codec). Our reader handles decompression.
"""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from . import parquet_thrift as pt


PARQUET_MAGIC = b"PAR1"


def write_openzl_parquet(
    output_path: Path | str,
    original_path: Path | str,
    column_blobs: dict[str, bytes],
    column_headers: dict[str, dict[str, Any]],
    num_rows: int,
    schema: pa.Schema,
    original_statistics: dict[str, dict] | None = None,
) -> int:
    """Write an OpenZL-compressed Parquet file.

    Args:
        output_path: Where to write the output.
        original_path: Path to the original Parquet file (for metadata preservation).
        column_blobs: Map of column_name → serialized chunk blob (from chunk_format).
        column_headers: Map of column_name → encoding header dict.
        num_rows: Total number of rows.
        schema: Arrow schema of the original file.
        original_statistics: Per-column statistics from the original file.

    Returns:
        Size of the written file in bytes.
    """
    output_path = Path(output_path)
    original_path = Path(original_path)

    # Preserve original footer for exact reconstruction
    original_footer_b64 = _read_original_footer_b64(original_path)

    # Build schema elements
    schema_elements = _build_schema_elements(schema)

    # Build key-value metadata
    kv_pairs = [
        ("openzl:version", "1.0"),
        ("openzl:columns", json.dumps(column_headers, separators=(",", ":"))),
        ("openzl:original_metadata", original_footer_b64),
    ]

    with open(output_path, "wb") as f:
        f.write(PARQUET_MAGIC)

        column_chunk_thrifts = []
        total_byte_size = 0

        for col_name in schema.names:
            blob = column_blobs[col_name]
            header = column_headers[col_name]

            uncompressed_size = header.get("uncompressed_bytes", len(blob))
            compressed_size = len(blob)

            page_header_bytes = pt.write_page_header(
                num_values=num_rows,
                uncompressed_size=uncompressed_size,
                compressed_size=compressed_size,
            )

            col_offset = f.tell()
            f.write(page_header_bytes)
            f.write(blob)

            col_total_compressed = len(page_header_bytes) + compressed_size
            col_total_uncompressed = len(page_header_bytes) + uncompressed_size

            arrow_type_str = str(schema.field(col_name).type)
            parquet_type = pt._parquet_type_for_arrow(arrow_type_str)

            stats_bytes = None
            if original_statistics and col_name in original_statistics:
                s = original_statistics[col_name]
                stats_bytes = pt.write_statistics(
                    min_val=s.get("min_bytes"),
                    max_val=s.get("max_bytes"),
                    null_count=s.get("null_count"),
                )

            col_meta = pt.write_column_metadata(
                parquet_type=parquet_type,
                encodings=[pt.PARQUET_ENCODING_PLAIN],
                path_in_schema=[col_name],
                codec=pt.OPENZL_CODEC_ID,
                num_values=num_rows,
                total_uncompressed=col_total_uncompressed,
                total_compressed=col_total_compressed,
                data_page_offset=col_offset,
                statistics_bytes=stats_bytes,
            )

            col_chunk = pt.write_column_chunk(
                file_offset=col_offset,
                column_metadata_bytes=col_meta,
            )
            column_chunk_thrifts.append(col_chunk)
            total_byte_size += col_total_uncompressed

        row_group = pt.write_row_group(
            column_chunks_bytes=column_chunk_thrifts,
            total_byte_size=total_byte_size,
            num_rows=num_rows,
        )

        footer = pt.write_file_metadata(
            version=2,
            schema_elements_bytes=schema_elements,
            row_groups_bytes=[row_group],
            num_rows=num_rows,
            key_value_pairs=kv_pairs,
            created_by="nyx (OpenZL)",
        )

        f.write(footer)
        f.write(struct.pack("<I", len(footer)))
        f.write(PARQUET_MAGIC)

    return output_path.stat().st_size


def _build_schema_elements(schema: pa.Schema) -> list[bytes]:
    """Build Thrift SchemaElement list from an Arrow schema."""
    elements = []
    root = pt.write_schema_element(
        name="schema",
        num_children=len(schema),
    )
    elements.append(root)

    for field in schema:
        arrow_type_str = str(field.type)
        parquet_type = pt._parquet_type_for_arrow(arrow_type_str)
        repetition = 1 if field.nullable else 0  # 1=OPTIONAL, 0=REQUIRED
        converted_type = _arrow_to_converted_type(field.type)
        elem = pt.write_schema_element(
            name=field.name,
            parquet_type=parquet_type,
            repetition=repetition,
            converted_type=converted_type,
        )
        elements.append(elem)

    return elements


def _arrow_to_converted_type(arrow_type: pa.DataType) -> int | None:
    """Map Arrow type to Parquet ConvertedType."""
    if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
        return pt.CONVERTED_TYPE_UTF8
    if pa.types.is_timestamp(arrow_type):
        unit = arrow_type.unit
        if unit == "ms":
            return pt.CONVERTED_TYPE_TIMESTAMP_MILLIS
        return pt.CONVERTED_TYPE_TIMESTAMP_MICROS
    if arrow_type == pa.int8():
        return pt.CONVERTED_TYPE_INT_8
    if arrow_type == pa.int16():
        return pt.CONVERTED_TYPE_INT_16
    if arrow_type == pa.int32():
        return pt.CONVERTED_TYPE_INT_32
    if arrow_type == pa.int64():
        return pt.CONVERTED_TYPE_INT_64
    if arrow_type == pa.uint8():
        return pt.CONVERTED_TYPE_UINT_8
    if arrow_type == pa.uint16():
        return pt.CONVERTED_TYPE_UINT_16
    if arrow_type == pa.uint32():
        return pt.CONVERTED_TYPE_UINT_32
    if arrow_type == pa.uint64():
        return pt.CONVERTED_TYPE_UINT_64
    return None


def _read_original_footer_b64(path: Path) -> str:
    """Read the raw Parquet footer and return as base64 string."""
    with open(path, "rb") as f:
        f.seek(-8, 2)
        footer_len = struct.unpack("<I", f.read(4))[0]
        f.seek(-8 - footer_len, 2)
        footer_bytes = f.read(footer_len)
    return base64.b64encode(footer_bytes).decode("ascii")


def extract_statistics(parquet_path: Path) -> dict[str, dict]:
    """Extract per-column statistics from a Parquet file.

    Returns a dict mapping column names to statistics dicts with:
        min_bytes, max_bytes, null_count
    """
    meta = pq.read_metadata(str(parquet_path))
    stats = {}

    for rg_idx in range(meta.num_row_groups):
        rg = meta.row_group(rg_idx)
        for col_idx in range(rg.num_columns):
            col_meta = rg.column(col_idx)
            col_name = col_meta.path_in_schema

            if col_name in stats:
                continue

            s = col_meta.statistics
            if s is None:
                continue

            entry = {"null_count": s.null_count}

            if s.has_min_max:
                try:
                    min_val = _stat_to_bytes(s.min, s.physical_type)
                    max_val = _stat_to_bytes(s.max, s.physical_type)
                    entry["min_bytes"] = min_val
                    entry["max_bytes"] = max_val
                except Exception:
                    pass

            stats[col_name] = entry

    return stats


def _stat_to_bytes(value, physical_type: str) -> bytes:
    """Convert a pyarrow statistic value to its Parquet binary representation."""
    if physical_type == "BOOLEAN":
        return struct.pack("<?", value)
    elif physical_type == "INT32":
        return struct.pack("<i", int(value))
    elif physical_type == "INT64":
        return struct.pack("<q", int(value))
    elif physical_type == "FLOAT":
        return struct.pack("<f", float(value))
    elif physical_type == "DOUBLE":
        return struct.pack("<d", float(value))
    elif physical_type == "BYTE_ARRAY":
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)
    elif physical_type == "FIXED_LEN_BYTE_ARRAY":
        return bytes(value)
    else:
        return str(value).encode("utf-8")
