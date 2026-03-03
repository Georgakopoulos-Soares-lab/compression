"""Auto-detect genomic file types from file content and extension."""

from pathlib import Path
from typing import Optional

EXTENSION_MAP = {
    ".fasta": "fasta",
    ".fa": "fasta",
    ".fna": "fasta",
    ".fas": "fasta",
    ".fastq": "fastq",
    ".fq": "fastq",
    ".vcf": "vcf",
    ".jsonl": "jsonl",
}


def detect_by_content(filepath: Path) -> Optional[str]:
    """Detect file type by inspecting the first 1KB of content.

    Mirrors the logic in biocompress_preprocessor.cpp detect_type().
    """
    try:
        with open(filepath, "rb") as f:
            head = f.read(1024)
    except OSError:
        return None

    if len(head) < 10:
        return None

    if head[:16].startswith(b"##fileformat=VCF"):
        return "vcf"

    if head[0:1] == b">":
        return "fasta"

    if head[0:1] == b"@":
        # FASTQ: 3rd line starts with '+'
        lines = head.split(b"\n", 3)
        if len(lines) >= 3 and lines[2].startswith(b"+"):
            return "fastq"

    if head[0:1] == b"{":
        # JSONL: first line is a valid JSON object
        first_line = head.split(b"\n", 1)[0]
        if first_line.rstrip().endswith(b"}"):
            return "jsonl"

    return None


def detect_by_extension(filepath: Path) -> Optional[str]:
    """Detect file type from file extension."""
    # Handle double extensions like .fasta.gz
    suffixes = filepath.suffixes
    for suffix in suffixes:
        lower = suffix.lower()
        if lower in EXTENSION_MAP:
            return EXTENSION_MAP[lower]
    return None


def detect_filetype(filepath: Path) -> Optional[str]:
    """Detect file type using content first, then extension fallback.

    Returns one of: 'fasta', 'fastq', 'vcf', or None for unknown.
    """
    result = detect_by_content(filepath)
    if result:
        return result
    return detect_by_extension(filepath)


def detect_fasta_subtype(filepath: Path) -> str:
    """Detect whether a FASTA file contains nucleotide or protein sequences.

    Reads the first ~10KB, scans sequence lines (non-header lines).
    If any sequence character is in {E, F, I, L, P, Q} (amino acids
    that are NOT valid IUPAC nucleotide codes), returns "protein".
    Otherwise returns "nucleotide".
    """
    # Characters that are amino acids but NOT IUPAC nucleotide codes
    PROTEIN_ONLY = set('EFILPQefilpq')

    try:
        with open(filepath, "rb") as f:
            head = f.read(10240)
    except OSError:
        return "nucleotide"

    for line in head.split(b"\n"):
        line = line.rstrip(b"\r")
        # Skip header lines and empty lines
        if not line or line.startswith(b">"):
            continue
        # Check sequence characters
        for ch in line:
            if chr(ch) in PROTEIN_ONLY:
                return "protein"

    return "nucleotide"


def is_genomic(filetype: Optional[str]) -> bool:
    """Check if a file type is a supported genomic format."""
    return filetype in ("fasta", "fastq", "vcf")


def is_structured(filetype: Optional[str]) -> bool:
    """Check if a file type is a supported structured format (non-genomic lossless)."""
    return filetype in ("jsonl",)
