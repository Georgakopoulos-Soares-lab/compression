"""Column chunk binary serialization format.

Packs encoding metadata, null bitmap, and .zl frame(s) into a single
contiguous byte sequence. This is the unit stored inside a Parquet column chunk.

Layout:
    [4 bytes LE] header_length
    [header_length bytes] header JSON (UTF-8)
    [4 bytes LE] null_count
    [if null_count > 0: ceil(num_rows/8) bytes] validity bitmap
    [4 bytes LE] primary_zl_length
    [primary_zl_length bytes] primary .zl frame
    [if encoding == "dictionary":
        [4 bytes LE] num_dict_chunks
        [for each chunk:
            [4 bytes LE] chunk_zl_length
            [chunk_zl_length bytes] dict chunk .zl frame
        ]
    ]
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ColumnChunkData:
    """Deserialized column chunk."""
    header: dict[str, Any]
    null_bitmap: bytes
    null_count: int
    primary_zl_data: bytes
    dict_chunk_zl_data: list[bytes] = field(default_factory=list)


def serialize_column_chunk(
    header_dict: dict[str, Any],
    null_bitmap: bytes,
    null_count: int,
    primary_zl_path: Path | str,
    sidecar_zl_paths: list[Path | str] | None = None,
) -> bytes:
    """Pack a column chunk into the binary format.

    Args:
        header_dict: Encoding metadata (encoding, profile, num_rows, arrow_type, etc.)
        null_bitmap: Validity bitmap bytes (empty if no nulls)
        null_count: Number of null values
        primary_zl_path: Path to the primary .zl file (indices for dict, data for others)
        sidecar_zl_paths: Paths to dictionary chunk .zl files (empty for non-dict encodings)
    """
    buf = bytearray()

    header_json = json.dumps(header_dict, separators=(",", ":")).encode("utf-8")
    buf.extend(struct.pack("<I", len(header_json)))
    buf.extend(header_json)

    buf.extend(struct.pack("<I", null_count))
    if null_count > 0:
        buf.extend(null_bitmap)

    primary_data = Path(primary_zl_path).read_bytes()
    buf.extend(struct.pack("<I", len(primary_data)))
    buf.extend(primary_data)

    if header_dict.get("encoding") == "dictionary" and sidecar_zl_paths:
        buf.extend(struct.pack("<I", len(sidecar_zl_paths)))
        for sp in sidecar_zl_paths:
            chunk_data = Path(sp).read_bytes()
            buf.extend(struct.pack("<I", len(chunk_data)))
            buf.extend(chunk_data)

    return bytes(buf)


def deserialize_column_chunk(data: bytes) -> ColumnChunkData:
    """Unpack a column chunk from the binary format.

    Raises ValueError on truncated or malformed data.
    """
    data_len = len(data)
    offset = 0

    if offset + 4 > data_len:
        raise ValueError("Truncated chunk: cannot read header length")
    header_len = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if offset + header_len > data_len:
        raise ValueError(f"Truncated chunk: header claims {header_len} bytes, only {data_len - offset} available")
    header = json.loads(data[offset : offset + header_len].decode("utf-8"))
    offset += header_len

    if offset + 4 > data_len:
        raise ValueError("Truncated chunk: cannot read null_count")
    null_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4

    null_bitmap = b""
    if null_count > 0:
        num_rows = header.get("num_rows")
        if num_rows is None:
            raise ValueError("Header missing 'num_rows' but null_count > 0")
        bitmap_len = (num_rows + 7) // 8
        if offset + bitmap_len > data_len:
            raise ValueError("Truncated chunk: bitmap data")
        null_bitmap = data[offset : offset + bitmap_len]
        offset += bitmap_len

    if offset + 4 > data_len:
        raise ValueError("Truncated chunk: cannot read primary_zl length")
    primary_zl_len = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    if offset + primary_zl_len > data_len:
        raise ValueError(f"Truncated chunk: primary .zl claims {primary_zl_len} bytes")
    primary_zl_data = data[offset : offset + primary_zl_len]
    offset += primary_zl_len

    dict_chunks = []
    if header.get("encoding") == "dictionary" and offset < data_len:
        if offset + 4 > data_len:
            raise ValueError("Truncated chunk: cannot read dict chunk count")
        num_dict_chunks = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        for ci in range(num_dict_chunks):
            if offset + 4 > data_len:
                raise ValueError(f"Truncated chunk: dict chunk {ci} length")
            chunk_len = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            if offset + chunk_len > data_len:
                raise ValueError(f"Truncated chunk: dict chunk {ci} data")
            dict_chunks.append(data[offset : offset + chunk_len])
            offset += chunk_len

    return ColumnChunkData(
        header=header,
        null_bitmap=null_bitmap,
        null_count=null_count,
        primary_zl_data=primary_zl_data,
        dict_chunk_zl_data=dict_chunks,
    )
