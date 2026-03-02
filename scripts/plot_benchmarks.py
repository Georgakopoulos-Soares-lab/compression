#!/usr/bin/env python3
"""
Generate benchmark plots for NYX vs baselines across BED, VCF, FASTA, FASTQ.
Reads data from artifacts/benchmark_results.csv for consistency.

Usage:
    python3 scripts/plot_benchmarks.py [--csv path/to/results.csv]
"""

import argparse
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# ── CLI ─────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(description="Plot NYX compression benchmarks")
    p.add_argument("--csv", default=None,
                   help="Path to benchmark_results.csv (default: artifacts/benchmark_results.csv)")
    p.add_argument("--outdir", default=None,
                   help="Output directory for PNGs (default: same dir as CSV)")
    return p.parse_args()

# ── Load data ───────────────────────────────────────────────────────
def load_results(csv_path):
    """Return dict: format -> list of (tool, ratio, seconds, input_bytes, output_bytes)"""
    data = {}
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            fmt = row["format"]
            tool = row["tool"]
            ratio = float(row["ratio"])
            secs = float(row["seconds"])
            inp = int(row["input_bytes"])
            out = int(row["output_bytes"])
            data.setdefault(fmt, []).append((tool, ratio, secs, inp, out))
    return data

# ── Compute MB/s ────────────────────────────────────────────────────
def add_mbps(entries):
    """Return list of (tool, ratio, mbps) from (tool, ratio, secs, inp, out)."""
    result = []
    for tool, ratio, secs, inp, out in entries:
        mbps = (inp / 1_048_576) / secs if secs > 0 else 0
        result.append((tool, ratio, mbps))
    return result

# ── Colors ──────────────────────────────────────────────────────────
NYX_COLOR = "#2563EB"       # blue-600
BASELINE_COLOR = "#94A3B8"  # slate-400

def bar_color(tool):
    return NYX_COLOR if tool == "NYX" else BASELINE_COLOR

# ── Sort by ratio (ascending for horizontal bars) ───────────────────
def sorted_by_ratio(data):
    return sorted(data, key=lambda x: x[1])

# ── Plot helper ─────────────────────────────────────────────────────
def plot_metric(axs, metric_idx, formats, all_data, ylabel):
    """
    metric_idx: 1 = ratio, 2 = mbps
    """
    for col, fmt in enumerate(formats):
        ax = axs[col]
        rows = sorted_by_ratio(add_mbps(all_data[fmt]))
        tools  = [r[0] for r in rows]
        values = [r[metric_idx] for r in rows]
        colors = [bar_color(t) for t in tools]

        y_pos = np.arange(len(tools))
        bars = ax.barh(y_pos, values, color=colors, edgecolor="white", height=0.6)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(tools, fontsize=9)
        ax.set_xlabel(ylabel, fontsize=9)
        ax.set_title(f"{fmt}", fontsize=11, fontweight="bold")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)

        # Value labels
        max_val = max(values) if values else 1
        for bar, val in zip(bars, values):
            if metric_idx == 1:
                label = f"{val:.1f}x" if val < 100 else f"{val:.0f}x"
            else:
                label = f"{val:.0f}"
            offset = max_val * 0.02
            ax.text(bar.get_width() + offset, bar.get_y() + bar.get_height() / 2,
                    label, va="center", fontsize=7.5, color="#334155")

        ax.set_xlim(0, max_val * 1.18)

# ── Main ────────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # Resolve CSV path
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)
    csv_path = args.csv or os.path.join(repo_root, "artifacts", "benchmark_results.csv")
    if not os.path.isfile(csv_path):
        print(f"Error: CSV not found at {csv_path}", file=sys.stderr)
        sys.exit(1)

    outdir = args.outdir or os.path.dirname(csv_path)
    os.makedirs(outdir, exist_ok=True)

    all_data = load_results(csv_path)
    formats = [f for f in ["BED", "VCF", "FASTA", "FASTQ"] if f in all_data]

    print(f"Loaded {sum(len(v) for v in all_data.values())} entries from {csv_path}")

    # ── Figure 1: Compression Ratio ──
    fig1, axs1 = plt.subplots(1, len(formats), figsize=(18, 4.5))
    fig1.suptitle("Compression Ratio (higher is better)", fontsize=14, fontweight="bold", y=1.02)
    plot_metric(axs1, 1, formats, all_data, "Compression Ratio (×)")
    fig1.tight_layout()
    p1 = os.path.join(outdir, "benchmark_ratio.png")
    fig1.savefig(p1, dpi=200, bbox_inches="tight", facecolor="white")
    print(f"Saved: {p1}")

    # ── Figure 2: Compression Speed (MB/s) ──
    fig2, axs2 = plt.subplots(1, len(formats), figsize=(18, 4.5))
    fig2.suptitle("Compression Speed (higher is better)", fontsize=14, fontweight="bold", y=1.02)
    plot_metric(axs2, 2, formats, all_data, "MB/s")
    fig2.tight_layout()
    p2 = os.path.join(outdir, "benchmark_speed.png")
    fig2.savefig(p2, dpi=200, bbox_inches="tight", facecolor="white")
    print(f"Saved: {p2}")

    # ── Figure 3: Combined 2-row layout ──
    fig3, axs3 = plt.subplots(2, len(formats), figsize=(18, 8.5))
    fig3.suptitle("NYX Compression Benchmarks", fontsize=15, fontweight="bold", y=1.01)
    plot_metric(axs3[0], 1, formats, all_data, "Compression Ratio (×)")
    plot_metric(axs3[1], 2, formats, all_data, "MB/s")
    axs3[0][0].set_ylabel("Compression Ratio", fontsize=10, fontweight="bold")
    axs3[1][0].set_ylabel("Compression Speed", fontsize=10, fontweight="bold")
    fig3.tight_layout()
    p3 = os.path.join(outdir, "benchmark_combined.png")
    fig3.savefig(p3, dpi=200, bbox_inches="tight", facecolor="white")
    print(f"Saved: {p3}")

    # ── Print summary table ──
    print("\n" + "=" * 80)
    print("BENCHMARK SUMMARY")
    print("=" * 80)
    for fmt in formats:
        entries = all_data[fmt]
        inp = entries[0][3]
        mib = inp / 1_048_576
        print(f"\n{'─' * 60}")
        print(f"  {fmt}  (input: {inp:,} bytes = {mib:.1f} MiB)")
        print(f"{'─' * 60}")
        print(f"  {'Tool':<14} {'Ratio':>8} {'Time (s)':>10} {'MB/s':>10}")
        print(f"  {'─'*14} {'─'*8} {'─'*10} {'─'*10}")
        rows = add_mbps(entries)
        rows_sorted = sorted(rows, key=lambda x: -x[1])
        for tool, ratio, mbps in rows_sorted:
            sec = mib / mbps if mbps > 0 else 0
            marker = " ◀" if tool == "NYX" else ""
            if ratio >= 100:
                print(f"  {tool:<14} {ratio:>7.0f}× {sec:>10.2f} {mbps:>10.1f}{marker}")
            else:
                print(f"  {tool:<14} {ratio:>7.2f}× {sec:>10.2f} {mbps:>10.1f}{marker}")
    print()


if __name__ == "__main__":
    main()
