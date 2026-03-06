#!/usr/bin/env python3
"""Benchmark all encoding strategies across datasets.

For each column in each dataset, runs every applicable encoding through
the ColumnEncoder, records compressed sizes, and compares against
Parquet+zstd baselines.

Usage:
    python parquet_experiments/benchmark_encodings.py [--dataset NAME] [--timeout SECS]

Output:
    parquet_experiments/results/encoding_benchmark.json
    Console summary table
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).parent.parent))
from parquet_experiments.column_encoder import ColumnEncoder, VALID_ENCODINGS, _detect_binary_type, _is_sorted, _arrow_type_to_profile

AUTO_ENCODER = None  # lazily initialised ColumnEncoder for auto-detect

DATA_DIR = Path(__file__).parent / "data"
RESULTS_DIR = Path(__file__).parent / "results"
OUT_DIR = Path(__file__).parent / "encoding_benchmark"

ZLI = sorted(Path(__file__).parent.parent.glob("nyx/openzl/cachedObjs/*/zli"),
             key=lambda p: p.stat().st_mtime)[-1]

DATASETS = {
    "numeric_heavy": "numeric_heavy_none.parquet",
    "ml_features": "ml_features_none.parquet",
    "mixed_type": "mixed_type_none.parquet",
    "highcard_strings": "highcard_strings_none.parquet",
}

ZSTD_FILES = {
    "numeric_heavy": "numeric_heavy_zstd.parquet",
    "ml_features": "ml_features_zstd.parquet",
    "mixed_type": "mixed_type_zstd.parquet",
    "highcard_strings": "highcard_strings_zstd.parquet",
}


def applicable_encodings(col: pa.Array, dtype: pa.DataType) -> list[str]:
    """Return the list of encodings that can be applied to this column.

    For strings, always include dictionary as a fallback so we never produce
    zero results. binary_convert is added when a structured pattern is detected.
    """
    result = []

    profile, _, _ = _arrow_type_to_profile(dtype)
    is_numeric = profile is not None
    is_string = pa.types.is_string(dtype) or pa.types.is_large_string(dtype)

    if is_numeric:
        result.append("raw")
        result.append("delta")

    if is_string:
        sample = [col[i].as_py() for i in range(min(200, len(col))) if col[i].is_valid]
        bin_type = _detect_binary_type(sample) if sample else None
        if bin_type is not None:
            result.append("binary_convert")

        result.append("dictionary")

    return result


def get_auto_pick(encoder: ColumnEncoder, col: pa.Array, dtype: pa.DataType) -> str:
    """Run the auto-detect logic and return what it would pick."""
    return encoder._auto_detect(col, dtype)


def get_parquet_zstd_column_size(dataset_name: str, col_name: str) -> int:
    """Get the compressed size of a column in the zstd Parquet file."""
    zstd_file = DATA_DIR / ZSTD_FILES.get(dataset_name, "")
    if not zstd_file.exists():
        return 0
    pf = pq.ParquetFile(str(zstd_file))
    meta = pf.metadata
    total = 0
    for rg_idx in range(meta.num_row_groups):
        rg = meta.row_group(rg_idx)
        for col_idx in range(rg.num_columns):
            col_meta = rg.column(col_idx)
            if col_meta.path_in_schema == col_name:
                total += col_meta.total_compressed_size
    return total


def run_benchmark(dataset_filter: str | None = None, timeout: int = 300):
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    encoder = ColumnEncoder(zli_path=ZLI)
    all_results = {}

    datasets_to_run = DATASETS
    if dataset_filter:
        datasets_to_run = {k: v for k, v in DATASETS.items() if k == dataset_filter}

    for ds_name, ds_file in datasets_to_run.items():
        ds_path = DATA_DIR / ds_file
        if not ds_path.exists():
            print(f"SKIP {ds_name}: {ds_path} not found")
            continue

        table = pq.read_table(str(ds_path))
        print(f"\n{'='*70}")
        print(f"  {ds_name}: {table.num_rows:,} rows x {table.num_columns} cols")
        print(f"{'='*70}")

        ds_out = OUT_DIR / ds_name
        ds_out.mkdir(parents=True, exist_ok=True)

        ds_results = {"dataset": ds_name, "num_rows": table.num_rows, "columns": []}

        for i in range(table.num_columns):
            field = table.schema.field(i)
            col_name = field.name
            col_type = field.type
            col_data = table.column(col_name)

            combined = col_data.combine_chunks()
            encodings = applicable_encodings(combined, col_type)
            auto_pick = get_auto_pick(encoder, combined, col_type)
            pq_zstd_size = get_parquet_zstd_column_size(ds_name, col_name)

            print(f"\n  {col_name} ({col_type}) — auto: {auto_pick}, testing: {encodings}")

            col_result = {
                "name": col_name,
                "type": str(col_type),
                "parquet_zstd_bytes": pq_zstd_size,
                "auto_detected_encoding": auto_pick,
                "encodings": {},
            }

            best_encoding = None
            best_compressed = float("inf")

            for enc in encodings:
                col_out = ds_out / col_name
                col_out.mkdir(parents=True, exist_ok=True)

                try:
                    result = encoder.encode_and_compress(
                        column_data=col_data,
                        column_name=col_name,
                        column_type=col_type,
                        encoding=enc,
                        output_dir=col_out,
                        compress_timeout=timeout,
                    )

                    total_compressed = result.compressed_bytes
                    for sp in result.sidecar_paths:
                        total_compressed += Path(sp).stat().st_size

                    enc_info = {
                        "compressed_bytes": total_compressed,
                        "zl_bytes": result.compressed_bytes,
                        "encoded_bytes": result.encoded_bytes,
                        "raw_string_bytes": result.raw_string_bytes,
                        "ratio_vs_raw": result.ratio_vs_raw,
                        "profile": result.profile,
                        "time_secs": result.compress_time_secs,
                        "metadata": result.metadata,
                    }

                    if pq_zstd_size > 0:
                        enc_info["vs_parquet_zstd_pct"] = round(
                            (1 - total_compressed / pq_zstd_size) * 100, 1
                        )

                    col_result["encodings"][enc] = enc_info

                    tag = "***" if total_compressed < best_compressed else "   "
                    if total_compressed < best_compressed:
                        best_compressed = total_compressed
                        best_encoding = enc

                    vs_pq = f" (vs pq+zstd: {enc_info.get('vs_parquet_zstd_pct', '?')}%)" if pq_zstd_size > 0 else ""
                    print(f"    {tag} {enc:16s} -> {total_compressed/1024/1024:>7.3f} MiB  [{result.compress_time_secs:.0f}s]{vs_pq}")

                except Exception as e:
                    col_result["encodings"][enc] = {"error": str(e)[:200]}
                    print(f"        {enc:16s} -> FAILED: {str(e)[:80]}")

            col_result["best_encoding"] = best_encoding
            col_result["best_compressed_bytes"] = int(best_compressed) if best_compressed < float("inf") else 0
            ds_results["columns"].append(col_result)

        all_results[ds_name] = ds_results

    results_path = RESULTS_DIR / "encoding_benchmark.json"
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {results_path}")

    print_summary(all_results)


def print_summary(all_results: dict):
    print(f"\n{'='*100}")
    print(f"  ENCODING BENCHMARK SUMMARY")
    print(f"{'='*100}")

    for ds_name, ds_data in all_results.items():
        print(f"\n--- {ds_name} ({ds_data['num_rows']:,} rows) ---")
        print(f"  {'Column':22s} {'Type':12s} {'Auto':16s} {'Best':16s} {'Match':5s} {'Size':>8s} {'Pq+zstd':>8s} {'Savings':>8s}")
        print(f"  {'-'*22} {'-'*12} {'-'*16} {'-'*16} {'-'*5} {'-'*8} {'-'*8} {'-'*8}")

        auto_correct = 0
        total_cols = 0

        for col in ds_data["columns"]:
            best = col.get("best_encoding") or "?"
            auto = col.get("auto_detected_encoding") or "?"
            best_bytes = col.get("best_compressed_bytes", 0)
            pq_bytes = col.get("parquet_zstd_bytes", 0)

            size_str = f"{best_bytes/1024/1024:.2f}M" if best_bytes > 0 else "?"
            pq_str = f"{pq_bytes/1024/1024:.2f}M" if pq_bytes > 0 else "?"

            if pq_bytes > 0 and best_bytes > 0:
                savings = (1 - best_bytes / pq_bytes) * 100
                sav_str = f"{savings:+.1f}%"
            else:
                sav_str = "?"

            match = "OK" if auto == best else "MISS"
            if best != "?":
                total_cols += 1
                if auto == best:
                    auto_correct += 1

            print(f"  {col['name']:22s} {col['type']:12s} {auto:16s} {best:16s} {match:5s} {size_str:>8s} {pq_str:>8s} {sav_str:>8s}")

        if total_cols > 0:
            print(f"\n  Auto-detect accuracy: {auto_correct}/{total_cols} ({100*auto_correct/total_cols:.0f}%)")


def main():
    parser = argparse.ArgumentParser(description="Benchmark column encodings")
    parser.add_argument("--dataset", type=str, default=None,
                        help="Run only this dataset (e.g. 'highcard_strings')")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Compression timeout per column in seconds (default 300)")
    args = parser.parse_args()

    run_benchmark(dataset_filter=args.dataset, timeout=args.timeout)


if __name__ == "__main__":
    main()
