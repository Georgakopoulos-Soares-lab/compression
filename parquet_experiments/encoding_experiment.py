#!/usr/bin/env python3
"""Encoding experiment: test pre-compression encoding + OpenZL.

Three-way comparison for each column x encoding:
  1. Parquet+zstd baseline (from file metadata)
  2. Raw OpenZL (current approach, from saved baselines)
  3. Encoded+OpenZL (new approach)

Results saved to: parquet_experiments/encoding_experiment/encoding_results.json
"""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

WORKSPACE = Path(__file__).parent.parent
ZLI = str((WORKSPACE / "nyx" / "openzl" / "zli").resolve())
DATA_DIR = WORKSPACE / "parquet_experiments" / "data"
OUT_DIR = WORKSPACE / "parquet_experiments" / "encoding_experiment"
BASELINE_FILE = (
    WORKSPACE / "parquet_experiments" / "per_column_experiment" / "per_column_results.json"
)
RESULTS_FILE = OUT_DIR / "encoding_results.json"

COMPRESS_TIMEOUT = 600


# ---------------------------------------------------------------------------
# Compression helper
# ---------------------------------------------------------------------------

def compress_with_openzl(
    bin_path: Path, profile: str, zl_path: Path, timeout: int = COMPRESS_TIMEOUT
) -> tuple[int, float]:
    cmd = [
        ZLI, "compress",
        str(bin_path.resolve()),
        "--profile", profile,
        "--train-inline",
        "--output", str(zl_path.resolve()),
        "--force",
    ]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"zli compress failed (rc={result.returncode}):\n{result.stderr[:500]}"
        )
    return zl_path.stat().st_size, elapsed


# ---------------------------------------------------------------------------
# Encoding implementations
# ---------------------------------------------------------------------------

def byte_stream_split(values: np.ndarray, byte_width: int) -> list[np.ndarray]:
    raw = values.tobytes()
    streams = []
    for byte_pos in range(byte_width):
        stream = np.frombuffer(raw, dtype=np.uint8)[byte_pos::byte_width].copy()
        streams.append(stream)
    return streams


def xor_delta(values: np.ndarray, byte_width: int) -> np.ndarray:
    if byte_width == 4:
        uint_view = values.view(np.uint32)
    elif byte_width == 8:
        uint_view = values.view(np.uint64)
    elif byte_width == 2:
        uint_view = values.view(np.uint16)
    else:
        raise ValueError(f"Unsupported byte width: {byte_width}")
    result = np.empty_like(uint_view)
    result[0] = uint_view[0]
    result[1:] = np.bitwise_xor(uint_view[1:], uint_view[:-1])
    return result


def bitpack_bools(values: np.ndarray) -> np.ndarray:
    return np.packbits(values.astype(np.uint8))


def dict_encode_int(values: np.ndarray):
    unique = np.unique(values)
    num_unique = len(unique)
    lookup = {int(v): i for i, v in enumerate(unique)}
    if num_unique <= 255:
        indices = np.array([lookup[int(v)] for v in values], dtype=np.uint8)
        profile = "serial"
    elif num_unique <= 65535:
        indices = np.array([lookup[int(v)] for v in values], dtype=np.uint16)
        profile = "le-i16"
    else:
        indices = np.array([lookup[int(v)] for v in values], dtype=np.int32)
        profile = "le-i32"
    dict_bytes = unique.tobytes()
    return indices, profile, dict_bytes, num_unique


def dict_encode_float(values: np.ndarray):
    unique = np.unique(values)
    num_unique = len(unique)
    if num_unique > 65535:
        return None, None, None, num_unique
    lookup = {v: i for i, v in enumerate(unique)}
    if num_unique <= 255:
        indices = np.array([lookup[v] for v in values], dtype=np.uint8)
        profile = "serial"
    elif num_unique <= 65535:
        indices = np.array([lookup[v] for v in values], dtype=np.uint16)
        profile = "le-i16"
    else:
        indices = np.array([lookup[v] for v in values], dtype=np.int32)
        profile = "le-i32"
    dict_bytes = unique.tobytes()
    return indices, profile, dict_bytes, num_unique


def delta_narrow(values: np.ndarray):
    deltas = np.empty(len(values), dtype=np.int64)
    deltas[0] = int(values[0])
    deltas[1:] = np.diff(values.astype(np.int64))
    if len(deltas) <= 1:
        min_d, max_d = 0, 0
    else:
        min_d, max_d = int(deltas[1:].min()), int(deltas[1:].max())
    if -128 <= min_d and max_d <= 127:
        return deltas.astype(np.int8), "serial", 1
    elif -32768 <= min_d and max_d <= 32767:
        return deltas.astype(np.int16), "le-i16", 2
    elif -2147483648 <= min_d and max_d <= 2147483647:
        return deltas.astype(np.int32), "le-i32", 4
    else:
        return deltas, "le-i64", 8


# ---------------------------------------------------------------------------
# Column loading
# ---------------------------------------------------------------------------

def load_column(parquet_path: Path, col_name: str) -> tuple[np.ndarray, pa.DataType]:
    table = pq.read_table(str(parquet_path), columns=[col_name])
    col = table.column(col_name)
    if isinstance(col, pa.ChunkedArray):
        col = col.combine_chunks()
    arrow_type = col.type

    if pa.types.is_boolean(arrow_type):
        filled = pc.fill_null(col, False)
        arr = filled.to_numpy(zero_copy_only=False).astype(np.uint8)
    elif pa.types.is_timestamp(arrow_type):
        col_i64 = pc.cast(col, pa.int64())
        filled = pc.fill_null(col_i64, 0)
        arr = filled.to_numpy(zero_copy_only=False).astype(np.int64)
    elif pa.types.is_floating(arrow_type):
        filled = pc.fill_null(col, 0.0)
        arr = filled.to_numpy(zero_copy_only=False)
        if arrow_type == pa.float32():
            arr = arr.astype(np.float32)
        else:
            arr = arr.astype(np.float64)
    else:
        filled = pc.fill_null(col, 0)
        arr = filled.to_numpy(zero_copy_only=False)
    return arr, arrow_type


def get_parquet_column_sizes(parquet_path: Path) -> dict[str, int]:
    f = pq.ParquetFile(str(parquet_path))
    sizes = {}
    rg = f.metadata.row_group(0)
    for i in range(rg.num_columns):
        c = rg.column(i)
        sizes[c.path_in_schema] = c.total_compressed_size
    return sizes


def get_byte_width(arrow_type: pa.DataType) -> int:
    if arrow_type in (pa.float32(), pa.int32()):
        return 4
    if arrow_type in (pa.float64(), pa.int64()):
        return 8
    if arrow_type in (pa.int16(), pa.uint16()):
        return 2
    if arrow_type == pa.bool_():
        return 1
    if pa.types.is_timestamp(arrow_type):
        return 8
    return 4


def get_le_profile(arrow_type: pa.DataType) -> str:
    if arrow_type in (pa.int16(), pa.uint16()):
        return "le-i16"
    if arrow_type in (pa.int32(), pa.float32()):
        return "le-i32"
    if arrow_type in (pa.int64(), pa.float64()):
        return "le-i64"
    if pa.types.is_timestamp(arrow_type):
        return "le-i64"
    if arrow_type == pa.bool_():
        return "serial"
    return "le-i32"


# ---------------------------------------------------------------------------
# Per-encoding runners
# ---------------------------------------------------------------------------

def run_byte_split(arr, arrow_type, col_dir: Path, col_name: str):
    bw = get_byte_width(arrow_type)
    streams = byte_stream_split(arr, bw)
    total_compressed = 0
    total_time = 0.0
    details = []
    for i, stream in enumerate(streams):
        bin_path = col_dir / f"{col_name}_bytesplit_s{i}.bin"
        stream.tofile(str(bin_path))
        zl_path = col_dir / f"{col_name}_bytesplit_s{i}.zl"
        sz, t = compress_with_openzl(bin_path, "serial", zl_path)
        total_compressed += sz
        total_time += t
        details.append({"stream": i, "compressed": sz, "time_secs": round(t, 1)})
        print(f"        stream {i}: {sz:,} bytes ({t:.1f}s)")
    return {
        "encoded_bytes": arr.nbytes,
        "compressed_bytes": total_compressed,
        "time_secs": round(total_time, 1),
        "stream_details": details,
    }


def run_xor_delta(arr, arrow_type, col_dir: Path, col_name: str):
    bw = get_byte_width(arrow_type)
    xored = xor_delta(arr, bw)
    profile = get_le_profile(arrow_type)
    bin_path = col_dir / f"{col_name}_xordelta.bin"
    xored.tofile(str(bin_path))
    zl_path = col_dir / f"{col_name}_xordelta.zl"
    sz, t = compress_with_openzl(bin_path, profile, zl_path)
    return {
        "encoded_bytes": xored.nbytes,
        "compressed_bytes": sz,
        "time_secs": round(t, 1),
    }


def run_xor_delta_bytesplit(arr, arrow_type, col_dir: Path, col_name: str):
    bw = get_byte_width(arrow_type)
    xored = xor_delta(arr, bw)
    streams = byte_stream_split(xored, bw)
    total_compressed = 0
    total_time = 0.0
    details = []
    for i, stream in enumerate(streams):
        bin_path = col_dir / f"{col_name}_xdbs_s{i}.bin"
        stream.tofile(str(bin_path))
        zl_path = col_dir / f"{col_name}_xdbs_s{i}.zl"
        sz, t = compress_with_openzl(bin_path, "serial", zl_path)
        total_compressed += sz
        total_time += t
        details.append({"stream": i, "compressed": sz, "time_secs": round(t, 1)})
        print(f"        stream {i}: {sz:,} bytes ({t:.1f}s)")
    return {
        "encoded_bytes": arr.nbytes,
        "compressed_bytes": total_compressed,
        "time_secs": round(total_time, 1),
        "stream_details": details,
    }


def run_bitpack(arr, arrow_type, col_dir: Path, col_name: str):
    packed = bitpack_bools(arr)
    bin_path = col_dir / f"{col_name}_bitpack.bin"
    packed.tofile(str(bin_path))
    zl_path = col_dir / f"{col_name}_bitpack.zl"
    sz, t = compress_with_openzl(bin_path, "serial", zl_path)
    return {
        "encoded_bytes": len(packed),
        "compressed_bytes": sz,
        "time_secs": round(t, 1),
    }


def run_dict_int(arr, arrow_type, col_dir: Path, col_name: str):
    indices, idx_profile, dict_bytes, num_unique = dict_encode_int(arr)
    original_profile = get_le_profile(arrow_type)

    idx_bin = col_dir / f"{col_name}_dict_indices.bin"
    indices.tofile(str(idx_bin))
    idx_zl = col_dir / f"{col_name}_dict_indices.zl"
    idx_sz, idx_t = compress_with_openzl(idx_bin, idx_profile, idx_zl)

    dict_bin = col_dir / f"{col_name}_dict_values.bin"
    dict_bin.write_bytes(dict_bytes)
    dict_zl = col_dir / f"{col_name}_dict_values.zl"
    dict_sz, dict_t = compress_with_openzl(dict_bin, original_profile, dict_zl)

    return {
        "encoded_bytes": indices.nbytes + len(dict_bytes),
        "compressed_bytes": idx_sz + dict_sz,
        "time_secs": round(idx_t + dict_t, 1),
        "num_unique": num_unique,
        "index_profile": idx_profile,
        "index_compressed": idx_sz,
        "dict_compressed": dict_sz,
    }


def run_dict_float(arr, arrow_type, col_dir: Path, col_name: str):
    indices, idx_profile, dict_bytes, num_unique = dict_encode_float(arr)
    if indices is None:
        return {
            "skipped": True,
            "reason": f"too many unique values ({num_unique})",
            "num_unique": num_unique,
        }
    original_profile = get_le_profile(arrow_type)

    idx_bin = col_dir / f"{col_name}_dictf_indices.bin"
    indices.tofile(str(idx_bin))
    idx_zl = col_dir / f"{col_name}_dictf_indices.zl"
    idx_sz, idx_t = compress_with_openzl(idx_bin, idx_profile, idx_zl)

    dict_bin = col_dir / f"{col_name}_dictf_values.bin"
    dict_bin.write_bytes(dict_bytes)
    dict_zl = col_dir / f"{col_name}_dictf_values.zl"
    dict_sz, dict_t = compress_with_openzl(dict_bin, original_profile, dict_zl)

    return {
        "encoded_bytes": indices.nbytes + len(dict_bytes),
        "compressed_bytes": idx_sz + dict_sz,
        "time_secs": round(idx_t + dict_t, 1),
        "num_unique": num_unique,
        "index_profile": idx_profile,
        "index_compressed": idx_sz,
        "dict_compressed": dict_sz,
    }


def run_delta_narrow(arr, arrow_type, col_dir: Path, col_name: str):
    narrowed, profile, container_width = delta_narrow(arr)
    bin_path = col_dir / f"{col_name}_deltanarrow.bin"
    narrowed.tofile(str(bin_path))
    zl_path = col_dir / f"{col_name}_deltanarrow.zl"
    sz, t = compress_with_openzl(bin_path, profile, zl_path)
    return {
        "encoded_bytes": narrowed.nbytes,
        "compressed_bytes": sz,
        "time_secs": round(t, 1),
        "container_width": container_width,
        "profile_used": profile,
        "original_width": get_byte_width(arrow_type),
    }


ENCODING_RUNNERS = {
    "byte_split": run_byte_split,
    "xor_delta": run_xor_delta,
    "xor_delta_bytesplit": run_xor_delta_bytesplit,
    "bitpack": run_bitpack,
    "dict_int": run_dict_int,
    "dict_float": run_dict_float,
    "delta_narrow": run_delta_narrow,
}


# ---------------------------------------------------------------------------
# Test matrix
# ---------------------------------------------------------------------------

TEST_MATRIX = [
    # numeric_heavy
    ("numeric_heavy", "temperature", ["byte_split", "xor_delta", "xor_delta_bytesplit"]),
    ("numeric_heavy", "humidity", ["byte_split", "xor_delta", "xor_delta_bytesplit"]),
    ("numeric_heavy", "pressure", ["byte_split", "xor_delta", "xor_delta_bytesplit"]),
    ("numeric_heavy", "battery_voltage", ["byte_split", "xor_delta", "xor_delta_bytesplit"]),
    ("numeric_heavy", "device_id", ["dict_int"]),
    ("numeric_heavy", "status_code", ["dict_int"]),
    ("numeric_heavy", "timestamp", ["delta_narrow"]),
    # mixed_type
    ("mixed_type", "is_returned", ["bitpack"]),
    ("mixed_type", "customer_id", ["dict_int"]),
    ("mixed_type", "quantity", ["dict_int"]),
    ("mixed_type", "unit_price", ["byte_split", "xor_delta", "xor_delta_bytesplit", "dict_float"]),
    ("mixed_type", "total_amount", ["byte_split", "xor_delta", "xor_delta_bytesplit"]),
    ("mixed_type", "order_date", ["byte_split", "xor_delta"]),
    # ml_features
    ("ml_features", "feature_01", ["byte_split", "xor_delta"]),
    ("ml_features", "feature_11", ["byte_split", "xor_delta"]),
    ("ml_features", "feature_21", ["byte_split"]),
    ("ml_features", "feature_31", ["dict_float", "byte_split"]),
]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_baselines() -> dict:
    with open(BASELINE_FILE) as f:
        return json.load(f)


def save_results(results: list[dict], summary: dict):
    payload = {
        "experiment": "encoding_before_openzl",
        "timestamp": datetime.now().isoformat(),
        "results": results,
        "summary": summary,
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(payload, f, indent=2)


def load_existing_results() -> tuple[list[dict], dict[str, dict]]:
    """Load previously saved results for resume capability."""
    if not RESULTS_FILE.exists():
        return [], {}
    with open(RESULTS_FILE) as f:
        data = json.load(f)
    results = data.get("results", [])
    best = data.get("summary", {}).get("best_encoding_per_column", {})
    return results, best


def is_column_done(results: list[dict], dataset: str, col_name: str, encodings: list[str]) -> bool:
    """Check if all encodings for a column are already completed."""
    for r in results:
        if r["dataset"] == dataset and r["column"] == col_name:
            done_encs = set(r.get("encodings", {}).keys())
            if set(encodings).issubset(done_encs):
                return True
    return False


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    baselines = load_baselines()

    results, best_per_column = load_existing_results()
    zstd_sizes_cache: dict[str, dict[str, int]] = {}

    total_tests = sum(len(encs) for _, _, encs in TEST_MATRIX)
    done = 0

    for dataset, col_name, encodings in TEST_MATRIX:
        if is_column_done(results, dataset, col_name, encodings):
            done += len(encodings)
            print(f"\n  [SKIP] {dataset}/{col_name} — already completed")
            continue

        parquet_path = DATA_DIR / f"{dataset}_zstd.parquet"

        if dataset not in zstd_sizes_cache:
            zstd_sizes_cache[dataset] = get_parquet_column_sizes(parquet_path)
        zstd_sizes = zstd_sizes_cache[dataset]

        print(f"\n{'='*70}")
        print(f"  {dataset} / {col_name}  ({len(encodings)} encodings)")
        print(f"{'='*70}")

        arr, arrow_type = load_column(parquet_path, col_name)
        num_rows = len(arr)
        raw_bytes = arr.nbytes
        pq_zstd = zstd_sizes.get(col_name, 0)

        raw_openzl = None
        raw_openzl_time = None
        ds_baselines = baselines.get(dataset, {}).get("columns", [])
        for bl in ds_baselines:
            if bl["name"] == col_name:
                raw_openzl = bl["zl_bytes"]
                raw_openzl_time = bl.get("time_secs")
                break

        print(f"  rows={num_rows:,}  type={arrow_type}  raw={raw_bytes:,}")
        print(f"  Parquet+zstd: {pq_zstd:,}   Raw OpenZL: {raw_openzl:,}")

        col_dir = OUT_DIR / dataset / col_name
        col_dir.mkdir(parents=True, exist_ok=True)

        col_result = {
            "dataset": dataset,
            "column": col_name,
            "arrow_type": str(arrow_type),
            "num_rows": num_rows,
            "raw_bytes": raw_bytes,
            "parquet_zstd_bytes": pq_zstd,
            "raw_openzl_bytes": raw_openzl,
            "raw_openzl_time_secs": raw_openzl_time,
            "encodings": {},
        }

        for enc_name in encodings:
            done += 1
            print(f"\n    [{done}/{total_tests}] {enc_name}...")
            runner = ENCODING_RUNNERS[enc_name]
            try:
                enc_result = runner(arr, arrow_type, col_dir, col_name)
            except Exception as e:
                print(f"      FAILED: {e}")
                enc_result = {"error": str(e)}
                col_result["encodings"][enc_name] = enc_result
                continue

            if enc_result.get("skipped"):
                print(f"      SKIPPED: {enc_result['reason']}")
                col_result["encodings"][enc_name] = enc_result
                continue

            comp = enc_result["compressed_bytes"]
            vs_raw = round((1 - comp / raw_openzl) * 100, 1) if raw_openzl else None
            vs_zstd = round((1 - comp / pq_zstd) * 100, 1) if pq_zstd else None
            enc_result["vs_raw_openzl_pct"] = vs_raw
            enc_result["vs_parquet_zstd_pct"] = vs_zstd

            col_result["encodings"][enc_name] = enc_result

            sign_raw = "+" if (vs_raw and vs_raw > 0) else ""
            sign_zstd = "+" if (vs_zstd and vs_zstd > 0) else ""
            print(
                f"      → {comp:,} bytes  "
                f"(vs raw: {sign_raw}{vs_raw}%, vs zstd: {sign_zstd}{vs_zstd}%)  "
                f"[{enc_result['time_secs']}s]"
            )

            key = f"{dataset}/{col_name}"
            if key not in best_per_column or comp < best_per_column[key]["compressed"]:
                best_per_column[key] = {
                    "encoding": enc_name,
                    "compressed": comp,
                    "vs_raw_openzl_pct": vs_raw,
                    "vs_parquet_zstd_pct": vs_zstd,
                }

        results.append(col_result)

        summary = build_summary(results, best_per_column)
        save_results(results, summary)
        print(f"\n  [results saved after {dataset}/{col_name}]")

    summary = build_summary(results, best_per_column)
    save_results(results, summary)
    print_summary_table(results, best_per_column)


def build_summary(results, best_per_column):
    total_tested = 0
    beat_raw = 0
    beat_zstd = 0
    for r in results:
        for enc_name, enc in r["encodings"].items():
            if enc.get("skipped") or enc.get("error"):
                continue
            total_tested += 1
            if enc.get("vs_raw_openzl_pct") and enc["vs_raw_openzl_pct"] > 0:
                beat_raw += 1
            if enc.get("vs_parquet_zstd_pct") and enc["vs_parquet_zstd_pct"] > 0:
                beat_zstd += 1
    return {
        "total_encodings_tested": total_tested,
        "encodings_that_beat_raw_openzl": beat_raw,
        "encodings_that_beat_parquet_zstd": beat_zstd,
        "best_encoding_per_column": best_per_column,
    }


def print_summary_table(results, best_per_column):
    print("\n\n" + "=" * 120)
    print("  ENCODING EXPERIMENT RESULTS")
    print("=" * 120)

    current_dataset = None
    header = (
        f"{'Column':<20s} | {'Type':<9s} | {'Parquet+zstd':>13s} | "
        f"{'Raw OpenZL':>13s} | {'Best Encoding':<22s} | "
        f"{'Encoded+OZL':>13s} | {'vs Raw':>8s} | {'vs Parquet':>10s}"
    )
    sep = "-" * len(header)

    for r in results:
        if r["dataset"] != current_dataset:
            current_dataset = r["dataset"]
            print(f"\n  Dataset: {current_dataset}")
            print(f"  {header}")
            print(f"  {sep}")

        key = f"{r['dataset']}/{r['column']}"
        best = best_per_column.get(key)
        if not best:
            continue

        vs_raw_s = f"+{best['vs_raw_openzl_pct']}%" if best.get("vs_raw_openzl_pct") and best["vs_raw_openzl_pct"] > 0 else f"{best.get('vs_raw_openzl_pct', 'N/A')}%"
        vs_zstd_s = f"+{best['vs_parquet_zstd_pct']}%" if best.get("vs_parquet_zstd_pct") and best["vs_parquet_zstd_pct"] > 0 else f"{best.get('vs_parquet_zstd_pct', 'N/A')}%"

        print(
            f"  {r['column']:<20s} | {r['arrow_type']:<9s} | "
            f"{r['parquet_zstd_bytes']:>13,} | {r['raw_openzl_bytes']:>13,} | "
            f"{best['encoding']:<22s} | {best['compressed']:>13,} | "
            f"{vs_raw_s:>8s} | {vs_zstd_s:>10s}"
        )

    print()


if __name__ == "__main__":
    main()
