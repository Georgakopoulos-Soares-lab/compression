#!/usr/bin/env python3
"""Multi-axis analysis of the benchmark CSVs: where NYX wins, loses, and dominates.

  python3 src/multi_axis.py --results results --out results/multi_axis.md

Ranking a compressor axis-by-axis against the *best* baseline on each axis is
misleading. The fastest baseline is almost always gzip, so "fastest tool" is a
question about gzip, not about us; and the smallest output is often a codec that
spends an hour to get there. Reported on its own, either number tells a reader
nothing about the trade a user actually makes.

So this script reports three things per input:

  1. per-axis win/loss against every baseline individually (the raw facts),
  2. win/loss against the two strong general-purpose codecs, zstd -19 and
     xz -9e, which is the comparison a general reader has in mind, and
  3. **Pareto dominance**: is there a single tool that beats NYX on ratio AND
     on compression speed at once? That is the claim that survives contact with
     a reviewer, because it does not let a tool borrow gzip's speed to argue
     about xz's ratio.

Only rows whose round trip verified are counted. A tool that does not reproduce
its input is not a data point.
"""
import argparse
import csv
import os
from collections import OrderedDict

NYX = {"nyx", "nyx_vcf", "nyx_bed"}
# The strong general-purpose codecs. "Strong" matters: beating gzip is not an
# argument, and neither is being faster than it. 7-Zip is LZMA2 like xz but is
# the stronger of the two on some inputs, so both are required.
# xz appears twice in the BED table: at its default block size and with one
# small enough that it can use the threads it was given. Both count.
GENERAL = ("zstd", "xz", "xz_mt", "7z")
# Baseline peak RSS lives in its own CSV where it was measured separately.
MEMORY = {"VCF": "vcf_memory_vs_baselines.csv"}


def load(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def group(rows):
    """{file: {tool: row}} keeping only verified round trips."""
    by_file = OrderedDict()
    for r in rows:
        if r.get("roundtrip", "OK") not in ("OK", ""):
            continue
        by_file.setdefault(r["file"].strip('"'), {})[r["tool"]] = r
    return by_file


def merge_memory(by_file, mem_rows):
    """Fold a memory-only benchmark into the main grouping.

    Baseline peak RSS is measured in a separate, shorter run (the main VCF table
    predates it), so the memory axis is blank in the main CSV. Copy the peak in
    where the file and tool line up; leave everything else alone, so ratio and
    time still come from the run that measured them.
    """
    for r in mem_rows:
        per = by_file.get(r["file"].strip('"'))
        if per is None:
            continue
        tool, pk = r["tool"], r.get("peak_RSS_MB")
        if tool in per and pk and not num(per[tool].get("peak_RSS_MB")):
            per[tool] = dict(per[tool], peak_RSS_MB=pk)
        elif tool not in per and tool.startswith("nyx"):
            # NYX under an explicit budget is a separate configuration, not a
            # baseline; keep it out of the dominance test but report it.
            per.setdefault("_budgeted", {})[tool] = r
    return by_file


def analyse(rows, fmt, mem_rows=()):
    """Returns (per-file records, totals) for one format."""
    recs, tot = [], {
        "n": 0, "pareto": 0,
        "ratio_gp": 0, "speed_gp": 0, "both_gp": 0,
        "ratio_any": 0, "mem_any": 0, "mem_n": 0,
    }
    for fname, tools in merge_memory(group(rows), mem_rows).items():
        budgeted = tools.pop("_budgeted", {})
        nyx = next((tools[t] for t in tools if t in NYX), None)
        if not nyx:
            continue
        n_ratio, n_sec = num(nyx["ratio"]), num(nyx["comp_sec"])
        if not n_ratio or not n_sec:
            continue
        n_mem = num(nyx.get("peak_RSS_MB"))

        # A tool DOMINATES us if it is at least as good on both axes and
        # strictly better on one. Ties do not count as a loss.
        dominators, beats_ratio, gp_ratio, gp_speed = [], [], [], []
        for t, r in tools.items():
            if t in NYX:
                continue
            ratio, sec = num(r["ratio"]), num(r["comp_sec"])
            if not ratio or not sec:
                continue
            if ratio >= n_ratio and sec <= n_sec and (ratio > n_ratio or sec < n_sec):
                dominators.append(t)
            if ratio > n_ratio:
                beats_ratio.append(t)
            if t in GENERAL:
                gp_ratio.append(n_ratio > ratio)
                gp_speed.append(n_sec < sec)

        # Memory is only comparable where the baseline was actually measured.
        mems = [(t, num(r.get("peak_RSS_MB"))) for t, r in tools.items() if t not in NYX]
        mems = [(t, m) for t, m in mems if m]
        # If a budgeted NYX run exists, memory is a dial the user sets, so the
        # fair question is what the dial can reach -- not where the default lands.
        best_mem, best_lbl = n_mem, "default"
        for lbl, r in budgeted.items():
            m = num(r.get("peak_RSS_MB"))
            if m and (best_mem is None or m < best_mem):
                best_mem, best_lbl = m, lbl
        # gzip is always the lightest and always far worse on ratio, so "lighter
        # than gzip" is not a question anyone is asking. Compare against every
        # other baseline whose memory was actually measured, and report nothing
        # at all where none was -- an unmeasured axis is not a loss.
        mem_win = None
        strong = [(t, m) for t, m in mems if t != "gzip"]
        if best_mem and strong:
            mem_win = all(best_mem < m for _, m in strong)
            tot["mem_n"] += 1
            tot["mem_any"] += bool(mem_win)

        tot["n"] += 1
        tot["pareto"] += not dominators
        tot["ratio_any"] += not beats_ratio
        r_gp, s_gp = bool(gp_ratio) and all(gp_ratio), bool(gp_speed) and all(gp_speed)
        tot["ratio_gp"] += r_gp
        tot["speed_gp"] += s_gp
        tot["both_gp"] += r_gp and s_gp
        recs.append({
            "file": fname, "fmt": fmt, "ratio": n_ratio, "sec": n_sec, "mem": n_mem,
            "dominators": dominators, "beats_ratio": beats_ratio,
            "gp_ratio": r_gp, "gp_speed": s_gp, "mem_win": mem_win,
            "best_mem": best_mem, "best_mem_lbl": best_lbl,
        })
    return recs, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="results")
    ap.add_argument("--out", default="results/multi_axis.md")
    a = ap.parse_args()

    sources = [("VCF", "vcf_bench.csv"), ("FASTQ", "fastq_bench.csv"),
               ("FASTA", "fasta_bench.csv"), ("BED", "bed_bench.csv")]

    all_recs, all_tot, lines = [], {}, []
    for fmt, csvname in sources:
        rows = load(os.path.join(a.results, csvname))
        if not rows:
            continue
        mem_rows = load(os.path.join(a.results, MEMORY.get(fmt, "")))
        recs, tot = analyse(rows, fmt, mem_rows)
        if not recs:
            continue
        all_recs += recs
        all_tot[fmt] = tot

        lines.append(f"\n## {fmt}\n")
        lines.append("| input | NYX ratio | comp s | best peak GB | beaten on ratio by | "
                     "beats zstd+xz on ratio | on speed | lightest | Pareto-optimal |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in sorted(recs, key=lambda x: -x["ratio"]):
            mem = f"{r['best_mem']/1024:.2f}" if r["best_mem"] else "-"
            if r["best_mem_lbl"] != "default":
                mem += " (budget)"
            lost = ", ".join(r["beats_ratio"]) or "-"
            mw = {True: "yes", False: "NO", None: "-"}[r["mem_win"]]
            lines.append(
                f"| {r['file']} | {r['ratio']:.2f}x | {r['sec']:.0f} | {mem} | {lost} | "
                f"{'yes' if r['gp_ratio'] else 'NO'} | {'yes' if r['gp_speed'] else 'NO'} | {mw} | "
                f"{'yes' if not r['dominators'] else 'no (' + ', '.join(r['dominators']) + ')'} |")
        n = tot["n"]
        lines.append(
            f"\n{fmt}: Pareto-optimal on **{tot['pareto']}/{n}**; highest ratio of any "
            f"codec on {tot['ratio_any']}/{n}; beats zstd -19 and xz -9e on ratio "
            f"{tot['ratio_gp']}/{n}, on compression speed {tot['speed_gp']}/{n}, on both "
            f"{tot['both_gp']}/{n}" +
            (f"; lighter than both on peak memory on {tot['mem_any']}/{tot['mem_n']}"
             if tot["mem_n"] else "; baseline memory not measured") + ".")

    # Thread scaling, if measured: the one-thread row is what answers "is NYX
    # faster than xz -9e, or just more parallel?".
    scale = load(os.path.join(a.results, "thread_scaling.csv"))
    if scale:
        lines.append("\n## Thread scaling\n")
        lines.append("| format | input | threads | ratio | comp s | MB/s | peak GB |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in scale:
            if r.get("roundtrip", "OK") not in ("OK", ""):
                continue
            lines.append(
                f"| {r['format']} | {r['file'].strip(chr(34))} | {r['threads']} | "
                f"{float(r['ratio']):.2f}x | {float(r['comp_sec']):.1f} | "
                f"{float(r['comp_MBps']):.0f} | {float(r['peak_RSS_MB'])/1024:.2f} |")

    n = len(all_recs)
    par = sum(1 for r in all_recs if not r["dominators"])
    head = [
        "# Multi-axis summary",
        "",
        "Verified round trips only. **Pareto-optimal** means no single tool in the",
        "comparison beats NYX on both compression ratio and compression speed;",
        "a tool that wins one axis by losing the other does not dominate.",
        "",
        f"**NYX is Pareto-optimal on {par}/{n} inputs across all formats.**",
    ]
    txt = "\n".join(head + lines) + "\n"
    with open(a.out, "w") as fh:
        fh.write(txt)
    print(txt)


if __name__ == "__main__":
    main()
