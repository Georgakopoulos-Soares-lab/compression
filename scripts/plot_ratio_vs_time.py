#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_TIME_RE = re.compile(r"elapsed_sec=([0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True)
class Point:
    name: str
    ratio: float
    seconds: float


def read_elapsed_seconds(path: Path) -> Optional[float]:
    if not path.exists():
        return None
    m = _TIME_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    return float(m.group(1))


def file_size(path: Path) -> Optional[int]:
    return path.stat().st_size if path.exists() else None


def safe_ratio(orig: int, out: int) -> float:
    return float(orig) / float(out) if out else float("inf")


def write_png(points: list[Point], out_png: Path, title: str) -> None:
    # PNG scatter plot using Pillow (no matplotlib dependency).
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except Exception as e:  # pragma: no cover
        raise SystemExit(
            "Pillow (PIL) is required to render a PNG. Install with: pip install --user pillow"
        ) from e

    width, height = 1600, 950
    margin_left, margin_right = 140, 40
    margin_top, margin_bottom = 80, 160

    xs = [p.ratio for p in points]
    ys = [p.seconds for p in points]
    ys_log = [math.log10(max(1e-9, y)) for y in ys]

    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys_log), max(ys_log)

    x_pad = (x_max - x_min) * 0.07 if x_max > x_min else 0.1
    y_pad = (y_max - y_min) * 0.12 if y_max > y_min else 0.1
    x_min -= x_pad
    x_max += x_pad
    y_min -= y_pad
    y_max += y_pad

    def x_to_px(x: float) -> int:
        t = (x - x_min) / (x_max - x_min) if x_max != x_min else 0.5
        return int(margin_left + t * (width - margin_left - margin_right))

    def y_to_px(ylog: float) -> int:
        t = (ylog - y_min) / (y_max - y_min) if y_max != y_min else 0.5
        return int(margin_top + (1.0 - t) * (height - margin_top - margin_bottom))

    im = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(im)

    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 18)
        font_b = ImageFont.truetype("DejaVuSans.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
        font_b = font

    axis = (0, 0, 0)
    grid = (220, 220, 220)

    x0, y0 = margin_left, height - margin_bottom
    x1, y1 = width - margin_right, margin_top

    # Grid + ticks
    for i in range(6):
        tx = x0 + int(i * (x1 - x0) / 5)
        draw.line([(tx, y0), (tx, y1)], fill=grid, width=1)
        x_val = x_min + (x_max - x_min) * (i / 5)
        draw.text((tx - 18, y0 + 10), f"{x_val:.2f}", fill=axis, font=font)

    for i in range(6):
        ty = y0 - int(i * (y0 - y1) / 5)
        draw.line([(x0, ty), (x1, ty)], fill=grid, width=1)
        y_val_log = y_min + (y_max - y_min) * (i / 5)
        y_val = 10 ** y_val_log
        draw.text((10, ty - 10), f"{y_val:.2f}s", fill=axis, font=font)

    # Axes
    draw.line([(x0, y0), (x1, y0)], fill=axis, width=3)
    draw.line([(x0, y0), (x0, y1)], fill=axis, width=3)

    # Title + labels
    draw.text((margin_left, 20), title, fill=axis, font=font_b)
    draw.text((margin_left, height - 110), "Compression ratio (original / compressed)", fill=axis, font=font_b)
    draw.text((margin_left, height - 75), "Compression time (seconds, log scale)", fill=axis, font=font_b)

    palette = [
        (31, 119, 180),
        (255, 127, 14),
        (44, 160, 44),
        (214, 39, 40),
        (148, 103, 189),
        (140, 86, 75),
        (227, 119, 194),
        (127, 127, 127),
        (188, 189, 34),
        (23, 190, 207),
    ]

    # Draw points + labels
    for i, p in enumerate(points):
        c = palette[i % len(palette)]
        px = x_to_px(p.ratio)
        py = y_to_px(math.log10(max(1e-9, p.seconds)))
        r = 8
        draw.ellipse([(px - r, py - r), (px + r, py + r)], fill=c, outline=(0, 0, 0))
        label = f"{p.name} ({p.ratio:.2f}x, {p.seconds:.2f}s)"
        draw.text((px + 12, py - 10), label, fill=(0, 0, 0), font=font)

    im.save(out_png)


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot compression ratio vs compression time")
    ap.add_argument(
        "--results",
        default="out/baselines/results_fasta.tsv",
        help="TSV produced by scripts/benchmark_baselines.sh (tool, ratio, seconds, ...)",
    )
    ap.add_argument("--out", default="out/baselines/ratio_vs_time_fasta.png", help="Output PNG path")
    ap.add_argument("--title", default="Compression ratio vs compression time", help="Plot title")
    args = ap.parse_args()

    results_path = Path(args.results)
    out_png = Path(args.out)
    out_png.parent.mkdir(parents=True, exist_ok=True)

    if not results_path.exists():
        raise SystemExit(f"Results TSV not found: {results_path}")

    points: list[Point] = []
    lines = results_path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines:
        raise SystemExit(f"Results TSV is empty: {results_path}")

    header = lines[0].split("\t")
    try:
        tool_i = header.index("tool")
        ratio_i = header.index("ratio")
        seconds_i = header.index("seconds")
    except ValueError:
        # backward compatibility: assume first three columns
        tool_i, ratio_i, seconds_i = 0, 1, 2

    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) <= max(tool_i, ratio_i, seconds_i):
            continue
        name = cols[tool_i]
        try:
            ratio = float(cols[ratio_i])
            secs = float(cols[seconds_i])
        except ValueError:
            continue
        points.append(Point(name, ratio, secs))

    if not points:
        raise SystemExit(f"No data points parsed from: {results_path}")

    # Sidecar TSV with exact labels
    tsv = out_png.with_suffix(".tsv")
    with tsv.open("wt", encoding="utf-8") as f:
        f.write("tool\tratio\tseconds\n")
        for p in sorted(points, key=lambda p: p.seconds):
            f.write(f"{p.name}\t{p.ratio:.6f}\t{p.seconds:.6f}\n")

    write_png(points, out_png, args.title)
    print(f"Wrote: {out_png}")
    print(f"Wrote: {tsv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
