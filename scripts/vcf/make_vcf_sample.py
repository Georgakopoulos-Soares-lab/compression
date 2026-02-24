#!/usr/bin/env python3
"""Create a size-bounded VCF sample suitable for fast benchmarks.

Outputs:
  - <prefix>.meta.txt     (all leading ## meta lines)
  - <prefix>.table.tsv    (#CHROM header line + variant rows; tab-separated)

The script reads either a .vcf or .vcf.gz input and writes the output until the
reconstructed VCF (meta + table) reaches approximately --target-mib MiB.

This preserves a valid VCF header and keeps full rows (never splits a line).
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path
from typing import BinaryIO, TextIO


def open_text_auto(path: Path) -> TextIO:
    # Use text mode with universal newlines; VCF is ASCII-ish.
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open("rt", encoding="utf-8", errors="replace", newline="")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Input .vcf or .vcf.gz")
    ap.add_argument("--out-prefix", required=True, help="Output prefix path (no extension)")
    ap.add_argument("--target-mib", type=float, default=500.0, help="Target size in MiB (default: 500)")
    args = ap.parse_args()

    in_path = Path(args.input)
    out_prefix = Path(args.out_prefix)
    target_bytes = int(args.target_mib * 1024 * 1024)

    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    meta_path = Path(str(out_prefix) + ".meta.txt")
    table_path = Path(str(out_prefix) + ".table.tsv")

    meta_lines: list[str] = []
    header_line: str | None = None

    written_bytes = 0
    wrote_header = False

    with open_text_auto(in_path) as fin:
        # Read meta lines and column header
        for line in fin:
            if line.startswith("##"):
                meta_lines.append(line)
                continue
            if line.startswith("#CHROM") or line.startswith("#"):
                # The VCF column header line starts with #CHROM; tolerate weird leading '#'.
                header_line = line
                break
            # If we hit a data line before #CHROM, treat it as malformed.
            raise SystemExit("VCF appears malformed: expected header lines before data")

        if header_line is None:
            raise SystemExit("VCF appears malformed: missing #CHROM header line")

        # Write meta and table header.
        with meta_path.open("wt", encoding="utf-8", newline="") as fmeta:
            for ml in meta_lines:
                fmeta.write(ml)
        with table_path.open("wt", encoding="utf-8", newline="") as ftab:
            ftab.write(header_line)
            wrote_header = True

            # Count meta+header bytes toward the target.
            # Use UTF-8 byte count; input is effectively ASCII.
            written_bytes = sum(len(s.encode("utf-8", errors="replace")) for s in meta_lines)
            written_bytes += len(header_line.encode("utf-8", errors="replace"))

            # Write data rows until we reach target.
            for line in fin:
                if written_bytes >= target_bytes:
                    break
                # Skip any stray meta lines after the header (rare but possible).
                if line.startswith("##"):
                    continue
                b = len(line.encode("utf-8", errors="replace"))
                if written_bytes + b > target_bytes and written_bytes > 0:
                    break
                ftab.write(line)
                written_bytes += b

    if not wrote_header:
        raise SystemExit("internal error: did not write table header")

    # Print a short summary
    meta_bytes = meta_path.stat().st_size
    tab_bytes = table_path.stat().st_size
    total_bytes = meta_bytes + tab_bytes
    print(f"Wrote: {meta_path} ({meta_bytes/1024/1024:.2f} MiB)")
    print(f"Wrote: {table_path} ({tab_bytes/1024/1024:.2f} MiB)")
    print(f"Approx reconstructed VCF size: {total_bytes/1024/1024:.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
