#!/usr/bin/env python3
"""Extract and decompose GenericChunk binary data.

Reads length-prefixed binary files produced by collect_raw_chunks.py and
decomposes each GenericChunk into three forms:
  1. Full chunk (complete bytes)
  2. Protobuf header (parsed and decoded)
  3. Raw TLV payload (the DNS records, ready for preprocessing)

Also produces a combined TLV file and a manifest CSV.

Usage:
    # Extract from collected raw chunk files:
    python3 extract_generic_chunks.py \
        --input-dir /tmp/dns-raw \
        --output-dir /tmp/dns-extracted

    # Skip individual per-chunk files (only combined + manifest):
    python3 extract_generic_chunks.py \
        --input-dir /tmp/dns-raw \
        --output-dir /tmp/dns-extracted \
        --no-individual-files
"""

import argparse
import glob
import logging
import os
import struct
import sys

logger = logging.getLogger("chunk-extractor")


def parse_generic_chunk(data):
    """Parse a GenericChunk binary blob into header and TLV payload.

    GenericChunk wire format:
        [4B total-length]
        [1B compression-flag]
        [4B header-length]
        [header bytes (protobuf)]
        [4B data-length]
        [data bytes (TLV payload)]

    Returns:
        (compression_flag, header_bytes, tlv_payload) or None on parse error
    """
    if len(data) < 13:  # minimum: 4+1+4+0+4+0
        return None

    offset = 0

    # Total length
    total_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4

    # Compression flag
    compression_flag = data[offset]
    offset += 1

    # Header length
    header_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4

    if offset + header_length > len(data):
        return None

    # Header bytes (protobuf-encoded ChunkHeader)
    header_bytes = data[offset:offset + header_length]
    offset += header_length

    # Data length
    if offset + 4 > len(data):
        return None
    data_length = struct.unpack_from(">I", data, offset)[0]
    offset += 4

    if offset + data_length > len(data):
        return None

    # TLV payload
    tlv_payload = data[offset:offset + data_length]

    return compression_flag, header_bytes, tlv_payload


def decode_protobuf_header(header_bytes):
    """Best-effort decode of the ChunkHeader protobuf.

    ChunkHeader fields (from Chunk.proto):
        1: type (string)
        2: source (string)
        3: format (string)
        5: timeStamp (uint64)
        6: engineType (string)
        7: engineVersion (string)
        8: nodeID (string)
        9: hostname (string)
    """
    fields = {}
    offset = 0
    data = header_bytes

    while offset < len(data):
        if offset >= len(data):
            break

        # Protobuf varint tag
        tag_byte = data[offset]
        offset += 1
        field_number = tag_byte >> 3
        wire_type = tag_byte & 0x07

        if wire_type == 0:  # varint
            value = 0
            shift = 0
            while offset < len(data):
                b = data[offset]
                offset += 1
                value |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields[field_number] = value

        elif wire_type == 2:  # length-delimited (string/bytes)
            length = 0
            shift = 0
            while offset < len(data):
                b = data[offset]
                offset += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            if offset + length > len(data):
                break
            try:
                fields[field_number] = data[offset:offset + length].decode("utf-8")
            except UnicodeDecodeError:
                fields[field_number] = data[offset:offset + length].hex()
            offset += length

        else:
            # Unknown wire type — stop parsing
            break

    field_names = {
        1: "type", 2: "source", 3: "format",
        5: "timeStamp", 6: "engineType", 7: "engineVersion",
        8: "nodeID", 9: "hostname",
    }

    return {field_names.get(k, f"field_{k}"): v for k, v in fields.items()}


def count_tlv_records(tlv_payload):
    """Count the number of DNS TLV records in a payload.

    Each record starts with a 4-byte length prefix.
    """
    offset = 0
    count = 0
    while offset + 4 <= len(tlv_payload):
        record_length = struct.unpack_from(">I", tlv_payload, offset)[0]
        if record_length == 0 or offset + 4 + record_length > len(tlv_payload):
            break
        offset += 4 + record_length
        count += 1
    return count


def iter_messages_from_files(input_dir):
    """Read length-prefixed messages from .bin files produced by collect_raw_chunks.py."""
    files = sorted(glob.glob(os.path.join(input_dir, "*.bin")))
    if not files:
        logger.error("No .bin files found in %s", input_dir)
        sys.exit(1)
    logger.info("Reading from %d files in %s", len(files), input_dir)
    for path in files:
        logger.info("  Reading %s", os.path.basename(path))
        with open(path, "rb") as f:
            while True:
                len_bytes = f.read(4)
                if len(len_bytes) < 4:
                    break
                msg_len = struct.unpack(">I", len_bytes)[0]
                msg_data = f.read(msg_len)
                if len(msg_data) < msg_len:
                    break
                yield msg_data


def main():
    parser = argparse.ArgumentParser(
        description="Extract and decompose GenericChunks from collected binary files",
    )
    parser.add_argument(
        "--input-dir", required=True,
        help="Directory containing .bin files from collect_raw_chunks.py",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory to write extracted chunks",
    )
    parser.add_argument(
        "--no-individual-files", action="store_true",
        help="Skip writing per-chunk files (only write combined outputs)",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    out = args.output_dir
    os.makedirs(out, exist_ok=True)
    if not args.no_individual_files:
        os.makedirs(os.path.join(out, "full"), exist_ok=True)
        os.makedirs(os.path.join(out, "headers"), exist_ok=True)
        os.makedirs(os.path.join(out, "tlv"), exist_ok=True)

    combined_tlv_path = os.path.join(out, "combined_tlv_payloads.bin")
    manifest_path = os.path.join(out, "manifest.csv")
    combined_tlv = open(combined_tlv_path, "wb")
    manifest = open(manifest_path, "w")
    manifest.write(
        "chunk_num,full_size,header_size,tlv_size,compression_flag,"
        "record_count,type,source,format,nodeID\n"
    )

    chunk_num = 0
    total_full_bytes = 0
    total_header_bytes = 0
    total_tlv_bytes = 0
    total_records = 0
    parse_errors = 0

    try:
        for raw in iter_messages_from_files(args.input_dir):
            result = parse_generic_chunk(raw)

            if result is None:
                parse_errors += 1
                continue

            compression_flag, header_bytes, tlv_payload = result
            header_info = decode_protobuf_header(header_bytes)
            record_count = count_tlv_records(tlv_payload)

            chunk_num += 1
            total_full_bytes += len(raw)
            total_header_bytes += len(header_bytes)
            total_tlv_bytes += len(tlv_payload)
            total_records += record_count

            if not args.no_individual_files:
                name = f"chunk_{chunk_num:06d}"
                with open(os.path.join(out, "full", f"{name}.bin"), "wb") as f:
                    f.write(raw)
                with open(os.path.join(out, "headers", f"{name}.txt"), "w") as f:
                    for k, v in header_info.items():
                        f.write(f"{k}: {v}\n")
                    f.write(f"compression_flag: {compression_flag}\n")
                    f.write(f"header_size: {len(header_bytes)}\n")
                    f.write(f"tlv_size: {len(tlv_payload)}\n")
                    f.write(f"record_count: {record_count}\n")
                with open(os.path.join(out, "tlv", f"{name}.tlv"), "wb") as f:
                    f.write(tlv_payload)

            combined_tlv.write(tlv_payload)

            manifest.write(
                f"{chunk_num},{len(raw)},{len(header_bytes)},"
                f"{len(tlv_payload)},{compression_flag},{record_count},"
                f"{header_info.get('type', '')},"
                f"{header_info.get('source', '')},"
                f"{header_info.get('format', '')},"
                f"{header_info.get('nodeID', '')}\n"
            )

    finally:
        combined_tlv.close()
        manifest.close()

        logger.info("")
        logger.info("=== Extraction Complete ===")
        logger.info("Chunks:        %d", chunk_num)
        logger.info("Records:       %d", total_records)
        logger.info("Parse errors:  %d", parse_errors)
        logger.info("Full bytes:    %s (avg %s/chunk)",
                     _fmt(total_full_bytes),
                     _fmt(total_full_bytes // max(chunk_num, 1)))
        logger.info("Header bytes:  %s (avg %s/chunk)",
                     _fmt(total_header_bytes),
                     _fmt(total_header_bytes // max(chunk_num, 1)))
        logger.info("TLV bytes:     %s (avg %s/chunk)",
                     _fmt(total_tlv_bytes),
                     _fmt(total_tlv_bytes // max(chunk_num, 1)))
        logger.info("Avg records/chunk: %d",
                     total_records // max(chunk_num, 1))
        logger.info("")
        logger.info("Output:")
        logger.info("  %s", out)
        if not args.no_individual_files:
            logger.info("  full/     — complete GenericChunk bytes per chunk")
            logger.info("  headers/  — decoded protobuf headers per chunk")
            logger.info("  tlv/      — raw TLV payloads per chunk")
        logger.info("  combined_tlv_payloads.bin — all TLV concatenated (%s)",
                     _fmt(total_tlv_bytes))
        logger.info("  manifest.csv — per-chunk sizes and metadata")


def _fmt(n):
    if n < 1024:
        return f"{n} B"
    elif n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    elif n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    else:
        return f"{n / (1024 * 1024 * 1024):.2f} GB"


if __name__ == "__main__":
    main()
