"""Subprocess wrapper for the genomic_preprocessor binary."""

import subprocess
from pathlib import Path

import click

from ..utils.paths import find_preprocessor


class PreprocessorError(Exception):
    """Raised when the genomic_preprocessor subprocess fails."""


def preprocess(
    input_file: Path,
    output_dir: Path,
    threads: int = 1,
    filetype: str | None = None,
    verbose: bool = False,
) -> list[Path]:
    """Run the genomic preprocessor to convert a text file into binary chunks.

    Args:
        input_file: Path to input genomic file (FASTA/FASTQ/VCF).
        output_dir: Directory where chunk files will be written.
        threads: Number of worker threads (affects chunking).
        filetype: Explicit type override (fasta_packed, fastq_v4, vcf, etc.).
                  If None, the preprocessor auto-detects.
        verbose: Print the command being run.

    Returns:
        Sorted list of paths to the generated chunk files.
    """
    preprocessor = find_preprocessor()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(preprocessor),
        str(input_file),
        str(output_dir),
        str(threads),
    ]

    if filetype:
        cmd.append(filetype)

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise PreprocessorError(
            f"genomic_preprocessor exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    # Collect generated chunk files (sorted by name for deterministic order)
    chunks = sorted(output_dir.glob("chunk_*.*"))
    if not chunks:
        raise PreprocessorError(
            f"Preprocessor produced no chunks in {output_dir}"
        )

    return chunks
