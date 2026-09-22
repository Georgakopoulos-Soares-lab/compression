#!/usr/bin/env python3
"""Publication figures for the NYX omics-compression manuscript.

Reads the benchmark CSVs written by scripts/*/benchmark_*.sh, so figures can be
regenerated without re-running any benchmark.

  python3 src/plot_paper_figures.py --results results --out plots

Design notes
------------
Colour carries one thing only: which *family* a tool belongs to (NYX,
format-specific, general-purpose). Individual tools are identified by direct
labels, never by colour alone, so the figures survive greyscale printing and
colour-vision deficiency. The three hues were checked against the
Machado-Oliveira-Fernandes severity-1.0 simulation: worst adjacent pair is
dE=11.0 (deutan) / 13.0 (protan) in OKLab x100, normal-vision worst pair 18.7,
all inside the OKLCH lightness band with contrast >= 3.3 against the surface.
"""
import argparse
import csv
import math
import os
from collections import OrderedDict, defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D
from matplotlib.ticker import LogLocator, NullFormatter, ScalarFormatter

# --- palette (validated; see module docstring) -------------------------------
NYX      = "#D55E00"
GENERAL  = "#0072B2"
SPECIFIC = "#009E73"
INK      = "#1a1a19"
INK_MUTED= "#6b6b68"
GRID     = "#e3e3e0"
SURFACE  = "#fcfcfb"

FAMILY = {
    "nyx": ("NYX (this work)", NYX, "o", 78),
    "nyx_vcf": ("NYX (this work)", NYX, "o", 78),
    "nyx_bed": ("NYX (this work)", NYX, "o", 78),
    "naf-20": ("format-specific", SPECIFIC, "s", 46),
    "naf": ("format-specific", SPECIFIC, "s", 46),
    "spring": ("format-specific", SPECIFIC, "s", 46),
    "genozip": ("format-specific", SPECIFIC, "s", 46),
    "bcf": ("format-specific", SPECIFIC, "s", 46),
}
DEFAULT_FAMILY = ("general-purpose", GENERAL, "^", 46)


def family(tool):
    """Family of a tool name, tolerant of level suffixes such as naf-1/naf-20."""
    t = tool.strip().lower()
    if t in FAMILY:
        return FAMILY[t]
    # A budgeted NYX run (nyx_vcf_2gbbudget) is still NYX, at a different
    # setting; without this it fell through to the default family and was drawn
    # as a general-purpose codec.
    if t.startswith("nyx"):
        return FAMILY.get("nyx", DEFAULT_FAMILY)
    base = t.split("-")[0]
    return FAMILY.get(base, DEFAULT_FAMILY)

PRETTY = {"nyx": "NYX", "nyx_vcf": "NYX", "nyx_bed": "NYX", "nyx_vcf_2gbbudget": "NYX, 2 GB budget", "naf-1": "NAF -1", "naf-20": "NAF -20", "gzip": "gzip",
          "zstd": "zstd", "xz": "xz", "xz_mt": "xz (blocked)", "7z": "7-Zip", "bcf": "BCF",
          "spring": "SPRING", "genozip": "Genozip", "pigz": "pigz",
          "brotli": "brotli", "bgzip": "bgzip", "parquet": "Parquet"}


def sequential_ramp(n):
    """n steps of one hue, light to dark.

    The stages of an ablation are an ordered magnitude, not unrelated
    categories, so this is a sequential ramp rather than a categorical palette.
    Steps are evenly spaced in sRGB between a light and a dark anchor of the
    same hue. Adjacent OKLCH lightness differences stay above the 0.06 floor up
    to n=8 (measured: 0.250 at n=3, 0.098 at n=6, 0.070 at n=8, and 0.054 at
    n=10, which would fail). Beyond 8 steps, facet instead of adding shades.
    """
    if n > 8:
        raise ValueError(f"sequential_ramp: {n} steps cannot stay distinguishable; facet instead")
    lo = (0.839, 0.902, 0.953)      # light anchor
    hi = (0.000, 0.310, 0.510)      # dark anchor, the categorical blue
    if n <= 1:
        return ["#%02x%02x%02x" % tuple(int(round(c * 255)) for c in hi)]
    out = []
    for i in range(n):
        f = i / (n - 1)
        rgb = tuple(lo[k] + (hi[k] - lo[k]) * f for k in range(3))
        out.append("#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb))
    return out


def _place_labels(ax, points):
    """Record direct labels for placement once the figure layout is final.

    Placement used to estimate label widths from a per-character constant, and
    the estimate was wrong often enough to matter: it put "brotli" on "zstd" in
    Figure 4b, then "zstd" on "Parquet" once the constant was widened. Labels are
    now placed by finalize_labels() from measured text extents, after
    tight_layout, so the geometry being tested is the geometry that is saved.
    """
    ax._nyx_labels = list(points)


def finalize_labels(fig):
    """Place every recorded label at the first candidate offset whose rendered
    box overlaps no marker, no other label and does not leave the panel."""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    offsets = [(6, 3, "left", "bottom"), (6, -3, "left", "top"),
               (-6, 3, "right", "bottom"), (-6, -3, "right", "top"),
               (6, 11, "left", "bottom"), (6, -11, "left", "top"),
               (-6, 11, "right", "bottom"), (-6, -11, "right", "top"),
               (0, 9, "center", "bottom"), (0, -9, "center", "top")]
    for ax in fig.axes:
        points = getattr(ax, "_nyx_labels", None)
        if not points:
            continue
        frame = ax.get_window_extent(renderer)
        px = [ax.transData.transform((x, y)) for x, y, _t, _c in points]
        r = 5.5 * fig.dpi / 72                       # marker half-size in pixels
        markers = [(u - r, v - r, u + r, v + r) for u, v in px]
        placed = []

        def clear(bb, own):
            if bb.x0 < frame.x0 or bb.x1 > frame.x1 or bb.y0 < frame.y0 or bb.y1 > frame.y1:
                return False
            for k, m in enumerate(markers):
                if k != own and not (bb.x1 < m[0] or bb.x0 > m[2] or bb.y1 < m[1] or bb.y0 > m[3]):
                    return False
            return all(bb.x1 < q.x0 or bb.x0 > q.x1 or bb.y1 < q.y0 or bb.y0 > q.y1 for q in placed)

        order = sorted(range(len(points)), key=lambda k: -points[k][1])
        for k in order:
            x, y, text, colour = points[k]
            chosen = None
            for dx, dy, ha, va in offsets:
                # A label pushed off its default spot gets a hairline back to its
                # marker, so in a dense panel it cannot be read as its neighbour's.
                lead = (dict(arrowstyle="-", color=INK_MUTED, lw=0.5, shrinkA=1, shrinkB=4)
                        if abs(dy) >= 9 or (dx < 0 and abs(dy) > 3) else None)
                ann = ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx, dy),
                                  ha=ha, va=va, fontsize=7, color=INK, zorder=4,
                                  arrowprops=lead,
                                  fontweight="bold" if colour == NYX else "normal")
                bb = ann.get_window_extent(renderer).expanded(1.06, 1.10)
                if clear(bb, k):
                    chosen = (ann, bb)
                    break
                ann.remove()
            if chosen is None:                        # nothing fits: take the default
                dx, dy, ha, va = offsets[0]
                ann = ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx, dy),
                                  ha=ha, va=va, fontsize=7, color=INK, zorder=4,
                                  fontweight="bold" if colour == NYX else "normal")
                chosen = (ann, ann.get_window_extent(renderer))
            placed.append(chosen[1])
        ax._nyx_labels = None


def style_axes(ax):
    """Recessive grid and axes; the data is the only prominent thing."""
    ax.set_facecolor(SURFACE)
    ax.grid(True, which="major", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=INK_MUTED, labelsize=7.5, length=3, width=0.8)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_color(INK_MUTED)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def fnum(row, key):
    try:
        v = float(row.get(key, "") or "nan")
        return v if math.isfinite(v) else None
    except ValueError:
        return None


def scatter_ratio_vs_speed(ax, rows, size_key, time_key, title, xlabel):
    """Ratio (y) against throughput (x, log). Upper-right is better."""
    seen_families = {}
    pts = []
    for r in rows:
        orig, comp = fnum(r, "orig_bytes"), fnum(r, "comp_bytes")
        secs = fnum(r, time_key)
        if not orig or not comp or not secs or secs <= 0:
            continue
        tool = r["tool"].strip().lower()
        label, colour, marker, size = family(tool)
        pts.append((orig / secs / 1e6, orig / comp, tool, colour, marker, size, label))
    if not pts:
        ax.set_visible(False)
        return seen_families
    placed = []
    for x, y, tool, colour, marker, size, label in pts:
        ax.scatter(x, y, s=size, c=colour, marker=marker, linewidths=0.8,
                   edgecolors=SURFACE, zorder=3)
        seen_families[label] = (colour, marker)
        placed.append((x, y, PRETTY.get(tool, tool), colour))
    ax.set_xscale("log")
    # A narrow data range on a log axis produces a thicket of minor ticks whose
    # labels collide; force a few plain-number majors instead.
    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0), numticks=5))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if max(xs) / max(min(xs), 1e-9) < 3:          # nearly one decade: pad it out
        ax.set_xlim(min(xs) / 1.8, max(xs) * 1.8)
    else:
        ax.set_xlim(min(xs) / 1.35, max(xs) * 1.6)
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.16 if hi > lo else max(hi * 0.08, 0.05)
    ax.set_ylim(lo - pad, hi + pad * 1.3)
    _place_labels(ax, placed)          # after the scale and limits are final
    ax.set_title(title, fontsize=8.5, color=INK, loc="left", pad=6)
    ax.set_xlabel(xlabel, fontsize=7.5, color=INK_MUTED)
    ax.set_ylabel("compression ratio (x)", fontsize=7.5, color=INK_MUTED)
    style_axes(ax)
    return seen_families


# The file each Figure 1 column plots. The manuscript's prose quotes the same
# ones (paper_drafts/build/revise_manuscript.py reads this list), so the figure
# and the text cannot describe different files. Chosen to be representative
# rather than extreme: a mammalian assembly and a standard NovaSeq run, not the
# two inputs we do worst on.
REPRESENTATIVE = {
    "FASTA": "GRCh38.p14.fa",
    "FASTQ": "ERR9539086.fastq",
    "VCF":   "1000G.chr20.phase3.vcf",
    # Not the largest BED file but the one the prose discusses: a chromatin-state
    # segmentation is where the cross-column references do the most work, and a
    # panel showing the file where the transform matters least would be odd.
    "BED":   "E003_15_coreMarks_dense.bed",
}


def fig_ratio_throughput(results, out):
    """Figure 1: ratio against throughput, one column per format."""
    panels = [
        ("FASTA", read_csv(os.path.join(results, "fasta_bench.csv"))),
        ("FASTQ", read_csv(os.path.join(results, "fastq_bench.csv"))),
        ("VCF",   read_csv(os.path.join(results, "vcf_bench.csv"))),
        ("BED",   read_csv(os.path.join(results, "bed_bench.csv"))),
    ]
    panels = [(n, r) for n, r in panels if r]
    if not panels:
        print("  (no benchmark CSVs yet — skipping Figure 1)")
        return
    # One row per format, compression on the left and decompression on the
    # right, so a reader compares a format's two panels side by side and the
    # formats down the page. (Previously two rows of four; changed at the
    # authors' request.)
    fig, axes = plt.subplots(len(panels), 2, figsize=(7.0, 2.55 * len(panels)),
                             facecolor=SURFACE)
    axes = axes.reshape(len(panels), 2)
    fams = {}
    letters = "abcdefgh"
    for row, (name, rows) in enumerate(panels):
        # One representative file per format. This has to be named, not derived:
        # "the largest" put bread wheat in panel a and SRR8899104 in panel b,
        # while the prose discussed GRCh38 and ERR9539086, so the figure and the
        # text described different measurements. Both now read from REPRESENTATIVE.
        by_file = defaultdict(list)
        for r in rows:
            by_file[r["file"].strip('"')].append(r)
        want = REPRESENTATIVE.get(name)
        if want and want in by_file:
            target = by_file[want]
        else:
            target = max(by_file.values(), key=lambda rs: fnum(rs[0], "orig_bytes") or 0)
        if name == "BED":
            # pigz -9 and bgzip are both deflate and land on one point; keep the
            # incumbent file format, as Figure 4 does.
            target = [r for r in target if r["tool"].strip() != "gzip"]
        fname = target[0]["file"].strip('"')
        for col_i, (tkey, what) in enumerate([("comp_sec", "compression"),
                                              ("decomp_sec", "decompression")]):
            ax = axes[row, col_i]
            f = scatter_ratio_vs_speed(
                ax, target, "orig_bytes", tkey,
                f"{letters[row * 2 + col_i]}   {name} · {what}",
                f"{what} throughput (MB/s, log)")
            fams.update(f)
        axes[row, 0].text(0.0, 1.16, fname, transform=axes[row, 0].transAxes,
                          fontsize=6.5, color=INK_MUTED)
    handles = [Line2D([], [], marker=m, color="none", markerfacecolor=c,
                      markeredgecolor=SURFACE, markersize=7, label=l)
               for l, (c, m) in sorted(fams.items())]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               fontsize=7.5, labelcolor=INK, bbox_to_anchor=(0.5, -0.002))
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    finalize_labels(fig)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"fig1_ratio_throughput.{ext}"), dpi=400,
                    facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("  fig1_ratio_throughput")


def memory_panels(results):
    """(format, file label, {tool: (ratio, peak GB)}) for Figure 2 -- shared with the
    manuscript generator, so the caption's claims are computed from what is drawn."""
    def load(fn):
        return read_csv(os.path.join(results, fn))

    def pick(rows, fname, drop=()):
        out = {}
        for r in rows:
            if r["file"].strip('"') == fname and r["tool"].strip() not in drop:
                if fnum(r, "peak_RSS_MB") and fnum(r, "ratio"):
                    out[r["tool"].strip()] = (fnum(r, "ratio"), fnum(r, "peak_RSS_MB") / 1024.0)
        return out

    panels = []
    fa = pick(load("fasta_bench.csv"), "GRCh38.p14.fa")
    fa.update(pick(load("memory_gp_baselines.csv"), "GRCh38.p14.fa"))
    panels.append(("FASTA", "GRCh38.p14", fa))
    fq = pick(load("fastq_bench.csv"), "ERR9539086.fastq")
    fq.update(pick(load("memory_gp_baselines.csv"), "ERR9539086.fastq"))
    panels.append(("FASTQ", "ERR9539086", fq))
    vc = pick(load("vcf_memory_vs_baselines.csv"), "1000G.chr22.phase3.vcf")
    vc.update({k: v for k, v in pick(load("vcf_bench.csv"), "1000G.chr22.phase3.vcf").items()
               if k not in vc})
    panels.append(("VCF", "1000G chr22", vc))
    bd = pick(load("bed_bench.csv"), "E003_15_coreMarks_dense.bed", drop=("gzip",))
    panels.append(("BED", "E003_15_coreMarks_dense", bd))
    return [p for p in panels if len(p[2]) >= 2]


def fig_memory(results, out):
    """Figure 2: compression ratio against peak memory, one file per format.

    The previous version plotted peak memory against input size. Lower was
    better on that axis, but every other figure in the paper puts better upward,
    and the FASTQ panel -- NYX at 2 GB under SPRING at 6-8 GB, and nothing else,
    because the general-purpose codecs' memory had never been measured -- read
    as a loss. The BED panel meanwhile had one marker per tool per file, over
    300, which nobody could read.

    Ratio against memory answers the question memory is actually asked in: what
    does a given ratio cost? Upper-left is better, as upper-right is in Figure 1.
    Each panel is the same representative file Figure 1 uses, except VCF, where
    peak memory for every tool was measured on chr22 (results/
    vcf_memory_vs_baselines.csv) rather than chr20.
    """
    panels = memory_panels(results)
    if not panels:
        print("  (no peak-RSS columns yet -- skipping Figure 2)")
        return

    ncol = 2
    nrow = (len(panels) + 1) // 2
    fig, axes = plt.subplots(nrow, ncol, figsize=(7.0, 2.9 * nrow), facecolor=SURFACE,
                             squeeze=False)
    fams = {}
    for k, (title, fname, pts) in enumerate(panels):
        ax = axes[k // ncol][k % ncol]
        placed = []
        for tool, (ratio, gb) in sorted(pts.items()):
            label, colour, marker, size = family(tool)
            budgeted = "budget" in tool.lower()
            ax.scatter(gb, ratio, s=size, marker=marker, zorder=3,
                       facecolors=SURFACE if budgeted else colour,
                       edgecolors=colour if budgeted else SURFACE,
                       linewidths=1.4 if budgeted else 0.8)
            fams.setdefault(label, (colour, marker))
            placed.append((gb, ratio, PRETTY.get(tool.lower(), tool), colour))
        ax.set_xscale("log")
        from matplotlib.ticker import FuncFormatter
        ax.xaxis.set_major_formatter(FuncFormatter(
            lambda v, _: f"{v*1000:g} MB" if v < 1 else f"{v:g} GB"))
        ax.xaxis.set_minor_formatter(NullFormatter())
        xs = [p[0] for p in placed]; ys = [p[1] for p in placed]
        ax.set_xlim(min(xs) / 2.2, max(xs) * 2.2)
        lo, hi = min(ys), max(ys)
        pad = (hi - lo) * 0.16 if hi > lo else hi * 0.1
        ax.set_ylim(max(0, lo - pad), hi + pad * 1.4)
        _place_labels(ax, placed)
        ax.set_title(f"{'abcd'[k]}   {title} · {fname}", fontsize=8.5, color=INK, loc="left", pad=6)
        ax.set_xlabel("peak resident memory (log)", fontsize=7.5, color=INK_MUTED)
        ax.set_ylabel("compression ratio (x)", fontsize=7.5, color=INK_MUTED)
        style_axes(ax)
    for k in range(len(panels), nrow * ncol):
        axes[k // ncol][k % ncol].set_visible(False)
    handles = [Line2D([], [], marker=m, color="none", markerfacecolor=c,
                      markeredgecolor=SURFACE, markersize=7, label=l)
               for l, (c, m) in sorted(fams.items())]
    if any("budget" in t.lower() for _, _, pts in panels for t in pts):
        handles.append(Line2D([], [], marker="o", color="none", markerfacecolor=SURFACE,
                              markeredgecolor=NYX, markersize=7,
                              label="NYX under --max-mem-mb 2000"))
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               fontsize=7.5, labelcolor=INK, bbox_to_anchor=(0.5, -0.004))
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    finalize_labels(fig)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"fig2_memory.{ext}"), dpi=400,
                    facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("  fig2_memory")


def fig_ablation(results, out):
    """Figure 3: what each VCF transform is worth, as small multiples.

    One panel per archetype with its own y-scale. A single shared scale is
    unusable here: the panel file reaches 374x while a gVCF reaches 18x, so on
    one axis every archetype but the panel collapses to the baseline.
    """
    rows = read_csv(os.path.join(results, "vcf_ablation.csv"))
    if not rows:
        print("  (no vcf_ablation.csv yet - skipping Figure 3)")
        return
    stages, per, arch = [], OrderedDict(), {}
    for r in rows:
        st = r["stage"]
        if st not in stages:
            stages.append(st)
        f = r["file"].strip('"')
        ratio = fnum(r, "ratio")
        if ratio:
            per.setdefault(f, {})[st] = ratio
            arch[f] = r.get("archetype", "")
    files = list(per)
    if not files:
        return
    ramp = sequential_ramp(len(stages))
    ncol = min(3, len(files))
    nrow = (len(files) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(2.55 * ncol, 2.25 * nrow),
                             facecolor=SURFACE, squeeze=False)
    for k, f in enumerate(files):
        ax = axes[k // ncol][k % ncol]
        ys = [per[f].get(st, 0) for st in stages]
        ax.bar(range(len(stages)), ys, width=0.78, color=ramp,
               edgecolor=SURFACE, linewidth=0.8, zorder=3)
        # the two numbers a reader wants: where it starts and where it ends
        ax.annotate(f"{ys[0]:.1f}x", (0, ys[0]), textcoords="offset points",
                    xytext=(0, 3), ha="center", fontsize=6.5, color=INK_MUTED)
        ax.annotate(f"{ys[-1]:.1f}x", (len(stages) - 1, ys[-1]),
                    textcoords="offset points", xytext=(0, 3), ha="center",
                    fontsize=7, color=INK, fontweight="bold")
        gain = ys[-1] / ys[0] if ys[0] else 0
        # two files can share an archetype, so name the file too
        short = f.replace(".vcf", "").replace(".g", "").replace("_GRCh38", "")
        short = short.split("_v4")[0][:22]
        ax.set_title(f"{arch.get(f, '')} - {short}\n{gain:.1f}x over generic",
                     fontsize=7.5, color=INK, loc="left", pad=4)
        ax.set_xticks([])
        ax.set_ylim(0, max(ys) * 1.25)
        ax.tick_params(labelsize=6.5)
        style_axes(ax)
        if k % ncol == 0:
            ax.set_ylabel("compression ratio (x)", fontsize=7, color=INK_MUTED)
    for k in range(len(files), nrow * ncol):
        axes[k // ncol][k % ncol].set_visible(False)
    handles = [mpatches.Patch(facecolor=ramp[i], edgecolor=SURFACE, label=st)
               for i, st in enumerate(stages)]
    fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False,
               fontsize=7, labelcolor=INK, bbox_to_anchor=(0.5, -0.02))
    fig.tight_layout(rect=(0, 0.14, 1, 1))
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"fig3_vcf_ablation.{ext}"), dpi=400,
                    facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("  fig3_vcf_ablation")


def fig_bed(results, out):
    """Figure 4: BED, across the whole corpus rather than one file.

    Figure 1 already has a BED column, but it shows one representative file.
    What BED needs shown is different: that the gain holds on every file and at
    every column width, because the argument for this codec is that it has no
    allow-list of layouts. Panel a therefore plots NYX's advantage over the best
    general-purpose codec for each of the files, grouped by dialect with its
    width. Panel b puts every tool on one ratio-throughput plane over the whole
    corpus, which is where a per-file concession (multi-threaded xz is faster on
    the small files) and a corpus-level result (it is not faster in total) can
    both be seen.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from bed_dialects import DIALECTS, dialect, STRONG_GP

    rows = read_csv(os.path.join(results, "bed_bench.csv"))
    if not rows:
        print("  (no bed_bench.csv yet - skipping Figure 4)")
        return
    by = defaultdict(dict)
    for r in rows:
        if r.get("roundtrip", "OK") == "OK":
            by[r["file"].strip('"')][r["tool"].strip()] = r

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.2, 3.2), facecolor=SURFACE,
                                   gridspec_kw={"width_ratios": [1.15, 1]})

    # --- a: per-file advantage over the best byte-stream codec, by dialect ---
    # Horizontal: eight dialect names with their widths read straight across,
    # where rotated tick labels on a vertical strip plot collided.
    order = [d for d, _ in DIALECTS]
    width = dict(DIALECTS)
    groups = defaultdict(list)
    for f, t in by.items():
        d = dialect(f)
        n = t.get("nyx_bed")
        rivals = [fnum(t[k], "ratio") for k in STRONG_GP if k in t and fnum(t[k], "ratio")]
        if d is None or not n or not rivals:
            continue
        groups[d].append(fnum(n, "ratio") / max(rivals))
    order = [d for d in order if groups.get(d)][::-1]     # narrowest at the top
    for i, d in enumerate(order):
        vals = groups[d]
        k = len(vals)
        # alternate small vertical offsets in file order, not value order, so the
        # spread cannot be read as a trend
        ys = [i + ((j % 5) - 2) * 0.11 for j in range(k)]
        ax1.scatter(vals, ys, s=30, c=NYX, marker="o", edgecolors=SURFACE,
                    linewidths=0.9, zorder=3)
    ax1.axvline(1.0, color=INK_MUTED, linewidth=0.9, linestyle=(0, (3, 3)), zorder=2)
    ax1.set_yticks(range(len(order)))
    ax1.set_yticklabels([f"{d}  ({width[d]} col, n={len(groups[d])})" for d in order],
                        fontsize=6.8)
    ax1.set_ylim(-0.7, len(order) - 0.3)
    allv = [v for d in order for v in groups[d]]
    ax1.set_xlim(0.9, max(allv) * 1.06)
    ax1.text(1.0, len(order) - 0.45, " parity", ha="left", va="bottom",
             fontsize=6.5, color=INK_MUTED)
    ax1.set_xlabel("NYX ratio / best general-purpose ratio", fontsize=7.5, color=INK_MUTED)
    ax1.set_title(f"a   every file, by dialect ({len(allv)} files)", fontsize=8.5,
                  color=INK, loc="left", pad=6)
    style_axes(ax1)
    for lbl in ax1.get_yticklabels():
        lbl.set_color(INK)
        lbl.set_fontsize(6.8)

    # --- b: whole-corpus ratio against compression throughput, per tool -----
    files = [f for f, t in by.items() if "nyx_bed" in t]
    agg = []
    for tool in sorted({k for t in by.values() for k in t} - {"gzip"}):
        have = [by[f][tool] for f in files if tool in by[f]]
        if len(have) != len(files):
            continue                      # a tool must cover the same files
        o = sum(fnum(r, "orig_bytes") for r in have)
        c = sum(fnum(r, "comp_bytes") for r in have)
        sec = sum(fnum(r, "comp_sec") for r in have)
        if sec <= 0:
            continue
        agg.append({"tool": tool, "orig_bytes": o, "comp_bytes": c, "comp_sec": sec})
    fams = scatter_ratio_vs_speed(
        ax2, agg, "orig_bytes", "comp_sec",
        f"b   whole corpus ({sum(a['orig_bytes'] for a in agg[:1]) / 1e9:.2f} GB)",
        "compression throughput (MB/s, log)")

    handles = [Line2D([], [], marker=m, color="none", markerfacecolor=c,
                      markeredgecolor=SURFACE, markersize=7, label=l)
               for l, (c, m) in sorted(fams.items())]
    fig.legend(handles=handles, loc="lower center", ncol=len(handles), frameon=False,
               fontsize=7.5, labelcolor=INK, bbox_to_anchor=(0.5, -0.01))
    fig.tight_layout(rect=(0, 0.05, 1, 1))
    finalize_labels(fig)
    for ext in ("pdf", "png"):
        fig.savefig(os.path.join(out, f"fig4_bed.{ext}"), dpi=400,
                    facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("  fig4_bed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default="plots")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    plt.rcParams.update({
        "font.family": "DejaVu Sans",
        "pdf.fonttype": 42,      # embed as TrueType so the PDF is editable
        "ps.fonttype": 42,
        "savefig.facecolor": SURFACE,
        "figure.facecolor": SURFACE,
    })
    print("writing figures to", a.out)
    fig_ratio_throughput(a.results, a.out)
    fig_memory(a.results, a.out)
    fig_ablation(a.results, a.out)
    fig_bed(a.results, a.out)


if __name__ == "__main__":
    main()
