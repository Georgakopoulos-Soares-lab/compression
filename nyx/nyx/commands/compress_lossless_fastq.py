"""nyx compress-lossless-fastq — Lossless FASTQ compression using stream separation + OpenZL."""

import concurrent.futures
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import fastq_codec, openzl
from ..core.zlfastq import create_zlfastq
from ..utils.paths import find_schema

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

# Default max sample size for training (200 MiB, matching main_branch approach)
_DEFAULT_TRAIN_SAMPLE_BYTES = 200 * 1024 * 1024  # 200 MiB

# If generic serial already achieves this ratio, skip training
_SKIP_TRAIN_RATIO = 100.0

# Default training time limit per stream (seconds)
_DEFAULT_MAX_TIME_SECS = 1800  # 30 minutes

# Default directory for trained models
_NYX_ROOT = Path(__file__).resolve().parent.parent.parent  # nyx/nyx/commands -> nyx/
_DEFAULT_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq"

# Packed-path compressor names (one per NQF variant)
_NQF1_COMPRESSOR = "fastq_illumina_fixed.zl_compressor"
_NQF2_COMPRESSOR = "fastq_illumina_variable.zl_compressor"
_NQF3_COMPRESSOR = "fastq_generic.zl_compressor"
_DEFAULT_PACKED_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq_packed"

# SDDL schemas for each variant
_NQF_SCHEMA_MAP = {
    b"NQF1": ("fastq_fixed.sddl", _NQF1_COMPRESSOR),
    b"NQF2": ("fastq_variable.sddl", _NQF2_COMPRESSOR),
    b"NQF3": ("fastq_variable.sddl", _NQF3_COMPRESSOR),
}

# CSV-path compressor and models directory
_CSV_COMPRESSOR = "fastq_csv.zl_compressor"
_DEFAULT_CSV_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq_csv"

# FASTQ extensions for group training
_FASTQ_EXTENSIONS = {".fastq", ".fq"}

# Target ~200 MiB NQF per training sample
_GROUP_TRAIN_CHUNK_TARGET = 200 * 1024 * 1024

# Pattern for CSV part files
_CSV_PART_PATTERN = re.compile(r"^part_\d{3}\.tsv$")


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
    train_threads: int = None,
    max_time_secs: int = None,
    train_sample_bytes: int = None,
) -> dict:
    """Train per-stream compressors from encoded stream files.

    For quality_pos_*.bin files, trains ONE universal compressor from a
    representative position file and applies it to all positions.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    compressors = {}

    if train_sample_bytes is None:
        train_sample_bytes = _DEFAULT_TRAIN_SAMPLE_BYTES

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
            sf, streams_dir, models_dir, verbose=verbose,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes))

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


def _detect_nqf_variant(chunk_path: Path) -> bytes:
    """Read the 4-byte magic from a packed chunk to detect NQF variant."""
    with open(chunk_path, "rb") as f:
        magic = f.read(4)
    if magic not in _NQF_SCHEMA_MAP:
        raise click.ClickException(
            f"Unknown NQF magic in {chunk_path.name}: {magic!r}")
    return magic


def _truncate_tsv_at_newline(src: Path, dest: Path, max_bytes: int) -> int:
    """Copy at most max_bytes from src to dest, cutting at a newline boundary.

    This ensures the truncated TSV file ends with a complete line so
    OpenZL's CSV lexer doesn't see a partial last row.
    Returns the number of bytes written.
    """
    with open(src, "rb") as fin:
        data = fin.read(max_bytes)
    # Find last newline within the read data
    last_nl = data.rfind(b"\n")
    if last_nl >= 0:
        data = data[: last_nl + 1]  # include the newline
    dest.write_bytes(data)
    return len(data)


def _compress_csv_fastq(
    input_path: Path,
    output_path: Path,
    models_dir: Path,
    do_train: bool,
    no_trained: bool,
    verbose: bool,
    train_threads: int,
    max_time_secs: int,
    train_sample_bytes: int,
    compress_jobs: int,
    group_train_dir: str = None,
):
    """FASTQ compression via CSV/TSV decomposition + OpenZL CSV profile."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_fastq_csv_")

    try:
        csv_dir = Path(tmpdir) / "csv"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode FASTQ into TSV parts + meta.bin
        _RAW_PER_PART = 400 * 1024 * 1024  # target ~400 MiB per TSV part
        num_parts = max(1, (input_size + _RAW_PER_PART - 1) // _RAW_PER_PART)
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding FASTQ into CSV/TSV parts...")
        csv_files = fastq_codec.encode_csv(
            input_path, csv_dir, num_parts=num_parts, verbose=verbose)

        tsv_parts = sorted(f for f in csv_files if _CSV_PART_PATTERN.match(f.name))
        sidecar_files = [f for f in csv_files if not _CSV_PART_PATTERN.match(f.name)]

        total_tsv_size = sum(f.stat().st_size for f in tsv_parts)
        click.echo(f"    {len(tsv_parts)} TSV part(s), "
                   f"{total_tsv_size:,} bytes total")
        if verbose:
            for f in sorted(csv_files):
                click.echo(f"    {f.name}: {f.stat().st_size:,} bytes")

        # Step 2: Train or load CSV compressor
        compressor_path = models_dir / _CSV_COMPRESSOR
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                group_path = Path(group_train_dir).resolve()
                fastq_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _FASTQ_EXTENSIONS
                )
                if not fastq_files:
                    raise click.ClickException(
                        f"No FASTQ files found in {group_path}")
                click.echo(f"  [2/4] Group training from {len(fastq_files)} FASTQ file(s)...")

                total_train_size = 0
                for idx, fq_file in enumerate(fastq_files):
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_files = fastq_codec.encode_csv(
                            fq_file, sample_tmp, num_parts=1, verbose=False)
                    except Exception as e:
                        click.echo(f"    Warning: skipping {fq_file.name}: {e}")
                        continue
                    sample_tsvs = [f for f in sample_files
                                   if _CSV_PART_PATTERN.match(f.name)]
                    for sf in sample_tsvs:
                        sz = sf.stat().st_size
                        # Truncate large samples at newline boundary
                        if sz > train_sample_bytes:
                            dest = training_dir / f"sample_{idx:03d}_{fq_file.stem}.tsv"
                            sz = _truncate_tsv_at_newline(sf, dest, train_sample_bytes)
                        else:
                            dest = training_dir / f"sample_{idx:03d}_{fq_file.stem}.tsv"
                            shutil.copy2(sf, dest)
                        total_train_size += sz
                    shutil.rmtree(sample_tmp, ignore_errors=True)

                # Include input file's TSV if not already in group
                input_in_group = any(
                    f.resolve() == input_path for f in fastq_files)
                if not input_in_group and tsv_parts:
                    dest = training_dir / f"sample_{len(fastq_files):03d}_{input_path.stem}.tsv"
                    shutil.copy2(tsv_parts[0], dest)
                    total_train_size += tsv_parts[0].stat().st_size

                num_samples = len(list(training_dir.iterdir()))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                click.echo(f"  [2/4] Training CSV compressor...")
                # Copy representative TSV parts to training dir
                if len(tsv_parts) <= 3:
                    sample_indices = list(range(len(tsv_parts)))
                else:
                    n = len(tsv_parts)
                    sample_indices = [0, n // 2, n - 1]
                total_sample_size = 0
                for si in sample_indices:
                    src = tsv_parts[si]
                    sz = src.stat().st_size
                    if sz > train_sample_bytes:
                        dest = training_dir / src.name
                        sz = _truncate_tsv_at_newline(src, dest, train_sample_bytes)
                    else:
                        shutil.copy2(src, training_dir / src.name)
                    total_sample_size += sz
                click.echo(f"    Training on {len(sample_indices)} sample(s): "
                           f"{total_sample_size:,} bytes")

            openzl.train(
                sample_dir=training_dir,
                output_file=compressor_path,
                profile="csv",
                profile_arg="\t",
                use_all_samples=True,
                no_ace_successors=True,
                threads=train_threads,
                max_time_secs=max_time_secs,
                force=True,
                verbose=verbose,
            )

            train_elapsed = time.time() - t0
            if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                click.echo(f"    Trained compressor: {compressor_path.stat().st_size:,} bytes "
                           f"in {train_elapsed:.1f}s")
                click.echo(f"    Saved to: {compressor_path}")
            else:
                click.echo("    Warning: training produced no output, falling back to csv profile")
                compressor_path = None
        elif not no_trained:
            if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                click.echo(f"  Using trained CSV compressor: {compressor_path}")
            else:
                compressor_path = None
        else:
            compressor_path = None

        # Step 3: Compress TSV parts with OpenZL CSV profile (parallel)
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing with OpenZL CSV profile...")

        entries = {}

        # Sidecar files (meta.bin, plus.bin, wrap.bin) — store raw (small)
        for sf in sidecar_files:
            data = sf.read_bytes()
            entries[sf.name] = (data, len(data))
            if verbose:
                click.echo(f"    {sf.name}: {len(data):,} bytes (raw)")

        def _compress_csv_part(part_path, comp_path):
            compressed_file = compressed_dir / (part_path.name + ".zl")
            if comp_path and Path(comp_path).is_file():
                try:
                    openzl.compress(
                        part_path, compressed_file,
                        compressor=comp_path,
                        force=True, verbose=verbose,
                    )
                    return part_path.name, compressed_file, "trained"
                except openzl.OpenZLError:
                    click.echo(f"    Warning: trained compressor failed for "
                               f"{part_path.name}, falling back to csv profile")
            openzl.compress(
                part_path, compressed_file,
                profile="csv",
                profile_arg="\t",
                force=True, verbose=verbose,
            )
            return part_path.name, compressed_file, "csv"

        num_workers = min(compress_jobs, len(tsv_parts))
        total_orig = 0
        total_comp = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_compress_csv_part, p, compressor_path): p
                for p in tsv_parts
            }
            for fut in concurrent.futures.as_completed(futures):
                name, compressed_file, mode = fut.result()
                orig_size = futures[fut].stat().st_size
                compressed_data = compressed_file.read_bytes()
                entries[name] = (compressed_data, orig_size)
                total_orig += orig_size
                total_comp += len(compressed_data)
                if verbose:
                    ratio = orig_size / len(compressed_data) if compressed_data else 0
                    click.echo(f"    {name}: {orig_size:,} -> "
                               f"{len(compressed_data):,} ({ratio:.2f}x) [{mode}]")

        if total_comp > 0:
            click.echo(f"    TSV total: {total_orig:,} -> {total_comp:,} "
                       f"({total_orig / total_comp:.2f}x)")

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


def _compress_packed_fastq(
    input_path: Path,
    output_path: Path,
    models_dir: Path,
    do_train: bool,
    no_trained: bool,
    verbose: bool,
    train_threads: int,
    max_time_secs: int,
    train_sample_bytes: int,
    compress_jobs: int,
    group_train_dir: str = None,
):
    """FASTQ compression via packed NQF binary + single SDDL compressor."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_fastq_packed_")

    try:
        chunks_dir = Path(tmpdir) / "chunks"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode-packed
        _MAX_NQF_CHUNK = 400 * 1024 * 1024
        _RAW_PER_CHUNK = 500 * 1024 * 1024
        num_chunks = max(1, (input_size + _RAW_PER_CHUNK - 1) // _RAW_PER_CHUNK)
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding FASTQ into packed NQF chunks...")
        chunk_files = fastq_codec.encode_packed(
            input_path, chunks_dir, num_chunks=num_chunks, verbose=verbose)

        # Safety check: re-encode if any chunk exceeds 500 MiB
        max_chunk_size = max(c.stat().st_size for c in chunk_files)
        if max_chunk_size > 500 * 1024 * 1024:
            needed = max(num_chunks + 1, input_size // _MAX_NQF_CHUNK + 1)
            click.echo(f"    Largest chunk {max_chunk_size:,} > 500 MiB, "
                       f"re-encoding with {needed} chunks...")
            shutil.rmtree(chunks_dir)
            chunk_files = fastq_codec.encode_packed(
                input_path, chunks_dir, num_chunks=needed, verbose=verbose)

        click.echo(f"    {len(chunk_files)} chunk(s), total "
                   f"{sum(c.stat().st_size for c in chunk_files):,} bytes")

        # Detect NQF variant from first chunk
        nqf_magic = _detect_nqf_variant(chunk_files[0])
        schema_name, compressor_name = _NQF_SCHEMA_MAP[nqf_magic]
        variant_label = nqf_magic.decode("ascii")
        click.echo(f"    Format: {variant_label}")

        # Step 2: Train or load compressor
        compressor_path = models_dir / compressor_name
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                group_path = Path(group_train_dir).resolve()
                fastq_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _FASTQ_EXTENSIONS
                )
                if not fastq_files:
                    raise click.ClickException(
                        f"No FASTQ files found in {group_path} "
                        f"(expected extensions: {', '.join(sorted(_FASTQ_EXTENSIONS))})")
                click.echo(f"  [2/4] Group training from {len(fastq_files)} FASTQ file(s)...")

                total_train_size = 0
                for idx, fq_file in enumerate(fastq_files):
                    file_size = fq_file.stat().st_size
                    n = max(1, (file_size + _GROUP_TRAIN_CHUNK_TARGET - 1)
                            // _GROUP_TRAIN_CHUNK_TARGET)
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_chunks = fastq_codec.encode_packed(
                            fq_file, sample_tmp,
                            num_chunks=n, verbose=False,
                        )
                    except Exception as e:
                        click.echo(f"    Warning: skipping {fq_file.name}: {e}")
                        continue

                    if sample_chunks:
                        # Only include chunks matching the same variant
                        first = sample_chunks[0]
                        try:
                            magic = _detect_nqf_variant(first)
                        except click.ClickException:
                            continue
                        if magic == nqf_magic:
                            dest = training_dir / f"sample_{idx:03d}_{fq_file.stem}.bin"
                            shutil.copy2(first, dest)
                            sz = first.stat().st_size
                            total_train_size += sz
                            if verbose:
                                click.echo(f"    {fq_file.name}: {sz:,} bytes")

                    shutil.rmtree(sample_tmp, ignore_errors=True)

                # Also include input file's first chunk
                input_in_group = any(
                    f.resolve() == input_path for f in fastq_files)
                if not input_in_group:
                    dest = training_dir / f"sample_{len(fastq_files):03d}_{input_path.stem}.bin"
                    shutil.copy2(chunk_files[0], dest)
                    total_train_size += chunk_files[0].stat().st_size

                num_samples = len(list(training_dir.glob("*.bin")))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                click.echo(f"  [2/4] Training SDDL compressor for {variant_label}...")
                # Pick representative samples spread across the file
                # (beginning, middle, end) instead of just the first chunk.
                n = len(chunk_files)
                if n <= 3:
                    sample_indices = list(range(n))
                else:
                    sample_indices = [0, n // 2, n - 1]
                total_sample_size = 0
                for si in sample_indices:
                    src = chunk_files[si]
                    shutil.copy2(src, training_dir / src.name)
                    total_sample_size += src.stat().st_size
                click.echo(f"    Training on {len(sample_indices)} sample chunk(s) "
                           f"from positions {sample_indices}: "
                           f"{total_sample_size:,} bytes")
                if verbose:
                    for si in sample_indices:
                        click.echo(f"      {chunk_files[si].name}: "
                                   f"{chunk_files[si].stat().st_size:,} bytes")

            sddl_path = find_schema(schema_name)
            openzl.train(
                sample_dir=training_dir,
                output_file=compressor_path,
                profile="sddl",
                profile_arg=str(sddl_path),
                use_all_samples=True,
                no_ace_successors=True,
                threads=train_threads,
                max_time_secs=max_time_secs,
                force=True,
                verbose=verbose,
            )

            train_elapsed = time.time() - t0
            if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                click.echo(f"    Trained compressor: {compressor_path.stat().st_size:,} bytes "
                           f"in {train_elapsed:.1f}s")
                click.echo(f"    Saved to: {compressor_path}")
            else:
                click.echo("    Warning: training produced no output, falling back to serial")
                compressor_path = None
        elif not no_trained:
            if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                click.echo(f"  Using trained SDDL compressor: {compressor_path}")
            else:
                compressor_path = None
        else:
            compressor_path = None

        # Step 3: Compress each chunk (parallel)
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing chunks with OpenZL...")

        entries = {}

        def _compress_chunk(chunk_path, comp_path):
            compressed_file = compressed_dir / (chunk_path.name + ".zl")
            if comp_path and Path(comp_path).is_file():
                try:
                    openzl.compress(
                        chunk_path, compressed_file,
                        compressor=comp_path,
                        force=True, verbose=verbose,
                    )
                    return chunk_path.name, compressed_file, "trained"
                except openzl.OpenZLError:
                    click.echo(f"    Warning: trained compressor failed for "
                               f"{chunk_path.name}, falling back to serial")
            openzl.compress(
                chunk_path, compressed_file,
                profile="serial",
                force=True, verbose=verbose,
            )
            return chunk_path.name, compressed_file, "serial"

        num_workers = min(compress_jobs, len(chunk_files))
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_compress_chunk, cf, compressor_path): cf
                for cf in chunk_files
            }
            total_orig = 0
            total_comp = 0
            for fut in concurrent.futures.as_completed(futures):
                name, compressed_file, mode = fut.result()
                orig_size = futures[fut].stat().st_size
                compressed_data = compressed_file.read_bytes()
                entries[name] = (compressed_data, orig_size)
                total_orig += orig_size
                total_comp += len(compressed_data)
                if verbose:
                    ratio = orig_size / len(compressed_data) if compressed_data else 0
                    click.echo(f"    {name}: {orig_size:,} -> "
                               f"{len(compressed_data):,} ({ratio:.2f}x) [{mode}]")

        if total_comp > 0:
            click.echo(f"    Chunks total: {total_orig:,} -> {total_comp:,} "
                       f"({total_orig / total_comp:.2f}x)")

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
    "--threads",
    type=int,
    default=1,
    help="Number of encoding threads for the C++ codec (default: 1).",
)
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
@click.option(
    "--legacy", is_flag=True,
    help="Use legacy per-stream compression instead of CSV/packed format.",
)
@click.option(
    "--packed", is_flag=True,
    help="Use packed NQF (SDDL) compression instead of CSV (default).",
)
@click.option(
    "--group-train",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Directory of FASTQ files for group training.",
)
def compress_lossless_fastq_cmd(input_file, output, verbose, force, threads, do_train, models_dir, no_trained,
                                 train_sample_mib, max_time_secs, train_threads, compress_jobs,
                                 legacy, packed, group_train):
    """Lossless FASTQ compression with byte-exact reconstruction.

    By default, uses CSV/TSV decomposition with OpenZL's CSV profile for
    optimal compression. Illumina headers are dictionary-encoded into
    tab-delimited columns. Use --packed for the NQF binary format or
    --legacy for the original per-stream approach.

    Use --train on the first run to train compressors. Use --group-train
    to train from multiple FASTQ files for better generalization.

    \b
    Example:
      nyx compress-lossless-fastq reads.fastq --train
      nyx compress-lossless-fastq reads.fastq
      nyx compress-lossless-fastq reads.fastq --packed --train
      nyx compress-lossless-fastq reads.fastq --legacy --train
      nyx compress-lossless-fastq reads.fastq --group-train /data/fastq/ --train
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

    # Resolve defaults
    if train_threads is None:
        train_threads = os.cpu_count() or 16
    if compress_jobs is None:
        compress_jobs = os.cpu_count() or 4
    train_sample_bytes = train_sample_mib * 1024 * 1024

    if legacy:
        mdir = Path(models_dir) if models_dir else _DEFAULT_MODELS_DIR
        _compress_legacy_fastq(
            input_path, output_path, mdir, do_train, no_trained,
            verbose, threads, train_threads, max_time_secs,
            train_sample_bytes, compress_jobs,
        )
    elif packed:
        mdir = Path(models_dir) if models_dir else _DEFAULT_PACKED_MODELS_DIR
        _compress_packed_fastq(
            input_path, output_path, mdir, do_train, no_trained,
            verbose, train_threads, max_time_secs,
            train_sample_bytes, compress_jobs,
            group_train_dir=group_train,
        )
    else:
        mdir = Path(models_dir) if models_dir else _DEFAULT_CSV_MODELS_DIR
        _compress_csv_fastq(
            input_path, output_path, mdir, do_train, no_trained,
            verbose, train_threads, max_time_secs,
            train_sample_bytes, compress_jobs,
            group_train_dir=group_train,
        )


def _compress_legacy_fastq(
    input_path: Path,
    output_path: Path,
    models_dir: Path,
    do_train: bool,
    no_trained: bool,
    verbose: bool,
    threads: int,
    train_threads: int,
    max_time_secs: int,
    train_sample_bytes: int,
    compress_jobs: int,
):
    """Legacy per-stream FASTQ compression."""
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
        stream_files = fastq_codec.encode(input_path, streams_dir, verbose=verbose, threads=threads)

        if verbose:
            for sf in stream_files:
                click.echo(f"    {sf.name}: {sf.stat().st_size:,} bytes")

        # Step 2 (optional): Train per-stream compressors
        compressors = {}
        if do_train:
            click.echo("  [2/4] Training per-stream compressors...")
            compressors = _train_stream_compressors(
                streams_dir, stream_files, models_dir, verbose=verbose,
                train_threads=train_threads,
                max_time_secs=max_time_secs,
                train_sample_bytes=train_sample_bytes,
            )
            train_elapsed = time.time() - t0
            click.echo(f"    Trained {len(compressors)} compressors in {train_elapsed:.1f}s")
            click.echo(f"    Models saved to: {models_dir}")
        elif not no_trained:
            quality_pos_names = [
                sf.name for sf in stream_files
                if _QUALITY_POS_PATTERN.match(sf.name)
            ]
            compressors = _find_trained_compressors(
                models_dir, quality_pos_names=quality_pos_names)
            if compressors:
                click.echo(f"  Using trained compressors from {models_dir}")
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

            if _is_compressible(name) and original_size > 0:
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

                # Collect results
                stream_stats = {}
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
