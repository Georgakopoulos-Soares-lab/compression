#!/usr/bin/env python3
"""Train generic OpenZL models on diverse synthetic data.

For each profile type (le-i16, le-i32, le-i64, serial), trains a single
generic compressor model using `zli train`. The i32 model is trained on
both INT32 and FLOAT32 data since they share the le-i32 profile.
"""

import shutil
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).parent / "generic_training"
DATA_DIR = BASE / "data"
MODELS_DIR = BASE / "models"
ZLI = sorted(Path(__file__).parent.parent.glob("nyx/openzl/cachedObjs/*/zli"))[-1]

PROFILES = [
    {
        "name": "i16",
        "profile": "le-i16",
        "files": ["generic_i16.bin"],
    },
    {
        "name": "i32",
        "profile": "le-i32",
        "files": ["generic_i32.bin", "generic_f32.bin"],
    },
    {
        "name": "i64",
        "profile": "le-i64",
        "files": ["generic_i64.bin"],
    },
    {
        "name": "serial",
        "profile": "serial",
        "files": ["generic_serial.bin"],
    },
]

MAX_TIME_SECS = 300


def train_model(name: str, profile: str, files: list[str]):
    samples_dir = BASE / f"samples_{name}"
    if samples_dir.exists():
        shutil.rmtree(samples_dir)
    samples_dir.mkdir(parents=True)

    for f in files:
        src = DATA_DIR / f
        if not src.exists():
            raise FileNotFoundError(f"Training data missing: {src}")
        shutil.copy(src, samples_dir / f)

    model_path = MODELS_DIR / f"generic_{name}.model"

    cmd = [
        str(ZLI), "train",
        str(samples_dir.resolve()),
        "--profile", profile,
        "--output", str(model_path.resolve()),
        "--max-time-secs", str(MAX_TIME_SECS),
        "--force",
    ]

    print(f"\n  Training {name} ({profile})...")
    print(f"    Files: {', '.join(files)}")
    print(f"    Command: {' '.join(cmd)}")

    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_TIME_SECS + 60)
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"    FAILED (rc={result.returncode}):")
        print(f"    {result.stderr[:500]}")
        return None

    model_size = model_path.stat().st_size
    print(f"    Done in {elapsed:.1f}s — model: {model_size:,} bytes ({model_size / 1024:.1f} KB)")

    shutil.rmtree(samples_dir, ignore_errors=True)
    return {"name": name, "profile": profile, "model_path": str(model_path),
            "model_bytes": model_size, "train_time_secs": round(elapsed, 1)}


def main():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Training generic models using: {ZLI}")

    results = []
    for p in PROFILES:
        r = train_model(p["name"], p["profile"], p["files"])
        if r:
            results.append(r)
        else:
            print(f"    WARNING: {p['name']} model training failed!")

    print("\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['name']:8s}  {r['profile']:8s}  "
              f"{r['model_bytes']:>8,} bytes  {r['train_time_secs']:>6.1f}s")


if __name__ == "__main__":
    main()
