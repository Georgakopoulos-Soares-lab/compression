# Encoding Experiment: Pre-Compression Encoding + OpenZL

## Purpose

We want to test whether applying **data encoding transformations** before OpenZL compression improves results. Currently, our pipeline feeds raw numeric bytes directly to OpenZL. Parquet applies encoding (dictionary, RLE, delta, byte-stream splitting) before its compression codec (zstd). We already do encoding for strings (dictionary + binary conversion) with excellent results. This experiment extends encoding to numeric columns and booleans.

## Three-Way Comparison

For every column × encoding combination, produce **three numbers**:

1. **Parquet+zstd** — baseline from the Parquet file column metadata
2. **Raw OpenZL** — our current approach (raw bytes → `zli compress --profile <LE> --train-inline`)
3. **Encoded+OpenZL** — the new approach (encode first → `zli compress --train-inline` on the encoded form)

## Setup

### Paths

- Workspace root: `/Users/pavlosrousoglou/Desktop/Cornell/startup/openzl/compression`
- Datasets: `parquet_experiments/data/{dataset}_zstd.parquet`
- `zli` binary: `nyx/openzl/zli` (symlink to `nyx/openzl/cachedObjs/a33dfeb063ea425b910f4ad6d1ddd68a/zli`)
- Existing raw OpenZL baselines: `parquet_experiments/per_column_experiment/per_column_results.json`
- Output directory: `parquet_experiments/encoding_experiment/`
- Output results: `parquet_experiments/encoding_experiment/encoding_results.json`

### Dependencies

Python 3.10+, `pyarrow`, `numpy` (already installed in `nyx/.venv`)

Activate the venv: `source nyx/.venv/bin/activate`

## Encodings to Implement

### 1. Byte-Stream Split (`byte_split`)

Split each N-byte numeric value into N independent single-byte streams. Compress each stream with OpenZL `serial` profile. Total compressed = sum of all stream `.zl` files.

```python
def byte_stream_split(values: np.ndarray, byte_width: int) -> list[np.ndarray]:
    """Split N-byte values into N streams of 1-byte each.
    
    For float32 (4 bytes), this separates:
      Stream 0: mantissa low bytes   (high entropy, noisy)
      Stream 1: mantissa mid bytes   (moderate entropy)
      Stream 2: exponent low + mantissa high (moderate entropy)
      Stream 3: sign + exponent high (very low entropy)
    
    Each stream has len(values) bytes.
    """
    raw = values.tobytes()
    n = len(values)
    streams = []
    for byte_pos in range(byte_width):
        stream = np.frombuffer(raw, dtype=np.uint8)[byte_pos::byte_width].copy()
        streams.append(stream)
    return streams
```

For each stream, write to `{col}_bytesplit_s{i}.bin`, compress with `zli compress --profile serial --train-inline`, record each `.zl` size. Total compressed = sum of all stream sizes.

### 2. XOR-Delta (`xor_delta`)

Compute XOR of consecutive values. For autocorrelated data (e.g., temperature readings that change slowly), XOR of consecutive floats produces values with many leading zero bits — highly compressible.

```python
def xor_delta(values: np.ndarray, byte_width: int) -> np.ndarray:
    """XOR each value with the previous one.
    
    For autocorrelated data, consecutive values share most bits,
    so XOR produces values with many zero bits.
    First value stored as-is.
    """
    # View the float/int array as unsigned integers for XOR
    if byte_width == 4:
        uint_view = values.view(np.uint32)
    elif byte_width == 8:
        uint_view = values.view(np.uint64)
    else:
        raise ValueError(f"Unsupported byte width: {byte_width}")
    
    result = np.empty_like(uint_view)
    result[0] = uint_view[0]
    result[1:] = np.bitwise_xor(uint_view[1:], uint_view[:-1])
    return result
```

Write result to `{col}_xordelta.bin`, compress with the original LE profile (`le-i32` for float32, `le-i64` for float64), record `.zl` size.

### 3. XOR-Delta + Byte-Stream Split (`xor_delta_bytesplit`)

Combine: first XOR-delta, then byte-stream split. This should be very effective for autocorrelated floats — XOR creates many zero bytes, byte-splitting groups them.

### 4. Bit-Packing for Booleans (`bitpack`)

Pack boolean values 8 per byte using `np.packbits`. Compress the packed array with OpenZL `serial` profile.

```python
def bitpack_bools(values: np.ndarray) -> np.ndarray:
    """Pack boolean array: 8 bools per byte."""
    return np.packbits(values.astype(np.uint8))
```

Write to `{col}_bitpack.bin`, compress with `serial` profile.

### 5. Dictionary Encoding for Integers (`dict_int`)

Build a sorted dictionary of unique values. Replace each value with its index in the smallest possible integer container.

```python
def dict_encode_int(values: np.ndarray):
    """Dictionary encode an integer array.
    
    Returns: (indices, index_profile, dict_bytes, num_unique)
    """
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
```

Write indices to `{col}_dict_indices.bin`, compress with OpenZL using the selected profile.
Write dictionary to `{col}_dict_values.bin`, compress with OpenZL using the original profile.
Total compressed = index `.zl` size + dictionary `.zl` size.

### 6. Dictionary Encoding for Quantized Floats (`dict_float`)

Same as dict_int but applied to float columns with few unique values. Detect by checking `num_unique = len(np.unique(values))`. Only apply if `num_unique <= 65535`.

### 7. Delta + Container Narrowing (`delta_narrow`)

Compute deltas, then use the smallest integer container that fits all delta values. This is for sorted columns where we already apply delta but currently store deltas in the original (wide) container.

```python
def delta_narrow(values: np.ndarray):
    """Compute deltas and narrow to smallest fitting type.
    
    Returns: (narrowed_deltas, profile, container_width)
    """
    deltas = np.empty(len(values), dtype=np.int64)
    deltas[0] = int(values[0])
    deltas[1:] = np.diff(values.astype(np.int64))
    
    min_d = int(deltas[1:].min()) if len(deltas) > 1 else 0
    max_d = int(deltas[1:].max()) if len(deltas) > 1 else 0
    
    if -128 <= min_d and max_d <= 127:
        return deltas.astype(np.int8), "serial", 1
    elif -32768 <= min_d and max_d <= 32767:
        return deltas.astype(np.int16), "le-i16", 2
    elif -2147483648 <= min_d and max_d <= 2147483647:
        return deltas.astype(np.int32), "le-i32", 4
    else:
        return deltas, "le-i64", 8
```

## Test Matrix

Test the following columns and encodings. Each row represents one experiment.

### numeric_heavy dataset (3M rows)

| Column | Type | Current OpenZL | Parquet+zstd | Encodings to Test |
|--------|------|---------------|-------------|-------------------|
| temperature | FLOAT32 | 8,492,492 | 4,082,145 | byte_split, xor_delta, xor_delta_bytesplit |
| humidity | FLOAT32 | 8,167,195 | 4,004,175 | byte_split, xor_delta, xor_delta_bytesplit |
| pressure | FLOAT32 | 4,117,516 | 3,204,486 | byte_split, xor_delta, xor_delta_bytesplit |
| battery_voltage | FLOAT32 | 7,085,773 | 4,029,798 | byte_split, xor_delta, xor_delta_bytesplit |
| device_id | INT32 | 3,750,035 | 1,318,551 | dict_int |
| status_code | INT16 | 254,723 | 148,748 | dict_int |
| timestamp | INT64 | 3,522,613 | 2,432,879 | delta_narrow |

### mixed_type dataset (2M rows)

| Column | Type | Current OpenZL | Parquet+zstd | Encodings to Test |
|--------|------|---------------|-------------|-------------------|
| is_returned | BOOL | 72,370 | 54,687 | bitpack |
| customer_id | INT32 | 3,614,411 | 2,158,987 | dict_int |
| quantity | INT16 | 620,299 | 473,625 | dict_int |
| unit_price | FLOAT64 | 1,864,491 | 1,260,402 | byte_split, xor_delta, xor_delta_bytesplit, dict_float |
| total_amount | FLOAT64 | 3,615,484 | 2,238,556 | byte_split, xor_delta, xor_delta_bytesplit |
| order_date | TIMESTAMP | 11,434,375 | 7,670,888 | byte_split, xor_delta |

### ml_features dataset (500K rows) — representative columns

We already beat Parquet on ML features. Test encoding to see if we can widen the gap.

| Column | Type | Current OpenZL | Parquet+zstd | Encodings to Test |
|--------|------|---------------|-------------|-------------------|
| feature_01 | FLOAT32 (monotonic) | 705,403 | 2,125,860 | byte_split, xor_delta |
| feature_11 | FLOAT32 (random normal) | 1,296,288 | 2,119,928 | byte_split, xor_delta |
| feature_21 | FLOAT32 (sparse) | 142,420 | 229,407 | byte_split |
| feature_31 | FLOAT32 (quantized, ~20 unique) | 312,667 | 344,394 | dict_float, byte_split |

## Implementation: `encoding_experiment.py`

Create a single script at `parquet_experiments/encoding_experiment.py`.

### Script Structure

```python
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
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

WORKSPACE = Path(__file__).parent.parent
ZLI = str((WORKSPACE / "nyx" / "openzl" / "zli").resolve())
DATA_DIR = WORKSPACE / "parquet_experiments" / "data"
OUT_DIR = WORKSPACE / "parquet_experiments" / "encoding_experiment"
BASELINE_FILE = WORKSPACE / "parquet_experiments" / "per_column_experiment" / "per_column_results.json"


def compress_with_openzl(bin_path: Path, profile: str, zl_path: Path, timeout: int = 600) -> tuple[int, float]:
    """Compress a binary file with OpenZL. Returns (compressed_size, elapsed_secs)."""
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
        raise RuntimeError(f"zli compress failed (rc={result.returncode}):\n{result.stderr[:500]}")
    return zl_path.stat().st_size, elapsed


def get_parquet_column_sizes(parquet_path: Path) -> dict[str, int]:
    """Read Parquet file metadata, return {column_name: compressed_bytes}."""
    f = pq.ParquetFile(str(parquet_path))
    sizes = {}
    rg = f.metadata.row_group(0)
    for i in range(rg.num_columns):
        c = rg.column(i)
        sizes[c.path_in_schema] = c.total_compressed_size
    return sizes


# ... implement encoding functions as described above ...
# ... implement test matrix as described above ...
# ... for each (column, encoding):
#       1. Load column from Parquet
#       2. Apply encoding → write binary file(s)
#       3. Compress with OpenZL
#       4. Record result
#       5. Read baseline from per_column_results.json
#       6. Read Parquet+zstd from file metadata
```

### Key Implementation Details

1. **Loading columns**: Use `pq.read_table(path, columns=[col_name])` to read one column at a time. Extract as numpy via `.to_numpy()` or `.to_pylist()`.

2. **Extracting numeric values**:
   ```python
   table = pq.read_table(parquet_path, columns=[col_name])
   col = table.column(col_name)
   if pa.types.is_boolean(col.type):
       arr = col.to_numpy(zero_copy_only=False).astype(np.uint8)
   elif pa.types.is_timestamp(col.type):
       arr = col.cast(pa.int64()).to_numpy(zero_copy_only=False).astype(np.int64)
   elif pa.types.is_floating(col.type):
       arr = col.to_numpy(zero_copy_only=False)
       # Ensure correct dtype (float32 or float64 based on schema)
   else:
       arr = col.to_numpy(zero_copy_only=False)
   ```

3. **Handle nulls**: Fill nulls with 0 before encoding: `arr = np.nan_to_num(arr, nan=0.0)` for floats, `pc.fill_null(col, 0)` for integers.

4. **Byte-stream split compression**: Each stream gets its own `.bin` and `.zl` file. Use `serial` profile for all byte streams (they're uint8 arrays). Sum all `.zl` sizes for the total.

5. **Dictionary encoding compression**: Compress both the index array AND the dictionary binary. The dictionary is typically tiny, but include it in the total.

6. **Timeouts**: Use 600 seconds per compression call. Some columns are large (12 MiB) and training takes time.

7. **Progress logging**: Print progress as you go. Each encoding × column takes 30-300 seconds. The full experiment will take 1-3 hours.

### Output Format

Save to `encoding_experiment/encoding_results.json`:

```json
{
  "experiment": "encoding_before_openzl",
  "timestamp": "2026-02-13T...",
  "results": [
    {
      "dataset": "numeric_heavy",
      "column": "temperature",
      "arrow_type": "float32",
      "num_rows": 3000000,
      "raw_bytes": 12000000,
      "parquet_zstd_bytes": 4082145,
      "raw_openzl_bytes": 8492492,
      "raw_openzl_time_secs": 166.7,
      "encodings": {
        "byte_split": {
          "encoded_bytes": 12000000,
          "compressed_bytes": 5500000,
          "time_secs": 120.5,
          "stream_details": [
            {"stream": 0, "compressed": 2800000},
            {"stream": 1, "compressed": 1500000},
            {"stream": 2, "compressed": 800000},
            {"stream": 3, "compressed": 400000}
          ],
          "vs_raw_openzl_pct": 35.2,
          "vs_parquet_zstd_pct": -34.7
        },
        "xor_delta": {
          "encoded_bytes": 12000000,
          "compressed_bytes": 3200000,
          "time_secs": 95.0,
          "vs_raw_openzl_pct": 62.3,
          "vs_parquet_zstd_pct": 21.6
        },
        "xor_delta_bytesplit": {
          "encoded_bytes": 12000000,
          "compressed_bytes": 2100000,
          "time_secs": 150.0,
          "stream_details": [...],
          "vs_raw_openzl_pct": 75.3,
          "vs_parquet_zstd_pct": 48.5
        }
      }
    }
  ],
  "summary": {
    "total_columns_tested": 18,
    "encodings_that_beat_raw_openzl": 15,
    "encodings_that_beat_parquet_zstd": 8,
    "best_encoding_per_column": {
      "temperature": {"encoding": "xor_delta_bytesplit", "compressed": 2100000, "vs_parquet_zstd_pct": 48.5}
    }
  }
}
```

The `vs_raw_openzl_pct` and `vs_parquet_zstd_pct` fields use positive = better (savings).
Formula: `vs_X_pct = (1 - encoded_compressed / X_bytes) * 100`

### Summary Table

At the end of the script, print a human-readable table:

```
ENCODING EXPERIMENT RESULTS
============================

Dataset: numeric_heavy
Column           | Type    | Parquet+zstd | Raw OpenZL | Best Encoding       | Encoded+OpenZL | vs Raw  | vs Parquet
-----------------+---------+--------------+------------+---------------------+----------------+---------+----------
temperature      | FLOAT32 | 4,082,145    | 8,492,492  | xor_delta_bytesplit | 2,100,000      | +75.3%  | +48.5%
humidity         | FLOAT32 | 4,004,175    | 8,167,195  | xor_delta_bytesplit | 1,900,000      | +76.7%  | +52.5%
...
```

## Execution

1. `cd /Users/pavlosrousoglou/Desktop/Cornell/startup/openzl/compression`
2. `source nyx/.venv/bin/activate`
3. `python parquet_experiments/encoding_experiment.py 2>&1 | tee parquet_experiments/encoding_experiment/experiment.log`

The script should create the output directory if it doesn't exist.

## Critical Notes

- **Do NOT skip any column × encoding combination in the test matrix.** Even if you suspect it won't help, run it — we want empirical evidence.
- **Save results durably.** Write the JSON after each column completes (not just at the end), so partial results survive crashes.
- **Log everything.** Print the zli command being run, the encoding parameters, and intermediate sizes.
- **Use the zstd Parquet files** (`*_zstd.parquet`) as the source for reading column data and Parquet+zstd baselines.
- **For byte_split, compress ALL streams** — don't skip any. The total must include every stream.
- **For dict_float on unit_price**: first check the number of unique values. unit_price has `.99` rounding, so it may have manageable cardinality. If `num_unique > 65535`, skip dict_float for that column and log why.
- **All binary files** should go into subdirectories under `encoding_experiment/` named by `{dataset}/{column}/`.
- **Read the existing baselines** from `per_column_results.json` rather than re-running raw OpenZL (to save time). If a column is missing from baselines, run it fresh.
