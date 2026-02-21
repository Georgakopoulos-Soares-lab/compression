"""Nyx CLI — wrapper for OpenZL genomic compression pipelines."""

import click

from . import __version__
from .commands.compress import compress_cmd
from .commands.decompress import decompress_cmd
from .commands.train import train_cmd
from .commands.benchmark import benchmark_cmd
from .commands.inspect_cmd import inspect_cmd
from .commands.list_profiles import list_profiles_cmd
from .commands.build import build_cmd
from .commands.compress_lossless import compress_lossless_cmd
from .commands.decompress_lossless import decompress_lossless_cmd
from .commands.compress_lossless_fastq import compress_lossless_fastq_cmd
from .commands.decompress_lossless_fastq import decompress_lossless_fastq_cmd


@click.group()
@click.version_option(__version__, prog_name="nyx")
def main():
    """
    \b
     ███╗   ██╗██╗   ██╗██╗  ██╗
     ████╗  ██║╚██╗ ██╔╝╚██╗██╔╝
     ██╔██╗ ██║ ╚████╔╝  ╚███╔╝
     ██║╚██╗██║  ╚██╔╝   ██╔██╗
     ██║ ╚████║   ██║   ██╔╝ ██╗
     ╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝

    Genomic compression toolkit powered by OpenZL.

    Seamless, schema-aware compression for FASTA, FASTQ, and VCF files
    with preprocessing, training, parallel compression, and archive bundling.

    \b
    Quick start:
      pip install -e ./nyx && nyx build
      nyx compress genome.fasta
      nyx decompress genome.fasta.nyx

    \b
    Compression modes:
      train_plain   Schema-aware (auto-selects schema for genomic files)
      train_custom  Schema-aware (user provides --sddl schema)
      default       Generic OpenZL compression (no preprocessing)
      inline_train  Generic with inline training on input
    """


main.add_command(compress_cmd)
main.add_command(decompress_cmd)
main.add_command(train_cmd)
main.add_command(benchmark_cmd)
main.add_command(inspect_cmd)
main.add_command(list_profiles_cmd)
main.add_command(build_cmd)
main.add_command(compress_lossless_cmd)
main.add_command(decompress_lossless_cmd)
main.add_command(compress_lossless_fastq_cmd)
main.add_command(decompress_lossless_fastq_cmd)


if __name__ == "__main__":
    main()
