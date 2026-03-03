"""VCF codec: split VCF into header + body TSV chunks, and reassemble.

Encoding splits a VCF file into:
  - header.vcf   (all ## meta-information lines + #CHROM header line)
  - part_000.tsv, part_001.tsv, ...  (tab-delimited data rows, line-safe chunks)
  - meta.json    (manifest for reassembly: header size, part sizes, part count)

Decoding concatenates header + decompressed body parts back into the
original VCF (byte-identical).
"""

import json
import os
from pathlib import Path
from typing import List


# Target ~40 MiB per body chunk (fits comfortably in OpenZL's 500 MB limit).
_TARGET_PART_BYTES = 40 * 1024 * 1024


def encode(input_path: Path, output_dir: Path, *,
           target_part_bytes: int = _TARGET_PART_BYTES,
           verbose: bool = False) -> List[Path]:
    """Encode a VCF file into header + body TSV parts + meta.json.

    Args:
        input_path: Path to the input VCF file.
        output_dir: Directory to write the encoded files.
        target_part_bytes: Target size per body chunk in bytes.
        verbose: Print progress.

    Returns:
        List of all output file paths (header.vcf, part_*.tsv, meta.json).
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    input_size = input_path.stat().st_size

    # --- Pass 1: find the boundary between header and body ---
    # Header = all lines starting with '#' (## meta + #CHROM header).
    # We read in binary to preserve exact bytes (line endings, encoding).
    header_end = 0
    with open(input_path, "rb") as f:
        while True:
            line_start = f.tell()
            line = f.readline()
            if not line:
                break
            if line.startswith(b"#"):
                header_end = f.tell()
            else:
                break

    body_start = header_end
    body_size = input_size - body_start

    if verbose:
        print(f"  [vcf_codec] header: {header_end:,} bytes, "
              f"body: {body_size:,} bytes")

    # --- Write header.vcf ---
    header_path = output_dir / "header.vcf"
    with open(input_path, "rb") as fin, open(header_path, "wb") as fout:
        fout.write(fin.read(header_end))

    # --- Compute chunk boundaries (line-safe) ---
    num_parts = max(1, (body_size + target_part_bytes - 1) // target_part_bytes)

    # Build evenly-spaced target offsets, then snap each to next newline
    boundaries = [body_start]
    with open(input_path, "rb") as f:
        for i in range(1, num_parts):
            target = body_start + i * (body_size // num_parts)
            f.seek(target)
            # Read ahead to find next newline (snap to line boundary)
            remainder = f.readline()  # consume rest of current line
            boundaries.append(f.tell())
        boundaries.append(input_size)

    # Remove duplicates and sort
    boundaries = sorted(set(boundaries))

    # --- Write body parts ---
    output_files = [header_path]
    part_infos = []

    with open(input_path, "rb") as fin:
        for idx in range(len(boundaries) - 1):
            start = boundaries[idx]
            end = boundaries[idx + 1]
            chunk_size = end - start
            if chunk_size == 0:
                continue

            part_name = f"part_{idx:03d}.tsv"
            part_path = output_dir / part_name
            fin.seek(start)
            with open(part_path, "wb") as fout:
                fout.write(fin.read(chunk_size))

            part_infos.append({
                "file": part_name,
                "bytes": chunk_size,
            })
            output_files.append(part_path)

            if verbose:
                print(f"  [vcf_codec] {part_name}: {chunk_size:,} bytes")

    # --- Write meta.json ---
    meta = {
        "format": "nyx_vcf_header_body_v1",
        "input_name": input_path.name,
        "header_file": "header.vcf",
        "header_bytes": header_end,
        "body_bytes": body_size,
        "parts": part_infos,
    }
    meta_path = output_dir / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    output_files.append(meta_path)

    if verbose:
        print(f"  [vcf_codec] {len(part_infos)} part(s), "
              f"header {header_end:,} + body {body_size:,} bytes")

    return output_files


def decode(input_dir: Path, output_path: Path, *,
           verbose: bool = False) -> Path:
    """Decode header + body TSV parts back into original VCF.

    Args:
        input_dir: Directory containing header.vcf, part_*.tsv, meta.json.
        output_path: Path to write the reconstructed VCF.
        verbose: Print progress.

    Returns:
        The output path.
    """
    meta_path = input_dir / "meta.json"
    with open(meta_path) as f:
        meta = json.load(f)

    header_path = input_dir / meta["header_file"]

    with open(output_path, "wb") as fout:
        # Write header
        with open(header_path, "rb") as fin:
            data = fin.read()
            fout.write(data)
            if verbose:
                print(f"  [vcf_codec] header: {len(data):,} bytes")

        # Write body parts in order
        for part_info in meta["parts"]:
            part_path = input_dir / part_info["file"]
            with open(part_path, "rb") as fin:
                data = fin.read()
                fout.write(data)
                if verbose:
                    print(f"  [vcf_codec] {part_info['file']}: "
                          f"{len(data):,} bytes")

    if verbose:
        total = output_path.stat().st_size
        print(f"  [vcf_codec] reconstructed: {total:,} bytes")

    return output_path
