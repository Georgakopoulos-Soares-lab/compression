"""CLI command: nyx json-benchmark — JSON compression benchmark suite."""

import click

from ..core.json_bench import BenchConfig, print_results_table, run_json_benchmark
from pathlib import Path


NYX_CMD_DEFAULT = (
    "nyx compress {input} -o {output} --mode train_custom --sddl {sddl} -f"
)
NYX_DEC_DEFAULT = "nyx decompress {input} -o {output} -f"


@click.command("json-benchmark")
@click.argument("input_file", type=click.Path(exists=True))
@click.option(
    "-o", "--outdir",
    default="bench_out",
    type=click.Path(),
    help="Output directory for results and artifacts.",
)
@click.option("--runs", default=5, type=int, help="Timed runs per pair (default 5).")
@click.option("--warmup", default=1, type=int, help="Warmup runs (default 1).")
@click.option("--keep-temp", is_flag=True, help="Keep decompressed temp files.")
@click.option("-f", "--force", is_flag=True, help="Overwrite existing output dir.")
@click.option(
    "--zstd-level", default=7, type=int, show_default=True,
    help="Zstandard compression level.",
)
@click.option(
    "--gzip-level", default=9, type=int, show_default=True,
    help="Gzip compression level.",
)
@click.option(
    "--pigz-level", default=9, type=int, show_default=True,
    help="Pigz compression level.",
)
@click.option(
    "--nyx-sddl", default="astbin_v1.sddl", show_default=True,
    help="SDDL schema for nyx/OpenZL compression of ASTBIN.",
)
@click.option(
    "--nyx-cmd", default=NYX_CMD_DEFAULT, show_default=True,
    help="Nyx compress command template. Placeholders: {input}, {output}, {sddl}.",
)
@click.option(
    "--nyx-dec", default=NYX_DEC_DEFAULT, show_default=True,
    help="Nyx decompress command template. Placeholders: {input}, {output}.",
)
@click.option("-v", "--verbose", is_flag=True, help="Print verbose output.")
def json_benchmark_cmd(
    input_file, outdir, runs, warmup, keep_temp, force,
    zstd_level, gzip_level, pigz_level,
    nyx_sddl, nyx_cmd, nyx_dec, verbose,
):
    """Benchmark JSON compression: original vs TOON-like vs ASTBIN.

    \b
    Generates three artifacts from the input JSON file:
      1. Original JSON (verbatim copy)
      2. TOON-like normalized text (sorted keys, stable formatting)
      3. ASTBIN v1 binary (AST-based deterministic container)

    \b
    Then compresses each with gzip, pigz, zstd, and nyx (ASTBIN only),
    measuring timing and verifying SHA-256 round-trip integrity.

    \b
    Results are printed as a markdown table and saved to:
      <outdir>/results.json
      <outdir>/results.csv
    """
    outdir_path = Path(outdir)

    if outdir_path.exists() and not force:
        click.echo(
            f"Output directory '{outdir}' already exists. "
            f"Use --force to overwrite.",
            err=True,
        )
        raise SystemExit(1)

    cfg = BenchConfig(
        input_path=Path(input_file).resolve(),
        outdir=outdir_path.resolve(),
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
        verbose=verbose,
    )

    click.echo(f"JSON Benchmark: {input_file}")
    click.echo(f"  Runs: {runs} (warmup: {warmup})")
    click.echo(f"  zstd level: {zstd_level}, gzip level: {gzip_level}, "
               f"pigz level: {pigz_level}")
    click.echo(f"  Output: {outdir}")
    click.echo("")

    click.echo("Generating artifacts...")
    results = run_json_benchmark(cfg)

    table = print_results_table(results)
    click.echo(table)

    click.echo(f"Results written to {outdir}/results.json and {outdir}/results.csv")
