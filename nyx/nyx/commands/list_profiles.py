"""nyx list-profiles — passthrough to zli list-profiles."""

import click

from ..core import openzl


@click.command("list-profiles")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
def list_profiles_cmd(verbose):
    """List available OpenZL compression profiles."""
    output = openzl.list_profiles(verbose=verbose)
    if output:
        click.echo(output)
