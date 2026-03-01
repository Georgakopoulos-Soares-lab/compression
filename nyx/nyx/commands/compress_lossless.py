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
from ..core.detect import detect_fasta_subtype, detect_filetype
from ..core.zlfasta import create_zlfasta
from ..core.zlfastq import create_zlfastq
from ..utils.paths import find_schema

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


_NUCLEOTIDE_FASTA_COMPRESSOR = "nucleotide_fasta.zl_compressor"
_PROTEIN_FASTA_COMPRESSOR = "protein_fasta.zl_compressor"


_FASTA_EXTENSIONS = {".fasta", ".fa", ".fna", ".fas", ".fsa"}

# Target ~200 MiB NXF2 per training sample from each file
_GROUP_TRAIN_CHUNK_TARGET = 200 * 1024 * 1024


def _compress_fasta_packed(
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
    group_train_dir: str = None,
):
    """FASTA compression via packed NXF2 binary + single SDDL compressor."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_fasta_packed_")

    try:
        chunks_dir = Path(tmpdir) / "chunks"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode-packed
        # zli cannot compress files >500 MiB. NXF2 packed size is ~60-75%
        # of raw FASTA, so we target 400 MiB NXF2 chunks. With a ~0.65
        # packing ratio, that means splitting at ~600 MiB of raw input.
        _MAX_NXF2_CHUNK = 400 * 1024 * 1024  # 400 MiB target per NXF2 chunk
        _RAW_PER_CHUNK = 500 * 1024 * 1024   # ~500 MiB raw → ~350 MiB NXF2
        num_chunks = max(1, (input_size + _RAW_PER_CHUNK - 1) // _RAW_PER_CHUNK)
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding FASTA into packed NXF2 chunks...")
        chunk_files = codec.encode_packed(
            input_path, chunks_dir, num_chunks=num_chunks, verbose=verbose)

        # Safety check: if any chunk still exceeds 500 MiB, re-encode
        # with more chunks (can happen with unusually dense FASTA)
        max_chunk_size = max(c.stat().st_size for c in chunk_files)
        if max_chunk_size > 500 * 1024 * 1024:
            needed = max(num_chunks + 1, input_size // _MAX_NXF2_CHUNK + 1)
            click.echo(f"    Largest chunk {max_chunk_size:,} > 500 MiB, "
                        f"re-encoding with {needed} chunks...")
            shutil.rmtree(chunks_dir)
            chunk_files = codec.encode_packed(
                input_path, chunks_dir, num_chunks=needed, verbose=verbose)

        click.echo(f"    {len(chunk_files)} chunk(s), total "
                    f"{sum(c.stat().st_size for c in chunk_files):,} bytes")

        # Step 2: Train or load compressor
        compressor_path = models_dir / _NUCLEOTIDE_FASTA_COMPRESSOR
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                # --- Group training: sample from many FASTA files ---
                group_path = Path(group_train_dir).resolve()
                fasta_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _FASTA_EXTENSIONS
                )
                if not fasta_files:
                    raise click.ClickException(
                        f"No FASTA files found in {group_path} "
                        f"(expected extensions: {', '.join(sorted(_FASTA_EXTENSIONS))})"
                    )
                click.echo(f"  [2/4] Group training from {len(fasta_files)} FASTA file(s)...")

                total_train_size = 0
                for idx, fasta_file in enumerate(fasta_files):
                    file_size = fasta_file.stat().st_size
                    # Chunk each file so first chunk is ~200 MiB NXF2
                    n = max(1, (file_size + _GROUP_TRAIN_CHUNK_TARGET - 1)
                            // _GROUP_TRAIN_CHUNK_TARGET)
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_chunks = codec.encode_packed(
                            fasta_file, sample_tmp,
                            num_chunks=n, verbose=False,
                        )
                    except Exception as e:
                        click.echo(f"    Warning: skipping {fasta_file.name}: {e}")
                        continue

                    if sample_chunks:
                        first = sample_chunks[0]
                        dest = training_dir / f"sample_{idx:03d}_{fasta_file.stem}.bin"
                        shutil.copy2(first, dest)
                        sz = first.stat().st_size
                        total_train_size += sz
                        if verbose:
                            click.echo(f"    {fasta_file.name}: {sz:,} bytes")

                    # Clean up temp encoding
                    shutil.rmtree(sample_tmp, ignore_errors=True)

                # Also include input file's first chunk if not already covered
                input_in_group = any(
                    f.resolve() == input_path for f in fasta_files
                )
                if not input_in_group:
                    dest = training_dir / f"sample_{len(fasta_files):03d}_{input_path.stem}.bin"
                    shutil.copy2(chunk_files[0], dest)
                    total_train_size += chunk_files[0].stat().st_size
                    if verbose:
                        click.echo(f"    {input_path.name} (input): "
                                   f"{chunk_files[0].stat().st_size:,} bytes")

                num_samples = len(list(training_dir.glob("*.bin")))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                # --- Single-file training: representative sample ---
                click.echo("  [2/4] Training SDDL compressor for nucleotide FASTA...")
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

            sddl_path = find_schema("nucleotide_fasta.sddl")
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

        # Step 4: Bundle into .zlfasta container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating .zlfasta container...")
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


def _compress_protein_packed(
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
    group_train_dir: str = None,
):
    """Protein FASTA compression via packed NXFP binary + single SDDL compressor."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_protein_packed_")

    try:
        chunks_dir = Path(tmpdir) / "chunks"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode-protein-packed
        _MAX_NXF_CHUNK = 400 * 1024 * 1024
        _RAW_PER_CHUNK = 500 * 1024 * 1024
        num_chunks = max(1, (input_size + _RAW_PER_CHUNK - 1) // _RAW_PER_CHUNK)
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding protein FASTA into packed NXFP chunks...")
        chunk_files = codec.encode_protein_packed(
            input_path, chunks_dir, num_chunks=num_chunks, verbose=verbose)

        # Safety check: if any chunk still exceeds 500 MiB, re-encode
        max_chunk_size = max(c.stat().st_size for c in chunk_files)
        if max_chunk_size > 500 * 1024 * 1024:
            needed = max(num_chunks + 1, input_size // _MAX_NXF_CHUNK + 1)
            click.echo(f"    Largest chunk {max_chunk_size:,} > 500 MiB, "
                        f"re-encoding with {needed} chunks...")
            shutil.rmtree(chunks_dir)
            chunk_files = codec.encode_protein_packed(
                input_path, chunks_dir, num_chunks=needed, verbose=verbose)

        click.echo(f"    {len(chunk_files)} chunk(s), total "
                    f"{sum(c.stat().st_size for c in chunk_files):,} bytes")

        # Step 2: Train or load compressor
        compressor_path = models_dir / _PROTEIN_FASTA_COMPRESSOR
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                # Group training: sample from many protein FASTA files
                group_path = Path(group_train_dir).resolve()
                fasta_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _FASTA_EXTENSIONS
                )
                if not fasta_files:
                    raise click.ClickException(
                        f"No FASTA files found in {group_path} "
                        f"(expected extensions: {', '.join(sorted(_FASTA_EXTENSIONS))})"
                    )
                click.echo(f"  [2/4] Group training from {len(fasta_files)} protein FASTA file(s)...")

                total_train_size = 0
                for idx, fasta_file in enumerate(fasta_files):
                    file_size = fasta_file.stat().st_size
                    n = max(1, (file_size + _GROUP_TRAIN_CHUNK_TARGET - 1)
                            // _GROUP_TRAIN_CHUNK_TARGET)
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_chunks = codec.encode_protein_packed(
                            fasta_file, sample_tmp,
                            num_chunks=n, verbose=False,
                        )
                    except Exception as e:
                        click.echo(f"    Warning: skipping {fasta_file.name}: {e}")
                        continue

                    if sample_chunks:
                        first = sample_chunks[0]
                        dest = training_dir / f"sample_{idx:03d}_{fasta_file.stem}.bin"
                        shutil.copy2(first, dest)
                        sz = first.stat().st_size
                        total_train_size += sz
                        if verbose:
                            click.echo(f"    {fasta_file.name}: {sz:,} bytes")

                    shutil.rmtree(sample_tmp, ignore_errors=True)

                input_in_group = any(
                    f.resolve() == input_path for f in fasta_files
                )
                if not input_in_group:
                    dest = training_dir / f"sample_{len(fasta_files):03d}_{input_path.stem}.bin"
                    shutil.copy2(chunk_files[0], dest)
                    total_train_size += chunk_files[0].stat().st_size

                num_samples = len(list(training_dir.glob("*.bin")))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                # Single-file training: representative sample
                click.echo("  [2/4] Training SDDL compressor for protein FASTA...")
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

            sddl_path = find_schema("protein_fasta.sddl")
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

        # Step 4: Bundle into .zlfasta container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating .zlfasta container...")
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
    type=click.Choice(["auto", "fasta", "fastq", "protein"], case_sensitive=False),
    default="auto",
    help="Input file type (default: auto-detect). Use 'protein' for protein FASTA.",
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
@click.option(
    "--group-train",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help="Train compressor from samples of all FASTA files in this directory (implies --train).",
)
def compress_lossless_cmd(input_file, output, verbose, force, file_type, threads,
                          do_train, models_dir, no_trained,
                          train_sample_mib, max_time_secs, train_threads,
                          compress_jobs, group_train):
    """Lossless FASTA/FASTQ compression with byte-exact reconstruction.

    Auto-detects FASTA vs FASTQ input (or use --type to specify).
    For FASTA, auto-detects nucleotide vs protein sequences.

    FASTA files are encoded into packed binary chunks (NXF2 for nucleotide,
    NXFP for protein) and compressed with a single SDDL-trained compressor.
    FASTQ files are decomposed into typed streams and compressed independently.

    Use --train on the first run to train format-specific compressors.
    Subsequent runs will automatically use the trained models for
    better compression ratios.

    \b
    Example:
      nyx compress-lossless genome.fasta                              # auto-detect FASTA
      nyx compress-lossless proteins.fasta --type protein              # explicit protein
      nyx compress-lossless reads.fastq --threads 4                   # parallel FASTQ
      nyx compress-lossless genome.fasta --train                       # train + compress
      nyx compress-lossless genome.fasta --group-train /data/genomes/  # train from many files
      nyx compress-lossless data.fa --type fasta                       # explicit nucleotide
    """
    input_path = Path(input_file).resolve()

    # Resolve defaults
    if train_threads is None:
        train_threads = os.cpu_count() or 16
    if compress_jobs is None:
        compress_jobs = os.cpu_count() or 4
    train_sample_bytes = train_sample_mib * 1024 * 1024

    # Detect file type
    is_protein = False
    if file_type == "protein":
        is_protein = True
        file_type = "fasta"  # protein is a sub-type of FASTA
    elif file_type == "auto":
        detected = detect_filetype(input_path)
        if detected not in ("fasta", "fastq"):
            raise click.ClickException(
                f"Cannot auto-detect file type for {input_path.name}. "
                f"Use --type fasta, --type protein, or --type fastq."
            )
        file_type = detected
        # For FASTA, auto-detect nucleotide vs protein
        if file_type == "fasta":
            subtype = detect_fasta_subtype(input_path)
            if subtype == "protein":
                is_protein = True

    if is_protein:
        click.echo("Format: PROTEIN FASTA")
    else:
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

    # Protein FASTA uses the NXFP + SDDL pipeline
    if is_protein:
        _compress_protein_packed(
            input_path=input_path,
            output_path=output_path,
            models_dir=mdir,
            do_train=do_train or bool(group_train),
            no_trained=no_trained,
            verbose=verbose,
            threads=threads,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
        return

    # Nucleotide FASTA uses the packed NXF2 + SDDL pipeline
    if not is_fastq:
        _compress_fasta_packed(
            input_path=input_path,
            output_path=output_path,
            models_dir=mdir,
            do_train=do_train or bool(group_train),
            no_trained=no_trained,
            verbose=verbose,
            threads=threads,
            train_threads=train_threads,
            max_time_secs=max_time_secs,
            train_sample_bytes=train_sample_bytes,
            compress_jobs=compress_jobs,
            group_train_dir=group_train,
        )
        return

    # --- FASTQ path (unchanged) ---
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
        click.echo(f"  {step1_label} Encoding FASTQ into streams...")
        stream_files = fastq_codec.encode(
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
                is_fastq=True, verbose=verbose,
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
            ]
            compressors = _find_trained_compressors(
                mdir, compressible_streams,
                is_fastq=True,
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

            if _is_fastq_compressible(name) and original_size > 0:
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

        # Step 4: Bundle into container
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
