"""nyx train — direct passthrough to zli train with all flags."""

import click

from ..core import openzl


@click.command("train", context_settings={"ignore_unknown_options": True,
                                           "allow_extra_args": True})
@click.argument("sample_dir", type=click.Path(exists=True))
@click.option("-o", "--output", required=True, type=click.Path(),
              help="Output path for trained compressor.")
@click.option("-p", "--profile", default=None, help="Profile name (e.g. sddl, serial).")
@click.option("--profile-arg", default=None, help="Argument for profile (e.g. schema path).")
@click.option("-c", "--compressor", default=None, type=click.Path(exists=True),
              help="Existing compressor to retrain.")
@click.option("--threads", type=int, default=None, help="Number of threads.")
@click.option("--max-time-secs", type=int, default=None, help="Training time limit (seconds).")
@click.option("--use-all-samples", is_flag=True, help="Use all samples ignoring size limits.")
@click.option("--no-ace-successors", is_flag=True, help="Disable ACE successor graphs.")
@click.option("--no-clustering", is_flag=True, help="Skip clustering during training.")
@click.option("--trainer", type=click.Choice(["greedy", "full-split", "bottom-up"]),
              default=None, help="Training algorithm (default: greedy).")
@click.option("-f", "--force", is_flag=True, help="Overwrite output.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.pass_context
def train_cmd(ctx, sample_dir, output, profile, profile_arg, compressor,
              threads, max_time_secs, use_all_samples, no_ace_successors,
              no_clustering, trainer, force, verbose):
    """Train a compressor on sample data.

    Passes through to openzl's train command. Any additional flags not listed
    here are forwarded directly to zli.
    """
    from pathlib import Path

    extra_args = ctx.args  # Captures unknown flags for passthrough

    openzl.train(
        sample_dir=Path(sample_dir),
        output_file=Path(output),
        profile=profile,
        profile_arg=profile_arg,
        compressor=Path(compressor) if compressor else None,
        threads=threads,
        max_time_secs=max_time_secs,
        use_all_samples=use_all_samples,
        no_ace_successors=no_ace_successors,
        no_clustering=no_clustering,
        trainer=trainer,
        force=force,
        verbose=verbose,
        extra_args=extra_args,
    )

    click.echo(f"Compressor saved to: {output}")
