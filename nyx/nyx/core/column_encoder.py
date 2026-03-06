#!/usr/bin/env python3
"""Column encoding pipeline for OpenZL Parquet compression.

Deterministic workflow that auto-detects the best encoding for any Parquet
column, then compresses it entirely with OpenZL (no zstd anywhere).

Decision tree:
    Numeric column:
      ├── Sorted/monotonic → delta encode + LE profile
      └── Otherwise        → raw LE profile
    String column:
      ├── Binary-convertible (UUID/IP/hex) → binary_convert + LE profile
      └── Otherwise → dictionary encode + OpenZL (indices: LE, dict blob: serial chunked)

Usage:
    from column_encoder import ColumnEncoder
    enc = ColumnEncoder(zli_path="path/to/zli")
    result = enc.encode_and_compress(col, "col_name", col.type, "auto", "out/")
"""

from __future__ import annotations

import socket
import struct
import subprocess
import time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

VALID_ENCODINGS = ("raw", "dictionary", "binary_convert", "delta", "auto")

DICT_CHUNK_SIZE = 1_000_000  # 1 MiB chunks for dictionary blob compression


@dataclass
class EncodingResult:
    column_name: str
    encoding: str
    profile: str
    raw_string_bytes: int = 0
    encoded_bytes: int = 0
    compressed_bytes: int = 0
    ratio_vs_raw: float = 0.0
    compress_time_secs: float = 0.0
    zl_path: str = ""
    sidecar_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ColumnEncoder:
    """Deterministic column encoder for OpenZL Parquet compression.

    Automatically selects the best encoding strategy for any column type,
    then compresses entirely with OpenZL — no zstd, no external codecs.
    """

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
        if encoding not in VALID_ENCODINGS:
            raise ValueError(f"Unknown encoding '{encoding}'. Choose from {VALID_ENCODINGS}")

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        if isinstance(column_data, pa.ChunkedArray):
            column_data = column_data.combine_chunks()

        if encoding == "auto":
            encoding = self._auto_detect(column_data, column_type)

        pipelines = {
            "raw": self._pipeline_raw,
            "delta": self._pipeline_delta,
            "binary_convert": self._pipeline_binary_convert,
            "dictionary": self._pipeline_dictionary,
        }

        return pipelines[encoding](
            column_data, column_type, column_name, out, compress_timeout
        )

    # ------------------------------------------------------------------
    # Auto-detect
    # ------------------------------------------------------------------

    def _auto_detect(self, col: pa.Array, dtype: pa.DataType) -> str:
        """Deterministic encoding selection for any column.

        Numeric:  sorted → delta, otherwise → raw
        String:   binary pattern → binary_convert, otherwise → dictionary
        """
        if pa.types.is_string(dtype) or pa.types.is_large_string(dtype):
            return self._auto_detect_string(col)
        if (pa.types.is_integer(dtype) or pa.types.is_floating(dtype)
                or pa.types.is_timestamp(dtype) or pa.types.is_boolean(dtype)):
            return self._auto_detect_numeric(col, dtype)
        return "raw"

    def _auto_detect_string(self, col: pa.Array) -> str:
        n = len(col)
        if n == 0:
            return "dictionary"

        sample = [col[i].as_py() for i in range(min(200, n)) if col[i].is_valid]
        if not sample:
            return "dictionary"

        if _detect_binary_type(sample) is not None:
            return "binary_convert"

        return "dictionary"

    def _auto_detect_numeric(self, col: pa.Array, dtype: pa.DataType) -> str:
        if _is_sorted(col):
            return "delta"
        return "raw"

    # ------------------------------------------------------------------
    # Pipeline: raw (numeric pass-through)
    # ------------------------------------------------------------------

    def _pipeline_raw(self, col, dtype, name, out, timeout) -> EncodingResult:
        profile, np_dtype, width = _arrow_type_to_profile(dtype)
        if profile is None:
            raise ValueError(f"Cannot raw-encode type {dtype}")

        arr = _extract_numeric(col, dtype, np_dtype)
        raw_bytes = arr.nbytes
        bin_path = out / f"{name}_raw.bin"
        arr.tofile(str(bin_path))

        zl_path = out / f"{name}_raw.zl"
        zl_size, elapsed = self._compress(bin_path, profile, zl_path, timeout)

        return EncodingResult(
            column_name=name, encoding="raw", profile=profile,
            raw_string_bytes=raw_bytes, encoded_bytes=raw_bytes,
            compressed_bytes=zl_size,
            ratio_vs_raw=round(raw_bytes / zl_size, 3) if zl_size > 0 else 0,
            compress_time_secs=round(elapsed, 1),
            zl_path=str(zl_path),
            metadata={"width": width},
        )

    # ------------------------------------------------------------------
    # Pipeline: delta (sorted/monotonic numerics)
    # ------------------------------------------------------------------

    def _pipeline_delta(self, col, dtype, name, out, timeout) -> EncodingResult:
        profile, np_dtype, width = _arrow_type_to_profile(dtype)
        if profile is None:
            raise ValueError(f"Delta encoding requires a numeric column, got {dtype}")

        arr = _extract_numeric(col, dtype, np_dtype)
        raw_bytes = arr.nbytes

        deltas = np.empty_like(arr)
        deltas[0] = arr[0]
        deltas[1:] = np.diff(arr)

        bin_path = out / f"{name}_delta.bin"
        deltas.tofile(str(bin_path))

        zl_path = out / f"{name}_delta.zl"
        zl_size, elapsed = self._compress(bin_path, profile, zl_path, timeout)

        return EncodingResult(
            column_name=name, encoding="delta", profile=profile,
            raw_string_bytes=raw_bytes, encoded_bytes=raw_bytes,
            compressed_bytes=zl_size,
            ratio_vs_raw=round(raw_bytes / zl_size, 3) if zl_size > 0 else 0,
            compress_time_secs=round(elapsed, 1),
            zl_path=str(zl_path),
            metadata={"base_value": int(arr[0]), "is_sorted": bool(np.all(deltas[1:] >= 0))},
        )

    # ------------------------------------------------------------------
    # Pipeline: binary_convert (UUID/IP/hex → compact binary)
    # ------------------------------------------------------------------

    def _pipeline_binary_convert(self, col, dtype, name, out, timeout) -> EncodingResult:
        values = col.to_pylist()
        raw_bytes = _string_raw_bytes(values)

        sample = [v for v in values[:200] if v is not None]
        bin_type = _detect_binary_type(sample)
        if bin_type is None:
            raise ValueError(f"Column '{name}' has no detectable binary pattern")

        if bin_type == "hex":
            converter, elem_size, profile = _get_hex_converter(sample)
        else:
            converter, elem_size, profile = _BINARY_CONVERTERS[bin_type]

        bin_buf = bytearray()
        for v in values:
            if v is None:
                bin_buf.extend(b"\x00" * elem_size)
            else:
                bin_buf.extend(converter(v))

        bin_path = out / f"{name}_binary.bin"
        bin_path.write_bytes(bin_buf)

        zl_path = out / f"{name}_binary_convert.zl"
        zl_size, elapsed = self._compress(bin_path, profile, zl_path, timeout)

        return EncodingResult(
            column_name=name, encoding="binary_convert", profile=profile,
            raw_string_bytes=raw_bytes, encoded_bytes=len(bin_buf),
            compressed_bytes=zl_size,
            ratio_vs_raw=round(raw_bytes / zl_size, 3) if zl_size > 0 else 0,
            compress_time_secs=round(elapsed, 1),
            zl_path=str(zl_path),
            metadata={"binary_type": bin_type, "element_size": elem_size},
        )

    # ------------------------------------------------------------------
    # Pipeline: dictionary (universal string compression)
    #
    # 1. Build dictionary of unique values + integer index array
    # 2. Compress index array with OpenZL LE profile
    # 3. Compress dictionary blob with OpenZL serial (1 MiB chunks)
    # Total compressed = index .zl + dictionary chunk .zl files
    # ------------------------------------------------------------------

    def _pipeline_dictionary(self, col, dtype, name, out, timeout) -> EncodingResult:
        values = col.to_pylist()
        raw_bytes = _string_raw_bytes(values)

        unique_sorted = sorted(set(v for v in values if v is not None))
        dict_map = {v: i for i, v in enumerate(unique_sorted)}
        num_unique = len(unique_sorted)

        if num_unique <= 255:
            idx_np_dtype, idx_profile = np.uint8, "serial"
        elif num_unique <= 65535:
            idx_np_dtype, idx_profile = np.uint16, "le-i16"
        else:
            idx_np_dtype, idx_profile = np.int32, "le-i32"

        indices = np.array([dict_map.get(v, 0) for v in values], dtype=idx_np_dtype)

        dict_buf = _serialise_dict(unique_sorted)

        idx_path = out / f"{name}_dict_indices.bin"
        indices.tofile(str(idx_path))

        idx_zl_path = out / f"{name}_dict_indices.zl"
        idx_zl_size, idx_time = self._compress(idx_path, idx_profile, idx_zl_path, timeout)

        dict_zl_size, dict_time, dict_zl_paths = self._compress_dict_blob(
            dict_buf, name, out, timeout
        )

        total_compressed = idx_zl_size + dict_zl_size
        total_time = idx_time + dict_time
        encoded_bytes = indices.nbytes + len(dict_buf)

        all_zl_paths = [str(idx_zl_path)] + [str(p) for p in dict_zl_paths]

        return EncodingResult(
            column_name=name, encoding="dictionary", profile=idx_profile,
            raw_string_bytes=raw_bytes, encoded_bytes=encoded_bytes,
            compressed_bytes=total_compressed,
            ratio_vs_raw=round(raw_bytes / total_compressed, 3) if total_compressed > 0 else 0,
            compress_time_secs=round(total_time, 1),
            zl_path=str(idx_zl_path),
            sidecar_paths=all_zl_paths[1:],
            metadata={
                "num_unique": num_unique,
                "index_dtype": idx_np_dtype.__name__,
                "dict_raw_bytes": len(dict_buf),
                "dict_compressed_bytes": dict_zl_size,
                "index_compressed_bytes": idx_zl_size,
                "dict_chunks": len(dict_zl_paths),
            },
        )

    def _compress_dict_blob(
        self, dict_buf: bytes, name: str, out: Path, timeout: int
    ) -> tuple[int, float, list[Path]]:
        """Compress a dictionary blob with OpenZL serial in 1 MiB chunks."""
        num_chunks = max(1, (len(dict_buf) + DICT_CHUNK_SIZE - 1) // DICT_CHUNK_SIZE)
        total_size = 0
        total_time = 0.0
        zl_paths = []

        for ci in range(num_chunks):
            start = ci * DICT_CHUNK_SIZE
            end = min(start + DICT_CHUNK_SIZE, len(dict_buf))
            chunk = dict_buf[start:end]

            chunk_bin = out / f"{name}_dict_chunk{ci:03d}.bin"
            chunk_bin.write_bytes(chunk)

            chunk_zl = out / f"{name}_dict_chunk{ci:03d}.zl"
            sz, t = self._compress(chunk_bin, "serial", chunk_zl, timeout)
            total_size += sz
            total_time += t
            zl_paths.append(chunk_zl)

        return total_size, total_time, zl_paths

    # ------------------------------------------------------------------
    # Compression (single file)
    # ------------------------------------------------------------------

    def _compress(
        self,
        bin_path: Path,
        profile: str,
        zl_path: Path,
        timeout: int,
    ) -> tuple[int, float]:
        """Compress a binary file with OpenZL inline training.

        Trains on the input data and compresses in a single step.
        This guarantees the compressor is optimal for the specific data.
        """
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
    """Map Arrow data type → (OpenZL profile, numpy dtype, byte width)."""
    if dtype == pa.int8():
        return "serial", np.dtype("int8"), 1
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
    """Extract an Arrow column as a contiguous little-endian numpy array.

    Fills nulls with zero-sentinel before conversion. The null bitmap
    is handled separately by the pipeline; here we just need valid bytes.
    """
    if pa.types.is_boolean(dtype):
        filled = pc.fill_null(col, False)
        return filled.to_numpy(zero_copy_only=False).astype(np.uint8)
    if pa.types.is_timestamp(dtype):
        casted = col.cast(pa.int64())
        filled = pc.fill_null(casted, 0)
        return filled.to_numpy(zero_copy_only=False).astype(np_dtype)
    if pa.types.is_floating(dtype):
        filled = pc.fill_null(col, 0.0)
        return filled.to_numpy(zero_copy_only=False).astype(np_dtype)
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
    """Serialise a dictionary as length-prefixed UTF-8 entries."""
    buf = bytearray()
    for v in unique_values:
        s = str(v).encode("utf-8")
        buf.extend(struct.pack("<I", len(s)))
        buf.extend(s)
    return bytes(buf)


def _is_sorted(col: pa.Array) -> bool:
    """Check if an Arrow array is sorted ascending (sampled)."""
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
    """Detect if sample strings are UUID, IPv4, IPv6, or hex hashes.

    Returns the type name if ≥95% of samples match, None otherwise.
    """
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
    "hex":   None,
}


def _get_hex_converter(sample: list[str]) -> tuple:
    """Determine hex element size from sample and return converter tuple."""
    valid = [s for s in sample if s is not None]
    if not valid:
        raise ValueError("No valid hex samples")
    hex_len = len(valid[0])
    elem_size = hex_len // 2
    if elem_size <= 4:
        profile = "le-i32"
    elif elem_size <= 8:
        profile = "le-i64"
    else:
        profile = "le-i64"
    return _convert_hex, elem_size, profile
