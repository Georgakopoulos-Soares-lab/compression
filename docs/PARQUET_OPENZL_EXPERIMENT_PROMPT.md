# Implementer Prompt: OpenZL vs Parquet Compression — Layout A Experiment

## Mission

Build and run a head-to-head compression benchmark: **OpenZL with SDDL schemas** vs **Parquet's built-in encoding+zstd pipeline**, on the same raw columnar data.

You will build two Python scripts:

1. **`extract_for_openzl.py`** — Reads Parquet files, extracts raw column values into a flat binary format, and auto-generates SDDL schemas.
2. **`benchmark_openzl.py`** — Trains OpenZL compressors, compresses the data, and prints a detailed comparison against Parquet.

Both scripts go in `parquet_experiments/`.

---

## Background: What We're Comparing

Parquet compresses data in two stages per column:

```
Raw typed values → Encoding (delta/RLE/dictionary/PLAIN) → Codec (zstd) → stored bytes
```

We are testing whether OpenZL can replace **both stages** with a single trained compressor:

```
Raw typed values → flat binary (PBL) → SDDL schema tells OpenZL the types → trained compression → .zl
```

The key bet: Parquet uses PLAIN encoding (no-op) for floats, so zstd sees raw float bytes and achieves only ~1.0-1.3x. OpenZL trains on the data and might learn float-specific patterns that zstd can't.

We also test OpenZL's built-in `parquet` profile as a second comparison point (OpenZL as a drop-in codec replacement within the Parquet format itself).

---

## Repository Layout

```
compression/                          ← repo root
├── nyx/
│   ├── openzl/
│   │   └── zli                       ← THE BINARY (this is the zli executable)
│   └── schemas/
│       └── fasta_packed.sddl         ← reference SDDL schema (working syntax example)
├── parquet_experiments/               ← YOUR WORKING DIRECTORY
│   ├── data/                         ← ALREADY EXISTS — 16 Parquet files
│   │   ├── numeric_heavy_none.parquet
│   │   ├── numeric_heavy_zstd.parquet
│   │   ├── ml_features_none.parquet
│   │   ├── ml_features_zstd.parquet
│   │   ├── mixed_type_none.parquet
│   │   ├── mixed_type_zstd.parquet
│   │   └── ... (snappy, gzip variants too)
│   ├── generate_samples.py           ← ALREADY EXISTS — how the data was created
│   ├── inspect_parquet.py            ← ALREADY EXISTS — Parquet internals inspector
│   ├── compare_compression.py        ← ALREADY EXISTS — Parquet codec comparison
│   ├── extract_for_openzl.py         ← YOU BUILD THIS
│   ├── benchmark_openzl.py           ← YOU BUILD THIS
│   ├── extracted/                    ← YOU CREATE THIS (output of extract script)
│   ├── schemas/                      ← YOU CREATE THIS (auto-generated SDDL files)
│   ├── models/                       ← YOU CREATE THIS (trained compressor models)
│   └── results/                      ← YOU CREATE THIS (benchmark output)
```

---

## Script 1: `extract_for_openzl.py`

### What It Does

For each dataset, this script:
1. Reads the `_none.parquet` file (uncompressed variant) with pyarrow
2. Identifies all numeric columns (skips string columns)
3. Extracts raw column values as numpy arrays
4. Writes them to a flat binary file (PBL format)
5. Auto-generates an SDDL schema that matches the binary layout
6. Saves metadata as JSON

### The Binary Format

We call this "PBL" (Parquet Binary Layout). It is NOT a standard — it's a trivial container we invented for this experiment. It exists solely to give SDDL something to parse.

```
Byte offset   | Size               | Content
──────────────|────────────────────|──────────────────────────────
0             | 4 bytes            | Magic: b"PBL1" (ASCII)
4             | 4 bytes            | num_rows : uint32, little-endian
8             | num_rows × width₁  | Column 1 values, contiguous, little-endian
8 + N×width₁  | num_rows × width₂  | Column 2 values, contiguous, little-endian
...           | ...                | ... (columns in schema order)
```

No padding between columns. No per-column headers. Just raw typed bytes.

### How to Create It (Core Logic)

```python
import pyarrow.parquet as pq
import numpy as np
import struct

table = pq.read_table("data/numeric_heavy_none.parquet")

with open("extracted/numeric_heavy/data.pbl", "wb") as f:
    # ── Header ──
    f.write(b"PBL1")                                   # 4 bytes magic
    f.write(struct.pack("<I", table.num_rows))          # 4 bytes uint32 LE

    # ── Column data ──
    for col_name in numeric_column_names:
        col = table.column(col_name)

        # Convert to numpy, filling nulls
        if col.type in (pa.float32(), pa.float64()):
            arr = col.to_numpy(zero_copy_only=False)    # NaN fills automatically
        elif col.type == pa.bool_():
            arr = col.to_numpy(zero_copy_only=False).astype(np.uint8)
        elif col.type == pa.timestamp("us"):
            arr = col.cast(pa.int64()).to_numpy(zero_copy_only=False)
        else:  # integers
            arr = col.to_numpy(zero_copy_only=False)
            arr = np.nan_to_num(arr, nan=0).astype(arr.dtype)

        # Ensure little-endian byte order
        if arr.dtype.byteorder not in ('<', '=', '|'):
            arr = arr.byteswap().newbyteorder('<')

        f.write(arr.tobytes())
```

The result for `numeric_heavy` (3M rows × 7 columns):
- 4 (magic) + 4 (num_rows) + 3M×(8+4+4+4+4+4+2) = **~85.8 MiB**

### Type Mapping Table

| Parquet Type          | Numpy dtype    | SDDL Type     | Byte Width |
|-----------------------|----------------|---------------|------------|
| `pa.int16()`          | `<i2`          | `Int16LE`     | 2          |
| `pa.int32()`          | `<i4`          | `Int32LE`     | 4          |
| `pa.int64()`          | `<i8`          | `Int64LE`     | 8          |
| `pa.float32()`        | `<f4`          | `Float32LE`   | 4          |
| `pa.float64()`        | `<f8`          | `Float64LE`   | 8          |
| `pa.bool_()`          | `uint8`        | `Byte`        | 1          |
| `pa.timestamp("us")`  | `<i8` (cast)   | `Int64LE`     | 8          |
| `pa.string()`         | **SKIP**       | —             | —          |

### Null Handling

- **Floats**: pyarrow's `to_numpy()` converts nulls to `NaN` automatically. Leave as NaN.
- **Integers**: replace nulls with `0`. Use `np.nan_to_num(arr, nan=0)`.
- **Booleans**: replace nulls with `False` (0). Cast to `uint8`.
- **Timestamps**: cast to INT64 first (`col.cast(pa.int64())`), then handle as integer.

### SDDL Schema Auto-Generation

**CRITICAL RULE — read this carefully**:

In SDDL, every direct use of a built-in type name (`Float32LE`, `Int64LE`, etc.) creates a **separate field instance** with its own **independent compression stream**. This is what we want — each column gets its own stream so OpenZL can learn column-specific patterns.

**DO NOT create type aliases** for column types. If you write `F32 = Float32LE` and then use `F32` for multiple columns, they all share ONE compression stream. This destroys column independence and will produce bad compression.

**DO write each column with the built-in type name directly:**

```sddl
# CORRECT — every Float32LE usage creates a separate stream
temperature : Float32LE[num_rows]
humidity    : Float32LE[num_rows]    # ← separate stream from temperature
pressure    : Float32LE[num_rows]    # ← separate stream from both
```

```sddl
# WRONG — all floats share one stream, terrible for compression
F32 = Float32LE
temperature : F32[num_rows]
humidity    : F32[num_rows]     # ← SAME stream as temperature!
```

**Generated schema template** (your script writes this as a `.sddl` text file):

```sddl
# Auto-generated SDDL for: numeric_heavy
# Source: numeric_heavy_none.parquet (3,000,000 rows × 7 numeric columns)
# Layout: Full columnar (Layout A) — all values of col 1, then col 2, etc.
# Generated by extract_for_openzl.py

: Byte[4]            # Magic "PBL1" — consumed, not referenced
num_rows : UInt32LE  # Row count → used as array dimension below

# ── Column streams ──────────────────────────────────────────────
timestamp       : Int64LE[num_rows]
device_id       : Int32LE[num_rows]
temperature     : Float32LE[num_rows]
humidity        : Float32LE[num_rows]
pressure        : Float32LE[num_rows]
battery_voltage : Float32LE[num_rows]
status_code     : Int16LE[num_rows]

: Byte[_rem]   # Permissive trailer
```

### Reference: Working SDDL Syntax

This is our production SDDL schema at `nyx/schemas/fasta_packed.sddl` — proof that this syntax works with our `zli` build:

```sddl
U32 = UInt32LE

magic       : Byte[4]
num_records : U32

hdr_offsets : U32[num_records + 1]
seq_offsets : U32[num_records + 1]
seq_lengths : U32[num_records]

hdr_total   : U32
seq_total   : U32
hdr_pad     : U32
seq_pad     : U32

headers     : Byte[hdr_total + hdr_pad]
sequences   : Byte[seq_total + seq_pad]

: Byte[_rem]
```

Key syntax: `: Byte[4]` consumes bytes anonymously. `num_rows : UInt32LE` stores a parsed value referenceable in array lengths. `: Byte[_rem]` consumes all remaining bytes. `#` for comments.

### Datasets to Process

| Dataset          | Source File                    | Extract Columns                                                                                                                                        | Skip Columns                        |
|------------------|--------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------|
| `numeric_heavy`  | `numeric_heavy_none.parquet`   | timestamp (INT64), device_id (INT32), temperature (F32), humidity (F32), pressure (F32), battery_voltage (F32), status_code (INT16)                      | — (all columns are numeric)          |
| `ml_features`    | `ml_features_none.parquet`     | entity_id (INT64), feature_01 through feature_50 (all F32)                                                                                              | — (all columns are numeric)          |
| `mixed_type`     | `mixed_type_none.parquet`      | order_id (INT64), customer_id (INT32), order_date (TIMESTAMP[us]→INT64), quantity (INT16), unit_price (F64), total_amount (F64), is_returned (BOOL→Byte) | product_name, shipping_country       |

### CLI Interface

```bash
python extract_for_openzl.py [--datasets numeric_heavy,ml_features,mixed_type] [--data-dir data/] [--output-dir extracted/] [--schema-dir schemas/]
```

Defaults: all 3 datasets, `data/`, `extracted/`, `schemas/`.

### Logging Requirements

Print clear, timestamped logs at every step. Example output:

```
[2026-02-14 10:30:01] ════════════════════════════════════════════════════════
[2026-02-14 10:30:01] Dataset: numeric_heavy
[2026-02-14 10:30:01] Source:  data/numeric_heavy_none.parquet (77.5 MiB)
[2026-02-14 10:30:01] ════════════════════════════════════════════════════════
[2026-02-14 10:30:01] Reading Parquet file...
[2026-02-14 10:30:02] Table: 3,000,000 rows × 7 columns
[2026-02-14 10:30:02] Extracting numeric columns:
[2026-02-14 10:30:02]   1. timestamp       : int64        → Int64LE    (8 bytes/row)
[2026-02-14 10:30:02]   2. device_id       : int32        → Int32LE    (4 bytes/row)
[2026-02-14 10:30:02]   3. temperature     : float        → Float32LE  (4 bytes/row)   [30,000 nulls → NaN]
[2026-02-14 10:30:02]   4. humidity        : float        → Float32LE  (4 bytes/row)   [24,000 nulls → NaN]
[2026-02-14 10:30:02]   5. pressure        : float        → Float32LE  (4 bytes/row)   [15,000 nulls → NaN]
[2026-02-14 10:30:02]   6. battery_voltage : float        → Float32LE  (4 bytes/row)
[2026-02-14 10:30:02]   7. status_code     : int16        → Int16LE    (2 bytes/row)
[2026-02-14 10:30:02] Skipping 0 string columns.
[2026-02-14 10:30:02] Writing PBL binary: extracted/numeric_heavy/data.pbl
[2026-02-14 10:30:03]   Header:  8 bytes (magic + num_rows)
[2026-02-14 10:30:03]   Columns: 90,000,000 bytes (30 bytes/row × 3,000,000 rows)
[2026-02-14 10:30:03]   Total:   90,000,008 bytes (85.8 MiB)
[2026-02-14 10:30:03] Writing SDDL schema: schemas/numeric_heavy.sddl
[2026-02-14 10:30:03] Writing metadata:    extracted/numeric_heavy/metadata.json
[2026-02-14 10:30:03] ✓ numeric_heavy complete (1.2s)
```

### Output Files

For each dataset:

| File | Purpose |
|------|---------|
| `extracted/<dataset>/data.pbl` | Flat binary blob (PBL format) |
| `schemas/<dataset>.sddl` | Auto-generated SDDL schema |
| `extracted/<dataset>/metadata.json` | Metadata for the benchmark script |

The metadata JSON must include:

```json
{
  "dataset": "numeric_heavy",
  "source_file": "numeric_heavy_none.parquet",
  "num_rows": 3000000,
  "columns": [
    {"name": "timestamp", "parquet_type": "int64", "sddl_type": "Int64LE", "byte_width": 8, "null_count": 0},
    {"name": "device_id", "parquet_type": "int32", "sddl_type": "Int32LE", "byte_width": 4, "null_count": 0},
    {"name": "temperature", "parquet_type": "float", "sddl_type": "Float32LE", "byte_width": 4, "null_count": 30000},
    ...
  ],
  "pbl_size_bytes": 90000008,
  "raw_column_bytes": 90000000,
  "parquet_none_bytes": 81281670,
  "parquet_zstd_bytes": 55232971,
  "parquet_gzip_bytes": 55537621,
  "parquet_snappy_bytes": 72085305,
  "extracted_at": "2026-02-14T10:30:03"
}
```

**Parquet file sizes**: Read these by `stat()`-ing the `data/<dataset>_<codec>.parquet` files. They include ALL columns (even strings we skipped), so they're not a perfectly fair comparison — but they're the practical user-facing numbers.

### Verification

After extraction, verify the PBL file size matches expectations:

```python
expected = 8  # header
for col in columns:
    expected += num_rows * col["byte_width"]
actual = Path("extracted/<dataset>/data.pbl").stat().st_size
assert actual == expected, f"PBL size mismatch: {actual} != {expected}"
```

Print this check in the logs.

---

## Script 2: `benchmark_openzl.py`

### What It Does

For each extracted dataset:
1. Locates the PBL binary and SDDL schema
2. Trains an OpenZL compressor using the SDDL profile
3. Compresses the PBL binary with the trained compressor
4. Also compresses the original `_none.parquet` file using OpenZL's built-in `parquet` profile
5. Collects all sizes and timings
6. Prints a detailed comparison table
7. Saves full results to CSV and JSON

### zli Binary Location

The `zli` binary is at: `<repo_root>/nyx/openzl/zli`

From `parquet_experiments/`, that's `../nyx/openzl/zli`.

Find it programmatically:

```python
from pathlib import Path

def find_zli() -> Path:
    """Locate the zli binary."""
    # Relative to this script's location
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    zli = repo_root / "nyx" / "openzl" / "zli"
    if zli.is_file():
        return zli
    raise FileNotFoundError(
        f"zli not found at {zli}. Run 'nyx build' or set NYX_ZLI env var."
    )
```

### Pipeline Per Dataset

Execute these steps **sequentially**, logging every command and its output:

#### Step 1: Preflight Checks

```python
pbl_file = Path("extracted/<dataset>/data.pbl")
sddl_file = Path("schemas/<dataset>.sddl")
metadata = json.load(open(f"extracted/<dataset>/metadata.json"))

assert pbl_file.exists(), f"Missing: {pbl_file}"
assert sddl_file.exists(), f"Missing: {sddl_file}"
```

#### Step 2: Create Training Directory

`zli train` expects a **directory** of sample files, not a single file. Create a directory and symlink the PBL file into it:

```python
train_dir = Path(f"models/<dataset>/train")
train_dir.mkdir(parents=True, exist_ok=True)
link = train_dir / "data.pbl"
if not link.exists():
    link.symlink_to(pbl_file.resolve())
```

#### Step 3: Train (OpenZL + SDDL)

```bash
<zli> train models/<dataset>/train/ \
    --profile sddl \
    --profile-arg <ABSOLUTE path to schemas/<dataset>.sddl> \
    --output models/<dataset>/compressor.model \
    --use-all-samples \
    --threads <cpu_count or user-specified> \
    --max-time-secs 300 \
    --force
```

**CRITICAL**: The `--profile-arg` value MUST be an absolute path. Use `sddl_file.resolve()` in Python. If you pass a relative path, `zli` may resolve it from the wrong directory and fail to find the schema.

Time this step. Capture both stdout and stderr. Log both.

#### Step 4: Compress with Trained Model (OpenZL + SDDL)

```bash
<zli> compress extracted/<dataset>/data.pbl \
    --compressor models/<dataset>/compressor.model \
    --output results/<dataset>.pbl.zl \
    --force
```

Time this step. Log the command and output.

#### Step 5: Compress with Built-in Parquet Profile (Path A Baseline)

```bash
<zli> compress data/<dataset>_none.parquet \
    --profile parquet \
    --train-inline \
    --output results/<dataset>_parquet_profile.zl \
    --force
```

This uses OpenZL's built-in Parquet parser. It compresses the Parquet file directly — no extraction needed. It trains inline (on the same file it compresses). Time this step.

#### Step 6: Collect Measurements

```python
results = {
    "dataset": dataset,
    "num_rows": metadata["num_rows"],
    "num_cols": len(metadata["columns"]),
    "raw_column_bytes": metadata["raw_column_bytes"],      # sum of (num_rows × width) per col
    "pbl_bytes": metadata["pbl_size_bytes"],                # PBL file size (raw + 8 byte header)
    "parquet_none_bytes": metadata["parquet_none_bytes"],    # _none.parquet file size
    "parquet_zstd_bytes": metadata["parquet_zstd_bytes"],    # _zstd.parquet file size
    "parquet_gzip_bytes": metadata["parquet_gzip_bytes"],
    "parquet_snappy_bytes": metadata["parquet_snappy_bytes"],
    "openzl_sddl_bytes": Path(f"results/{dataset}.pbl.zl").stat().st_size,
    "openzl_parquet_bytes": Path(f"results/{dataset}_parquet_profile.zl").stat().st_size,
    "train_time_secs": ...,       # wall clock for Step 3
    "compress_sddl_secs": ...,    # wall clock for Step 4
    "compress_parquet_secs": ...,  # wall clock for Step 5
}

# Compute ratios (higher = better compression)
results["openzl_sddl_ratio"] = results["raw_column_bytes"] / results["openzl_sddl_bytes"]
results["openzl_parquet_ratio"] = results["parquet_none_bytes"] / results["openzl_parquet_bytes"]
results["parquet_zstd_ratio"] = results["raw_column_bytes"] / results["parquet_zstd_bytes"]
```

### Logging Requirements

**Every subprocess command must be logged in full** — the exact command string, its exit code, its stdout, and its stderr. If a command fails, print the full error output before moving to the next dataset.

Example output for one dataset:

```
[10:32:00] ════════════════════════════════════════════════════════════════════
[10:32:00] BENCHMARK: numeric_heavy
[10:32:00] ════════════════════════════════════════════════════════════════════
[10:32:00] PBL file:    extracted/numeric_heavy/data.pbl (85.8 MiB)
[10:32:00] SDDL schema: schemas/numeric_heavy.sddl (7 columns)
[10:32:00] ────────────────────────────────────────────────────────────────────
[10:32:00] Step 1/4: Training OpenZL compressor (SDDL profile, max 300s)...
[10:32:00] CMD: /path/to/zli train models/numeric_heavy/train/ --profile sddl --profile-arg /abs/path/schemas/numeric_heavy.sddl --output models/numeric_heavy/compressor.model --use-all-samples --threads 14 --max-time-secs 300 --force
[10:32:00] [zli stdout streams here as training runs]
[10:37:12] Training complete: 312.4s
[10:37:12] Model: models/numeric_heavy/compressor.model (245 KiB)
[10:37:12] ────────────────────────────────────────────────────────────────────
[10:37:12] Step 2/4: Compressing PBL with trained model...
[10:37:12] CMD: /path/to/zli compress extracted/numeric_heavy/data.pbl --compressor models/numeric_heavy/compressor.model --output results/numeric_heavy.pbl.zl --force
[10:37:18] Compression complete: 6.2s
[10:37:18] Output: results/numeric_heavy.pbl.zl (42.3 MiB)
[10:37:18] ────────────────────────────────────────────────────────────────────
[10:37:18] Step 3/4: Compressing Parquet with built-in parquet profile (Path A)...
[10:37:18] CMD: /path/to/zli compress data/numeric_heavy_none.parquet --profile parquet --train-inline --output results/numeric_heavy_parquet_profile.zl --force
[10:37:45] Compression complete: 27.3s
[10:37:45] Output: results/numeric_heavy_parquet_profile.zl (48.1 MiB)
[10:37:45] ────────────────────────────────────────────────────────────────────
[10:37:45] Step 4/4: Results for numeric_heavy
[10:37:45]
[10:37:45]   Pipeline                 │ Size (MiB)  │ Ratio   │ Time
[10:37:45]   ─────────────────────────┼─────────────┼─────────┼──────────
[10:37:45]   Raw column data          │     85.8    │  1.00x  │ —
[10:37:45]   Parquet (none)           │     77.5    │  1.11x  │ —
[10:37:45]   Parquet (snappy)         │     68.7    │  1.25x  │ —
[10:37:45]   Parquet (gzip)           │     53.0    │  1.62x  │ —
[10:37:45]   Parquet (zstd)           │     52.7    │  1.63x  │ —
[10:37:45]   OpenZL+SDDL (Path B)    │     42.3    │  2.03x  │ 312s train + 6s compress
[10:37:45]   OpenZL Parquet (Path A)  │     48.1    │  1.78x  │ 27s (inline)
[10:37:45]
[10:37:45]   WINNER: OpenZL+SDDL (Path B) — 1.24x smaller than Parquet+zstd
[10:37:45] ════════════════════════════════════════════════════════════════════
```

(The actual numbers are hypothetical — the point is the format.)

### Final Summary Table

After all datasets, print a combined summary:

```
╔══════════════════╦═══════════╦═══════════════╦════════════════╦═════════════════╦══════════╗
║ Dataset          ║ Raw (MiB) ║ Parquet+zstd  ║ OpenZL+SDDL   ║ OpenZL Parquet  ║ Winner   ║
╠══════════════════╬═══════════╬═══════════════╬════════════════╬═════════════════╬══════════╣
║ numeric_heavy    ║     85.8  ║  52.7 (1.63x) ║  ??.? (??.?x)  ║  ??.? (??.?x)   ║ ???      ║
║ ml_features      ║     99.2  ║  70.0 (1.42x) ║  ??.? (??.?x)  ║  ??.? (??.?x)   ║ ???      ║
║ mixed_type       ║     74.4  ║  30.9 (2.41x) ║  ??.? (??.?x)  ║  ??.? (??.?x)   ║ ???      ║
╚══════════════════╩═══════════╩═══════════════╩════════════════╩═════════════════╩══════════╝
```

### Results Storage

Save ALL results to two files:

1. **`results/comparison.csv`** — One row per dataset, machine-readable:

```csv
dataset,num_rows,num_cols,raw_column_bytes,pbl_bytes,parquet_none_bytes,parquet_snappy_bytes,parquet_gzip_bytes,parquet_zstd_bytes,openzl_sddl_bytes,openzl_sddl_ratio,openzl_parquet_bytes,openzl_parquet_ratio,parquet_zstd_ratio,train_time_secs,compress_sddl_secs,compress_parquet_secs
numeric_heavy,3000000,7,90000000,90000008,81281670,72085305,55537621,55232971,...
```

2. **`results/comparison.json`** — Full structured results including all metadata, column details, and the exact commands run:

```json
{
  "experiment": "openzl_vs_parquet_layout_a",
  "run_date": "2026-02-14T10:30:00",
  "zli_path": "/abs/path/to/zli",
  "datasets": [
    {
      "dataset": "numeric_heavy",
      "num_rows": 3000000,
      "num_cols": 7,
      "columns": [...],
      "raw_column_bytes": 90000000,
      "pbl_bytes": 90000008,
      "parquet_none_bytes": 81281670,
      "parquet_zstd_bytes": 55232971,
      "openzl_sddl_bytes": ...,
      "openzl_sddl_ratio": ...,
      "openzl_parquet_bytes": ...,
      "openzl_parquet_ratio": ...,
      "train_time_secs": ...,
      "compress_sddl_secs": ...,
      "compress_parquet_secs": ...,
      "train_command": "...",
      "compress_command": "...",
      "train_stdout": "...",
      "train_stderr": "...",
      "status": "success"
    },
    ...
  ]
}
```

### CLI Interface

```bash
python benchmark_openzl.py [--datasets numeric_heavy,ml_features,mixed_type] [--max-train-time 300] [--threads 0] [--skip-existing]
```

- `--datasets`: comma-separated list, default all 3
- `--max-train-time`: seconds for `--max-time-secs` flag (default 300 = 5 minutes per dataset)
- `--threads`: thread count for training (default 0 = all CPUs)
- `--skip-existing`: skip training if model file already exists (for resumability)

### Error Handling

- If `zli` not found: print location hint and exit
- If training fails for one dataset: log full error (stderr, exit code), mark as "failed" in results, continue to next dataset
- If compression fails: same — log and continue
- Never abort the whole run because one dataset failed

---

## Execution Order

The implementer should run:

```bash
cd parquet_experiments/

# Step 1: Extract all datasets
python extract_for_openzl.py

# Step 2: Run benchmarks (this will take ~15-20 minutes total)
python benchmark_openzl.py
```

After completion, all results will be in `results/comparison.csv` and `results/comparison.json`.

---

## zli Reference

### Train Options

```
zli train <sample-dir> [options]

Required:
  sample-dir                 Directory containing sample files to train on

Key options:
  --profile sddl             Use SDDL profile (required for our experiment)
  --profile-arg <path>       ABSOLUTE path to .sddl schema file
  --output <path>            Output model file path
  --force                    Overwrite existing output
  --use-all-samples          Use all files in the directory
  --threads <N>              Thread count (default: all CPUs)
  --max-time-secs <N>        Training time limit in seconds
  --trainer <algo>           full-split | greedy (default) | bottom-up
  --no-ace-successors        Disable ACE successors
  --no-clustering            Skip clustering
```

### Compress Options

```
zli compress <input-file> [options]

Key options:
  --compressor <path>        Path to trained model file
  --output <path>            Output file path
  --force                    Overwrite existing output
  --profile <name>           Use built-in profile (e.g., "parquet")
  --train-inline             Train on the input file before compressing
```

### Available Profiles

```
csv, le-i16, le-i32, le-i64, le-u16, le-u32, le-u64,
parquet, pytorch, sao, sddl, serial
```

---

## Gotchas — Read Before You Start

1. **`--profile-arg` MUST be an absolute path.** `zli` resolves the schema from its own working directory. Always use `Path.resolve()` or `os.path.abspath()`.

2. **`zli train` takes a DIRECTORY, not a file.** Create `models/<dataset>/train/` and symlink/copy `data.pbl` into it.

3. **End every SDDL schema with `: Byte[_rem]`**. This consumes any trailing bytes. Without it, `zli` errors if there's even 1 byte of unexpected data at the end.

4. **No type aliases for column fields.** Each column must use the built-in type name directly (`Float32LE`, `Int64LE`, etc.) to get its own compression stream.

5. **Numpy byte order.** macOS (ARM and x86) uses little-endian by default, but be explicit: check `arr.dtype.byteorder` or use `.astype('<f4')` notation to guarantee LE.

6. **Parquet file sizes include string columns.** The `mixed_type_zstd.parquet` size includes `product_name` and `shipping_country` columns that we excluded from our PBL file. Note this in the results — the Parquet sizes are the practical numbers a user would see, but not a perfectly apples-to-apples comparison for `mixed_type`. For `numeric_heavy` and `ml_features`, all columns are numeric so the comparison is clean.

7. **Subprocess timeout.** Training can take up to `--max-time-secs` + overhead. Set your Python subprocess timeout to at least `max_time_secs + 60` to avoid killing training prematurely.

8. **Stream training output in real-time.** Use `subprocess.Popen` with line-by-line stdout reading (not `subprocess.run`) for the training step so the user can see progress. Fall back to `subprocess.run` with `capture_output=True` if streaming is too complex.

---

## Verification Checklist

Before declaring done:

- [ ] `extract_for_openzl.py` runs without errors on all 3 datasets
- [ ] Each `.pbl` file size matches: 8 + sum(num_rows × byte_width per col)
- [ ] Each `.sddl` schema uses built-in type names directly — NO aliases for column types
- [ ] `metadata.json` is written for each dataset with all Parquet variant sizes
- [ ] `zli train` with `--max-time-secs 10` completes without error on at least one dataset (quick sanity check)
- [ ] `zli compress` with the trained model produces a `.zl` file
- [ ] The built-in `parquet` profile test also produces a `.zl` file
- [ ] Per-dataset results are logged clearly with exact sizes and ratios
- [ ] Final summary table is printed after all datasets
- [ ] `results/comparison.csv` is written
- [ ] `results/comparison.json` is written with full details including commands run
