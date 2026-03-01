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

    Lossless compression for FASTA and FASTQ files, schema-aware
    compression for genomic formats, and generic compression for any file.

    \b
    Quick start:
      pip install -e ./nyx && nyx build
      nyx compress genome.fasta           # lossless → .zlfasta
      nyx compress reads.fastq            # lossless → .zlfastq
      nyx decompress genome.fasta.zlfasta

    \b
    Compression modes (--mode):
      auto        Auto-select based on file type (default)
      lossless    Byte-exact lossless (FASTA/FASTQ only)
      schema      Schema-aware with SDDL (genomic files)
      generic     Generic OpenZL compression
      inline      Generic with inline training
    """


main.add_command(compress_cmd)
main.add_command(decompress_cmd)
main.add_command(train_cmd)
main.add_command(benchmark_cmd)
main.add_command(inspect_cmd)
main.add_command(list_profiles_cmd)
main.add_command(build_cmd)


if __name__ == "__main__":
    main()
