#!/usr/bin/env python3
"""Generate diverse synthetic training data for generic OpenZL models.

Creates binary files covering common data patterns for each profile type.
These are used to train generic (non-dataset-specific) compressor models.
"""

import os
from pathlib import Path

import numpy as np

OUT_DIR = Path(__file__).parent / "generic_training" / "data"
N = 1_000_000  # values per pattern segment


def generate_i16():
    """INT16: status codes, flags, small sensor readings."""
    rng = np.random.default_rng(42)
    parts = [
        rng.integers(0, 20, size=2 * N, dtype=np.int16),       # status codes / flags
        rng.integers(0, 1000, size=2 * N, dtype=np.int16),      # medium-range sensor
        rng.integers(-32768, 32767, size=N, dtype=np.int16),     # uniform random
    ]
    arr = np.concatenate(parts)
    path = OUT_DIR / "generic_i16.bin"
    arr.tofile(str(path))
    print(f"  generic_i16.bin: {arr.nbytes / 1e6:.1f} MB ({len(arr):,} values)")


def generate_i32():
    """INT32: IDs, counters, categorical ints, sensor readings."""
    rng = np.random.default_rng(43)
    parts = [
        rng.integers(-2**31, 2**31 - 1, size=N, dtype=np.int32),                        # uniform random
        (rng.normal(1000, 200, size=N)).astype(np.int32),                                 # Gaussian
        (rng.zipf(1.5, size=N) % 100_000).astype(np.int32),                              # power-law IDs
        rng.integers(0, 100, size=N, dtype=np.int32),                                     # small-range categorical
        np.cumsum(rng.integers(-5, 6, size=N, dtype=np.int32)).astype(np.int32),          # random walk
    ]
    arr = np.concatenate(parts)
    path = OUT_DIR / "generic_i32.bin"
    arr.tofile(str(path))
    print(f"  generic_i32.bin: {arr.nbytes / 1e6:.1f} MB ({len(arr):,} values)")


def generate_f32():
    """FLOAT32: Gaussian noise, sensor data, sparse ML features, prices."""
    rng = np.random.default_rng(44)
    gaussian = rng.normal(0, 1, size=N).astype(np.float32)
    random_walk = np.cumsum(rng.normal(0, 0.1, size=N)).astype(np.float32)
    sparse = np.where(rng.random(N) < 0.8, 0.0, rng.normal(0, 1, N)).astype(np.float32)
    uniform = rng.uniform(0, 1000, size=N).astype(np.float32)
    centers = rng.choice([10, 50, 200, 500, 900], size=N)
    clustered = (centers + rng.normal(0, 5, size=N)).astype(np.float32)

    arr = np.concatenate([gaussian, random_walk, sparse, uniform, clustered])
    path = OUT_DIR / "generic_f32.bin"
    arr.tofile(str(path))
    print(f"  generic_f32.bin: {arr.nbytes / 1e6:.1f} MB ({len(arr):,} values)")


def generate_i64():
    """INT64: timestamps, sequential IDs, power-law IDs, random."""
    rng = np.random.default_rng(45)
    base_ts = 1_700_000_000_000  # ~2023 epoch millis
    ts_monotonic = (base_ts + np.arange(N) * 1000 + rng.integers(0, 500, size=N)).astype(np.int64)
    ts_random = rng.integers(base_ts - 31_536_000_000, base_ts + 31_536_000_000, size=N, dtype=np.int64)
    seq_ids = (np.arange(N, dtype=np.int64) + 1_000_000)
    zipf_ids = (rng.zipf(1.5, size=N) % 10_000_000).astype(np.int64)
    uniform = rng.integers(0, 2**63 - 1, size=N, dtype=np.int64)

    arr = np.concatenate([ts_monotonic, ts_random, seq_ids, zipf_ids, uniform])
    path = OUT_DIR / "generic_i64.bin"
    arr.tofile(str(path))
    print(f"  generic_i64.bin: {arr.nbytes / 1e6:.1f} MB ({len(arr):,} values)")


def generate_serial():
    """Serial: random bytes for boolean columns, dictionary blobs, mixed binary."""
    rng = np.random.default_rng(46)
    arr = rng.integers(0, 256, size=5 * N, dtype=np.uint8)
    path = OUT_DIR / "generic_serial.bin"
    arr.tofile(str(path))
    print(f"  generic_serial.bin: {arr.nbytes / 1e6:.1f} MB ({len(arr):,} values)")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("Generating generic training data...")
    generate_i16()
    generate_i32()
    generate_f32()
    generate_i64()
    generate_serial()
    print(f"\nAll files written to {OUT_DIR}")


if __name__ == "__main__":
    main()
