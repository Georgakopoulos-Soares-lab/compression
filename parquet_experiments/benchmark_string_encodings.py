#!/usr/bin/env python3
"""Focused benchmark: alternative encodings for high-cardinality free-form strings.

Tests multiple encoding strategies on email and url_path columns to find
approaches that beat Parquet+zstd for text data that isn't binary-convertible.

Encodings tested:
  1. dict_current    — our current approach: OpenZL indices + raw dict blob
  2. dict_compressed — OpenZL indices + zstd-compressed dict blob
  3. fixed_width     — pad to fixed width, compress with le-i64
  4. chunked_serial  — split raw bytes into small chunks, serial each
  5. structural      — decompose into parts (email: user+domain, url: parts)

Usage:
    python parquet_experiments/benchmark_string_encodings.py
"""

from __future__ import annotations

import json
import struct
import subprocess
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parent / "data"
OUT_DIR = Path(__file__).parent / "string_encoding_experiment"
RESULTS_DIR = Path(__file__).parent / "results"

ZLI = sorted(Path(__file__).parent.parent.glob("nyx/openzl/cachedObjs/*/zli"),
             key=lambda p: p.stat().st_mtime)[-1]

TIMEOUT = 300


def zli_compress(bin_path: Path, profile: str, zl_path: Path) -> tuple[int, float]:
    cmd = [str(ZLI), "compress", str(bin_path), "--profile", profile,
           "--train-inline", "--output", str(zl_path), "--force"]
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    elapsed = time.time() - t0
    if result.returncode != 0:
        raise RuntimeError(f"zli failed: {result.stderr[:300]}")
    return zl_path.stat().st_size, elapsed


def zstd_compress(data: bytes) -> bytes:
    return pa.compress(data, codec="zstd", asbytes=True)


def get_pq_zstd_size(col_name: str) -> int:
    pf = pq.ParquetFile(str(DATA_DIR / "highcard_strings_zstd.parquet"))
    meta = pf.metadata
    total = 0
    for rg_idx in range(meta.num_row_groups):
        rg = meta.row_group(rg_idx)
        for col_idx in range(rg.num_columns):
            cm = rg.column(col_idx)
            if cm.path_in_schema == col_name:
                total += cm.total_compressed_size
    return total


# ──────────────────────────────────────────────────────────────────
# Encoding 1: dict_current (our existing approach — raw dict blob)
# ──────────────────────────────────────────────────────────────────

def encode_dict_current(values: list[str], name: str, out: Path) -> dict:
    unique_sorted = sorted(set(v for v in values if v is not None))
    dict_map = {v: i for i, v in enumerate(unique_sorted)}
    num_unique = len(unique_sorted)

    indices = np.array([dict_map.get(v, 0) for v in values], dtype=np.int32)
    idx_path = out / f"{name}_dict_indices.bin"
    indices.tofile(str(idx_path))

    dict_buf = bytearray()
    for v in unique_sorted:
        s = v.encode("utf-8")
        dict_buf.extend(struct.pack("<I", len(s)))
        dict_buf.extend(s)

    dict_path = out / f"{name}_dict_blob.bin"
    dict_path.write_bytes(dict_buf)

    zl_path = out / f"{name}_dict_indices.zl"
    zl_size, zl_time = zli_compress(idx_path, "le-i32", zl_path)

    total = zl_size + len(dict_buf)
    return {
        "encoding": "dict_current",
        "total_bytes": total,
        "index_zl_bytes": zl_size,
        "dict_bytes": len(dict_buf),
        "dict_compressed_bytes": len(dict_buf),
        "num_unique": num_unique,
        "time_secs": round(zl_time, 1),
    }


# ──────────────────────────────────────────────────────────────────
# Encoding 2: dict_compressed (zstd-compress the dict blob)
# ──────────────────────────────────────────────────────────────────

def encode_dict_compressed(values: list[str], name: str, out: Path) -> dict:
    unique_sorted = sorted(set(v for v in values if v is not None))
    dict_map = {v: i for i, v in enumerate(unique_sorted)}
    num_unique = len(unique_sorted)

    indices = np.array([dict_map.get(v, 0) for v in values], dtype=np.int32)
    idx_path = out / f"{name}_dictc_indices.bin"
    indices.tofile(str(idx_path))

    dict_buf = bytearray()
    for v in unique_sorted:
        s = v.encode("utf-8")
        dict_buf.extend(struct.pack("<I", len(s)))
        dict_buf.extend(s)

    dict_compressed = zstd_compress(bytes(dict_buf))

    zl_path = out / f"{name}_dictc_indices.zl"
    zl_size, zl_time = zli_compress(idx_path, "le-i32", zl_path)

    total = zl_size + len(dict_compressed)
    return {
        "encoding": "dict_compressed",
        "total_bytes": total,
        "index_zl_bytes": zl_size,
        "dict_bytes": len(dict_buf),
        "dict_compressed_bytes": len(dict_compressed),
        "num_unique": num_unique,
        "time_secs": round(zl_time, 1),
    }


# ──────────────────────────────────────────────────────────────────
# Encoding 3: fixed_width (pad to fixed width, compress with le-i64)
# ──────────────────────────────────────────────────────────────────

def encode_fixed_width(values: list[str], name: str, out: Path, pad_to: int = 32) -> dict:
    n = len(values)
    buf = np.zeros((n, pad_to), dtype=np.uint8)
    for i, v in enumerate(values):
        if v is not None:
            b = v.encode("utf-8")[:pad_to]
            buf[i, :len(b)] = list(b)

    bin_path = out / f"{name}_fw{pad_to}.bin"
    buf.tofile(str(bin_path))

    zl_path = out / f"{name}_fw{pad_to}.zl"
    zl_size, zl_time = zli_compress(bin_path, "le-i64", zl_path)

    return {
        "encoding": f"fixed_width_{pad_to}",
        "total_bytes": zl_size,
        "padded_raw_bytes": n * pad_to,
        "pad_width": pad_to,
        "time_secs": round(zl_time, 1),
    }


# ──────────────────────────────────────────────────────────────────
# Encoding 4: chunked_serial (split into small chunks)
# ──────────────────────────────────────────────────────────────────

def encode_chunked_serial(values: list[str], name: str, out: Path,
                          chunk_size: int = 1_000_000) -> dict:
    raw_buf = bytearray()
    for v in values:
        s = (v or "").encode("utf-8")
        raw_buf.extend(struct.pack("<I", len(s)))
        raw_buf.extend(s)

    raw_bytes = len(raw_buf)
    num_chunks = (raw_bytes + chunk_size - 1) // chunk_size
    total_zl = 0
    total_time = 0.0

    for ci in range(num_chunks):
        start = ci * chunk_size
        end = min(start + chunk_size, raw_bytes)
        chunk = raw_buf[start:end]

        chunk_path = out / f"{name}_chunk{ci:03d}.bin"
        chunk_path.write_bytes(chunk)

        zl_path = out / f"{name}_chunk{ci:03d}.zl"
        zl_size, zl_time = zli_compress(chunk_path, "serial", zl_path)
        total_zl += zl_size
        total_time += zl_time

    return {
        "encoding": "chunked_serial",
        "total_bytes": total_zl,
        "raw_bytes": raw_bytes,
        "num_chunks": num_chunks,
        "chunk_size": chunk_size,
        "time_secs": round(total_time, 1),
    }


# ──────────────────────────────────────────────────────────────────
# Encoding 5: structural decomposition
# ──────────────────────────────────────────────────────────────────

def encode_structural_email(values: list[str], name: str, out: Path) -> dict:
    """Split emails into (username, domain). Dict-encode domain, fixed-width username."""
    usernames = []
    domains = []
    for v in values:
        if v and "@" in v:
            parts = v.rsplit("@", 1)
            usernames.append(parts[0])
            domains.append(parts[1])
        else:
            usernames.append(v or "")
            domains.append("")

    unique_domains = sorted(set(domains))
    domain_map = {d: i for i, d in enumerate(unique_domains)}
    domain_indices = np.array([domain_map[d] for d in domains], dtype=np.uint16)

    domain_idx_path = out / f"{name}_struct_domain_idx.bin"
    domain_indices.tofile(str(domain_idx_path))

    domain_dict_buf = bytearray()
    for d in unique_domains:
        s = d.encode("utf-8")
        domain_dict_buf.extend(struct.pack("<I", len(s)))
        domain_dict_buf.extend(s)
    domain_dict_compressed = zstd_compress(bytes(domain_dict_buf))

    max_user_len = max(len(u.encode("utf-8")) for u in usernames)
    pad_to = ((max_user_len + 7) // 8) * 8
    n = len(usernames)
    user_buf = np.zeros((n, pad_to), dtype=np.uint8)
    for i, u in enumerate(usernames):
        b = u.encode("utf-8")[:pad_to]
        user_buf[i, :len(b)] = list(b)

    user_path = out / f"{name}_struct_usernames.bin"
    user_buf.tofile(str(user_path))

    domain_zl_path = out / f"{name}_struct_domain.zl"
    domain_zl_size, t1 = zli_compress(domain_idx_path, "le-i16", domain_zl_path)

    user_zl_path = out / f"{name}_struct_usernames.zl"
    user_zl_size, t2 = zli_compress(user_path, "le-i64", user_zl_path)

    total = domain_zl_size + len(domain_dict_compressed) + user_zl_size
    return {
        "encoding": "structural_email",
        "total_bytes": total,
        "domain_zl_bytes": domain_zl_size,
        "domain_dict_bytes": len(domain_dict_compressed),
        "domain_unique": len(unique_domains),
        "username_zl_bytes": user_zl_size,
        "username_pad_width": pad_to,
        "time_secs": round(t1 + t2, 1),
    }


def encode_structural_url(values: list[str], name: str, out: Path) -> dict:
    """Split URLs into (prefix, resource_id, action). Compress each part."""
    resources = []
    ids = []
    actions = []

    for v in values:
        parts = (v or "").strip("/").split("/")
        if len(parts) >= 4:
            resources.append(parts[2] if len(parts) > 2 else "")
            try:
                ids.append(int(parts[3]))
            except (ValueError, IndexError):
                ids.append(0)
            actions.append(parts[4] if len(parts) > 4 else "")
        else:
            resources.append("")
            ids.append(0)
            actions.append("")

    unique_resources = sorted(set(resources))
    res_map = {r: i for i, r in enumerate(unique_resources)}
    res_indices = np.array([res_map[r] for r in resources], dtype=np.uint8)
    res_path = out / f"{name}_struct_resource.bin"
    res_indices.tofile(str(res_path))

    id_arr = np.array(ids, dtype=np.int32)
    id_path = out / f"{name}_struct_ids.bin"
    id_arr.tofile(str(id_path))

    unique_actions = sorted(set(actions))
    act_map = {a: i for i, a in enumerate(unique_actions)}
    act_indices = np.array([act_map[a] for a in actions], dtype=np.uint8)
    act_path = out / f"{name}_struct_action.bin"
    act_indices.tofile(str(act_path))

    prefix = "/api/v2/".encode("utf-8")
    prefix_size = len(prefix)

    res_dict_buf = bytearray()
    for r in unique_resources:
        s = r.encode("utf-8")
        res_dict_buf.extend(struct.pack("<I", len(s)))
        res_dict_buf.extend(s)
    act_dict_buf = bytearray()
    for a in unique_actions:
        s = a.encode("utf-8")
        act_dict_buf.extend(struct.pack("<I", len(s)))
        act_dict_buf.extend(s)
    dicts_compressed = zstd_compress(bytes(res_dict_buf) + bytes(act_dict_buf))

    res_zl_path = out / f"{name}_struct_resource.zl"
    res_zl, t1 = zli_compress(res_path, "serial", res_zl_path)

    id_zl_path = out / f"{name}_struct_ids.zl"
    id_zl, t2 = zli_compress(id_path, "le-i32", id_zl_path)

    act_zl_path = out / f"{name}_struct_action.zl"
    act_zl, t3 = zli_compress(act_path, "serial", act_zl_path)

    total = prefix_size + res_zl + id_zl + act_zl + len(dicts_compressed)
    return {
        "encoding": "structural_url",
        "total_bytes": total,
        "prefix_bytes": prefix_size,
        "resource_zl_bytes": res_zl,
        "id_zl_bytes": id_zl,
        "action_zl_bytes": act_zl,
        "dicts_compressed_bytes": len(dicts_compressed),
        "resource_unique": len(unique_resources),
        "action_unique": len(unique_actions),
        "time_secs": round(t1 + t2 + t3, 1),
    }


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    table = pq.read_table(str(DATA_DIR / "highcard_strings_none.parquet"),
                          columns=["email", "url_path"])

    all_results = {}

    for col_name in ["email", "url_path"]:
        col = table.column(col_name)
        values = col.to_pylist()
        pq_zstd = get_pq_zstd_size(col_name)
        raw_bytes = sum(len((v or "").encode("utf-8")) + 4 for v in values)

        col_out = OUT_DIR / col_name
        col_out.mkdir(parents=True, exist_ok=True)

        print(f"\n{'='*70}")
        print(f"  {col_name}: {len(values):,} values, {raw_bytes/1024/1024:.1f} MiB raw")
        print(f"  Parquet+zstd baseline: {pq_zstd/1024/1024:.2f} MiB")
        print(f"{'='*70}")

        results = {"column": col_name, "parquet_zstd_bytes": pq_zstd, "raw_bytes": raw_bytes, "encodings": {}}

        encodings_to_try = [
            ("dict_current", lambda: encode_dict_current(values, col_name, col_out)),
            ("dict_compressed", lambda: encode_dict_compressed(values, col_name, col_out)),
            ("fixed_width_32", lambda: encode_fixed_width(values, col_name, col_out, pad_to=32)),
            ("chunked_serial", lambda: encode_chunked_serial(values, col_name, col_out)),
        ]

        if col_name == "email":
            encodings_to_try.append(
                ("structural", lambda: encode_structural_email(values, col_name, col_out)))
        elif col_name == "url_path":
            encodings_to_try.append(
                ("structural", lambda: encode_structural_url(values, col_name, col_out)))

        for enc_name, enc_fn in encodings_to_try:
            try:
                print(f"\n  Testing: {enc_name} ...", flush=True)
                result = enc_fn()
                total = result["total_bytes"]
                vs_pq = (1 - total / pq_zstd) * 100 if pq_zstd > 0 else 0
                result["vs_parquet_zstd_pct"] = round(vs_pq, 1)
                results["encodings"][enc_name] = result

                tag = "BETTER" if total < pq_zstd else "WORSE"
                print(f"    {total/1024/1024:>8.2f} MiB  ({vs_pq:+.1f}% vs Parquet+zstd)  [{tag}]  {result.get('time_secs',0):.0f}s")

            except Exception as e:
                print(f"    FAILED: {e}")
                results["encodings"][enc_name] = {"error": str(e)[:200]}

        all_results[col_name] = results

    results_path = RESULTS_DIR / "string_encoding_experiment.json"
    with open(results_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {results_path}")

    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    for col_name, data in all_results.items():
        pq_size = data["parquet_zstd_bytes"]
        print(f"\n  {col_name} (Parquet+zstd = {pq_size/1024/1024:.2f} MiB):")
        for enc_name, enc_data in data["encodings"].items():
            if "error" in enc_data:
                print(f"    {enc_name:20s}  FAILED")
                continue
            total = enc_data["total_bytes"]
            vs = enc_data.get("vs_parquet_zstd_pct", 0)
            tag = "BETTER" if total < pq_size else "worse"
            print(f"    {enc_name:20s}  {total/1024/1024:>7.2f} MiB  ({vs:+.1f}%)  [{tag}]")


if __name__ == "__main__":
    main()
