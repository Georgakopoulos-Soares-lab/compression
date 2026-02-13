"""nyx benchmark — passthrough to zli benchmark."""

import click

from ..core import openzl


@click.command("benchmark", context_settings={"ignore_unknown_options": True,
                                                "allow_extra_args": True})
@click.argument("input_dir", type=click.Path(exists=True))
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.pass_context
def benchmark_cmd(ctx, input_dir, verbose):
    """Benchmark compression on a directory of samples.

    All flags are passed through to openzl's benchmark command.
    Use -- to separate nyx flags from openzl flags if needed.
    """
    extra_args = ctx.args
    output = openzl.benchmark(
        input_dir=__import__("pathlib").Path(input_dir),
        extra_args=extra_args,
        verbose=verbose,
    )
    if output:
        click.echo(output)
