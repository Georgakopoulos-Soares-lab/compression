#!/usr/bin/env python3
"""
File format analyzer for the SDDL generator.

Samples the input file and produces statistics used to decide
the optimal binary layout and SDDL schema.

Usage:
    python3 analyzer.py <input_file> [--sample-bytes N]
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(path: Path, head: bytes) -> str:
    """Detect file format from content and extension.

    Returns one of: fasta, fastq, vcf, tsv, csv, json, generic
    """
    # Decode first chunk for text analysis (ignore errors for binary)
    text = head.decode("utf-8", errors="replace")
    lines = text.split("\n")
    non_empty = [l for l in lines if l.strip()]

    if not non_empty:
        return "generic"

    first = non_empty[0]

    # VCF: header line
    if first.startswith("##fileformat=VCF"):
        return "vcf"

    # FASTA: starts with >
    if first.startswith(">"):
        return "fasta"

    # FASTQ: starts with @, third line starts with +
    if first.startswith("@") and len(non_empty) >= 3 and non_empty[2].startswith("+"):
        return "fastq"

    # JSON: starts with { or [
    stripped = text.strip()
    if stripped and stripped[0] in "{[":
        return "json"

    # TSV: consistent tab-separated columns
    if "\t" in first:
        col_counts = [l.count("\t") + 1 for l in non_empty[:20] if l.strip()]
        if col_counts and all(c == col_counts[0] for c in col_counts) and col_counts[0] >= 2:
            return "tsv"

    # CSV: consistent comma-separated columns
    if "," in first:
        col_counts = [l.count(",") + 1 for l in non_empty[:20] if l.strip()]
        if col_counts and all(c == col_counts[0] for c in col_counts) and col_counts[0] >= 2:
            return "csv"

    # Extension fallback
    ext = path.suffix.lower()
    ext_map = {
        ".fasta": "fasta", ".fa": "fasta", ".fna": "fasta", ".fas": "fasta",
        ".fastq": "fastq", ".fq": "fastq",
        ".vcf": "vcf",
        ".tsv": "tsv", ".tab": "tsv",
        ".csv": "csv",
        ".json": "json", ".jsonl": "json",
    }
    return ext_map.get(ext, "generic")


# ---------------------------------------------------------------------------
# Format-specific samplers
# ---------------------------------------------------------------------------

def _sample_fasta(path: Path, max_bytes: int) -> Dict[str, Any]:
    """Sample FASTA records."""
    records = 0
    header_lengths: List[int] = []
    seq_lengths: List[int] = []
    char_freq: Counter = Counter()
    headers: List[str] = []

    bytes_read = 0
    current_header = ""
    current_seq_len = 0

    with open(path, "r", errors="replace") as f:
        for line in f:
            bytes_read += len(line)
            if bytes_read > max_bytes:
                break

            line = line.rstrip("\n\r")
            if line.startswith(">"):
                # Finish previous record
                if current_header:
                    header_lengths.append(len(current_header))
                    seq_lengths.append(current_seq_len)
                    records += 1

                current_header = line[1:]  # strip >
                headers.append(current_header)
                current_seq_len = 0
            else:
                for ch in line:
                    char_freq[ch.upper()] += 1
                current_seq_len += len(line)

    # Finish last record
    if current_header and current_seq_len > 0:
        header_lengths.append(len(current_header))
        seq_lengths.append(current_seq_len)
        records += 1

    # Compute common header prefix
    common_prefix = ""
    if headers:
        common_prefix = headers[0]
        for h in headers[1:]:
            i = 0
            while i < len(common_prefix) and i < len(h) and common_prefix[i] == h[i]:
                i += 1
            common_prefix = common_prefix[:i]
            if not common_prefix:
                break

    # Alphabet analysis
    total_bases = sum(char_freq.values())
    acgt_count = sum(char_freq.get(b, 0) for b in "ACGT")
    n_count = char_freq.get("N", 0)
    has_n = n_count > 0
    n_ratio = n_count / total_bases if total_bases > 0 else 0

    return {
        "format": "fasta",
        "records": records,
        "header_len_avg": sum(header_lengths) / len(header_lengths) if header_lengths else 0,
        "header_len_min": min(header_lengths) if header_lengths else 0,
        "header_len_max": max(header_lengths) if header_lengths else 0,
        "seq_len_avg": sum(seq_lengths) / len(seq_lengths) if seq_lengths else 0,
        "seq_len_min": min(seq_lengths) if seq_lengths else 0,
        "seq_len_max": max(seq_lengths) if seq_lengths else 0,
        "alphabet": sorted(char_freq.keys()),
        "alphabet_size": len(char_freq),
        "has_n_bases": has_n,
        "n_ratio": round(n_ratio, 6),
        "acgt_ratio": round(acgt_count / total_bases, 6) if total_bases > 0 else 0,
        "char_freq": dict(char_freq.most_common(20)),
        "common_header_prefix": common_prefix,
        "common_prefix_len": len(common_prefix),
        "fixed_length_seqs": len(set(seq_lengths)) == 1 if seq_lengths else False,
    }


def _sample_fastq(path: Path, max_bytes: int) -> Dict[str, Any]:
    """Sample FASTQ records."""
    records = 0
    read_lengths: List[int] = []
    header_lengths: List[int] = []
    qual_range: List[int] = []
    char_freq: Counter = Counter()
    headers: List[str] = []

    bytes_read = 0
    with open(path, "r", errors="replace") as f:
        while bytes_read < max_bytes:
            h = f.readline()
            if not h:
                break
            s = f.readline()
            p = f.readline()
            q = f.readline()
            if not q:
                break

            bytes_read += len(h) + len(s) + len(p) + len(q)
            h = h.rstrip("\n\r")
            s = s.rstrip("\n\r")
            q_line = q.rstrip("\n\r")

            headers.append(h)
            header_lengths.append(len(h))
            read_lengths.append(len(s))
            for ch in s:
                char_freq[ch.upper()] += 1
            for ch in q_line:
                qual_range.append(ord(ch))
            records += 1

    # Common prefix
    common_prefix = ""
    if headers:
        common_prefix = headers[0]
        for h in headers[1:]:
            i = 0
            while i < len(common_prefix) and i < len(h) and common_prefix[i] == h[i]:
                i += 1
            common_prefix = common_prefix[:i]
            if not common_prefix:
                break

    total_bases = sum(char_freq.values())
    n_count = char_freq.get("N", 0)

    return {
        "format": "fastq",
        "records": records,
        "read_len_avg": sum(read_lengths) / len(read_lengths) if read_lengths else 0,
        "read_len_min": min(read_lengths) if read_lengths else 0,
        "read_len_max": max(read_lengths) if read_lengths else 0,
        "header_len_avg": sum(header_lengths) / len(header_lengths) if header_lengths else 0,
        "fixed_length_reads": len(set(read_lengths)) == 1 if read_lengths else False,
        "alphabet": sorted(char_freq.keys()),
        "alphabet_size": len(char_freq),
        "has_n_bases": n_count > 0,
        "n_ratio": round(n_count / total_bases, 6) if total_bases > 0 else 0,
        "qual_min": min(qual_range) if qual_range else 0,
        "qual_max": max(qual_range) if qual_range else 0,
        "char_freq": dict(char_freq.most_common(20)),
        "common_header_prefix": common_prefix,
        "common_prefix_len": len(common_prefix),
    }


def _sample_vcf(path: Path, max_bytes: int) -> Dict[str, Any]:
    """Sample VCF records."""
    records = 0
    header_lines = 0
    chroms: Counter = Counter()
    filters: Counter = Counter()
    ref_lengths: List[int] = []
    alt_lengths: List[int] = []
    info_lengths: List[int] = []
    has_genotypes = False

    bytes_read = 0
    raw_header = []
    with open(path, "r", errors="replace") as f:
        for line in f:
            bytes_read += len(line)
            if bytes_read > max_bytes:
                break
            line = line.rstrip("\n\r")
            if line.startswith("#"):
                header_lines += 1
                raw_header.append(line)
                continue
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            records += 1
            chroms[cols[0]] += 1
            filters[cols[6]] += 1
            ref_lengths.append(len(cols[3]))
            alt_lengths.append(len(cols[4]))
            info_lengths.append(len(cols[7]))
            if len(cols) > 8:
                has_genotypes = True

    return {
        "format": "vcf",
        "records": records,
        "header_lines": header_lines,
        "unique_chroms": len(chroms),
        "chrom_list": list(chroms.keys()),
        "unique_filters": len(filters),
        "filter_list": list(filters.keys()),
        "ref_len_avg": sum(ref_lengths) / len(ref_lengths) if ref_lengths else 0,
        "alt_len_avg": sum(alt_lengths) / len(alt_lengths) if alt_lengths else 0,
        "info_len_avg": sum(info_lengths) / len(info_lengths) if info_lengths else 0,
        "has_genotypes": has_genotypes,
    }


def _sample_tabular(path: Path, max_bytes: int, delimiter: str) -> Dict[str, Any]:
    """Sample TSV/CSV records."""
    records = 0
    col_count = 0
    col_types: Dict[int, Counter] = {}
    header_row: Optional[List[str]] = None

    bytes_read = 0
    with open(path, "r", errors="replace") as f:
        for line_num, line in enumerate(f):
            bytes_read += len(line)
            if bytes_read > max_bytes:
                break
            line = line.rstrip("\n\r")
            if not line:
                continue
            cols = line.split(delimiter)
            if line_num == 0:
                # Check if header
                header_row = cols
                col_count = len(cols)
                for i in range(col_count):
                    col_types[i] = Counter()
                continue
            records += 1
            for i, val in enumerate(cols):
                if i >= col_count:
                    break
                col_types.setdefault(i, Counter())
                col_types[i][_detect_value_type(val)] += 1

    # Determine dominant type per column
    col_type_summary = {}
    for i in range(col_count):
        if i in col_types and col_types[i]:
            col_type_summary[header_row[i] if header_row else f"col_{i}"] = col_types[i].most_common(1)[0][0]

    return {
        "format": "tsv" if delimiter == "\t" else "csv",
        "records": records,
        "columns": col_count,
        "header_row": header_row,
        "column_types": col_type_summary,
    }


def _detect_value_type(val: str) -> str:
    """Guess the type of a string value."""
    val = val.strip()
    if not val or val in ("", ".", "NA", "null", "None"):
        return "null"
    try:
        int(val)
        return "int"
    except ValueError:
        pass
    try:
        float(val)
        return "float"
    except ValueError:
        pass
    if val.lower() in ("true", "false"):
        return "bool"
    return "string"


def _sample_generic(path: Path, max_bytes: int) -> Dict[str, Any]:
    """Generic file sampling — byte frequency distribution."""
    freq: Counter = Counter()
    with open(path, "rb") as f:
        data = f.read(max_bytes)
    freq.update(data)
    printable = sum(1 for b in data if 32 <= b <= 126)
    return {
        "format": "generic",
        "sample_size": len(data),
        "printable_ratio": round(printable / len(data), 4) if data else 0,
        "unique_bytes": len(freq),
        "is_text": printable / len(data) > 0.9 if data else False,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def analyze(path: Path, max_bytes: int = 10 * 1024 * 1024) -> Dict[str, Any]:
    """Analyze a file and return statistics dictionary."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    file_size = path.stat().st_size

    # Read head for format detection
    with open(path, "rb") as f:
        head = f.read(min(8192, file_size))

    fmt = detect_format(path, head)

    # Format-specific sampling
    if fmt == "fasta":
        stats = _sample_fasta(path, max_bytes)
    elif fmt == "fastq":
        stats = _sample_fastq(path, max_bytes)
    elif fmt == "vcf":
        stats = _sample_vcf(path, max_bytes)
    elif fmt == "tsv":
        stats = _sample_tabular(path, max_bytes, "\t")
    elif fmt == "csv":
        stats = _sample_tabular(path, max_bytes, ",")
    else:
        stats = _sample_generic(path, max_bytes)

    stats["file_path"] = str(path)
    stats["file_size"] = file_size
    stats["file_size_human"] = _human_size(file_size)

    return stats


def _human_size(size: int) -> str:
    """Format byte count as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def print_analysis(stats: Dict[str, Any]) -> None:
    """Print analysis summary to stdout."""
    print("=== SDDL Generator: Analysis ===")
    print(f"File: {stats['file_path']}")
    print(f"Detected format: {stats['format']}")
    print(f"File size: {stats['file_size_human']} ({stats['file_size']} bytes)")

    fmt = stats["format"]
    if fmt in ("fasta", "fastq"):
        print(f"Records sampled: {stats['records']}")
        key = "seq_len_avg" if fmt == "fasta" else "read_len_avg"
        key_min = "seq_len_min" if fmt == "fasta" else "read_len_min"
        key_max = "seq_len_max" if fmt == "fasta" else "read_len_max"
        print(f"Sequence length: avg={stats[key]:.0f} min={stats[key_min]} max={stats[key_max]}")
        print(f"Alphabet: {stats['alphabet']} (size={stats['alphabet_size']})")
        if fmt == "fasta":
            print(f"Fixed-length sequences: {stats['fixed_length_seqs']}")
        else:
            print(f"Fixed-length reads: {stats['fixed_length_reads']}")
        print(f"N-base ratio: {stats['n_ratio']}")
        if stats.get("common_prefix_len", 0) > 0:
            print(f"Common header prefix: \"{stats['common_header_prefix'][:60]}...\" ({stats['common_prefix_len']} chars)")
    elif fmt == "vcf":
        print(f"Records sampled: {stats['records']}")
        print(f"Header lines: {stats['header_lines']}")
        print(f"Unique chromosomes: {stats['unique_chroms']}")
        print(f"Has genotypes: {stats['has_genotypes']}")
    elif fmt in ("tsv", "csv"):
        print(f"Records sampled: {stats['records']}")
        print(f"Columns: {stats['columns']}")
        print(f"Column types: {stats['column_types']}")
    elif fmt == "generic":
        print(f"Sample size: {stats['sample_size']} bytes")
        print(f"Printable ratio: {stats['printable_ratio']}")
        print(f"Unique bytes: {stats['unique_bytes']}")

    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze file for SDDL generation")
    parser.add_argument("input", type=Path, help="Input file to analyze")
    parser.add_argument("--sample-bytes", type=int, default=10 * 1024 * 1024,
                        help="Max bytes to sample (default: 10 MB)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    stats = analyze(args.input, args.sample_bytes)
    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        print_analysis(stats)
