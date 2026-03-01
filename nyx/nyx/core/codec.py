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


def encode_packed(
    input_file: Path,
    output_dir: Path,
    num_chunks: int = 1,
    verbose: bool = False,
) -> List[Path]:
    """Run fasta_codec encode-packed to produce NXF2 packed binary chunks.

    Args:
        input_file: Path to input FASTA file.
        output_dir: Directory where chunk_*.bin files will be written.
        num_chunks: Number of chunks to split into (default 1).
        verbose: Print the command being run.

    Returns:
        Sorted list of paths to the generated chunk files.
    """
    codec = find_fasta_codec()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(codec), "encode-packed", str(input_file), str(output_dir), str(num_chunks)]

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
            f"fasta_codec encode-packed exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    chunks = sorted(output_dir.glob("chunk_*.bin"))
    if not chunks:
        raise FastaCodecError(
            f"fasta_codec encode-packed produced no chunk files in {output_dir}"
        )

    return chunks


def decode_packed(
    packed_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fasta_codec decode-packed to reconstruct a FASTA from NXF2 chunks.

    Args:
        packed_dir: Directory containing chunk_*.bin files.
        output_file: Path to write the reconstructed FASTA.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec = find_fasta_codec()

    cmd = [str(codec), "decode-packed", str(packed_dir), str(output_file)]

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
            f"fasta_codec decode-packed exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    return output_file


def encode_protein_packed(
    input_file: Path,
    output_dir: Path,
    num_chunks: int = 1,
    verbose: bool = False,
) -> List[Path]:
    """Run fasta_codec encode-protein-packed to produce NXFP packed binary chunks.

    Args:
        input_file: Path to input protein FASTA file.
        output_dir: Directory where chunk_*.bin files will be written.
        num_chunks: Number of chunks to split into (default 1).
        verbose: Print the command being run.

    Returns:
        Sorted list of paths to the generated chunk files.
    """
    codec_bin = find_fasta_codec()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(codec_bin), "encode-protein-packed", str(input_file), str(output_dir), str(num_chunks)]

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
            f"fasta_codec encode-protein-packed exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    chunks = sorted(output_dir.glob("chunk_*.bin"))
    if not chunks:
        raise FastaCodecError(
            f"fasta_codec encode-protein-packed produced no chunk files in {output_dir}"
        )

    return chunks


def decode_protein_packed(
    packed_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fasta_codec decode-protein-packed to reconstruct a protein FASTA from NXFP chunks.

    Args:
        packed_dir: Directory containing chunk_*.bin files.
        output_file: Path to write the reconstructed FASTA.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec_bin = find_fasta_codec()

    cmd = [str(codec_bin), "decode-protein-packed", str(packed_dir), str(output_file)]

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
            f"fasta_codec decode-protein-packed exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    return output_file
