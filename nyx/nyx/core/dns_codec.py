"""DNS TSV codec: split DNS TSV into line-safe chunks, and reassemble.

Encoding splits a DNS TSV file (headerless, tab-delimited) into:
  - part_000.tsv, part_001.tsv, ...  (line-safe chunks, empty lines stripped)
  - meta.json    (manifest for reassembly)

Decoding concatenates parts back into the original file (byte-identical
modulo empty-line stripping).
"""

import json
from pathlib import Path
from typing import List


# Target ~40 MiB per chunk (fits comfortably in OpenZL's 500 MB limit).
_TARGET_PART_BYTES = 40 * 1024 * 1024


def encode(input_path: Path, output_dir: Path, *,
           target_part_bytes: int = _TARGET_PART_BYTES,
           verbose: bool = False) -> List[Path]:
    """Encode a DNS TSV file into line-safe chunks + meta.json.

    Empty lines (batch separators from nom-kafka-dump) are stripped because
    they break OpenZL's CSV lexer.  The stripped count is recorded in
    meta.json so decode can note the difference.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Pass 1: strip empty lines into a clean temp copy, compute size ---
    clean_path = output_dir / "_clean.tsv"
    empty_count = 0
    with open(input_path, "rb") as fin, open(clean_path, "wb") as fout:
        for line in fin:
            if line.strip() == b"":
                empty_count += 1
                continue
            fout.write(line)

    input_size = clean_path.stat().st_size

    if verbose:
        print(f"  [dns_codec] body: {input_size:,} bytes "
              f"({empty_count} empty lines stripped)")

    # --- Compute chunk boundaries (line-safe) ---
    num_parts = max(1, (input_size + target_part_bytes - 1) // target_part_bytes)

    boundaries = [0]
    with open(clean_path, "rb") as f:
        for i in range(1, num_parts):
            target = i * (input_size // num_parts)
            f.seek(target)
            f.readline()  # snap to next line boundary
            boundaries.append(f.tell())
        boundaries.append(input_size)

    boundaries = sorted(set(boundaries))

    # --- Write parts ---
    output_files = []
    part_infos = []

    with open(clean_path, "rb") as fin:
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

            part_infos.append({"file": part_name, "bytes": chunk_size})
            output_files.append(part_path)

            if verbose:
                print(f"  [dns_codec] {part_name}: {chunk_size:,} bytes")

    # Clean up temp file
    clean_path.unlink()

    # --- Write meta.json ---
    meta = {
        "format": "nyx_dns_tsv_v1",
        "input_name": input_path.name,
        "body_bytes": input_size,
        "empty_lines_stripped": empty_count,
        "parts": part_infos,
    }
    meta_path = output_dir / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    output_files.append(meta_path)

    if verbose:
        print(f"  [dns_codec] {len(part_infos)} part(s), "
              f"{input_size:,} bytes total")

    return output_files


def decode(input_dir: Path, output_path: Path, *,
           verbose: bool = False) -> Path:
    """Decode TSV parts back into a single file.

    Concatenates all parts in order.
    """
    meta_path = input_dir / "meta.json"
    with open(meta_path) as f:
        meta = json.load(f)

    with open(output_path, "wb") as fout:
        for part_info in meta["parts"]:
            part_path = input_dir / part_info["file"]
            with open(part_path, "rb") as fin:
                data = fin.read()
                fout.write(data)
                if verbose:
                    print(f"  [dns_codec] {part_info['file']}: "
                          f"{len(data):,} bytes")

    if verbose:
        total = output_path.stat().st_size
        stripped = meta.get("empty_lines_stripped", 0)
        print(f"  [dns_codec] reconstructed: {total:,} bytes"
              + (f" ({stripped} empty lines were stripped)" if stripped else ""))

    return output_path
