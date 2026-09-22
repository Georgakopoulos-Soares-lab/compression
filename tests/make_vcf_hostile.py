#!/usr/bin/env python3
"""Hostile inputs: files a VCF compressor may be handed in the wild.

Unlike tests/make_vcf_edge_cases.py, which exercises legal-but-awkward VCF,
these are malformed, degenerate, or extreme. The tool must either round-trip
them byte-for-byte or fail cleanly with a message -- never crash, hang, or
write an archive that decodes to something else.
"""
import os
import sys

HDR = ('##fileformat=VCFv4.3\n'
       '##contig=<ID=1,length=249250621>\n'
       '##INFO=<ID=DP,Number=1,Type=Integer,Description="d">\n'
       '##INFO=<ID=SVLEN,Number=.,Type=Integer,Description="sv">\n'
       '##ALT=<ID=DEL,Description="deletion">\n'
       '##FORMAT=<ID=GT,Number=1,Type=String,Description="gt">\n')
CH = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\n"
CH0 = "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"

C = {}

# --- degenerate files ---
C["empty"] = ""
C["header_no_chrom"] = "##fileformat=VCFv4.3\n##contig=<ID=1,length=100>\n"
C["chrom_line_only"] = HDR + CH
C["one_data_row"] = HDR + CH + "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\n"
C["blank_lines_between_rows"] = HDR + CH + "".join(
    f"1\t{i}\t.\tA\tT\t.\t.\tDP={i}\tGT\t0|0\n" for i in range(5))
C["no_final_newline_single_row"] = HDR + CH + "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0"

# --- structural variants / symbolic and breakend ALTs ---
C["symbolic_and_breakend_alt"] = HDR + CH + (
    "1\t100\t.\tA\t<DEL>\t.\t.\tSVLEN=-500\tGT\t0/1\n"
    "1\t200\tbnd_W\tG\tG]17:198982]\t.\t.\tDP=1\tGT\t0/1\n"
    "1\t300\tbnd_V\tT\t]13:123456]T\t.\t.\tDP=1\tGT\t0/1\n"
    "1\t400\t.\tA\t.[13:123457[\t.\t.\tDP=1\tGT\t0/1\n"
    "1\t500\t.\tA\t<CNV:TR>\t.\t.\tDP=1\tGT\t0/1\n"
    "1\t600\t.\tA\t*\t.\t.\tDP=1\tGT\t0/1\n")

# --- extreme field sizes ---
C["huge_info_one_row"] = HDR + CH0.replace("\tFORMAT\tS1", "") + (
    "1\t1\t.\tA\tT\t.\t.\t" + ";".join(f"K{i}={'x'*40}" for i in range(20000)) + "\n")
C["very_long_ref_alt"] = HDR + CH + (
    f"1\t1\t.\t{'ACGT'*80000}\t{'TGCA'*80000}\t.\t.\tDP=1\tGT\t0|0\n"
    "1\t2\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\n")
C["many_samples"] = (HDR
    + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\t"
    + "\t".join(f"S{i}" for i in range(5000)) + "\n"
    + "".join("1\t%d\t.\tA\tT\t.\t.\tDP=1\tGT\t" % p
              + "\t".join("0|1" if (p + i) % 7 else "1|1" for i in range(5000)) + "\n"
              for p in range(1, 40)))

# --- malformed / inconsistent ---
C["ragged_column_count"] = HDR + CH + (
    "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\n"
    "1\t2\t.\tA\tT\t.\t.\tDP=1\tGT\n"                      # missing sample
    "1\t3\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\t0|1\n")           # extra sample
C["embedded_nul_and_high_bytes"] = HDR + CH0 + (
    "1\t1\t.\tA\tT\t.\t.\tNOTE=café_naïve\n"
    "1\t2\t.\tA\tT\t.\t.\tNOTE=中文\n")
C["cr_only_line_endings"] = (HDR + CH + "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\n").replace("\n", "\r")
C["mixed_crlf_and_lf"] = HDR + CH + (
    "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\r\n"
    "1\t2\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\n"
    "1\t3\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\r\n")
C["utf8_bom"] = "﻿" + HDR + CH0 + "1\t1\t.\tA\tT\t.\t.\tDP=1\n"
C["duplicate_sample_names"] = (HDR
    + "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tS1\tS1\n"
    + "1\t1\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\t0|1\n")
C["tabs_in_header_text"] = ('##fileformat=VCFv4.3\n##weird=<ID=x,Desc="has\ttab">\n'
                            + CH0 + "1\t1\t.\tA\tT\t.\t.\tDP=1\n")
C["not_a_vcf_at_all"] = "this is not a VCF\njust some text\n" * 50
C["binary_garbage"] = None          # written separately as raw bytes


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "tests/vcf_hostile"
    os.makedirs(out, exist_ok=True)
    n = 0
    for name, body in C.items():
        p = os.path.join(out, f"{name}.vcf")
        if body is None:
            with open(p, "wb") as fh:
                fh.write(bytes(range(256)) * 200)
        else:
            with open(p, "w", newline="", encoding="utf-8") as fh:
                fh.write(body)
        n += 1
    print(f"wrote {n} hostile inputs to {out}")


if __name__ == "__main__":
    main()
