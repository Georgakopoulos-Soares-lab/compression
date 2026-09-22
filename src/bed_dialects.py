"""Which BED dialect each corpus file is, and how many columns it has.

Shared by src/plot_paper_figures.py and paper_drafts/build/revise_manuscript.py
so Figure 4 and the BED table cannot group the corpus differently. Widths are
properties of the files, measured when scripts/bed/download_bed.sh fetched them
(it prints the modal field count of every file); they are restated here only
because the figures and tables are built from CSVs, not from the data.
"""

# (dialect label, column count). Order is the order they appear in the table:
# by width, then by name, so the reader sees widths grow down the page.
DIALECTS = [
    ("ChromHMM segments", 4),
    ("SCREEN cCRE registry", 6),
    ("ENCODE TFBS clusters", 6),
    ("broadPeak", 9),
    ("ChromHMM dense", 9),
    ("FANTOM5 CAGE peaks", 9),
    ("narrowPeak", 10),
    ("gappedPeak", 15),
]
WIDTH = dict(DIALECTS)


def dialect(fname):
    """Dialect label for a corpus file name, or None if it is not one of ours."""
    f = fname.strip('"')
    if f.endswith(".narrowPeak"):
        return "narrowPeak"
    if f.endswith(".broadPeak"):
        return "broadPeak"
    if f.endswith(".gappedPeak"):
        return "gappedPeak"
    if "_segments.bed" in f:
        return "ChromHMM segments"
    if "_dense.bed" in f:
        return "ChromHMM dense"
    if f.startswith("screen_ccre"):
        return "SCREEN cCRE registry"
    if f.startswith("encode_tfbs"):
        return "ENCODE TFBS clusters"
    if f.startswith("fantom_cage"):
        return "FANTOM5 CAGE peaks"
    return None


# The byte-stream codecs a BED result is compared against when we say "the best
# general-purpose codec". Parquet and bgzip are reported separately: Parquet is a
# columnar control rather than a byte codec, and bgzip is the incumbent format
# rather than a strong one.
STRONG_GP = ("zstd", "xz", "xz_mt", "7z", "brotli")
