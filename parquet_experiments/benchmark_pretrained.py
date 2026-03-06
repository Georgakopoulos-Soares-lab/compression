#!/usr/bin/env python3
"""Benchmark pre-trained generic models against existing baselines.

Compresses every numeric column across all 4 datasets using:
  A. Parquet+zstd (baseline, read from parquet metadata)
  B. Generic pre-trained model (the new experiment)
  C. OpenZL Parquet profile (whole-file, already on disk)
  D. Custom per-column --train-inline (already in per_column_results.json)

Produces pretrained_benchmark.json and prints a human-readable summary.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
RESULTS_DIR = BASE / "results"
MODELS_DIR = BASE / "generic_training" / "models"
PER_COL_DIR = BASE / "per_column_experiment"
ZLI = str(sorted(BASE.parent.glob("nyx/openzl/cachedObjs/*/zli"))[-1])

PROFILE_MAP = {
    "le-i16": "generic_i16.model",
    "le-i32": "generic_i32.model",
    "le-i64": "generic_i64.model",
    "serial": "generic_serial.model",
}

PARQUET_PROFILE_SIZES = {
    "numeric_heavy": 37_472_495,
    "ml_features": 34_998_790,
    "mixed_type": 30_861_076,
}

DATASETS = ["numeric_heavy", "ml_features", "mixed_type", "highcard_strings"]


def arrow_type_to_profile(dtype: pa.DataType):
    if dtype == pa.int8():
        return "le-i16", np.dtype("<i1"), 1
    if dtype == pa.int16():
        return "le-i16", np.dtype("<i2"), 2
    if dtype in (pa.int32(), pa.float32()):
        dt = "<i4" if dtype == pa.int32() else "<f4"
        return "le-i32", np.dtype(dt), 4
    if dtype in (pa.int64(), pa.float64()):
        dt = "<i8" if dtype == pa.int64() else "<f8"
        return "le-i64", np.dtype(dt), 8
    if pa.types.is_timestamp(dtype):
        return "le-i64", np.dtype("<i8"), 8
    if dtype == pa.bool_():
        return "serial", np.dtype("uint8"), 1
    return None, None, None


def extract_column_to_bin(table: pa.Table, col_name: str, dtype: pa.DataType,
                          np_dtype, out_path: Path) -> int:
    col = table.column(col_name)
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()

    if pa.types.is_boolean(dtype):
        filled = pc.fill_null(col, False)
        arr = filled.to_numpy(zero_copy_only=False).astype(np.uint8)
    elif pa.types.is_timestamp(dtype):
        filled = pc.fill_null(col, col.type.to_pandas_dtype().type(0))
        arr = filled.to_numpy(zero_copy_only=False).view(np.int64).astype(np.int64)
    elif pa.types.is_floating(dtype):
        filled = pc.fill_null(col, 0.0)
        arr = filled.to_numpy(zero_copy_only=False).astype(np_dtype)
    else:
        filled = pc.fill_null(col, 0)
        arr = filled.to_numpy(zero_copy_only=False).astype(np_dtype)

    arr.tofile(str(out_path))
    return arr.nbytes


def compress_with_model(bin_path: Path, model_path: Path, zl_path: Path,
                        timeout: int = 120) -> tuple[int, float]:
    cmd = [
        ZLI, "compress",
        str(bin_path.resolve()),
        "--compressor", str(model_path.resolve()),
        "--output", str(zl_path.resolve()),
        "--force",
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - t0

    if result.returncode != 0:
        print(f"      COMPRESS FAILED: {result.stderr[:200]}")
        return -1, elapsed

    return zl_path.stat().st_size, elapsed


def get_zstd_per_column(dataset: str) -> dict[str, int]:
    path = DATA_DIR / f"{dataset}_zstd.parquet"
    f = pq.ParquetFile(str(path))
    rg = f.metadata.row_group(0)
    return {rg.column(i).path_in_schema: rg.column(i).total_compressed_size
            for i in range(rg.num_columns)}


def get_custom_trained(dataset: str) -> dict[str, dict]:
    per_col_path = PER_COL_DIR / "per_column_results.json"
    with open(per_col_path) as f:
        data = json.load(f)

    if dataset in data:
        return {c["name"]: c for c in data[dataset]["columns"]}

    enc_path = RESULTS_DIR / "encoding_benchmark.json"
    if enc_path.exists():
        with open(enc_path) as f:
            enc_data = json.load(f)
        if dataset in enc_data:
            return {c["name"]: c for c in enc_data[dataset]["columns"]}

    return {}


def benchmark_dataset(dataset: str) -> dict:
    print(f"\n{'='*60}")
    print(f"  Dataset: {dataset}")
    print(f"{'='*60}")

    none_path = DATA_DIR / f"{dataset}_none.parquet"
    table = pq.read_table(str(none_path))
    schema = table.schema

    zstd_sizes = get_zstd_per_column(dataset)
    custom_data = get_custom_trained(dataset)

    columns = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        for col_name in schema.names:
            arrow_type = schema.field(col_name).type
            profile, np_dtype, width = arrow_type_to_profile(arrow_type)

            if profile is None:
                print(f"  {col_name}: SKIPPING (string/unsupported type: {arrow_type})")
                continue

            model_file = PROFILE_MAP.get(profile)
            if not model_file:
                print(f"  {col_name}: SKIPPING (no model for profile {profile})")
                continue

            model_path = MODELS_DIR / model_file
            if not model_path.exists():
                print(f"  {col_name}: SKIPPING (model not found: {model_path})")
                continue

            existing_bin = PER_COL_DIR / dataset / f"{col_name}.bin"
            if existing_bin.exists():
                bin_path = existing_bin
                raw_bytes = bin_path.stat().st_size
            else:
                bin_path = tmpdir / f"{col_name}.bin"
                raw_bytes = extract_column_to_bin(table, col_name, arrow_type, np_dtype, bin_path)

            zl_path = tmpdir / f"{col_name}_generic.zl"
            zl_size, comp_time = compress_with_model(bin_path, model_path, zl_path)

            zstd_bytes = zstd_sizes.get(col_name, 0)

            custom_bytes = None
            custom_time = None
            if col_name in custom_data:
                cd = custom_data[col_name]
                custom_bytes = cd.get("zl_bytes")
                custom_time = cd.get("time_secs")

            col_result = {
                "name": col_name,
                "arrow_type": str(arrow_type),
                "profile": profile,
                "raw_bytes": raw_bytes,
                "parquet_zstd_bytes": zstd_bytes,
                "generic_pretrained_bytes": zl_size,
                "generic_pretrained_time_secs": round(comp_time, 2),
            }
            if custom_bytes is not None:
                col_result["custom_trained_bytes"] = custom_bytes
            if custom_time is not None:
                col_result["custom_trained_time_secs"] = custom_time

            columns.append(col_result)

            vs_zstd = ""
            if zstd_bytes > 0 and zl_size > 0:
                pct = (1 - zl_size / zstd_bytes) * 100
                vs_zstd = f"vs zstd: {pct:+.1f}%"

            vs_custom = ""
            if custom_bytes and zl_size > 0:
                pct = (1 - zl_size / custom_bytes) * 100
                vs_custom = f"vs custom: {pct:+.1f}%"

            print(f"  {col_name:20s}  {profile:8s}  "
                  f"generic={zl_size:>10,}  zstd={zstd_bytes:>10,}  "
                  f"{comp_time:.2f}s  {vs_zstd}  {vs_custom}")

    totals = {
        "parquet_zstd": sum(c["parquet_zstd_bytes"] for c in columns),
        "generic_pretrained": sum(c["generic_pretrained_bytes"] for c in columns),
        "generic_pretrained_time_secs": round(
            sum(c["generic_pretrained_time_secs"] for c in columns), 1),
    }

    custom_cols = [c for c in columns if "custom_trained_bytes" in c]
    if custom_cols:
        totals["custom_trained"] = sum(c["custom_trained_bytes"] for c in custom_cols)
        totals["custom_trained_time_secs"] = round(
            sum(c.get("custom_trained_time_secs", 0) for c in custom_cols), 1)

    if dataset in PARQUET_PROFILE_SIZES:
        totals["parquet_profile_whole_file"] = PARQUET_PROFILE_SIZES[dataset]

    return {
        "dataset": dataset,
        "num_rows": table.num_rows,
        "num_columns_tested": len(columns),
        "columns": columns,
        "totals": totals,
    }


def print_summary(results: list[dict]):
    print("\n" + "=" * 80)
    print("  COMPRESSION TIER COMPARISON — FINAL RESULTS")
    print("=" * 80)

    for r in results:
        ds = r["dataset"]
        t = r["totals"]
        n_cols = r["num_columns_tested"]
        n_rows = r["num_rows"]

        print(f"\n  Dataset: {ds} ({n_rows:,} rows, {n_cols} numeric columns)")
        print(f"  {'':20s}  {'Parquet+zstd':>14s}  {'Generic Model':>14s}  {'Custom Trained':>14s}  {'Parquet Prof.':>14s}")
        print(f"  {'':20s}  {'(baseline)':>14s}  {'(no training)':>14s}  {'(inline)':>14s}  {'(whole-file)':>14s}")
        print(f"  {'-'*20}  {'-'*14}  {'-'*14}  {'-'*14}  {'-'*14}")

        for c in r["columns"]:
            zstd_s = f"{c['parquet_zstd_bytes'] / 1e6:.2f} MiB"
            gen_s = f"{c['generic_pretrained_bytes'] / 1e6:.2f} MiB"
            cust_s = f"{c.get('custom_trained_bytes', 0) / 1e6:.2f} MiB" if "custom_trained_bytes" in c else "-"
            print(f"  {c['name']:20s}  {zstd_s:>14s}  {gen_s:>14s}  {cust_s:>14s}  {'-':>14s}")

        print(f"  {'-'*20}  {'-'*14}  {'-'*14}  {'-'*14}  {'-'*14}")

        zstd_total = t["parquet_zstd"]
        gen_total = t["generic_pretrained"]
        cust_total = t.get("custom_trained")
        prof_total = t.get("parquet_profile_whole_file")

        print(f"  {'TOTAL':20s}  {zstd_total / 1e6:>11.2f} MiB  {gen_total / 1e6:>11.2f} MiB", end="")
        if cust_total:
            print(f"  {cust_total / 1e6:>11.2f} MiB", end="")
        else:
            print(f"  {'-':>14s}", end="")
        if prof_total:
            print(f"  {prof_total / 1e6:>11.2f} MiB")
        else:
            print(f"  {'-':>14s}")

        gen_pct = (1 - gen_total / zstd_total) * 100 if zstd_total > 0 else 0
        print(f"  {'vs zstd':20s}  {'baseline':>14s}  {gen_pct:>+11.1f}%  ", end="")
        if cust_total:
            cust_pct = (1 - cust_total / zstd_total) * 100
            print(f"  {cust_pct:>+11.1f}%", end="")
        else:
            print(f"  {'-':>14s}", end="")
        if prof_total:
            prof_pct = (1 - prof_total / zstd_total) * 100
            print(f"  {prof_pct:>+11.1f}%")
        else:
            print(f"  {'-':>14s}")

        gen_time = t["generic_pretrained_time_secs"]
        print(f"  {'Compression time':20s}  {'instant':>14s}  {gen_time:>11.1f}s  ", end="")
        if "custom_trained_time_secs" in t:
            print(f"  {t['custom_trained_time_secs']:>11.1f}s", end="")
        else:
            print(f"  {'-':>14s}", end="")
        print(f"  {'-':>14s}")

        if cust_total and gen_total > 0:
            premium = (1 - cust_total / gen_total) * 100
            print(f"  Training premium (custom vs generic): {premium:+.1f}%")

    print("\n" + "=" * 80)
    print("  KEY METRICS")
    print("=" * 80)

    for r in results:
        ds = r["dataset"]
        t = r["totals"]
        zstd = t["parquet_zstd"]
        gen = t["generic_pretrained"]
        cust = t.get("custom_trained")

        gen_pct = (1 - gen / zstd) * 100 if zstd > 0 else 0
        viable = "YES" if gen_pct >= 15 else "NO"

        print(f"\n  {ds}:")
        print(f"    Generic vs zstd:    {gen_pct:+.1f}% {'(VIABLE)' if gen_pct >= 15 else '(BELOW 15% THRESHOLD)'}")
        if cust:
            cust_pct = (1 - cust / zstd) * 100
            premium = (1 - cust / gen) * 100
            print(f"    Custom vs zstd:     {cust_pct:+.1f}%")
            print(f"    Training premium:   {premium:+.1f}% {'(WORTH UPSELLING)' if premium >= 10 else '(MARGINAL UPSELL)'}")
        if "parquet_profile_whole_file" in t:
            prof = t["parquet_profile_whole_file"]
            prof_vs_gen = (1 - prof / gen) * 100
            print(f"    Parquet profile vs generic: {prof_vs_gen:+.1f}% "
                  f"{'(WITHIN 10%, OK)' if abs(prof_vs_gen) <= 10 else '(SIGNIFICANT DIFFERENCE)'}")
        gen_time = t["generic_pretrained_time_secs"]
        cust_time = t.get("custom_trained_time_secs", 0)
        if cust_time > 0:
            speedup = cust_time / gen_time if gen_time > 0 else float("inf")
            print(f"    Speed: generic {gen_time:.1f}s vs custom {cust_time:.1f}s ({speedup:.0f}x faster)")


def main():
    all_results = []

    for ds in DATASETS:
        result = benchmark_dataset(ds)
        all_results.append(result)

    out_path = RESULTS_DIR / "pretrained_benchmark.json"
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {out_path}")

    print_summary(all_results)


if __name__ == "__main__":
    main()
