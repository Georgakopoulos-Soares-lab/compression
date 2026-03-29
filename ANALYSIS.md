# Telegraf JSONL Compression: Detailed Analysis

## Overview

This pipeline achieves **~42x lossless compression** on telegraf JSONL telemetry data with **bit-exact reconstruction**. A 184 MB JSONL file compresses to ~4.4 MB in ~2.6 seconds and decompresses in ~1.8 seconds.

The compression is a three-stage process:

1. **Structural decomposition** — JSONL records are parsed, grouped by `type`, and converted to type-grouped TSV files
2. **Trained compression** — Each TSV is compressed by OpenZL using a pre-trained compressor profile tuned for telemetry CSV/TSV patterns
3. **Container packaging** — All compressed parts are bundled into a single `.zljsonl` binary container

Decompression reverses the process: extract container, decompress parts, reconstruct the exact original JSONL.

---

## Stage 1: JSONL → Type-Grouped TSVs

### Input Format

Each line of the input is a JSON object with a common telegraf envelope:

```json
{"type":"kernel","creator":"telegraf","current-time":"2024-01-15T10:30:00Z","host-name":"node1","content":{"boot_time":1705123456,"context_switches":98765432,"interrupts":54321098}}
```

The top-level keys fall into two categories:
- **Envelope fields** (fixed set): `type`, `creator`, `current-time`, `host-name`, `key`, `node-id`, `server-start-time`
- **Content field**: A nested object whose keys vary by `type`

### Parsing (C Extension)

Each JSONL line is parsed by a C extension (`_telemetry_scanner.c`) that provides ~12x speedup over pure Python. The scanner:

- Operates as a **raw text scanner** — it does not convert JSON values to Python types. Numbers like `98765432` are preserved as the exact string `"98765432"`, not parsed to float and back. This is critical for bit-exact round-trips.
- Returns `(record_type, row_dict, top_keys)` for each line, where:
  - `record_type` = the unquoted value of `"type"` (e.g., `"kernel"`)
  - `row_dict` = flat dict with envelope fields as-is and content fields prefixed with `c.` (e.g., `c.boot_time`)
  - `top_keys` = ordered list of top-level keys (preserves original field ordering)

### Parallel Parsing

For files >10 MB, the file is split at newline boundaries into chunks (one per CPU core). Each chunk is parsed independently via `multiprocessing.Pool`. Chunks are read in binary mode with byte-offset seeking, then decoded to UTF-8 and split into lines. Results are merged with a fast-path optimization: when chunk columns match exactly (the common case for uniform-schema types), records are bulk-extended with no remapping.

### Type Grouping

Records are grouped by their `type` field. A typical telegraf dataset contains 30–35 distinct types (e.g., `kernel`, `disk`, `cpu`, `mem`, `net`, `processes`, `x509_cert`, etc.). Each type gets its own TSV file.

### The `__ckeys__` Optimization (A1)

Each record originally carries a `__ckeys__` column — a comma-separated list of its content field names. This is needed for reconstruction because different types have different content schemas.

**Uniform-schema types** (most types — e.g., `kernel` always has the same content keys): The content key list is stored **once in the schema** and the `__ckeys__` column is dropped entirely. This eliminates massive redundancy — without this, `__ckeys__` would repeat the same key list for every single row.

**Multi-schema types** (rare — e.g., `x509_cert` where different certificates have different fields): The distinct schemas are enumerated and each row stores a compact integer index (`__skidx__`) instead of the full key list. For example, if there are 3 distinct content schemas, rows store `0`, `1`, or `2` instead of repeating the full comma-separated key names.

### TSV Output

Each type produces a TSV file with:
- **Header row**: tab-separated column names
- **Data rows**: tab-separated raw JSON values (strings still quoted, numbers as raw text)

Column layout for a type like `kernel`:
```
creator	current-time	host-name	key	node-id	server-start-time	c.boot_time	c.context_switches	c.interrupts	c.processes_forked	c.entropy_avail	c.disk_pages_in	c.disk_pages_out
"telegraf"	"2024-01-15T10:30:00Z"	"node1"	...	...	...	1705123456	98765432	54321098	...	...	...	...
```

### Manifest (Line Ordering)

The original JSONL has records interleaved across types. To reconstruct the exact file, the line ordering is captured in a **binary manifest** (`manifest.bin`):

- **RLE-encoded**: Consecutive runs of the same type are stored as `(type_index, count)` pairs
- **Binary format**: Each run is 5 bytes (1-byte type index + 4-byte LE count)
- **Per-type running counters**: The row index within each type's TSV is implicit — during decode, counters track the current position per type
- Blank lines are encoded with a special type index (`0xFF`)

A typical manifest is ~2–10 KB for a 184 MB file (vs. ~2 MB if stored as JSON).

### Schema

A reusable `schema.json` is saved alongside the TSVs containing:
- Column names for each type
- Field ordering for each type (to reconstruct the exact JSON key order)
- Content schema(s) for each type (the `__ckeys__` optimization data)
- A `type_index` mapping type names to compact integer IDs (used in the binary manifest)

The schema is pre-computed during training and reused across files from the same telemetry source. It is also embedded in the compressed output for self-contained decompression.

---

## Stage 2: TSV Compression with OpenZL

Each TSV file is compressed using the `zli` binary (OpenZL's command-line tool) with a **pre-trained compressor** (`telemetry_csv.zl_compressor`).

### Why Trained Compression?

Generic compressors (gzip, zstd) achieve ~5–8x on telemetry JSONL. The trained OpenZL compressor achieves ~42x because:

1. **Column-aware modelling**: The TSV decomposition converts heterogeneous JSON into homogeneous columnar data. Each column contains values of the same semantic type (timestamps, hostnames, numeric counters), which compress far better than mixed JSON.

2. **Trained probability models**: The compressor was trained on representative telemetry data. It learns patterns specific to telegraf output — timestamp formats, hostname distributions, counter value ranges, field repetition patterns.

3. **Structural redundancy elimination**: The `__ckeys__` optimization removes the single largest source of redundancy before compression even starts.

### Compression of Non-TSV Parts

- **`manifest.bin`**: Compressed with the `serial` profile (generic, no trained model) since it's already compact binary
- **`schema.json`**: Also compressed with the `serial` profile

### Parallelism

All TSV files (plus manifest and schema) are compressed in parallel using a `ThreadPoolExecutor`. Each `zli compress` call is a subprocess, so the GIL is not a bottleneck — true parallelism across CPU cores.

---

## Stage 3: `.zljsonl` Container

All compressed parts are bundled into a single binary container:

```
Offset  Content
------  -------
0       Magic: "ZLJSONL\0" (8 bytes)
8       Version: 1 (U32LE)
12      Num entries (U32LE)
16      Entry directory:
          For each entry:
            name_len (U16LE)
            name (UTF-8 bytes)
            compressed_size (U64LE)
            original_size (U64LE)
...     Entry data: sequential compressed blobs in directory order
-4      CRC32 checksum (U32LE) of everything from magic through last blob byte
```

The container is self-describing — it lists all parts with their names and sizes, allowing extraction without external metadata.

---

## Decompression Pipeline

### Step 1: Extract Container

The `.zljsonl` file is opened, CRC32 is verified, and each entry is extracted to a temporary directory.

### Step 2: Decompress Parts

All parts are decompressed in parallel using `zli decompress`. No trained model is needed — `zli` stores the decompressor state within each compressed file.

### Step 3: Reconstruct JSONL

The decoder reads the schema and manifest, then reconstructs the original JSONL:

1. **Load TSV data**: Each TSV is bulk-read as bytes and split into rows. The header line is skipped. All values remain as byte strings to avoid encoding overhead.

2. **Parse manifest**: The binary manifest is decoded to produce a flat list of `(type_name, row_index)` pairs — one entry per original line.

3. **Template-based reconstruction**: For each type, a set of **byte-level instructions** is pre-computed:
   - `"type_literal"` — emit the literal bytes `"type":"kernel"`
   - `"field"` — emit `"field_name":` followed by `row[column_index]`
   - `"content_uniform"` — emit `"content":{` followed by content fields from the known schema
   - `"content_multi"` — look up `__skidx__` to determine which content schema to use

4. **Buffered output**: A `bytearray` buffer accumulates output and flushes every 1 MiB. All operations are in bytes — no string encoding in the inner loop.

The reconstruction follows the manifest line-by-line, pulling the correct row from the correct type's TSV data and emitting the JSON with the original field ordering.

---

## Compression Ratio Breakdown

For a typical 184 MB telegraf JSONL file:

| Component | Original | Compressed | Ratio |
|-----------|----------|------------|-------|
| Type-grouped TSVs (total) | ~165 MB | — | ~0.9x (slight expansion from headers) |
| After `__ckeys__` elimination | ~105 MB | — | ~0.57x of original |
| After OpenZL compression | — | ~4.1 MB | ~25x on TSVs |
| Manifest + schema | ~10 KB | ~2 KB | ~5x |
| **Total pipeline** | **184 MB** | **~4.4 MB** | **~42x** |

The ratio breaks down as:
- **~1.75x** from structural decomposition + `__ckeys__` elimination (redundancy removal)
- **~24x** from trained OpenZL compression (statistical modelling)
- Combined: **~42x**

---

## Performance Characteristics

| Operation | Time (184 MB file) | Throughput |
|-----------|-------------------|------------|
| Compress | ~2.6s | ~71 MB/s |
| Decompress | ~1.8s | ~102 MB/s |

Key performance optimizations:
- **C extension** for JSON parsing (~12x vs pure Python)
- **Parallel chunk parsing** via multiprocessing
- **Fast-path merge** when chunk columns match (avoids row remapping)
- **Bytes-native decode** — TSV rows loaded and output written as bytes, no string encoding in the hot path
- **Buffered I/O** with 1 MiB flush threshold
- **Parallel zli compression/decompression** via thread pool
- **Pre-computed templates** for JSONL reconstruction (array-index lookups, not dict lookups)

---

## File Inventory

| File | Role | Size |
|------|------|------|
| `telemetry_codec.py` | JSONL ↔ TSV encode/decode | ~1100 lines |
| `_telemetry_scanner.c` | C extension for fast JSON scanning | ~480 lines |
| `zljsonl.py` | Container format read/write | ~130 lines |
| `telemetry_service.py` | Top-level compress/decompress API | ~180 lines |
| `zli` | Pre-built OpenZL binary | ~4 MB |
| `models/lossless_telemetry/telemetry_csv.zl_compressor` | Trained compressor | ~1.5 MB |
| `models/lossless_telemetry/telemetry_schema.json` | Pre-trained schema (35 types) | ~15 KB |

**Zero runtime dependencies** beyond Python 3.8+ standard library. The C extension is built with `python3 setup.py build_ext --inplace`.
