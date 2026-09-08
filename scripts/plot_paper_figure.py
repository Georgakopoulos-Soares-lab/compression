#!/usr/bin/env python3
"""Regenerate the manuscript figure from results/paper/.

Compression (a-c) and decompression (d-f) throughput against compression ratio,
one column per format. Reads the CSVs in results/paper/ so the figure always
tracks the reported numbers rather than hard-coded values.

    python3 scripts/plot_paper_figure.py [-o paper/fig1_combined.png]
"""
import argparse, csv, math, os, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedLocator, NullLocator, FuncFormatter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "results", "paper")
OURS, GP, DS = "#D55E00", "#0072B2", "#009E73"
COL = {"ours": OURS, "gp": GP, "ds": DS}
MUT, INK = "#5b6472", "#16191f"

# dataset plotted per format, and how each CSV names its columns
FASTA_FILE = "GCF_000001635.27_GRCm39_genomic.fna"          # GRCm39
FASTQ_FILE = "ERR9539086"
VCF_FILE   = "HG002_GRCh38_1_22_v4.2.1_benchmark.vcf"
NAME = {"our_method_openzl": "NYX", "ours": "NYX", "our_method_vcfzl": "NYX",
        "7zip": "7z", "spring": "SPRING"}
GROUP = {"NYX": "ours", "SPRING": "ds"}

# Manual label nudges, (dx, dy, ha) in points, so the published figure is
# reproducible exactly. Anything not listed uses the default above-centre.
OFF = {
    ("FASTQ", "c", "7z"): (-8, -3, "right"), ("FASTQ", "c", "xz"): (9, -3, "left"),
    ("FASTQ", "c", "SPRING"): (0, -13, "center"),
    ("FASTQ", "d", "7z"): (0, -13, "center"), ("FASTQ", "d", "xz"): (9, -3, "left"),
    ("FASTQ", "d", "SPRING"): (0, -13, "center"), ("FASTQ", "d", "gzip"): (0, -13, "center"),
    ("FASTA", "c", "zstd"): (-9, -3, "right"), ("FASTA", "c", "7z"): (9, -3, "left"),
    ("FASTA", "d", "gzip"): (0, -13, "center"), ("FASTA", "d", "xz"): (0, -13, "center"),
    ("FASTA", "d", "7z"): (9, -3, "left"),
    ("VCF", "c", "gzip"): (0, -13, "center"), ("VCF", "c", "7z"): (0, -13, "center"),
    ("VCF", "c", "xz"): (9, -3, "left"),
}


def rows_for(path, key_col, want, cols):
    """(label, ratio, comp_s, decomp_s, group) for one dataset in one CSV."""
    out = []
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if want not in r[key_col]:
                continue
            tool = r[cols["tool"]]
            label = NAME.get(tool, tool)
            try:
                ratio = float(r[cols["ratio"]])
                comp = float(r[cols["comp"]])
            except (ValueError, KeyError):
                continue
            try:
                dec = float(r[cols["decomp"]])
            except (ValueError, KeyError, TypeError):
                dec = None
            mb = int(r[cols["orig"]]) / 1e6
            out.append((label, ratio, mb / comp, (mb / dec) if dec else None,
                        GROUP.get(label, "gp")))
    return sorted(out, key=lambda x: x[1])


def style(ax, title):
    ax.set_title(title, fontsize=10, fontweight="bold", loc="left", color=INK, pad=7)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#9aa3b0")


def panel(ax, rows, which, title, ylab, fmt=""):
    pts = [(n, r, c if which == "c" else d, g) for n, r, c, d, g in rows]
    pts = [(n, r, v, g) for n, r, v, g in pts if v]
    if not pts:
        ax.set_xticks([]); ax.set_yticks([])
        ax.text(0.5, 0.5, "decompression not timed\nin this run", ha="center", va="center",
                fontsize=8.5, color="#9aa3b0", style="italic", transform=ax.transAxes)
        style(ax, title)
        for s in ("left", "bottom"):
            ax.spines[s].set_color("#d9dde4")
        return
    xs = [p[1] for p in pts]; ys = [p[2] for p in pts]
    xpad = (max(xs) - min(xs)) * 0.12 or 1
    xlim = (min(xs) - xpad, max(xs) + xpad)
    ylim = (min(ys) / 2.2, max(ys) * 2.2)
    for n, r, v, g in pts:
        big = g == "ours"
        ax.scatter([r], [v], s=88 if big else 55, c=COL[g], zorder=3,
                   edgecolors="white", linewidths=1.3)
        dx, dy, ha = OFF.get((fmt, which, n), (-9, -4, "right") if big else (0, 8, "center"))
        ax.annotate(n, (r, v), textcoords="offset points", xytext=(dx, dy),
                    ha=ha, fontsize=9 if big else 8,
                    color=COL[g] if big else MUT,
                    fontweight="bold" if big else "normal", zorder=4)
    ax.set_yscale("log"); ax.set_xlim(*xlim); ax.set_ylim(*ylim)
    tk, dd = [], math.floor(math.log10(ylim[0]))
    while 10 ** dd <= ylim[1] * 1.001:
        for m in (1, 2, 5):
            v = m * 10 ** dd
            if ylim[0] * 0.999 <= v <= ylim[1] * 1.001:
                tk.append(v)
        dd += 1
    ax.yaxis.set_major_locator(FixedLocator(tk)); ax.yaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}" if v < 1000 else f"{v/1000:g}k"))
    ax.set_xlabel("Compression ratio (×)", fontsize=8.5, color=MUT)
    if ylab:
        ax.set_ylabel("Throughput (MB/s)", fontsize=8.5, color=MUT)
    ax.grid(True, which="major", color="#dfe3e9", lw=0.7, zorder=0)
    ax.tick_params(labelsize=7.5, colors=MUT)
    style(ax, title)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "paper", "fig1_combined.png"))
    a = ap.parse_args()

    fasta = rows_for(os.path.join(RES, "fasta_whole_file_3458801.csv"), "file", FASTA_FILE,
                     dict(tool="tool", ratio="ratio", comp="comp_sec",
                          decomp="decomp_sec", orig="orig_bytes"))
    fastq = rows_for(os.path.join(RES, "fastq_whole_file_3458771.csv"), "file", FASTQ_FILE,
                     dict(tool="method", ratio="ratio", comp="compress_s",
                          decomp="decompress_s", orig="raw_bytes"))
    vcf = rows_for(os.path.join(RES, "vcf_corpus_3462183.csv"), "file", VCF_FILE,
                   dict(tool="method", ratio="ratio", comp="comp_sec",
                        decomp="decomp_sec", orig="orig_bytes"))
    fasta = [r for r in fasta if r[0] != "harc_openzl"]      # ablation, not a competitor

    f, ax = plt.subplots(2, 3, figsize=(10.4, 6.3), dpi=300)
    for col, (rows, fmt) in enumerate(((fasta, "FASTA"), (fastq, "FASTQ"), (vcf, "VCF"))):
        panel(ax[0][col], rows, "c", f"{'abc'[col]}   {fmt} · compression", col == 0, fmt)
        panel(ax[1][col], rows, "d", f"{'def'[col]}   {fmt} · decompression", col == 0, fmt)

    h = [Line2D([], [], marker="o", ls="", ms=8, mfc=OURS, mec="white", label="NYX (this work)"),
         Line2D([], [], marker="o", ls="", ms=7, mfc=DS, mec="white", label="format-specific (SPRING)"),
         Line2D([], [], marker="o", ls="", ms=7, mfc=GP, mec="white", label="general-purpose")]
    f.legend(handles=h, loc="lower center", ncol=3, frameon=False, fontsize=9,
             bbox_to_anchor=(0.5, -0.005))
    f.patch.set_facecolor("white"); f.tight_layout(rect=[0, 0.05, 1, 1])
    os.makedirs(os.path.dirname(a.output), exist_ok=True)
    f.savefig(a.output, dpi=300, facecolor="white", bbox_inches="tight")
    print("wrote", a.output)


if __name__ == "__main__":
    sys.exit(main())
