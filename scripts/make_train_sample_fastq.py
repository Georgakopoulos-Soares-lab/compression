#!/usr/bin/env python3
"""Create a ~N MiB FASTQ training sample by copying whole FASTQ records.

Important (FASTQ-specific):
- FASTQ records are exactly 4 lines: @header, sequence, +, quality.
- We never cut mid-record (same as FASTA). Cutting after line 2 would produce
  invalid FASTQ and break the preprocessor.
- We validate that line 3 starts with '+' and that quality length equals
  sequence length; malformed records are skipped so the output stays valid.
- After writing at least one record, we skip records that would push total
  size over the target (to stay near the target without overshooting).

The output is valid 4-line FASTQ and can be fed to the preprocessor.
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path


def _open_maybe_gz(path: Path):
    if path.suffix == ".gz" or path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="surrogateescape")
    return path.open("rt", encoding="utf-8", errors="surrogateescape")


def iter_fastq_records(path: Path):
    """Yield whole FASTQ records as (lines_tuple, raw_text). Supports 4-line and wrapped format."""
    with _open_maybe_gz(path) as f:
        while True:
            line1 = f.readline()
            if not line1:
                break
            if not line1.startswith("@"):
                continue
            # Sequence line(s): read until we see a line starting with '+'
            seq_lines = []
            while True:
                line = f.readline()
                if not line:
                    return
                if line.startswith("+"):
                    plus_line = line
                    break
                seq_lines.append(line)
            # Quality: same number of lines as sequence
            num_qual_lines = len(seq_lines)
            qual_lines = []
            for _ in range(num_qual_lines):
                line = f.readline()
                if not line:
                    return
                qual_lines.append(line)
            # Validate lengths (strip newlines for comparison)
            seq_stripped = [l.rstrip("\n\r") for l in seq_lines]
            qual_stripped = [l.rstrip("\n\r") for l in qual_lines]
            seq_len = sum(len(s) for s in seq_stripped)
            qual_len = sum(len(q) for q in qual_stripped)
            if seq_len != qual_len:
                continue  # skip malformed
            # Emit 4-line format for preprocessor: header, one seq line, plus, one qual line
            seq_line = "".join(seq_stripped) + "\n"
            qual_line = "".join(qual_stripped) + "\n"
            raw = line1 + seq_line + plus_line + qual_line
            yield (line1, seq_line, plus_line, qual_line), raw


def main() -> int:
    ap = argparse.ArgumentParser()
    repo_root = Path(__file__).resolve().parent.parent
    default_out = repo_root / "out" / "train_200MiB.fastq"
    ap.add_argument("--in", dest="inp", required=True, help="Input FASTQ (.fastq or .fastq.gz)")
    ap.add_argument("--out", dest="out", default=str(default_out), help=f"Output FASTQ sample (default: {default_out})")
    ap.add_argument(
        "--target-mib",
        type=int,
        default=200,
        help=(
            "Target size in MiB (approx). After writing at least 1 record, "
            "records that would exceed the target are skipped."
        ),
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = args.target_mib * 1024 * 1024

    if not inp.exists():
        raise SystemExit(f"Input file does not exist: {inp}")
    if not inp.is_file():
        raise SystemExit(f"Input is not a file (e.g. it is a directory): {inp}")

    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    records = 0

    with out.open("wt", encoding="utf-8", errors="surrogateescape") as w:
        for _lines, raw in iter_fastq_records(inp):
            record_bytes = len(raw.encode("utf-8", errors="surrogateescape"))

            if records > 0 and written + record_bytes > target_bytes:
                # Don't overshoot: skip this record (same logic as FASTA).
                continue

            w.write(raw)
            written += record_bytes
            records += 1

    if records == 0:
        raise SystemExit(
            "No valid FASTQ records found. Check that the input file exists, is not empty, "
            "and is valid FASTQ (4-line or wrapped). You can pass a .fastq.gz path directly."
        )
    print(f"Wrote records: {records}")
    print(f"Output bytes: {written}")
    print(f"Output path:  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
