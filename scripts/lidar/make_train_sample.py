#!/usr/bin/env python3
"""Create a ~N MiB LiDAR training sample from a raw KITTI-format binary.

KITTI Velodyne .bin files store interleaved (x, y, z, intensity) as float32,
so each point is exactly 16 bytes. This script copies whole points (never
splitting mid-point) up to the requested target size.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

POINT_SIZE = 16  # 4 floats × 4 bytes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True, help="Input raw LiDAR .bin")
    ap.add_argument("--out", dest="out", required=True, help="Output training sample .bin")
    ap.add_argument(
        "--target-mib",
        type=int,
        default=200,
        help="Target size in MiB (approx). Rounded down to whole-point boundary.",
    )
    args = ap.parse_args()

    inp = Path(args.inp)
    out = Path(args.out)
    target_bytes = args.target_mib * 1024 * 1024

    # Round down to a whole-point boundary
    target_bytes = (target_bytes // POINT_SIZE) * POINT_SIZE

    out.parent.mkdir(parents=True, exist_ok=True)

    with inp.open("rb") as f:
        data = f.read(target_bytes)

    # Trim any trailing partial point (shouldn't happen, but be safe)
    usable = (len(data) // POINT_SIZE) * POINT_SIZE
    data = data[:usable]

    with out.open("wb") as f:
        f.write(data)

    points = usable // POINT_SIZE
    print(f"Wrote points:  {points}")
    print(f"Output bytes:  {usable}")
    print(f"Output MiB:    {usable / 1024 / 1024:.2f}")
    print(f"Output path:   {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
