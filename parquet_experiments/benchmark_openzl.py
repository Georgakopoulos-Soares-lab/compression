#!/usr/bin/env python3
"""Benchmark OpenZL vs Parquet compression on extracted column data.

For each dataset:
  1. Trains an OpenZL compressor using the SDDL profile on PBL data
  2. Compresses the PBL file with the trained compressor
  3. Compresses the original Parquet file with OpenZL's built-in parquet profile
  4. Compares all results in a formatted table

Prerequisites:
  - Run extract_for_openzl.py first to create PBL files and SDDL schemas
  - zli must be built (run 'nyx build')

Usage:
  python benchmark_openzl.py
  python benchmark_openzl.py --datasets numeric_heavy
  python benchmark_openzl.py --max-train-time 60 --threads 4
  python benchmark_openzl.py --skip-existing  # reuse trained models
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

ALL_DATASETS = ["numeric_heavy", "ml_features", "mixed_type"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str = ""):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def find_zli() -> Path:
    """Locate the zli binary."""
    # Check environment variable first
    env_zli = os.environ.get("NYX_ZLI")
    if env_zli:
        p = Path(env_zli)
        if p.is_file():
            return p

    # Standard location
    zli = REPO_ROOT / "nyx" / "openzl" / "zli"
    if zli.is_file():
        return zli.resolve()

    raise FileNotFoundError(
        f"zli not found at {zli}.\n"
        f"Run 'nyx build' to compile it, or set NYX_ZLI environment variable."
    )


def run_with_progress(cmd: list[str], timeout: int | None = None,
                      label: str = "") -> tuple[int, str, float]:
    """Run a command, printing a periodic progress indicator.

    Uses subprocess.run (not line-by-line streaming) because zli uses
    carriage-return progress bars that block Python's line iterator.

    Returns (exit_code, full_output, elapsed_seconds).
    """
    import threading

    start = time.monotonic()

    # Print periodic progress dots while the command runs
    stop_event = threading.Event()

    def progress_printer():
        interval = 15  # seconds
        while not stop_event.wait(interval):
            elapsed = time.monotonic() - start
            log(f"  ... {label} still running ({elapsed:.0f}s elapsed)")

    progress_thread = threading.Thread(target=progress_printer, daemon=True)
    progress_thread.start()

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        rc = result.returncode
        output = result.stdout
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        log(f"TIMEOUT: {label} killed after {elapsed:.0f}s")
        stop_event.set()
        return -1, "", elapsed
    finally:
        stop_event.set()
        progress_thread.join(timeout=2)

    elapsed = time.monotonic() - start

    # Extract meaningful lines from output (skip progress bar spam)
    clean_lines = []
    for line in output.split("\n"):
        # Strip ANSI escape sequences and carriage returns
        clean = line.replace("\x1b[K", "").strip()
        if clean and not clean.startswith("["):
            # Skip progress bar lines like [====---]
            clean_lines.append(clean)
        elif "Training improved" in clean or "Benchmarking" in clean:
            clean_lines.append(clean)

    if clean_lines:
        for cl in clean_lines:
            log(f"  │ {cl}")

    return rc, output, elapsed


def run_capture(cmd: list[str], timeout: int | None = None,
                label: str = "") -> tuple[int, str, str, float]:
    """Run a command, capturing stdout and stderr separately.

    Returns (exit_code, stdout, stderr, elapsed_seconds).
    """
    start = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        log(f"TIMEOUT: {label} killed after {elapsed:.0f}s")
        return -1, "", "TimeoutExpired", elapsed

    elapsed = time.monotonic() - start
    return result.returncode, result.stdout, result.stderr, elapsed


# ─────────────────────────────────────────────────────────────────────────────
# Per-dataset benchmark
# ─────────────────────────────────────────────────────────────────────────────

def benchmark_dataset(
    dataset: str,
    zli: Path,
    data_dir: Path,
    extracted_dir: Path,
    schema_dir: Path,
    models_dir: Path,
    results_dir: Path,
    max_train_time: int,
    threads: int,
    skip_existing: bool,
) -> dict | None:
    """Run the full benchmark pipeline for one dataset.

    Returns a results dict, or None if critical preflight checks fail.
    """
    # ── Preflight ──
    pbl_file = extracted_dir / dataset / "data.pbl"
    sddl_file = schema_dir / f"{dataset}.sddl"
    meta_file = extracted_dir / dataset / "metadata.json"
    parquet_file = data_dir / f"{dataset}_none.parquet"

    log("═" * 70)
    log(f"BENCHMARK: {dataset}")
    log("═" * 70)

    for f, desc in [(pbl_file, "PBL file"), (sddl_file, "SDDL schema"),
                    (meta_file, "metadata"), (parquet_file, "Parquet file")]:
        if not f.exists():
            log(f"ERROR: Missing {desc}: {f}")
            log(f"Run extract_for_openzl.py first.")
            return None

    metadata = json.loads(meta_file.read_text())

    log(f"PBL file:    {pbl_file} ({fmt_bytes(pbl_file.stat().st_size)})")
    log(f"SDDL schema: {sddl_file} ({len(metadata['columns'])} columns)")
    log(f"Parquet:      {parquet_file} ({fmt_bytes(parquet_file.stat().st_size)})")

    result = {
        "dataset": dataset,
        "num_rows": metadata["num_rows"],
        "num_cols": len(metadata["columns"]),
        "raw_column_bytes": metadata["raw_column_bytes"],
        "pbl_bytes": metadata["pbl_size_bytes"],
        "parquet_none_bytes": metadata.get("parquet_none_bytes", 0),
        "parquet_zstd_bytes": metadata.get("parquet_zstd_bytes", 0),
        "parquet_gzip_bytes": metadata.get("parquet_gzip_bytes", 0),
        "parquet_snappy_bytes": metadata.get("parquet_snappy_bytes", 0),
        "status": "pending",
        "skipped_columns": metadata.get("skipped_columns", []),
    }

    # ── Step 1: Create training directory ──
    train_dir = models_dir / dataset / "train"
    train_dir.mkdir(parents=True, exist_ok=True)
    link = train_dir / "data.pbl"
    if not link.exists():
        link.symlink_to(pbl_file.resolve())
        log(f"Symlinked training data: {link} → {pbl_file.resolve()}")

    compressor_path = models_dir / dataset / "compressor.model"
    results_dir.mkdir(parents=True, exist_ok=True)
    sddl_output = results_dir / f"{dataset}.pbl.zl"
    parquet_output = results_dir / f"{dataset}_parquet_profile.zl"

    # ── Step 2: Train (OpenZL + SDDL) ──
    log("─" * 70)

    if skip_existing and compressor_path.exists():
        log(f"Step 1/3: SKIPPED — model already exists: {compressor_path}")
        train_time = 0.0
        train_output = "(skipped)"
        train_cmd_str = "(skipped)"
    else:
        log(f"Step 1/3: Training OpenZL compressor "
            f"(SDDL profile, max {max_train_time}s)...")
        train_cmd = [
            str(zli), "train",
            str(train_dir.resolve()),
            "--profile", "sddl",
            "--profile-arg", str(sddl_file.resolve()),
            "--output", str(compressor_path.resolve()),
            "--use-all-samples",
            "--threads", str(threads),
            "--max-time-secs", str(max_train_time),
            "--no-ace-successors",  # matches nyx default; faster training
            "--force",
        ]
        train_cmd_str = " ".join(train_cmd)
        log(f"CMD: {train_cmd_str}")

        rc, train_output, train_time = run_with_progress(
            train_cmd,
            timeout=max_train_time + 600,  # generous buffer for clustering overhead
            label="Training",
        )

        if rc != 0:
            log(f"ERROR: Training failed (exit code {rc})")
            log(f"Output:\n{train_output}")
            result["status"] = "train_failed"
            result["train_command"] = train_cmd_str
            result["train_output"] = train_output
            result["train_time_secs"] = train_time
            return result

        log(f"Training complete: {train_time:.1f}s")
        if compressor_path.exists():
            log(f"Model: {compressor_path} "
                f"({fmt_bytes(compressor_path.stat().st_size)})")

    result["train_time_secs"] = train_time
    result["train_command"] = train_cmd_str
    result["train_output"] = train_output

    # ── Step 3: Compress PBL with trained model ──
    log("─" * 70)
    log("Step 2/3: Compressing PBL with trained model...")
    compress_cmd = [
        str(zli), "compress",
        str(pbl_file.resolve()),
        "--compressor", str(compressor_path.resolve()),
        "--output", str(sddl_output.resolve()),
        "--force",
    ]
    compress_cmd_str = " ".join(compress_cmd)
    log(f"CMD: {compress_cmd_str}")

    rc, stdout, stderr, compress_sddl_time = run_capture(
        compress_cmd, timeout=600, label="Compress SDDL",
    )

    if stdout.strip():
        log(f"  {stdout.strip()}")
    if rc != 0:
        log(f"ERROR: Compression failed (exit code {rc})")
        if stderr.strip():
            log(f"  stderr: {stderr.strip()}")
        result["status"] = "compress_sddl_failed"
        result["compress_sddl_command"] = compress_cmd_str
        result["compress_sddl_time_secs"] = compress_sddl_time
        return result

    sddl_compressed_size = sddl_output.stat().st_size
    log(f"Compression complete: {compress_sddl_time:.1f}s")
    log(f"Output: {sddl_output} ({fmt_bytes(sddl_compressed_size)})")

    result["openzl_sddl_bytes"] = sddl_compressed_size
    result["compress_sddl_secs"] = compress_sddl_time
    result["compress_sddl_command"] = compress_cmd_str
    result["compress_sddl_stdout"] = stdout.strip()

    # ── Step 4: Compress Parquet with built-in parquet profile ──
    log("─" * 70)
    log("Step 3/3: Compressing Parquet with built-in parquet profile...")
    parquet_cmd = [
        str(zli), "compress",
        str(parquet_file.resolve()),
        "--profile", "parquet",
        "--train-inline",
        "--output", str(parquet_output.resolve()),
        "--force",
    ]
    parquet_cmd_str = " ".join(parquet_cmd)
    log(f"CMD: {parquet_cmd_str}")

    rc, stdout, stderr, compress_pq_time = run_capture(
        parquet_cmd, timeout=600, label="Compress Parquet profile",
    )

    if stdout.strip():
        log(f"  {stdout.strip()}")

    if rc != 0:
        log(f"WARNING: Parquet profile compression failed (exit code {rc})")
        if stderr.strip():
            log(f"  stderr: {stderr.strip()}")
        result["openzl_parquet_bytes"] = None
        result["compress_parquet_secs"] = compress_pq_time
        result["compress_parquet_status"] = "failed"
    else:
        pq_compressed_size = parquet_output.stat().st_size
        log(f"Compression complete: {compress_pq_time:.1f}s")
        log(f"Output: {parquet_output} ({fmt_bytes(pq_compressed_size)})")
        result["openzl_parquet_bytes"] = pq_compressed_size
        result["compress_parquet_secs"] = compress_pq_time

    result["compress_parquet_command"] = parquet_cmd_str

    # ── Compute ratios ──
    raw = result["raw_column_bytes"]
    result["openzl_sddl_ratio"] = raw / result["openzl_sddl_bytes"]

    pq_none = result["parquet_none_bytes"]
    if result.get("openzl_parquet_bytes"):
        result["openzl_parquet_ratio"] = pq_none / result["openzl_parquet_bytes"]
    else:
        result["openzl_parquet_ratio"] = None

    pq_zstd = result["parquet_zstd_bytes"]
    result["parquet_zstd_ratio"] = raw / pq_zstd if pq_zstd > 0 else None

    result["status"] = "success"

    # ── Print per-dataset results ──
    print_dataset_results(result)

    return result


def print_dataset_results(r: dict) -> None:
    """Print the comparison table for one dataset."""
    log("─" * 70)
    log(f"Results for {r['dataset']}")
    log("")

    raw = r["raw_column_bytes"]

    rows = [
        ("Raw column data", raw, 1.0, "—"),
        ("Parquet (none)", r["parquet_none_bytes"],
         raw / r["parquet_none_bytes"] if r["parquet_none_bytes"] else 0, "—"),
        ("Parquet (snappy)", r["parquet_snappy_bytes"],
         raw / r["parquet_snappy_bytes"] if r["parquet_snappy_bytes"] else 0, "—"),
        ("Parquet (gzip)", r["parquet_gzip_bytes"],
         raw / r["parquet_gzip_bytes"] if r["parquet_gzip_bytes"] else 0, "—"),
        ("Parquet (zstd)", r["parquet_zstd_bytes"],
         raw / r["parquet_zstd_bytes"] if r["parquet_zstd_bytes"] else 0, "—"),
    ]

    # OpenZL + SDDL
    sddl_bytes = r.get("openzl_sddl_bytes", 0)
    sddl_ratio = r.get("openzl_sddl_ratio", 0)
    train_t = r.get("train_time_secs", 0)
    comp_t = r.get("compress_sddl_secs", 0)
    sddl_time = f"{train_t:.0f}s train + {comp_t:.1f}s compress"
    rows.append(("OpenZL+SDDL (PBL)", sddl_bytes, sddl_ratio, sddl_time))

    # OpenZL Parquet profile
    pq_bytes = r.get("openzl_parquet_bytes")
    if pq_bytes:
        pq_ratio = r.get("openzl_parquet_ratio", 0)
        pq_time = f"{r.get('compress_parquet_secs', 0):.1f}s (inline)"
        rows.append(("OpenZL Parquet", pq_bytes, pq_ratio, pq_time))
    else:
        rows.append(("OpenZL Parquet", 0, 0, "(failed)"))

    # Print table
    log(f"  {'Pipeline':<25s} │ {'Size (MiB)':>11s} │ {'Ratio':>7s} │ Time")
    log(f"  {'─' * 25}┼{'─' * 13}┼{'─' * 9}┼{'─' * 30}")
    for label, size, ratio, timing in rows:
        size_mib = size / (1024 * 1024) if size else 0
        ratio_str = f"{ratio:.2f}x" if ratio else "N/A"
        log(f"  {label:<25s} │ {size_mib:>9.1f}   │ {ratio_str:>7s} │ {timing}")

    # Determine winner
    log("")
    pq_zstd = r["parquet_zstd_bytes"]
    if sddl_bytes and pq_zstd:
        if sddl_bytes < pq_zstd:
            improvement = pq_zstd / sddl_bytes
            log(f"  WINNER: OpenZL+SDDL — {improvement:.2f}x smaller than Parquet+zstd")
        elif sddl_bytes > pq_zstd:
            worse = sddl_bytes / pq_zstd
            log(f"  Parquet+zstd wins — OpenZL is {worse:.2f}x larger")
        else:
            log(f"  TIE: Same size as Parquet+zstd")

    if r.get("skipped_columns"):
        log(f"  Note: Parquet sizes include skipped string columns: "
            f"{', '.join(r['skipped_columns'])}")

    log("═" * 70)


# ─────────────────────────────────────────────────────────────────────────────
# Final summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: list[dict]) -> None:
    """Print the combined summary table."""
    log("")
    log("╔══════════════════╦═══════════╦═══════════════╦═══════════════"
        "═╦═════════════════╦══════════╗")
    log("║ Dataset          ║ Raw (MiB) ║ Parquet+zstd  ║ OpenZL+SDDL  "
        "  ║ OpenZL Parquet  ║ Winner   ║")
    log("╠══════════════════╬═══════════╬═══════════════╬═══════════════"
        "═╬═════════════════╬══════════╣")

    for r in results:
        ds = r["dataset"]
        raw_mib = r["raw_column_bytes"] / (1024 * 1024)

        pq_zstd_mib = r["parquet_zstd_bytes"] / (1024 * 1024)
        pq_zstd_ratio = r.get("parquet_zstd_ratio", 0)
        pq_zstd_str = f"{pq_zstd_mib:>5.1f} ({pq_zstd_ratio:.2f}x)"

        sddl_bytes = r.get("openzl_sddl_bytes", 0)
        sddl_ratio = r.get("openzl_sddl_ratio", 0)
        if sddl_bytes:
            sddl_mib = sddl_bytes / (1024 * 1024)
            sddl_str = f"{sddl_mib:>5.1f} ({sddl_ratio:.2f}x)"
        else:
            sddl_str = "  FAILED       "

        pq_prof_bytes = r.get("openzl_parquet_bytes")
        pq_prof_ratio = r.get("openzl_parquet_ratio")
        if pq_prof_bytes:
            pq_prof_mib = pq_prof_bytes / (1024 * 1024)
            pq_prof_str = f"{pq_prof_mib:>5.1f} ({pq_prof_ratio:.2f}x)"
        else:
            pq_prof_str = "    FAILED       "

        # Determine winner
        if r["status"] != "success":
            winner = "ERROR"
        elif sddl_bytes < r["parquet_zstd_bytes"]:
            winner = "OpenZL"
        elif sddl_bytes > r["parquet_zstd_bytes"]:
            winner = "Parquet"
        else:
            winner = "TIE"

        log(f"║ {ds:<16s} ║ {raw_mib:>7.1f}   ║ {pq_zstd_str:<13s} "
            f"║ {sddl_str:<15s} ║ {pq_prof_str:<15s} ║ {winner:<8s} ║")

    log("╚══════════════════╩═══════════╩═══════════════╩═══════════════"
        "═╩═════════════════╩══════════╝")


def save_results(results: list[dict], results_dir: Path, zli: Path) -> None:
    """Save results to CSV and JSON."""
    results_dir.mkdir(parents=True, exist_ok=True)

    # ── CSV ──
    csv_path = results_dir / "comparison.csv"
    csv_fields = [
        "dataset", "num_rows", "num_cols", "raw_column_bytes", "pbl_bytes",
        "parquet_none_bytes", "parquet_snappy_bytes", "parquet_gzip_bytes",
        "parquet_zstd_bytes", "openzl_sddl_bytes", "openzl_sddl_ratio",
        "openzl_parquet_bytes", "openzl_parquet_ratio", "parquet_zstd_ratio",
        "train_time_secs", "compress_sddl_secs", "compress_parquet_secs",
        "status",
    ]

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    log(f"CSV saved: {csv_path}")

    # ── JSON ──
    # Clean results for JSON (remove large stdout blobs if needed)
    json_results = []
    for r in results:
        clean = {k: v for k, v in r.items()
                 if k != "train_output" or len(str(v)) < 10000}
        if "train_output" in r and len(str(r["train_output"])) >= 10000:
            clean["train_output"] = "(truncated — too large for JSON)"
        json_results.append(clean)

    json_data = {
        "experiment": "openzl_vs_parquet_layout_a",
        "run_date": datetime.now().isoformat(),
        "zli_path": str(zli),
        "datasets": json_results,
    }

    json_path = results_dir / "comparison.json"
    json_path.write_text(json.dumps(json_data, indent=2, default=str))
    log(f"JSON saved: {json_path}")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Benchmark OpenZL vs Parquet compression.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python benchmark_openzl.py                       # All datasets, defaults
  python benchmark_openzl.py --datasets numeric_heavy --max-train-time 60
  python benchmark_openzl.py --skip-existing       # Reuse trained models
""",
    )
    parser.add_argument(
        "--datasets", type=str, default=",".join(ALL_DATASETS),
        help=f"Comma-separated datasets (default: {','.join(ALL_DATASETS)})",
    )
    parser.add_argument(
        "--max-train-time", type=int, default=300,
        help="Max training time per dataset in seconds (default: 300)",
    )
    parser.add_argument(
        "--threads", type=int, default=0,
        help="Threads for training (default: 0 = all CPUs)",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Skip training if model already exists.",
    )
    args = parser.parse_args()

    datasets = [d.strip() for d in args.datasets.split(",")]
    threads = args.threads if args.threads > 0 else (os.cpu_count() or 4)

    # Resolve directories relative to script
    data_dir = SCRIPT_DIR / "data"
    extracted_dir = SCRIPT_DIR / "extracted"
    schema_dir = SCRIPT_DIR / "schemas"
    models_dir = SCRIPT_DIR / "models"
    results_dir = SCRIPT_DIR / "results"

    # Find zli
    try:
        zli = find_zli()
    except FileNotFoundError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    log("═" * 70)
    log("OpenZL vs Parquet Compression Benchmark")
    log(f"zli:            {zli}")
    log(f"Datasets:       {', '.join(datasets)}")
    log(f"Max train time: {args.max_train_time}s per dataset")
    log(f"Threads:        {threads}")
    log(f"Skip existing:  {args.skip_existing}")
    log("═" * 70)

    all_results = []

    for ds in datasets:
        result = benchmark_dataset(
            dataset=ds,
            zli=zli,
            data_dir=data_dir,
            extracted_dir=extracted_dir,
            schema_dir=schema_dir,
            models_dir=models_dir,
            results_dir=results_dir,
            max_train_time=args.max_train_time,
            threads=threads,
            skip_existing=args.skip_existing,
        )
        if result:
            all_results.append(result)

    # ── Final summary ──
    successful = [r for r in all_results if r["status"] == "success"]
    if successful:
        print_summary(successful)
        save_results(all_results, results_dir, zli)
    else:
        log("No successful benchmarks to summarize.")
        if all_results:
            save_results(all_results, results_dir, zli)


if __name__ == "__main__":
    main()
