"""CLI command: nyx openalex-benchmark — OpenAlex snapshot compression benchmark."""

import click
from pathlib import Path

from ..core.openalex_bench import (
    OpenAlexBenchConfig,
    print_openalex_table,
    run_openalex_benchmark,
)


NYX_CMD_DEFAULT = (
    "nyx compress {input} -o {output} --mode train_custom --sddl {sddl} -f"
)
NYX_DEC_DEFAULT = "nyx decompress {input} -o {output} -f"


@click.command("openalex-benchmark")
@click.option(
    "--input-gz-dir", required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Directory containing OpenAlex .gz JSONL files.",
)
@click.option(
    "-o", "--outdir", default="bench_out",
    type=click.Path(),
    help="Output directory for results and artifacts.",
)
@click.option("--pattern", default="*.gz", show_default=True,
              help="Glob pattern for .gz files.")
@click.option("--shard-mb", default=128, type=int, show_default=True,
              help="Target shard size in MiB (uncompressed JSONL).")
@click.option("--runs", default=5, type=int, show_default=True,
              help="Timed runs per (shard, compressor) pair.")
@click.option("--warmup", default=1, type=int, show_default=True,
              help="Warmup runs (excluded from stats).")
@click.option("--keep-temp", is_flag=True, help="Keep decompressed temp files.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output dir.")
@click.option("--zstd-level", default=7, type=int, show_default=True,
              help="Zstandard compression level.")
@click.option("--gzip-level", default=9, type=int, show_default=True,
              help="Gzip compression level.")
@click.option("--pigz-level", default=9, type=int, show_default=True,
              help="Pigz compression level.")
@click.option("--nyx-sddl", default="astbin_v1.sddl", show_default=True,
              help="SDDL schema for nyx/OpenZL on ASTBIN shards.")
@click.option("--nyx-cmd", default=NYX_CMD_DEFAULT, show_default=True,
              help="Nyx compress template: {input}, {output}, {sddl}.")
@click.option("--nyx-dec", default=NYX_DEC_DEFAULT, show_default=True,
              help="Nyx decompress template: {input}, {output}.")
@click.option("--limit-records", default=None, type=int,
              help="Stop after N records (smoke test mode).")
@click.option("-v", "--verbose", is_flag=True, help="Verbose output.")
def openalex_benchmark_cmd(
    input_gz_dir, outdir, pattern, shard_mb, runs, warmup, keep_temp,
    force, zstd_level, gzip_level, pigz_level,
    nyx_sddl, nyx_cmd, nyx_dec, limit_records, verbose,
):
    """Benchmark compression on OpenAlex .gz JSONL snapshot data.

    \b
    Pipeline:
      1. Stream-decompress .gz files into deterministic JSONL shards
      2. Convert JSONL shards to ASTBIN v1 binary format
      3. Benchmark gzip/pigz/zstd on both JSONL + ASTBIN shards,
         and nyx/OpenZL on ASTBIN shards

    \b
    Example:
      nyx openalex-benchmark \\
        --input-gz-dir data/openalex_authors_8gb \\
        --outdir bench_out/authors \\
        --shard-mb 128 --runs 5

    \b
    Smoke test (small subset):
      nyx openalex-benchmark \\
        --input-gz-dir data/openalex_authors_8gb \\
        --outdir bench_out/smoke \\
        --limit-records 10000 --runs 2 --warmup 0
    """
    outdir_path = Path(outdir).resolve()

    if outdir_path.exists() and not force:
        click.echo(
            f"Output directory '{outdir}' already exists. Use --force to overwrite.",
            err=True,
        )
        raise SystemExit(1)

    cfg = OpenAlexBenchConfig(
        input_gz_dir=Path(input_gz_dir).resolve(),
        outdir=outdir_path,
        pattern=pattern,
        shard_mib=shard_mb,
        runs=runs,
        warmup=warmup,
        keep_temp=keep_temp,
        force=force,
        zstd_level=zstd_level,
        gzip_level=gzip_level,
        pigz_level=pigz_level,
        nyx_sddl=nyx_sddl,
        nyx_cmd_template=nyx_cmd,
        nyx_dec_template=nyx_dec,
        limit_records=limit_records,
        verbose=verbose,
    )

    click.echo("OpenAlex Benchmark")
    click.echo(f"  Input: {input_gz_dir} ({pattern})")
    click.echo(f"  Output: {outdir}")
    click.echo(f"  Shard size: {shard_mb} MiB")
    click.echo(f"  Runs: {runs} (warmup: {warmup})")
    click.echo(f"  zstd: {zstd_level}, gzip: {gzip_level}, pigz: {pigz_level}")
    if limit_records:
        click.echo(f"  Limit: {limit_records} records (smoke test)")
    click.echo("")

    results = run_openalex_benchmark(cfg)
    table = print_openalex_table(results)
    click.echo(table)

    click.echo(
        f"Results: {outdir}/results.json, {outdir}/results.csv\n"
        f"Environment: {outdir}/env.json"
    )
