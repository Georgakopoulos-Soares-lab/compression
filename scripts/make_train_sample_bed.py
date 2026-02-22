#!/usr/bin/env python3
"""Create a ~N MiB BED training sample by copying whole lines (record-safe).

Does not cut mid-line. Stops after writing at least one line when adding the
next line would exceed the target size. Output is valid BED and can be fed
to the preprocessor.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input BED (.bed or .bed.gz)")
    ap.add_argument("--out", dest="out", required=True, help="Output BED sample")
    ap.add_argument(
        "--target-mib",
        type=float,
        default=100,
        help="Target size in MiB (approx). Stops at line boundary once near target.",
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = int(args.target_mib * 1024 * 1024)

    out.parent.mkdir(parents=True, exist_ok=True)

    open_fn = open
    if inp.suffix == ".gz":
        import gzip
        open_fn = lambda p, mode, **kw: gzip.open(p, mode.replace("t", ""), **kw)

    written = 0
    lines = 0

    with open_fn(inp, "rt", encoding="utf-8", errors="surrogateescape") as f, \
         out.open("wt", encoding="utf-8", errors="surrogateescape") as w:
        for line in f:
            if lines > 0 and written + len(line.encode("utf-8", errors="surrogateescape")) > target_bytes:
                break
            w.write(line)
            written += len(line.encode("utf-8", errors="surrogateescape"))
            lines += 1

    print(f"Wrote lines: {lines}")
    print(f"Output bytes: {written}")
    print(f"Output path:  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
