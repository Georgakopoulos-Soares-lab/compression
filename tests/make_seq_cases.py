#!/usr/bin/env python3
"""Generate legal-but-awkward FASTA and FASTQ files for round-trip testing.

  python3 tests/make_seq_cases.py tests/seq_cases

The VCF side of the package has had a hostile corpus since the raw-fallback bug
(a fallback that silently dropped bytes, caught only by cmp). FASTA and FASTQ had
none, which is how the FASTA packer's memory defects went unnoticed for so long:
nothing exercised it beyond four well-behaved reference assemblies.

These are not malformed files a compressor may refuse. They are files that occur
-- soft-masked assemblies, CRLF from a Windows tool, an empty record, a read of
length one, a quality line that happens to start with '@' -- plus a handful that
are genuinely hostile (empty input, binary, a 4 MB single line) to check that the
tool fails loudly or falls back rather than corrupting.
"""
import os
import random
import sys

random.seed(20260907)

BASES = "ACGT"
IUPAC = "ACGTNRYKMSWBDHVN"


def seq(n, alphabet=BASES):
    return "".join(random.choice(alphabet) for _ in range(n))


def wrap(s, w=60):
    return "\n".join(s[i:i + w] for i in range(0, len(s), w))


def fasta_cases():
    c = {}
    c["empty"] = ""
    c["header_only"] = ">chr1 no sequence at all\n"
    c["no_trailing_newline"] = ">a\n" + wrap(seq(200))
    c["crlf"] = (">a desc\r\n" + wrap(seq(300)).replace("\n", "\r\n") + "\r\n")
    c["single_long_line"] = ">a\n" + seq(500000) + "\n"
    c["many_tiny_records"] = "".join(
        f">r{i}\n{seq(random.randint(1, 8))}\n" for i in range(5000))
    c["empty_sequence_record"] = ">a\n\n>b\nACGT\n>c\n\n"
    c["soft_masked"] = ">chr\n" + wrap("".join(
        (b.lower() if random.random() < 0.4 else b) for b in seq(50000))) + "\n"
    c["iupac"] = ">amb\n" + wrap(seq(20000, IUPAC)) + "\n"
    c["long_n_runs"] = ">gap\n" + wrap(seq(1000) + "N" * 60000 + seq(1000)) + "\n"
    c["ragged_line_widths"] = ">a\n" + "\n".join(
        seq(random.randint(1, 120)) for _ in range(500)) + "\n"
    c["long_header"] = ">" + "x" * 100000 + "\n" + wrap(seq(500)) + "\n"
    c["blank_lines_between"] = ">a\nACGT\n\n\n>b\nGGGG\n"
    c["lowercase_only"] = ">a\n" + wrap(seq(10000).lower()) + "\n"
    c["protein_like"] = ">p\n" + wrap(seq(5000, "ACDEFGHIKLMNPQRSTVWY*")) + "\n"
    c["no_leading_gt"] = "ACGTACGTACGT\nACGT\n"          # not FASTA at all
    c["binary"] = "".join(chr(b) for b in range(256)) * 40
    c["utf8_bom"] = "﻿>a\nACGT\n"
    c["cr_only"] = ">a\rACGTACGT\rACGT\r"
    return c


def fastq_cases():
    c = {}
    c["empty"] = ""
    c["single_record"] = "@r1\nACGT\n+\nIIII\n"
    c["no_trailing_newline"] = "@r1\nACGT\n+\nIIII"
    c["crlf"] = "@r1 x\r\nACGT\r\n+\r\nIIII\r\n"
    c["len_one_reads"] = "".join(f"@r{i}\nA\n+\nI\n" for i in range(20000))
    c["variable_lengths"] = "".join(
        (lambda n: f"@r{i} run:1:{i}\n{seq(n)}\n+\n{seq(n, chr(33) + chr(40) + chr(70))}\n")(
            random.randint(20, 300)) for i in range(20000))
    # A quality line may legally begin with '@' (chr(64) is a valid Phred+33
    # score), which is the classic way a naive record splitter loses sync.
    c["quality_starts_with_at"] = "".join(
        f"@r{i}\n{seq(40)}\n+\n" + "@" * 40 + "\n" for i in range(5000))
    c["plus_repeats_header"] = "".join(
        f"@r{i} d\n{seq(50)}\n+r{i} d\n{seq(50, chr(35) + chr(74))}\n" for i in range(5000))
    c["full_phred_alphabet"] = "".join(
        f"@r{i}\n{seq(94)}\n+\n" + "".join(chr(33 + j) for j in range(94)) + "\n"
        for i in range(3000))
    c["n_heavy"] = "".join(
        f"@r{i}\n{'N' * 60}\n+\n{'#' * 60}\n" for i in range(5000))
    c["fixed_len_illumina"] = "".join(
        f"@INST:1:FC:1:1101:{i}:{i + 7}/1\n{seq(51)}\n+\n{seq(51, chr(35) + chr(70) + chr(74))}\n"
        for i in range(20000))
    c["nanopore_like"] = "".join(
        (lambda n: f"@{i:08x}-uuid runid=abc read={i}\n{seq(n)}\n+\n{seq(n, chr(33) + chr(45))}\n")(
            random.randint(2000, 20000)) for i in range(200))
    c["one_giant_read"] = "@big\n" + seq(4000000) + "\n+\n" + "I" * 4000000 + "\n"
    c["empty_sequence"] = "@r1\n\n+\n\n@r2\nACGT\n+\nIIII\n"
    c["not_fastq"] = "just some text\nwith lines\n"
    c["truncated_last_record"] = "@r1\nACGT\n+\nIIII\n@r2\nACGT\n"
    c["utf8_bom"] = "﻿@r1\nACGT\n+\nIIII\n"
    return c


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "tests/seq_cases"
    n = 0
    for kind, cases in (("fasta", fasta_cases()), ("fastq", fastq_cases())):
        d = os.path.join(out, kind)
        os.makedirs(d, exist_ok=True)
        ext = "fa" if kind == "fasta" else "fq"
        for name, body in cases.items():
            with open(os.path.join(d, f"{name}.{ext}"), "wb") as fh:
                fh.write(body.encode("utf-8", "surrogateescape"))
            n += 1
    print(f"wrote {n} cases under {out}")


if __name__ == "__main__":
    main()
