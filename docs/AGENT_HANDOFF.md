# Agent Handoff: Parquet Compression Experiments

**Date**: Feb 20, 2026
**Purpose**: Transfer context from the outgoing agent to a new agent continuing the Parquet compression work.

---

## Persona

You are a principal engineer specializing in data compression, columnar storage formats (Apache Parquet), and the OpenZL compression framework. You communicate directly, give honest assessments of what works and what doesn't, and always justify technical decisions with empirical data. You run experiments, show numbers, and don't hand-wave. When the user asks a question, you answer it precisely, with benchmarks when relevant. You have deep knowledge of OpenZL's CLI (`zli`), its SDDL schema language, its LE profiles (`le-i16`, `le-i32`, `le-i64`), its `serial` profile, and its inline training (`--train-inline`). You understand Parquet internals (row groups, column chunks, pages, dictionary encoding, RLE, codec IDs, key-value metadata).

---

## What We Are Building

We are building a system where OpenZL replaces zstd as the compression layer inside Apache Parquet files. The goal: produce Parquet files that are **queryable** (column pruning, predicate pushdown still work) but compressed 20-60% better than Parquet+zstd.

The approach: keep the Parquet container format intact (metadata, row groups, schema), but replace the compression codec on each column chunk with OpenZL. Each column is compressed independently so a reader can decompress only the columns a query needs.

---

## What Has Been Proven (With Numbers)

### Numeric columns: OpenZL wins by 20-45%

Three datasets tested (numeric_heavy: 3M rows/7 cols, ml_features: 500K rows/51 cols, mixed_type: 2M rows/9 cols). Per-column independent compression with OpenZL LE profiles vs Parquet+zstd(max):

| Dataset | Per-Column OpenZL | Parquet+zstd(max) | Savings |
|---|---|---|---|
| numeric_heavy | 33.75 MiB | 50.58 MiB | **33.3%** |
| ml_features | 32.63 MiB | 58.85 MiB | **44.6%** |
| mixed_type | 23.19 MiB | 28.90 MiB | **19.7%** |

Per-column compression is actually 2-6% BETTER than whole-file compression because each column gets a model tailored to its data distribution. This validates queryability.

### Structured string IDs (UUIDs, IPs, hashes): OpenZL wins by 6-41%

Binary conversion (text → compact binary) + LE profile compression:

| Column Type | OpenZL | Parquet+zstd | Savings |
|---|---|---|---|
| Random UUIDs | 15.02 MiB | 18.94 MiB | **20.7%** |
| Time-ordered UUIDs | 8.82 MiB | 13.59 MiB | **35.0%** |
| IPv4 addresses | 3.81 MiB | 6.51 MiB | **41.4%** |
| SHA256 hashes | 30.52 MiB | 32.51 MiB | **6.1%** |
| Sequential INT64 | 45 bytes | 1.22 MiB | **~100%** |
| Low-cardinality (5 unique) | 0.23 MiB | 0.31 MiB | **26.4%** |

### High-cardinality free-form strings: OpenZL wins by 26-63%

This was the hardest problem. We tested 5 encoding strategies. The breakthrough results (just obtained):

**Email column** (632K unique, avg 24 chars, Parquet+zstd = 5.82 MiB):

| Encoding | Size | vs Parquet+zstd |
|---|---|---|
| dict_current (old, raw dict blob) | 19.18 MiB | -230% (terrible) |
| dict_compressed (dict blob compressed with zstd) | 4.30 MiB | **+26% better** |
| chunked_serial (1MB chunks) | 4.54 MiB | +22% better |
| structural (split user@domain) | **2.42 MiB** | **+58.5% better** |

**URL path column** (330K unique, avg 26 chars, Parquet+zstd = 5.94 MiB):

| Encoding | Size | vs Parquet+zstd |
|---|---|---|
| dict_current (old, raw dict blob) | 11.60 MiB | -95% (terrible) |
| dict_compressed (dict blob compressed with zstd) | 2.68 MiB | **+55% better** |
| chunked_serial (1MB chunks) | 3.90 MiB | +34% better |
| structural (split resource/id/action) | **2.20 MiB** | **+63% better** |

---

## Key Technical Decisions Made

1. **Per-column independent compression** — each column is a separate `.zl` frame. Validated that this loses nothing (actually 2-6% better than whole-file).

2. **Profile selection by Arrow type** — INT64/DOUBLE/TIMESTAMP → `le-i64`, INT32/FLOAT → `le-i32`, INT16 → `le-i16`, BOOL → `serial`. Automatic via `_arrow_type_to_profile()`.

3. **Auto-detect for strings** — samples 200 values, checks for binary-convertible patterns (UUID, IPv4, IPv6, hex), falls back to dictionary. Never returns `raw` for strings (serial profile times out on >5 MiB). Validated 100% accuracy on 8-column test.

4. **Binary conversion** — text to compact binary for structured IDs. UUID (36 chars → 16 bytes), IPv4 (text → 4 bytes), hex hashes (64 chars → 32 bytes). Then compress with LE profile.

5. **Dictionary encoding** — the dictionary blob MUST be compressed (with zstd or OpenZL). Without compressing the dict blob, results are 2-3x worse than Parquet+zstd. With compression, 26-55% better.

6. **Structural decomposition** — the best approach for semi-structured strings. Split emails on `@`, URLs on `/`, compress each semantic part independently. 58-63% better than Parquet+zstd.

7. **Serial profile limitation** — `--train-inline` with serial profile times out on files >5 MiB. This ruled out raw string compression and fixed-width padding at 32 MiB scale.

---

## Where We Left Off / What Comes Next

The user's last strategic direction was: **replicate Parquet's encoding pipeline exactly, but replace the zstd compression step with OpenZL inline training and compression.** We validated this works spectacularly for dictionary encoding (+26% to +55%) and structural decomposition (+58% to +63%).

### Final validated universal approach (ALL-OPENZL, no zstd):

| Column | Parquet+zstd | All-OpenZL | Savings |
|---|---|---|---|
| email (632K unique) | 5.82 MiB | 3.68 MiB | **+36.7%** |
| url_path (330K unique) | 5.94 MiB | 2.52 MiB | **+57.6%** |

Pipeline: dictionary encode → OpenZL le-i32 on indices + OpenZL serial (1 MiB chunks) on dictionary blob. No zstd, no structural parsing, works for any string column. Results in `results/universal_openzl_dict.json`.

### Immediate next steps the user will likely want:

1. **Implement the unified approach the user described**: Instead of compressing indices with OpenZL and dictionary blob with zstd separately, concatenate dict+indices into one blob and run `zli compress --profile serial --train-inline` on the whole thing. The user explicitly asked for this — the entire pipeline should be OpenZL end-to-end.

2. **Integrate dict_compressed and structural into column_encoder.py**: The winning encodings were tested in a standalone script (`benchmark_string_encodings.py`). They need to be added to the main `ColumnEncoder` class and auto-detect logic.

3. **Update auto-detect**: When auto-detect detects email-like or URL-like structure, it should pick `structural`. Otherwise, for generic high-cardinality strings, pick `dict_compressed`.

4. **Re-run the full benchmark** across all 4 datasets with the updated encoder to produce a single consolidated results table.

5. **Update documentation** — `docs/PARQUET_EXPERIMENTS.md` needs the string encoding experiment results and the new encoding strategies.

6. **Design the actual Parquet file format integration**: How to embed per-column `.zl` frames inside a Parquet container with a custom codec ID, models in key-value metadata, and a reader plugin for decompression.

---

## Critical Files

| File | Purpose |
|---|---|
| `parquet_experiments/column_encoder.py` | Main encoding pipeline (ColumnEncoder class, 5 strategies, auto-detect) |
| `parquet_experiments/benchmark_encodings.py` | Benchmark all encodings with auto-detect validation |
| `parquet_experiments/benchmark_string_encodings.py` | Focused string experiment (dict_compressed, structural, chunked_serial) |
| `parquet_experiments/generate_samples.py` | Creates 4 datasets (numeric_heavy, string_heavy, mixed_type, ml_features) |
| `parquet_experiments/generate_highcard_samples.py` | Creates highcard_strings dataset (UUIDs, IPs, emails, URLs, SHA256) |
| `parquet_experiments/benchmark_openzl.py` | Original baseline benchmark (whole-file OpenZL vs Parquet) |
| `parquet_experiments/extract_for_openzl.py` | Extracts columns to PBL format + generates SDDL schemas |
| `parquet_experiments/results/encoding_benchmark.json` | Per-column encoding benchmark results |
| `parquet_experiments/results/string_encoding_experiment.json` | String encoding experiment results (the breakthrough numbers) |
| `parquet_experiments/results/comparison_comprehensive.json` | Whole-file baseline comparison results |
| `parquet_experiments/per_column_experiment/per_column_results.json` | Phase A per-column compression results |
| `docs/PARQUET_EXPERIMENTS.md` | Documentation of all experiments (needs update with latest results) |

---

## Errors and Gotchas to Avoid

1. **OpenZL Parquet profile requires canonical Parquet**: `compression='NONE'`, `use_dictionary=False`, `write_statistics=False`. Otherwise `zli` errors with "Found compressed chunk!".

2. **Serial profile times out on >5 MiB with `--train-inline`**: Never use serial for large string blobs. Use dictionary encoding first, or chunk into <1 MiB pieces.

3. **Fixed-width padding at 32 MiB times out**: The le-i64 profile with `--train-inline` on 32 MiB (1M * 32 bytes) exceeds 300s timeout. Would need longer timeout or smaller data.

4. **Dictionary blob must be compressed**: Without compressing it, dictionary encoding is 2-3x worse than Parquet+zstd for any column with >10K unique values.

5. **uuid_v7_like generator had a bug**: `rand_hex[7:19]` only yielded 9 chars instead of 12, producing invalid UUIDs. Fixed by generating a 32-char hex string from two int64 values.

6. **pip install needs `--trusted-host` flags**: SSL cert issues on this machine. Use `pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org`.

7. **PyArrow stderr warnings are harmless**: `IOError: sysctlbyname failed for 'hw.optional.neon'` appears due to sandbox restrictions. Ignore.

8. **Python output buffering**: When running scripts in background, `print()` output may not appear until the script finishes. Use `flush=True` or `PYTHONUNBUFFERED=1`.

---

## How to Run Things

```bash
# Activate the Python environment
cd /path/to/compression
source nyx/.venv/bin/activate

# Find the zli binary
ZLI=$(ls -t nyx/openzl/cachedObjs/*/zli | head -1)

# Generate datasets
python parquet_experiments/generate_samples.py
python parquet_experiments/generate_highcard_samples.py

# Run the encoding benchmark (all datasets, ~2 hours)
python parquet_experiments/benchmark_encodings.py

# Run the string encoding experiment (email + url_path, ~30 min)
python parquet_experiments/benchmark_string_encodings.py

# Run a single dataset
python parquet_experiments/benchmark_encodings.py --dataset highcard_strings --timeout 300
```
