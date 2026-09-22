#!/usr/bin/env python3
"""Generate small VCFs that exercise the corners of the format.

Each file is a byte-exactness trap: if nyx_vcf's transform is not perfectly
reversible on it, `--verify` fails. Run tests/test_vcf_roundtrip.sh to check.
"""
import os
import sys

HDR = (
    "##fileformat=VCFv4.2\n"
    "##contig=<ID=1,length=249250621>\n"
    "##contig=<ID=chrX,length=156040895>\n"
    '##INFO=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
    '##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency">\n'
    '##INFO=<ID=END,Number=1,Type=Integer,Description="End">\n'
    '##INFO=<ID=SOMATIC,Number=0,Type=Flag,Description="Somatic">\n'
    '##INFO=<ID=CSQ,Number=.,Type=String,Description="Format: Allele|Gene|Impact">\n'
    '##FORMAT=<ID=GT,Number=1,Type=String,Description="Genotype">\n'
    '##FORMAT=<ID=DP,Number=1,Type=Integer,Description="Depth">\n'
    '##FORMAT=<ID=AD,Number=R,Type=Integer,Description="Allelic depths">\n'
    '##ALT=<ID=NON_REF,Description="non-ref">\n'
)

def cols(samples):
    c = ["#CHROM", "POS", "ID", "REF", "ALT", "QUAL", "FILTER", "INFO"]
    if samples:
        c += ["FORMAT"] + samples
    return "\t".join(c) + "\n"

CASES = {}

# sites-only, no FORMAT column at all
CASES["sites_only"] = HDR + cols([]) + "".join(
    f"1\t{1000+i*7}\t.\tA\tG\t{'.' if i%3 else '50'}\t{'PASS' if i%2 else '.'}\tDP={i};AF=0.0{i:03d}\n"
    for i in range(50))

# every field that may legally be "."
CASES["all_dots"] = HDR + cols(["S1"]) + "".join(
    f"1\t{2000+i}\t.\t.\t.\t.\t.\t.\tGT\t./.\n" for i in range(20))

# flag INFO keys (no '='), duplicate keys, and an empty value after '='
CASES["info_shapes"] = HDR + cols([]) + (
    "1\t10\t.\tA\tT\t.\t.\tSOMATIC\n"
    "1\t11\t.\tA\tT\t.\t.\tDP=1;DP=2\n"
    "1\t12\t.\tA\tT\t.\t.\tDP=\n"
    "1\t13\t.\tA\tT\t.\t.\t.\n"
    "1\t14\t.\tA\tT\t.\t.\tSOMATIC;DP=5;AF=0.5\n")

# unphased, phased, mixed separators, missing alleles, multi-allelic
CASES["gt_shapes"] = HDR + cols(["A", "B", "C"]) + (
    "1\t100\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0\t0/1\t1|1\n"
    "1\t101\t.\tA\tT,G\t.\t.\tDP=1\tGT\t0|1\t2|1\t./.\n"
    "1\t102\t.\tA\tT\t.\t.\tDP=1\tGT\t.|0\t0|.\t1/0\n"
    "1\t103\t.\tA\tT\t.\t.\tDP=1\tGT\t0\t1\t0\n"          # haploid row
    "1\t104\t.\tA\tT\t.\t.\tDP=1\tGT\t0|0|0\t1|1|1\t0|1|0\n")  # triploid row

# haploid throughout (chrX/chrY style)
CASES["haploid"] = HDR + cols(["A", "B"]) + "".join(
    f"chrX\t{500+i}\t.\tA\tG\t.\t.\tDP={i}\tGT\t{i%2}\t{(i+1)%2}\n" for i in range(30))

# samples carrying fewer FORMAT subfields than declared
CASES["ragged_format"] = HDR + cols(["A", "B"]) + (
    "1\t200\t.\tA\tT\t.\t.\tDP=1\tGT:DP:AD\t0|0:10:5,5\t0|1:12:6,6\n"
    "1\t201\t.\tA\tT\t.\t.\tDP=1\tGT:DP:AD\t0|0:10\t0|1\n"
    "1\t202\t.\tA\tT\t.\t.\tDP=1\tGT:DP:AD\t0|0\t0|1:9:4,5\n")

# FORMAT varies row to row, as in a real gVCF
CASES["mixed_format"] = HDR + cols(["S"]) + (
    "1\t300\t.\tN\t<NON_REF>\t.\t.\tEND=400\tGT:DP\t0/0:3\n"
    "1\t401\t.\tA\tT,<NON_REF>\t.\t.\tDP=9\tGT:DP:AD\t0/1:9:4,5,0\n"
    "1\t402\t.\tN\t<NON_REF>\t.\t.\tEND=500\tGT:DP\t0/0:2\n")

# VEP-style CSQ: comma-separated records of pipe-separated fields
CASES["csq_annotations"] = HDR + cols([]) + "".join(
    f"1\t{700+i}\t.\tA\tT\t.\t.\tCSQ=T|GENE{i%5}|HIGH,T|GENE{i%7}|LOW\n" for i in range(40))

# POS text that is not a canonical integer, and huge coordinates
CASES["odd_pos"] = HDR + cols([]) + (
    "1\t0001000\t.\tA\tT\t.\t.\tDP=1\n"     # leading zeros
    "1\t1000\t.\tA\tT\t.\t.\tDP=1\n"
    "1\t999999999999\t.\tA\tT\t.\t.\tDP=1\n"
    "1\t5\t.\tA\tT\t.\t.\tDP=1\n")          # decreasing POS

# numbers that must not be normalised away
CASES["number_text"] = HDR + cols([]) + (
    "1\t10\t.\tA\tT\t.\t.\tAF=0.500\n"      # trailing zero
    "1\t11\t.\tA\tT\t.\t.\tAF=.5\n"         # no leading digit
    "1\t12\t.\tA\tT\t.\t.\tAF=1e-05\n"      # exponent
    "1\t13\t.\tA\tT\t.\t.\tAF=-0.25\n"      # negative
    "1\t14\t.\tA\tT\t.\t.\tAF=00.5\n")      # leading zeros

# rsID and long REF/ALT
CASES["ids_and_indels"] = HDR + cols([]) + "".join(
    f"1\t{800+i}\trs{1000000+i*13}\t{'A'*(i%9+1)}\t{'ACGT'*(i%5+1)}\t.\t.\tDP={i}\n"
    for i in range(40))

def write(path, text, newline=True, crlf=False):
    if crlf:
        text = text.replace("\n", "\r\n")
    if not newline and text.endswith("\n" if not crlf else "\r\n"):
        text = text[:-2] if crlf else text[:-1]
    with open(path, "w", newline="") as fh:
        fh.write(text)

def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "tests/vcf_edge_cases"
    os.makedirs(out, exist_ok=True)
    n = 0
    for name, body in CASES.items():
        write(os.path.join(out, f"{name}.vcf"), body)
        n += 1
    # line-ending and trailing-newline variants of a representative case
    write(os.path.join(out, "crlf.vcf"), CASES["gt_shapes"], crlf=True)
    write(os.path.join(out, "no_trailing_newline.vcf"), CASES["gt_shapes"], newline=False)
    write(os.path.join(out, "crlf_no_trailing_newline.vcf"), CASES["gt_shapes"],
          newline=False, crlf=True)
    # header with no data rows at all
    write(os.path.join(out, "header_only.vcf"), HDR + cols(["S1"]))
    n += 4
    print(f"wrote {n} edge-case VCFs to {out}")

if __name__ == "__main__":
    main()
