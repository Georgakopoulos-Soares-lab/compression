"""Subprocess wrapper for the fastq_codec binary (lossless FASTQ encoder/decoder)."""

import subprocess
from pathlib import Path
from typing import List

import click

from ..utils.paths import find_fastq_codec


class FastqCodecError(Exception):
    """Raised when the fastq_codec subprocess fails."""


def encode(
    input_file: Path,
    output_dir: Path,
    verbose: bool = False,
    threads: int = 1,
) -> List[Path]:
    """Run fastq_codec encode to split a FASTQ into compressed streams.

    Args:
        input_file: Path to input FASTQ file.
        output_dir: Directory where stream files will be written.
        verbose: Print the command being run.
        threads: Number of encoding threads (default 1).

    Returns:
        Sorted list of paths to the generated stream files.
    """
    codec = find_fastq_codec()
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
        raise FastqCodecError(
            f"fastq_codec encode exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    streams = sorted(output_dir.glob("*.bin"))
    if not streams:
        raise FastqCodecError(
            f"fastq_codec produced no stream files in {output_dir}"
        )

    return streams


def decode(
    streams_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fastq_codec decode to reconstruct a FASTQ from streams.

    Args:
        streams_dir: Directory containing stream files.
        output_file: Path to write the reconstructed FASTQ.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec = find_fastq_codec()

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
        raise FastqCodecError(
            f"fastq_codec decode exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    return output_file
