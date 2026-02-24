#!/usr/bin/env python3
"""Plot VCF compression benchmark: ratio vs time scatter."""
import argparse, csv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default='artifacts/results_vcf_clinvar.csv')
    ap.add_argument('--out', default='artifacts/vcf_clinvar_benchmark_plot.png')
    ap.add_argument('--title', default='VCF Compression Benchmark — ClinVar (500 MiB table)')
    args = ap.parse_args()

    tools, ratios, seconds = [], [], []
    with open(args.csv) as f:
        for row in csv.DictReader(f):
            tools.append(row['tool'].strip('"'))
            ratios.append(float(row['ratio']))
            seconds.append(float(row['seconds']))

    colors = ['#e63946' if 'OpenZL' in t else '#457b9d' for t in tools]

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.scatter(ratios, seconds, c=colors, s=120, zorder=5,
               edgecolors='black', linewidths=0.5)

    for i, t in enumerate(tools):
        ox, oy, ha = 0.08, 0.5, 'left'
        if 'bgzip -l2' in t:
            oy = -1.5
        if '7z' in t:
            ox, ha, oy = -0.08, 'right', 1.0
        if 'xz' in t:
            oy = -1.8
        if 'OpenZL' in t:
            oy = 1.5
        ax.annotate(t, (ratios[i], seconds[i]),
                    textcoords='offset points',
                    xytext=(ox * 72, oy * 5),
                    fontsize=8.5, ha=ha, va='bottom',
                    fontweight='bold' if 'OpenZL' in t else 'normal',
                    color='#e63946' if 'OpenZL' in t else '#1d3557')

    ax.set_xlabel('Compression Ratio (higher = better)', fontsize=12, fontweight='bold')
    ax.set_ylabel('Time (seconds, lower = better)', fontsize=12, fontweight='bold')
    ax.set_title(args.title, fontsize=14, fontweight='bold')
    ax.annotate('Best ↘', xy=(0.97, 0.03), xycoords='axes fraction',
                fontsize=10, color='green', ha='right', fontweight='bold')
    ax.grid(True, alpha=0.3)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    plt.tight_layout()
    plt.savefig(args.out, dpi=150, bbox_inches='tight')
    print(f'Plot saved to {args.out}')

if __name__ == '__main__':
    main()
