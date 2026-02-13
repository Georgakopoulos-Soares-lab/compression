"""Subprocess wrapper for the openzl/zli binary."""

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import click

from ..utils.paths import find_zli


class OpenZLError(Exception):
    """Raised when an openzl/zli subprocess fails."""


def _build_cmd(args: List[str]) -> List[str]:
    """Build the full zli command list."""
    zli = find_zli()
    return [str(zli)] + args


def _run(args: List[str], verbose: bool = False) -> subprocess.CompletedProcess:
    """Run zli with the given arguments."""
    cmd = _build_cmd(args)

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise OpenZLError(
            f"zli exited with code {result.returncode}\n"
            f"Command: {' '.join(cmd)}\n"
            f"Stderr: {stderr}"
        )

    return result


def compress(
    input_file: Path,
    output_file: Path,
    compressor: Optional[Path] = None,
    profile: Optional[str] = None,
    profile_arg: Optional[str] = None,
    train_inline: bool = False,
    force: bool = True,
    verbose: bool = False,
) -> None:
    """Compress a single file using zli."""
    args = ["compress", str(input_file), "--output", str(output_file)]

    if compressor:
        args += ["--compressor", str(compressor)]
    elif profile:
        args += ["--profile", profile]
        if profile_arg:
            args += ["--profile-arg", str(profile_arg)]

    if train_inline:
        args.append("--train-inline")

    if force:
        args.append("--force")

    _run(args, verbose=verbose)


def decompress(
    input_file: Path,
    output_file: Path,
    force: bool = True,
    verbose: bool = False,
) -> None:
    """Decompress a single file using zli."""
    args = [
        "decompress", str(input_file),
        "--output", str(output_file),
    ]
    if force:
        args.append("--force")

    _run(args, verbose=verbose)


def _build_train_args(
    sample_dir: Path,
    output_file: Path,
    profile: Optional[str] = None,
    profile_arg: Optional[str] = None,
    compressor: Optional[Path] = None,
    threads: Optional[int] = None,
    max_time_secs: Optional[int] = None,
    use_all_samples: bool = True,
    no_ace_successors: bool = True,
    no_clustering: bool = False,
    trainer: Optional[str] = None,
    force: bool = True,
    extra_args: Optional[List[str]] = None,
) -> List[str]:
    """Build the argument list for a train command."""
    args = ["train", str(sample_dir), "--output", str(output_file)]

    if profile:
        args += ["--profile", profile]
        if profile_arg:
            args += ["--profile-arg", str(profile_arg)]
    elif compressor:
        args += ["--compressor", str(compressor)]

    if threads is not None:
        args += ["--threads", str(threads)]
    if max_time_secs is not None:
        args += ["--max-time-secs", str(max_time_secs)]
    if use_all_samples:
        args.append("--use-all-samples")
    if no_ace_successors:
        args.append("--no-ace-successors")
    if no_clustering:
        args.append("--no-clustering")
    if trainer:
        args += ["--trainer", trainer]
    if force:
        args.append("--force")
    if extra_args:
        args.extend(extra_args)

    return args


def train(
    sample_dir: Path,
    output_file: Path,
    profile: Optional[str] = None,
    profile_arg: Optional[str] = None,
    compressor: Optional[Path] = None,
    threads: Optional[int] = None,
    max_time_secs: Optional[int] = None,
    use_all_samples: bool = True,
    no_ace_successors: bool = True,
    no_clustering: bool = False,
    trainer: Optional[str] = None,
    force: bool = True,
    verbose: bool = False,
    extra_args: Optional[List[str]] = None,
) -> None:
    """Train a compressor on sample data."""
    args = _build_train_args(
        sample_dir=sample_dir,
        output_file=output_file,
        profile=profile,
        profile_arg=profile_arg,
        compressor=compressor,
        threads=threads,
        max_time_secs=max_time_secs,
        use_all_samples=use_all_samples,
        no_ace_successors=no_ace_successors,
        no_clustering=no_clustering,
        trainer=trainer,
        force=force,
        extra_args=extra_args,
    )

    _run(args, verbose=verbose)


def train_async(
    sample_dir: Path,
    output_file: Path,
    profile: Optional[str] = None,
    profile_arg: Optional[str] = None,
    compressor: Optional[Path] = None,
    threads: Optional[int] = None,
    max_time_secs: Optional[int] = None,
    use_all_samples: bool = True,
    no_ace_successors: bool = True,
    no_clustering: bool = False,
    trainer: Optional[str] = None,
    force: bool = True,
    verbose: bool = False,
    extra_args: Optional[List[str]] = None,
) -> subprocess.Popen:
    """Start training as a background process (non-blocking).

    Returns the Popen object. Caller should poll/wait and handle errors.
    """
    args = _build_train_args(
        sample_dir=sample_dir,
        output_file=output_file,
        profile=profile,
        profile_arg=profile_arg,
        compressor=compressor,
        threads=threads,
        max_time_secs=max_time_secs,
        use_all_samples=use_all_samples,
        no_ace_successors=no_ace_successors,
        no_clustering=no_clustering,
        trainer=trainer,
        force=force,
        extra_args=extra_args,
    )
    cmd = _build_cmd(args)

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def get_train_cmd(
    sample_dir: Path,
    output_file: Path,
    **kwargs,
) -> List[str]:
    """Get the full command list for a train invocation (for display)."""
    args = _build_train_args(sample_dir, output_file, **kwargs)
    return _build_cmd(args)


def benchmark(input_dir: Path, extra_args: Optional[List[str]] = None,
              verbose: bool = False) -> str:
    """Run benchmark and return stdout."""
    args = ["benchmark", str(input_dir)]
    if extra_args:
        args.extend(extra_args)
    result = _run(args, verbose=verbose)
    return result.stdout


def inspect(compressor: Path, extra_args: Optional[List[str]] = None,
            verbose: bool = False) -> str:
    """Inspect a compressor and return stdout (JSON)."""
    args = ["inspect", str(compressor)]
    if extra_args:
        args.extend(extra_args)
    result = _run(args, verbose=verbose)
    return result.stdout


def list_profiles(verbose: bool = False) -> str:
    """List available profiles and return stdout."""
    result = _run(["list-profiles"], verbose=verbose)
    return result.stdout


def passthrough(args: List[str], verbose: bool = False) -> None:
    """Pass arbitrary arguments directly to zli, streaming output."""
    zli = find_zli()
    cmd = [str(zli)] + args

    if verbose:
        click.echo(f"  [nyx] {' '.join(cmd)}")

    result = subprocess.run(cmd)
    sys.exit(result.returncode)
