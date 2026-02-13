"""Training sample creation for genomic files."""

import subprocess
import sys
from pathlib import Path

import click

from ..utils.paths import find_make_train_sample


class SampleError(Exception):
    """Raised when training sample creation fails."""


def create_training_sample(
    input_file: Path,
    output_file: Path,
    target_mib: int = 200,
    verbose: bool = False,
) -> Path:
    """Create a record-safe training sample from a FASTA file.

    Uses the existing make_train_sample.py script which copies whole
    FASTA records until reaching the target size.

    Args:
        input_file: Path to full input FASTA file.
        output_file: Path for the training sample output.
        target_mib: Target sample size in MiB.
        verbose: Print the command being run.

    Returns:
        Path to the created sample file.
    """
    script = find_make_train_sample()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(script),
        "--in", str(input_file),
        "--out", str(output_file),
        "--target-mib", str(target_mib),
    ]

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise SampleError(
            f"make_train_sample.py failed with code {result.returncode}\n"
            f"Stderr: {result.stderr.strip()}"
        )

    if not output_file.is_file():
        raise SampleError(f"Training sample was not created at {output_file}")

    return output_file
