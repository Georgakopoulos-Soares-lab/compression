#!/usr/bin/env python3
"""Legal-but-awkward and outright hostile BED inputs.

The FASTA memory defects and two crashes in the sequence codecs were all things
a corpus of four well-behaved reference files could never have caught. This is
the BED equivalent: everything a compressor actually gets handed, not what the
spec says it will get.

Writes one file per case into the directory given as argv[1].
"""
import os, sys, random

def w(d, name, data):
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode())
    return p

def main():
    d = sys.argv[1]
    os.makedirs(d, exist_ok=True)
    random.seed(7)

    rows6 = "".join(f"chr1\t{i*100}\t{i*100+50}\tfeat{i}\t{i%1000}\t{'+-'[i%2]}\n"
                    for i in range(1, 2001))

    # --- widths, including the ones Genozip's allow-list rejects -------------
    for k in (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 17, 24):
        rows = []
        for i in range(1, 1501):
            f = ["chr%d" % (1 + i % 5), str(i * 100), str(i * 100 + 60)]
            f += ["name%d" % i, str(i % 1000), "+-"[i % 2]][: max(0, k - 3)]
            while len(f) < k:
                f.append(str((i * len(f)) % 997))
            w_ = "\t".join(f[:k])
            rows.append(w_)
        w(d, f"width{k:02d}.bed", "\n".join(rows) + "\n")

    # --- the real dialects, in miniature ------------------------------------
    w(d, "narrowPeak.bed", "".join(
        f"chr{1+i%22}\t{i*250}\t{i*250+150}\tpeak_{i}\t{i%1000}\t.\t"
        f"{i/7:.5f}\t{i/11:.5f}\t{-1 if i%3 else i/13:.5f}\t{i%150}\n"
        for i in range(1, 1201)))
    w(d, "bed12.bed", "".join(
        f"chr{1+i%5}\t{i*500}\t{i*500+300}\ttx{i}\t0\t{'+-'[i%2]}\t"
        f"{i*500+10}\t{i*500+290}\t0,0,255\t3\t50,60,70,\t0,100,200,\n"
        for i in range(1, 801)))
    w(d, "bedgraph.bed", "track type=bedGraph name=x\n" + "".join(
        f"chr1\t{i*10}\t{i*10+10}\t{i/3:.4f}\n" for i in range(1, 1501)))

    # --- headers and comments ----------------------------------------------
    w(d, "header_track.bed", 'track name="a" description="b"\n' + rows6)
    w(d, "header_browser.bed", "browser position chr1:1-1000\ntrack name=x\n" + rows6)
    w(d, "header_hash.bed", "#chrom\tstart\tend\tname\tscore\tstrand\n" + rows6)
    w(d, "comment_midfile.bed", rows6[:1000] + "\n# a comment in the middle\n" + rows6[1000:])
    w(d, "bom.bed", b"\xef\xbb\xbf" + rows6.encode())

    # --- line endings and terminators --------------------------------------
    w(d, "crlf.bed", rows6.replace("\n", "\r\n"))
    w(d, "cr_only.bed", rows6.replace("\n", "\r"))
    w(d, "no_final_newline.bed", rows6.rstrip("\n"))
    w(d, "blank_lines.bed", rows6.replace("\n", "\n\n", 20))

    # --- values that break naive numeric transforms -------------------------
    w(d, "leading_zeros.bed", "".join(
        f"chr1\t{i*100:08d}\t{i*100+50}\tn{i}\t007\t+\n" for i in range(1, 801)))
    w(d, "negative_coords.bed", "".join(
        f"chr1\t{i*100}\t{i*100+50}\tn{i}\t{-i}\t-\n" for i in range(1, 801)))
    w(d, "huge_ints.bed", "".join(
        f"chr1\t{9223372036854775000+i}\t{9223372036854775300+i}\tn\t0\t+\n" for i in range(1, 401)))
    w(d, "float_variants.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t{v}\n" for i, v in enumerate(
            ["1.5", "1.50", "-0.001", "0", "-3", "10.000", "0.0"] * 120, start=1)))
    w(d, "sci_notation.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t{i}e-5\n" for i in range(1, 601)))
    w(d, "empty_fields.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t\t\t\n" for i in range(1, 601)))
    w(d, "dot_fields.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t.\t.\t.\n" for i in range(1, 601)))
    w(d, "unsorted.bed", "".join(
        f"chr{random.randint(1,22)}\t{random.randint(0,10**8)}\t{random.randint(0,10**8)}\tn{i}\n"
        for i in range(1, 1201)))
    w(d, "identical_rows.bed", "chr1\t100\t200\tx\t0\t+\n" * 3000)
    w(d, "one_row.bed", "chr1\t100\t200\n")
    w(d, "ragged.bed", "".join(
        ("chr1\t%d\t%d\ta\tb\tc\n" if i % 7 else "chr1\t%d\t%d\n") % (i*10, i*10+5)
        for i in range(1, 901)))
    w(d, "mostly_ragged.bed", "".join(
        "chr1\t%d\t%d%s\n" % (i*10, i*10+5, "\tx" * (i % 9)) for i in range(1, 901)))
    w(d, "utf8_names.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\tgène_é中文_{i}\n" for i in range(1, 601)))
    w(d, "long_field.bed", "chr1\t0\t10\t" + "A" * 900000 + "\n" + rows6)
    w(d, "many_columns.bed", "".join(
        "\t".join(["chr1", str(i*10), str(i*10+5)] + [str((i*j) % 97) for j in range(60)]) + "\n"
        for i in range(1, 401)))
    w(d, "trailing_tab.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t\n" for i in range(1, 601)))
    w(d, "intlist_edge.bed", "".join(
        f"chr1\t{i*10}\t{i*10+5}\t{v}\n" for i, v in enumerate(
            ["1,2,3", "1,2,3,", "0", "0,", "10,0,5"] * 160, start=1)))

    # --- not BED at all -----------------------------------------------------
    w(d, "empty.bed", "")
    w(d, "newline_only.bed", "\n")
    w(d, "binary.bed", bytes(random.randrange(256) for _ in range(60000)))
    w(d, "prose.bed", "The quick brown fox jumps over the lazy dog.\n" * 800)
    w(d, "fasta_not_bed.bed", ">seq1\nACGTACGTAC\n>seq2\nTTTTGGGGCC\n" * 300)
    w(d, "header_only.bed", 'track name="empty"\n')
    w(d, "nul_bytes.bed", b"chr1\t0\t10\tna\x00me\n" * 600)

    print(f"{len(os.listdir(d))} cases in {d}")

main()
