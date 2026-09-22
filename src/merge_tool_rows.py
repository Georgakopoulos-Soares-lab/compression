#!/usr/bin/env python3
"""Fold a single-tool benchmark CSV into the per-format benchmark tables.

  python3 src/merge_tool_rows.py results/sevenzip_bench.csv

A tool added after the main sweep (7-Zip here) is measured on its own so the
other rows are not re-run, but it has to end up in the same tables as everything
else or it will not reach the figures. Rows are matched to a format by which
table already contains that file, so a file the study does not use is skipped
rather than silently creating a new row.
"""
import csv
import os
import sys

FORMATS = ("results/vcf_bench.csv", "results/fasta_bench.csv", "results/fastq_bench.csv")


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "results/sevenzip_bench.csv"
    new = list(csv.DictReader(open(src)))
    if not new:
        print("nothing to merge")
        return

    for main_csv in FORMATS:
        if not os.path.isfile(main_csv):
            continue
        rows = list(csv.DictReader(open(main_csv)))
        fields = list(rows[0].keys())
        known = {r["file"].strip('"') for r in rows}
        # Carry per-file constants (the VCF table's archetype column) from a row
        # that already describes this file, rather than inventing them.
        meta = {r["file"].strip('"'): r for r in rows}
        added = 0
        for r in new:
            f = r["file"].strip('"')
            if f not in known:
                continue
            if any(x["tool"] == r["tool"] and x["file"].strip('"') == f for x in rows):
                continue
            out = {c: "" for c in fields}
            for c in fields:
                if c in r:
                    out[c] = r[c]
            out["file"] = '"%s"' % f
            if "archetype" in fields:
                out["archetype"] = meta[f].get("archetype", "")
            if "comp_MBps" in fields:
                o, s = num(r.get("orig_bytes")), num(r.get("comp_sec"))
                out["comp_MBps"] = f"{o/s/1e6:.1f}" if o and s else ""
            rows.append(out)
            added += 1
        if not added:
            continue
        with open(main_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, quoting=csv.QUOTE_NONE, quotechar=None)
            w.writeheader()
            for x in rows:
                x["file"] = '"%s"' % x["file"].strip('"')
                w.writerow(x)
        print(f"  {os.path.basename(main_csv)}: +{added} rows")


if __name__ == "__main__":
    main()
