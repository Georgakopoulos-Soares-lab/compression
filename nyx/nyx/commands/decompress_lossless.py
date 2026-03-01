"""nyx decompress-lossless — Unified lossless decompression from .zlfasta/.zlfastq containers."""

import concurrent.futures
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import codec, fastq_codec, openzl
from ..core.zlfasta import extract_zlfasta
from ..core.zlfastq import extract_zlfastq

# Magic bytes for container detection
_ZLFASTA_MAGIC = b"ZLFASTA\x00"
_ZLFASTQ_MAGIC = b"ZLFASTQ\x00"

# Streams per format that were compressed (need decompression)
_FASTA_COMPRESSIBLE = {
    "meta.bin",
    "headers.bin",
    "nmask.bin",
    "acgtmask.bin",
    "bases2.bin",
    "exceptions.bin",
    "case.bin",
    "wrapping.bin",
}

_FASTQ_COMPRESSIBLE = {
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

# Pattern matching chunked stream names like "bases2.bin.000"
_CHUNK_PATTERN = re.compile(r"^(.+\.bin)\.(\d{3})$")

# Pattern for per-position quality files (v3 layout=2)
_QUALITY_POS_PATTERN = re.compile(r"^quality_pos_\d{4}\.bin$")

# Pattern for packed NXF2 chunk files
_PACKED_CHUNK_PATTERN = re.compile(r"^chunk_\d{6}\.bin$")


def _is_packed_format(entry_names):
    """Detect if container uses the new packed format (chunk_*.bin entries)."""
    return any(_PACKED_CHUNK_PATTERN.match(n) for n in entry_names)


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


def _detect_container(filepath: Path) -> str:
    """Detect container type from magic bytes. Returns 'fasta' or 'fastq'."""
    with open(filepath, "rb") as f:
        magic = f.read(8)
    if magic == _ZLFASTA_MAGIC:
        return "fasta"
    if magic == _ZLFASTQ_MAGIC:
        return "fastq"
    raise click.ClickException(
        f"Unrecognized container format in {filepath.name}. "
        f"Expected .zlfasta or .zlfastq magic bytes."
    )


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


@click.command("decompress-lossless")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output path (default: strip .zlfasta/.zlfastq extension).",
)
@click.option("-v", "--verbose", is_flag=True, help="Print subprocess commands.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output file.")
def decompress_lossless_cmd(input_file, output, verbose, force):
    """Decompress a .zlfasta or .zlfastq container back to the original file.

    Auto-detects the container type from magic bytes. The output is
    byte-identical to the original input file.

    \b
    Example:
      nyx decompress-lossless genome.fasta.zlfasta
      nyx decompress-lossless reads.fastq.zlfastq -o reads.fastq
    """
    input_path = Path(input_file).resolve()

    # Detect container type
    fmt = _detect_container(input_path)
    is_fastq = (fmt == "fastq")
    compressible = _FASTQ_COMPRESSIBLE if is_fastq else _FASTA_COMPRESSIBLE
    container_ext = ".zlfastq" if is_fastq else ".zlfasta"
    format_label = "FASTQ" if is_fastq else "FASTA"

    if output is None:
        name = input_path.name
        if name.endswith(container_ext):
            output_name = name[: -len(container_ext)]
        else:
            output_name = name + (".fastq" if is_fastq else ".fasta")
        output_path = input_path.parent / output_name
    else:
        output_path = Path(output).resolve()

    if output_path.exists() and not force:
        raise click.ClickException(
            f"Output already exists: {output_path}\n"
            f"Use -f/--force to overwrite."
        )

    click.echo(f"Input:  {input_path.name} ({format_label} container)")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_dec_")

    try:
        extract_dir = Path(tmpdir) / "extracted"
        decomp_dir = Path(tmpdir) / "decompressed"
        decomp_dir.mkdir()
        streams_dir = Path(tmpdir) / "streams"
        streams_dir.mkdir()

        # Step 1: Extract container
        click.echo(f"  [1/3] Extracting {container_ext} container...")
        if is_fastq:
            entries = extract_zlfastq(input_path, extract_dir)
        else:
            entries = extract_zlfasta(input_path, extract_dir)

        if verbose:
            for name, path in sorted(entries.items()):
                click.echo(f"    {name}: {path.stat().st_size:,} bytes")

        # Detect packed NXF2 format (new FASTA path) vs old multi-stream format
        use_packed = (not is_fastq) and _is_packed_format(entries.keys())

        if use_packed:
            # --- New packed FASTA decompression path ---
            # Step 2: Decompress all chunk entries (parallel)
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
            # Detect nucleotide vs protein by reading magic from first chunk
            first_chunk = sorted(streams_dir.glob("chunk_*.bin"))[0]
            packed_subtype = _detect_packed_subtype(first_chunk)

            if packed_subtype == "protein":
                click.echo(f"  [3/3] Reconstructing protein FASTA from packed chunks...")
                codec.decode_protein_packed(streams_dir, output_path, verbose=verbose)
            else:
                click.echo(f"  [3/3] Reconstructing FASTA from packed chunks...")
                codec.decode_packed(streams_dir, output_path, verbose=verbose)
        else:
            # --- Old multi-stream decompression path (FASTA legacy + FASTQ) ---
            # Step 2: Decompress streams (parallel)
            click.echo("  [2/3] Decompressing streams with OpenZL...")

            decompress_tasks = []
            copy_tasks = []
            for name, extracted_path in sorted(entries.items()):
                if _is_compressible(name, compressible, is_fastq=is_fastq) and extracted_path.stat().st_size > 0:
                    decompress_tasks.append((name, extracted_path, decomp_dir / name))
                else:
                    copy_tasks.append((extracted_path, decomp_dir / name))

            # Copy non-compressed entries directly
            for src, dst in copy_tasks:
                shutil.copy2(src, dst)

            # Decompress in parallel
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
                        fut.result()  # raises on error

            # Step 2b: Reassemble chunked streams
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

            # Step 3: Decode streams back to original format
            click.echo(f"  [3/3] Reconstructing {format_label}...")
            if is_fastq:
                fastq_codec.decode(streams_dir, output_path, verbose=verbose)
            else:
                codec.decode(streams_dir, output_path, verbose=verbose)

        elapsed = time.time() - t0
        output_size = output_path.stat().st_size

        click.echo(
            f"\nOutput: {output_path.name} ({output_size:,} bytes)\n"
            f"Time:   {elapsed:.1f}s"
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
