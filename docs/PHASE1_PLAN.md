# Phase 1: Implementation Plan

**Date**: February 2026
**Status**: Ready to execute

---

## Guiding Principle

Build bottom-up. Each step produces something testable. Never move to the next step until the current step passes its verification. This prevents the scenario where we build the whole system and then spend days debugging which layer broke.

---

## Step 0: Understand What Already Exists

Before writing any code, be explicit about what we have and what's missing.

### What exists (working, tested)

| Component | Location | Status |
|---|---|---|
| Column encoder (4 encodings + auto-detect) | `parquet_experiments/column_encoder.py` | Working, 523 lines |
| OpenZL subprocess wrapper (compress, decompress, train) | `nyx/nyx/core/openzl.py` | Working, production |
| Tar-based .nyx archive (for genomic files) | `nyx/nyx/core/archive.py` | Working, production |
| CLI framework (click-based, subcommands) | `nyx/nyx/cli.py` | Working, production |
| File type detection | `nyx/nyx/core/detect.py` | Working, needs Parquet added |
| 4 benchmark datasets | `parquet_experiments/data/` | Generated, verified |
| Benchmark results (baseline numbers) | `parquet_experiments/results/` | Complete |

### What's missing (must build)

| Component | Purpose | Depends On |
|---|---|---|
| Column decoder | Reverse every encoding back to Arrow arrays | Nothing — pure logic |
| Null bitmap encode/decode | Handle null values through the pipeline | Nothing — pure logic |
| Column chunk serializer | Pack header + bitmap + .zl frames into bytes | Encoder, null bitmap |
| Column chunk deserializer | Unpack bytes back into components | Decoder, null bitmap |
| Parquet writer | Write a valid Parquet file with codec 100 | Serializer |
| Parquet reader | Read codec 100 files and return Arrow tables | Deserializer |
| CLI commands | `nyx compress`/`decompress` for Parquet | Writer, reader |
| Test suite | Automated round-trip + benchmark verification | Everything |

---

## Step 1: Column Decoder

**What**: Build `column_decoder.py` — the reverse of every encoding in `column_encoder.py`.

**Why first**: The decoder is pure logic. No file I/O, no OpenZL subprocess calls, no Parquet format knowledge. If decoding doesn't work, nothing downstream works. We need this before we can verify anything.

### Functions to implement

```
decode_raw(decompressed_bytes, dtype, width) → numpy array
    Reinterpret a flat byte buffer as a numpy array of the given dtype.
    Example: 8,000,000 bytes → 1,000,000 float64 values

decode_delta(decompressed_bytes, dtype, width, base_value) → numpy array
    Reinterpret as numpy array, then apply cumulative sum.
    deltas[0] = base_value, result[i] = sum(deltas[0:i+1])
    
decode_binary_convert(decompressed_bytes, binary_type, element_size) → list[str]
    Convert compact binary back to text.
    - uuid: 16 bytes → "550e8400-e29b-41d4-a716-446655440000"
    - ipv4: 4 bytes → "192.168.1.1"
    - ipv6: 16 bytes → "::1"
    - hex: N bytes → "a1b2c3..."

decode_dictionary(decompressed_indices_bytes, decompressed_dict_blobs, index_dtype, num_unique) → list[str]
    1. Reinterpret index bytes as numpy array (uint8, uint16, or int32)
    2. Deserialize dict blob (length-prefixed UTF-8 entries)
    3. Map each index to its dictionary entry
    
deserialize_dict(dict_bytes) → list[str]
    Reverse of _serialise_dict in column_encoder.py.
    Read 4-byte length prefix, then that many UTF-8 bytes, repeat.
```

### Verification (Step 1 test)

For each encoding type, do a round-trip WITHOUT compression (just encoding → decoding):

```python
# Test raw
arr = np.array([1.5, 2.7, 3.14, ...], dtype=np.float32)
encoded = arr.tobytes()
decoded = decode_raw(encoded, np.dtype("<f4"), 4)
assert np.array_equal(arr, decoded)

# Test delta
arr = np.array([100, 102, 105, 110, ...], dtype=np.int64)
deltas = np.empty_like(arr); deltas[0] = arr[0]; deltas[1:] = np.diff(arr)
encoded = deltas.tobytes()
decoded = decode_delta(encoded, np.dtype("<i8"), 8, base_value=100)
assert np.array_equal(arr, decoded)

# Test binary_convert (UUID)
uuids = ["550e8400-e29b-41d4-a716-446655440000", ...]
binary = b"".join(uuid.UUID(u).bytes for u in uuids)
decoded = decode_binary_convert(binary, "uuid", 16)
assert uuids == decoded

# Test dictionary
values = ["apple", "banana", "apple", "cherry", "banana"]
unique_sorted = sorted(set(values))
dict_map = {v: i for i, v in enumerate(unique_sorted)}
indices = np.array([dict_map[v] for v in values], dtype=np.uint8)
dict_blob = _serialise_dict(unique_sorted)  # from column_encoder
decoded = decode_dictionary(indices.tobytes(), dict_blob, np.uint8, len(unique_sorted))
assert values == decoded
```

**Pass criteria**: All 4 encoding types round-trip perfectly with at least 3 different data patterns each.

### File to create

`nyx/nyx/core/column_decoder.py`

---

## Step 2: Null Bitmap

**What**: Encode and decode null bitmaps so we can handle nullable columns.

### How Parquet and Arrow handle nulls

Arrow represents nulls as a validity bitmap — one bit per row. Bit = 1 means valid, bit = 0 means null. The bitmap is stored separately from the values.

Our encoding pipeline (column_encoder.py) currently fills nulls with sentinels (0 for numeric, "" for strings) before encoding. We need to:
1. Extract the bitmap before encoding
2. Store it alongside the compressed data
3. Apply it after decoding to restore the original null positions

### Functions to implement

```
extract_null_bitmap(col: pa.Array) → tuple[bytes, int]
    Returns (bitmap_bytes, null_count).
    bitmap_bytes = ceil(len(col) / 8) bytes.
    If null_count == 0, returns (b"", 0) — no bitmap stored.

apply_null_bitmap(values: np.array | list, bitmap_bytes: bytes, null_count: int, num_rows: int) → pa.Array
    Creates an Arrow array from the decoded values with nulls at the correct positions.
    If null_count == 0, returns array without nulls.
```

### Verification (Step 2 test)

```python
# Create array with known null positions
arr = pa.array([1, None, 3, None, 5, 6, None, 8], type=pa.int32())
bitmap, null_count = extract_null_bitmap(arr)
assert null_count == 3

# Fill nulls, then restore
filled = pc.fill_null(arr, 0)
values = filled.to_numpy()
restored = apply_null_bitmap(values, bitmap, null_count, len(arr))
assert arr.equals(restored)

# Repeat for strings
str_arr = pa.array(["hello", None, "world", None], type=pa.string())
bitmap, null_count = extract_null_bitmap(str_arr)
# ... fill, encode, decode, apply bitmap, compare
```

**Pass criteria**: Null positions survive round-trip for int32, int64, float64, string, and boolean arrays, including edge cases (all nulls, no nulls, first/last null).

### File to create

`nyx/nyx/core/null_bitmap.py`

---

## Step 3: Encode → Compress → Decompress → Decode Round-Trip

**What**: Wire the encoder, compressor, decompressor, and decoder together and verify the full pipeline end-to-end, WITHOUT any container format. This tests the data pipeline in isolation.

### What we're testing

```
Arrow array
  → extract null bitmap
  → fill nulls with sentinel
  → column_encoder.encode_and_compress()
  → produces .zl file(s) on disk
  → zli decompress each .zl file
  → column_decoder.decode_*()
  → apply null bitmap
  → Arrow array
  → compare with original
```

### Implementation

Create a test script `nyx/tests/test_column_roundtrip.py`:

```
For each column type:
  1. Create a realistic Arrow array (from our benchmark datasets or synthetic)
  2. Run column_encoder.encode_and_compress() → get EncodingResult + .zl files
  3. Run zli decompress on each .zl file → get .bin files
  4. Call column_decoder to reverse the encoding → get values
  5. Apply null bitmap → get Arrow array
  6. Compare with original array
```

### Column types to test

| # | Type | Encoding | Example Data |
|---|---|---|---|
| 1 | INT64, sorted | delta | [1, 2, 3, ..., 10000] |
| 2 | INT64, unsorted | raw | Random int64 values |
| 3 | INT32 | raw | Random int32 values |
| 4 | FLOAT32 | raw | Random float32 values |
| 5 | FLOAT64 | raw | Random float64 values |
| 6 | BOOL | raw | Random booleans |
| 7 | TIMESTAMP | raw | Timestamps with jitter |
| 8 | STRING, UUID | binary_convert | Random UUIDs |
| 9 | STRING, IPv4 | binary_convert | Random IPs |
| 10 | STRING, hex hash | binary_convert | Random SHA256 |
| 11 | STRING, low-cardinality | dictionary | 5 unique status values |
| 12 | STRING, high-cardinality | dictionary | 1000 unique emails |
| 13 | INT64 with nulls | raw | Random int64, 10% null |
| 14 | STRING with nulls | dictionary | Emails, 10% null |

### Pass criteria

For all 14 cases:
- `original_array.equals(restored_array)` returns True
- Null positions match exactly
- Data types match exactly
- Compression ratio is > 1.0 (we're actually compressing, not expanding)

### Files to create/modify

- `nyx/tests/test_column_roundtrip.py` — the test script
- `nyx/nyx/core/column_encoder.py` — copy from `parquet_experiments/column_encoder.py`, adapt imports
- May need minor adjustments to decoder based on what we discover during testing

---

## Step 4: Column Chunk Binary Serialization

**What**: Define a binary format for a single column chunk — the unit that gets stored in the output file. This format packs the encoding header, null bitmap, and .zl frame(s) into a single byte sequence.

### Why a binary format

We need to go from "a bunch of files on disk" (index.zl, dict_chunk_000.zl, dict_chunk_001.zl, ...) to "a single contiguous blob of bytes" that we can write into a Parquet column chunk or any container.

### Format specification

```
Column chunk blob:
  [4 bytes, LE] header_length
  [header_length bytes] header_json (UTF-8 encoded JSON)
  [4 bytes, LE] null_count
  [null_count > 0 ? ceil(num_rows/8) bytes : 0] validity_bitmap
  [4 bytes, LE] primary_zl_frame_length
  [primary_zl_frame_length bytes] primary .zl frame data
  [if encoding == "dictionary":
    [4 bytes, LE] num_dict_chunks
    [for each dict chunk:
      [4 bytes, LE] chunk_zl_length
      [chunk_zl_length bytes] dict chunk .zl frame data
    ]
  ]
```

The header JSON contains:

```json
{
  "encoding": "dictionary",
  "profile": "le-i32",
  "num_rows": 1000000,
  "arrow_type": "string",
  "metadata": {
    "num_unique": 632262,
    "index_dtype": "int32",
    "dict_raw_bytes": 17697021,
    "dict_chunks": 18
  }
}
```

### Functions to implement

```
serialize_column_chunk(
    encoding_result: EncodingResult,
    null_bitmap: bytes,
    null_count: int,
    num_rows: int,
    arrow_type: str,
) → bytes
    Read .zl file(s) from disk, pack into the binary format above.

deserialize_column_chunk(blob: bytes) → ColumnChunkData
    Parse the binary format, return a dataclass with:
      - header (dict)
      - null_bitmap (bytes)
      - primary_zl_data (bytes)
      - dict_chunk_zl_data (list[bytes], empty if not dictionary)
```

### Verification (Step 4 test)

```python
# Encode and compress a column
result = encoder.encode_and_compress(col, "email", col.type, "auto", tmpdir)
bitmap, null_count = extract_null_bitmap(col)

# Serialize
blob = serialize_column_chunk(result, bitmap, null_count, len(col), str(col.type))

# Deserialize
chunk = deserialize_column_chunk(blob)

# Verify all fields survived
assert chunk.header["encoding"] == "dictionary"
assert chunk.header["num_rows"] == len(col)
assert len(chunk.dict_chunk_zl_data) == result.metadata["dict_chunks"]
```

Then, combine with Step 3 to do: Arrow → encode → serialize → deserialize → decode → Arrow.

**Pass criteria**: Serialize → deserialize round-trip preserves all fields. Combined with Step 3, the full pipeline (Arrow → blob → Arrow) produces identical data.

### File to create

`nyx/nyx/core/chunk_format.py`

---

## Step 5: Parquet Writer

**What**: Write a valid Parquet file where each column chunk contains our serialized column chunk blob, and the codec ID is set to 100.

### Approach: Use `parquet-format` Thrift definitions via `thriftpy2`

The Parquet file format is defined in a `.thrift` file. We use `thriftpy2` to load the Thrift definitions and generate Python classes, then construct the file binary from these classes.

The Parquet Thrift IDL is publicly available at: https://github.com/apache/parquet-format/blob/master/src/main/thrift/parquet.thrift

We only need to implement a subset of the Parquet spec for Phase 1:
- Single row group per file (we can extend to multiple later)
- Single data page per column chunk
- PLAIN encoding (our encoding is done pre-compression)
- Our custom codec ID (100)
- Statistics (copied from input file)
- Key-value metadata (our encoding info + original metadata)

### Alternative if Thrift is too complex

If `thriftpy2` + raw Thrift proves brittle, there is a fallback: use `fastparquet`, which is a pure-Python Parquet implementation. We can write a file with `fastparquet` using no compression, then binary-patch the footer to change the codec ID and replace column chunk data. This is less clean but gets us unblocked.

### What the writer does

```python
def write_openzl_parquet(
    output_path: Path,
    schema: pa.Schema,
    column_blobs: dict[str, bytes],     # column_name → serialized chunk blob
    column_metadata: dict[str, dict],   # column_name → encoding metadata
    original_statistics: dict[str, Statistics],
    original_metadata: bytes,           # base64-encoded original footer
    num_rows: int,
):
    with open(output_path, "wb") as f:
        # 1. Write magic "PAR1"
        f.write(b"PAR1")
        
        # 2. For each column, write: PageHeader (Thrift) + column blob
        column_offsets = {}
        for col_name in schema.names:
            offset = f.tell()
            page_header = build_page_header(...)
            f.write(serialize_thrift(page_header))
            f.write(column_blobs[col_name])
            column_offsets[col_name] = (offset, f.tell() - offset)
        
        # 3. Build footer metadata
        file_metadata = build_file_metadata(
            schema, column_offsets, column_metadata,
            original_statistics, num_rows, original_metadata,
        )
        
        # 4. Write footer
        footer_bytes = serialize_thrift(file_metadata)
        footer_offset = f.tell()
        f.write(footer_bytes)
        f.write(struct.pack("<I", len(footer_bytes)))
        f.write(b"PAR1")
```

### Verification (Step 5 test)

1. Write a file → check it starts with `PAR1` and ends with `PAR1`
2. Read the footer with `pyarrow.parquet.read_metadata()` → should parse successfully and show our schema, statistics, and key-value metadata. The column data will be unreadable (codec 100) but the metadata should be valid.
3. Verify file size = magic(4) + sum(column_chunks) + footer + footer_len(4) + magic(4)

**Pass criteria**: PyArrow can read the metadata (schema, row count, column names, statistics) from our output file without errors.

### File to create

`nyx/nyx/core/parquet_writer.py`

### Dependencies to add

`thriftpy2` (or vendor the generated Parquet Thrift classes)

---

## Step 6: Parquet Reader

**What**: Read a Parquet file with codec ID 100, extract the column chunk blobs, and return an Arrow table.

### What the reader does

```python
def read_openzl_parquet(
    input_path: Path,
    columns: list[str] | None = None,  # None = all columns
) → pa.Table:
    # 1. Read the file footer
    footer = read_parquet_footer(input_path)
    
    # 2. Extract openzl:columns metadata
    encoding_info = json.loads(footer.key_value_metadata["openzl:columns"])
    
    # 3. For each requested column:
    arrays = {}
    for col_name in (columns or footer.schema.names):
        # a. Seek to column chunk offset
        offset, length = get_column_location(footer, col_name)
        
        # b. Read column chunk bytes (skip page header)
        raw = read_bytes(input_path, offset, length)
        page_header, data_start = parse_page_header(raw)
        blob = raw[data_start:]
        
        # c. Deserialize column chunk (from Step 4)
        chunk = deserialize_column_chunk(blob)
        
        # d. Decompress .zl frame(s) (call zli decompress)
        decompressed_primary = zli_decompress(chunk.primary_zl_data)
        decompressed_dicts = [zli_decompress(d) for d in chunk.dict_chunk_zl_data]
        
        # e. Decode (from Step 1)
        values = decode(chunk.header, decompressed_primary, decompressed_dicts)
        
        # f. Apply null bitmap (from Step 2)
        arrow_array = apply_null_bitmap(values, chunk.null_bitmap, ...)
        
        arrays[col_name] = arrow_array
    
    # 4. Build Arrow table
    return pa.table(arrays, schema=reconstruct_schema(footer, columns))
```

### Verification (Step 6 test)

1. Take a known Parquet file from our benchmark datasets
2. Compress it with the writer (Step 5)
3. Read it back with the reader (Step 6)
4. Compare with the original Arrow table

```python
original = pq.read_table("data/numeric_heavy_none.parquet")
# compress (writer)
compress_parquet("data/numeric_heavy_none.parquet", "output.parquet")
# decompress (reader)
restored = read_openzl_parquet("output.parquet")
assert original.equals(restored)
```

**Pass criteria**: Round-trip works on at least one dataset with only numeric columns (simplest case).

### File to create

`nyx/nyx/core/parquet_reader.py`

---

## Step 7: CLI Integration

**What**: Wire the writer and reader into the Nyx CLI so users can run `nyx compress` and `nyx decompress` on Parquet files.

### Commands

```bash
# Compress a Parquet file
nyx compress input.parquet -o output.parquet

# Decompress back to standard Parquet
nyx decompress output.parquet -o restored.parquet

# Decompress specific columns only
nyx decompress output.parquet -o restored.parquet --columns timestamp,user_id

# Show file info
nyx inspect output.parquet
```

### Implementation

Modify `nyx/nyx/core/detect.py` to detect Parquet files (check for `PAR1` magic bytes at start of file). Modify `nyx/nyx/commands/compress.py` to route Parquet files to the new pipeline instead of the genomic pipeline. Create or extend `nyx/nyx/commands/decompress.py` to handle Parquet.

### Verification (Step 7 test)

Run the commands manually on each benchmark dataset and confirm the output matches. This is an end-to-end smoke test, not the thorough test suite.

```bash
nyx compress parquet_experiments/data/numeric_heavy_none.parquet -o /tmp/nh.parquet -f
nyx decompress /tmp/nh.parquet -o /tmp/nh_restored.parquet -f
python -c "
import pyarrow.parquet as pq
orig = pq.read_table('parquet_experiments/data/numeric_heavy_none.parquet')
rest = pq.read_table('/tmp/nh_restored.parquet')
print('Match:', orig.equals(rest))
"
```

**Pass criteria**: CLI commands work on all 4 benchmark datasets. No crashes, no hangs, correct output.

### Files to create/modify

- `nyx/nyx/commands/compress.py` — MODIFY (add Parquet routing)
- `nyx/nyx/commands/decompress.py` — MODIFY (add Parquet handling)
- `nyx/nyx/core/detect.py` — MODIFY (add Parquet detection)
- `nyx/nyx/core/config.py` — MODIFY (add OPENZL_CODEC_ID = 100)

---

## Step 8: Automated Test Suite

**What**: Build a comprehensive test suite that verifies correctness across all data types, null patterns, and edge cases. This is what gives us confidence to ship.

### Test categories

**Category A: Column-level round-trip (14 tests from Step 3)**

Each test: create Arrow array → encode → compress → decompress → decode → compare.

| Test | Type | Encoding | Nulls |
|---|---|---|---|
| A1 | INT64, sorted | delta | no |
| A2 | INT64, unsorted | raw | no |
| A3 | INT32 | raw | no |
| A4 | FLOAT32 | raw | no |
| A5 | FLOAT64 | raw | no |
| A6 | BOOL | raw | no |
| A7 | TIMESTAMP | raw | no |
| A8 | STRING, UUID | binary_convert | no |
| A9 | STRING, IPv4 | binary_convert | no |
| A10 | STRING, hex | binary_convert | no |
| A11 | STRING, low-card | dictionary | no |
| A12 | STRING, high-card | dictionary | no |
| A13 | INT64 | raw | 10% null |
| A14 | STRING | dictionary | 10% null |

**Category B: Full-file round-trip (4 tests)**

Each test: read Parquet file → compress → decompress → compare Arrow tables.

| Test | Dataset | Columns | Types |
|---|---|---|---|
| B1 | numeric_heavy | 7 | all numeric |
| B2 | ml_features | 51 | all numeric |
| B3 | mixed_type | 9 | numeric + string |
| B4 | highcard_strings | 8 | numeric + many string types |

**Category C: Column pruning (3 tests)**

Decompress a subset of columns and verify only those columns are returned, with correct values.

| Test | Dataset | Requested Columns | Verify |
|---|---|---|---|
| C1 | numeric_heavy | [timestamp, status_code] | 2 of 7 cols, values match |
| C2 | mixed_type | [order_id, product_name] | 1 numeric + 1 string |
| C3 | highcard_strings | [uuid_v4, email, status] | 3 string types |

**Category D: Edge cases (5 tests)**

| Test | Scenario |
|---|---|
| D1 | Column with ALL nulls |
| D2 | Column with NO nulls |
| D3 | Column with single row |
| D4 | Empty column (0 rows) |
| D5 | Column with very long strings (>1000 chars) |

**Category E: Metadata preservation (3 tests)**

| Test | Verify |
|---|---|
| E1 | Schema (column names, types) matches original |
| E2 | Statistics (min, max, null_count) match original |
| E3 | Row count matches original |

### Pass criteria

All tests pass. Zero failures. Any failure blocks the step.

### File to create

`nyx/tests/test_parquet_roundtrip.py`

---

## Step 9: Benchmark Verification

**What**: Run the full compression pipeline on all datasets and verify that compression ratios match (or are very close to) our experimental results. If they don't, investigate why.

### What we're comparing

| Dataset | Expected Ratio vs Parquet+zstd | Source |
|---|---|---|
| numeric_heavy | ~33% better | per_column_results.json |
| ml_features | ~45% better | per_column_results.json |
| mixed_type | ~20% better | per_column_results.json |
| highcard_strings | ~24% better | encoding_benchmark.json |

### How to run

```bash
python nyx/tests/benchmark_parquet.py
```

This script:
1. Takes each uncompressed Parquet file from `parquet_experiments/data/`
2. Compresses it with `nyx compress`
3. Compresses the original with `pyarrow` + zstd(1) and zstd(19) as baselines
4. Records sizes and calculates ratios
5. Prints a comparison table
6. Flags any column where the ratio is >5% worse than our experimental result (tolerance for overhead from headers, bitmap, etc.)

### Expected variance

Our Phase 1 output will be slightly larger than the raw per-column .zl files because of:
- Column chunk headers (~100-200 bytes per column, JSON)
- Null bitmaps (~0 for columns without nulls, ~125 KB for 1M rows)
- Parquet page headers and footer (~few KB)

This overhead should be <0.1% of total file size for any realistic dataset. If we see more than 1% variance from our experimental results, something is wrong.

### Pass criteria

For every dataset, the ratio vs Parquet+zstd is within 2% of our experimental result. If it's worse by more than 2%, we investigate before proceeding.

### File to create

`nyx/tests/benchmark_parquet.py`

---

## Step 10: Inspect Command

**What**: Add a `nyx inspect` command that displays useful information about an OpenZL-compressed Parquet file.

### Output

```
$ nyx inspect output.parquet

File: output.parquet (23.2 MiB)
Format: Parquet + OpenZL (codec 100)
Rows: 3,000,000
Row Groups: 1

Columns (7):
  timestamp      INT64    encoding=raw      profile=le-i64   3.36 MiB (ratio 6.8x)
  device_id      INT32    encoding=raw      profile=le-i32   3.58 MiB (ratio 3.2x)
  temperature    FLOAT32  encoding=raw      profile=le-i32   8.10 MiB (ratio 1.4x)
  humidity       FLOAT32  encoding=raw      profile=le-i32   7.79 MiB (ratio 1.5x)
  pressure       FLOAT32  encoding=raw      profile=le-i32   3.93 MiB (ratio 2.9x)
  battery_volt   FLOAT32  encoding=raw      profile=le-i32   6.76 MiB (ratio 1.7x)
  status_code    INT16    encoding=raw      profile=le-i16   0.24 MiB (ratio 23.6x)

Total compressed: 33.75 MiB
vs Parquet+zstd:  50.58 MiB (-33.3%)
```

### Verification

Run on a compressed file and confirm the output matches expected values.

### Files to modify

`nyx/nyx/commands/inspect_cmd.py`

---

## Execution Order Summary

```
Step 0: Audit existing code                    ← 0.5 day
Step 1: Column decoder                         ← 1 day
Step 2: Null bitmap                            ← 0.5 day
Step 3: Encode→compress→decompress→decode test ← 1 day
   [CHECKPOINT: data pipeline verified, no container format yet]
Step 4: Column chunk serialization             ← 1 day
Step 5: Parquet writer                         ← 2-3 days
Step 6: Parquet reader                         ← 1-2 days
   [CHECKPOINT: end-to-end file round-trip works]
Step 7: CLI integration                        ← 1 day
Step 8: Automated test suite                   ← 1-2 days
Step 9: Benchmark verification                 ← 1 day
Step 10: Inspect command                       ← 0.5 day
   [CHECKPOINT: Phase 1 complete]
```

Total: ~10-13 days of focused engineering.

---

## Risk Log (Phase 1 specific)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Thrift-based Parquet writer is harder than expected | Medium | 2-3 day delay | Fallback: use `fastparquet` or binary-patch approach |
| PyArrow can't read metadata from our custom-codec file | Low | 1 day to debug | Test early (Step 5 verification). PyArrow should handle unknown codecs in metadata-only mode. |
| `zli decompress` requires file I/O (no stdin/stdout) | Medium | Workaround needed | Write to temp files, decompress, read back. Ugly but functional for Phase 1. |
| Float NaN/Inf handling breaks round-trip | Low | 1 day to fix | Test explicitly with NaN/Inf values in Step 3. |
| Dictionary serialization format mismatch | Low | 0.5 day | Use the exact same `_serialise_dict` format in both encoder and decoder. |
| Large dictionary blobs exceed memory | Low | 1 day | Already chunked at 1 MiB. Test with the highcard_strings dataset (17 MiB dict blob). |

---

## What Phase 1 Does NOT Include

These are explicitly deferred to later phases:

- No DuckDB extension (Phase 2)
- No Arrow C++ codec plugin (Phase 2)
- No model reuse across files (Phase 2)
- No parallel column compression (Phase 2)
- No paged compression / random access (Phase 3)
- No cloud storage integration (Phase 4)
- No C API integration — we use `zli` as subprocess throughout Phase 1

Phase 1 is about correctness and proving the architecture. Speed and ecosystem integration come in Phase 2.
