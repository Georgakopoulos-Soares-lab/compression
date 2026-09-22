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
                    # Skip anything before first header (shouldn't happen in real FASTA)
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
            "records that would exceed the target are skipped (to stay near the target)."
        ),
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = args.target_mib * 1024 * 1024

    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    records = 0

    def enc_len(s: str) -> int:
        return len(s.encode("utf-8", errors="surrogateescape"))

    with out.open("wt", encoding="utf-8", errors="surrogateescape") as w:
        for header, seq_lines in iter_fasta_records(inp):
            record_text = header + "".join(seq_lines)
            record_bytes = enc_len(record_text)

            if records > 0 and written + record_bytes > target_bytes:
                # Don't overshoot: skip this record and look for smaller ones.
                continue

            if written + record_bytes > target_bytes:
                # A single record larger than the whole target (e.g. a plant
                # chromosome). Emit it truncated at a sequence-line boundary so
                # the sample stays near the target and, crucially, so the
                # preprocessed training chunk stays under OpenZL's per-file
                # training size limit. Still valid FASTA.
                budget = target_bytes - written - enc_len(header)
                w.write(header)
                acc = 0
                for ln in seq_lines:
                    lb = enc_len(ln)
                    if acc + lb > budget and acc > 0:
                        break
                    w.write(ln)
                    acc += lb
                if seq_lines and not seq_lines[-1].endswith("\n"):
                    w.write("\n")
                written += enc_len(header) + acc
                records += 1
                break

            w.write(record_text)
            written += record_bytes
            records += 1

    print(f"Wrote records: {records}")
    print(f"Output bytes: {written}")
    print(f"Output path:  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
