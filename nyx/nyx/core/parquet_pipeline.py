"""End-to-end Parquet compression/decompression pipeline.

Orchestrates: read input → encode columns → compress → write output.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .chunk_format import serialize_column_chunk
from .column_encoder import ColumnEncoder
from .null_bitmap import extract_null_bitmap
from .parquet_writer import write_openzl_parquet, extract_statistics
from .parquet_reader import read_openzl_parquet

import pyarrow.compute as pc


def compress_parquet(
    input_path: Path | str,
    output_path: Path | str,
    zli_path: str,
    verbose: bool = False,
    compress_timeout: int = 600,
) -> dict:
    """Compress a standard Parquet file with OpenZL.

    Returns a summary dict with per-column sizes and timings.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    table = pq.read_table(str(input_path))
    schema = table.schema
    num_rows = table.num_rows
    encoder = ColumnEncoder(zli_path)

    original_stats = extract_statistics(input_path)

    tmpdir = Path(tempfile.mkdtemp(prefix="nyx_pq_"))
    column_blobs = {}
    column_headers = {}
    summary = {"columns": [], "num_rows": num_rows}

    try:
        for field in schema:
            col_name = field.name
            col_data = table.column(col_name)
            arrow_type = field.type

            if isinstance(col_data, pa.ChunkedArray):
                col_data = col_data.combine_chunks()

            if verbose:
                print(f"  Encoding {col_name} ({arrow_type})...")

            bitmap, null_count = extract_null_bitmap(col_data)

            if null_count > 0:
                if pa.types.is_string(arrow_type) or pa.types.is_large_string(arrow_type):
                    filled = pc.fill_null(col_data, "")
                elif pa.types.is_boolean(arrow_type):
                    filled = pc.fill_null(col_data, False)
                elif pa.types.is_timestamp(arrow_type):
                    filled = pc.fill_null(col_data.cast(pa.int64()), 0)
                    filled = filled.cast(arrow_type)
                else:
                    filled = pc.fill_null(col_data, pa.scalar(0, type=arrow_type))
            else:
                filled = col_data

            col_tmpdir = tmpdir / col_name
            col_tmpdir.mkdir()

            t0 = time.time()
            result = encoder.encode_and_compress(
                filled, col_name, arrow_type, "auto",
                str(col_tmpdir), compress_timeout,
            )
            elapsed = time.time() - t0

            raw_bytes = _column_raw_bytes(col_data, arrow_type)

            header_dict = {
                "encoding": result.encoding,
                "profile": result.profile,
                "num_rows": num_rows,
                "arrow_type": str(arrow_type),
                "uncompressed_bytes": raw_bytes,
                "metadata": result.metadata,
            }

            blob = serialize_column_chunk(
                header_dict, bitmap, null_count,
                result.zl_path, result.sidecar_paths,
            )

            column_blobs[col_name] = blob
            column_headers[col_name] = header_dict

            summary["columns"].append({
                "name": col_name,
                "type": str(arrow_type),
                "encoding": result.encoding,
                "profile": result.profile,
                "raw_bytes": raw_bytes,
                "compressed_bytes": len(blob),
                "null_count": null_count,
                "time_secs": round(elapsed, 1),
            })

            if verbose:
                ratio = raw_bytes / len(blob) if len(blob) > 0 else 0
                print(f"    {result.encoding} → {len(blob):,} bytes "
                      f"(ratio {ratio:.1f}x, {elapsed:.1f}s)")

        file_size = write_openzl_parquet(
            output_path=output_path,
            original_path=input_path,
            column_blobs=column_blobs,
            column_headers=column_headers,
            num_rows=num_rows,
            schema=schema,
            original_statistics=original_stats,
        )

        summary["output_size"] = file_size
        summary["input_size"] = input_path.stat().st_size

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return summary


def decompress_parquet(
    input_path: Path | str,
    output_path: Path | str,
    zli_path: str,
    columns: list[str] | None = None,
    output_compression: str = "none",
    verbose: bool = False,
) -> None:
    """Decompress an OpenZL Parquet file back to a standard Parquet file.

    Args:
        input_path: Path to OpenZL-compressed .parquet file.
        output_path: Where to write the restored .parquet file.
        zli_path: Path to zli binary.
        columns: Optional column subset to decompress.
        output_compression: Compression for output file ('none', 'zstd', 'snappy').
        verbose: Print progress.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    table = read_openzl_parquet(input_path, zli_path, columns=columns, verbose=verbose)

    pq.write_table(table, str(output_path), compression=output_compression)


def _column_raw_bytes(col: pa.Array | pa.ChunkedArray, dtype: pa.DataType) -> int:
    """Estimate uncompressed column size in bytes."""
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    n = len(col)
    if pa.types.is_boolean(dtype):
        return n
    if pa.types.is_int8(dtype) or pa.types.is_uint8(dtype):
        return n
    if pa.types.is_int16(dtype) or pa.types.is_uint16(dtype):
        return n * 2
    if pa.types.is_int32(dtype) or pa.types.is_uint32(dtype) or pa.types.is_float32(dtype):
        return n * 4
    if pa.types.is_int64(dtype) or pa.types.is_uint64(dtype) or pa.types.is_float64(dtype):
        return n * 8
    if pa.types.is_timestamp(dtype):
        return n * 8
    if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
        byte_lengths = pc.utf8_length(pc.fill_null(col, ""))
        data_bytes = pc.sum(byte_lengths).as_py() or 0
        return data_bytes + n * 4
    return n * 8
