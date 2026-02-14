"""Auto-detect genomic file types from file content and extension."""

from pathlib import Path

EXTENSION_MAP = {
    ".fasta": "fasta",
    ".fa": "fasta",
    ".fna": "fasta",
    ".fas": "fasta",
    ".fastq": "fastq",
    ".fq": "fastq",
    ".vcf": "vcf",
}


def detect_by_content(filepath: Path) -> str | None:
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

    return None


def detect_by_extension(filepath: Path) -> str | None:
    """Detect file type from file extension."""
    # Handle double extensions like .fasta.gz
    suffixes = filepath.suffixes
    for suffix in suffixes:
        lower = suffix.lower()
        if lower in EXTENSION_MAP:
            return EXTENSION_MAP[lower]
    return None


def detect_filetype(filepath: Path) -> str | None:
    """Detect file type using content first, then extension fallback.

    Returns one of: 'fasta', 'fastq', 'vcf', or None for unknown.
    """
    result = detect_by_content(filepath)
    if result:
        return result
    return detect_by_extension(filepath)


def is_genomic(filetype: str | None) -> bool:
    """Check if a file type is a supported genomic format."""
    return filetype in ("fasta", "fastq", "vcf")
