#!/usr/bin/env python3
"""
Deterministic grid search over OpenZL training hyperparameters.
================================================================================

Evaluates all 180 combinations of quality-affecting training parameters by
actually training compressors and compressing the full GRCm39 mouse genome
(~2.7 GB, 14 FAV4 chunks). Results are saved incrementally to a CSV file.

Grid (5 parameters, 180 combinations):
  trainer:           full-split, bottom-up, greedy          (3 values)
  max_time_secs:     60, 120, 300, 600, 1800                (5 values)
  no_ace_successors: True, False                             (2 values)
  no_clustering:     False, True                             (2 values)
  target_train_mib:  50, 100, 200                            (3 values)

Fixed (speed-only, no effect on compression quality):
  threads:           min(cpu_count, 16)
  compress_jobs:     sequential (one chunk at a time)

Prerequisites:
  - nyx is built:  cd nyx && pip install -e . && nyx build
  - Data prepared: bash nyx/evolve/prepare_data.sh

Usage:
  python3 optimizing_openzl/grid_search.py

Resume after interruption (skips already-completed configs):
  python3 optimizing_openzl/grid_search.py

Load results into pandas for analysis:
  >>> import pandas as pd
  >>> df = pd.read_csv("optimizing_openzl/results/grid_results.csv")
  >>> df[df.status == "success"].sort_values("compression_ratio", ascending=False).head(10)
"""

import csv
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

# ── Path Resolution ──────────────────────────────────────────────────────────

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
_NYX_ROOT = _REPO_ROOT / "nyx"
_EVOLVE_DATA = _REPO_ROOT / "evolve_data"
_FULL_GENOME_CHUNKS = _EVOLVE_DATA / "full_genome_chunks"
_ORIGINAL_SIZE_FILE = _FULL_GENOME_CHUNKS / "original_genome_bytes.txt"
_SCHEMA = _NYX_ROOT / "schemas" / "fasta_packed.sddl"
_RESULTS_DIR = _SCRIPT_DIR / "results"
_RESULTS_CSV = _RESULTS_DIR / "grid_results.csv"

# Locate zli binary (same resolution order as nyx)
_ZLI = _NYX_ROOT / "openzl" / "zli"
if not _ZLI.is_file():
    _zli_path = shutil.which("zli")
    if _zli_path:
        _ZLI = Path(_zli_path)

# ── Grid Definition ──────────────────────────────────────────────────────────
# These parameters affect compression quality and are the search target.

TRAINERS = ["full-split", "bottom-up", "greedy"]
MAX_TIME_SECS = [60, 120, 300, 600, 1800]
NO_ACE_SUCCESSORS = [True, False]
NO_CLUSTERING = [False, True]
TARGET_TRAIN_MIB = [50, 100, 200]

# Fixed: affects speed only, not quality. Set once for all runs.
THREADS = min(os.cpu_count() or 4, 16)

# ── CSV Schema ───────────────────────────────────────────────────────────────

CSV_COLUMNS = [
    "run_id",
    "trainer",
    "max_time_secs",
    "no_ace_successors",
    "no_clustering",
    "target_train_mib",
    "threads",
    "training_time_secs",
    "compress_time_secs",
    "total_time_secs",
    "original_text_bytes",
    "binary_bytes",
    "compressed_bytes",
    "compression_ratio",
    "binary_ratio",
    "compress_speed_mbps",
    "compressor_size_bytes",
    "num_chunks",
    "status",
    "error_message",
]


# ── Grid Helpers ─────────────────────────────────────────────────────────────


def generate_grid():
    """Generate all parameter combinations, sorted fastest-first."""
    combos = list(
        itertools.product(
            TRAINERS,
            MAX_TIME_SECS,
            NO_ACE_SUCCESSORS,
            NO_CLUSTERING,
            TARGET_TRAIN_MIB,
        )
    )
    # Sort so fast configs run first: fast trainer → small data → short budget
    trainer_order = {"full-split": 0, "bottom-up": 1, "greedy": 2}
    combos.sort(key=lambda c: (trainer_order[c[0]], c[4], c[1]))
    return combos


def config_key(trainer, max_time, no_ace, no_clust, target_mib):
    """Unique string key for a configuration (used for resume detection).

    Works identically whether values are native types (from the grid) or
    strings (from CSV), because f-string conversion of True == "True", etc.
    """
    return f"{trainer}|{max_time}|{no_ace}|{no_clust}|{target_mib}"


def load_completed(csv_path):
    """Load already-completed config keys from the results CSV."""
    completed = set()
    if csv_path.is_file() and csv_path.stat().st_size > 0:
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                key = config_key(
                    row["trainer"],
                    row["max_time_secs"],
                    row["no_ace_successors"],
                    row["no_clustering"],
                    row["target_train_mib"],
                )
                completed.add(key)
    return completed


# ── ZLI Runner ───────────────────────────────────────────────────────────────


def run_zli(args, timeout=3600):
    """Run zli with the given arguments. Returns CompletedProcess."""
    cmd = [str(_ZLI)] + args
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


# ── Single Configuration Evaluation ─────────────────────────────────────────


def run_single_config(
    run_id, trainer, max_time, no_ace, no_clust, target_mib,
    original_text_bytes, eval_chunks,
):
    """Execute one grid point: train a compressor + compress all chunks.

    Returns a dict with all CSV columns populated.
    """
    result = {
        "run_id": run_id,
        "trainer": trainer,
        "max_time_secs": max_time,
        "no_ace_successors": no_ace,
        "no_clustering": no_clust,
        "target_train_mib": target_mib,
        "threads": THREADS,
        "original_text_bytes": original_text_bytes,
        "num_chunks": len(eval_chunks),
        "training_time_secs": "",
        "compress_time_secs": "",
        "total_time_secs": "",
        "binary_bytes": "",
        "compressed_bytes": "",
        "compression_ratio": "",
        "binary_ratio": "",
        "compress_speed_mbps": "",
        "compressor_size_bytes": "",
        "status": "failed",
        "error_message": "",
    }

    tmpdir = None
    try:
        # Resolve training data directory
        train_dir = _EVOLVE_DATA / f"train_{target_mib}MiB"
        if not train_dir.is_dir():
            result["error_message"] = f"Training data not found: {train_dir}"
            return result

        tmpdir = Path(tempfile.mkdtemp(prefix="grid_eval_"))
        compressor_path = tmpdir / "compressor.model"

        # ── Train ─────────────────────────────────────────────────────────
        train_args = [
            "train",
            str(train_dir),
            "--output", str(compressor_path),
            "--profile", "sddl",
            "--profile-arg", str(_SCHEMA),
            "--threads", str(THREADS),
            "--max-time-secs", str(max_time),
            "--trainer", trainer,
            "--use-all-samples",
            "--force",
        ]
        if no_ace:
            train_args.append("--no-ace-successors")
        if no_clust:
            train_args.append("--no-clustering")

        train_start = time.monotonic()
        tres = run_zli(train_args, timeout=max_time + 300)
        train_elapsed = time.monotonic() - train_start
        result["training_time_secs"] = round(train_elapsed, 2)

        if tres.returncode != 0:
            result["error_message"] = (
                f"Training failed (exit {tres.returncode}): "
                f"{tres.stderr[:500]}"
            )
            return result

        if not compressor_path.is_file():
            result["error_message"] = "Training produced no compressor file"
            return result

        result["compressor_size_bytes"] = compressor_path.stat().st_size

        # ── Compress all full-genome chunks ───────────────────────────────
        compressed_dir = tmpdir / "compressed"
        compressed_dir.mkdir()

        binary_bytes = 0
        compressed_bytes = 0
        compress_start = time.monotonic()

        for chunk in eval_chunks:
            out = compressed_dir / (chunk.name + ".zl")
            cres = run_zli(
                [
                    "compress", str(chunk),
                    "--compressor", str(compressor_path),
                    "--output", str(out),
                    "--force",
                ],
                timeout=300,
            )

            if cres.returncode != 0:
                result["error_message"] = (
                    f"Compression failed on {chunk.name}: "
                    f"{cres.stderr[:300]}"
                )
                return result

            binary_bytes += chunk.stat().st_size
            if out.is_file():
                compressed_bytes += out.stat().st_size

        compress_elapsed = time.monotonic() - compress_start
        result["compress_time_secs"] = round(compress_elapsed, 2)
        result["total_time_secs"] = round(train_elapsed + compress_elapsed, 2)
        result["binary_bytes"] = binary_bytes
        result["compressed_bytes"] = compressed_bytes

        if compressed_bytes == 0:
            result["error_message"] = "All compressed files are empty"
            return result

        # ── Compute metrics ───────────────────────────────────────────────
        # compression_ratio: text-to-compressed (matches nyx compress output)
        result["compression_ratio"] = round(
            original_text_bytes / compressed_bytes, 4
        )
        # binary_ratio: binary-to-compressed (OpenZL-only contribution)
        result["binary_ratio"] = round(binary_bytes / compressed_bytes, 4)
        # compress_speed_mbps: end-to-end throughput during compression
        result["compress_speed_mbps"] = round(
            (original_text_bytes / 1e6) / compress_elapsed, 2
        ) if compress_elapsed > 0 else 0

        result["status"] = "success"
        return result

    except subprocess.TimeoutExpired:
        result["error_message"] = "Subprocess timed out"
        return result
    except Exception as e:
        result["error_message"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        if tmpdir and tmpdir.is_dir():
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Summary Printer ──────────────────────────────────────────────────────────


def _fmt_ace(val):
    """Format ACE value for display."""
    return "off" if str(val) == "True" else " on"


def _fmt_clust(val):
    """Format clustering value for display."""
    return "off" if str(val) == "True" else " on"


def print_summary(csv_path):
    """Print a formatted summary of all results."""
    results = []
    failed_count = 0

    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["status"] == "success":
                # Convert numeric fields for sorting
                row["compression_ratio"] = float(row["compression_ratio"])
                row["training_time_secs"] = float(row["training_time_secs"])
                row["compress_time_secs"] = float(row["compress_time_secs"])
                row["total_time_secs"] = float(row["total_time_secs"])
                row["compress_speed_mbps"] = float(row["compress_speed_mbps"])
                results.append(row)
            else:
                failed_count += 1

    if not results:
        print("\nNo successful runs to summarize.")
        return

    by_ratio = sorted(
        results, key=lambda r: r["compression_ratio"], reverse=True
    )
    by_speed = sorted(results, key=lambda r: r["total_time_secs"])

    header = (
        f"{'#':>3} {'Ratio':>7} {'Trainer':<12} {'Train(s)':>9} "
        f"{'Comp(s)':>8} {'Total(s)':>9} {'MaxT':>5} "
        f"{'ACE':>4} {'Clst':>5} {'MiB':>4} {'MB/s':>7}"
    )
    sep = "-" * 93

    # ── Top 10 by compression ratio ──────────────────────────────────────
    print(f"\n{'=' * 93}")
    print("  TOP 10 BY COMPRESSION RATIO")
    print(f"{'=' * 93}")
    print(header)
    print(sep)
    for i, r in enumerate(by_ratio[:10], 1):
        print(
            f"{i:>3} {r['compression_ratio']:>7.3f} {r['trainer']:<12} "
            f"{r['training_time_secs']:>9.1f} {r['compress_time_secs']:>8.1f} "
            f"{r['total_time_secs']:>9.1f} {r['max_time_secs']:>5} "
            f"{_fmt_ace(r['no_ace_successors']):>4} "
            f"{_fmt_clust(r['no_clustering']):>5} "
            f"{r['target_train_mib']:>4} "
            f"{r['compress_speed_mbps']:>7.1f}"
        )

    # ── Top 10 by total speed ────────────────────────────────────────────
    print(f"\n{'=' * 93}")
    print("  TOP 10 BY TOTAL SPEED (fastest training + compression)")
    print(f"{'=' * 93}")
    print(header)
    print(sep)
    for i, r in enumerate(by_speed[:10], 1):
        print(
            f"{i:>3} {r['compression_ratio']:>7.3f} {r['trainer']:<12} "
            f"{r['training_time_secs']:>9.1f} {r['compress_time_secs']:>8.1f} "
            f"{r['total_time_secs']:>9.1f} {r['max_time_secs']:>5} "
            f"{_fmt_ace(r['no_ace_successors']):>4} "
            f"{_fmt_clust(r['no_clustering']):>5} "
            f"{r['target_train_mib']:>4} "
            f"{r['compress_speed_mbps']:>7.1f}"
        )

    # ── Pareto frontier ──────────────────────────────────────────────────
    # Best compression ratio at each total-time tier
    time_tiers = [
        (0, 30, "<30s"),
        (30, 60, "30-60s"),
        (60, 120, "1-2min"),
        (120, 300, "2-5min"),
        (300, 600, "5-10min"),
        (600, 1200, "10-20min"),
        (1200, float("inf"), ">20min"),
    ]

    print(f"\n{'=' * 93}")
    print("  PARETO FRONTIER (best ratio at each time tier)")
    print(f"{'=' * 93}")
    print(
        f"{'Tier':<10} {'Ratio':>7} {'Trainer':<12} {'Train(s)':>9} "
        f"{'Total(s)':>9} {'MaxT':>5} {'ACE':>4} {'Clst':>5} {'MiB':>4}"
    )
    print(sep)

    for lo, hi, label in time_tiers:
        tier = [r for r in results if lo <= r["total_time_secs"] < hi]
        if tier:
            best = max(tier, key=lambda r: r["compression_ratio"])
            print(
                f"{label:<10} {best['compression_ratio']:>7.3f} "
                f"{best['trainer']:<12} "
                f"{best['training_time_secs']:>9.1f} "
                f"{best['total_time_secs']:>9.1f} {best['max_time_secs']:>5} "
                f"{_fmt_ace(best['no_ace_successors']):>4} "
                f"{_fmt_clust(best['no_clustering']):>5} "
                f"{best['target_train_mib']:>4}"
            )
        else:
            print(f"{label:<10} {'(no data)':>7}")

    # ── Overall stats ────────────────────────────────────────────────────
    print(f"\n{'=' * 93}")
    print(
        f"  Total: {len(results)} successful, {failed_count} failed, "
        f"{len(results) + failed_count} evaluated"
    )

    best = by_ratio[0]
    fastest = by_speed[0]
    print(
        f"\n  Best ratio:  {best['compression_ratio']:.3f}x — "
        f"trainer={best['trainer']}, max_time={best['max_time_secs']}s, "
        f"ace={_fmt_ace(best['no_ace_successors']).strip()}, "
        f"clustering={_fmt_clust(best['no_clustering']).strip()}, "
        f"train_mib={best['target_train_mib']}, "
        f"total={best['total_time_secs']:.0f}s"
    )
    print(
        f"  Fastest run: {fastest['total_time_secs']:.0f}s — "
        f"{fastest['compression_ratio']:.3f}x ratio, "
        f"trainer={fastest['trainer']}, max_time={fastest['max_time_secs']}s, "
        f"train_mib={fastest['target_train_mib']}"
    )
    print(f"{'=' * 93}")


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    print("=" * 70)
    print("  OpenZL Training Hyperparameter Grid Search")
    print("=" * 70)

    # ── Preflight checks ─────────────────────────────────────────────────
    errors = []

    if not _ZLI.is_file():
        errors.append(
            f"zli not found at {_ZLI}. Run 'nyx build' first."
        )

    if not _SCHEMA.is_file():
        errors.append(f"SDDL schema not found at {_SCHEMA}")

    if not _FULL_GENOME_CHUNKS.is_dir():
        errors.append(
            f"Full genome chunks not found at {_FULL_GENOME_CHUNKS}. "
            "Run 'bash nyx/evolve/prepare_data.sh' first."
        )

    if not _ORIGINAL_SIZE_FILE.is_file():
        errors.append(
            f"Original genome size file not found at {_ORIGINAL_SIZE_FILE}. "
            "Run 'bash nyx/evolve/prepare_data.sh' first."
        )

    for mib in TARGET_TRAIN_MIB:
        d = _EVOLVE_DATA / f"train_{mib}MiB"
        if not d.is_dir():
            errors.append(f"Training data not found: {d}")

    if errors:
        print("\nPreflight checks FAILED:")
        for e in errors:
            print(f"  ERROR: {e}")
        sys.exit(1)

    # ── Load data info ───────────────────────────────────────────────────
    original_text_bytes = int(_ORIGINAL_SIZE_FILE.read_text().strip())
    eval_chunks = sorted(_FULL_GENOME_CHUNKS.glob("*.fasta_packed.bin"))

    if not eval_chunks:
        print("ERROR: No .fasta_packed.bin chunks in full_genome_chunks/")
        sys.exit(1)

    binary_total = sum(c.stat().st_size for c in eval_chunks)

    print(f"\nOriginal text genome: {original_text_bytes / 1e9:.2f} GB")
    print(
        f"Preprocessed binary:  {binary_total / 1e9:.2f} GB "
        f"({len(eval_chunks)} FAV4 chunks)"
    )
    print(f"Preprocessing ratio:  {original_text_bytes / binary_total:.2f}x")
    print(f"Threads per run:      {THREADS}")

    # ── Generate grid & check for existing results ───────────────────────
    grid = generate_grid()
    total = len(grid)

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    completed = load_completed(_RESULTS_CSV)

    remaining = [
        (i, combo)
        for i, combo in enumerate(grid, 1)
        if config_key(*combo) not in completed
    ]

    print(f"\nGrid: {total} total configurations")
    if completed:
        print(f"Already completed: {len(completed)} (will resume)")
    print(f"Remaining: {len(remaining)}")

    if not remaining:
        print("\nAll configurations already evaluated!")
        print_summary(_RESULTS_CSV)
        return

    # Rough ETA: training usually finishes well before budget (avg ~2 min)
    est_minutes = len(remaining) * 2
    print(
        f"Estimated runtime: ~{est_minutes // 60}h {est_minutes % 60}m "
        f"(~2 min avg per config)"
    )
    print(f"\nResults: {_RESULTS_CSV}")
    print("-" * 70)

    # ── Main loop ────────────────────────────────────────────────────────
    needs_header = (
        not _RESULTS_CSV.is_file() or _RESULTS_CSV.stat().st_size == 0
    )
    search_start = time.monotonic()
    runs_done = 0

    try:
        with open(_RESULTS_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            if needs_header:
                writer.writeheader()
                f.flush()

            for run_id, (trainer, max_time, no_ace, no_clust, target_mib) in remaining:
                runs_done += 1
                done_total = len(completed) + runs_done
                elapsed = time.monotonic() - search_start
                avg = elapsed / runs_done
                eta = avg * (total - done_total)

                print(
                    f"\n[{done_total}/{total}] "
                    f"trainer={trainer}, max_time={max_time}s, "
                    f"ace={'off' if no_ace else 'on'}, "
                    f"clustering={'off' if no_clust else 'on'}, "
                    f"train_mib={target_mib} "
                    f"(ETA: {timedelta(seconds=int(eta))})"
                )

                result = run_single_config(
                    run_id,
                    trainer,
                    max_time,
                    no_ace,
                    no_clust,
                    target_mib,
                    original_text_bytes,
                    eval_chunks,
                )

                # Write immediately so results survive interruption
                writer.writerow(
                    {col: result.get(col, "") for col in CSV_COLUMNS}
                )
                f.flush()

                if result["status"] == "success":
                    print(
                        f"  -> ratio={result['compression_ratio']:.3f}x, "
                        f"train={result['training_time_secs']:.1f}s, "
                        f"compress={result['compress_time_secs']:.1f}s, "
                        f"total={result['total_time_secs']:.1f}s, "
                        f"speed={result['compress_speed_mbps']:.1f} MB/s"
                    )
                else:
                    print(
                        f"  -> FAILED: {result['error_message'][:120]}"
                    )

    except KeyboardInterrupt:
        total_elapsed = time.monotonic() - search_start
        print(
            f"\n\nInterrupted after {runs_done} runs "
            f"({timedelta(seconds=int(total_elapsed))}). "
            f"Results saved to {_RESULTS_CSV}."
        )
        print("Re-run this script to resume from where you left off.")
        sys.exit(130)

    # ── Done ─────────────────────────────────────────────────────────────
    total_elapsed = time.monotonic() - search_start
    print(f"\n{'=' * 70}")
    print(
        f"Grid search complete: {runs_done} runs in "
        f"{timedelta(seconds=int(total_elapsed))}"
    )
    print(f"Results saved to: {_RESULTS_CSV}")

    print_summary(_RESULTS_CSV)


if __name__ == "__main__":
    main()
