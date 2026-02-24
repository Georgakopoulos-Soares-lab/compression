#!/usr/bin/env python3
"""Plot FASTQ compression benchmark: ratio vs time (log-scale y-axis)."""
import argparse, csv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='artifacts/fastq_benchmark.csv')
    ap.add_argument('--out', default='artifacts/fastq_benchmark_plot.png')
    ap.add_argument('--title', default='FASTQ Compression Benchmark\n(ERR9539086, 505 MiB, 3.5M reads)')
    args = ap.parse_args()

    tools, ratios, seconds = [], [], []
    with open(args.csv) as f:
        for row in csv.DictReader(f):
            tools.append(row['tool'].strip('"'))
            ratios.append(float(row['ratio']))
            seconds.append(float(row['seconds']))

    COLOR_MAP = {
        'OpenZL': '#e74c3c',
        'xz': '#3498db', '7z': '#3498db',
        'zstd': '#2ecc71',
        'pigz': '#9b59b6',
        'bgzip': '#f39c12',
    }
    def tool_color(t):
        for k, c in COLOR_MAP.items():
            if k in t:
                return c
        return '#95a5a6'

    colors = [tool_color(t) for t in tools]
    sizes  = [200 if 'OpenZL' in t else 100 for t in tools]

    fig, ax = plt.subplots(figsize=(11, 7))
    ax.scatter(ratios, seconds, c=colors, s=sizes, zorder=5,
               edgecolors='black', linewidths=0.5)
    # Star marker for OpenZL
    for i, t in enumerate(tools):
        if 'OpenZL' in t:
            ax.scatter([ratios[i]], [seconds[i]], c=[colors[i]], s=[250],
                       zorder=6, edgecolors='black', linewidths=0.8, marker='*')

    for i, t in enumerate(tools):
        ox, oy, ha = 0.08, 0, 'left'
        if 'gzip' in t and 'pigz' not in t and 'bgzip' not in t:
            oy = 4
        elif '7z' in t:
            oy = 3
        elif 'xz' in t:
            oy = -5
        elif 'OpenZL' in t:
            oy = 4
        ax.annotate(t, (ratios[i], seconds[i]),
                    textcoords='offset points', xytext=(ox*50, oy+8),
                    fontsize=8.5, ha=ha,
                    fontweight='bold' if 'OpenZL' in t else 'normal')

    ax.set_xlabel('Compression Ratio (higher = better)', fontsize=12)
    ax.set_ylabel('Wall-clock Time (seconds, log scale)', fontsize=12)
    ax.set_title(args.title, fontsize=14)
    ax.set_yscale('log')
    ax.grid(True, alpha=0.3, which='both')
    plt.tight_layout()
    plt.savefig(args.out, dpi=150)
    print(f'Saved {args.out}')

if __name__ == '__main__':
    main()
