#!/usr/bin/env python3
"""Generate diverse sample Parquet files for OpenZL compression experiments.

Creates four datasets simulating common enterprise data patterns:
  1. numeric_heavy  — IoT sensor readings    (3M rows,  7 cols, ~80-110 MiB uncompressed)
  2. string_heavy   — Application event logs  (1M rows,  6 cols, ~80-100 MiB uncompressed)
  3. mixed_type     — Business order analytics (2M rows,  9 cols, ~80-100 MiB uncompressed)
  4. ml_features    — ML feature store         (500K rows, 51 cols, ~100 MiB uncompressed)

Each dataset is written with four compression variants:
  none (uncompressed), snappy, gzip, zstd

Output directory: parquet_experiments/data/
File naming:      {dataset}_{compression}.parquet
Total files:      16

Design notes:
  - Data has realistic patterns (correlations, trends, power-law distributions)
    to exercise Parquet's encoding machinery (delta, RLE, dictionary) and produce
    meaningful compression benchmarks — NOT random noise.
  - Null values are scattered across columns at realistic rates (0.5%-5%).
  - All datasets use a fixed seed for reproducibility.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parent / "data"

COMPRESSIONS = [
    ("none", "NONE"),
    ("snappy", "snappy"),
    ("gzip", "gzip"),
    ("zstd", "zstd"),
]


# ─────────────────────────────────────────────────────────────────────────────
# Dataset 1: Numeric-heavy (IoT / sensor telemetry)
# ─────────────────────────────────────────────────────────────────────────────

def generate_numeric_heavy(num_rows: int = 3_000_000, seed: int = 42) -> pa.Table:
    """Simulate IoT sensor readings with temporal and physical correlations.

    Key patterns for compression analysis:
      - timestamp:       monotonic with jitter  → delta-encoding friendly
      - device_id:       1,000 unique values     → small dictionary
      - temperature:     sinusoidal daily cycle   → autocorrelated floats
      - humidity:        anti-correlated with temperature
      - pressure:        random walk              → strongly autocorrelated
      - battery_voltage: sawtooth discharge       → periodic pattern
      - status_code:     90% zeros                → RLE-friendly
    """
    print(f"  Generating {num_rows:,} sensor readings...")
    rng = np.random.default_rng(seed)

    # ── Timestamps: ~1-second intervals with ms jitter ──
    base_ms = 1_700_000_000_000  # ~Nov 2023 in milliseconds
    timestamps = base_ms + np.arange(num_rows, dtype=np.int64) * 1000
    timestamps += rng.integers(-200, 200, size=num_rows)

    # ── Device IDs: 1,000 devices, uniform ──
    device_ids = rng.integers(1, 1001, size=num_rows, dtype=np.int32)

    # ── Temperature: daily sinusoidal + Gaussian noise ──
    hour_frac = (np.arange(num_rows) % 86400) / 86400.0
    daily_cycle = 22.0 + 8.0 * np.sin(2 * np.pi * (hour_frac - 0.25))
    temperature = (daily_cycle + rng.normal(0, 1.5, num_rows)).astype(np.float32)

    # ── Humidity: anti-correlated with temperature ──
    humidity = np.clip(
        75.0 - 1.2 * (temperature - 22.0) + rng.normal(0, 4.0, num_rows),
        5.0, 100.0,
    ).astype(np.float32)

    # ── Pressure: random walk around 1013 hPa ──
    pressure = (1013.25 + np.cumsum(rng.normal(0, 0.02, num_rows))).astype(np.float32)

    # ── Battery voltage: sawtooth discharge (4.2V → 3.2V per 10K readings) ──
    cycle_pos = (np.arange(num_rows) % 10_000) / 10_000.0
    battery = (4.2 - 1.0 * cycle_pos + rng.normal(0, 0.02, num_rows)).astype(np.float32)

    # ── Status code: power-law skew (90% = 0) ──
    status_probs = [0.90, 0.04, 0.025, 0.02, 0.01, 0.005]
    status = rng.choice(len(status_probs), size=num_rows, p=status_probs).astype(np.int16)

    # ── Null masks ──
    temp_nulls = rng.random(num_rows) < 0.01
    hum_nulls = rng.random(num_rows) < 0.008
    pres_nulls = rng.random(num_rows) < 0.005

    return pa.table({
        "timestamp":       pa.array(timestamps, type=pa.int64()),
        "device_id":       pa.array(device_ids, type=pa.int32()),
        "temperature":     pa.array(temperature, type=pa.float32(), mask=temp_nulls),
        "humidity":        pa.array(humidity, type=pa.float32(), mask=hum_nulls),
        "pressure":        pa.array(pressure, type=pa.float32(), mask=pres_nulls),
        "battery_voltage": pa.array(battery, type=pa.float32()),
        "status_code":     pa.array(status, type=pa.int16()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dataset 2: String-heavy (application event logs)
# ─────────────────────────────────────────────────────────────────────────────

def generate_string_heavy(num_rows: int = 1_000_000, seed: int = 43) -> pa.Table:
    """Simulate application event logs with mixed-cardinality string columns.

    Key patterns:
      - user_id:      100K unique hex IDs       → large dictionary
      - event_type:   20 values, power-law      → tiny dictionary, very RLE-friendly
      - url:          ~5K unique structured URLs → medium dictionary
      - country:      50 values, weighted        → tiny dictionary
      - payload_json: high cardinality JSON      → hard for dictionary, tests raw compression
    """
    print(f"  Generating {num_rows:,} event log entries...")
    rng = np.random.default_rng(seed)

    # ── Timestamps ──
    base_ms = 1_700_000_000_000
    timestamps = base_ms + np.arange(num_rows, dtype=np.int64) * 50
    timestamps += rng.integers(-10, 10, size=num_rows)

    # ── User IDs: 100K unique, Zipf-distributed activity ──
    num_users = 100_000
    user_pool = [f"usr_{i:012x}" for i in range(num_users)]
    user_weights = 1.0 / np.arange(1, num_users + 1) ** 0.8
    user_weights /= user_weights.sum()
    user_indices = rng.choice(num_users, size=num_rows, p=user_weights)
    user_ids = [user_pool[i] for i in user_indices]

    # ── Event types: 20 values, power-law ──
    event_types = [
        "page_view", "click", "scroll", "form_submit", "search",
        "add_to_cart", "purchase", "login", "logout", "error",
        "signup", "share", "comment", "like", "bookmark",
        "download", "upload", "delete", "update", "notification",
    ]
    event_probs = np.array([
        0.25, 0.18, 0.12, 0.08, 0.06, 0.05, 0.04, 0.035, 0.03, 0.025,
        0.02, 0.015, 0.015, 0.012, 0.01, 0.008, 0.008, 0.006, 0.005, 0.006,
    ])
    event_probs /= event_probs.sum()
    events = rng.choice(event_types, size=num_rows, p=event_probs).tolist()

    # ── URLs: ~5K unique, realistic structure ──
    paths = [
        "/", "/home", "/products", "/products/detail", "/checkout",
        "/cart", "/profile", "/settings", "/search", "/help",
        "/about", "/blog", "/docs", "/api/status", "/dashboard",
        "/admin", "/reports", "/analytics", "/feed", "/messages",
    ]
    query_params = [
        "", "?page=1", "?page=2", "?sort=date", "?sort=price",
        "?q=shoes", "?q=electronics", "?ref=email", "?ref=social",
        "?utm_source=google", "?lang=en", "?lang=es", "?lang=fr",
    ]
    url_pool = [f"https://app.example.com{p}{q}" for p in paths for q in query_params]
    url_indices = rng.integers(0, len(url_pool), size=num_rows)
    urls = [url_pool[i] for i in url_indices]

    # ── Countries: 50, weighted toward US/EU ──
    countries = [
        "US", "GB", "DE", "FR", "CA", "AU", "JP", "BR", "IN", "KR",
        "NL", "ES", "IT", "MX", "SE", "NO", "DK", "FI", "PL", "CH",
        "AT", "BE", "PT", "IE", "NZ", "SG", "HK", "TW", "IL", "ZA",
        "AR", "CL", "CO", "TH", "MY", "ID", "PH", "VN", "CZ", "RO",
        "HU", "GR", "TR", "UA", "NG", "EG", "KE", "AE", "SA", "PK",
    ]
    country_weights = np.array([
        0.25, 0.08, 0.07, 0.06, 0.05, 0.04, 0.04, 0.035, 0.035, 0.03,
        0.025, 0.025, 0.025, 0.02, 0.015, 0.012, 0.012, 0.01, 0.01, 0.01,
        0.008, 0.008, 0.008, 0.008, 0.007, 0.007, 0.006, 0.006, 0.006, 0.006,
        0.005, 0.005, 0.005, 0.005, 0.005, 0.004, 0.004, 0.004, 0.004, 0.004,
        0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003, 0.003,
    ])
    country_weights /= country_weights.sum()
    country_list = rng.choice(countries, size=num_rows, p=country_weights).tolist()

    # ── Payload JSON: high cardinality, semi-structured ──
    actions = ["click", "view", "scroll", "hover", "submit", "type", "drag", "resize"]
    elements = ["button", "link", "input", "image", "card", "menu", "modal", "tab"]

    p_actions = rng.choice(actions, size=num_rows)
    p_elements = rng.choice(elements, size=num_rows)
    p_x = rng.integers(0, 1920, size=num_rows)
    p_y = rng.integers(0, 1080, size=num_rows)
    p_dur = rng.integers(10, 5000, size=num_rows)
    p_sess = rng.integers(1000, 99999, size=num_rows)

    payloads = [
        f'{{"action":"{a}","element":"{e}","x":{x},"y":{y},'
        f'"duration_ms":{d},"session":{s}}}'
        for a, e, x, y, d, s in zip(p_actions, p_elements, p_x, p_y, p_dur, p_sess)
    ]

    return pa.table({
        "timestamp":    pa.array(timestamps, type=pa.int64()),
        "user_id":      pa.array(user_ids, type=pa.string()),
        "event_type":   pa.array(events, type=pa.string()),
        "url":          pa.array(urls, type=pa.string()),
        "country":      pa.array(country_list, type=pa.string()),
        "payload_json": pa.array(payloads, type=pa.string()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dataset 3: Mixed-type (business order analytics)
# ─────────────────────────────────────────────────────────────────────────────

def generate_mixed_type(num_rows: int = 2_000_000, seed: int = 44) -> pa.Table:
    """Simulate a business analytics orders table with diverse column types.

    Key patterns:
      - order_id:         sequential monotonic  → perfect delta encoding
      - customer_id:      50K customers, Zipf   → dictionary with power-law
      - order_date:       timestamps over 2 yrs → delta-friendly
      - product_name:     500 products          → medium dictionary
      - quantity:         geometric(0.4), 1-20  → small integer, RLE-friendly
      - unit_price:       log-normal with .99   → float with patterns
      - total_amount:     correlated with qty * price
      - is_returned:      5% true               → very RLE-friendly boolean
      - shipping_country: 30 values             → small dictionary
    """
    print(f"  Generating {num_rows:,} order records...")
    rng = np.random.default_rng(seed)

    # ── Sequential order IDs ──
    order_ids = np.arange(1_000_000, 1_000_000 + num_rows, dtype=np.int64)

    # ── Customer IDs: 50K customers, power-law ──
    num_customers = 50_000
    cust_weights = 1.0 / np.arange(1, num_customers + 1) ** 0.7
    cust_weights /= cust_weights.sum()
    customer_ids = rng.choice(
        np.arange(1, num_customers + 1, dtype=np.int32),
        size=num_rows, p=cust_weights,
    )

    # ── Order dates: 2 years, growth skew toward recent ──
    base_us = 1_640_000_000_000_000  # Jan 2022 in microseconds
    day_offsets = rng.beta(2, 1.5, size=num_rows) * 730  # skewed toward recent
    order_dates = (base_us + (day_offsets * 86_400_000_000).astype(np.int64))

    # ── Product names: ~500 products, Zipf popularity ──
    adjectives = [
        "Premium", "Basic", "Deluxe", "Pro", "Eco", "Ultra", "Mini",
        "Max", "Classic", "Modern", "Vintage", "Smart", "Organic",
        "Wireless", "Portable",
    ]
    nouns = [
        "Widget", "Gadget", "Tool", "Device", "Kit", "Pack", "Set",
        "Module", "Unit", "Component", "System", "Adapter", "Cable",
        "Sensor", "Display", "Controller", "Hub", "Speaker", "Camera",
        "Keyboard", "Mouse", "Charger", "Battery", "Filter", "Cover",
        "Stand", "Mount", "Bracket", "Sleeve", "Case", "Dock", "Clip",
        "Strap",
    ]
    colors = ["Red", "Blue", "Black", "White", "Green", "Silver", "Gold"]
    product_pool = [f"{a} {n} ({c})" for a in adjectives for n in nouns for c in colors[:3]]
    product_pool = product_pool[:500]  # Trim to 500

    prod_weights = 1.0 / np.arange(1, len(product_pool) + 1) ** 0.6
    prod_weights /= prod_weights.sum()
    products = rng.choice(product_pool, size=num_rows, p=prod_weights).tolist()

    # ── Quantity: geometric distribution, clipped to 1-20 ──
    quantity = np.clip(rng.geometric(0.4, size=num_rows), 1, 20).astype(np.int16)

    # ── Unit price: log-normal with .99/.49 rounding ──
    base_prices = rng.lognormal(3.0, 1.0, size=num_rows)
    unit_price = np.clip(np.round(base_prices * 2) / 2 - 0.01, 0.99, 999.99)

    # ── Total amount: derived, with tax/discount noise ──
    total_amount = np.round(
        quantity.astype(np.float64) * unit_price
        * (1 + rng.uniform(-0.05, 0.15, num_rows)),
        2,
    )

    # ── Is returned: ~5% ──
    is_returned = rng.random(num_rows) < 0.05

    # ── Shipping countries: 30, US-heavy ──
    ship_countries = [
        "US", "CA", "GB", "DE", "FR", "AU", "JP", "BR", "IN", "KR",
        "NL", "ES", "IT", "MX", "SE", "CH", "AT", "BE", "PL", "DK",
        "NO", "FI", "IE", "NZ", "SG", "PT", "CZ", "HU", "GR", "RO",
    ]
    ship_weights = np.array([
        0.30, 0.08, 0.07, 0.06, 0.05, 0.04, 0.04, 0.03, 0.03, 0.025,
        0.02, 0.02, 0.02, 0.015, 0.012, 0.012, 0.01, 0.01, 0.01, 0.008,
        0.008, 0.008, 0.008, 0.007, 0.007, 0.006, 0.005, 0.005, 0.005, 0.005,
    ])
    ship_weights /= ship_weights.sum()
    shipping = rng.choice(ship_countries, size=num_rows, p=ship_weights).tolist()

    return pa.table({
        "order_id":         pa.array(order_ids, type=pa.int64()),
        "customer_id":      pa.array(customer_ids, type=pa.int32()),
        "order_date":       pa.array(order_dates, type=pa.timestamp("us")),
        "product_name":     pa.array(products, type=pa.string()),
        "quantity":         pa.array(quantity, type=pa.int16()),
        "unit_price":       pa.array(unit_price, type=pa.float64()),
        "total_amount":     pa.array(total_amount, type=pa.float64()),
        "is_returned":      pa.array(is_returned, type=pa.bool_()),
        "shipping_country": pa.array(shipping, type=pa.string()),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Dataset 4: ML features (feature store)
# ─────────────────────────────────────────────────────────────────────────────

def generate_ml_features(num_rows: int = 500_000, seed: int = 45) -> pa.Table:
    """Simulate an ML feature store table with 50 FLOAT32 features.

    Feature groups (to exercise different compression strategies):
      Features 01-10: monotonic trends + noise   → delta-encoding friendly
      Features 11-20: normal distributions        → standard float compression
      Features 21-30: sparse (80-95% zeros)       → RLE-friendly
      Features 31-40: low-cardinality (quantized) → dictionary-friendly floats
      Features 41-50: correlated with features 1-10 (linear transforms)

    ~5% nulls scattered across all features.
    """
    print(f"  Generating {num_rows:,} entity feature rows (50 features)...")
    rng = np.random.default_rng(seed)

    columns = {"entity_id": pa.array(np.arange(num_rows, dtype=np.int64), type=pa.int64())}

    # ── Group 1 (01-10): Monotonic trends with noise ──
    # These should compress well with delta encoding.
    trend_sources = {}
    for i in range(1, 11):
        slope = rng.uniform(0.001, 0.1)
        noise_std = rng.uniform(0.01, 0.5)
        values = (np.arange(num_rows, dtype=np.float64) * slope
                  + rng.normal(0, noise_std, num_rows)).astype(np.float32)
        trend_sources[i] = values  # Save for group 5 correlation
        null_mask = rng.random(num_rows) < 0.05
        columns[f"feature_{i:02d}"] = pa.array(values, type=pa.float32(), mask=null_mask)

    # ── Group 2 (11-20): Random normal distributions ──
    # Each with different mean/std — tests how well codecs handle uncorrelated floats.
    for i in range(11, 21):
        mean = rng.uniform(-10, 10)
        std = rng.uniform(0.1, 5.0)
        values = rng.normal(mean, std, num_rows).astype(np.float32)
        null_mask = rng.random(num_rows) < 0.05
        columns[f"feature_{i:02d}"] = pa.array(values, type=pa.float32(), mask=null_mask)

    # ── Group 3 (21-30): Sparse features (80-95% zeros) ──
    # Common in NLP/recommendation systems. RLE should handle the zeros well.
    for i in range(21, 31):
        sparsity = rng.uniform(0.80, 0.95)
        values = np.zeros(num_rows, dtype=np.float32)
        non_zero = rng.random(num_rows) > sparsity
        values[non_zero] = rng.standard_normal(non_zero.sum()).astype(np.float32)
        null_mask = rng.random(num_rows) < 0.05
        columns[f"feature_{i:02d}"] = pa.array(values, type=pa.float32(), mask=null_mask)

    # ── Group 4 (31-40): Quantized / low-cardinality floats ──
    # E.g., bucketed features. Should dictionary-encode well.
    for i in range(31, 41):
        num_levels = rng.integers(5, 50)
        levels = np.sort(rng.uniform(-5, 5, num_levels)).astype(np.float32)
        values = rng.choice(levels, size=num_rows).astype(np.float32)
        null_mask = rng.random(num_rows) < 0.05
        columns[f"feature_{i:02d}"] = pa.array(values, type=pa.float32(), mask=null_mask)

    # ── Group 5 (41-50): Correlated with group 1 ──
    # Linear transforms of features 1-10 — tests whether compression
    # can exploit cross-column correlation (spoiler: Parquet can't, OpenZL might).
    for i in range(41, 51):
        source = np.nan_to_num(trend_sources[i - 40], nan=0.0)
        scale = rng.uniform(0.5, 3.0)
        offset = rng.uniform(-2, 2)
        values = (source * scale + offset
                  + rng.normal(0, 0.1, num_rows)).astype(np.float32)
        null_mask = rng.random(num_rows) < 0.05
        columns[f"feature_{i:02d}"] = pa.array(values, type=pa.float32(), mask=null_mask)

    return pa.table(columns)


# ─────────────────────────────────────────────────────────────────────────────
# File writing & main
# ─────────────────────────────────────────────────────────────────────────────

def save_variants(table: pa.Table, name: str) -> None:
    """Write a table with all compression variants."""
    for comp_label, codec in COMPRESSIONS:
        path = DATA_DIR / f"{name}_{comp_label}.parquet"
        start = time.monotonic()
        pq.write_table(table, str(path), compression=codec)
        elapsed = time.monotonic() - start
        size_mib = path.stat().st_size / (1024 * 1024)
        print(f"    {path.name:<40s}  {size_mib:>7.1f} MiB  ({elapsed:.1f}s)")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    datasets = [
        ("numeric_heavy", generate_numeric_heavy),
        ("string_heavy",  generate_string_heavy),
        ("mixed_type",    generate_mixed_type),
        ("ml_features",   generate_ml_features),
    ]

    total_start = time.monotonic()

    for name, generator in datasets:
        print(f"\n{'=' * 60}")
        print(f"Dataset: {name}")
        print(f"{'=' * 60}")

        gen_start = time.monotonic()
        table = generator()
        gen_elapsed = time.monotonic() - gen_start

        print(f"  → {table.num_rows:,} rows × {table.num_columns} columns "
              f"in {gen_elapsed:.1f}s")
        print(f"  Writing variants:")
        save_variants(table, name)

        # Free memory before generating the next dataset
        del table

    total_elapsed = time.monotonic() - total_start
    print(f"\n{'=' * 60}")
    print(f"All done in {total_elapsed:.1f}s")
    print(f"Output directory: {DATA_DIR}")

    # List all generated files
    files = sorted(DATA_DIR.glob("*.parquet"))
    total_bytes = sum(f.stat().st_size for f in files)
    print(f"Files: {len(files)}")
    print(f"Total size: {total_bytes / (1024**2):.1f} MiB")


if __name__ == "__main__":
    main()
