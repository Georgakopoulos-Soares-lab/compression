#!/usr/bin/env python3
"""Column encoding pipeline for OpenZL Parquet compression.

Provides a configurable encoding layer that preprocesses Parquet column data
before OpenZL compression.  Five encoding strategies are supported, selectable
via a string parameter:

    raw            – pass-through (fixed-width numerics)
    dictionary     – build lookup table, replace values with integer indices
    binary_convert – convert structured text (UUID/IP/hash) to compact binary
    delta          – store first value + differences (sorted/monotonic data)
    dict_delta     – dictionary encode, then delta-encode the index array
    auto           – analyse column stats and pick the best encoding

Usage:
    from column_encoder import ColumnEncoder
    enc = ColumnEncoder(zli_path="path/to/zli")
    result = enc.encode_and_compress(col, "col_name", col.type, "auto", "out/")
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import time
import uuid as _uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

VALID_ENCODINGS = ("raw", "dictionary", "binary_convert", "delta", "dict_delta", "auto")


@dataclass
class EncodingResult:
    column_name: str
    encoding: str
    profile: str
    raw_string_bytes: int = 0
    encoded_bytes: int = 0
    compressed_bytes: int = 0
    ratio_vs_raw: float = 0.0
    ratio_vs_encoded: float = 0.0
    compress_time_secs: float = 0.0
    zl_path: str = ""
    sidecar_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ColumnEncoder:
    """Configurable column encoder for OpenZL Parquet compression."""

    def __init__(self, zli_path: str | Path):
        self.zli = str(Path(zli_path).resolve())
        if not Path(self.zli).exists():
            raise FileNotFoundError(f"zli binary not found: {self.zli}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def encode_and_compress(
        self,
        column_data: pa.Array | pa.ChunkedArray,
        column_name: str,
        column_type: pa.DataType,
        encoding: str = "auto",
        output_dir: str | Path = ".",
        compress_timeout: int = 600,
    ) -> EncodingResult:
        """Encode a column and compress it with OpenZL.

        Parameters
        ----------
        column_data : Arrow array (possibly chunked)
        column_name : name used for output file prefixes
        column_type : Arrow data type of the column
        encoding    : one of VALID_ENCODINGS
        output_dir  : directory for output .bin / .zl files
        compress_timeout : seconds before killing zli
        """
        if encoding not in VALID_ENCODINGS:
            raise ValueError(f"Unknown encoding '{encoding}'. Choose from {VALID_ENCODINGS}")

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        if isinstance(column_data, pa.ChunkedArray):
            column_data = column_data.combine_chunks()

        if encoding == "auto":
            encoding = self._auto_detect(column_data, column_type)

        dispatch = {
            "raw": self._encode_raw,
            "dictionary": self._encode_dictionary,
            "binary_convert": self._encode_binary_convert,
            "delta": self._encode_delta,
            "dict_delta": self._encode_dict_delta,
        }

        bin_path, profile, raw_string_bytes, encoded_bytes, sidecar_paths, meta = dispatch[encoding](
            column_data, column_type, column_name, out
        )

        zl_path = out / f"{column_name}_{encoding}.zl"
        compressed_bytes, comp_time = self._compress(bin_path, profile, zl_path, compress_timeout)

        ratio_raw = raw_string_bytes / compressed_bytes if compressed_bytes > 0 else 0
        ratio_enc = encoded_bytes / compressed_bytes if compressed_bytes > 0 else 0

        return EncodingResult(
            column_name=column_name,
            encoding=encoding,
            profile=profile,
            raw_string_bytes=raw_string_bytes,
            encoded_bytes=encoded_bytes,
            compressed_bytes=compressed_bytes,
            ratio_vs_raw=round(ratio_raw, 3),
            ratio_vs_encoded=round(ratio_enc, 3),
            compress_time_secs=round(comp_time, 1),
            zl_path=str(zl_path),
            sidecar_paths=[str(p) for p in sidecar_paths],
            metadata=meta,
        )

    # ------------------------------------------------------------------
    # Auto-detect
    # ------------------------------------------------------------------

    def _auto_detect(self, col: pa.Array, dtype: pa.DataType) -> str:
        if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
            return self._auto_detect_string(col)
        if pa.types.is_integer(dtype) or pa.types.is_timestamp(dtype):
            return self._auto_detect_numeric(col, dtype)
        return "raw"

    def _auto_detect_string(self, col: pa.Array) -> str:
        n = len(col)
        if n == 0:
            return "raw"

        unique_count = pc.count_distinct(col).as_py()

        sample_size = min(100, n)
        sample = [col[i].as_py() for i in range(sample_size) if col[i].is_valid]
        if not sample:
            return "raw"

        bin_type = _detect_binary_type(sample)
        if bin_type is not None:
            return "binary_convert"

        if unique_count <= 65535:
            if _is_sorted(col):
                return "dict_delta"
            return "dictionary"

        if unique_count <= n // 2:
            return "dictionary"

        return "raw"

    def _auto_detect_numeric(self, col: pa.Array, dtype: pa.DataType) -> str:
        if _is_sorted(col):
            return "delta"
        return "raw"

    # ------------------------------------------------------------------
    # Encoding: raw
    # ------------------------------------------------------------------

    def _encode_raw(self, col, dtype, name, out):
        profile, np_dtype, width = _arrow_type_to_profile(dtype)
        if profile is None:
            if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
                buf = bytearray()
                for v in col.to_pylist():
                    s = (v or "").encode("utf-8")
                    buf.extend(struct.pack("<I", len(s)))
                    buf.extend(s)
                raw_bytes = len(buf)
                bin_path = out / f"{name}_raw.bin"
                bin_path.write_bytes(buf)
                return bin_path, "serial", raw_bytes, raw_bytes, [], {}
            raise ValueError(f"Cannot raw-encode type {dtype}")

        arr = _extract_numeric(col, dtype, np_dtype)
        raw_bytes = len(arr) * arr.itemsize
        bin_path = out / f"{name}_raw.bin"
        arr.tofile(str(bin_path))
        return bin_path, profile, raw_bytes, raw_bytes, [], {"width": width}

    # ------------------------------------------------------------------
    # Encoding: dictionary
    # ------------------------------------------------------------------

    def _encode_dictionary(self, col, dtype, name, out):
        values = col.to_pylist()
        raw_bytes = _string_raw_bytes(values)

        unique_sorted = sorted(set(v for v in values if v is not None))
        dict_map = {v: i for i, v in enumerate(unique_sorted)}
        num_unique = len(unique_sorted)

        if num_unique <= 255:
            idx_dtype, idx_profile = np.uint8, "serial"
        elif num_unique <= 65535:
            idx_dtype, idx_profile = np.uint16, "le-i16"
        else:
            idx_dtype, idx_profile = np.int32, "le-i32"

        indices = np.array([dict_map.get(v, 0) for v in values], dtype=idx_dtype)
        idx_path = out / f"{name}_dict_indices.bin"
        indices.tofile(str(idx_path))

        dict_buf = _serialise_dict(unique_sorted)
        dict_path = out / f"{name}_dict_values.bin"
        dict_path.write_bytes(dict_buf)

        encoded_bytes = indices.nbytes + len(dict_buf)
        meta = {"num_unique": num_unique, "index_dtype": str(idx_dtype.__name__), "dict_size": len(dict_buf)}
        return idx_path, idx_profile, raw_bytes, encoded_bytes, [dict_path], meta

    # ------------------------------------------------------------------
    # Encoding: binary_convert
    # ------------------------------------------------------------------

    def _encode_binary_convert(self, col, dtype, name, out):
        values = col.to_pylist()
        raw_bytes = _string_raw_bytes(values)

        sample = [v for v in values[:200] if v is not None]
        bin_type = _detect_binary_type(sample)
        if bin_type is None:
            raise ValueError(f"Column '{name}' does not match any binary_convert pattern (UUID/IPv4/IPv6/hex)")

        converter, elem_size, profile = _BINARY_CONVERTERS[bin_type]

        bin_buf = bytearray()
        for v in values:
            if v is None:
                bin_buf.extend(b"\x00" * elem_size)
            else:
                bin_buf.extend(converter(v))

        bin_path = out / f"{name}_binary.bin"
        bin_path.write_bytes(bin_buf)
        encoded_bytes = len(bin_buf)

        meta = {"binary_type": bin_type, "element_size": elem_size}
        return bin_path, profile, raw_bytes, encoded_bytes, [], meta

    # ------------------------------------------------------------------
    # Encoding: delta
    # ------------------------------------------------------------------

    def _encode_delta(self, col, dtype, name, out):
        profile, np_dtype, width = _arrow_type_to_profile(dtype)
        if profile is None:
            raise ValueError(f"Delta encoding requires a numeric column, got {dtype}")

        arr = _extract_numeric(col, dtype, np_dtype)
        raw_bytes = arr.nbytes

        deltas = np.empty_like(arr)
        deltas[0] = arr[0]
        deltas[1:] = np.diff(arr)

        delta_path = out / f"{name}_delta.bin"
        deltas.tofile(str(delta_path))

        meta = {"base_value": int(arr[0]), "is_sorted": bool(np.all(deltas[1:] >= 0))}
        return delta_path, profile, raw_bytes, raw_bytes, [], meta

    # ------------------------------------------------------------------
    # Encoding: dict_delta
    # ------------------------------------------------------------------

    def _encode_dict_delta(self, col, dtype, name, out):
        values = col.to_pylist()
        raw_bytes = _string_raw_bytes(values)

        unique_sorted = sorted(set(v for v in values if v is not None))
        dict_map = {v: i for i, v in enumerate(unique_sorted)}
        num_unique = len(unique_sorted)

        if num_unique <= 255:
            idx_dtype, idx_profile = np.uint8, "serial"
        elif num_unique <= 65535:
            idx_dtype, idx_profile = np.uint16, "le-i16"
        else:
            idx_dtype, idx_profile = np.int32, "le-i32"

        indices = np.array([dict_map.get(v, 0) for v in values], dtype=idx_dtype)

        deltas = np.empty_like(indices)
        deltas[0] = indices[0]
        deltas[1:] = np.diff(indices.astype(np.int64)).astype(idx_dtype)

        delta_path = out / f"{name}_dict_delta_indices.bin"
        deltas.tofile(str(delta_path))

        dict_buf = _serialise_dict(unique_sorted)
        dict_path = out / f"{name}_dict_values.bin"
        dict_path.write_bytes(dict_buf)

        encoded_bytes = deltas.nbytes + len(dict_buf)
        meta = {"num_unique": num_unique, "index_dtype": str(idx_dtype.__name__), "dict_size": len(dict_buf)}
        return delta_path, idx_profile, raw_bytes, encoded_bytes, [dict_path], meta

    # ------------------------------------------------------------------
    # Compression
    # ------------------------------------------------------------------

    def _compress(self, bin_path: Path, profile: str, zl_path: Path, timeout: int) -> tuple[int, float]:
        cmd = [
            self.zli, "compress",
            str(bin_path.resolve()),
            "--profile", profile,
            "--train-inline",
            "--output", str(zl_path.resolve()),
            "--force",
        ]
        t0 = time.time()
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - t0

        if result.returncode != 0:
            raise RuntimeError(
                f"zli compress failed (rc={result.returncode}):\n{result.stderr[:500]}"
            )

        return zl_path.stat().st_size, elapsed


# ======================================================================
# Helpers
# ======================================================================

def _arrow_type_to_profile(dtype: pa.DataType):
    """Return (profile, numpy_dtype, width) for a given Arrow type."""
    if dtype == pa.int16():
        return "le-i16", np.dtype("<i2"), 2
    if dtype in (pa.int32(), pa.float32()):
        dt = "<i4" if dtype == pa.int32() else "<f4"
        return "le-i32", np.dtype(dt), 4
    if dtype in (pa.int64(), pa.float64()):
        dt = "<i8" if dtype == pa.int64() else "<f8"
        return "le-i64", np.dtype(dt), 8
    if pa.types.is_timestamp(dtype):
        return "le-i64", np.dtype("<i8"), 8
    if dtype == pa.bool_():
        return "serial", np.dtype("uint8"), 1
    if dtype == pa.uint8():
        return "serial", np.dtype("uint8"), 1
    if dtype == pa.uint16():
        return "le-i16", np.dtype("<u2"), 2
    if dtype == pa.uint32():
        return "le-i32", np.dtype("<u4"), 4
    if dtype == pa.uint64():
        return "le-i64", np.dtype("<u8"), 8
    return None, None, None


def _extract_numeric(col: pa.Array, dtype: pa.DataType, np_dtype) -> np.ndarray:
    """Extract an Arrow column as a contiguous numpy array."""
    if pa.types.is_boolean(dtype):
        filled = pc.fill_null(col, False)
        return filled.to_numpy(zero_copy_only=False).astype(np.uint8)
    if pa.types.is_timestamp(dtype):
        casted = col.cast(pa.int64())
        filled = pc.fill_null(casted, 0)
        return filled.to_numpy(zero_copy_only=False).astype(np_dtype)
    if pa.types.is_floating(dtype):
        return col.to_numpy(zero_copy_only=False).astype(np_dtype)
    scalar_zero = pa.scalar(0, type=dtype)
    filled = pc.fill_null(col, scalar_zero)
    return filled.to_numpy(zero_copy_only=False).astype(np_dtype)


def _string_raw_bytes(values: list) -> int:
    """Total bytes of raw string data (UTF-8 + 4-byte length prefix per value)."""
    total = 0
    for v in values:
        s = (v or "").encode("utf-8")
        total += 4 + len(s)
    return total


def _serialise_dict(unique_values: list) -> bytes:
    """Serialise a dictionary as length-prefixed entries."""
    buf = bytearray()
    for v in unique_values:
        s = str(v).encode("utf-8")
        buf.extend(struct.pack("<I", len(s)))
        buf.extend(s)
    return bytes(buf)


def _is_sorted(col: pa.Array) -> bool:
    """Check if an Arrow array is sorted (ascending)."""
    try:
        n = len(col)
        if n <= 1:
            return True
        sample_size = min(10000, n)
        step = max(1, n // sample_size)
        prev = None
        for i in range(0, n, step):
            val = col[i].as_py()
            if val is None:
                continue
            if prev is not None and val < prev:
                return False
            prev = val
        return True
    except (TypeError, pa.ArrowInvalid):
        return False


# ------------------------------------------------------------------
# Binary conversion helpers
# ------------------------------------------------------------------

def _convert_uuid(s: str) -> bytes:
    return _uuid.UUID(s).bytes


def _convert_ipv4(s: str) -> bytes:
    return socket.inet_aton(s)


def _convert_ipv6(s: str) -> bytes:
    return socket.inet_pton(socket.AF_INET6, s)


def _convert_hex(s: str) -> bytes:
    return bytes.fromhex(s)


def _detect_binary_type(sample: list[str]) -> str | None:
    """Detect if sample strings are UUID, IPv4, IPv6, or hex hashes."""
    if not sample:
        return None

    checks = [
        ("uuid", _try_uuid),
        ("ipv4", _try_ipv4),
        ("ipv6", _try_ipv6),
        ("hex", _try_hex),
    ]
    for name, checker in checks:
        successes = sum(1 for s in sample if checker(s))
        if successes >= len(sample) * 0.95:
            return name
    return None


def _try_uuid(s: str) -> bool:
    try:
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _try_ipv4(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return len(s) <= 15 and "." in s
    except (OSError, TypeError):
        return False


def _try_ipv6(s: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET6, s)
        return True
    except (OSError, TypeError):
        return False


def _try_hex(s: str) -> bool:
    try:
        if len(s) < 16 or len(s) % 2 != 0:
            return False
        bytes.fromhex(s)
        return True
    except (ValueError, TypeError):
        return False


_BINARY_CONVERTERS = {
    "uuid":  (_convert_uuid,  16, "le-i64"),
    "ipv4":  (_convert_ipv4,   4, "le-i32"),
    "ipv6":  (_convert_ipv6,  16, "le-i64"),
    "hex":   (_convert_hex,   32, "le-i64"),
}
