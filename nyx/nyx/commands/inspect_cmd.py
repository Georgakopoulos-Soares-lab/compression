"""nyx inspect — inspect compressor files or OpenZL Parquet files."""

import json
import os
from pathlib import Path

import click

from ..core import openzl
from ..core.detect import is_parquet


@click.command("inspect", context_settings={"ignore_unknown_options": True,
                                              "allow_extra_args": True})
@click.argument("compressor_file", type=click.Path(exists=True))
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
@click.pass_context
def inspect_cmd(ctx, compressor_file, verbose):
    """Inspect a trained compressor or OpenZL Parquet file.

    For Parquet files, shows schema, compression details, and per-column info.
    For compressor files, all flags are passed through to zli inspect.
    """
    filepath = Path(compressor_file)

    if is_parquet(filepath):
        _inspect_parquet(filepath, verbose)
        return

    extra_args = ctx.args
    output = openzl.inspect(
        compressor=filepath,
        extra_args=extra_args,
        verbose=verbose,
    )
    if output:
        click.echo(output)


def _inspect_parquet(filepath: Path, verbose: bool) -> None:
    """Print inspection info for an OpenZL Parquet file."""
    import pyarrow.parquet as pq
    from ..core.parquet_reader import is_openzl_parquet

    file_size = os.path.getsize(filepath)
    meta = pq.read_metadata(str(filepath))
    schema = pq.read_schema(str(filepath))

    is_ozl = is_openzl_parquet(filepath)

    click.echo(f"File: {filepath.name}")
    click.echo(f"Size: {file_size:,} bytes ({file_size / 1e6:.1f} MB)")
    click.echo(f"Type: {'OpenZL Parquet' if is_ozl else 'Standard Parquet'}")
    click.echo(f"Rows: {meta.num_rows:,}")
    click.echo(f"Columns: {meta.num_columns}")
    click.echo(f"Row groups: {meta.num_row_groups}")
    click.echo()

    click.echo("Schema:")
    for field in schema:
        click.echo(f"  {field.name}: {field.type}")
    click.echo()

    if is_ozl and schema.metadata:
        raw_meta = schema.metadata
        if b"openzl:version" in raw_meta:
            click.echo(f"OpenZL version: {raw_meta[b'openzl:version'].decode()}")
        if b"openzl:columns" in raw_meta:
            col_meta = json.loads(raw_meta[b"openzl:columns"].decode())
            click.echo("\nPer-column compression:")
            click.echo(f"  {'Column':20s} {'Encoding':16s} {'Profile':12s} {'Raw bytes':>14s}")
            click.echo(f"  {'-'*62}")
            for name, info in col_meta.items():
                encoding = info.get("encoding", "?")
                profile = info.get("profile", "?")
                raw = info.get("uncompressed_bytes", 0)
                click.echo(f"  {name:20s} {encoding:16s} {profile:12s} {raw:>14,}")

    if verbose:
        click.echo("\nRow group details:")
        for rg_idx in range(meta.num_row_groups):
            rg = meta.row_group(rg_idx)
            click.echo(f"  Row group {rg_idx}: {rg.num_rows:,} rows")
            for col_idx in range(rg.num_columns):
                col = rg.column(col_idx)
                click.echo(
                    f"    {col.path_in_schema}: "
                    f"compressed={col.total_compressed_size:,} "
                    f"uncompressed={col.total_uncompressed_size:,} "
                    f"codec={col.compression}"
                )
