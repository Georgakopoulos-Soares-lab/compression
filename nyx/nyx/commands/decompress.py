"""nyx decompress — extract and decompress a .nyx archive or OpenZL Parquet file."""

import shutil
import tempfile
import time
from pathlib import Path

import click

from ..core import openzl, archive
from ..core.detect import is_parquet


@click.command("decompress")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_dir", type=click.Path(),
              default=None,
              help="Output path (directory for .nyx, file for .parquet).")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output.")
@click.option("--columns", type=str, default=None,
              help="Comma-separated column names to decompress (Parquet only).")
@click.option("--keep-temp", is_flag=True, help="Keep temporary directories.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
def decompress_cmd(input_file, output_dir, force, columns, keep_temp, verbose):
    """Decompress a .nyx archive or OpenZL-compressed Parquet file.

    For Parquet files, reconstructs a standard Parquet file.
    Use --columns to decompress only specific columns.
    """
    input_path = Path(input_file).resolve()

    if is_parquet(input_path):
        _decompress_parquet(input_path, output_dir, force, columns, verbose)
        return

    # Default output directory
    if output_dir is None:
        output_dir = str(input_path).removesuffix(".nyx") + "_decompressed"
    output_path = Path(output_dir).resolve()

    if output_path.exists() and not force:
        raise click.UsageError(
            f"Output directory exists: {output_path}. Use -f/--force to overwrite."
        )

    if output_path.exists() and force:
        shutil.rmtree(output_path)

    tmpdir = Path(tempfile.mkdtemp(prefix="nyx_dec_"))

    try:
        # Extract archive
        click.echo(f"Extracting {input_path.name}...")
        manifest = archive.extract_archive(input_path, tmpdir)

        mode = manifest.get("mode", "unknown")
        filetype = manifest.get("filetype", "unknown")
        chunk_count = manifest.get("chunk_count", 0)
        original = manifest.get("original_filename", "unknown")

        click.echo(
            f"  Original: {original} | Type: {filetype} | "
            f"Mode: {mode} | Chunks: {chunk_count}"
        )

        # Get compressed chunks and compressor
        compressed_chunks = archive.get_chunks_from_extract(tmpdir)
        compressor = archive.get_compressor_from_extract(tmpdir)

        if not compressed_chunks:
            raise click.ClickException("No chunks found in archive.")

        # Decompress each chunk
        output_path.mkdir(parents=True, exist_ok=True)
        click.echo(f"Decompressing {len(compressed_chunks)} chunk(s)...")

        for chunk in compressed_chunks:
            # Strip .zl extension for output name
            out_name = chunk.name
            if out_name.endswith(".zl"):
                out_name = out_name[:-3]

            out_file = output_path / out_name
            openzl.decompress(chunk, out_file, force=force, verbose=verbose)

        click.echo(f"Done. Output in: {output_path}")

        if filetype in ("fasta", "fastq", "vcf"):
            click.echo(
                "  Note: Output is in binary chunk format. "
                "Postprocessing to reconstruct the original text format "
                "is not yet implemented."
            )

    finally:
        if not keep_temp:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            click.echo(f"Temp directory kept: {tmpdir}")


def _decompress_parquet(input_path, output_path, force, columns_str, verbose):
    """Decompress an OpenZL Parquet file to a standard Parquet file."""
    from ..core.parquet_pipeline import decompress_parquet
    from ..core.parquet_reader import is_openzl_parquet
    from ..utils.paths import find_zli

    if not is_openzl_parquet(input_path):
        raise click.ClickException(
            "This Parquet file is not OpenZL-compressed (missing openzl:version metadata)."
        )

    if output_path is None:
        name = input_path.name
        if name.endswith(".ozl.parquet"):
            output_path = input_path.parent / name.replace(".ozl.parquet", "_restored.parquet")
        else:
            output_path = input_path.parent / (input_path.stem + "_restored.parquet")
    output_path = Path(output_path).resolve()

    if output_path.exists() and not force:
        raise click.UsageError(
            f"Output file exists: {output_path}. Use -f/--force to overwrite."
        )

    columns = None
    if columns_str:
        columns = [c.strip() for c in columns_str.split(",")]

    zli = str(find_zli())

    click.echo(f"Decompressing {input_path.name}...")
    t0 = time.monotonic()

    decompress_parquet(
        input_path, output_path, zli, columns=columns, verbose=verbose,
    )

    elapsed = time.monotonic() - t0

    import os
    click.echo(
        f"Done: {output_path.name} "
        f"({os.path.getsize(output_path):,} bytes) "
        f"in {elapsed:.1f}s"
    )
