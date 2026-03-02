#!/usr/bin/env python3
"""Create a ~N MiB VCF training sample by copying whole VCF records.

Header lines (starting with '#') are always written verbatim so the output
is a valid VCF file that can be fed to vcf_preprocessor.

Variant records are copied until the target size is reached.  Records that
would push the total over the target are skipped once at least one record
has been written (same semantics as make_train_sample.py for FASTA).
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in",  dest="inp", required=True, help="Input VCF file")
    ap.add_argument("--out", dest="out", required=True, help="Output VCF sample")
    ap.add_argument(
        "--target-mib",
        type=int,
        default=200,
        help="Target size in MiB (approx). Default: 200",
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = args.target_mib * 1024 * 1024

    out.parent.mkdir(parents=True, exist_ok=True)

    written_payload = 0   # bytes of variant records written
    records = 0

    with inp.open("rt", encoding="utf-8", errors="surrogateescape") as src, \
         out.open("wt", encoding="utf-8", errors="surrogateescape") as dst:
        for line in src:
            if line.startswith("#"):
                # Always copy header lines
                dst.write(line)
                continue

            record_bytes = len(line.encode("utf-8", errors="surrogateescape"))

            if records > 0 and written_payload + record_bytes > target_bytes:
                continue

            dst.write(line)
            written_payload += record_bytes
            records += 1

    print(f"Wrote records : {records}")
    print(f"Payload bytes : {written_payload}")
    print(f"Output        : {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
