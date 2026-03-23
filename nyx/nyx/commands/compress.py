"""nyx compress — unified compression command for all file types."""

import os
import shutil
import tempfile
import time
from concurrent.futures import as_completed
from pathlib import Path
from typing import List, Optional

import click
from tqdm import tqdm

from ..core import openzl, preprocessor, archive, sample
from ..core.benchmark import BenchmarkResult, run_benchmarks, print_benchmark_table
from ..core.config import (
    DEFAULT_MAX_TIME_SECS,
    DEFAULT_PROFILE,
    SCHEMA_REGISTRY,
)
from ..core.detect import detect_filetype, detect_fasta_subtype, is_genomic, is_structured
from ..utils.paths import find_schema
from ._lossless import (
    compress_fasta_packed,
    compress_protein_packed,
    compress_csv_fastq,
    compress_vcf,
    compress_jsonl,
    compress_telemetry,
    compress_dns,
    DEFAULT_FASTA_MODELS_DIR,
    DEFAULT_CSV_MODELS_DIR,
    DEFAULT_VCF_MODELS_DIR,
    DEFAULT_JSONL_MODELS_DIR,
    DEFAULT_TELEMETRY_MODELS_DIR,
    DEFAULT_DNS_MODELS_DIR,
)


@click.command("compress")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_file", type=click.Path(),
              default=None, help="Output file path (auto-determined by pipeline).")
@click.option("-t", "--type", "filetype",
              type=click.Choice(["auto", "fasta", "protein", "fastq", "vcf", "jsonl", "telemetry", "dns", "generic"]),
              default="auto",
              help="File type (default: auto-detect). Use 'protein' for protein FASTA.")
@click.option("--mode",
              type=click.Choice(["auto", "lossless", "schema", "generic", "inline"]),
              default="auto",
              help="Compression mode (default: auto-select based on file type).")
@click.option("--train", "do_train", is_flag=True,
              help="Train compressors before compressing (improves ratio).")
@click.option("--models-dir", type=click.Path(file_okay=False),
              default=None, help="Directory for trained compressor models (lossless).")
@click.option("--no-trained", is_flag=True,
              help="Ignore trained compressors, use generic profile.")
@click.option("--group-train", type=click.Path(exists=True, file_okay=False),
              default=None, help="Train from directory of sample files (lossless).")
@click.option("--sddl", type=click.Path(exists=True),
              help="Custom SDDL schema (forces schema mode).")
@click.option("--threads", type=int, default=1,
              help="Encoding/preprocessing threads (default: 1).")
@click.option("--train-threads", type=int, default=None,
              help="OpenZL training threads (default: CPU count).")
@click.option("--max-time-secs", type=int, default=None,
              help=f"Training time limit in seconds (default: {DEFAULT_MAX_TIME_SECS}).")
@click.option("--compress-jobs", type=int, default=None,
              help="Parallel compression jobs (default: CPU count).")
@click.option("--train-sample-mib", type=int, default=200,
              help="Training sample size in MiB (default: 200).")
@click.option("--trainer", type=click.Choice(["greedy", "full-split", "bottom-up"]),
              default=None, help="Training algorithm (schema mode).")
@click.option("--no-clustering", is_flag=True,
              help="Skip clustering during training (schema mode).")
@click.option("--benchmark", is_flag=True,
              help="Run competitor benchmarks (gzip, pigz, zstd).")
@click.option("--keep-temp", is_flag=True, help="Keep temporary directories.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output file.")
def compress_cmd(input_file, output_file, filetype, mode, do_train, models_dir,
                 no_trained, group_train, sddl, threads, train_threads,
                 max_time_secs, compress_jobs, train_sample_mib, trainer,
                 no_clustering, benchmark, keep_temp, verbose, force):
    """Compress a file using Nyx.

    Supports lossless compression for FASTA and FASTQ, schema-aware
    compression for genomic files with SDDL schemas, and generic
    compression for any file.

    \b
    Auto-routing (--mode auto, the default):
      FASTA file  → lossless packed (auto-detects nucleotide/protein) → .zlfasta
      FASTQ file  → lossless CSV decomposition                       → .zlfastq
      JSONL file  → lossless type-grouped TSV decomposition           → .zljsonl
      Other       → generic OpenZL compression                       → .nyx

    \b
    Compression modes (--mode):
      auto      Auto-select the best mode based on file type (default)
      lossless  Byte-exact lossless compression (FASTA/FASTQ only)
      schema    Schema-aware compression using SDDL (genomic files)
      generic   Generic OpenZL compression (any file, fast)
      inline    Generic with inline training (moderate compression, no separate train step)

    \b
    File type override (--type):
      auto      Auto-detect from file extension and content (default)
      fasta     Force FASTA (nucleotide) processing
      protein   Force protein FASTA processing
      fastq     Force FASTQ processing
      vcf       Force VCF processing
      generic   Force generic (non-genomic) processing

    \b
    Lossless pipeline flags:
      --train                 Train stream compressors before compressing (better ratio)
      --models-dir DIR        Directory to store/load trained compressor models
      --no-trained            Ignore any trained compressors, use generic profile
      --group-train DIR       Train from a directory of sample files instead of the input

    \b
    Schema pipeline flags:
      --sddl PATH             Path to a custom SDDL schema file (forces --mode schema)
      --trainer ALGO          Training algorithm: greedy, full-split, or bottom-up
      --no-clustering         Skip clustering during training

    \b
    Performance flags:
      --threads N             Encoding/preprocessing threads (default: 1)
      --train-threads N       OpenZL training threads (default: all CPUs)
      --max-time-secs N       Training time budget in seconds (default: 1800)
      --compress-jobs N       Parallel chunk compression jobs (default: all CPUs)
      --train-sample-mib N    Training sample size in MiB (default: 200)

    \b
    Output and behavior:
      -o, --output PATH       Output file path (auto-determined if omitted)
      -f, --force             Overwrite existing output file
      -v, --verbose           Verbose output
      --benchmark             Run gzip, pigz, zstd benchmarks and print comparison table
      --keep-temp             Keep temporary directories for debugging

    \b
    Examples:
      nyx compress genome.fasta                  # lossless FASTA → .zlfasta
      nyx compress reads.fastq                   # lossless FASTQ → .zlfastq
      nyx compress proteins.fasta --type protein # protein FASTA → .zlfasta
      nyx compress genome.fasta --train          # train + lossless compress
      nyx compress data.bin                      # generic → .nyx
      nyx compress genome.fasta --mode schema    # schema-aware → .nyx
      nyx compress genome.fasta --benchmark      # compress + benchmark competitors
      nyx compress reads.fastq --train --group-train /path/to/samples/
      nyx compress telemetry.jsonl                   # lossless JSONL → .zljsonl
      nyx compress telemetry.jsonl --train           # train + lossless compress
      nyx compress telemetry.jsonl --train --group-train /path/to/jsonl/
    """
    input_path = Path(input_file).resolve()
    train_threads = train_threads or (os.cpu_count() or 4)
    max_time_secs = max_time_secs or DEFAULT_MAX_TIME_SECS
    compress_jobs = compress_jobs or (os.cpu_count() or 4)
    train_sample_bytes = train_sample_mib * 1024 * 1024

    # --- Detect file type ---
    if filetype == "auto":
        detected = detect_filetype(input_path)
    elif filetype == "protein":
        detected = "fasta"
    elif filetype == "telemetry":
        detected = "telemetry"
    elif filetype == "dns":
        detected = "dns"
    elif filetype == "generic":
        detected = None
    else:
        detected = filetype

    genomic = is_genomic(detected)

    # --- If --sddl provided, force schema mode ---
    if sddl and mode == "auto":
        mode = "schema"

    structured = is_structured(detected)

    # --- Auto-select mode ---
    if mode == "auto":
        if detected in ("fasta", "fastq", "vcf", "jsonl", "telemetry", "dns"):
            mode = "lossless"
        elif genomic:
            cfg = SCHEMA_REGISTRY.get(detected)
            if cfg and cfg.sddl:
                mode = "schema"
            else:
                mode = "generic"
        else:
            mode = "generic"

    # --- Validate mode + options ---
    if mode == "lossless" and detected not in ("fasta", "fastq", "vcf", "jsonl", "telemetry", "dns"):
        raise click.UsageError(
            f"Lossless mode requires FASTA, FASTQ, VCF, or JSONL input, "
            f"but detected: {detected or 'unknown'}. "
            f"Use --type to override or --mode generic."
        )

    schema_mode = None
    if mode == "schema":
        if sddl:
            schema_mode = "train_custom"
        elif genomic:
            cfg = SCHEMA_REGISTRY.get(detected)
            if not cfg or not cfg.sddl:
                raise click.UsageError(
                    f"No SDDL schema available for '{detected}'. "
                    f"Provide --sddl <path> or use --mode generic."
                )
            schema_mode = "train_plain"
        else:
            raise click.UsageError(
                "Schema mode requires a genomic file or --sddl. "
                "Use --mode generic for non-genomic files."
            )

    # --- Determine output path ---
    if output_file is None:
        if mode == "lossless" and detected == "fasta":
            output_path = input_path.parent / (input_path.name + ".zlfasta")
        elif mode == "lossless" and detected == "fastq":
            output_path = input_path.parent / (input_path.name + ".zlfastq")
        elif mode == "lossless" and detected == "vcf":
            output_path = input_path.parent / (input_path.name + ".zlvcf")
        elif mode == "lossless" and detected == "jsonl":
            output_path = input_path.parent / (input_path.name + ".zljsonl")
        elif mode == "lossless" and detected == "telemetry":
            output_path = input_path.parent / (input_path.name + ".zljsonl")
        elif mode == "lossless" and detected == "dns":
            output_path = input_path.parent / (input_path.name + ".zldns")
        else:
            output_path = input_path.parent / (input_path.name + ".nyx")
    else:
        output_path = Path(output_file).resolve()

    if output_path.exists() and not force:
        raise click.UsageError(
            f"Output file exists: {output_path}. Use -f/--force to overwrite."
        )

    # --- Route to pipeline ---
    total_start = time.monotonic()

    if mode == "lossless":
        _run_lossless(
            input_path=input_path,
            output_path=output_path,
            detected=detected,
            filetype=filetype,
            do_train=do_train,
            models_dir=models_dir,
            no_trained=no_trained,
            group_train=group_train,
            verbose=verbose,
            threads=threads,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            compress_jobs=compress_jobs,
            train_sample_bytes=train_sample_bytes,
        )
    else:
        # Schema, generic, or inline — managed with shared tmpdir
        tmpdir = Path(tempfile.mkdtemp(prefix="nyx_"))
        try:
            if mode == "schema":
                click.echo(f"Compressing {input_path.name} "
                           f"(mode=schema, type={detected or 'generic'})")
                _compress_trained(
                    input_path=input_path,
                    output_path=output_path,
                    mode=schema_mode,
                    sddl_path=Path(sddl) if sddl else None,
                    detected=detected,
                    tmpdir=tmpdir,
                    threads=threads,
                    max_time_secs=max_time_secs,
                    compress_jobs=compress_jobs,
                    target_train_mib=train_sample_mib,
                    trainer=trainer,
                    no_clustering=no_clustering,
                    verbose=verbose,
                )
            elif mode == "generic":
                click.echo(f"Compressing {input_path.name} (mode=generic)")
                _compress_default(
                    input_path=input_path,
                    output_path=output_path,
                    tmpdir=tmpdir,
                    verbose=verbose,
                )
            elif mode == "inline":
                click.echo(f"Compressing {input_path.name} "
                           f"(mode=inline, type={detected or 'generic'})")
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
        finally:
            if not keep_temp:
                shutil.rmtree(tmpdir, ignore_errors=True)
            else:
                click.echo(f"Temp directory kept: {tmpdir}")

    # --- Benchmark ---
    if benchmark:
        total_secs = time.monotonic() - total_start
        size_in = input_path.stat().st_size
        size_out = output_path.stat().st_size
        click.echo("\nRunning competitor benchmarks on original file...")
        nyx_result = BenchmarkResult(
            name="nyx",
            original_bytes=size_in,
            compressed_bytes=size_out,
            compress_secs=total_secs,
        )
        bench_tmpdir = Path(tempfile.mkdtemp(prefix="nyx_bench_"))
        try:
            competitor_results = run_benchmarks(input_path, bench_tmpdir)
            print_benchmark_table(nyx_result, competitor_results)
        finally:
            shutil.rmtree(bench_tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Lossless routing
# ---------------------------------------------------------------------------

def _run_lossless(input_path, output_path, detected, filetype, do_train,
                  models_dir, no_trained, group_train, verbose, threads,
                  train_threads, max_time_secs, compress_jobs, train_sample_bytes):
    """Route to the appropriate lossless compression pipeline."""
    if detected == "fasta":
        if filetype == "protein":
            is_protein = True
        else:
            subtype = detect_fasta_subtype(input_path)
            is_protein = (subtype == "protein")

        fasta_models = Path(models_dir) if models_dir else DEFAULT_FASTA_MODELS_DIR

        if is_protein:
            click.echo(f"Compressing {input_path.name} (lossless PROTEIN FASTA)")
            compress_protein_packed(
                input_path=input_path,
                output_path=output_path,
                models_dir=fasta_models,
                do_train=do_train,
                no_trained=no_trained,
                verbose=verbose,
                threads=threads,
                train_threads=train_threads,
                max_time_secs=max_time_secs,
                train_sample_bytes=train_sample_bytes,
                compress_jobs=compress_jobs,
                group_train_dir=group_train,
            )
        else:
            click.echo(f"Compressing {input_path.name} (lossless NUCLEOTIDE FASTA)")
            compress_fasta_packed(
                input_path=input_path,
                output_path=output_path,
                models_dir=fasta_models,
                do_train=do_train,
                no_trained=no_trained,
                verbose=verbose,
                threads=threads,
                train_threads=train_threads,
                max_time_secs=max_time_secs,
                train_sample_bytes=train_sample_bytes,
                compress_jobs=compress_jobs,
                group_train_dir=group_train,
            )
    elif detected == "fastq":
        click.echo(f"Compressing {input_path.name} (lossless FASTQ CSV)")
        csv_models = Path(models_dir) if models_dir else DEFAULT_CSV_MODELS_DIR
        compress_csv_fastq(
            input_path=input_path,
            output_path=output_path,
            models_dir=csv_models,
            do_train=do_train,
            no_trained=no_trained,
            verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
    elif detected == "vcf":
        click.echo(f"Compressing {input_path.name} (lossless VCF)")
        vcf_models = Path(models_dir) if models_dir else DEFAULT_VCF_MODELS_DIR
        compress_vcf(
            input_path=input_path,
            output_path=output_path,
            models_dir=vcf_models,
            do_train=do_train,
            no_trained=no_trained,
            verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
    elif detected == "telemetry":
        click.echo(f"Compressing {input_path.name} (lossless telemetry JSONL)")
        tel_models = Path(models_dir) if models_dir else DEFAULT_TELEMETRY_MODELS_DIR
        compress_telemetry(
            input_path=input_path,
            output_path=output_path,
            models_dir=tel_models,
            do_train=do_train,
            no_trained=no_trained,
            verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
    elif detected == "jsonl":
        click.echo(f"Compressing {input_path.name} (lossless JSONL)")
        jsonl_models = Path(models_dir) if models_dir else DEFAULT_JSONL_MODELS_DIR
        compress_jsonl(
            input_path=input_path,
            output_path=output_path,
            models_dir=jsonl_models,
            do_train=do_train,
            no_trained=no_trained,
            verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
    elif detected == "dns":
        click.echo(f"Compressing {input_path.name} (lossless DNS TSV)")
        dns_models = Path(models_dir) if models_dir else DEFAULT_DNS_MODELS_DIR
        compress_dns(
            input_path=input_path,
            output_path=output_path,
            models_dir=dns_models,
            do_train=do_train,
            no_trained=no_trained,
            verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )


# ---------------------------------------------------------------------------
# Pipeline: schema (train_plain / train_custom)
# ---------------------------------------------------------------------------

def _compress_trained(
    input_path: Path,
    output_path: Path,
    mode: str,
    sddl_path: Optional[Path],
    detected: Optional[str],
    tmpdir: Path,
    threads: int,
    max_time_secs: int,
    compress_jobs: int,
    target_train_mib: int,
    trainer: Optional[str],
    no_clustering: bool,
    verbose: bool,
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
# Pipeline: generic
# ---------------------------------------------------------------------------

def _compress_default(
    input_path: Path,
    output_path: Path,
    tmpdir: Path,
    verbose: bool,
) -> None:
    """Pipeline for generic mode (generic openzl compression)."""
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
# Pipeline: inline
# ---------------------------------------------------------------------------

def _compress_inline(
    input_path: Path,
    output_path: Path,
    detected: Optional[str],
    genomic: bool,
    tmpdir: Path,
    threads: int,
    verbose: bool,
) -> None:
    """Pipeline for inline training mode."""
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
    chunks: List[Path],
    compressor: Path,
    jobs: int,
    verbose: bool,
) -> List[Path]:
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
