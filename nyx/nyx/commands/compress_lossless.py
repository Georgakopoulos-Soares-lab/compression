"""nyx compress-lossless — Unified lossless FASTA/FASTQ compression with auto-detection."""

import concurrent.futures
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import codec, fastq_codec, openzl
from ..core.detect import detect_filetype
from ..core.zlfasta import create_zlfasta
from ..core.zlfastq import create_zlfastq

# Streams per format that get compressed with OpenZL
_FASTA_STREAMS = {
    "meta.bin",
    "headers.bin",
    "nmask.bin",
    "acgtmask.bin",
    "bases2.bin",
    "exceptions.bin",
    "case.bin",
    "wrapping.bin",
}

_FASTQ_STREAMS = {
    "meta.bin",
    "headers.bin",
    "plus.bin",
    "nmask.bin",
    "acgtmask.bin",
    "bases2.bin",
    "exceptions.bin",
    "case.bin",
    "seq_wrap.bin",
    "qual_wrap.bin",
    "quality.bin",
}

# Pattern for per-position quality files (v3 layout=2)
_QUALITY_POS_PATTERN = re.compile(r"^quality_pos_\d{4}\.bin$")


def _is_fastq_compressible(name: str) -> bool:
    """Check if a FASTQ stream name should be compressed."""
    if name in _FASTQ_STREAMS:
        return True
    if _QUALITY_POS_PATTERN.match(name):
        return True
    return False


# zli cannot compress files >500 MiB without chunking support.
_MAX_CHUNK_BYTES = 400 * 1024 * 1024  # 400 MiB

# Default max sample size for training (200 MiB, matching main_branch approach)
_DEFAULT_TRAIN_SAMPLE_BYTES = 200 * 1024 * 1024  # 200 MiB

# If generic serial already achieves this ratio, skip training
_SKIP_TRAIN_RATIO = 100.0

# Default training time limit per stream (seconds)
_DEFAULT_MAX_TIME_SECS = 1800  # 30 minutes

# Default directories for trained models
_NYX_ROOT = Path(__file__).resolve().parent.parent.parent  # nyx/nyx/commands -> nyx/
_DEFAULT_FASTA_MODELS_DIR = _NYX_ROOT / "models" / "lossless"
_DEFAULT_FASTQ_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq"


def _split_file(src: Path, chunk_dir: Path, max_bytes: int) -> list:
    """Split a file into chunks of at most max_bytes."""
    chunk_dir.mkdir(parents=True, exist_ok=True)
    total = src.stat().st_size
    if total <= max_bytes:
        return [(src, total)]

    chunks = []
    idx = 0
    with open(src, "rb") as f:
        while True:
            data = f.read(max_bytes)
            if not data:
                break
            chunk_path = chunk_dir / f"{src.name}.{idx:03d}"
            chunk_path.write_bytes(data)
            chunks.append((chunk_path, len(data)))
            idx += 1
    return chunks


def _get_compressor_path(models_dir: Path, stream_name: str) -> Path:
    return models_dir / f"{stream_name}.zl_compressor"


def _find_trained_compressors(models_dir: Path, compressible_streams: set,
                              is_fastq: bool = False,
                              quality_pos_names: list = None) -> dict:
    compressors = {}
    if not models_dir.is_dir():
        return compressors
    for name in compressible_streams:
        comp_path = _get_compressor_path(models_dir, name)
        if comp_path.is_file() and comp_path.stat().st_size > 0:
            compressors[name] = comp_path
    # For FASTQ: use universal quality_delta compressor for all per-position files
    if is_fastq:
        universal_qc = models_dir / "quality_delta.zl_compressor"
        if universal_qc.is_file() and universal_qc.stat().st_size > 0:
            if quality_pos_names:
                for name in quality_pos_names:
                    compressors[name] = universal_qc
    return compressors


def _train_stream_compressors(
    streams_dir: Path,
    stream_files: list,
    models_dir: Path,
    compressible_streams: set,
    is_fastq: bool = False,
    verbose: bool = False,
    train_threads: int = None,
    max_time_secs: int = None,
    train_sample_bytes: int = None,
) -> dict:
    models_dir.mkdir(parents=True, exist_ok=True)
    compressors = {}

    if train_sample_bytes is None:
        train_sample_bytes = _DEFAULT_TRAIN_SAMPLE_BYTES

    # Separate quality_pos files from regular streams
    quality_pos_files = []
    regular_files = []
    for sf in sorted(stream_files):
        if is_fastq and _QUALITY_POS_PATTERN.match(sf.name):
            quality_pos_files.append(sf)
        else:
            regular_files.append(sf)

    # Train regular stream compressors
    for sf in regular_files:
        name = sf.name
        if is_fastq:
            if not _is_fastq_compressible(name):
                continue
        elif name not in compressible_streams:
            continue

        compressors.update(_train_single_stream(
            sf, streams_dir, models_dir, verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes))

    # Train ONE universal quality compressor from a representative position file
    if is_fastq and quality_pos_files:
        non_empty = [f for f in quality_pos_files if f.stat().st_size > 0]
        if non_empty:
            # Pick the middle position as representative
            representative = non_empty[len(non_empty) // 2]
            click.echo(
                f"    Training universal quality compressor from "
                f"{representative.name} ({len(non_empty)} position files)..."
            )
            compressor_path = models_dir / "quality_delta.zl_compressor"

            sample_dir = streams_dir / "_train_quality_delta"
            sample_dir.mkdir(exist_ok=True)

            original_size = representative.stat().st_size
            sample_path = sample_dir / representative.name
            if original_size > train_sample_bytes:
                with open(representative, "rb") as fin:
                    sample_data = fin.read(train_sample_bytes)
                sample_path.write_bytes(sample_data)
                sample_size = train_sample_bytes
                if verbose:
                    click.echo(
                        f"    Using {sample_size:,} byte sample "
                        f"(of {original_size:,})"
                    )
            else:
                shutil.copy2(representative, sample_path)
                sample_size = original_size

            # Probe serial ratio
            probe_out = sample_dir / (representative.name + ".probe.zl")
            openzl.compress(
                sample_path, probe_out,
                profile="serial",
                force=True,
                verbose=False,
            )
            probe_size = probe_out.stat().st_size
            probe_ratio = sample_size / probe_size if probe_size > 0 else 0
            probe_out.unlink()

            if probe_ratio >= _SKIP_TRAIN_RATIO:
                click.echo(
                    f"    quality_delta: already {probe_ratio:.0f}x with "
                    f"serial, skipping training"
                )
            else:
                openzl.train(
                    sample_dir=sample_dir,
                    output_file=compressor_path,
                    profile="serial",
                    use_all_samples=True,
                    no_ace_successors=True,
                    threads=train_threads,
                    max_time_secs=max_time_secs,
                    force=True,
                    verbose=verbose,
                )

                if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                    # Map universal compressor to ALL position files
                    for qf in quality_pos_files:
                        compressors[qf.name] = compressor_path
                    if verbose:
                        click.echo(
                            f"    quality_delta: universal compressor saved "
                            f"({compressor_path.stat().st_size:,} bytes), "
                            f"applied to {len(quality_pos_files)} positions"
                        )
                else:
                    click.echo(
                        f"    quality_delta: training produced no output, "
                        f"using generic profile"
                    )

    return compressors


def _train_single_stream(
    sf: Path,
    streams_dir: Path,
    models_dir: Path,
    verbose: bool = False,
    train_threads: int = None,
    max_time_secs: int = None,
    train_sample_bytes: int = None,
) -> dict:
    """Train a compressor for a single stream file. Returns {name: path} or {}."""
    name = sf.name
    original_size = sf.stat().st_size
    if original_size == 0:
        if verbose:
            click.echo(f"    {name}: empty, skipping training")
        return {}

    if train_sample_bytes is None:
        train_sample_bytes = _DEFAULT_TRAIN_SAMPLE_BYTES

    sample_dir = streams_dir / f"_train_{name}"
    sample_dir.mkdir(exist_ok=True)

    sample_path = sample_dir / name
    if original_size > train_sample_bytes:
        with open(sf, "rb") as fin:
            sample_data = fin.read(train_sample_bytes)
        sample_path.write_bytes(sample_data)
        sample_size = train_sample_bytes
        if verbose:
            click.echo(
                f"    {name}: using {sample_size:,} byte "
                f"sample (of {original_size:,})"
            )
    else:
        shutil.copy2(sf, sample_path)
        sample_size = original_size

    probe_out = sample_dir / (name + ".probe.zl")
    openzl.compress(
        sample_path, probe_out,
        profile="serial",
        force=True,
        verbose=False,
    )
    probe_size = probe_out.stat().st_size
    probe_ratio = sample_size / probe_size if probe_size > 0 else 0
    probe_out.unlink()

    if probe_ratio >= _SKIP_TRAIN_RATIO:
        click.echo(
            f"    {name}: already {probe_ratio:.0f}x with serial "
            f"(near-constant data), skipping training"
        )
        return {}

    compressor_path = _get_compressor_path(models_dir, name)

    # Skip if already trained (reuse existing compressor)
    if compressor_path.is_file() and compressor_path.stat().st_size > 0:
        if verbose:
            click.echo(f"    {name}: reusing existing compressor")
        return {name: compressor_path}

    click.echo(f"    Training compressor for {name}...")
    openzl.train(
        sample_dir=sample_dir,
        output_file=compressor_path,
        profile="serial",
        use_all_samples=True,
        no_ace_successors=True,
        threads=train_threads,
        max_time_secs=max_time_secs,
        force=True,
        verbose=verbose,
    )

    if compressor_path.is_file():
        if verbose:
            click.echo(
                f"    {name}: trained compressor saved "
                f"({compressor_path.stat().st_size:,} bytes)"
            )
        return {name: compressor_path}
    else:
        click.echo(f"    {name}: training produced no output, using generic profile")
        return {}


def _compress_stream(
    input_path: Path,
    output_path: Path,
    compressor: Path = None,
    train_inline: bool = False,
    verbose: bool = False,
) -> str:
    if train_inline:
        openzl.compress(
            input_path, output_path,
            profile="serial",
            train_inline=True,
            force=True,
            verbose=verbose,
        )
        return "inline"

    if compressor and compressor.is_file():
        try:
            openzl.compress(
                input_path, output_path,
                compressor=compressor,
                force=True,
                verbose=verbose,
            )
            return "trained"
        except openzl.OpenZLError:
            click.echo(
                f"    Warning: trained compressor failed for "
                f"{input_path.name}, falling back to serial profile"
            )
            openzl.compress(
                input_path, output_path,
                profile="serial",
                force=True,
                verbose=verbose,
            )
            return "serial"
    else:
        openzl.compress(
            input_path, output_path,
            profile="serial",
            force=True,
            verbose=verbose,
        )
        return "serial"


@click.command("compress-lossless")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output path (default: <input>.zlfasta or .zlfastq).",
)
@click.option("-v", "--verbose", is_flag=True, help="Print subprocess commands.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output file.")
@click.option(
    "-t", "--type",
    "file_type",
    type=click.Choice(["auto", "fasta", "fastq"], case_sensitive=False),
    default="auto",
    help="Input file type (default: auto-detect).",
)
@click.option(
    "--threads",
    type=int,
    default=1,
    help="Number of encoding threads (default: 1).",
)
@click.option(
    "--train", "do_train", is_flag=True,
    help="Train per-stream compressors before compressing (one-time, improves ratio).",
)
@click.option(
    "--models-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory for trained compressor models.",
)
@click.option(
    "--no-trained", is_flag=True,
    help="Ignore trained compressors, use generic serial profile.",
)
@click.option(
    "--train-sample-mib",
    type=int,
    default=200,
    help="Training sample size in MiB (default: 200).",
)
@click.option(
    "--max-time-secs",
    type=int,
    default=_DEFAULT_MAX_TIME_SECS,
    help=f"Max training time per stream in seconds (default: {_DEFAULT_MAX_TIME_SECS}).",
)
@click.option(
    "--train-threads",
    type=int,
    default=None,
    help="Threads for OpenZL training (default: CPU count).",
)
@click.option(
    "--compress-jobs",
    type=int,
    default=None,
    help="Number of streams to compress in parallel (default: CPU count).",
)
def compress_lossless_cmd(input_file, output, verbose, force, file_type, threads,
                          do_train, models_dir, no_trained,
                          train_sample_mib, max_time_secs, train_threads,
                          compress_jobs):
    """Lossless FASTA/FASTQ compression with byte-exact reconstruction.

    Auto-detects FASTA vs FASTQ input (or use --type to specify).

    Encodes the input into typed streams (N-mask, 2-bit bases, ACGT-mask,
    exceptions, case, wrapping, quality) and compresses each stream
    independently with OpenZL.

    Use --train on the first run to train format-specific compressors.
    Subsequent runs will automatically use the trained models for
    better compression ratios.

    \b
    Example:
      nyx compress-lossless genome.fasta                # auto-detect FASTA
      nyx compress-lossless reads.fastq --threads 4     # parallel FASTQ
      nyx compress-lossless genome.fasta --train         # train + compress
      nyx compress-lossless data.fa --type fasta         # explicit type
    """
    input_path = Path(input_file).resolve()

    # Resolve defaults
    if train_threads is None:
        train_threads = os.cpu_count() or 16
    if compress_jobs is None:
        compress_jobs = os.cpu_count() or 4
    train_sample_bytes = train_sample_mib * 1024 * 1024

    # Detect file type
    if file_type == "auto":
        detected = detect_filetype(input_path)
        if detected not in ("fasta", "fastq"):
            raise click.ClickException(
                f"Cannot auto-detect file type for {input_path.name}. "
                f"Use --type fasta or --type fastq."
            )
        file_type = detected
    click.echo(f"Format: {file_type.upper()}")

    is_fastq = (file_type == "fastq")
    compressible_streams = _FASTQ_STREAMS if is_fastq else _FASTA_STREAMS
    container_ext = ".zlfastq" if is_fastq else ".zlfasta"
    default_mdir = _DEFAULT_FASTQ_MODELS_DIR if is_fastq else _DEFAULT_FASTA_MODELS_DIR

    if output is None:
        output_path = input_path.with_suffix(input_path.suffix + container_ext)
    else:
        output_path = Path(output).resolve()

    if output_path.exists() and not force:
        raise click.ClickException(
            f"Output already exists: {output_path}\n"
            f"Use -f/--force to overwrite."
        )

    mdir = Path(models_dir) if models_dir else default_mdir

    input_size = input_path.stat().st_size
    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_")

    try:
        streams_dir = Path(tmpdir) / "streams"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()
        chunks_dir = Path(tmpdir) / "chunks"

        # Step 1: Encode into streams
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding {file_type.upper()} into streams...")
        if is_fastq:
            stream_files = fastq_codec.encode(
                input_path, streams_dir, verbose=verbose, threads=threads)
        else:
            stream_files = codec.encode(
                input_path, streams_dir, verbose=verbose, threads=threads)

        if verbose:
            for sf in stream_files:
                click.echo(f"    {sf.name}: {sf.stat().st_size:,} bytes")

        # Step 2 (optional): Train per-stream compressors
        compressors = {}
        if do_train:
            click.echo("  [2/4] Training per-stream compressors...")
            compressors = _train_stream_compressors(
                streams_dir, stream_files, mdir, compressible_streams,
                is_fastq=is_fastq, verbose=verbose,
                train_threads=train_threads,
                max_time_secs=max_time_secs,
                train_sample_bytes=train_sample_bytes,
            )
            train_elapsed = time.time() - t0
            click.echo(f"    Trained {len(compressors)} compressors in {train_elapsed:.1f}s")
            click.echo(f"    Models saved to: {mdir}")
        elif not no_trained:
            quality_pos_names = [
                sf.name for sf in stream_files
                if _QUALITY_POS_PATTERN.match(sf.name)
            ] if is_fastq else None
            compressors = _find_trained_compressors(
                mdir, compressible_streams,
                is_fastq=is_fastq,
                quality_pos_names=quality_pos_names,
            )
            if compressors:
                click.echo(f"  Using trained compressors from {mdir}")
                if verbose:
                    for name in sorted(compressors):
                        click.echo(f"    {name}: {compressors[name]}")

        # Step 3: Compress each stream with OpenZL (parallel)
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing streams with OpenZL...")
        entries = {}

        # Build task list for parallel compression
        compress_tasks = []
        for sf in sorted(stream_files):
            name = sf.name
            original_size = sf.stat().st_size

            is_compressible = (_is_fastq_compressible(name) if is_fastq
                               else name in compressible_streams)
            if is_compressible and original_size > 0:
                stream_compressor = compressors.get(name)
                pieces = _split_file(sf, chunks_dir / name, _MAX_CHUNK_BYTES)

                for chunk_path, piece_size in pieces:
                    chunk_name = chunk_path.name if len(pieces) > 1 else name
                    compressed_file = compressed_dir / (chunk_name + ".zl")
                    compress_tasks.append((
                        chunk_path, compressed_file, stream_compressor,
                        piece_size, chunk_name, name, len(pieces),
                    ))
            else:
                data = sf.read_bytes()
                entries[name] = (data, original_size)

        # Execute compression in parallel
        if compress_tasks:
            num_workers = min(compress_jobs, len(compress_tasks))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=num_workers
            ) as executor:
                future_to_task = {}
                for task in compress_tasks:
                    (chunk_path, compressed_file, stream_compressor,
                     piece_size, chunk_name, orig_name, num_pieces) = task
                    fut = executor.submit(
                        _compress_stream,
                        chunk_path, compressed_file,
                        compressor=stream_compressor,
                        train_inline=False,
                        verbose=verbose,
                    )
                    future_to_task[fut] = task

                # Collect results and verbose output
                # Group by original stream name for summary reporting
                stream_stats = {}  # orig_name -> (total_orig, total_compressed, modes)
                for fut in concurrent.futures.as_completed(future_to_task):
                    task = future_to_task[fut]
                    (chunk_path, compressed_file, _sc,
                     piece_size, chunk_name, orig_name, num_pieces) = task
                    mode = fut.result()
                    compressed_data = compressed_file.read_bytes()
                    entries[chunk_name] = (compressed_data, piece_size)

                    if orig_name not in stream_stats:
                        stream_stats[orig_name] = [0, 0, set(), num_pieces]
                    stats = stream_stats[orig_name]
                    stats[0] += piece_size
                    stats[1] += len(compressed_data)
                    stats[2].add(mode)

            if verbose:
                for orig_name in sorted(stream_stats):
                    total_orig, total_comp, modes, npieces = stream_stats[orig_name]
                    ratio = total_orig / total_comp if total_comp else 0
                    mode_str = "/".join(sorted(modes))
                    if npieces > 1:
                        click.echo(
                            f"    {orig_name} total: {total_orig:,} -> "
                            f"{total_comp:,} ({ratio:.2f}x) [{mode_str}]"
                        )
                    else:
                        click.echo(
                            f"    {orig_name}: {total_orig:,} -> "
                            f"{total_comp:,} ({ratio:.2f}x) [{mode_str}]"
                        )

        # Step 4: Bundle into container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating {container_ext} container...")
        if is_fastq:
            create_zlfastq(output_path, entries)
        else:
            create_zlfasta(output_path, entries)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size
        ratio = input_size / output_size if output_size > 0 else 0

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Ratio:  {ratio:.3f}x  "
            f"(saved {(1 - output_size / input_size) * 100:.1f}%)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
