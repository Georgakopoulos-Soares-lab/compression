"""nyx decompress — extract and decompress a .nyx archive."""

import shutil
import tempfile
from pathlib import Path

import click

from ..core import openzl, archive


@click.command("decompress")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_dir", type=click.Path(),
              default=None,
              help="Output directory for decompressed chunks (default: <input>_decompressed/).")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output.")
@click.option("--keep-temp", is_flag=True, help="Keep temporary directories.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
def decompress_cmd(input_file, output_dir, force, keep_temp, verbose):
    """Decompress a .nyx archive.

    Extracts and decompresses all chunks from the archive.
    Note: postprocessing (binary chunks -> original text format) is not yet
    implemented. Output will be decompressed binary chunks.
    """
    input_path = Path(input_file).resolve()

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
