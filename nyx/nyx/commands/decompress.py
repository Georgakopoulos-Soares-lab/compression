"""nyx decompress — unified decompression command for all container formats."""

import shutil
import tempfile
from pathlib import Path

import click

from ..core import openzl, archive
from ._lossless import (
    decompress_lossless_fasta,
    decompress_lossless_fastq,
    decompress_lossless_vcf,
    ZLFASTA_MAGIC,
    ZLFASTQ_MAGIC,
    ZLVCF_MAGIC,
)


@click.command("decompress")
@click.argument("input_file", type=click.Path(exists=True, dir_okay=False))
@click.option("-o", "--output", "output_file", type=click.Path(),
              default=None, help="Output file/directory path (auto-determined).")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output.")
@click.option("--keep-temp", is_flag=True, help="Keep temporary directories.")
def decompress_cmd(input_file, output_file, verbose, force, keep_temp):
    """Decompress a Nyx archive (.zlfasta, .zlfastq, .zlvcf, or .nyx).

    Auto-detects the container format from magic bytes and routes to
    the appropriate decompression pipeline. Lossless containers produce
    byte-identical reconstructions of the original file.

    \b
    Supported formats (auto-detected from magic bytes):
      .zlfasta   Lossless FASTA container → byte-identical .fasta
      .zlfastq   Lossless FASTQ container → byte-identical .fastq
      .zlvcf     Lossless VCF container   → byte-identical .vcf
      .nyx       Tar-based archive        → decompressed chunks directory

    \b
    Examples:
      nyx decompress genome.fasta.zlfasta                # → genome.fasta
      nyx decompress reads.fastq.zlfastq -o reads.fastq  # → reads.fastq
      nyx decompress variants.vcf.zlvcf                  # → variants.vcf
      nyx decompress data.nyx                            # → data_decompressed/
    """
    input_path = Path(input_file).resolve()

    # Read first 8 bytes to detect container format
    with open(input_path, "rb") as f:
        magic = f.read(8)

    if magic == ZLFASTA_MAGIC:
        # --- Lossless FASTA ---
        if output_file is None:
            name = input_path.name
            if name.endswith(".zlfasta"):
                output_name = name[: -len(".zlfasta")]
            else:
                output_name = name + ".fasta"
            output_path = input_path.parent / output_name
        else:
            output_path = Path(output_file).resolve()

        if output_path.exists() and not force:
            raise click.ClickException(
                f"Output already exists: {output_path}\n"
                f"Use -f/--force to overwrite."
            )

        click.echo(f"Input: {input_path.name} (FASTA lossless container)")
        decompress_lossless_fasta(input_path, output_path, verbose=verbose)

    elif magic == ZLFASTQ_MAGIC:
        # --- Lossless FASTQ ---
        if output_file is None:
            name = input_path.name
            if name.endswith(".zlfastq"):
                output_name = name[: -len(".zlfastq")]
            else:
                output_name = name + ".fastq"
            output_path = input_path.parent / output_name
        else:
            output_path = Path(output_file).resolve()

        if output_path.exists() and not force:
            raise click.ClickException(
                f"Output already exists: {output_path}\n"
                f"Use -f/--force to overwrite."
            )

        click.echo(f"Input: {input_path.name} (FASTQ lossless container)")
        decompress_lossless_fastq(input_path, output_path, verbose=verbose)

    elif magic == ZLVCF_MAGIC:
        # --- Lossless VCF ---
        if output_file is None:
            name = input_path.name
            if name.endswith(".zlvcf"):
                output_name = name[: -len(".zlvcf")]
            else:
                output_name = name + ".vcf"
            output_path = input_path.parent / output_name
        else:
            output_path = Path(output_file).resolve()

        if output_path.exists() and not force:
            raise click.ClickException(
                f"Output already exists: {output_path}\n"
                f"Use -f/--force to overwrite."
            )

        click.echo(f"Input: {input_path.name} (VCF lossless container)")
        decompress_lossless_vcf(input_path, output_path, verbose=verbose)

    else:
        # --- .nyx archive (tar-based) ---
        if output_file is None:
            output_dir = str(input_path).removesuffix(".nyx") + "_decompressed"
            output_path = Path(output_dir).resolve()
        else:
            output_path = Path(output_file).resolve()

        if output_path.exists() and not force:
            raise click.UsageError(
                f"Output exists: {output_path}. Use -f/--force to overwrite."
            )

        if output_path.exists() and force:
            shutil.rmtree(output_path)

        tmpdir = Path(tempfile.mkdtemp(prefix="nyx_dec_"))
        try:
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

            compressed_chunks = archive.get_chunks_from_extract(tmpdir)
            compressor = archive.get_compressor_from_extract(tmpdir)

            if not compressed_chunks:
                raise click.ClickException("No chunks found in archive.")

            output_path.mkdir(parents=True, exist_ok=True)
            click.echo(f"Decompressing {len(compressed_chunks)} chunk(s)...")

            for chunk in compressed_chunks:
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
