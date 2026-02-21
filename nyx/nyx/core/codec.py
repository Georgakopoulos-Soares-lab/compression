"""Subprocess wrapper for the fasta_codec binary (lossless FASTA encoder/decoder)."""

import subprocess
from pathlib import Path
from typing import List

import click

from ..utils.paths import find_fasta_codec


class FastaCodecError(Exception):
    """Raised when the fasta_codec subprocess fails."""


def encode(
    input_file: Path,
    output_dir: Path,
    verbose: bool = False,
    threads: int = 1,
) -> List[Path]:
    """Run fasta_codec encode to split a FASTA into compressed streams.

    Args:
        input_file: Path to input FASTA file.
        output_dir: Directory where stream files will be written.
        verbose: Print the command being run.
        threads: Number of encoding threads (default 1).

    Returns:
        Sorted list of paths to the generated stream files.
    """
    codec = find_fasta_codec()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(codec), "encode", str(input_file), str(output_dir)]
    if threads > 1:
        cmd.append(str(threads))

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise FastaCodecError(
            f"fasta_codec encode exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    streams = sorted(output_dir.glob("*.bin"))
    if not streams:
        raise FastaCodecError(
            f"fasta_codec produced no stream files in {output_dir}"
        )

    return streams


def decode(
    streams_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fasta_codec decode to reconstruct a FASTA from streams.

    Args:
        streams_dir: Directory containing stream files.
        output_file: Path to write the reconstructed FASTA.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec = find_fasta_codec()

    cmd = [str(codec), "decode", str(streams_dir), str(output_file)]

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise FastaCodecError(
            f"fasta_codec decode exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    return output_file
