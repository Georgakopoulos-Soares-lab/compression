# OpenZL Parquet: Architecture Roadmap

**Date**: February 2026
**Author**: Head Architect
**Status**: Approved for implementation

---

## Goal

Build `nyx`, a production-grade tool that compresses Apache Parquet files 20-60% better than Parquet+zstd while preserving — and improving upon — every benefit Parquet provides today: schema, column pruning, predicate pushdown, statistics, ecosystem compatibility, and lossless round-trip reconstruction.

The tool will ship as a single compiled executable with OpenZL embedded. No external dependencies at runtime.

---

## What We Have Proven

Before describing what we're building, here is the empirical foundation this roadmap is built on. Every number is from an experiment we ran and can reproduce.

| Data Type | Encoding Strategy | vs Parquet+zstd |
|---|---|---|
| Sorted/sequential integers | Delta + LE profile | ~100% smaller |
| Timestamps | LE-i64 (raw or delta) | 30-33% smaller |
| Floating point | LE-i32/i64 | 20-45% smaller |
| Integer IDs | LE-i32/i64 | 20-30% smaller |
| UUIDs (v4 random) | Binary convert + LE-i64 | 21% smaller |
| UUIDs (v7 time-ordered) | Binary convert + LE-i64 | 35% smaller |
| IPv4 addresses | Binary convert + LE-i32 | 41% smaller |
| SHA256 hashes | Binary convert + LE-i64 | 6% smaller |
| Low-cardinality strings | Dictionary + OpenZL | 26% smaller |
| High-cardinality strings | Dictionary + OpenZL (chunked serial) | 37-58% smaller |

Per-column independent compression validated at **2-6% better** than whole-file compression. OpenZL decompression speed measured at **~1,000 MB/s**.

Code implementing all encoding strategies: `parquet_experiments/column_encoder.py` (523 lines, fully tested).

---

## Evaluation of Query Architecture Options

The other agent's architecture document (`PARQUET_QUERY_ARCHITECTURE.md`) lays out two approaches. Here is my assessment.

### Option A: One `.zl` frame per column chunk

**Verdict: Build this first. It is the correct Phase 1.**

Reasons:
1. It is the architecture all our benchmarks were measured against. Every compression ratio number above was collected at column-chunk granularity. No extrapolation needed.
2. It is a direct codec substitution. The Parquet container format is unchanged. Row groups, column chunk offsets, statistics, schema — all remain intact. The only delta from a standard Parquet file is the codec ID.
3. Column pruning and row-group-level predicate pushdown work identically to standard Parquet. A query that reads 3 of 50 columns decompresses only those 3 column chunks.
4. For the analytical workloads Parquet is designed for (full scans, aggregations, GROUP BY, joins), decompressing a full column chunk is the normal cost. Zstd does the same thing today. We do it smaller.
5. Standard Parquet readers fail gracefully ("unknown codec") and our reader plugin handles it. No data corruption, no silent errors.

The one limitation — no sub-chunk random access — is acceptable for Phase 1. Parquet with zstd has the same limitation today, and the entire industry runs on it.

### Option B: Multiple `.zl` frames per column chunk (paged)

**Verdict: Build this as Phase 3. It is a competitive advantage, not a launch requirement.**

The mitigation strategy in the doc is sound: train on the full column, compress each page with the trained model. This preserves near-optimal compression ratios while enabling per-page random access. OpenZL's decoupled train/compress architecture makes this natural.

But it adds significant complexity: page index management, custom reader logic, page-size tuning, page-level statistics. None of this is needed for the initial product. We should design the metadata format to accommodate it from day one so that an Option A file is just an Option B file with exactly one page per column chunk.

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                          nyx CLI                                  │
│                                                                    │
│  nyx compress input.parquet -o output.parquet                      │
│  nyx decompress output.parquet -o restored.parquet                 │
│  nyx inspect output.parquet                                        │
│  nyx query output.parquet --sql "SELECT ..."                       │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│                       Parquet Engine                               │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐        │
│  │ Parquet       │  │ Parquet       │  │ Column Encoder    │        │
│  │ Reader        │  │ Writer        │  │ (auto-detect,     │        │
│  │ (pyarrow)     │  │ (low-level)   │  │  encode, compress)│        │
│  └──────────────┘  └──────────────┘  └───────────────────┘        │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│                     OpenZL Core                                    │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐             │
│  │ zli compress  │  │ zli decompress│  │ zli train     │             │
│  │ (per-column)  │  │ (per-column)  │  │ (reusable     │             │
│  │              │  │              │  │  models)       │             │
│  └──────────────┘  └──────────────┘  └──────────────┘             │
│                                                                    │
├──────────────────────────────────────────────────────────────────┤
│                   Ecosystem Plugins                                │
│                                                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │ DuckDB    │  │ Arrow C++ │  │ Spark     │  │ Python   │          │
│  │ Extension │  │ Codec     │  │ Plugin    │  │ SDK      │          │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘          │
└──────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Writer + Reader + Round-Trip (Option A)

**Goal**: `nyx compress input.parquet` → compressed Parquet file. `nyx decompress compressed.parquet` → byte-identical original. Column pruning works. Statistics preserved.

**Timeline estimate**: 4-6 weeks of engineering.

### 1.1 Output File Format

The output IS a valid Parquet file. It uses the standard Parquet binary layout (magic bytes, row groups, footer, Thrift metadata). The only difference: the codec ID on each column chunk is set to a custom value.

```
Standard Parquet:
  Column chunk codec = ZSTD (6)

Our Parquet:
  Column chunk codec = OPENZL (100)   ← custom, outside the spec's enum
```

Why a real Parquet file and not a custom container:
- Standard tools can still read the schema, row counts, column names, and statistics. They just can't decompress the column data without our plugin.
- The file can be listed by AWS Glue, registered in Hive metastore, described by `parquet-tools`, and cataloged normally.
- Future ecosystem plugins (DuckDB, Arrow, Spark) just register codec 100 and everything works — they don't need to understand a new format.

#### Encoding Metadata

Each column chunk needs metadata describing how it was encoded (raw LE, delta, binary_convert, dictionary). Standard Parquet has no field for this. We store it in two places:

1. **Parquet key-value metadata** (footer-level): a JSON blob under the key `openzl:columns` that maps each column name to its encoding metadata:
   ```json
   {
     "timestamp": {"encoding": "raw", "profile": "le-i64", "width": 8},
     "user_id": {"encoding": "delta", "profile": "le-i64", "base_value": 1},
     "email": {
       "encoding": "dictionary",
       "profile": "le-i32",
       "num_unique": 632262,
       "index_dtype": "int32",
       "dict_raw_bytes": 17697021,
       "dict_chunks": 18,
       "null_count": 0
     },
     "uuid": {"encoding": "binary_convert", "profile": "le-i64", "binary_type": "uuid"}
   }
   ```

2. **Within the compressed frame itself**: a small header prepended to the `.zl` data in each column chunk. This makes each column chunk self-describing — you don't need the footer to decode a single column chunk.

#### Null Handling

Nulls are stored as a separate validity bitmap per column chunk, prepended to the compressed data:

```
Column chunk bytes:
  [4 bytes: header length]
  [header: encoding metadata JSON]
  [null_count > 0? validity bitmap (ceil(num_rows / 8) bytes)]
  [.zl frame: compressed column data (nulls filled with sentinel)]
  [dictionary encoding? additional .zl frames for dict blob chunks]
```

For dictionary-encoded columns with multiple .zl frames (index + dictionary chunks), the layout is:

```
Column chunk bytes:
  [header]
  [validity bitmap]
  [.zl frame: compressed indices]
  [4 bytes: number of dict chunks]
  [.zl frame: dict chunk 0]
  [.zl frame: dict chunk 1]
  ...
  [.zl frame: dict chunk N]
```

Each `.zl` frame is self-delimiting (OpenZL stores its frame length internally), so the reader can parse them sequentially without a separate index.

#### Statistics Preservation

Column statistics (min, max, null_count, distinct_count) are preserved from the original Parquet file and stored in the footer metadata. For numeric columns where we apply delta encoding, the statistics remain valid because they describe the original values, not the encoded form.

For string columns with binary_convert or dictionary encoding, we copy the original statistics. The reader applies the reverse transformation after decompression, so min/max comparisons for predicate pushdown operate on the original string values.

### 1.2 The Writer: `nyx compress`

```
nyx compress input.parquet [-o output.parquet] [--force] [--verbose]
```

Pipeline:

```
Step 1: Read input Parquet file
        → pyarrow.parquet.read_metadata() for schema, row groups, statistics
        → pyarrow.parquet.ParquetFile() for column data access

Step 2: For each row group:
          For each column:
            a. Read the column chunk as an Arrow array
            b. Auto-detect encoding (column_encoder._auto_detect)
            c. Extract null bitmap
            d. Fill nulls with sentinel (0 for numeric, "" for string)
            e. Encode (delta, binary_convert, dictionary, or raw)
            f. Compress with OpenZL (zli compress --train-inline)
            g. Build column chunk bytes (header + bitmap + .zl frames)

Step 3: Write output Parquet file
        → Custom Parquet writer (see Section 1.4)
        → Original schema, row group structure, statistics preserved
        → Codec ID = 100 (OPENZL)
        → Key-value metadata includes openzl:columns and openzl:version

Step 4: Verify round-trip (optional, enabled by --verify flag)
        → Decompress, compare against original
```

**Parallelism**: Columns within a row group are independent. On an N-core machine, compress up to N columns simultaneously using a thread pool. Each `zli compress` invocation internally uses multiple threads for training, so limit parallelism to `cores / 2` to avoid oversubscription.

### 1.3 The Reader: `nyx decompress`

```
nyx decompress compressed.parquet [-o restored.parquet] [--columns col1,col2]
```

Pipeline:

```
Step 1: Read footer of compressed Parquet file
        → Parse openzl:columns metadata for encoding info per column
        → If --columns specified, only process those columns (column pruning)

Step 2: For each row group:
          For each column (or requested columns):
            a. Seek to column chunk offset (from footer metadata)
            b. Read column chunk bytes
            c. Parse header (encoding metadata)
            d. Read validity bitmap
            e. Decompress .zl frame(s) with zli decompress
            f. Reverse encoding:
               - raw: reinterpret as numpy array
               - delta: cumulative sum to restore original values
               - binary_convert: binary → text (UUID, IPv4, hex)
               - dictionary: index lookup + deserialize dict blob
            g. Apply null bitmap to create Arrow array with nulls

Step 3: Write output as standard Parquet file
        → pyarrow.parquet.write_table() with compression='zstd' (or 'none')
        → Schema and statistics from the compressed file's footer
```

The restored file must be byte-for-byte identical to the original at the logical level (same Arrow table). If the user wants the exact same Parquet binary, we store the original Parquet writer metadata (row group sizes, page sizes, encoding) in key-value metadata and replay them.

### 1.4 Custom Parquet Writer

PyArrow's `write_table()` does not support custom codec IDs. We need a low-level Parquet writer that constructs the binary format directly.

**Two approaches**:

**A. Python Thrift writer** (recommended for Phase 1): Use the Parquet format's Thrift definitions (already available as Python classes from the `parquet-format` package or generated from the `.thrift` files). Construct the file metadata, column chunk metadata, and page headers in Python, then concatenate them with the OpenZL-compressed data bytes.

```
┌─────────────┐
│ PAR1 magic   │ 4 bytes
├─────────────┤
│ Row Group 0  │
│  Col chunk 0 │ → [page_header (Thrift)] + [compressed data]
│  Col chunk 1 │ → [page_header (Thrift)] + [compressed data]
│  ...         │
├─────────────┤
│ Row Group 1  │
│  ...         │
├─────────────┤
│ Footer       │ → FileMetaData (Thrift, serialized)
│ Footer len   │ 4 bytes (little-endian)
│ PAR1 magic   │ 4 bytes
└─────────────┘
```

Each column chunk contains a single data page with:
- `PageHeader`: type=DATA_PAGE, uncompressed_page_size, compressed_page_size, data_page_header (num_values, encoding=PLAIN)
- Page data: our OpenZL-compressed bytes

The `ColumnMetaData` in the footer specifies:
- `codec = 100` (OPENZL)
- `total_compressed_size`, `total_uncompressed_size`
- `statistics` (min, max, null_count)

**B. C++ writer using Arrow/Parquet library** (Phase 2): Register OpenZL as a custom codec in Arrow's C++ codec registry. Then use Arrow's standard `ParquetFileWriter` which handles all the Thrift serialization, page management, and statistics automatically. This requires a C++ shared library (`.so` / `.dylib`) that Arrow loads at runtime.

For Phase 1, approach A gives us full control and zero C++ dependencies beyond `zli`. We'll build approach B in Phase 2 for ecosystem integration.

### 1.5 Files to Create / Modify

| File | Action | Purpose |
|---|---|---|
| `nyx/nyx/commands/parquet.py` | **CREATE** | `nyx compress --parquet` and `nyx decompress --parquet` commands |
| `nyx/nyx/core/parquet_writer.py` | **CREATE** | Low-level Parquet file writer (Thrift + binary assembly) |
| `nyx/nyx/core/parquet_reader.py` | **CREATE** | OpenZL-aware Parquet file reader |
| `nyx/nyx/core/column_encoder.py` | **CREATE** | Move `parquet_experiments/column_encoder.py` into the package, adapt for production |
| `nyx/nyx/core/column_decoder.py` | **CREATE** | Reverse all encodings (delta cumsum, dict lookup, binary→text) |
| `nyx/nyx/core/null_bitmap.py` | **CREATE** | Null bitmap encode/decode utilities |
| `nyx/nyx/core/detect.py` | **MODIFY** | Add Parquet detection (check for PAR1 magic bytes) |
| `nyx/nyx/core/config.py` | **MODIFY** | Add Parquet-specific configuration (codec ID, chunk sizes) |
| `nyx/nyx/commands/compress.py` | **MODIFY** | Route Parquet files to the new pipeline |
| `nyx/pyproject.toml` | **MODIFY** | Add `pyarrow` dependency |

### 1.6 OpenZL Internals: What Needs to Change

For Phase 1, we use `zli` as a subprocess (same as today). No changes to OpenZL internals required.

However, we should note two performance issues that will drive Phase 2 changes:

1. **Subprocess overhead**: Each column invokes `zli compress` as a separate process. For a table with 50 columns and 10 row groups, that's 500 process spawns. Each one loads the OpenZL library, parses arguments, allocates memory. This adds ~0.5s per invocation.

2. **No streaming API**: We write each column to a temp file, then compress from file to file. Ideally we'd pass the data directly in memory.

Both are addressed in Phase 2 by switching to the OpenZL C API (`ZL_Compressor`, `ZL_CCtx`, `ZL_DCtx`).

### 1.7 Round-Trip Verification

The round-trip test is critical. After `nyx compress` → `nyx decompress`, the restored Parquet file must produce the exact same Arrow table as the original:

```python
original = pq.read_table("input.parquet")
restored = pq.read_table("restored.parquet")
assert original.equals(restored)
```

For every column type, this means:
- **Numeric**: exact bit-for-bit match (float NaN handling must be correct)
- **String**: exact character-for-character match (UTF-8 round-trip)
- **Nulls**: null positions match exactly
- **Types**: Arrow schema types match exactly
- **Row order**: rows in the same order

We'll build an automated test suite that runs this on all 4 benchmark datasets.

### 1.8 Deliverables

At the end of Phase 1:
- `nyx compress input.parquet -o output.parquet` works on any Parquet file
- `nyx decompress output.parquet -o restored.parquet` reconstructs the original
- `nyx inspect output.parquet` shows schema, column encodings, sizes, compression ratios
- Round-trip verified on all 4 benchmark datasets
- Compression ratios match our benchmarks (20-58% better than Parquet+zstd)
- Column pruning works (decompress only requested columns)

---

## Phase 2: Performance + Ecosystem (4-8 weeks after Phase 1)

### 2.1 OpenZL C API Integration

Replace subprocess calls with direct C API calls via Python `ctypes` or `cffi`. This eliminates:
- Process spawn overhead (~0.5s per column → ~0ms)
- Temp file I/O (compress/decompress in memory)
- Serialization overhead

The relevant OpenZL C API functions:

```c
// Training
ZL_Compressor* ZL_Compressor_create(profile, data, size);
ZL_Compressor* ZL_Compressor_train(data, size, options);

// Compression
ZL_CCtx* ZL_CCtx_create(compressor);
size_t ZL_CCtx_compress(ctx, dst, dst_capacity, src, src_size);

// Decompression
ZL_DCtx* ZL_DCtx_create();
size_t ZL_DCtx_decompress(ctx, dst, dst_capacity, src, src_size);
ZL_FrameInfo ZL_getFrameInfo(src, src_size);  // get decompressed size
```

**Implementation**: Build a Python C extension (`_openzl.so`) that links against the OpenZL static library. Expose `compress(data: bytes, profile: str) -> bytes` and `decompress(data: bytes) -> bytes`. The `ColumnEncoder` and `ColumnDecoder` call these directly.

**Impact**: Compression time drops dramatically. The overhead per column goes from ~0.5s + file I/O to ~0ms. For a 50-column table, this saves ~25 seconds of pure overhead.

This requires compiling OpenZL as a static library (`.a`) and linking it into our Python extension. Since we control the build (`nyx build`), this is straightforward.

### 2.2 Model Reuse (Separate Train + Compress)

In Phase 1, every compression uses `--train-inline`: the model is trained on the data being compressed, in the same pass. This is simple but has two costs:
1. Training time dominates compression time (e.g., 200s training + 5s compression)
2. Each column chunk trains independently, even if column data is similar across files

**Model reuse** separates the two steps:

```
Step 1 (one-time): Train a model on a representative sample
  zli train sample_data.bin --profile le-i32 --output model.zlc

Step 2 (per-file): Compress using the trained model
  zli compress data.bin --compressor model.zlc --output data.zl
```

Benefits:
- Training happens once. Compression is fast (seconds, not minutes).
- The same model works across files with similar schemas (e.g., daily partitions of the same table).
- Models can be versioned and stored alongside the data (in Parquet key-value metadata or a sidecar).

**Schema-level model library**: For a table with columns `(timestamp INT64, user_id INT32, email STRING, ...)`, we train one model per column type:
- `model_timestamp_le-i64.zlc`
- `model_user_id_le-i32.zlc`
- `model_email_dict_indices_le-i32.zlc`
- `model_email_dict_blob_serial.zlc`

These models are stored in a `.nyx-models/` directory or embedded in the Parquet key-value metadata. When compressing a new file with the same schema, the models are loaded and reused.

### 2.3 DuckDB Extension

DuckDB is the most important ecosystem target. It's the primary tool data engineers use for ad-hoc Parquet queries.

**What the extension does**:
1. Registers a custom Parquet codec (ID 100 → OpenZL)
2. When DuckDB reads a Parquet file with codec 100, it calls our decompression function
3. The decompressed data is returned as Arrow arrays
4. DuckDB handles the rest (query execution, joins, aggregations)

**Implementation**:
- DuckDB extensions are C++ shared libraries (`.duckdb_extension`)
- Our extension links against the OpenZL static library
- It registers a `ParquetCodec` that calls `ZL_DCtx_decompress()`
- It also handles encoding reversal (delta, binary_convert, dictionary) based on the column metadata in the Parquet key-value metadata

**User experience**:
```sql
-- Install the extension (one-time)
INSTALL openzl FROM '/path/to/openzl.duckdb_extension';
LOAD openzl;

-- Query OpenZL-compressed Parquet files just like normal Parquet
SELECT email, COUNT(*) FROM 'compressed.parquet' GROUP BY email;
```

Column pruning and predicate pushdown work automatically because DuckDB reads the Parquet footer (schema, statistics, column offsets) the same way it does for any Parquet file. It only calls our decompression function for the specific column chunks it needs.

### 2.4 Arrow C++ Codec Plugin

Register OpenZL as a codec in Apache Arrow's C++ library. This makes our files readable by any Arrow-based tool: Pandas, Polars, Spark (via Arrow), Trino, DataFusion.

```cpp
class OpenZLCodec : public arrow::util::Codec {
 public:
  arrow::Result<int64_t> Decompress(
      int64_t input_len, const uint8_t* input,
      int64_t output_buffer_len, uint8_t* output_buffer) override {
    // Call ZL_decompress()
  }
  // ...
};

// Register at library load time
ARROW_REGISTER_CODEC(100, OpenZLCodec);
```

### 2.5 Python SDK

```python
import openzl_parquet as ozl

# Compress
ozl.compress("input.parquet", "output.parquet")

# Decompress
ozl.decompress("output.parquet", "restored.parquet")

# Read directly (returns pyarrow.Table)
table = ozl.read_parquet("output.parquet", columns=["email", "score"])

# Schema inspection
info = ozl.inspect("output.parquet")
print(info.columns)  # [ColumnInfo(name='email', encoding='dictionary', ...)]
```

### 2.6 Parallel Column Compression

Compress multiple columns simultaneously using a thread pool. Each column is independent.

```
Row Group with 10 columns, 8 CPU cores:
  Thread pool: 4 workers (cores / 2, to avoid oversubscription with OpenZL's internal threading)
  Each worker: compress 1 column at a time
  Total: 10 columns / 4 workers = ~3 batches
  Wall clock: ~3x the slowest single-column time (vs 10x sequential)
```

With C API integration (2.1), this becomes trivial — no subprocess spawning, just in-memory calls.

### 2.7 Deliverables

At the end of Phase 2:
- `nyx compress` runs 5-10x faster (C API + parallelism + model reuse)
- DuckDB can query our files natively with the extension installed
- Python SDK available via `pip install openzl-parquet`
- Model reuse across files with the same schema
- Arrow C++ codec enables reading from Pandas, Polars, and other Arrow-based tools

---

## Phase 3: Paged Compression (Option B)

**Goal**: Enable sub-column-chunk random access for point lookups and narrow range queries. This is a competitive advantage over every existing Parquet codec.

**Timeline estimate**: 4-6 weeks after Phase 2.

### 3.1 What Changes

Instead of compressing each column chunk as a single `.zl` frame, we split it into pages of fixed row count (configurable, default 10,000 rows). Each page is independently decompressible.

```
Column chunk (1M rows, INT64):

Phase 1 (Option A):
  ┌───────────────────────────────────┐
  │    Single .zl frame (2.1 MB)      │
  └───────────────────────────────────┘

Phase 3 (Option B):
  ┌────────┬────────┬────────┬───┬────────┐
  │ Page 0 │ Page 1 │ Page 2 │...│ Page 99│
  │ 10K    │ 10K    │ 10K    │   │ 10K    │
  │ rows   │ rows   │ rows   │   │ rows   │
  │ (.zl)  │ (.zl)  │ (.zl)  │   │ (.zl)  │
  └────────┴────────┴────────┴───┴────────┘
  + Page index: [0, 21340, 42190, ...]
```

### 3.2 Train on Full Column, Compress per Page

This is the key insight from the architecture document. OpenZL's training and compression are decoupled:

```
Full column (1M values)
        │
        ▼
   zli train ────► model.zlc (captures data patterns)
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
   Page 0 (10K)    Page 1 (10K)    Page 99 (10K)
        │               │               │
   zli compress    zli compress    zli compress
   --compressor    --compressor    --compressor
   model.zlc       model.zlc       model.zlc
        │               │               │
        ▼               ▼               ▼
   page_0.zl       page_1.zl       page_99.zl
```

The model learns the full column's data distribution (e.g., "monotonically increasing INT64 with small deltas" or "5 unique values with frequencies 40/25/20/10/5%"). These patterns hold equally well for each page. Compression ratio loss from paging is expected to be <2% based on our Nyx FASTA experience (same architecture: train on 200 MiB sample, compress independent chunks).

### 3.3 Page Index

Stored in the column chunk header or in the Parquet key-value metadata:

```json
{
  "email": {
    "encoding": "dictionary",
    "page_size": 10000,
    "num_pages": 100,
    "page_offsets": [0, 21340, 42190, 63401, ...],
    "page_stats": [
      {"min": "aaa@example.com", "max": "bzz@test.org", "null_count": 0},
      {"min": "caa@example.com", "max": "dzz@test.org", "null_count": 0},
      ...
    ]
  }
}
```

### 3.4 Three-Level Predicate Pushdown

With paged compression and page-level statistics, we achieve three levels of data skipping:

```
Query: SELECT * FROM data WHERE score > 95

Level 1: Row group skipping
  Footer says row_group_0.score.max = 100 → read it
  Footer says row_group_1.score.max = 80  → SKIP entire row group

Level 2: Column pruning
  Query only references "score" → skip all other columns

Level 3: Page skipping (NEW — our competitive advantage)
  Page 0: score.max = 72 → SKIP
  Page 1: score.max = 68 → SKIP
  Page 2: score.max = 98 → DECOMPRESS (only this 10K-row page)
  ...
```

No existing Parquet codec offers Level 3 skipping within a column chunk. Parquet's built-in page index is not widely used and is separate from the codec. Our approach integrates statistics directly into the compression metadata.

### 3.5 Backward Compatibility

An Option A file is a special case of Option B with exactly one page per column chunk. The reader always checks the page index:
- If `num_pages == 1`: decompress the entire column chunk (Option A behavior)
- If `num_pages > 1`: use the page index for random access

No version flag needed. The metadata is self-describing.

### 3.6 Deliverables

At the end of Phase 3:
- Point lookups decompress ~80 KB instead of ~8 MB per column
- Three-level predicate pushdown (row group → column → page)
- Configurable page size (default 10K rows)
- DuckDB extension updated to use page-level access
- Compression ratios within 2% of Option A (validated by benchmark)

---

## Phase 4: Production Hardening + Scale

### 4.1 Large File Support

- Row groups larger than RAM: stream column chunks instead of loading entirely
- Files with thousands of columns: lazy metadata parsing
- Multi-gigabyte columns: chunked compression/decompression with bounded memory

### 4.2 Schema Evolution

- Handle schema changes across Parquet files (added/removed/reordered columns)
- Model compatibility checks (detect when a column's distribution has shifted enough to warrant retraining)

### 4.3 Cloud-Native Integration

- S3/GCS/Azure Blob: read footer with a single range request, then fetch only needed column chunks
- Predicate pushdown at the storage layer (S3 Select-style)
- Partitioned datasets: apply models across partition files with shared schemas

### 4.4 Monitoring + Observability

- Compression ratio per column, per row group
- Training time and model size tracking
- Decompression speed monitoring
- Alerting when compression ratios degrade (data distribution shift)

---

## Risk Assessment

| Risk | Impact | Mitigation |
|---|---|---|
| Custom codec ID (100) rejected by strict Parquet readers | Files unreadable without plugin | Fail gracefully; provide clear error message and plugin installation instructions. Long-term: propose OPENZL codec to Parquet spec. |
| Serial profile timeout on large string dictionaries | Dictionary encoding fails | Already mitigated: 1 MiB chunking. Validated on 17 MiB dictionary blobs. |
| Model reuse produces worse ratios on out-of-distribution data | Regression in compression quality | Fall back to inline training if model-compressed size exceeds threshold. Track ratio per file and alert. |
| DuckDB / Arrow API changes break our extension | Plugin stops working | Pin to specific DuckDB/Arrow versions. Test in CI. |
| Subprocess overhead (Phase 1) makes compression slow | Bad user experience for large tables | Acceptable for Phase 1 (minutes, not hours). C API in Phase 2 eliminates this. |
| Paged compression ratio loss (Phase 3) exceeds expectations | Competitive advantage weakened | Full-column training mitigates this. Validated in FASTA pipeline (<2% loss). Benchmark before shipping. |

---

## Immediate Next Steps (This Week)

1. **Move `column_encoder.py` into the Nyx package** — promote from experiment to production code
2. **Build `column_decoder.py`** — implement the reverse of every encoding (delta cumsum, dict lookup, binary→text, null bitmap)
3. **Build the Parquet writer** — Python Thrift-based writer that produces valid Parquet files with codec ID 100
4. **Build the Parquet reader** — reads codec 100 files, calls decoder, returns Arrow arrays
5. **Wire into the CLI** — `nyx compress input.parquet` and `nyx decompress output.parquet`
6. **Round-trip test** — verify on all 4 benchmark datasets

---

## Appendix: File Format Specification (Phase 1)

### Magic + Row Groups + Footer

Identical to standard Parquet. The only change is in the column chunk metadata:

```thrift
struct ColumnMetaData {
  1: required CompressionCodec codec = 100  // OPENZL
  2: required list<Encoding> encodings
  3: required list<string> path_in_schema
  4: required Type type
  5: required i64 total_uncompressed_size
  6: required i64 total_compressed_size
  7: optional Statistics statistics
  ...
}
```

### Column Chunk Data Layout

```
Bytes:
  [Thrift PageHeader]
    type = DATA_PAGE
    uncompressed_page_size = <original column data size>
    compressed_page_size = <total bytes that follow>
  [Encoding header: variable length]
    [2 bytes: header version (1)]
    [2 bytes: encoding ID]
      0 = raw
      1 = delta
      2 = binary_convert
      3 = dictionary
    [2 bytes: profile ID]
      0 = serial
      1 = le-i16
      2 = le-i32
      3 = le-i64
    [4 bytes: null_count]
    [encoding-specific metadata: variable length, self-describing]
  [null_count > 0? Validity bitmap: ceil(num_rows / 8) bytes]
  [.zl frame: primary compressed data]
  [encoding == dictionary?
    [4 bytes: dict_chunk_count]
    [.zl frame: dict chunk 0]
    [.zl frame: dict chunk 1]
    ...
  ]
```

### Key-Value Metadata

```json
{
  "openzl:version": "1.0",
  "openzl:columns": {
    "timestamp": {
      "encoding": "raw",
      "profile": "le-i64",
      "width": 8
    },
    "email": {
      "encoding": "dictionary",
      "profile": "le-i32",
      "num_unique": 632262,
      "index_dtype": "int32",
      "dict_chunks": 18
    }
  },
  "openzl:original_metadata": "<base64-encoded original footer for exact reconstruction>"
}
```

The `openzl:original_metadata` field stores the original Parquet file's footer metadata (serialized and base64-encoded). This enables exact reconstruction of the original file during decompression — same row group sizes, same page boundaries, same statistics, same key-value metadata.
