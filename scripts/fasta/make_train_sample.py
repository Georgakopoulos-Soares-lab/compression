#!/usr/bin/env python3
"""Create a ~N MiB FASTA training sample by copying whole FASTA records.

Important:
- This does NOT cut mid-record (unlike `head -c`).
- To get *closer* to the target size on genomes with very large contigs, we
    **skip** records that would push us over the target once we have written at
    least one record.

The output is valid FASTA and can be fed to the preprocessor.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def iter_fasta_records(path: Path):
    header = None
    seq_lines = []
    with path.open("rt", encoding="utf-8", errors="surrogateescape") as f:
        for line in f:
            if line.startswith(">"):
                if header is not None:
                    yield header, seq_lines
                header = line
                seq_lines = []
            else:
                if header is None:
                    continue
                seq_lines.append(line)
        if header is not None:
            yield header, seq_lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input FASTA (.fna/.fa)")
    ap.add_argument("--out", dest="out", required=True, help="Output FASTA sample")
    ap.add_argument(
        "--target-mib",
        type=int,
        default=250,
        help=(
            "Target size in MiB (approx). After writing at least 1 record, "
            "records that would exceed the target are skipped."
        ),
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = args.target_mib * 1024 * 1024

    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    records = 0

    with out.open("wt", encoding="utf-8", errors="surrogateescape") as w:
        for header, seq_lines in iter_fasta_records(inp):
            record_text = header + "".join(seq_lines)
            record_bytes = len(record_text.encode("utf-8", errors="surrogateescape"))

            if records > 0 and written + record_bytes > target_bytes:
                continue

            w.write(record_text)
            written += record_bytes
            records += 1

    print(f"Wrote records: {records}")
    print(f"Output bytes: {written}")
    print(f"Output path:  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
