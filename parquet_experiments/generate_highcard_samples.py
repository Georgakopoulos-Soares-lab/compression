#!/usr/bin/env python3
"""Generate a high-cardinality string dataset for encoding experiments.

Creates one dataset:
    highcard_strings — 1M rows, 8 columns with UUIDs, IPs, emails, URLs, hashes

Written with compression variants: none, zstd
Output: parquet_experiments/data/highcard_strings_{compression}.parquet

Design notes:
  - uuid_v4:      random UUIDs (1M unique, ~36 bytes each)
  - uuid_v7_like: time-ordered UUIDs (sorted, 1M unique)
  - ipv4_addr:    random IPv4 addresses (~65K unique, 7-15 bytes each)
  - email:        generated emails (~100K unique, 15-30 bytes each)
  - url_path:     URL paths (~50K unique, 20-50 bytes each)
  - sha256_hash:  random hex hashes (1M unique, 64 bytes each)
  - status:       5 unique values (baseline low-cardinality)
  - row_id:       sequential INT64 (delta-encoding test)
"""

import hashlib
import sys
import time
import uuid as _uuid
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parent / "data"
SEED = 2026


def generate_highcard_strings(num_rows: int = 1_000_000) -> pa.Table:
    """Create a table with diverse high-cardinality string columns."""
    rng = np.random.default_rng(SEED)
    print(f"  Generating {num_rows:,} rows ...", flush=True)

    # row_id: sequential (delta-encoding test)
    row_id = np.arange(1, num_rows + 1, dtype=np.int64)

    # uuid_v4: random UUIDs (all unique)
    print("    uuid_v4 ...", flush=True)
    uuid_v4 = []
    for _ in range(num_rows):
        hi = int(rng.integers(0, 2**63)) << 64
        lo = int(rng.integers(0, 2**63))
        uuid_v4.append(str(_uuid.UUID(int=hi | lo)))

    # uuid_v7_like: time-ordered UUIDs (sorted, monotonically increasing)
    print("    uuid_v7_like ...", flush=True)
    base_ts = 1_700_000_000_000
    uuid_v7 = []
    for i in range(num_rows):
        ts = base_ts + i
        ts_hex = f"{ts:012x}"
        rand_a = int(rng.integers(0, 2**63))
        rand_b = int(rng.integers(0, 2**63))
        rand_hex = f"{rand_a:016x}{rand_b:016x}"
        uid = f"{ts_hex[:8]}-{ts_hex[8:12]}-7{rand_hex[:3]}-{rand_hex[3:7]}-{rand_hex[7:19]}"
        uuid_v7.append(uid)

    # ipv4_addr: random IPv4 addresses
    print("    ipv4_addr ...", flush=True)
    octets = rng.integers(1, 255, size=(num_rows, 4))
    ipv4_addr = [f"{o[0]}.{o[1]}.{o[2]}.{o[3]}" for o in octets]

    # email: generated emails (~100K unique)
    print("    email ...", flush=True)
    first_names = [f"user{i}" for i in range(1000)]
    last_names = [f"name{i}" for i in range(100)]
    domains = ["gmail.com", "yahoo.com", "outlook.com", "company.io",
               "example.org", "mail.co", "test.net", "acme.com",
               "corp.biz", "startup.dev"]
    emails = []
    for _ in range(num_rows):
        fn = first_names[rng.integers(0, len(first_names))]
        ln = last_names[rng.integers(0, len(last_names))]
        d = domains[rng.integers(0, len(domains))]
        emails.append(f"{fn}.{ln}@{d}")

    # url_path: URL paths (~50K unique)
    print("    url_path ...", flush=True)
    resources = ["users", "orders", "products", "events", "sessions",
                 "metrics", "logs", "configs", "reports", "analytics"]
    actions = ["view", "create", "update", "delete", "list", "export", "import"]
    url_paths = []
    for _ in range(num_rows):
        res = resources[rng.integers(0, len(resources))]
        action = actions[rng.integers(0, len(actions))]
        rid = rng.integers(1, 5001)
        url_paths.append(f"/api/v2/{res}/{rid}/{action}")

    # sha256_hash: random hex hashes (all unique)
    print("    sha256_hash ...", flush=True)
    sha256_hashes = []
    for i in range(num_rows):
        h = hashlib.sha256(f"item-{SEED}-{i}".encode()).hexdigest()
        sha256_hashes.append(h)

    # status: 5 unique values (low-cardinality baseline)
    statuses = ["active", "inactive", "pending", "suspended", "deleted"]
    status_weights = [0.5, 0.2, 0.15, 0.1, 0.05]
    status_col = rng.choice(statuses, size=num_rows, p=status_weights).tolist()

    table = pa.table({
        "row_id": pa.array(row_id, type=pa.int64()),
        "uuid_v4": pa.array(uuid_v4, type=pa.string()),
        "uuid_v7_like": pa.array(uuid_v7, type=pa.string()),
        "ipv4_addr": pa.array(ipv4_addr, type=pa.string()),
        "email": pa.array(emails, type=pa.string()),
        "url_path": pa.array(url_paths, type=pa.string()),
        "sha256_hash": pa.array(sha256_hashes, type=pa.string()),
        "status": pa.array(status_col, type=pa.string()),
    })

    return table


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Generating highcard_strings dataset ...")
    t0 = time.time()
    table = generate_highcard_strings()
    print(f"  Generated in {time.time() - t0:.1f}s")
    print(f"  Shape: {table.num_rows:,} rows x {table.num_columns} cols")

    for suffix, codec in [("none", "NONE"), ("zstd", "zstd")]:
        path = DATA_DIR / f"highcard_strings_{suffix}.parquet"
        pq.write_table(table, str(path), compression=codec)
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"  Written {path.name}: {size_mb:.1f} MiB")

    print("Done.")


if __name__ == "__main__":
    main()
