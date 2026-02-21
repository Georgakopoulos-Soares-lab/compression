"""Resolve paths to binaries and resources within the nyx package."""

import os
import shutil
from pathlib import Path

_BUILD_HINT = "Run 'nyx build' to compile from source."


def _nyx_root() -> Path:
    """Return the nyx/ package root (where pyproject.toml, schemas/, etc. live)."""
    return Path(__file__).resolve().parents[2]


def find_zli() -> Path:
    """Locate the openzl/zli binary.

    Search order:
      1. NYX_ZLI environment variable
      2. <nyx_root>/openzl/zli  (built by 'nyx build')
      3. ``zli`` on PATH
    """
    env = os.environ.get("NYX_ZLI")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(f"NYX_ZLI points to missing file: {env}")

    local = _nyx_root() / "openzl" / "zli"
    if local.is_file():
        return local

    on_path = shutil.which("zli")
    if on_path:
        return Path(on_path)

    raise FileNotFoundError(
        f"Cannot find zli binary. {_BUILD_HINT} "
        f"Or set NYX_ZLI environment variable."
    )


def find_preprocessor() -> Path:
    """Locate the genomic_preprocessor binary.

    Search order:
      1. NYX_PREPROCESSOR environment variable
      2. <nyx_root>/bin/genomic_preprocessor  (built by 'nyx build')
      3. ``genomic_preprocessor`` on PATH
    """
    env = os.environ.get("NYX_PREPROCESSOR")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"NYX_PREPROCESSOR points to missing file: {env}"
        )

    local = _nyx_root() / "bin" / "genomic_preprocessor"
    if local.is_file():
        return local

    on_path = shutil.which("genomic_preprocessor")
    if on_path:
        return Path(on_path)

    raise FileNotFoundError(
        f"Cannot find genomic_preprocessor binary. {_BUILD_HINT} "
        f"Or set NYX_PREPROCESSOR environment variable."
    )


def find_schema(name: str) -> Path:
    """Locate an SDDL schema file by name (e.g. 'fasta_packed.sddl').

    Search order:
      1. Absolute / relative path (if ``name`` exists as-is)
      2. <nyx_root>/schemas/<name>
    """
    p = Path(name)
    if p.is_file():
        return p.resolve()

    local = _nyx_root() / "schemas" / name
    if local.is_file():
        return local

    raise FileNotFoundError(f"Schema not found: {name}")


def find_fasta_codec() -> Path:
    """Locate the fasta_codec binary.

    Search order:
      1. NYX_FASTA_CODEC environment variable
      2. <nyx_root>/bin/fasta_codec  (built by 'nyx build')
      3. ``fasta_codec`` on PATH
    """
    env = os.environ.get("NYX_FASTA_CODEC")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"NYX_FASTA_CODEC points to missing file: {env}"
        )

    local = _nyx_root() / "bin" / "fasta_codec"
    if local.is_file():
        return local

    on_path = shutil.which("fasta_codec")
    if on_path:
        return Path(on_path)

    raise FileNotFoundError(
        f"Cannot find fasta_codec binary. {_BUILD_HINT} "
        f"Or set NYX_FASTA_CODEC environment variable."
    )


def find_fastq_codec() -> Path:
    """Locate the fastq_codec binary.

    Search order:
      1. NYX_FASTQ_CODEC environment variable
      2. <nyx_root>/bin/fastq_codec  (built by 'nyx build')
      3. ``fastq_codec`` on PATH
    """
    env = os.environ.get("NYX_FASTQ_CODEC")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(
            f"NYX_FASTQ_CODEC points to missing file: {env}"
        )

    local = _nyx_root() / "bin" / "fastq_codec"
    if local.is_file():
        return local

    on_path = shutil.which("fastq_codec")
    if on_path:
        return Path(on_path)

    raise FileNotFoundError(
        f"Cannot find fastq_codec binary. {_BUILD_HINT} "
        f"Or set NYX_FASTQ_CODEC environment variable."
    )


def find_make_train_sample() -> Path:
    """Locate the make_train_sample.py script."""
    p = _nyx_root() / "scripts" / "make_train_sample.py"
    if p.is_file():
        return p
    raise FileNotFoundError(f"make_train_sample.py not found at {p}")
