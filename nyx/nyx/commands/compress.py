"""nyx compress — the primary command orchestrating the full pipeline."""

import shutil
import tempfile
import time
from concurrent.futures import as_completed
from pathlib import Path

import click
from tqdm import tqdm

from ..core import openzl, preprocessor, archive, sample
from ..core.benchmark import BenchmarkResult, run_benchmarks, print_benchmark_table
from ..core.config import (
    DEFAULT_COMPRESS_JOBS,
    DEFAULT_MAX_TIME_SECS,
    DEFAULT_THREADS,
    DEFAULT_TRAIN_MIB,
    DEFAULT_PROFILE,
    SCHEMA_REGISTRY,
)
from ..core.detect import detect_filetype, is_genomic
from ..utils.paths import find_schema


@click.command("compress")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_file", type=click.Path(),
              default=None, help="Output .nyx file path (default: <input>.nyx).")
@click.option("--mode", "mode",
              type=click.Choice(["train_plain", "train_custom", "default",
                                 "inline_train"]),
              default=None,
              help="Compression mode. Auto-selected if omitted.")
@click.option("--sddl", type=click.Path(exists=True),
              help="Path to SDDL schema file (required for train_custom).")
@click.option("--type", "filetype",
              type=click.Choice(["fasta", "fastq", "vcf"]),
              default=None, help="Override auto-detected file type.")
@click.option("--threads", type=int, default=None,
              help=f"Thread count for training/preprocessing (default: {DEFAULT_THREADS}).")
@click.option("--max-time-secs", type=int, default=None,
              help=f"Training time budget in seconds (default: {DEFAULT_MAX_TIME_SECS}).")
@click.option("--compress-jobs", type=int, default=DEFAULT_COMPRESS_JOBS,
              help=f"Parallel compression jobs (default: {DEFAULT_COMPRESS_JOBS}).")
@click.option("--target-train-mib", type=int, default=DEFAULT_TRAIN_MIB,
              help=f"Training sample size in MiB (default: {DEFAULT_TRAIN_MIB}).")
@click.option("--trainer", type=click.Choice(["greedy", "full-split", "bottom-up"]),
              default=None, help="Training algorithm (default: greedy).")
@click.option("--no-clustering", is_flag=True,
              help="Skip clustering during training.")
@click.option("--ace-successors", is_flag=True,
              help="Enable ACE successor models during training. "
                   "May improve ratio on in-distribution data but can hurt generalization.")
@click.option("--benchmark", is_flag=True,
              help="Run competitor benchmarks (gzip, pigz, zstd) and print comparison table.")
@click.option("--keep-temp", is_flag=True, help="Keep temporary directories.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.option("-f", "--force", is_flag=True, help="Overwrite output file.")
def compress_cmd(input_file, output_file, mode, sddl, filetype, threads,
                 max_time_secs, compress_jobs, target_train_mib, trainer,
                 no_clustering, ace_successors, benchmark, keep_temp, verbose,
                 force):
    """Compress a file using OpenZL.

    Supports genomic formats (FASTA, FASTQ, VCF) with schema-aware compression,
    and generic files with standard compression.

    Use --benchmark to compare results against gzip, pigz, and zstd.
    """
    input_path = Path(input_file).resolve()
    threads = threads or DEFAULT_THREADS
    max_time_secs = max_time_secs or DEFAULT_MAX_TIME_SECS

    # Detect file type
    detected = filetype or detect_filetype(input_path)
    genomic = is_genomic(detected)

    # Auto-select mode if not specified
    if mode is None:
        mode = "train_plain" if genomic else "default"

    # Validate mode + options
    if mode == "train_custom" and not sddl:
        raise click.UsageError("--sddl is required for train_custom mode.")

    if mode == "train_plain" and not genomic:
        raise click.UsageError(
            f"train_plain requires a genomic file type (fasta/fastq/vcf), "
            f"but detected: {detected or 'unknown'}. "
            f"Use --type to override or --mode default."
        )

    if mode == "train_plain":
        cfg = SCHEMA_REGISTRY[detected]
        if cfg.sddl is None:
            raise click.UsageError(
                f"No SDDL schema available yet for '{detected}'. "
                f"Use --mode train_custom --sddl <path> instead."
            )

    # Output path
    if output_file is None:
        output_file = str(input_path) + ".nyx"
    output_path = Path(output_file).resolve()

    if output_path.exists() and not force:
        raise click.UsageError(
            f"Output file exists: {output_path}. Use -f/--force to overwrite."
        )

    click.echo(f"Compressing {input_path.name} (mode={mode}, type={detected or 'generic'})")

    tmpdir = Path(tempfile.mkdtemp(prefix="nyx_"))
    total_start = time.monotonic()

    try:
        if mode in ("train_plain", "train_custom"):
            _compress_trained(
                input_path=input_path,
                output_path=output_path,
                mode=mode,
                sddl_path=Path(sddl) if sddl else None,
                detected=detected,
                tmpdir=tmpdir,
                threads=threads,
                max_time_secs=max_time_secs,
                compress_jobs=compress_jobs,
                target_train_mib=target_train_mib,
                trainer=trainer,
                no_clustering=no_clustering,
                no_ace_successors=not ace_successors,
                verbose=verbose,
            )
        elif mode == "default":
            _compress_default(
                input_path=input_path,
                output_path=output_path,
                tmpdir=tmpdir,
                verbose=verbose,
            )
        elif mode == "inline_train":
            _compress_inline(
                input_path=input_path,
                output_path=output_path,
                detected=detected,
                genomic=genomic,
                tmpdir=tmpdir,
                threads=threads,
                verbose=verbose,
            )

        total_secs = time.monotonic() - total_start
        size_in = input_path.stat().st_size
        size_out = output_path.stat().st_size
        ratio = size_in / size_out if size_out > 0 else 0
        click.echo(
            f"\nDone: {_human_size(size_in)} -> {_human_size(size_out)} "
            f"({ratio:.2f}x) in {_format_time(total_secs)}"
        )

        # Run competitor benchmarks if requested
        if benchmark:
            click.echo("\nRunning competitor benchmarks on original file...")
            nyx_result = BenchmarkResult(
                name="nyx",
                original_bytes=size_in,
                compressed_bytes=size_out,
                compress_secs=total_secs,
            )
            competitor_results = run_benchmarks(input_path, tmpdir)
            print_benchmark_table(nyx_result, competitor_results)

    finally:
        if not keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            click.echo(f"Temp directory kept: {tmpdir}")


# ---------------------------------------------------------------------------
# Pipeline: train_plain / train_custom
# ---------------------------------------------------------------------------

def _compress_trained(
    input_path: Path,
    output_path: Path,
    mode: str,
    sddl_path: Path | None,
    detected: str | None,
    tmpdir: Path,
    threads: int,
    max_time_secs: int,
    compress_jobs: int,
    target_train_mib: int,
    trainer: str | None,
    no_clustering: bool,
    no_ace_successors: bool = True,
    verbose: bool = False,
) -> None:
    """Pipeline for train_plain and train_custom modes."""
    cfg = SCHEMA_REGISTRY[detected]

    # Resolve SDDL schema
    if mode == "train_plain":
        schema_path = find_schema(cfg.sddl)
    else:
        schema_path = Path(sddl_path).resolve()

    preprocessor_type = cfg.preprocessor_type

    # Step 1: Create training sample
    with _step_progress("Step 1/5", "Creating training sample") as pbar:
        if detected == "fasta":
            train_sample = tmpdir / "train_sample.fasta"
            sample.create_training_sample(
                input_path, train_sample, target_mib=target_train_mib,
                verbose=verbose,
            )
        else:
            train_sample = input_path
        pbar.update(1)

    # Step 2: Preprocess training sample
    with _step_progress("Step 2/5", "Preprocessing training sample") as pbar:
        train_chunks_dir = tmpdir / "chunks_train"
        train_chunks = preprocessor.preprocess(
            train_sample, train_chunks_dir, threads=1,
            filetype=preprocessor_type, verbose=verbose,
        )
        pbar.update(1)
    click.echo(f"  {len(train_chunks)} training chunk(s)")

    # Step 3: Train compressor (long-running — live progress bar)
    click.echo("Step 3/5: Training compressor...")
    compressor_path = tmpdir / "compressor.model"
    proc = openzl.train_async(
        sample_dir=train_chunks_dir,
        output_file=compressor_path,
        profile="sddl",
        profile_arg=str(schema_path),
        threads=threads,
        max_time_secs=max_time_secs,
        no_ace_successors=no_ace_successors,
        trainer=trainer,
        no_clustering=no_clustering,
        verbose=verbose,
    )
    _wait_with_timer(proc, "Training", max_time_secs)

    # Step 4: Preprocess full file
    with _step_progress("Step 4/5", "Preprocessing full file") as pbar:
        full_chunks_dir = tmpdir / "chunks_full"
        full_chunks = preprocessor.preprocess(
            input_path, full_chunks_dir, threads=threads,
            filetype=preprocessor_type, verbose=verbose,
        )
        pbar.update(1)
    click.echo(f"  {len(full_chunks)} chunk(s)")

    # Step 5: Compress all chunks in parallel (per-chunk progress)
    click.echo(f"Step 5/5: Compressing chunks ({compress_jobs} parallel jobs)")
    compressed_chunks = _compress_chunks_parallel(
        full_chunks, compressor_path, compress_jobs, verbose,
    )

    # Bundle into .nyx archive
    manifest = {
        "original_filename": input_path.name,
        "filetype": detected,
        "mode": mode,
        "binary_format": preprocessor_type,
        "schema": cfg.sddl if mode == "train_plain" else sddl_path.name,
        "chunk_count": len(compressed_chunks),
        "has_compressor": True,
        "nyx_version": "0.1.0",
    }
    archive.create_archive(output_path, manifest, compressed_chunks,
                           compressor_path)


# ---------------------------------------------------------------------------
# Pipeline: default
# ---------------------------------------------------------------------------

def _compress_default(
    input_path: Path,
    output_path: Path,
    tmpdir: Path,
    verbose: bool,
) -> None:
    """Pipeline for default mode (generic openzl compression)."""
    with _step_progress("Compressing", "generic profile") as pbar:
        compressed = tmpdir / (input_path.name + ".zl")
        openzl.compress(
            input_path, compressed,
            profile=DEFAULT_PROFILE,
            verbose=verbose,
        )
        pbar.update(1)

    manifest = {
        "original_filename": input_path.name,
        "filetype": "generic",
        "mode": "default",
        "binary_format": None,
        "schema": None,
        "chunk_count": 1,
        "has_compressor": False,
        "nyx_version": "0.1.0",
    }
    archive.create_archive(output_path, manifest, [compressed])


# ---------------------------------------------------------------------------
# Pipeline: inline_train
# ---------------------------------------------------------------------------

def _compress_inline(
    input_path: Path,
    output_path: Path,
    detected: str | None,
    genomic: bool,
    tmpdir: Path,
    threads: int,
    verbose: bool,
) -> None:
    """Pipeline for inline_train mode."""
    if genomic:
        cfg = SCHEMA_REGISTRY[detected]

        with _step_progress("Step 1/2", "Preprocessing") as pbar:
            chunks_dir = tmpdir / "chunks"
            chunks = preprocessor.preprocess(
                input_path, chunks_dir, threads=threads,
                filetype=cfg.preprocessor_type, verbose=verbose,
            )
            pbar.update(1)

        compressed_chunks = []
        desc = "Step 2/2: Compressing chunks"
        for chunk in tqdm(chunks, desc=desc, unit="chunk", dynamic_ncols=True):
            out = chunk.parent / (chunk.name + ".zl")
            openzl.compress(
                chunk, out,
                profile="sddl",
                profile_arg=str(find_schema(cfg.sddl)),
                train_inline=True,
                verbose=verbose,
            )
            compressed_chunks.append(out)

        manifest = {
            "original_filename": input_path.name,
            "filetype": detected,
            "mode": "inline_train",
            "binary_format": cfg.preprocessor_type,
            "schema": cfg.sddl,
            "chunk_count": len(compressed_chunks),
            "has_compressor": False,
            "nyx_version": "0.1.0",
        }
        archive.create_archive(output_path, manifest, compressed_chunks)
    else:
        with _step_progress("Compressing", "inline training") as pbar:
            compressed = tmpdir / (input_path.name + ".zl")
            openzl.compress(
                input_path, compressed,
                profile=DEFAULT_PROFILE,
                train_inline=True,
                verbose=verbose,
            )
            pbar.update(1)

        manifest = {
            "original_filename": input_path.name,
            "filetype": "generic",
            "mode": "inline_train",
            "binary_format": None,
            "schema": None,
            "chunk_count": 1,
            "has_compressor": False,
            "nyx_version": "0.1.0",
        }
        archive.create_archive(output_path, manifest, [compressed])


# ---------------------------------------------------------------------------
# Parallel chunk compression with progress bar
# ---------------------------------------------------------------------------

def _compress_chunks_parallel(
    chunks: list[Path],
    compressor: Path,
    jobs: int,
    verbose: bool,
) -> list[Path]:
    """Compress multiple chunks in parallel with a tqdm progress bar."""
    compressed = []

    def _compress_one(chunk: Path) -> Path:
        out = chunk.parent / (chunk.name + ".zl")
        openzl.compress(chunk, out, compressor=compressor, verbose=verbose)
        return out

    pbar = tqdm(
        total=len(chunks),
        desc="  Chunks",
        unit="chunk",
        dynamic_ncols=True,
    )

    if len(chunks) <= 1 or jobs <= 1:
        for chunk in chunks:
            compressed.append(_compress_one(chunk))
            pbar.update(1)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(jobs, len(chunks))) as pool:
            futures = {pool.submit(_compress_one, c): c for c in chunks}
            for future in as_completed(futures):
                compressed.append(future.result())
                pbar.update(1)

    pbar.close()
    return sorted(compressed)


# ---------------------------------------------------------------------------
# Progress bar helpers
# ---------------------------------------------------------------------------

class _step_progress:
    """Context manager showing a tqdm bar for a single-unit step."""

    def __init__(self, prefix: str, description: str):
        self.desc = f"{prefix}: {description}"

    def __enter__(self):
        self._bar = tqdm(
            total=1,
            desc=self.desc,
            bar_format="{desc} {elapsed}",
            dynamic_ncols=True,
        )
        return self._bar

    def __exit__(self, *args):
        self._bar.update(1 - self._bar.n)  # Ensure it's complete
        self._bar.close()


def _wait_with_timer(proc, description: str, max_secs: int) -> None:
    """Wait for a subprocess while showing a live time-budget progress bar."""
    from ..core.openzl import OpenZLError

    pbar = tqdm(
        total=max_secs,
        desc=f"  {description}",
        unit="s",
        bar_format="  {desc}: |{bar}| {n:.0f}/{total:.0f}s [{elapsed}<{remaining}]",
        dynamic_ncols=True,
    )

    start = time.monotonic()
    while proc.poll() is None:
        elapsed = time.monotonic() - start
        pbar.n = min(int(elapsed), max_secs)
        pbar.refresh()
        time.sleep(1.0)

    elapsed = time.monotonic() - start
    # Snap bar to actual elapsed time so it looks complete (not 92/1800)
    actual = int(elapsed)
    pbar.total = actual
    pbar.n = actual
    pbar.refresh()
    pbar.close()

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise OpenZLError(
            f"Training failed with exit code {proc.returncode}\n"
            f"Stderr: {stderr.strip()}"
        )

    click.echo(f"  Training completed in {_format_time(elapsed)}")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _human_size(n: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _format_time(secs: float) -> str:
    """Format seconds as a human-readable string."""
    if secs < 60:
        return f"{secs:.1f}s"
    minutes = int(secs // 60)
    remaining = secs % 60
    return f"{minutes}m{remaining:.0f}s"
