#!/usr/bin/env python3
"""
SDDL Generator CLI — Main entry point.

Analyzes an input file, generates an optimal SDDL schema and a matching
preprocessing script. Writes exactly two NEW files and prints a decision log.

Usage:
    python3 cli.py <input_file> [--output-dir DIR] [--sample-bytes N]

Examples:
    python3 cli.py /data/genome.fasta
    python3 cli.py /data/reads.fastq --output-dir ./out
    python3 cli.py /data/variants.vcf --output-dir /tmp/sddl_out
"""

import argparse
import os
import sys
from pathlib import Path

# Allow importing siblings
sys.path.insert(0, str(Path(__file__).parent))

from analyzer import analyze, print_analysis
from sddl_generator import generate_sddl, safe_output_path
from preprocess_generator import generate_preprocess


def print_decision_log(stats: dict, sddl_path: Path, preprocess_path: Path) -> None:
    """Print the decision log summarizing all layout choices."""
    fmt = stats.get("format", "generic")

    # Determine strategy details
    soa = fmt in ("fasta", "fastq", "vcf", "tsv", "csv")
    packing = "none"
    if fmt in ("fasta", "fastq"):
        has_n = stats.get("has_n_bases", True)
        if has_n:
            packing = "4-bit nibble (A=0,C=1,G=2,T=3,N=4; 2 bases/byte)"
        else:
            packing = "4-bit nibble (could use 2-bit but keeping 4-bit for pipeline compat)"

    instant_parse = True
    scan_fields = []
    # Root-level schemas always reference parsed num_records, which is technically scan
    if fmt != "generic":
        scan_fields.append("num_records (parsed field used for array sizes — standard pattern)")

    header_dedup = False
    if fmt == "fastq":
        prefix_len = stats.get("common_prefix_len", 0)
        header_dedup = prefix_len > 5

    entropy_methods = []
    if fmt in ("fasta", "fastq"):
        entropy_methods.append("case normalization (uppercase)")
        entropy_methods.append("whitespace removal (newlines stripped from sequences)")
        entropy_methods.append("delimiter removal (> and @ not stored)")
        if fmt in ("fasta", "fastq"):
            entropy_methods.append("symbol packing (4-bit)")
        if header_dedup:
            entropy_methods.append(f"header dedup (common prefix: {stats.get('common_prefix_len', 0)} chars)")
    elif fmt == "vcf":
        entropy_methods.append("dictionary encoding (CHROM, FILTER)")
        entropy_methods.append("delimiter removal (tabs not stored)")
    elif fmt in ("tsv", "csv"):
        entropy_methods.append("native binary encoding for numeric columns")
        entropy_methods.append("delimiter removal")

    print("=== SDDL Generator: Decision Log ===")
    print()
    print(f"Format: {fmt}")
    print(f"Strategy: {'SoA columnar' if soa else 'chunked blob'}")
    print()
    print("Layout decisions:")
    print(f"  - SoA columnar: {'yes' if soa else 'no'} — {'similar data grouped for better compression' if soa else 'no structure detected'}")
    print(f"  - Symbol packing: {packing}")
    print(f"  - Instant-parse: {'practically yes' if not scan_fields else 'root-level scan required'}")
    if scan_fields:
        for sf in scan_fields:
            print(f"      scan field: {sf}")
    print(f"  - Padding: 4-byte alignment on all payload sections")
    print(f"  - Header dedup: {'yes' if header_dedup else 'no'}" +
          (f" — {stats.get('common_prefix_len', 0)} char common prefix" if header_dedup else ""))
    print(f"  - Entropy reduction: {', '.join(entropy_methods) if entropy_methods else 'none'}")
    print()

    # Compatibility notes
    print("SDDL compatibility notes:")
    if fmt in ("fasta", "fastq", "vcf", "tsv", "csv"):
        print("  - Schema uses standard SDDL v0.6 features (Byte arrays, UInt32LE, expressions)")
        print("  - All features supported by pinned OpenZL build (commit e40fe9f3)")
    else:
        print("  - Minimal schema; consider --mode default instead of custom SDDL")
    print()

    print("Output files:")
    print(f"  1. {sddl_path}")
    print(f"  2. {preprocess_path}")
    print()
    print("To use with nyx:")
    print(f"  nyx compress <input> --mode train_custom --sddl {sddl_path}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Generate SDDL schema + preprocessing script for any input file",
        epilog="Generated files are always NEW — existing files are never modified.",
    )
    parser.add_argument("input", type=Path, help="Input file to analyze and generate for")
    parser.add_argument("--output-dir", type=Path, default=Path("./sddl_out"),
                        help="Output directory (default: ./sddl_out)")
    parser.add_argument("--sample-bytes", type=int, default=10 * 1024 * 1024,
                        help="Max bytes to sample for analysis (default: 10 MB)")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: {args.input} does not exist", file=sys.stderr)
        sys.exit(1)

    # Phase 1: Analyze
    stats = analyze(args.input, args.sample_bytes)
    print_analysis(stats)

    # Phase 2: Generate SDDL
    sddl_content = generate_sddl(stats)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    sddl_path = safe_output_path(args.output_dir, "custom", ".sddl")
    sddl_path.write_text(sddl_content)

    # Phase 3: Generate preprocessor
    preprocess_content = generate_preprocess(stats)
    preprocess_path = safe_output_path(args.output_dir, "preprocess", ".py")
    preprocess_path.write_text(preprocess_content)
    os.chmod(str(preprocess_path), 0o755)

    # Phase 4: Decision log
    print_decision_log(stats, sddl_path, preprocess_path)

    return sddl_path, preprocess_path


if __name__ == "__main__":
    main()
