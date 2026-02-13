"""nyx build — compile OpenZL and genomic_preprocessor from source."""

import subprocess
import sys
from pathlib import Path

import click


def _nyx_root() -> Path:
    """Return the nyx/ package root (where pyproject.toml lives)."""
    return Path(__file__).resolve().parents[2]


@click.command("build")
@click.option("-j", "--jobs", type=int, default=None,
              help="Parallel build jobs (default: auto-detect CPU count).")
@click.option("-v", "--verbose", is_flag=True, help="Verbose build output.")
def build_cmd(jobs, verbose):
    """Build OpenZL and the genomic preprocessor from source.

    This clones the pinned OpenZL commit, compiles it, and builds the
    genomic preprocessor. All artifacts are placed inside the nyx/ directory.

    Run this once after installing nyx:

    \b
      pip install -e ./nyx
      nyx build
    """
    nyx_root = _nyx_root()
    build_script = nyx_root / "scripts" / "build.sh"

    if not build_script.is_file():
        raise click.ClickException(f"Build script not found: {build_script}")

    env = {}
    if jobs:
        env["JOBS"] = str(jobs)

    click.echo("Building OpenZL and genomic_preprocessor...")
    click.echo(f"  Source: {nyx_root}")
    click.echo("")

    result = subprocess.run(
        ["bash", str(build_script)],
        env={**__import__("os").environ, **env},
        cwd=str(nyx_root),
    )

    if result.returncode != 0:
        raise click.ClickException(
            f"Build failed with exit code {result.returncode}. "
            f"Check the output above for errors."
        )

    click.echo("")
    click.echo("Build successful. You can now use nyx compress/decompress.")
