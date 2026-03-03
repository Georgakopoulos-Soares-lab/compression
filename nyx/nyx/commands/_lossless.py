"""Internal lossless compression/decompression pipelines for FASTA, FASTQ, VCF, and JSONL.

This module consolidates all lossless pipeline functions. It is imported by
the unified compress.py and decompress.py commands — it has no Click commands
of its own.
"""

import concurrent.futures
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import codec, fastq_codec, jsonl_codec, openzl, vcf_codec
from ..core.zlfasta import create_zlfasta, extract_zlfasta
from ..core.zlfastq import create_zlfastq, extract_zlfastq
from ..core.zljsonl import create_zljsonl, extract_zljsonl
from ..core.zlvcf import create_zlvcf, extract_zlvcf
from ..utils.paths import find_schema


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NYX_ROOT = Path(__file__).resolve().parent.parent.parent  # nyx/nyx/commands -> nyx/

# Stream sets (used by legacy decompression paths for backward compat)
FASTA_COMPRESSIBLE = {
    "meta.bin", "headers.bin", "nmask.bin", "acgtmask.bin",
    "bases2.bin", "exceptions.bin", "case.bin", "wrapping.bin",
}

FASTQ_COMPRESSIBLE = {
    "meta.bin", "headers.bin", "plus.bin", "nmask.bin", "acgtmask.bin",
    "bases2.bin", "exceptions.bin", "case.bin", "seq_wrap.bin",
    "qual_wrap.bin", "quality.bin",
}

# Regex patterns
_QUALITY_POS_PATTERN = re.compile(r"^quality_pos_\d{4}\.bin$")
_CHUNK_PATTERN = re.compile(r"^(.+\.bin)\.(\d{3})$")
_PACKED_CHUNK_PATTERN = re.compile(r"^chunk_\d{6}\.bin$")
_CSV_PART_PATTERN = re.compile(r"^part_\d{3}\.tsv$")
_CSV_SIDECAR_FILES = {"meta.bin", "plus.bin", "wrap.bin"}
_JSONL_TSV_PATTERN = re.compile(r"^.+\.tsv$")
_JSONL_SIDECAR_FILES = {"meta.json"}

# Container magic bytes
ZLFASTA_MAGIC = b"ZLFASTA\x00"
ZLFASTQ_MAGIC = b"ZLFASTQ\x00"
ZLJSONL_MAGIC = b"ZLJSONL\x00"
ZLVCF_MAGIC = b"ZLVCF\x00\x00\x00"

# Default model directories
DEFAULT_FASTA_MODELS_DIR = _NYX_ROOT / "models" / "lossless"
DEFAULT_CSV_MODELS_DIR = _NYX_ROOT / "models" / "lossless_fastq_csv"
DEFAULT_JSONL_MODELS_DIR = _NYX_ROOT / "models" / "lossless_jsonl"
DEFAULT_VCF_MODELS_DIR = _NYX_ROOT / "models" / "lossless_vcf"

# Compressor filenames
_NUCLEOTIDE_FASTA_COMPRESSOR = "nucleotide_fasta.zl_compressor"
_PROTEIN_FASTA_COMPRESSOR = "protein_fasta.zl_compressor"
_CSV_COMPRESSOR = "fastq_csv.zl_compressor"
_JSONL_COMPRESSOR = "jsonl_csv.zl_compressor"
_VCF_COMPRESSOR = "vcf_csv.zl_compressor"

# File extensions for group training
_FASTA_EXTENSIONS = {".fasta", ".fa", ".fna", ".fas", ".fsa"}
_FASTQ_EXTENSIONS = {".fastq", ".fq"}
_JSONL_EXTENSIONS = {".jsonl"}
_VCF_EXTENSIONS = {".vcf"}

# VCF body part regex
_VCF_PART_PATTERN = re.compile(r"^part_\d{3}\.tsv$")
_VCF_SIDECAR_FILES = {"header.vcf", "meta.json"}

# Training sample target per file for group training
_GROUP_TRAIN_CHUNK_TARGET = 200 * 1024 * 1024  # 200 MiB

# Default training time limit per stream (seconds)
DEFAULT_MAX_TIME_SECS = 1800  # 30 minutes

# Default training sample size
DEFAULT_TRAIN_SAMPLE_BYTES = 200 * 1024 * 1024  # 200 MiB


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


def _decompress_one(name, extracted_path, decompressed_path, verbose):
    """Decompress a single stream. Returns (name, success, fallback)."""
    try:
        openzl.decompress(
            extracted_path, decompressed_path,
            force=True,
            verbose=verbose,
        )
        return (name, True, False)
    except openzl.OpenZLError:
        if name == "meta.bin":
            # Backward compat: old v1 archives have raw meta.bin
            shutil.copy2(extracted_path, decompressed_path)
            return (name, True, True)
        raise


def detect_container(filepath: Path) -> str:
    """Detect container type from magic bytes. Returns 'fasta', 'fastq', 'vcf', or 'jsonl'."""
    with open(filepath, "rb") as f:
        magic = f.read(8)
    if magic == ZLFASTA_MAGIC:
        return "fasta"
    if magic == ZLFASTQ_MAGIC:
        return "fastq"
    if magic == ZLJSONL_MAGIC:
        return "jsonl"
    raise click.ClickException(
        f"Unrecognized container format in {filepath.name}. "
        f"Expected .zlfasta, .zlfastq, .zlvcf, or .zljsonl magic bytes."
    )


def _detect_packed_subtype(chunk_path: Path) -> str:
    """Detect whether a decompressed chunk is nucleotide (NXF2) or protein (NXFP).

    Reads the first 4 bytes (magic) of the chunk file.
    Returns 'nucleotide' or 'protein'.
    """
    with open(chunk_path, "rb") as f:
        magic = f.read(4)
    if magic == b"NXFP":
        return "protein"
    return "nucleotide"


def _is_packed_format(entry_names):
    """Detect if container uses the new packed format (chunk_*.bin entries)."""
    return any(_PACKED_CHUNK_PATTERN.match(n) for n in entry_names)


def _is_csv_format(entry_names):
    """Detect if the archive uses CSV/TSV format."""
    names = set(entry_names)
    return (any(_CSV_PART_PATTERN.match(n) for n in names)
            and "meta.bin" in names)


def _is_compressible(name: str, compressible: set, is_fastq: bool = False) -> bool:
    """Check if an entry name is a compressed stream (or chunk of one)."""
    if name in compressible:
        return True
    m = _CHUNK_PATTERN.match(name)
    if m and m.group(1) in compressible:
        return True
    # For FASTQ: also match per-position quality files and their chunks
    if is_fastq:
        if _QUALITY_POS_PATTERN.match(name):
            return True
        if m and _QUALITY_POS_PATTERN.match(m.group(1)):
            return True
    return False


# ---------------------------------------------------------------------------
# FASTA Compress: Nucleotide (NXF2 packed)
# ---------------------------------------------------------------------------

def compress_fasta_packed(
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
        _RAW_PER_CHUNK = 500 * 1024 * 1024   # ~500 MiB raw -> ~350 MiB NXF2
        num_chunks = max(1, (input_size + _RAW_PER_CHUNK - 1) // _RAW_PER_CHUNK)
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding FASTA into packed NXF2 chunks...")
        chunk_files = codec.encode_packed(
            input_path, chunks_dir, num_chunks=num_chunks, verbose=verbose)

        # Safety check: if any chunk still exceeds 500 MiB, re-encode
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


# ---------------------------------------------------------------------------
# FASTA Compress: Protein (NXFP packed)
# ---------------------------------------------------------------------------

def compress_protein_packed(
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


# ---------------------------------------------------------------------------
# FASTQ Compress: CSV/TSV decomposition
# ---------------------------------------------------------------------------

def compress_csv_fastq(
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

        # Sidecar files (meta.bin, plus.bin, wrap.bin) -- store raw (small)
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


# ---------------------------------------------------------------------------
# FASTA Decompress (handles packed NXF2/NXFP and legacy multi-stream)
# ---------------------------------------------------------------------------

def decompress_lossless_fasta(input_path: Path, output_path: Path,
                               verbose: bool = False):
    """Decompress a .zlfasta container back to original FASTA."""
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_dec_fasta_")

    try:
        extract_dir = Path(tmpdir) / "extracted"
        decomp_dir = Path(tmpdir) / "decompressed"
        decomp_dir.mkdir()
        streams_dir = Path(tmpdir) / "streams"
        streams_dir.mkdir()

        # Step 1: Extract container
        click.echo("  [1/3] Extracting .zlfasta container...")
        entries = extract_zlfasta(input_path, extract_dir)

        if verbose:
            for name, path in sorted(entries.items()):
                click.echo(f"    {name}: {path.stat().st_size:,} bytes")

        # Detect packed NXF2 format vs old multi-stream format
        use_packed = _is_packed_format(entries.keys())

        if use_packed:
            # --- New packed FASTA decompression path ---
            click.echo("  [2/3] Decompressing NXF2 chunks with OpenZL...")
            decompress_tasks = []
            for name, extracted_path in sorted(entries.items()):
                if extracted_path.stat().st_size > 0:
                    decompress_tasks.append((name, extracted_path, streams_dir / name))

            if decompress_tasks:
                num_workers = min(os.cpu_count() or 4, len(decompress_tasks))
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=num_workers
                ) as executor:
                    futures = {}
                    for name, extracted_path, decompressed_path in decompress_tasks:
                        fut = executor.submit(
                            _decompress_one,
                            name, extracted_path, decompressed_path, verbose,
                        )
                        futures[fut] = name
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()

            # Step 3: Decode packed chunks back to FASTA
            first_chunk = sorted(streams_dir.glob("chunk_*.bin"))[0]
            packed_subtype = _detect_packed_subtype(first_chunk)

            if packed_subtype == "protein":
                click.echo("  [3/3] Reconstructing protein FASTA from packed chunks...")
                codec.decode_protein_packed(streams_dir, output_path, verbose=verbose)
            else:
                click.echo("  [3/3] Reconstructing FASTA from packed chunks...")
                codec.decode_packed(streams_dir, output_path, verbose=verbose)
        else:
            # --- Old multi-stream decompression path ---
            click.echo("  [2/3] Decompressing streams with OpenZL...")

            decompress_tasks = []
            copy_tasks = []
            for name, extracted_path in sorted(entries.items()):
                if _is_compressible(name, FASTA_COMPRESSIBLE) and extracted_path.stat().st_size > 0:
                    decompress_tasks.append((name, extracted_path, decomp_dir / name))
                else:
                    copy_tasks.append((extracted_path, decomp_dir / name))

            for src, dst in copy_tasks:
                shutil.copy2(src, dst)

            if decompress_tasks:
                num_workers = min(os.cpu_count() or 4, len(decompress_tasks))
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=num_workers
                ) as executor:
                    futures = {}
                    for name, extracted_path, decompressed_path in decompress_tasks:
                        fut = executor.submit(
                            _decompress_one,
                            name, extracted_path, decompressed_path, verbose,
                        )
                        futures[fut] = name
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()

            # Reassemble chunked streams
            chunk_groups = {}
            non_chunked = []
            for name in sorted(entries.keys()):
                m = _CHUNK_PATTERN.match(name)
                if m:
                    base = m.group(1)
                    chunk_groups.setdefault(base, []).append(decomp_dir / name)
                else:
                    non_chunked.append(name)

            for name in non_chunked:
                src = decomp_dir / name
                if src.exists():
                    shutil.copy2(src, streams_dir / name)

            for base_name, chunk_paths in sorted(chunk_groups.items()):
                out_path = streams_dir / base_name
                with open(out_path, "wb") as out_f:
                    for cp in sorted(chunk_paths):
                        with open(cp, "rb") as in_f:
                            shutil.copyfileobj(in_f, out_f)
                if verbose:
                    click.echo(
                        f"    Reassembled {base_name} from "
                        f"{len(chunk_paths)} chunks "
                        f"({out_path.stat().st_size:,} bytes)"
                    )

            # Step 3: Decode streams back to FASTA
            click.echo("  [3/3] Reconstructing FASTA...")
            codec.decode(streams_dir, output_path, verbose=verbose)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# FASTQ Decompress (handles CSV, packed NQF, and legacy per-stream)
# ---------------------------------------------------------------------------

def decompress_lossless_fastq(input_path: Path, output_path: Path,
                               verbose: bool = False):
    """Decompress a .zlfastq container back to original FASTQ."""
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_dec_fastq_")

    try:
        extract_dir = Path(tmpdir) / "extracted"
        decomp_dir = Path(tmpdir) / "decompressed"
        decomp_dir.mkdir()
        streams_dir = Path(tmpdir) / "streams"
        streams_dir.mkdir()

        # Step 1: Extract .zlfastq container
        click.echo("  [1/3] Extracting .zlfastq container...")
        entries = extract_zlfastq(input_path, extract_dir)

        if verbose:
            for name, path in sorted(entries.items()):
                click.echo(f"    {name}: {path.stat().st_size:,} bytes")

        if _is_csv_format(entries.keys()):
            # --- CSV/TSV path ---
            click.echo("    Detected CSV/TSV format")

            csv_dir = Path(tmpdir) / "csv_parts"
            csv_dir.mkdir()

            # Separate TSV parts (compressed) from sidecars (raw)
            tsv_entries = {n: p for n, p in entries.items()
                          if _CSV_PART_PATTERN.match(n)}
            sidecar_entries = {n: p for n, p in entries.items()
                              if n in _CSV_SIDECAR_FILES}

            # Step 2: Decompress TSV parts (parallel) + copy sidecars
            click.echo("  [2/3] Decompressing TSV parts with OpenZL...")

            for name, extracted_path in sidecar_entries.items():
                shutil.copy2(extracted_path, csv_dir / name)

            if tsv_entries:
                num_workers = min(os.cpu_count() or 4, len(tsv_entries))
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=num_workers
                ) as executor:
                    futures = {}
                    for name, extracted_path in sorted(tsv_entries.items()):
                        decompressed_path = csv_dir / name
                        fut = executor.submit(
                            _decompress_one,
                            name, extracted_path, decompressed_path, verbose,
                        )
                        futures[fut] = name
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()

            if verbose:
                for p in sorted(csv_dir.iterdir()):
                    click.echo(f"    {p.name}: {p.stat().st_size:,} bytes")

            # Step 3: Decode CSV back to FASTQ
            click.echo("  [3/3] Reconstructing FASTQ from CSV/TSV...")
            fastq_codec.decode_csv(csv_dir, output_path, verbose=verbose)

        elif _is_packed_format(entries.keys()):
            # --- Packed NQF path ---
            click.echo("    Detected packed NQF format")

            packed_dir = Path(tmpdir) / "packed_chunks"
            packed_dir.mkdir()

            chunk_entries = {n: p for n, p in entries.items()
                            if _PACKED_CHUNK_PATTERN.match(n)}

            # Step 2: Decompress packed chunks (parallel)
            click.echo("  [2/3] Decompressing NQF chunks with OpenZL...")
            num_workers = min(os.cpu_count() or 4, len(chunk_entries))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=num_workers
            ) as executor:
                futures = {}
                for name, extracted_path in sorted(chunk_entries.items()):
                    decompressed_path = packed_dir / name
                    fut = executor.submit(
                        _decompress_one,
                        name, extracted_path, decompressed_path, verbose,
                    )
                    futures[fut] = name
                for fut in concurrent.futures.as_completed(futures):
                    fut.result()

            if verbose:
                for name in sorted(chunk_entries):
                    dp = packed_dir / name
                    click.echo(f"    {name}: {dp.stat().st_size:,} bytes")

            # Step 3: Decode packed chunks back to FASTQ
            click.echo("  [3/3] Reconstructing FASTQ from packed chunks...")
            fastq_codec.decode_packed(packed_dir, output_path, verbose=verbose)

        else:
            # --- Legacy per-stream path ---
            click.echo("    Detected legacy per-stream format")

            click.echo("  [2/3] Decompressing streams with OpenZL...")

            decompress_tasks = []
            copy_tasks = []
            for name, extracted_path in sorted(entries.items()):
                if _is_compressible(name, FASTQ_COMPRESSIBLE, is_fastq=True) and extracted_path.stat().st_size > 0:
                    decompress_tasks.append((name, extracted_path, decomp_dir / name))
                else:
                    copy_tasks.append((extracted_path, decomp_dir / name))

            for src, dst in copy_tasks:
                shutil.copy2(src, dst)

            if decompress_tasks:
                num_workers = min(os.cpu_count() or 4, len(decompress_tasks))
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=num_workers
                ) as executor:
                    futures = {}
                    for name, extracted_path, decompressed_path in decompress_tasks:
                        fut = executor.submit(
                            _decompress_one,
                            name, extracted_path, decompressed_path, verbose,
                        )
                        futures[fut] = name
                    for fut in concurrent.futures.as_completed(futures):
                        fut.result()

            # Reassemble chunked streams
            chunk_groups = {}
            non_chunked = []
            for name in sorted(entries.keys()):
                m = _CHUNK_PATTERN.match(name)
                if m:
                    base = m.group(1)
                    chunk_groups.setdefault(base, []).append(decomp_dir / name)
                else:
                    non_chunked.append(name)

            for name in non_chunked:
                src = decomp_dir / name
                if src.exists():
                    shutil.copy2(src, streams_dir / name)

            for base_name, chunk_paths in sorted(chunk_groups.items()):
                out_path = streams_dir / base_name
                with open(out_path, "wb") as out_f:
                    for cp in sorted(chunk_paths):
                        with open(cp, "rb") as in_f:
                            shutil.copyfileobj(in_f, out_f)
                if verbose:
                    click.echo(
                        f"    Reassembled {base_name} from "
                        f"{len(chunk_paths)} chunks "
                        f"({out_path.stat().st_size:,} bytes)"
                    )

            # Step 3: Decode streams back to FASTQ
            click.echo("  [3/3] Reconstructing FASTQ...")
            fastq_codec.decode(streams_dir, output_path, verbose=verbose)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# VCF Compress: header/body split + CSV/TSV compression
# ---------------------------------------------------------------------------

def compress_vcf(
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
    """VCF compression via header/body split + OpenZL CSV profile."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_vcf_")

    try:
        vcf_dir = Path(tmpdir) / "vcf"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode VCF into header + body TSV parts + meta.json
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Splitting VCF into header + body parts...")
        output_files = vcf_codec.encode(
            input_path, vcf_dir, verbose=verbose)

        tsv_parts = sorted(f for f in output_files
                           if _VCF_PART_PATTERN.match(f.name))
        sidecar_files = [f for f in output_files
                         if f.name in _VCF_SIDECAR_FILES]

        total_tsv_size = sum(f.stat().st_size for f in tsv_parts)
        click.echo(f"    {len(tsv_parts)} body part(s), "
                   f"{total_tsv_size:,} bytes total")
        if verbose:
            for f in sorted(output_files):
                click.echo(f"    {f.name}: {f.stat().st_size:,} bytes")

        # Step 2: Train or load CSV compressor
        compressor_path = models_dir / _VCF_COMPRESSOR
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                group_path = Path(group_train_dir).resolve()
                vcf_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _VCF_EXTENSIONS
                )
                if not vcf_files:
                    raise click.ClickException(
                        f"No VCF files found in {group_path}")
                click.echo(f"  [2/4] Group training from "
                           f"{len(vcf_files)} VCF file(s)...")

                total_train_size = 0
                for idx, vf in enumerate(vcf_files):
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_files = vcf_codec.encode(
                            vf, sample_tmp, verbose=False)
                    except Exception as e:
                        click.echo(f"    Warning: skipping {vf.name}: {e}")
                        continue
                    sample_tsvs = [f for f in sample_files
                                   if _VCF_PART_PATTERN.match(f.name)]
                    for sf in sample_tsvs[:1]:
                        sz = sf.stat().st_size
                        if sz > train_sample_bytes:
                            dest = training_dir / f"sample_{idx:03d}_{vf.stem}.tsv"
                            sz = _truncate_tsv_at_newline(
                                sf, dest, train_sample_bytes)
                        else:
                            dest = training_dir / f"sample_{idx:03d}_{vf.stem}.tsv"
                            shutil.copy2(sf, dest)
                        total_train_size += sz
                    shutil.rmtree(sample_tmp, ignore_errors=True)

                input_in_group = any(
                    f.resolve() == input_path.resolve() for f in vcf_files)
                if not input_in_group and tsv_parts:
                    dest = training_dir / f"sample_{len(vcf_files):03d}_{input_path.stem}.tsv"
                    sz = tsv_parts[0].stat().st_size
                    if sz > train_sample_bytes:
                        _truncate_tsv_at_newline(
                            tsv_parts[0], dest, train_sample_bytes)
                    else:
                        shutil.copy2(tsv_parts[0], dest)
                    total_train_size += min(sz, train_sample_bytes)

                num_samples = len(list(training_dir.iterdir()))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                click.echo(f"  [2/4] Training CSV compressor...")
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
                        sz = _truncate_tsv_at_newline(
                            src, dest, train_sample_bytes)
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
                click.echo(
                    f"    Trained compressor: "
                    f"{compressor_path.stat().st_size:,} bytes "
                    f"in {train_elapsed:.1f}s")
                click.echo(f"    Saved to: {compressor_path}")
            else:
                click.echo(
                    "    Warning: training produced no output, "
                    "falling back to csv profile")
                compressor_path = None
        elif not no_trained:
            if compressor_path.is_file() and compressor_path.stat().st_size > 0:
                click.echo(
                    f"  Using trained VCF compressor: {compressor_path}")
            else:
                compressor_path = None
        else:
            compressor_path = None

        # Step 3: Compress body parts + sidecars
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing with OpenZL CSV profile...")

        entries = {}

        # Sidecar files: header.vcf and meta.json
        for sf in sidecar_files:
            data = sf.read_bytes()
            orig_size = len(data)
            if orig_size > 1024:
                compressed_file = compressed_dir / (sf.name + ".zl")
                try:
                    openzl.compress(
                        sf, compressed_file,
                        profile="serial",
                        force=True, verbose=False,
                    )
                    comp_data = compressed_file.read_bytes()
                    if len(comp_data) < orig_size:
                        entries[sf.name] = (comp_data, orig_size)
                        if verbose:
                            ratio = orig_size / len(comp_data) if comp_data else 0
                            click.echo(
                                f"    {sf.name}: {orig_size:,} -> "
                                f"{len(comp_data):,} ({ratio:.2f}x) [serial]")
                        continue
                except openzl.OpenZLError:
                    pass
            entries[sf.name] = (data, orig_size)
            if verbose:
                click.echo(f"    {sf.name}: {orig_size:,} bytes (raw)")

        def _compress_vcf_part(part_path, comp_path):
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
                    click.echo(
                        f"    Warning: trained compressor failed for "
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
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=num_workers
        ) as executor:
            futures = {
                executor.submit(
                    _compress_vcf_part, p, compressor_path): p
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
                    ratio = (orig_size / len(compressed_data)
                             if compressed_data else 0)
                    click.echo(
                        f"    {name}: {orig_size:,} -> "
                        f"{len(compressed_data):,} ({ratio:.2f}x) [{mode}]")

        if total_comp > 0:
            click.echo(
                f"    Body total: {total_orig:,} -> {total_comp:,} "
                f"({total_orig / total_comp:.2f}x)")

        # Step 4: Bundle into .zlvcf container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating .zlvcf container...")
        create_zlvcf(output_path, entries)

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


# ---------------------------------------------------------------------------
# VCF Decompress
# ---------------------------------------------------------------------------

def decompress_lossless_vcf(input_path: Path, output_path: Path,
                             verbose: bool = False):
    """Decompress a .zlvcf container back to original VCF."""
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_dec_vcf_")

    try:
        extract_dir = Path(tmpdir) / "extracted"
        decomp_dir = Path(tmpdir) / "decompressed"
        decomp_dir.mkdir()

        # Step 1: Extract .zlvcf container
        click.echo("  [1/3] Extracting .zlvcf container...")
        entries = extract_zlvcf(input_path, extract_dir)

        if verbose:
            for name, path in sorted(entries.items()):
                click.echo(f"    {name}: {path.stat().st_size:,} bytes")

        # Separate TSV parts from sidecars
        tsv_entries = {n: p for n, p in entries.items()
                       if _VCF_PART_PATTERN.match(n)}
        sidecar_entries = {n: p for n, p in entries.items()
                          if n in _VCF_SIDECAR_FILES}

        # Step 2: Decompress all entries
        click.echo("  [2/3] Decompressing body parts with OpenZL...")

        # Decompress sidecars (may be compressed or raw)
        for name, extracted_path in sidecar_entries.items():
            dest = decomp_dir / name
            try:
                openzl.decompress(
                    extracted_path, dest,
                    force=True, verbose=verbose,
                )
            except openzl.OpenZLError:
                shutil.copy2(extracted_path, dest)

        # Decompress TSV parts (parallel)
        if tsv_entries:
            num_workers = min(os.cpu_count() or 4, len(tsv_entries))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=num_workers
            ) as executor:
                futures = {}
                for name, extracted_path in sorted(tsv_entries.items()):
                    decompressed_path = decomp_dir / name
                    fut = executor.submit(
                        _decompress_one,
                        name, extracted_path, decompressed_path, verbose,
                    )
                    futures[fut] = name
                for fut in concurrent.futures.as_completed(futures):
                    fut.result()

        if verbose:
            for p in sorted(decomp_dir.iterdir()):
                click.echo(f"    {p.name}: {p.stat().st_size:,} bytes")

        # Step 3: Reconstruct VCF from header + body parts
        click.echo("  [3/3] Reconstructing VCF...")
        vcf_codec.decode(decomp_dir, output_path, verbose=verbose)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# JSONL Compress: type-grouped TSV decomposition + OpenZL CSV profile
# ---------------------------------------------------------------------------

def compress_jsonl(
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
    """JSONL compression via type-grouped TSV decomposition + OpenZL CSV profile."""
    input_size = input_path.stat().st_size

    click.echo(f"Input:  {input_path.name} ({input_size:,} bytes)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_jsonl_")

    try:
        tsv_dir = Path(tmpdir) / "tsv"
        compressed_dir = Path(tmpdir) / "compressed"
        compressed_dir.mkdir()

        # Step 1: Encode JSONL into type-grouped TSVs + meta.json
        step1_label = "[1/4]" if do_train else "[1/3]"
        click.echo(f"  {step1_label} Encoding JSONL into type-grouped TSVs...")
        tsv_paths, meta_path = jsonl_codec.encode(
            input_path, tsv_dir, verbose=verbose)

        total_tsv_size = sum(f.stat().st_size for f in tsv_paths)
        meta_size = meta_path.stat().st_size
        click.echo(f"    {len(tsv_paths)} type TSV(s), "
                   f"{total_tsv_size:,} bytes TSV + {meta_size:,} bytes meta")
        if verbose:
            for f in tsv_paths:
                click.echo(f"    {f.name}: {f.stat().st_size:,} bytes")

        # Step 2: Train or load CSV compressor
        compressor_path = models_dir / _JSONL_COMPRESSOR
        if do_train:
            models_dir.mkdir(parents=True, exist_ok=True)
            training_dir = Path(tmpdir) / "training"
            training_dir.mkdir()

            if group_train_dir:
                group_path = Path(group_train_dir).resolve()
                jsonl_files = sorted(
                    f for f in group_path.iterdir()
                    if f.is_file() and f.suffix.lower() in _JSONL_EXTENSIONS
                )
                if not jsonl_files:
                    raise click.ClickException(
                        f"No JSONL files found in {group_path}")
                click.echo(f"  [2/4] Group training from {len(jsonl_files)} JSONL file(s)...")

                total_train_size = 0
                for idx, jl_file in enumerate(jsonl_files):
                    sample_tmp = Path(tmpdir) / f"group_{idx:03d}"
                    try:
                        sample_tsvs, _ = jsonl_codec.encode(
                            jl_file, sample_tmp, verbose=False)
                    except Exception as e:
                        click.echo(f"    Warning: skipping {jl_file.name}: {e}")
                        continue
                    for sf in sample_tsvs:
                        sz = sf.stat().st_size
                        dest = training_dir / f"sample_{idx:03d}_{sf.name}"
                        if sz > train_sample_bytes:
                            sz = _truncate_tsv_at_newline(sf, dest, train_sample_bytes)
                        else:
                            shutil.copy2(sf, dest)
                        total_train_size += sz
                    shutil.rmtree(sample_tmp, ignore_errors=True)

                # Include input file's TSVs if not already in group
                input_in_group = any(
                    f.resolve() == input_path for f in jsonl_files)
                if not input_in_group:
                    for sf in tsv_paths:
                        dest = training_dir / f"sample_{len(jsonl_files):03d}_{sf.name}"
                        shutil.copy2(sf, dest)
                        total_train_size += sf.stat().st_size

                num_samples = len(list(training_dir.iterdir()))
                click.echo(f"    {num_samples} samples, "
                           f"total {total_train_size:,} bytes")
            else:
                click.echo(f"  [2/4] Training CSV compressor on type TSVs...")
                # Copy all type TSVs to training dir (truncated if needed)
                total_sample_size = 0
                for sf in tsv_paths:
                    sz = sf.stat().st_size
                    if sz > train_sample_bytes:
                        dest = training_dir / sf.name
                        sz = _truncate_tsv_at_newline(sf, dest, train_sample_bytes)
                    else:
                        shutil.copy2(sf, training_dir / sf.name)
                    total_sample_size += sz
                click.echo(f"    Training on {len(tsv_paths)} type TSV(s): "
                           f"{total_sample_size:,} bytes")

            # Use serial profile for training (csv profile triggers an OpenZL
            # assertion in encode_frameheader.c with high-cardinality string
            # columns).  The trained compressor still compresses TSVs well.
            openzl.train(
                sample_dir=training_dir,
                output_file=compressor_path,
                profile="serial",
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

        # Step 3: Compress TSVs with OpenZL CSV profile (parallel)
        step_label = "[3/4]" if do_train else "[2/3]"
        click.echo(f"  {step_label} Compressing with OpenZL CSV profile...")

        entries = {}

        # meta.json — compress with OpenZL (can be large for many types/records)
        meta_compressed = compressed_dir / "meta.json.zl"
        openzl.compress(
            meta_path, meta_compressed,
            profile="serial",
            force=True, verbose=verbose,
        )
        meta_orig_size = meta_path.stat().st_size
        meta_comp_data = meta_compressed.read_bytes()
        entries["meta.json"] = (meta_comp_data, meta_orig_size)
        if verbose:
            ratio = meta_orig_size / len(meta_comp_data) if meta_comp_data else 0
            click.echo(f"    meta.json: {meta_orig_size:,} -> "
                       f"{len(meta_comp_data):,} ({ratio:.2f}x) [serial]")

        def _compress_tsv_part(part_path, comp_path):
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

        num_workers = min(compress_jobs, len(tsv_paths))
        total_orig = 0
        total_comp = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {
                executor.submit(_compress_tsv_part, p, compressor_path): p
                for p in tsv_paths
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

        # Step 4: Bundle into .zljsonl container
        step_label = "[4/4]" if do_train else "[3/3]"
        click.echo(f"  {step_label} Creating .zljsonl container...")
        create_zljsonl(output_path, entries)

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


# ---------------------------------------------------------------------------
# JSONL Decompress
# ---------------------------------------------------------------------------

def decompress_lossless_jsonl(input_path: Path, output_path: Path,
                               verbose: bool = False):
    """Decompress a .zljsonl container back to original JSONL."""
    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_dec_jsonl_")

    try:
        extract_dir = Path(tmpdir) / "extracted"
        tsv_dir = Path(tmpdir) / "tsv_parts"
        tsv_dir.mkdir()

        # Step 1: Extract .zljsonl container
        click.echo("  [1/3] Extracting .zljsonl container...")
        entries = extract_zljsonl(input_path, extract_dir)

        if verbose:
            for name, path in sorted(entries.items()):
                click.echo(f"    {name}: {path.stat().st_size:,} bytes")

        # All entries are compressed (TSVs + meta.json)
        all_compressed = dict(entries)

        # Step 2: Decompress all entries (parallel)
        click.echo("  [2/3] Decompressing type TSVs + meta with OpenZL...")

        if all_compressed:
            num_workers = min(os.cpu_count() or 4, len(all_compressed))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=num_workers
            ) as executor:
                futures = {}
                for name, extracted_path in sorted(all_compressed.items()):
                    decompressed_path = tsv_dir / name
                    fut = executor.submit(
                        _decompress_one,
                        name, extracted_path, decompressed_path, verbose,
                    )
                    futures[fut] = name
                for fut in concurrent.futures.as_completed(futures):
                    fut.result()

        if verbose:
            for p in sorted(tsv_dir.iterdir()):
                click.echo(f"    {p.name}: {p.stat().st_size:,} bytes")

        # Step 3: Decode TSVs back to JSONL
        click.echo("  [3/3] Reconstructing JSONL from type-grouped TSVs...")
        jsonl_codec.decode(tsv_dir, output_path, verbose=verbose)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
