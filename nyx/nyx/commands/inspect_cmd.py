"""nyx inspect — passthrough to zli inspect."""

import click

from ..core import openzl


@click.command("inspect", context_settings={"ignore_unknown_options": True,
                                              "allow_extra_args": True})
@click.argument("compressor_file", type=click.Path(exists=True))
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.pass_context
def inspect_cmd(ctx, compressor_file, verbose):
    """Inspect a trained compressor (outputs JSON).

    All flags are passed through to openzl's inspect command.
    """
    extra_args = ctx.args
    output = openzl.inspect(
        compressor=__import__("pathlib").Path(compressor_file),
        extra_args=extra_args,
        verbose=verbose,
    )
    if output:
        click.echo(output)
