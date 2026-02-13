"""Schema registry and default configuration for Nyx."""

from dataclasses import dataclass
from typing import Dict, Optional
import os


@dataclass(frozen=True)
class FileTypeConfig:
    """Configuration for a supported genomic file type."""

    preprocessor_type: str  # Argument passed to genomic_preprocessor
    sddl: Optional[str]     # SDDL schema filename (in schemas/)
    chunk_extension: str     # Extension of output chunks


# Maps detected file types to their preprocessing configuration.
# To add a new file type: add an entry here + SDDL schema + preprocessor support.
SCHEMA_REGISTRY: Dict[str, FileTypeConfig] = {
    "fasta": FileTypeConfig(
        preprocessor_type="fasta_packed",
        sddl="fasta_packed.sddl",
        chunk_extension=".fasta_packed.bin",
    ),
    "fastq": FileTypeConfig(
        preprocessor_type="fastq_v4",
        sddl=None,  # TODO: create fastq_v4.sddl
        chunk_extension=".fastq_v4.bin",
    ),
    "vcf": FileTypeConfig(
        preprocessor_type="vcf",
        sddl=None,  # TODO: create vcf.sddl
        chunk_extension=".vcf.bin",
    ),
}

GENOMIC_TYPES = set(SCHEMA_REGISTRY.keys())

# Defaults
DEFAULT_THREADS = os.cpu_count() or 4
DEFAULT_COMPRESS_JOBS = 4
DEFAULT_TRAIN_MIB = 200
DEFAULT_MAX_TIME_SECS = 1800
DEFAULT_PROFILE = "serial"  # Generic openzl profile for non-genomic files
