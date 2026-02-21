"""nyx compress-lossless-fastq — Lossless FASTQ compression using stream separation + OpenZL."""

import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import fastq_codec, openzl
from ..core.zlfastq import create_zlfastq

# Streams that get compressed with OpenZL (everything except meta.bin)
_COMPRESSIBLE_STREAMS = {
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


def _is_compressible(name: str) -> bool:
    """Check if a FASTQ stream name should be compressed."""
    if name in _COMPRESSIBLE_STREAMS:
        return True
    if _QUALITY_POS_PATTERN.match(name):
        return True
    return False


# zli cannot compress files >500 MiB without chunking support.
_MAX_CHUNK_BYTES = 400 * 1024 * 1024  # 400 MiB

# Max sample size for training
_MAX_TRAIN_SAMPLE_BYTES = 10 * 1024 * 1024  # 10 MiB

# If generic serial already achieves this ratio, skip training
_SKIP_TRAIN_RATIO = 100.0

# Default directory for trained models
_NYX_ROOT = Path(__file__).resolve().parent.parent.parent  # nyx/nyx/commands -> nyx/
_DEFAULT_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq"


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
    """Get the path where a trained compressor for a stream would be stored."""
    return models_dir / f"{stream_name}.zl_compressor"


def _find_trained_compressors(models_dir: Path,
                              quality_pos_names: list = None) -> dict:
    """Find all trained compressors in the models directory."""
    compressors = {}
    if not models_dir.is_dir():
        return compressors
    for name in _COMPRESSIBLE_STREAMS:
        comp_path = _get_compressor_path(models_dir, name)
        if comp_path.is_file() and comp_path.stat().st_size > 0:
            compressors[name] = comp_path
    # Use universal quality_delta compressor for all per-position files
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
    verbose: bool = False,
) -> dict:
    """Train per-stream compressors from encoded stream files.

    For quality_pos_*.bin files, trains ONE universal compressor from a
    representative position file and applies it to all positions.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    compressors = {}

    # Separate quality_pos files from regular streams
    quality_pos_files = []
    regular_files = []
    for sf in sorted(stream_files):
        if _QUALITY_POS_PATTERN.match(sf.name):
            quality_pos_files.append(sf)
        else:
            regular_files.append(sf)

    # Train regular stream compressors
    for sf in regular_files:
        if not _is_compressible(sf.name):
            continue
        compressors.update(_train_single_stream(
            sf, streams_dir, models_dir, verbose=verbose))

    # Train ONE universal quality compressor from a representative position file
    if quality_pos_files:
        non_empty = [f for f in quality_pos_files if f.stat().st_size > 0]
        if non_empty:
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
            if original_size > _MAX_TRAIN_SAMPLE_BYTES:
                with open(representative, "rb") as fin:
                    sample_data = fin.read(_MAX_TRAIN_SAMPLE_BYTES)
                sample_path.write_bytes(sample_data)
                sample_size = _MAX_TRAIN_SAMPLE_BYTES
                if verbose:
                    click.echo(
                        f"    Using {sample_size:,} byte sample "
                        f"(of {original_size:,})"
                    )
            else:
                shutil.copy2(representative, sample_path)
                sample_size = original_size

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
                    force=True,
                    verbose=verbose,
                )

                if compressor_path.is_file() and compressor_path.stat().st_size > 0:
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
) -> dict:
    """Train a compressor for a single stream file. Returns {name: path} or {}."""
    name = sf.name
    original_size = sf.stat().st_size
    if original_size == 0:
        if verbose:
            click.echo(f"    {name}: empty, skipping training")
        return {}

    sample_dir = streams_dir / f"_train_{name}"
    sample_dir.mkdir(exist_ok=True)

    sample_path = sample_dir / name
    if original_size > _MAX_TRAIN_SAMPLE_BYTES:
        with open(sf, "rb") as fin:
            sample_data = fin.read(_MAX_TRAIN_SAMPLE_BYTES)
        sample_path.write_bytes(sample_data)
        sample_size = _MAX_TRAIN_SAMPLE_BYTES
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
    """Compress a single stream file."""
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


@click.command("compress-lossless-fastq")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output .zlfastq path (default: <input>.zlfastq).",
)
@click.option("-v", "--verbose", is_flag=True, help="Print subprocess commands.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output file.")
@click.option(
    "--train", "do_train", is_flag=True,
    help="Train per-stream compressors before compressing (one-time, improves ratio).",
)
@click.option(
    "--models-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="Directory for trained compressor models (default: nyx/models/lossless_fastq/).",
)
@click.option(
    "--no-trained", is_flag=True,
    help="Ignore trained compressors, use generic serial profile.",
)
def compress_lossless_fastq_cmd(input_file, output, verbose, force, do_train, models_dir, no_trained):
    """Lossless FASTQ compression with byte-exact reconstruction.

    Encodes the input FASTQ into typed streams (N-mask, 2-bit bases,
    ACGT-mask, exceptions, case, quality, wrapping metadata) and compresses
    each stream independently with OpenZL.

    Use --train on the first run to train FASTQ-specific compressors.
    Subsequent runs will automatically use the trained models for
    better compression ratios.

    \b
    Example:
      nyx compress-lossless-fastq reads.fastq --train
      nyx compress-lossless-fastq reads.fastq
      nyx compress-lossless-fastq reads.fastq --no-trained
    """
    input_path = Path(input_file).resolve()
    if output is None:
        output_path = input_path.with_suffix(input_path.suffix + ".zlfastq")
    else:
        output_path = Path(output).resolve()

    if output_path.exists() and not force:
        raise click.ClickException(
            f"Output already exists: {output_path}\n"
            f"Use -f/--force to overwrite."
        )

    mdir = Path(models_dir) if models_dir else _DEFAULT_MODELS_DIR

    input_size = input_path.stat().st_size
    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_fq_")

    try:
        streams_dir = Path(tmpdir) / "streams"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()
        chunks_dir = Path(tmpdir) / "chunks"

        # Step 1: Encode FASTQ into streams
        click.echo("  [1/4] Encoding FASTQ into streams..." if do_train else "  [1/3] Encoding FASTQ into streams...")
        stream_files = fastq_codec.encode(input_path, streams_dir, verbose=verbose)

        if verbose:
            for sf in stream_files:
                click.echo(f"    {sf.name}: {sf.stat().st_size:,} bytes")

        # Step 2 (optional): Train per-stream compressors
        compressors = {}
        if do_train:
            click.echo("  [2/4] Training per-stream compressors...")
            compressors = _train_stream_compressors(
                streams_dir, stream_files, mdir, verbose=verbose,
            )
            train_elapsed = time.time() - t0
            click.echo(f"    Trained {len(compressors)} compressors in {train_elapsed:.1f}s")
            click.echo(f"    Models saved to: {mdir}")
        elif not no_trained:
            quality_pos_names = [
                sf.name for sf in stream_files
                if _QUALITY_POS_PATTERN.match(sf.name)
            ]
            compressors = _find_trained_compressors(
                mdir, quality_pos_names=quality_pos_names)
            if compressors:
                click.echo(f"  Using trained compressors from {mdir}")
                if verbose:
                    for name in sorted(compressors):
                        click.echo(f"    {name}: {compressors[name]}")

        # Step 3: Compress each stream with OpenZL
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing streams with OpenZL...")
        entries = {}
        for sf in sorted(stream_files):
            name = sf.name
            original_size = sf.stat().st_size

            if _is_compressible(name) and original_size > 0:
                stream_compressor = compressors.get(name)
                pieces = _split_file(sf, chunks_dir / name, _MAX_CHUNK_BYTES)

                if len(pieces) == 1 and pieces[0][0] == sf:
                    compressed_file = compressed_dir / (name + ".zl")
                    mode = _compress_stream(
                        sf, compressed_file,
                        compressor=stream_compressor,
                        train_inline=False,
                        verbose=verbose,
                    )
                    compressed_data = compressed_file.read_bytes()
                    entries[name] = (compressed_data, original_size)

                    if verbose:
                        ratio = original_size / len(compressed_data) if compressed_data else 0
                        click.echo(
                            f"    {name}: {original_size:,} -> "
                            f"{len(compressed_data):,} ({ratio:.2f}x) [{mode}]"
                        )
                else:
                    if verbose:
                        click.echo(
                            f"    {name}: {original_size:,} bytes "
                            f"-> split into {len(pieces)} chunks"
                        )
                    total_compressed = 0
                    modes_seen = set()
                    for chunk_path, piece_size in pieces:
                        chunk_name = chunk_path.name
                        compressed_file = compressed_dir / (chunk_name + ".zl")
                        mode = _compress_stream(
                            chunk_path, compressed_file,
                            compressor=stream_compressor,
                            train_inline=False,
                            verbose=verbose,
                        )
                        modes_seen.add(mode)
                        compressed_data = compressed_file.read_bytes()
                        entries[chunk_name] = (compressed_data, piece_size)
                        total_compressed += len(compressed_data)

                    if verbose:
                        ratio = original_size / total_compressed if total_compressed else 0
                        mode_str = "/".join(sorted(modes_seen))
                        click.echo(
                            f"    {name} total: {original_size:,} -> "
                            f"{total_compressed:,} ({ratio:.2f}x) [{mode_str}]"
                        )
            else:
                data = sf.read_bytes()
                entries[name] = (data, original_size)

        # Step 4: Bundle into .zlfastq container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating .zlfastq container...")
        create_zlfastq(output_path, entries)

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
