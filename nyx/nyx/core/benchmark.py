"""Run compression benchmarks against generic competitors (gzip, pigz, zstd)."""

import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import click
from tqdm import tqdm


@dataclass
class BenchmarkResult:
    """Result of a single compression benchmark run."""

    name: str
    original_bytes: int
    compressed_bytes: int
    compress_secs: float
    available: bool = True

    @property
    def ratio(self) -> float:
        if self.compressed_bytes == 0:
            return 0.0
        return self.original_bytes / self.compressed_bytes

    @property
    def savings_pct(self) -> float:
        if self.original_bytes == 0:
            return 0.0
        return (1 - self.compressed_bytes / self.original_bytes) * 100

    @property
    def speed_mbps(self) -> float:
        if self.compress_secs == 0:
            return 0.0
        return (self.original_bytes / (1024 * 1024)) / self.compress_secs


# Competitors: practical defaults, fast-to-slow order.
# gzip -1 (fast baseline), pigz -9 (parallel best), zstd -3 (fast modern), zstd -9 (good ratio)
COMPETITORS = [
    {
        "name": "gzip -1",
        "binary": "gzip",
        "cmd": ["gzip", "-1", "-k", "-c"],
    },
    {
        "name": "pigz -9",
        "binary": "pigz",
        "cmd": ["pigz", "-9", "-k", "-c"],
    },
    {
        "name": "zstd -3",
        "binary": "zstd",
        "cmd": ["zstd", "-3", "-c", "--no-progress"],
    },
    {
        "name": "zstd -9",
        "binary": "zstd",
        "cmd": ["zstd", "-9", "-c", "--no-progress"],
    },
]


def _run_competitor(
    spec: dict,
    input_file: Path,
    output_file: Path,
) -> Optional[BenchmarkResult]:
    """Run a single competitor and return the result."""
    binary = spec["binary"]
    if not shutil.which(binary):
        return BenchmarkResult(
            name=spec["name"],
            original_bytes=input_file.stat().st_size,
            compressed_bytes=0,
            compress_secs=0.0,
            available=False,
        )

    original_size = input_file.stat().st_size
    cmd = spec["cmd"] + [str(input_file)]

    # Run subprocess in a thread so we can update a progress spinner
    result_holder = {}
    done_event = threading.Event()

    def _run():
        with open(output_file, "wb") as out_f:
            result_holder["proc"] = subprocess.run(
                cmd,
                stdout=out_f,
                stderr=subprocess.PIPE,
            )
        done_event.set()

    start = time.monotonic()
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    # Show elapsed time while waiting
    size_mib = original_size / (1024 * 1024)
    pbar = tqdm(
        total=None,
        desc=f"  {spec['name']:<10}",
        bar_format="{desc} {elapsed} ({rate_fmt})",
        unit="MiB",
        dynamic_ncols=True,
    )

    while not done_event.wait(timeout=0.5):
        elapsed = time.monotonic() - start
        if elapsed > 0:
            # Estimate progress by checking output file size
            try:
                written = output_file.stat().st_size if output_file.exists() else 0
                pbar.n = written / (1024 * 1024)
                pbar.refresh()
            except OSError:
                pass

    thread.join()
    elapsed = time.monotonic() - start
    pbar.close()

    proc = result_holder.get("proc")
    if proc is None or proc.returncode != 0:
        click.echo(f"  {spec['name']:<10} failed")
        return BenchmarkResult(
            name=spec["name"],
            original_bytes=original_size,
            compressed_bytes=0,
            compress_secs=elapsed,
            available=False,
        )

    compressed_size = output_file.stat().st_size if output_file.is_file() else 0

    click.echo(
        f"  {spec['name']:<10} {_human_size(compressed_size)} "
        f"({original_size / compressed_size:.2f}x) in {_format_time(elapsed)}"
    )

    # Clean up
    if output_file.is_file():
        output_file.unlink()

    return BenchmarkResult(
        name=spec["name"],
        original_bytes=original_size,
        compressed_bytes=compressed_size,
        compress_secs=elapsed,
    )


def run_benchmarks(input_file: Path, tmpdir: Path) -> List[BenchmarkResult]:
    """Run all available competitor benchmarks on the input file."""
    results = []

    for spec in COMPETITORS:
        binary = spec["binary"]
        if not shutil.which(binary):
            click.echo(f"  {spec['name']:<10} skipped ({binary} not installed)")
            results.append(BenchmarkResult(
                name=spec["name"],
                original_bytes=input_file.stat().st_size,
                compressed_bytes=0,
                compress_secs=0.0,
                available=False,
            ))
            continue

        suffix = spec["name"].replace(" ", "_")
        output = tmpdir / f"benchmark_{suffix}"
        result = _run_competitor(spec, input_file, output)
        if result:
            results.append(result)

    return results


def print_benchmark_table(
    nyx_result: BenchmarkResult,
    competitor_results: List[BenchmarkResult],
) -> None:
    """Print a formatted comparison table."""
    all_results = [nyx_result] + competitor_results
    available = [r for r in all_results if r.available]
    unavailable = [r for r in all_results if not r.available]

    # Find column widths
    name_width = max(len(r.name) for r in available) if available else 10

    click.echo("")
    click.echo("=" * 72)
    click.echo("  COMPRESSION BENCHMARK RESULTS")
    click.echo("=" * 72)
    click.echo(
        f"  {'Compressor':<{name_width}}  "
        f"{'Compressed':>12}  "
        f"{'Ratio':>7}  "
        f"{'Savings':>8}  "
        f"{'Speed':>10}  "
        f"{'Time':>8}"
    )
    click.echo("-" * 72)

    for r in sorted(available, key=lambda x: x.compressed_bytes if x.compressed_bytes > 0 else float("inf")):
        marker = " *" if r.name == nyx_result.name else "  "
        click.echo(
            f"{marker}{r.name:<{name_width}}  "
            f"{_human_size(r.compressed_bytes):>12}  "
            f"{r.ratio:>6.2f}x  "
            f"{r.savings_pct:>6.1f}%  "
            f"{r.speed_mbps:>7.1f} MB/s  "
            f"{_format_time(r.compress_secs):>8}"
        )

    if unavailable:
        click.echo("-" * 72)
        for r in unavailable:
            click.echo(f"  {r.name:<{name_width}}  (not installed)")

    click.echo("=" * 72)
    click.echo(f"  Original size: {_human_size(nyx_result.original_bytes)}")
    click.echo(f"  * = nyx")
    click.echo("")


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _format_time(secs: float) -> str:
    if secs < 60:
        return f"{secs:.1f}s"
    minutes = int(secs // 60)
    remaining = secs % 60
    return f"{minutes}m{remaining:.0f}s"
