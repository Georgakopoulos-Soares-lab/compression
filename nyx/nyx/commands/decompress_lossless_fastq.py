"""nyx decompress-lossless-fastq — Lossless FASTQ decompression from .zlfastq container."""

import concurrent.futures
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import fastq_codec, openzl
from ..core.zlfastq import extract_zlfastq

# Streams that were compressed (need decompression)
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

# Pattern matching chunked stream names like "bases2.bin.000"
_CHUNK_PATTERN = re.compile(r"^(.+\.bin)\.(\d{3})$")

# Pattern for per-position quality files (v3 layout=2)
_QUALITY_POS_PATTERN = re.compile(r"^quality_pos_\d{4}\.bin$")


def _is_compressible(name: str) -> bool:
    """Check if an entry name is a compressed stream (or chunk of one)."""
    if name in _COMPRESSIBLE_STREAMS:
        return True
    m = _CHUNK_PATTERN.match(name)
    if m and m.group(1) in _COMPRESSIBLE_STREAMS:
        return True
    # Per-position quality files and their chunks
    if _QUALITY_POS_PATTERN.match(name):
        return True
    if m and _QUALITY_POS_PATTERN.match(m.group(1)):
        return True
    return False


# Pattern for packed NQF chunk files (chunk_000000.bin, chunk_000001.bin, etc.)
_PACKED_CHUNK_PATTERN = re.compile(r"^chunk_\d{6}\.bin$")


def _is_packed_format(entry_names):
    """Detect if the archive uses packed NQF format."""
    return any(_PACKED_CHUNK_PATTERN.match(n) for n in entry_names)


# Pattern for CSV/TSV part files
_CSV_PART_PATTERN = re.compile(r"^part_\d{3}\.tsv$")

# Sidecar files that are stored raw (not compressed) in CSV archives
_CSV_SIDECAR_FILES = {"meta.bin", "plus.bin", "wrap.bin"}


def _is_csv_format(entry_names):
    """Detect if the archive uses CSV/TSV format."""
    names = set(entry_names)
    return (any(_CSV_PART_PATTERN.match(n) for n in names)
            and "meta.bin" in names)


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


@click.command("decompress-lossless-fastq")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "-o", "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output FASTQ path (default: strip .zlfastq extension).",
)
@click.option("-v", "--verbose", is_flag=True, help="Print subprocess commands.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output file.")
def decompress_lossless_fastq_cmd(input_file, output, verbose, force):
    """Decompress a .zlfastq container back to the original FASTQ.

    The output is byte-identical to the original input file. Streams are
    decompressed in parallel for performance. Supports v1, v2, and v3
    encoded archives, including Illumina dictionary-encoded headers.

    \b
    Example:
      nyx decompress-lossless-fastq reads.fastq.zlfastq
      nyx decompress-lossless-fastq compressed.zlfastq -o reads.fastq
    """
    input_path = Path(input_file).resolve()
    if output is None:
        name = input_path.name
        if name.endswith(".zlfastq"):
            output_name = name[: -len(".zlfastq")]
        else:
            output_name = name + ".fastq"
        output_path = input_path.parent / output_name
    else:
        output_path = Path(output).resolve()

    if output_path.exists() and not force:
        raise click.ClickException(
            f"Output already exists: {output_path}\n"
            f"Use -f/--force to overwrite."
        )

    click.echo(f"Input: {input_path.name}")

    t0 = time.time()
    tmpdir = tempfile.mkdtemp(prefix="nyx_lossless_fq_dec_")

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

            # Copy sidecar files raw
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
                for name in sorted(csv_dir.iterdir()):
                    click.echo(f"    {name.name}: {name.stat().st_size:,} bytes")

            # Step 3: Decode CSV back to FASTQ
            click.echo("  [3/3] Reconstructing FASTQ from CSV/TSV...")
            fastq_codec.decode_csv(csv_dir, output_path, verbose=verbose)

        elif _is_packed_format(entries.keys()):
            # --- Packed NQF path ---
            click.echo("    Detected packed NQF format")

            # Step 2: Decompress packed chunks (parallel)
            click.echo("  [2/3] Decompressing NQF chunks with OpenZL...")
            packed_dir = Path(tmpdir) / "packed_chunks"
            packed_dir.mkdir()

            chunk_entries = {n: p for n, p in entries.items()
                            if _PACKED_CHUNK_PATTERN.match(n)}

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

            # Step 2: Decompress streams (parallel)
            click.echo("  [2/3] Decompressing streams with OpenZL...")

            decompress_tasks = []
            copy_tasks = []
            for name, extracted_path in sorted(entries.items()):
                if _is_compressible(name) and extracted_path.stat().st_size > 0:
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
