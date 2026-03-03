"""Read and write .zlvcf container files.

Container layout:
    "ZLVCF\\0\\0\\0" (8 bytes magic)
    version     (U32LE)
    num_entries (U32LE)
    [Entry directory: num_entries x (name_len U16LE, name bytes, compressed_size U64LE, original_size U64LE)]
    [Entry data: sequential blobs in directory order]
    checksum    (U32LE, CRC32 of everything from magic through last blob byte)
"""

import struct
import zlib
from pathlib import Path
from typing import Dict, Tuple

ZLVCF_MAGIC = b"ZLVCF\x00\x00\x00"
ZLVCF_VERSION = 1


class ZlvcfError(Exception):
    """Raised on container format errors."""


def create_zlvcf(
    output_path: Path,
    entries: Dict[str, Tuple[bytes, int]],
) -> Path:
    """Create a .zlvcf container.

    Args:
        output_path: Path for the output .zlvcf file.
        entries: Ordered dict mapping name -> (data_bytes, original_size).

    Returns:
        The output path.
    """
    header = ZLVCF_MAGIC + struct.pack("<II", ZLVCF_VERSION, len(entries))

    directory = bytearray()
    for name, (data, orig_size) in entries.items():
        name_bytes = name.encode("utf-8")
        directory += struct.pack("<H", len(name_bytes))
        directory += name_bytes
        directory += struct.pack("<QQ", len(data), orig_size)

    # Stream to disk with incremental CRC32
    crc = 0
    with open(output_path, "wb") as f:
        f.write(header)
        crc = zlib.crc32(header, crc)

        dir_bytes = bytes(directory)
        f.write(dir_bytes)
        crc = zlib.crc32(dir_bytes, crc)

        for _name, (data, _orig) in entries.items():
            f.write(data)
            crc = zlib.crc32(data, crc)

        f.write(struct.pack("<I", crc & 0xFFFFFFFF))

    return output_path


def extract_zlvcf(
    input_path: Path,
    output_dir: Path,
) -> Dict[str, Path]:
    """Extract a .zlvcf container.

    Verifies the CRC32 checksum. Writes each entry to output_dir/<name>.

    Args:
        input_path: Path to the .zlvcf file.
        output_dir: Directory to extract entries into.

    Returns:
        Dict mapping entry name -> extracted file path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(input_path, "rb") as f:
        all_data = f.read()

    if len(all_data) < 20:  # magic(8) + version(4) + num(4) + crc(4)
        raise ZlvcfError("File too small to be a valid .zlvcf container")

    if all_data[:8] != ZLVCF_MAGIC:
        raise ZlvcfError(
            f"Bad magic: expected {ZLVCF_MAGIC!r}, got {all_data[:8]!r}"
        )

    stored_crc = struct.unpack("<I", all_data[-4:])[0]
    payload = all_data[:-4]
    computed_crc = zlib.crc32(payload) & 0xFFFFFFFF
    if stored_crc != computed_crc:
        raise ZlvcfError(
            f"CRC32 mismatch: stored=0x{stored_crc:08X}, "
            f"computed=0x{computed_crc:08X}"
        )

    offset = 8
    version, num_entries = struct.unpack_from("<II", payload, offset)
    offset += 8

    if version != ZLVCF_VERSION:
        raise ZlvcfError(f"Unsupported version: {version}")

    entries_info = []
    for _ in range(num_entries):
        name_len = struct.unpack_from("<H", payload, offset)[0]
        offset += 2
        name = payload[offset:offset + name_len].decode("utf-8")
        offset += name_len
        compressed_size, original_size = struct.unpack_from("<QQ", payload, offset)
        offset += 16
        entries_info.append((name, compressed_size, original_size))

    result = {}
    for name, compressed_size, _original_size in entries_info:
        data = payload[offset:offset + compressed_size]
        offset += compressed_size

        if ".." in name or name.startswith("/"):
            raise ZlvcfError(f"Unsafe entry name: {name}")

        out_path = output_dir / name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(data)
        result[name] = out_path

    return result
