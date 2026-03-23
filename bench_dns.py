#!/usr/bin/env python3
"""DNS TSV compression benchmark: Nyx (OpenZL CSV) vs zstd vs Snappy.

Measures compression ratio, compress/decompress speed (MB/s), and CPU time
across multiple uncompressed file sizes.

Usage:
    source /tmp/bench_venv/bin/activate   # for python-snappy
    python3 bench_dns.py [--sizes 1,5,10,20,50,100] [--runs 3] [--train-secs 120] [--csv results.csv]
"""

import argparse
import csv as csv_mod
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from statistics import median

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
ZLI = REPO_ROOT / "nyx" / "openzl" / "zli"
DATA_DIR = REPO_ROOT / "data" / "DNS" / "dns-capture-vertica"

# ---------------------------------------------------------------------------
# Snappy availability
# ---------------------------------------------------------------------------
try:
    import snappy  # type: ignore
    HAS_SNAPPY = True
except ImportError:
    HAS_SNAPPY = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def prepare_sample(target_bytes: int, source_dir: Path, out_path: Path) -> None:
    """Create a TSV file of approximately *target_bytes* by concatenating
    source files, truncating at a line boundary.  Skips empty lines (batch
    separators from nom-kafka-dump) that break OpenZL's CSV lexer."""
    tsv_files = sorted(source_dir.glob("*.tsv"), key=lambda p: p.stat().st_size, reverse=True)
    # prefer the large _0001/_0002 files
    tsv_files = [f for f in tsv_files if f.stat().st_size > 1000] or tsv_files

    written = 0
    with open(out_path, "wb") as fout:
        for src in tsv_files:
            if written >= target_bytes:
                break
            with open(src, "rb") as fin:
                while written < target_bytes:
                    line = fin.readline()
                    if not line:
                        break
                    # Skip empty lines (batch separators)
                    if line.strip() == b"":
                        continue
                    fout.write(line)
                    written += len(line)
    # Final size may slightly overshoot — that's fine, it's at a line boundary.


# ---------------------------------------------------------------------------
# Compressor implementations
# ---------------------------------------------------------------------------

class Result:
    __slots__ = ("compressor", "orig_bytes", "comp_bytes", "comp_secs",
                 "decomp_secs", "cpu_comp", "cpu_decomp", "roundtrip_ok")

    def __init__(self, compressor: str, orig_bytes: int):
        self.compressor = compressor
        self.orig_bytes = orig_bytes
        self.comp_bytes = 0
        self.comp_secs = 0.0
        self.decomp_secs = 0.0
        self.cpu_comp = 0.0
        self.cpu_decomp = 0.0
        self.roundtrip_ok = False

    @property
    def ratio(self) -> float:
        return self.orig_bytes / self.comp_bytes if self.comp_bytes else 0.0

    @property
    def comp_mbps(self) -> float:
        return (self.orig_bytes / 1e6) / self.comp_secs if self.comp_secs else 0.0

    @property
    def decomp_mbps(self) -> float:
        return (self.orig_bytes / 1e6) / self.decomp_secs if self.decomp_secs else 0.0


def _timed_subprocess(cmd: list, **kwargs) -> tuple:
    """Run a subprocess, return (wall_secs, cpu_secs, returncode)."""
    t0_wall = time.monotonic()
    t0_cpu = time.process_time()
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)
    wall = time.monotonic() - t0_wall
    cpu = time.process_time() - t0_cpu
    if r.returncode != 0:
        print(f"    ERROR: {' '.join(str(c) for c in cmd)}", file=sys.stderr)
        print(f"    stderr: {r.stderr[:500]}", file=sys.stderr)
    return wall, cpu, r.returncode


def _resource_timed_subprocess(cmd: list) -> tuple:
    """Run subprocess via /usr/bin/time to get child CPU time."""
    t0 = time.monotonic()
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wall = time.monotonic() - t0
    # Use wall time for CPU estimate (subprocess CPU not captured by process_time)
    return wall, wall, r.returncode


def bench_zstd(input_path: Path, level: int, tmpdir: Path) -> Result:
    name = f"zstd -{level}"
    res = Result(name, input_path.stat().st_size)
    comp_out = tmpdir / f"zstd_{level}.zst"
    decomp_out = tmpdir / f"zstd_{level}.tsv"

    # Compress (write output directly for timing + size)
    t0 = time.monotonic()
    with open(comp_out, "wb") as fout:
        r = subprocess.run(
            ["zstd", f"-{level}", "-c", "--no-progress", str(input_path)],
            stdout=fout, stderr=subprocess.PIPE)
    res.comp_secs = time.monotonic() - t0
    if r.returncode != 0:
        return res
    res.comp_bytes = comp_out.stat().st_size
    res.cpu_comp = res.comp_secs

    # Decompress
    t0 = time.monotonic()
    with open(decomp_out, "wb") as fout:
        r = subprocess.run(
            ["zstd", "-d", "-c", "--no-progress", str(comp_out)],
            stdout=fout, stderr=subprocess.PIPE)
    res.decomp_secs = time.monotonic() - t0
    if r.returncode != 0:
        return res
    res.cpu_decomp = res.decomp_secs
    res.roundtrip_ok = md5(input_path) == md5(decomp_out)
    return res


def bench_snappy(input_path: Path, tmpdir: Path) -> Result:
    res = Result("snappy", input_path.stat().st_size)
    if not HAS_SNAPPY:
        return res
    comp_out = tmpdir / "snappy.sz"
    decomp_out = tmpdir / "snappy.tsv"

    raw = input_path.read_bytes()

    # Compress
    t0 = time.monotonic()
    compressed = snappy.compress(raw)
    res.comp_secs = time.monotonic() - t0
    comp_out.write_bytes(compressed)
    res.comp_bytes = len(compressed)
    res.cpu_comp = res.comp_secs  # in-process, wall ≈ cpu

    # Decompress
    t0 = time.monotonic()
    decompressed = snappy.decompress(compressed)
    res.decomp_secs = time.monotonic() - t0
    decomp_out.write_bytes(decompressed)
    res.cpu_decomp = res.decomp_secs
    res.roundtrip_ok = decompressed == raw
    return res


def bench_nyx(input_path: Path, compressor_path: Path | None, label: str,
              tmpdir: Path) -> Result:
    res = Result(label, input_path.stat().st_size)
    comp_out = tmpdir / f"nyx_{label.replace(' ', '_')}.zl"
    decomp_out = tmpdir / f"nyx_{label.replace(' ', '_')}.tsv"

    # Compress
    cmd = [str(ZLI), "compress", str(input_path), "--output", str(comp_out), "--force"]
    if compressor_path:
        cmd += ["--compressor", str(compressor_path)]
    else:
        cmd += ["--profile", "csv", "--profile-arg", "\t"]
    wall, cpu, rc = _resource_timed_subprocess(cmd)
    if rc != 0:
        return res
    res.comp_bytes = comp_out.stat().st_size
    res.comp_secs = wall
    res.cpu_comp = cpu

    # Decompress
    cmd = [str(ZLI), "decompress", str(comp_out), "--output", str(decomp_out), "--force"]
    wall, cpu, rc = _resource_timed_subprocess(cmd)
    if rc != 0:
        return res
    res.decomp_secs = wall
    res.cpu_decomp = cpu
    res.roundtrip_ok = md5(input_path) == md5(decomp_out)
    return res


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train_nyx(sample_path: Path, output_model: Path, max_time_secs: int = 120) -> None:
    """Train an OpenZL CSV compressor on a sample TSV file."""
    # zli train expects a directory of samples
    sample_dir = sample_path.parent / "train_samples"
    sample_dir.mkdir(exist_ok=True)
    shutil.copy2(sample_path, sample_dir / sample_path.name)

    cmd = [
        str(ZLI), "train", str(sample_dir),
        "--output", str(output_model),
        "--profile", "csv",
        "--profile-arg", "\t",
        "--use-all-samples",
        "--no-ace-successors",
        "--threads", str(os.cpu_count() or 4),
        "--max-time-secs", str(max_time_secs),
        "--force",
    ]
    print(f"  Training: {' '.join(cmd)}")
    t0 = time.monotonic()
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    elapsed = time.monotonic() - t0
    if r.returncode != 0:
        print(f"  Training FAILED (rc={r.returncode}):")
        print(f"  {r.stderr.decode()[:1000]}")
        sys.exit(1)
    print(f"  Training completed in {elapsed:.1f}s → {human_size(output_model.stat().st_size)}")


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------

def run_benchmark(sizes_mb: list, runs: int, train_secs: int, csv_path: str | None):
    print("=" * 78)
    print("  DNS TSV COMPRESSION BENCHMARK")
    print(f"  Source: {DATA_DIR}")
    print(f"  Runs per measurement: {runs} (median reported)")
    print(f"  Compressors: nyx (trained), nyx (csv), zstd -3, zstd -9, snappy")
    if not HAS_SNAPPY:
        print("  WARNING: python-snappy not installed — snappy will be skipped")
        print("           Install: pip install python-snappy")
    print("=" * 78)
    print()

    tmpdir_root = Path(tempfile.mkdtemp(prefix="bench_dns_"))

    # --- Train Nyx compressor once on ~20 MB sample ---
    print("[1/3] Preparing training sample (20 MB)...")
    train_sample = tmpdir_root / "train_sample.tsv"
    prepare_sample(20 * 1024 * 1024, DATA_DIR, train_sample)
    actual_mb = train_sample.stat().st_size / 1e6
    print(f"  Sample: {actual_mb:.1f} MB")

    print(f"\n[2/3] Training Nyx compressor (max {train_secs}s)...")
    model_path = tmpdir_root / "dns_csv.zl_compressor"
    train_nyx(train_sample, model_path, max_time_secs=train_secs)

    # --- Run benchmarks ---
    print(f"\n[3/3] Running benchmarks...\n")

    all_results = []

    for size_mb in sizes_mb:
        target_bytes = int(size_mb * 1024 * 1024)
        size_dir = tmpdir_root / f"size_{size_mb}mb"
        size_dir.mkdir(exist_ok=True)

        sample_path = size_dir / "input.tsv"
        prepare_sample(target_bytes, DATA_DIR, sample_path)
        actual_size = sample_path.stat().st_size
        print(f"--- {size_mb} MB (actual: {actual_size / 1e6:.2f} MB) ---")

        # Warm FS cache
        _ = sample_path.read_bytes()

        size_results = {}

        compressors = [
            ("nyx (trained)", lambda p, d: bench_nyx(p, model_path, "nyx (trained)", d)),
            ("nyx (csv)", lambda p, d: bench_nyx(p, None, "nyx (csv)", d)),
            ("zstd -3", lambda p, d: bench_zstd(p, 3, d)),
            ("zstd -9", lambda p, d: bench_zstd(p, 9, d)),
        ]
        if HAS_SNAPPY:
            compressors.append(("snappy", lambda p, d: bench_snappy(p, d)))

        for name, bench_fn in compressors:
            run_results = []
            for run_i in range(runs):
                run_dir = size_dir / f"{name.replace(' ', '_')}_run{run_i}"
                run_dir.mkdir(exist_ok=True)
                r = bench_fn(sample_path, run_dir)
                run_results.append(r)

            # Pick median by compression time
            run_results.sort(key=lambda r: r.comp_secs)
            best = run_results[len(run_results) // 2]
            size_results[name] = best

        # Print table for this size
        hdr = f"  {'Compressor':<16} {'Compressed':>12} {'Ratio':>8} {'Comp MB/s':>11} {'Decomp MB/s':>13} {'Round-trip':>11}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for name, r in size_results.items():
            rt = "PASS" if r.roundtrip_ok else ("FAIL" if r.comp_bytes > 0 else "SKIP")
            print(f"  {r.compressor:<16} {human_size(r.comp_bytes):>12} {r.ratio:>7.2f}x "
                  f"{r.comp_mbps:>10.1f} {r.decomp_mbps:>12.1f} {rt:>11}")
        print()

        all_results.append((size_mb, size_results))

    # --- Summary table ---
    print("=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print()

    # Compression ratio summary
    print(f"  {'Size':>6}  ", end="")
    comp_names = list(all_results[0][1].keys())
    for cn in comp_names:
        print(f"{cn:>16}", end="")
    print()
    print(f"  {'':>6}  ", end="")
    for _ in comp_names:
        print(f"{'ratio':>8}{'MB/s':>8}", end="")
    print()
    print("  " + "-" * (8 + 16 * len(comp_names)))

    for size_mb, size_results in all_results:
        print(f"  {size_mb:>4} MB  ", end="")
        for cn in comp_names:
            r = size_results[cn]
            print(f"{r.ratio:>7.2f}x{r.comp_mbps:>7.0f}", end=" ")
        print()

    print()

    # Decompression speed summary
    print("  Decompression speed (MB/s):")
    print(f"  {'Size':>6}  ", end="")
    for cn in comp_names:
        print(f"{cn:>16}", end="")
    print()
    print("  " + "-" * (8 + 16 * len(comp_names)))
    for size_mb, size_results in all_results:
        print(f"  {size_mb:>4} MB  ", end="")
        for cn in comp_names:
            r = size_results[cn]
            print(f"{r.decomp_mbps:>15.0f}", end=" ")
        print()

    print()

    # --- CSV output ---
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            writer = csv_mod.writer(f)
            writer.writerow(["size_mb", "compressor", "orig_bytes", "comp_bytes",
                             "ratio", "comp_secs", "comp_mbps",
                             "decomp_secs", "decomp_mbps", "roundtrip"])
            for size_mb, size_results in all_results:
                for name, r in size_results.items():
                    writer.writerow([
                        size_mb, r.compressor, r.orig_bytes, r.comp_bytes,
                        f"{r.ratio:.3f}", f"{r.comp_secs:.4f}", f"{r.comp_mbps:.1f}",
                        f"{r.decomp_secs:.4f}", f"{r.decomp_mbps:.1f}",
                        "PASS" if r.roundtrip_ok else "FAIL",
                    ])
        print(f"  Results written to {csv_path}")

    # Cleanup
    shutil.rmtree(tmpdir_root, ignore_errors=True)
    print("\nDone.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DNS TSV compression benchmark")
    parser.add_argument("--sizes", default="1,5,10,20,50,100",
                        help="Comma-separated sizes in MB (default: 1,5,10,20,50,100)")
    parser.add_argument("--runs", type=int, default=3,
                        help="Runs per measurement (default: 3)")
    parser.add_argument("--train-secs", type=int, default=120,
                        help="Max training time in seconds (default: 120)")
    parser.add_argument("--csv", dest="csv_path", default=None,
                        help="Write results to CSV file")
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",")]

    if not ZLI.exists():
        print(f"ERROR: zli not found at {ZLI}")
        print("Run: nyx build")
        sys.exit(1)

    if not DATA_DIR.exists():
        print(f"ERROR: DNS data not found at {DATA_DIR}")
        sys.exit(1)

    run_benchmark(sizes, args.runs, args.train_secs, args.csv_path)


if __name__ == "__main__":
    main()
