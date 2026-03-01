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


def encode_packed(
    input_file: Path,
    output_dir: Path,
    num_chunks: int = 1,
    verbose: bool = False,
) -> List[Path]:
    """Run fastq_codec encode-packed to produce packed binary chunks.

    Args:
        input_file: Path to input FASTQ file.
        output_dir: Directory where chunk_*.bin files will be written.
        num_chunks: Number of chunks to split into (default 1).
        verbose: Print the command being run.

    Returns:
        Sorted list of paths to the generated chunk_*.bin files.
    """
    codec = find_fastq_codec()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(codec), "encode-packed", str(input_file), str(output_dir)]
    if num_chunks > 1:
        cmd.append(str(num_chunks))

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    # Stream stderr so progress is visible for large files
    if verbose:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,  # inherit — visible in terminal
            text=True,
        )
        proc.wait()
        if proc.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec encode-packed exited with code {proc.returncode}\n"
                f"Command: {' '.join(cmd)}"
            )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec encode-packed exited with code {result.returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stderr: {result.stderr.strip()}"
            )

    chunks = sorted(output_dir.glob("chunk_*.bin"))
    if not chunks:
        raise FastqCodecError(
            f"fastq_codec produced no chunk files in {output_dir}"
        )

    return chunks


def decode_packed(
    packed_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fastq_codec decode-packed to reconstruct a FASTQ from packed chunks.

    Args:
        packed_dir: Directory containing chunk_*.bin files.
        output_file: Path to write the reconstructed FASTQ.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec = find_fastq_codec()

    cmd = [str(codec), "decode-packed", str(packed_dir), str(output_file)]

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    if verbose:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
        proc.wait()
        if proc.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec decode-packed exited with code {proc.returncode}\n"
                f"Command: {' '.join(cmd)}"
            )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec decode-packed exited with code {result.returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stderr: {result.stderr.strip()}"
            )

    return output_file


def encode_csv(
    input_file: Path,
    output_dir: Path,
    num_parts: int = 1,
    verbose: bool = False,
) -> List[Path]:
    """Run fastq_codec encode-csv to produce TSV parts + meta.bin.

    Args:
        input_file: Path to input FASTQ file.
        output_dir: Directory where part_*.tsv and meta.bin will be written.
        num_parts: Number of TSV parts to split into (default 1).
        verbose: Print the command being run.

    Returns:
        Sorted list of paths to all generated files (meta.bin, part_*.tsv, etc.).
    """
    codec = find_fastq_codec()
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [str(codec), "encode-csv", str(input_file), str(output_dir)]
    if num_parts > 1:
        cmd.append(str(num_parts))

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    if verbose:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
        proc.wait()
        if proc.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec encode-csv exited with code {proc.returncode}\n"
                f"Command: {' '.join(cmd)}"
            )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec encode-csv exited with code {result.returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stderr: {result.stderr.strip()}"
            )

    outputs = sorted(output_dir.iterdir())
    if not outputs:
        raise FastqCodecError(
            f"fastq_codec produced no files in {output_dir}"
        )

    return outputs


def decode_csv(
    csv_dir: Path,
    output_file: Path,
    verbose: bool = False,
) -> Path:
    """Run fastq_codec decode-csv to reconstruct a FASTQ from TSV parts.

    Args:
        csv_dir: Directory containing meta.bin, part_*.tsv, and optional sidecars.
        output_file: Path to write the reconstructed FASTQ.
        verbose: Print the command being run.

    Returns:
        Path to the reconstructed file.
    """
    codec = find_fastq_codec()

    cmd = [str(codec), "decode-csv", str(csv_dir), str(output_file)]

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    if verbose:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
        )
        proc.wait()
        if proc.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec decode-csv exited with code {proc.returncode}\n"
                f"Command: {' '.join(cmd)}"
            )
    else:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise FastqCodecError(
                f"fastq_codec decode-csv exited with code {result.returncode}\n"
                f"Command: {' '.join(cmd)}\n"
                f"Stderr: {result.stderr.strip()}"
            )

    return output_file
