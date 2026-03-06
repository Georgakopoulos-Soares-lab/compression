# Parquet + OpenZL: Query Architecture Options

**Date**: Feb 20, 2026
**Status**: Design discussion — no implementation yet

---

## Background

We are replacing zstd with OpenZL as the compression codec inside Apache Parquet files. Our per-column benchmarks show 20–45% size reductions on numeric data, 6–41% on structured string IDs, and 26–63% on high-cardinality free-form strings versus Parquet+zstd.

The compression results are proven. The open design question is: **what is the unit of compression within a column chunk, and how does that affect query performance?**

This document describes two approaches, their trade-offs, and a mitigation strategy that preserves the best of both.

---

## Parquet File Structure (Prerequisite)

A Parquet file is organized as follows:

```
┌──────────────────────────────────────────────────────────────┐
│ Row Group 0                                                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐         │
│  │ Column Chunk  │ │ Column Chunk  │ │ Column Chunk  │  ...   │
│  │ (user_id)     │ │ (email)       │ │ (score)       │        │
│  └──────────────┘ └──────────────┘ └──────────────┘         │
├──────────────────────────────────────────────────────────────┤
│ Row Group 1                                                  │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐         │
│  │ Column Chunk  │ │ Column Chunk  │ │ Column Chunk  │  ...   │
│  │ (user_id)     │ │ (email)       │ │ (score)       │        │
│  └──────────────┘ └──────────────┘ └──────────────┘         │
├──────────────────────────────────────────────────────────────┤
│ Footer (metadata)                                            │
│  - Schema                                                    │
│  - Per-row-group, per-column: byte offset, size, min, max    │
│  - Key-value metadata                                        │
└──────────────────────────────────────────────────────────────┘
```

**Row group**: A horizontal partition of the table (typically 500K–1M rows). The footer stores per-column statistics (min, max, null count) for each row group, enabling row group skipping: if a query's predicate cannot match any value in a row group's range, the entire row group is never read from disk.

**Column chunk**: Within a row group, each column's values are stored contiguously as a single block. This enables column pruning: if a query only references columns A and C, the reader seeks past column B entirely.

**Footer**: A small metadata section at the end of the file. Contains the schema, the byte offset and compressed size of every column chunk, and per-column statistics. A query engine reads the footer first, then seeks directly to the column chunks it needs.

In standard Parquet, each column chunk is compressed as one unit (one zstd frame, one snappy frame, etc.). To read any value from a column chunk, the entire chunk must be decompressed.

---

## Option A: One `.zl` Frame per Column Chunk

### What it is

A direct codec replacement. Everywhere Parquet currently writes one zstd-compressed column chunk, we write one OpenZL-compressed `.zl` frame instead. The Parquet container format — row groups, footer, byte offsets, statistics — remains completely unchanged.

```
Column chunk (1M user_id values, INT64):

Standard Parquet:
┌─────────────────────────────────────────────┐
│           One zstd frame (3.2 MB)           │
└─────────────────────────────────────────────┘

Option A:
┌─────────────────────────────────────────────┐
│           One .zl frame (2.1 MB)            │
└─────────────────────────────────────────────┘
```

To read any row from this column chunk, the reader decompresses the entire `.zl` frame.

### Benefits

1. **Simplest integration path.** The only change is the codec ID in the Parquet metadata (from `ZSTD` to a custom codec ID for OpenZL). Row groups, column chunk offsets, statistics, schema — all untouched.

2. **Best compression ratios.** The compressor sees the entire column chunk as one contiguous input. More data means more context for pattern detection, better entropy modeling, and longer-range redundancy elimination. Our benchmarks (20–45% better than zstd on numerics) are measured at this granularity.

3. **Column pruning works unchanged.** Each column chunk is independent. A query that doesn't reference a column never decompresses it.

4. **Row group skipping works unchanged.** Statistics remain in the footer. Predicate pushdown operates exactly as it does today.

5. **Compatible with standard Parquet readers** (with a codec plugin). A reader that registers an OpenZL decompression codec can read these files without any other changes.

### Disadvantages

1. **No sub-chunk random access.** To read row 734,218 from a column chunk of 1M rows, the entire column chunk (e.g., 2.1 MB compressed → 8 MB uncompressed) must be decompressed. For point lookups or narrow range queries, this is wasteful.

2. **Decompression granularity matches row group size.** For a row group of 1M rows, the minimum decompression unit per column is the full 1M values. On a query that matches only 100 rows, we still decompress all 1M.

3. **Memory pressure for large row groups.** Decompressing a full column chunk materializes the entire uncompressed array in memory. For wide tables or large row groups, this can consume significant RAM even when the query only needs a small slice.

### Best suited for

Analytical workloads: full-table scans, aggregations, GROUP BY, joins on large ranges, ML feature extraction. These queries touch most rows in a column chunk anyway, so decompressing the whole chunk has no marginal cost.

---

## Option B: Multiple `.zl` Frames per Column Chunk (Paged Compression)

### What it is

Instead of compressing a column chunk as a single frame, we split it into **pages** of fixed row count (e.g., 10,000 rows per page). Each page is independently compressed as its own `.zl` frame. A lightweight **page index** — an array of byte offsets, one per page — is stored alongside the column chunk metadata so that a reader can seek directly to any page.

```
Column chunk (1M user_id values, INT64, 100 pages of 10K rows):

┌─────────┬─────────┬─────────┬─────┬──────────┐
│ Page 0  │ Page 1  │ Page 2  │ ... │ Page 99  │
│ rows    │ rows    │ rows    │     │ rows     │
│ 0–9,999 │ 10K–20K │ 20K–30K │     │ 990K–1M  │
│ (.zl)   │ (.zl)   │ (.zl)   │     │ (.zl)    │
└─────────┴─────────┴─────────┴─────┴──────────┘

Page index (stored in metadata):
  [0, 21340, 42190, 63401, ..., 2078112]   ← byte offset of each page
```

To read row 734,218:
1. Page number = 734,218 / 10,000 = **page 73**
2. Look up byte offsets: page 73 starts at offset[73], ends at offset[74]
3. Seek to that position within the column chunk, read those bytes
4. Decompress that single `.zl` frame (~21 KB → ~80 KB)
5. Row 734,218 is at index 734,218 % 10,000 = **position 4,218** within the decompressed page

### Benefits

1. **Fine-grained random access.** Point lookups decompress only one page (e.g., 80 KB) instead of the entire column chunk (8 MB). For a page size of 10K rows, this is a ~100x reduction in decompressed bytes.

2. **Lower memory pressure.** Only the pages actually needed are materialized in memory. A query touching 50 rows across 3 pages decompresses ~240 KB instead of 8 MB per column.

3. **Page-level statistics (optional extension).** Each page can store min/max values in the page index. This enables a third level of predicate pushdown: after selecting row groups (Level 1) and columns (Level 2), the reader can skip individual pages whose value ranges don't match the predicate (Level 3). For a query like `WHERE score > 95`, most pages might be skippable.

4. **Parallelizable decompression.** Independent pages can be decompressed in parallel on separate threads or cores.

5. **Column pruning and row group skipping still work.** These are orthogonal to page-level splitting.

### Disadvantages

1. **Compression ratio impact.** Each page is compressed independently, so the compressor has less data to work with per frame. Cross-page redundancy (e.g., if row 9,999 and row 10,000 share a pattern) cannot be exploited. See the mitigation strategy below.

2. **More metadata overhead.** The page index adds storage. For 100 pages, the index is ~800 bytes (100 x 8-byte offsets). For 10,000 pages, it's ~80 KB. This is negligible relative to the data but adds implementation complexity.

3. **More complex writer and reader.** The writer must split data into pages, compress each independently, build the page index, and store it in metadata. The reader must parse the page index and compute page boundaries. This is more engineering than Option A.

4. **Not compatible with standard Parquet readers.** Standard readers expect one compressed blob per column chunk. A paged layout requires a custom reader that understands the page index format.

5. **Page size tuning.** The optimal page size depends on the workload. Too small (100 rows) = excessive overhead and poor compression. Too large (500K rows) = loses the random-access benefit. The right value depends on the access pattern and must be chosen or made configurable.

### Best suited for

Mixed or interactive workloads: point lookups, narrow range queries, dashboard filters, serving individual records, any scenario where queries touch a small fraction (<10%) of rows in a row group.

---

## Mitigating the Compression Ratio Impact of Option B

The primary disadvantage of Option B is the potential loss in compression ratio from smaller compression units. This can be substantially mitigated by **separating training from compression**:

**Step 1 — Train on the full column.** Run `zli train` on the entire column (or a large representative sample). This produces a `.zlc` compressor artifact that encodes the learned data patterns: value distributions, correlation structures, delta characteristics, entropy models. The model sees the full context of the column.

**Step 2 — Compress each page with the trained model.** Run `zli compress --compressor model.zlc` on each 10K-row page independently. The trained model is applied to each page, producing small independent `.zl` frames.

```
Full column (1M values)
        │
        ▼
   zli train ──────► model.zlc (trained compressor)
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
     Page 0 (10K)    Page 1 (10K)   ... Page 99 (10K)
          │               │               │
          ▼               ▼               ▼
   zli compress     zli compress    zli compress
   --compressor     --compressor    --compressor
   model.zlc        model.zlc       model.zlc
          │               │               │
          ▼               ▼               ▼
     page_0.zl       page_1.zl      page_99.zl
```

**Why this works:** OpenZL's training and compression are decoupled by design. The `.zlc` model captures data-type-specific patterns (e.g., "this column is monotonically increasing INT64 with small deltas" or "this column has 5 unique values distributed with frequencies 40/25/20/10/5%"). These patterns hold equally well for each page because pages from the same column share the same distribution. The only compression loss is cross-page redundancy in the raw data itself (values near page boundaries that could be deduplicated across pages), which is typically negligible for columnar data.

This is the same pattern used in the Nyx FASTA compression pipeline: train on a 200 MiB sample, then compress hundreds of independent chunks with the same model. Benchmark results confirm near-zero compression loss versus single-chunk compression.

**Storage of the trained model:** The `.zlc` model (typically a few KB to a few hundred KB) is stored once per column chunk in the Parquet key-value metadata or as a sidecar. It is needed only for compression, not for decompression — each `.zl` frame is independently decompressible by OpenZL's universal decompressor.

---

## Comparison Summary

| Property | Option A (single frame) | Option B (paged) |
|---|---|---|
| Compression unit | Entire column chunk | Fixed-size page (e.g., 10K rows) |
| Compression ratio | Best (full context) | Near-best (with full-column training) |
| Point lookup cost | Decompress full column chunk | Decompress one page |
| Range query cost | Decompress full column chunk | Decompress only pages in range |
| Full scan cost | Decompress full column chunk | Decompress all pages (equivalent) |
| Page-level predicate pushdown | Not possible | Possible (with page-level statistics) |
| Implementation complexity | Low (codec swap) | Medium (page index, custom reader) |
| Standard Parquet reader compat | Yes (with codec plugin) | No (requires custom reader) |
| Metadata overhead | None beyond codec ID | Page index (~8 bytes per page) |
| Memory pressure | Full column chunk in RAM | One page in RAM per column |

---

## Recommendation

**Start with Option A.** It is the simplest path to production, preserves full Parquet compatibility, and is where all our compression benchmarks have been validated. For the analytical workloads that Parquet is primarily designed for, full-column-chunk decompression is acceptable and often optimal.

**Design the metadata format to accommodate Option B from the start.** An Option A file is a special case of Option B where each column chunk contains exactly 1 page. If the metadata schema supports an optional page index, Option B can be added later without breaking compatibility with Option A files.

**Build Option B when a concrete workload demands it.** If we encounter or target use cases with point-lookup or narrow-range access patterns, page-level splitting with full-column training can be added as an incremental improvement on top of the existing Option A infrastructure.
