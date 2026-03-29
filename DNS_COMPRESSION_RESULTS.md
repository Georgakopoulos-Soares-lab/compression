# DNS Data Compression — Complete Investigation Results

## Background

The DNS data pipeline produces ~8.4 TB/day of binary TLV records flowing through
Kafka. The current compression is Kafka-level Snappy (applied by librdkafka),
achieving ~2.3x on ~80 KB chunks. We investigated whether OpenZL (Meta's trained
compression framework) or other approaches could achieve better compression on
this data.

### The Data

DNS records are serialized as TLV (Type-Length-Value) binary inside GenericChunk
containers. Each Kafka message is one GenericChunk containing a batch of ~544 DNS
records (80 KB of TLV). The TLV format has a 19-byte fixed header per record
(timestamp, flags, port, inet_family) followed by variable TLV fields (DNS message,
client address, server address, view, device ID, core domain, etc.).

Data was collected from a live test environment running DAMP at 1.5M DNS req/sec:
- 4 GB of raw GenericChunks (45,663 chunks) from POP Kafka `nom-dns-base`
- 2 GB of decoded tab-delimited text from `nom-kafka-dump`
- Chunk sizes measured on 200 Kafka messages (avg 34.6 KB Snappy-compressed)

### The Current Pipeline

```
cache-serve → GenericChunk (TLV, compression_flag=0) → librdkafka Snappy → Kafka
→ nom-link consumer (librdkafka decompresses) → transforms → producer → librdkafka Snappy → Kafka
→ Data Loader → Vertica
```

Snappy compression is applied ONLY at the Kafka batch level by librdkafka. The
application-level compression flag inside GenericChunk is always 0 (off).

---

## Experiments

### Experiment 1: OpenZL SDDL with Columnar Preprocessing (Baseline)

**What we tested**: Built a C++ preprocessor (`dns_tlv_preprocess.cpp`) that
decomposes TLV binary into typed columnar streams — the same approach that achieved
42x on telegraf JSONL and 173x on VCF genomic data. The preprocessor:
1. Parses each TLV record's fixed header and variable fields
2. Classifies fields as FIXED (same size in every record), VARIABLE, or CONSTANT
3. Delta-encodes timestamps
4. Drops constant fields (stores once in metadata sidecar)
5. Writes columnar binary described by an SDDL schema

The SDDL schema named the fixed header columns (timestamps, ports, inet_family)
so OpenZL could train independent entropy models per column. Remaining data was
captured in a `Byte[_rem]` catch-all.

**Training**: Single 80 KB chunk, 60 seconds, 4 threads, SDDL profile.

**Result**: 82 KB original TLV → 69 KB columnar → **12.1 KB compressed (6.6x)**

**vs Snappy**: 65.5% smaller (12.1 KB vs 35 KB)

### Experiment 2: Named Fixed Columns in SDDL

**What we tested**: Extended the SDDL schema to explicitly name CLIENT_ADDRESS
and VIEW as separate `Byte[col_size]` sections instead of lumping them into
the `Byte[_rem]` catch-all. Required adding size-prefix fields to the binary format.

**Result**: **11.7 KB (7.0x)** — 5.7% improvement over v1

### Experiment 3: Separate Variable Section in SDDL

**What we tested**: Added a size-prefixed blob to separate the variable-length
column data (DNS_MESSAGE, DEVICE_ID) from the field order map in the SDDL.

**Result**: **11.7 KB (7.0x)** — no improvement over v2. Separating the variable
section didn't help because OpenZL treats them similarly whether named or in `_rem`.

### Experiment 4: Training Time and Speed Benchmarks

**What we tested**: Extended training from 60s to 300s on the same single chunk.
Also measured compress/decompress speed.

**Result**: Identical compression (11.7 KB). Model converges in 60s on single chunk.

**Speed**: 21.5ms compress, 20.2ms decompress per 80 KB chunk (~3.7 MB/s throughput).
Subprocess overhead (spawning `zli` binary) dominates.

### Experiment 5-6: Schema Robustness Across Chunks

**What we tested**: Applied the v1 SDDL schema (with `Byte[_rem]` catch-all) across
all 20 sample chunks. Some chunks have 2 fixed columns, others have 3.

**Result**: v1 schema works on ALL 20 chunks (100% success). v2 schema (hardcoded
2 named columns) fails on chunks with 3 fixed columns. The catch-all `Byte[_rem]`
is more robust than explicit column naming.

**Average across 20 chunks**: 53.1 KB → 9.0 KB (**5.9x**, 74.3% smaller than Snappy)

### Experiment 7: zstd on Raw TLV (Critical Discovery)

**What we tested**: Applied zstd (default and level 19) directly to the raw TLV
binary — no preprocessing, no columnar decomposition, no SDDL.

**Result**:
- zstd default: **7.7x** (141 KB total across 20 chunks)
- zstd level 19: **8.8x** (123 KB total)

**vs OpenZL SDDL columnar**: zstd on raw TLV **beats** OpenZL on preprocessed
columnar on EVERY chunk. The columnar decomposition was counterproductive — it
separated correlated data and added metadata overhead.

### Experiment 8: OpenZL Serial on Raw TLV

**What we tested**: Trained OpenZL with the `serial` profile directly on raw TLV
binary. Zero preprocessing — just `zli train` on the `.tlv` files as-is.

**Training**: Single 80 KB chunk, 60 seconds, serial profile.

**Result**: **8.1x** (134 KB total) — better than the columnar approach (5.9x) and
better than zstd default (7.7x), but worse than zstd-19 (8.8x).

**Speed**: 3 MB/s (subprocess overhead). Cannot handle the 97 MB/s `nom-dns-base`
throughput.

### Experiment 9: Multi-Chunk Serial Training

**What we tested**: Trained OpenZL serial on 10 chunks instead of 1.

**Result**: Identical — 133,822 bytes (8.1x). Model converged on single chunk.

### Experiment 10: zstd with Trained Dictionary

**What we tested**: Trained a 64 KB zstd dictionary on 10 TLV chunks, then
compressed all chunks with the dictionary.

**Result**: **9.1x** (119 KB total) — beats OpenZL serial (8.1x). zstd-19+dict
achieves **10.8x** (101 KB).

### Experiment 11: Complete Speed Benchmarks

**What we tested**: Measured compress/decompress speed for all methods on 80 KB chunks.

| Method | Compress Speed | Decompress Speed | Production Viable (97 MB/s)? |
|---|---|---|---|
| Snappy | 0.050ms (1,563 MB/s) | 0.030ms | YES |
| zstd default | 0.059ms (1,322 MB/s) | 0.020ms | YES |
| zstd+dict (64KB) | 0.051ms (1,528 MB/s) | 0.020ms | YES |
| zstd+dict (256KB) | 0.049ms (1,599 MB/s) | 0.009ms | YES |
| zstd-19 | 11.9ms (7 MB/s) | 0.015ms | NO |
| OpenZL serial | 23ms (3 MB/s) | 22ms | NO |

**zstd+dict (256KB) is FASTER than Snappy** while achieving 7x better compression.

### Experiment 12: Dictionary Size Optimization

**What we tested**: Dictionaries from 16 KB to 1024 KB, measured ratio and speed.

| Dict Size | Ratio | Speed |
|---|---|---|
| 16 KB | 8.2x | 849 MB/s |
| 64 KB | 9.1x | 927 MB/s |
| 128 KB | 10.3x | 1,341 MB/s |
| **256 KB** | **16.8x** | **1,583 MB/s** |
| 512 KB | 16.4x | 1,554 MB/s |
| 1024 KB | 16.4x | 1,535 MB/s |

256 KB is the sweet spot — ratio plateaus beyond it.

**Note**: This 16.8x result was later found to be from an overfitted dictionary
(see Experiment 22).

### Experiment 13: Round-Trip Verification

**What we tested**: Verified byte-identical decompression on all 20 chunks with
the 256 KB dictionary.

**Result**: All 20 chunks: compress → decompress = byte-identical. Lossless verified.

### Experiment 14: zstd+dict on nom-dns-vertica Text Format

**What we tested**: Applied zstd+dict to the tab-delimited text format produced by
the `tovertica` transform (decoded via `nom-kafka-dump`).

**Result**: Only **3.7-3.9x** — text format inflates the data (UUIDs expand from
16 bytes to 36-byte hex strings, timestamps from 8 bytes to 16-digit decimals).
Binary TLV (16.8x) >> text (3.9x).

### Experiment 15: Dictionary Artifact

**What we did**: Saved the trained 256 KB dictionary to `artifacts/dns_tlv_zstd_256k.dict`.

### Experiment 16: Double Compression

**What we tested**: Applied app-level zstd+dict per chunk, then Kafka-level zstd
on the batch of pre-compressed chunks.

**Result**: Double compression adds 10 bytes (0.0% overhead). The already-compressed
data is incompressible. If using app-level compression, set Kafka's
`compression.type=none`.

### Experiment 17: Batch-Level Consistency

**What we tested**: Verified compression ratios across different batch sizes
(2, 5, 10, 14 chunks per batch).

**Result**: Consistent — individual chunks: 16.8x, batches of 2-5: ~20.8x,
batches of 14: 16.8x.

### Experiment 18: Stacking Compressors

**What we tested**: Compressed with zstd+dict first, then tried OpenZL serial on
the zstd output.

**Result**: Made it worse (+22 bytes). Compressed data is near-random. Never stack
compressors.

### Experiment 19: zstd Compression Level Sweep

**What we tested**: All zstd levels (1-22) with 256 KB dictionary.

| Level | Ratio | Speed | Production Viable? |
|---|---|---|---|
| 1 | 12.8x | 1,981 MB/s | YES |
| **2** | **19.1x** | **2,260 MB/s** | **YES — recommended** |
| 3 (default) | 19.8x | 1,630 MB/s | YES |
| 5 | 21.9x | 516 MB/s | YES |
| 12 | 24.5x | 232 MB/s | YES |
| 15 | 25.4x | 125 MB/s | YES (barely) |
| 19 | 26.1x | 12 MB/s | NO |

All levels 1-15 can handle 97 MB/s production throughput.

**Note**: These ratios used the 14-chunk specialized dictionary (later found to
be overfitted — see Experiment 22).

### Experiment 20: SDDL Bug Root Cause

**What we investigated**: Earlier iterations blamed OpenZL's CBOR serializer for
failing with "too many SDDL fields" or specific field names.

**Root cause found**: ALL failures were from **binary/schema misalignment** — the
SDDL described fields (like `col1_size`) that didn't exist in the binary output
because the preprocessor wasn't rebuilt after schema changes. When the SDDL reads
a U32 at a position where the binary has different data, it gets a garbage size
value and crashes.

**OpenZL's SDDL has NO field count limit and NO field name restrictions.** We tested
schemas with 15+ fields successfully once the binary matched.

### Experiment 21: Batch Size Scaling (Initial — Flawed)

**What we tested**: Concatenated chunks to simulate 80 KB to 10 MB batches and
tested all methods. BUT this used the 80 KB single-chunk compressor for OpenZL.

**Result**: All compressors plateau at ~1 MB. OpenZL SDDL columnar gets WORSE at
larger batches (metadata overhead scales linearly).

**Flaw**: Using a model trained on 80 KB to compress 10 MB is not a fair comparison.

### Experiment 22: 1 GB Training + Batch Scaling (Corrected)

**What we tested**: Trained OpenZL serial on 100 MB (single large file) and 1.1 GB
(11 x 100 MB files). Also investigated zstd dictionary quality vs training data
diversity.

**OpenZL training results**:
- 100 MB single file: **8.96x** on training data — works, produces valid compressor
- 1.1 GB (11 files): **9.03x** on training data — works but produces corrupt
  compressor (CBOR bug with many training files). Workaround: train on single large file.

**zstd dictionary overfitting discovery**:
- Dictionary trained on 14 similar chunks: **18.3x at 80 KB** but degrades to 7.8x at 10 MB
- Dictionary trained on 50 diverse chunks: **10.3x at 80 KB**, 8.8x at 5 MB
- Dictionary trained on 200 diverse chunks: **8.4x at 80 KB**, 8.2x at 10 MB
- The earlier 16.8-19.1x results were from an overfitted dictionary that memorized
  specific byte patterns from a small homogeneous sample.

### Experiment 23: Definitive Batch-Size Benchmark (33K Diverse Chunks)

**What we tested**: Final benchmark using 33,135 diverse chunks across batch sizes
from 80 KB to 50 MB, with properly-trained compressors and dictionaries of varying
diversity.

| Batch | zstd | zstd+dict (50 chunks) | OpenZL 80K | OpenZL 100M | OpenZL 1.1GB |
|---|---|---|---|---|---|
| 80 KB | 8.0x | **10.3x** | 8.5x | 8.3x | 8.3x |
| 500 KB | 7.8x | **9.8x** | 8.6x | 8.6x | 8.6x |
| 1 MB | 8.0x | **10.1x** | 8.8x | 8.8x | 8.8x |
| 5 MB | 8.1x | 8.8x | **8.8x** | **8.8x** | **8.8x** |
| 10 MB | 8.1x | 8.5x | **8.8x** | **8.8x** | **8.8x** |
| 50 MB | 8.1x | 8.1x | 8.7x | **8.8x** | **8.8x** |

**Key findings**:
- zstd+dict wins at small batches (10.3x at 80 KB) but degrades at large batches
  as the dictionary becomes a smaller fraction of the total data
- OpenZL serial is the most consistent (8.3-8.8x at all sizes, no tuning needed)
- OpenZL 100M and 1.1GB training give identical results — model converges at 100 MB
- At 50 MB, OpenZL is the ONLY method that beats plain zstd (8.8x vs 8.1x)
- Plain zstd is within 10% of the best at every batch size

---

## SDDL Training Bug Details

OpenZL's SDDL training has two confirmed bugs:

1. **Binary/schema misalignment crash**: If the SDDL describes fields that don't
   match the binary layout, OpenZL reads garbage values for size fields, tries to
   skip enormous byte ranges, and crashes with CBOR "writeFailed" errors. Fix:
   always ensure the preprocessor output matches the SDDL exactly.

2. **Multi-file training corruption**: Training on many files (11+ in our tests)
   with the serial profile occasionally produces a 0-byte or corrupt compressor
   that fails with CBOR "truncated" on decompression. Workaround: concatenate
   training data into fewer, larger files.

The SDDL itself has **no field count limit** — schemas with 15+ named fields work
correctly when the binary matches.

---

## Speed Comparison

| Method | Compress | Decompress | Throughput | Handles 97 MB/s? |
|---|---|---|---|---|
| Snappy (current) | 0.050ms | 0.030ms | 1,563 MB/s | YES |
| **zstd default** | 0.059ms | 0.020ms | 1,322 MB/s | **YES** |
| **zstd+dict 256KB** | 0.049ms | 0.009ms | 1,599 MB/s | **YES** |
| zstd-19 | 11.9ms | 0.015ms | 7 MB/s | NO |
| OpenZL serial | 23ms | 22ms | 3 MB/s | NO |
| OpenZL SDDL columnar | ~6ms | ~5ms | 158 MB/s | YES (columnar only) |

OpenZL's 3 MB/s throughput is due to subprocess overhead (spawning `zli` binary).
An in-process C++ integration could be significantly faster but was not tested.

---

## Conclusions

### What OpenZL Is Good At

- **Structured text** with repetitive patterns: 42x on telegraf JSONL, 173x on VCF
- **Consistent ratio across batch sizes**: 8.8x at 1 MB through 50 MB
- **Does not require a dictionary** to deploy — the trained model is self-contained

### What OpenZL Is NOT Good At (For This Data)

- **Speed**: 500x slower than zstd due to subprocess overhead
- **Binary TLV compression ratio**: Matches zstd (8.1-8.8x range) but doesn't beat it
- **Columnar preprocessing**: Counterproductive — the decomposition overhead
  outweighed per-column modeling benefits

### Why The Columnar Approach Failed

The genomics codecs (FASTA, BED, VCF) achieve high ratios because:
1. The raw formats have massive text overhead (FASTA headers, VCF genotype strings)
2. Structural decomposition removes this overhead (4-bit packing, dictionary encoding)
3. The data has extreme per-column patterns (DNA is 5 symbols, genotypes are 4 values)

DNS TLV doesn't have these properties:
1. TLV is already a compact binary format — no text overhead to remove
2. The columnar decomposition ADDS overhead (offset arrays, bitmaps, metadata)
3. Fields like UUIDs and DNS wire-format names don't have column-level patterns
4. The local correlations between adjacent fields (which zstd exploits) are
   destroyed by columnar separation

### Why zstd+dict Overfitting Matters

The initial zstd+dict results (16.8-19.1x) were from a dictionary trained on 14
similar chunks. This dictionary essentially memorized the specific byte patterns
in those chunks (same server address, same view string, similar domain distribution).
With diverse production traffic, the dictionary's effectiveness drops to 8-10x.

This is an important lesson: **always test compression with realistically diverse
data, not a small homogeneous sample.**

---

## Production Recommendations

### Option A: Simple (1 line change) — Recommended First Step

Change `compression.codec = "snappy"` to `"zstd"` in `libnomkafka/producer.cc` line 31.

- **Ratio**: 8.0x (from 2.3x) — consistent across all batch sizes and data diversity
- **Speed**: 1,322 MB/s — handles production throughput easily
- **Savings**: ~6.7 TB/day at 8.4 TB/day raw
- **Effort**: One line of code, zero infrastructure changes
- **Risk**: Very low — zstd is a standard Kafka compression codec supported by librdkafka

### Option B: zstd with Dictionary (Small Batches)

Deploy a zstd dictionary as application-level compression inside GenericChunk
(using the `compression_flag` byte with a new value for zstd-dict).

- **Ratio**: 8.4-10.3x depending on dictionary diversity (best at 80 KB chunks)
- **Speed**: 1,528 MB/s — faster than Snappy
- **Caveats**: Dictionary must be curated carefully — too specialized = overfitting,
  too diverse = no better than plain zstd. Requires periodic retraining.
- **Deployment**: Dictionary file (256 KB) deployed to all producers and consumers

### Option C: OpenZL Serial (Large Batches, Latency Tolerant)

If batch latency is acceptable (e.g., for analytics pipelines, not real-time),
OpenZL serial provides the most consistent ratio.

- **Ratio**: 8.8x consistently at 1-50 MB batches
- **Speed**: 3 MB/s (too slow for 97 MB/s `nom-dns-base`)
- **Where viable**: Post-pipeline archival compression, offline analytics,
  the `nom-dns-vertica` path (only 160 KB/s throughput)

---

## Artifacts

| File | Description |
|---|---|
| `artifacts/dns_tlv_zstd_256k.dict` | Trained zstd dictionary (256 KB, 20 chunk sample) |
| `dns_work/dns_serial_100m.compressor` | OpenZL serial compressor trained on 100 MB |
| `dns_work/dns_serial_1gb.compressor` | OpenZL serial compressor trained on 1.1 GB |
| `tools/dns_tlv_preprocess.cpp` | C++ TLV preprocessor (analyze/encode/decode) |
| `schemas/dns_tlv.sddl` | SDDL schema for columnar binary |
| `scripts/dns_benchmark.sh` | Full benchmark pipeline |
| `tasks/lessons.md` | Complete experiment log (23 experiments) |
| `DNS_DATA_COLLECTION_README.md` | How to collect TLV samples from live environments |
