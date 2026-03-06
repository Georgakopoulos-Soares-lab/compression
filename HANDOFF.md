# Agent Hand-Off Document: Telemetry Compression Service

**Created:** 2026-03-05
**Branch:** `theodore-telemetry`
**Repo:** `compression/` (stripped, deployment-ready)

---

## 1. What This Project Is

A **lossless compression service** for telegraf JSONL telemetry data. It takes raw JSONL output from telegraf agents, compresses it at ~42x ratio with bit-exact reconstruction, and bundles it into a single `.zljsonl` container file.

The codebase was stripped from a larger multi-format compression tool (FASTA, FASTQ, VCF, generic JSONL, CLI framework) down to **7 source files with zero runtime dependencies** beyond Python 3.8+ stdlib. The stripping itself yielded a major performance improvement: compress went from 15.8s to 2.6s, decompress from 6.7s to 1.8s — mostly by eliminating import overhead and unnecessary abstraction layers.

---

## 2. The Telemetry Data We Are Compressing

### Source

Telegraf agents collect system metrics (CPU, memory, disk, network, kernel stats, PostgreSQL metrics, Kafka health, Vertica stats, x509 certificates, etc.) and emit them as JSONL — one JSON object per line.

### Structure of Each Record

```json
{
  "type": "kernel",
  "creator": "nom-telegraf",
  "current-time": "2024-01-15T10:30:00Z",
  "host-name": "node-abc",
  "key": "",
  "node-id": "550e8400-e29b-41d4-a716-446655440000",
  "server-start-time": "2024-01-10T00:00:00Z",
  "content": {
    "boot_time": 1705123456,
    "context_switches": 98765432,
    "interrupts": 54321098,
    "processes_forked": 12345,
    "entropy_avail": 3890
  }
}
```

Every record has:
- **Envelope fields** (fixed set of 7): `type`, `creator`, `current-time`, `host-name`, `key`, `node-id`, `server-start-time`
- **Content field**: A nested object whose keys vary by `type`

### Dataset Characteristics

- **Source directory:** `data/openZL-telemetru/nom-telegraf/` (5 parts, ~524 MB each, ~2.6 GB total)
- **35 distinct record types** in the trained schema
- Records are **interleaved** — a single JSONL file contains all types mixed together
- Field ordering within each JSON object is semantically meaningful and must be preserved for bit-exact reconstruction
- Numeric values appear as raw text (integers, floats, percentages) — **no float normalization is acceptable**

### The 35 Types (from trained schema)

| Type | Columns | Schema | Content Fields |
|------|---------|--------|----------------|
| `cpu` | 17 | uniform | 11 (usage_guest, usage_idle, usage_iowait, etc.) |
| `mem` | 40 | uniform | 34 (active, available, buffered, cached, swap_*, etc.) |
| `net` | 96 | **multi(2)** | 88+ (bytes_recv/sent, icmp_*, tcp_*, udp_*) |
| `disk` | 19 | uniform | 13 (device, free, fstype, path, total, used, etc.) |
| `diskio` | 22 | uniform | 15 (io_await, io_time, read_bytes, write_bytes, etc.) |
| `kernel` | 11 | uniform | 5 (boot_time, context_switches, entropy_avail, etc.) |
| `processes` | 17 | uniform | 11 (blocked, running, sleeping, total, zombies, etc.) |
| `x509_cert` | 27 | **multi(4)** | 17 base + 4 distinct schemas (varies by cert type) |
| `swap` | 13 | **multi(2)** | varies |
| `system` | 14 | **multi(2)** | varies |
| `postgresql` | 11 | uniform | 5 |
| `pg_leader_state` | 17 | uniform | 11 |
| `pg_replica_state` | 13 | **multi(2)** | varies |
| `internal_write` | 20 | **multi(2)** | varies |
| `vertica-queries-stats` | 23 | uniform | 17 |
| ... and 20 more | | | |

**Multi-schema types** (6 out of 35): `net`, `x509_cert`, `swap`, `system`, `pg_replica_state`, `internal_write` — these have records with varying content key sets.

---

## 3. How We Compress: The Three-Stage Pipeline

### Stage 1: Structural Decomposition (JSONL → Type-Grouped TSVs)

**Why:** Raw JSONL is terrible for compression. Each line mixes heterogeneous data (timestamps next to hostnames next to integers). By grouping records by type and converting to columnar TSVs, each column becomes homogeneous data that compresses dramatically better.

**How:**

1. **Parse each JSONL line** using a C extension (`_telemetry_scanner.c`) that is ~12x faster than pure Python. The scanner preserves exact numeric text — it never converts `"98765432"` to a float and back. This is critical for bit-exact round-trips.

2. **Group records by `type`**. Each type gets its own TSV file. The `type` field itself is not stored in the TSV (it's implicit from the filename).

3. **Separate envelope from content**. Envelope fields become direct TSV columns. Content fields are prefixed with `c.` (e.g., `c.boot_time`).

4. **Eliminate `__ckeys__` redundancy** (the A1 optimization):
   - For **uniform-schema types** (29 of 35): The content key list is stored once in the schema. The per-row `__ckeys__` column is dropped entirely. Without this, every row of `kernel.tsv` would repeat `"boot_time,context_switches,entropy_avail,interrupts,processes_forked"`.
   - For **multi-schema types** (6 of 35): The distinct content key combinations are enumerated and each row stores a 1-byte integer index (`__skidx__`) instead of the full comma-separated key list.

5. **Capture line ordering** in a binary manifest (`manifest.bin`):
   - RLE-encoded: consecutive runs of the same type stored as `(type_index, count)` — 5 bytes per run
   - Type indices are 1-byte references into `schema.type_index`
   - Blank lines encoded as `0xFF`
   - Result: ~2–10 KB for a 184 MB file (vs. ~2 MB if stored as JSON)

6. **Embed schema** as `schema.json` alongside the TSVs for self-contained decompression.

**Impact:** This stage alone reduces effective data size ~1.75x through redundancy elimination, and — more importantly — transforms the data into a form that trained compression can exploit.

### Stage 2: Trained OpenZL Compression

Each TSV file is compressed by the `zli` binary using a pre-trained compressor model.

```
zli compress <input.tsv> --output <output.tsv.zl> --force --compressor telemetry_csv.zl_compressor
```

For the manifest and schema (already compact), a generic profile is used:
```
zli compress <manifest.bin> --output <manifest.bin.zl> --force --profile serial --profile-arg ""
```

All parts are compressed in parallel using a `ThreadPoolExecutor` (one thread per CPU core). Each `zli` call is a subprocess, so no GIL contention.

**Impact:** ~24x compression on the pre-processed TSVs.

### Stage 3: Container Packaging

All compressed parts are bundled into a `.zljsonl` binary container:

```
"ZLJSONL\0"        (8 bytes magic)
version: 1         (U32LE)
num_entries         (U32LE)
[Directory]         For each entry: name_len(U16LE), name(bytes), compressed_size(U64LE), original_size(U64LE)
[Data blobs]        Sequential compressed data in directory order
CRC32               (U32LE checksum of everything before it)
```

### Decompression (Reverse)

1. Extract container, verify CRC32
2. Decompress all parts in parallel (`zli decompress`)
3. Reconstruct JSONL using template-based reconstruction:
   - TSV rows loaded as raw bytes (no string decoding in hot path)
   - Per-type byte-level instruction templates pre-computed (field ordering, content schema lookups)
   - 1 MiB buffered binary output
   - Result: byte-exact original JSONL

---

## 4. Why These Specific Manipulations

### Why type-grouping instead of just compressing JSONL directly?

Raw JSONL with gzip/zstd achieves ~5–8x. Our pipeline achieves ~42x. The difference comes from:

1. **Column homogeneity**: When `cpu` records are grouped, column 7 is always `usage_idle` — a stream of percentage floats. This is drastically more compressible than interleaved `{"type":"cpu","creator":...}` lines where the compressor sees key names mixed with values.

2. **Key elimination**: In raw JSONL, every record repeats all its key names. After TSV conversion, keys appear once (in the header). For `mem` with 34 content fields, this alone eliminates 34 key strings × N records of redundancy.

3. **`__ckeys__` elimination**: The A1 optimization removes the content schema from every row. For a type with 15 content fields and 100K records, this saves ~2 MB of pure redundancy.

### Why a C extension for JSON parsing?

Python's `json.loads()` is unsuitable because it **normalizes numbers**. The value `1705123456` might round-trip through float as `1705123456.0` or lose precision on very large integers. The C scanner treats all values as raw text slices — no parsing, no conversion. It also provides ~12x speedup over the pure-Python fallback scanner.

### Why binary manifest instead of JSON?

The original line ordering (which type appears on which line) was stored as JSON arrays. For a 433K-line file, this was ~2 MB. The binary RLE manifest reduces this to ~5 KB — a 400x reduction that also compresses better.

### Why parallel chunk parsing?

For files >10 MB, the file is split at newline boundaries into N chunks (one per CPU core). Each chunk is parsed independently via `multiprocessing.Pool`. This cuts parse time roughly proportional to core count. The merge step has a fast-path: when chunks have identical column sets for a type (the common case), records are bulk-extended with zero remapping overhead.

### Why bytes-native decode?

The reconstruction path was originally string-based (build Python strings, encode to UTF-8 at write time). Moving to bytes throughout — TSV rows loaded as `bytes`, template fragments as `bytes`, output buffered as `bytearray` — eliminated per-line encoding overhead and cut decode time significantly.

---

## 5. OpenZL: What It Is

**OpenZL** is a **learned/trained compression system**. Unlike general-purpose compressors (gzip, zstd, lz4) that use fixed algorithms, OpenZL trains probability models on representative data and uses those models for compression. This is conceptually similar to how a language model learns text patterns, except OpenZL learns byte-level patterns for compression.

### The `zli` Binary

- **Version:** `zstrong-cli version 0.1` — "Demo CLI for OpenZL. NO VERSION STABILITY IS IMPLIED!!"
- **Type:** Mach-O 64-bit ARM64 (macOS only in current build)
- **Size:** 4.2 MB
- **Commands:** `compress`, `decompress`, `train`, `benchmark`, `inspect`, `list-profiles`

### Available Profiles

| Profile | Description |
|---------|-------------|
| `csv` | CSV/TSV data. Pass separator with `--profile-arg` |
| `serial` | Raw bytes (generic, no structure assumed) |
| `sddl` | Structured data via Simple Data Description Language |
| `parquet` | Apache Parquet (canonical format) |
| `pytorch` | PyTorch model files |
| `le-i16/32/64` | Little-endian signed integers |
| `le-u16/32/64` | Little-endian unsigned integers |
| `sao` | SAO format (Silesia corpus) |

### How We Use It

1. **Training** (done once, offline): `zli train` on representative telemetry TSV data produces `telemetry_csv.zl_compressor` (14 KB model file)
2. **Compression**: `zli compress --compressor <model>` applies the trained model
3. **Decompression**: `zli decompress` — no model needed, decompressor state is embedded in compressed output
4. **Fallback**: For non-TSV data (manifest, schema), `--profile serial` uses generic compression

### The Trained Compressor Model

- **File:** `models/lossless_telemetry/telemetry_csv.zl_compressor` (14 KB)
- **Trained on:** Representative telegraf JSONL data processed through our TSV pipeline
- **Profile used for training:** `csv` (the model learns TSV/CSV column patterns)
- **What it learned:** Timestamp formats, hostname distributions, counter value ranges, field repetition patterns, numeric precision characteristics specific to telegraf output

---

## 6. OpenZL: Discovered Limitations and Considerations

### Platform Limitation

The current `zli` binary is **ARM64 macOS only**. For deployment on Linux servers, an x86_64 or aarch64 Linux build would be needed. The binary is pre-built; we do not compile OpenZL from source (the full source tree was 245 MB and was deleted during the stripping process).

### Version Stability

The CLI explicitly states "NO VERSION STABILITY IS IMPLIED." This means:
- Compressed file format could theoretically change between `zli` versions
- The `--compressor` model format could change
- For production, pin the exact `zli` binary version

### Model Specificity

The trained compressor is **specific to telegraf telemetry TSVs**. It will:
- Work well on any telegraf data with similar type distributions
- Degrade gracefully on telegraf data with new/unseen types (falls back to generic compression for unfamiliar patterns)
- NOT be optimal for completely different data formats (genomic data, log files, etc.) — use format-specific models for those

### Compression Ratio Variability

Observed ratios across parts of the same dataset:

| File | Size | Compressed | Ratio |
|------|------|------------|-------|
| part-001.jsonl | 524 MB | 10.7 MB | 49x |
| part-002.jsonl | 524 MB | 14.9 MB | 35x |
| part-003.jsonl | 524 MB | 14.1 MB | 37x |
| part-005.jsonl | 524 MB | 10.9 MB | 48x |

The variation (35x–49x) correlates with **type distribution per file**. Files with more uniform, repetitive types (e.g., heavy on `cpu`, `kernel`) compress better. Files with more `x509_cert` (multi-schema, variable content) or `net` (96 columns) compress less.

### The `csv` Profile and TSV

OpenZL's `csv` profile is designed for comma-separated data but works with tab-separated data as well. The `--profile-arg` flag can specify a custom separator, but during training with our TSV data the model learned tab-delimited patterns directly. We do NOT pass `--profile-arg "\t"` at compress time — the trained compressor already encodes this knowledge.

### No Streaming Support

`zli` operates on complete files. It cannot compress a stream incrementally. For a service receiving continuous telegraf output, data must be batched into files (e.g., time-windowed chunks) before compression.

### Subprocess Overhead

Each `zli` call is a subprocess (`subprocess.run()`). For very small files, the process spawn overhead (~10ms) may dominate. This is irrelevant for our 184 MB files but matters if someone tries to compress individual records or very small batches.

### Decompressor Embedding

`zli` embeds decompressor state in the compressed output. This means:
- **No model needed for decompression** — `zli decompress` works standalone
- Compressed files are **self-contained** — they don't depend on the `.zl_compressor` file
- The 14 KB model is essentially amortized across the compressed output

---

## 7. Key Bugs We Fixed and Lessons Learned

### Multiprocessing Binary-Mode Seek Bug

**Problem:** When parsing JSONL in parallel, workers opened the file in text mode (`open("r")`) but seeked to byte positions computed from binary mode. Python's text-mode `seek()` only accepts positions from `tell()`, not arbitrary byte offsets. This caused workers to start reading at wrong positions, producing corrupted output.

**Fix:** Changed to `open("rb")` with manual `.decode("utf-8")` after reading. Single-worker always passed; multi-worker (2, 4, 8) failed before the fix.

**Lesson:** Never mix binary byte offsets with text-mode file operations in Python.

### x509_cert Fast-Path Row Building Failure

**Problem:** Attempted an optimization where if `len(row_dict) == expected_column_count`, skip column-name checking and assume columns match. This broke because `x509_cert` records have multiple content schemas with **different keys but the same count**. Values ended up in wrong columns.

**Fix:** Reverted to safe column-name checking. The fast-path is only safe at the merge level (comparing full column lists), not at the row level (comparing counts).

**Lesson:** Multi-schema types like `x509_cert` are adversarial for column-count-based optimizations.

### setuptools Auto-Discovery Error

**Problem:** `python3 setup.py build_ext --inplace` failed with "Multiple top-level packages discovered in a flat-layout: ['data', 'models']". setuptools tried to auto-discover Python packages from `data/` and `models/` directories.

**Fix:** Added `py_modules=[], packages=[]` to `setup()` call to disable auto-discovery.

---

## 8. Performance Optimization History

| Change | Compress Impact | Decompress Impact |
|--------|----------------|-------------------|
| Binary-mode chunk reading (bug fix) | 7s → 15s (regression) | 3s → 6s (regression) |
| Fast-path merge optimization | -3s (merge: 4.1s → 1.1s) | — |
| Bytes-native TSV loading | — | -1s |
| Bytes-native JSONL writing | — | -1s |
| Codebase stripping (eliminate imports) | 15s → 2.6s | 6s → 1.8s |

The stripping was the biggest single win — eliminating `click`, `tqdm`, the entire `nyx` package tree, and dozens of unused imports cut startup and runtime dramatically.

---

## 9. File Inventory (Complete)

```
compression/
├── telemetry_codec.py          # 1099 lines — JSONL↔TSV encode/decode, parallel parsing, manifest, RLE
├── _telemetry_scanner.c        # 481 lines — C extension: fast JSON scanning, raw text preservation
├── zljsonl.py                  # 132 lines — .zljsonl container format (magic, directory, CRC32)
├── telemetry_service.py        # 179 lines — Top-level compress()/decompress() API + CLI
├── test_roundtrip.py           # 74 lines — Round-trip correctness with 1/2/4/8 workers
├── setup.py                    # 15 lines — Build C extension
├── pyproject.toml              # 10 lines — Minimal project metadata
├── zli                         # 4.2 MB — Pre-built OpenZL binary (ARM64 macOS)
├── models/
│   └── lossless_telemetry/
│       ├── telemetry_csv.zl_compressor   # 14 KB — Trained compressor model
│       └── telemetry_schema.json         # 21 KB — Schema for 35 telegraf types
├── ANALYSIS.md                 # Technical analysis document
├── HANDOFF.md                  # This file
└── .gitignore
```

**Zero runtime dependencies.** Only needs Python 3.8+ stdlib + the `zli` binary. The C extension is optional (pure-Python fallback exists, ~12x slower).

Build: `python3 setup.py build_ext --inplace`

---

## 10. Telegraf Integration Considerations

### Current Data Flow

```
Telegraf Agent → JSONL files → compress() → .zljsonl → storage/transfer → decompress() → original JSONL
```

### For Production Deployment

1. **Batching strategy**: Telegraf emits data continuously. The service needs a batching layer — accumulate N seconds or N MB of JSONL, then compress. The current code operates on complete files.

2. **Schema evolution**: If telegraf adds new metric types or new fields to existing types, the schema auto-extends at encode time. New types get generic compression (no trained model benefit). To maintain optimal ratios, periodically retrain the compressor on fresh data.

3. **Multi-host aggregation**: If collecting from multiple hosts, the data is already mixed in the JSONL. The pipeline handles this naturally — type grouping separates regardless of source host.

4. **Linux deployment**: The current `zli` is macOS ARM64 only. Need a Linux build for server deployment.

5. **API usage**:
   ```python
   from telemetry_service import compress, decompress

   # Compress
   compress("input.jsonl", "output.zljsonl")

   # Decompress
   decompress("output.zljsonl", "reconstructed.jsonl")

   # With custom models directory
   compress("input.jsonl", "output.zljsonl", models_dir="/path/to/models")

   # Control parallelism
   compress("input.jsonl", "output.zljsonl", num_workers=4)
   ```

6. **CLI usage**:
   ```bash
   python3 telemetry_service.py compress input.jsonl output.zljsonl
   python3 telemetry_service.py decompress output.zljsonl reconstructed.jsonl
   ```

---

## 11. What Was Deleted (And Why It Doesn't Matter)

The original codebase (`nyx/`) contained:
- **Genomic codecs**: FASTA, FASTQ, VCF — not telemetry
- **Generic JSONL codec**: Replaced by telemetry-specific `telemetry_codec.py`
- **CLI framework**: `click`-based CLI with 15+ commands — replaced by direct Python API
- **OpenZL source tree**: 245 MB of C++ source/build artifacts — only the 4 MB binary is needed
- **Orchestration layer**: 2124-line `_lossless.py` — replaced by 179-line `telemetry_service.py`
- **Training commands**: Not needed for deployment (model already trained)
- **Benchmark/inspect tools**: Development-only
- **21+ GB test data**: Not deployment artifacts

Everything needed for compress/decompress is in the 7 files listed above.

---

## 12. Open Questions / Future Work

1. **Linux `zli` binary**: Required for server deployment. Either cross-compile from OpenZL source or obtain pre-built.
2. **C++ rewrite of codec**: The Python codec (`telemetry_codec.py`) is the main CPU bottleneck. A C++ implementation could potentially cut encode/decode time 5–10x. The C extension already handles parsing; the remaining Python work is column management, TSV writing, and JSONL reconstruction.
3. **Streaming compression**: Current architecture is batch-only. For real-time telegraf integration, investigate whether OpenZL supports incremental/streaming operation.
4. **Compressor retraining**: The current model was trained on a specific telegraf dataset. As data evolves, retraining may improve ratios.
5. **SDDL integration**: OpenZL's `sddl` profile allows describing data structure formally. This could potentially improve compression further by teaching OpenZL the exact TSV column semantics.
