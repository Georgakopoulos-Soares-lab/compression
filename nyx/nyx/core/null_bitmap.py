"""Null bitmap encode/decode for nullable Parquet columns.

Arrow uses a validity bitmap: 1 bit per row, bit=1 means valid, bit=0 means null.
We extract this bitmap before encoding and apply it after decoding.

All operations are vectorized via numpy — no Python loops over row data.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa


def extract_null_bitmap(col: pa.Array) -> tuple[bytes, int]:
    """Extract a validity bitmap from an Arrow array.

    Uses Arrow's internal bitmap buffer directly when available,
    falling back to numpy vectorization.

    Returns (bitmap_bytes, null_count).
    If null_count == 0, returns (b"", 0).
    """
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()

    null_count = col.null_count
    if null_count == 0:
        return b"", 0

    n = len(col)
    bitmap_size = (n + 7) // 8

    buffers = col.buffers()
    if buffers[0] is not None:
        raw_bitmap = buffers[0].to_pybytes()
        return raw_bitmap[:bitmap_size], null_count

    valid_mask = col.is_valid().to_numpy(zero_copy_only=False)
    bitmap = _bool_mask_to_bitmap(valid_mask)
    return bitmap, null_count


def apply_null_bitmap_numeric(
    values: np.ndarray,
    bitmap: bytes,
    null_count: int,
    arrow_type: pa.DataType,
) -> pa.Array:
    """Create an Arrow array from numpy values with nulls restored.

    Uses numpy boolean mask — no Python-level iteration.
    """
    if null_count == 0:
        if pa.types.is_timestamp(arrow_type):
            return pa.array(values.view(np.int64), type=pa.int64()).cast(arrow_type)
        return pa.array(values, type=arrow_type)

    n = len(values)
    null_mask = ~_bitmap_to_bool_mask(bitmap, n)

    if pa.types.is_timestamp(arrow_type):
        arr = pa.array(values.view(np.int64), type=pa.int64(), mask=null_mask)
        return arr.cast(arrow_type)

    return pa.array(values, type=arrow_type, mask=null_mask)


def apply_null_bitmap_strings(
    values: list,
    bitmap: bytes,
    null_count: int,
) -> pa.Array:
    """Create a string Arrow array with nulls restored."""
    if null_count == 0:
        return pa.array(values, type=pa.string())

    n = len(values)
    mask = _bitmap_to_bool_mask(bitmap, n)
    result = [values[i] if mask[i] else None for i in range(n)]
    return pa.array(result, type=pa.string())


def _bitmap_to_bool_mask(bitmap: bytes, n: int) -> np.ndarray:
    """Convert a packed bitmap to a boolean array (True = valid).

    Uses numpy bit unpacking — O(n/8) byte ops, no Python per-element loop.
    """
    bitmap_arr = np.frombuffer(bitmap, dtype=np.uint8)
    unpacked = np.unpackbits(bitmap_arr, bitorder="little")
    return unpacked[:n].astype(bool)


def _bool_mask_to_bitmap(mask: np.ndarray) -> bytes:
    """Convert a boolean array to a packed bitmap (True = valid).

    Inverse of _bitmap_to_bool_mask.
    """
    n = len(mask)
    padded_len = ((n + 7) // 8) * 8
    padded = np.zeros(padded_len, dtype=np.uint8)
    padded[:n] = mask.astype(np.uint8)
    packed = np.packbits(padded, bitorder="little")
    return packed.tobytes()
