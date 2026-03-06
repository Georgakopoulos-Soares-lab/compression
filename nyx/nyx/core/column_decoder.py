"""Column decoding pipeline — reverses every encoding in column_encoder.py.

For each encoding type, provides a function that takes decompressed bytes
and returns the original values (numpy array or Python list).
"""

from __future__ import annotations

import socket
import struct
import uuid as _uuid

import numpy as np


def decode_raw(data: bytes, np_dtype: np.dtype, width: int) -> np.ndarray:
    """Reinterpret flat bytes as a numpy array of the given dtype."""
    return np.frombuffer(data, dtype=np_dtype)


def decode_delta(data: bytes, np_dtype: np.dtype, width: int) -> np.ndarray:
    """Reverse delta encoding: reinterpret as deltas, then cumulative sum."""
    deltas = np.frombuffer(data, dtype=np_dtype).copy()
    np.cumsum(deltas, out=deltas)
    return deltas


def decode_binary_convert(data: bytes, binary_type: str, element_size: int) -> list[str]:
    """Convert compact binary back to text strings."""
    converter = _BINARY_DECODERS[binary_type]
    num_elements = len(data) // element_size
    result = []
    for i in range(num_elements):
        chunk = data[i * element_size : (i + 1) * element_size]
        result.append(converter(chunk))
    return result


def decode_dictionary(
    index_data: bytes,
    dict_blob: bytes,
    index_dtype: np.dtype,
    num_unique: int,
) -> list:
    """Reverse dictionary encoding: look up each index in the dictionary."""
    indices = np.frombuffer(index_data, dtype=index_dtype)
    dictionary = deserialize_dict(dict_blob)
    if len(dictionary) != num_unique:
        raise ValueError(
            f"Dictionary size mismatch: got {len(dictionary)}, expected {num_unique}"
        )
    max_idx = int(indices.max()) if len(indices) > 0 else 0
    if max_idx >= len(dictionary):
        raise ValueError(
            f"Index {max_idx} out of range for dictionary of size {len(dictionary)}"
        )
    dict_arr = np.array(dictionary, dtype=object)
    return dict_arr[indices].tolist()


def deserialize_dict(data: bytes) -> list[str]:
    """Reverse of _serialise_dict: read length-prefixed UTF-8 entries."""
    result = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            break
        length = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        value = data[offset : offset + length].decode("utf-8")
        offset += length
        result.append(value)
    return result


# ------------------------------------------------------------------
# Binary decoders (reverse of _convert_* in column_encoder.py)
# ------------------------------------------------------------------

def _decode_uuid(data: bytes) -> str:
    return str(_uuid.UUID(bytes=data))


def _decode_ipv4(data: bytes) -> str:
    return socket.inet_ntoa(data)


def _decode_ipv6(data: bytes) -> str:
    return socket.inet_ntop(socket.AF_INET6, data)


def _decode_hex(data: bytes) -> str:
    return data.hex()


_BINARY_DECODERS = {
    "uuid": _decode_uuid,
    "ipv4": _decode_ipv4,
    "ipv6": _decode_ipv6,
    "hex": _decode_hex,
}


# ------------------------------------------------------------------
# Dtype helpers (mirrors column_encoder._arrow_type_to_profile)
# ------------------------------------------------------------------

INDEX_DTYPE_MAP = {
    "uint8": np.dtype("uint8"),
    "uint16": np.dtype("<u2"),
    "int32": np.dtype("<i4"),
}

PROFILE_TO_NP_DTYPE = {
    "serial": np.dtype("uint8"),
    "le-i16": np.dtype("<i2"),
    "le-i32": np.dtype("<i4"),
    "le-i64": np.dtype("<i8"),
}
