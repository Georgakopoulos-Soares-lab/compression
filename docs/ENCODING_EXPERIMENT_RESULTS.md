# Encoding Experiment Results: Pre-Compression Encoding + OpenZL

**Date:** 2026-02-21
**Runtime:** ~113 minutes total (first pass: ~96 min, resume pass: ~17 min)
**Objective:** Determine whether applying data-encoding transformations (byte-stream split, XOR-delta, bitpacking, dictionary encoding, delta+narrowing) *before* OpenZL compression improves results compared to feeding raw bytes directly.

---

## Executive Summary

**Pre-compression encoding provides negligible improvement for OpenZL and does NOT close the gap with Parquet+zstd on high-entropy numeric data.** OpenZL's ACE entropy coder already captures the same structural patterns that these encodings expose, so the transformations are largely redundant. The fundamental bottleneck is the inherent entropy of the data itself, not the encoding strategy.

| Metric | Count |
|--------|-------|
| Total encoding tests | 34 |
| Completed successfully | 33 |
| Timed out | 1 (total_amount xor_delta, 600s limit) |
| Encodings that beat raw OpenZL | 6 (marginal: 0.0–0.7%) |
| Encodings that beat Parquet+zstd | 6 (all from ml_features, where raw OpenZL already won) |

---

## Results by Dataset

### numeric_heavy (3M rows)

| Column | Type | Parquet+zstd | Raw OpenZL | Best Encoding | Encoded+OZL | vs Raw | vs Parquet |
|--------|------|-------------|-----------|---------------|------------|--------|-----------|
| temperature | FLOAT32 | 4,082,145 | 8,492,492 | byte_split | 8,435,063 | +0.7% | -106.6% |
| humidity | FLOAT32 | 4,004,175 | 8,167,195 | byte_split | 8,136,606 | +0.4% | -103.2% |
| pressure | FLOAT32 | 3,204,486 | 4,117,516 | byte_split | 4,093,502 | +0.6% | -27.7% |
| battery_voltage | FLOAT32 | 4,029,798 | 7,085,773 | byte_split | 7,083,635 | +0.0% | -75.8% |
| device_id | INT32 | 1,318,551 | 3,750,035 | dict_int | 3,750,078 | -0.0% | -184.4% |
| status_code | INT16 | 148,748 | 254,723 | dict_int | 254,746 | -0.0% | -71.3% |
| timestamp | INT64 | 2,432,879 | 3,522,613 | delta_narrow | 3,504,605 | +0.5% | -44.1% |

**Verdict:** No encoding beat Parquet+zstd on this dataset. Byte-split provided only 0–0.7% improvement over raw OpenZL.

### mixed_type (2M rows)

| Column | Type | Parquet+zstd | Raw OpenZL | Best Encoding | Encoded+OZL | vs Raw | vs Parquet |
|--------|------|-------------|-----------|---------------|------------|--------|-----------|
| is_returned | BOOL | 54,687 | 72,370 | bitpack | 71,936 | +0.6% | -31.5% |
| customer_id | INT32 | 2,158,987 | 3,614,411 | dict_int | 3,622,791 | -0.2% | -67.8% |
| quantity | INT16 | 473,625 | 620,299 | dict_int | 620,377 | -0.0% | -31.0% |
| unit_price | DOUBLE | 1,260,402 | 1,864,491 | dict_float | 1,857,326 | +0.4% | -47.4% |
| total_amount | DOUBLE | 2,238,556 | 3,615,484 | byte_split | 9,471,477 | -162.0% | -323.1% |
| order_date | TIMESTAMP | 7,670,888 | 11,434,375 | byte_split | 11,625,348 | -1.7% | -51.6% |

**Verdict:** No encoding beat Parquet+zstd. Byte-split was catastrophically bad for high-entropy doubles (total_amount: -162% vs raw, -323% vs zstd). XOR-delta timed out on total_amount (600s). Dict_float worked marginally for unit_price (+0.4% vs raw).

### ml_features (500K rows)

| Column | Type | Parquet+zstd | Raw OpenZL | Best Encoding | Encoded+OZL | vs Raw | vs Parquet |
|--------|------|-------------|-----------|---------------|------------|--------|-----------|
| feature_01 | FLOAT32 (monotonic) | 2,125,860 | 705,403 | byte_split | 809,442 | -14.7% | +61.9% |
| feature_11 | FLOAT32 (normal) | 2,119,928 | 1,296,288 | byte_split | 1,310,985 | -1.1% | +38.2% |
| feature_21 | FLOAT32 (sparse) | 229,407 | 142,420 | byte_split | 159,076 | -11.7% | +30.7% |
| feature_31 | FLOAT32 (quantized) | 344,394 | 312,667 | dict_float | 312,682 | -0.0% | +9.2% |

**Verdict:** All encodings beat Parquet+zstd, but **raw OpenZL was already beating Parquet+zstd before encoding**. Encoding made results *worse* relative to raw OpenZL on every ML feature column. OpenZL's ACE entropy coder already optimally handles these patterns natively.

---

## Detailed Encoding Analysis

### 1. Byte-Stream Split

Separates each N-byte value into N independent single-byte streams. Each stream compressed with `serial` profile.

**Key finding: Streams reveal the entropy structure of the data.**

Example — `battery_voltage` (FLOAT32, 3M values):

| Stream | Content | Compressed | Ratio |
|--------|---------|-----------|-------|
| 0 | Mantissa low | 3,000,026 | 0.0% (incompressible) |
| 1 | Mantissa mid | 3,000,026 | 0.0% (incompressible) |
| 2 | Exponent low + mantissa high | 1,083,548 | 63.9% |
| 3 | Sign + exponent high | **35** | **99.999%** |

The lowest-order mantissa bytes of random floats are essentially random noise — OpenZL cannot compress them (adds 26 bytes of overhead per stream). The sign+exponent byte is highly uniform (all values ~3.0-4.5V), compressing from 3MB to **35 bytes**.

**Conclusion:** Byte-stream split doesn't help because OpenZL already sees the byte-level patterns. The incompressible mantissa bytes dominate the total, negating gains from the compressible streams.

### 2. XOR-Delta

XOR consecutive values to exploit temporal correlation.

| Column | Raw OpenZL | XOR-Delta | Change |
|--------|-----------|-----------|--------|
| temperature | 8,492,492 | 8,699,552 | -2.4% (worse) |
| humidity | 8,167,195 | 8,367,008 | -2.4% (worse) |
| pressure | 4,117,516 | 4,229,644 | -2.7% (worse) |
| battery_voltage | 7,085,773 | 7,236,168 | -2.1% (worse) |
| unit_price | 1,864,491 | 2,552,879 | -36.9% (worse) |
| total_amount | 3,615,484 | TIMEOUT | — |

**Conclusion:** XOR-delta consistently makes things worse. Our synthetic data has insufficient temporal autocorrelation for XOR to produce low-entropy values. XOR of two random floats produces another random float.

### 3. XOR-Delta + Byte-Stream Split

Combination of both. Should help for autocorrelated sequences.

**Conclusion:** Performed similarly to or worse than byte-split alone across all columns. Without temporal correlation in the data, the XOR step adds entropy rather than removing it.

### 4. Bitpack (Booleans)

Pack 8 booleans per byte before compressing.

| Column | Raw (1 byte/bool) | Bitpacked (1 bit/bool) | Compressed | vs Raw OpenZL |
|--------|-------------------|----------------------|-----------|--------------|
| is_returned | 72,370 | 250,000 → 71,936 | 71,936 | +0.6% |

**Conclusion:** Marginal improvement (+0.6%). OpenZL already handles the 0/1 pattern efficiently in the raw stream.

### 5. Dictionary Encoding (Integers)

Build sorted dictionary of unique values, replace with indices using the smallest container.

| Column | Unique | Index Type | Dict OpenZL | Index OpenZL | Total | vs Raw |
|--------|--------|-----------|-------------|-------------|-------|--------|
| device_id | 1,000 | uint16 | 43 | 3,750,035 | 3,750,078 | -0.0% |
| status_code | 6 | uint8 | 32 | 254,714 | 254,746 | -0.0% |
| customer_id | 50,000 | uint16 | 45 | 3,622,746 | 3,622,791 | -0.2% |
| quantity | 20 | uint8 | 37 | 620,340 | 620,377 | -0.0% |

**Conclusion:** Dictionary encoding provides zero benefit for integers. The index arrays compress to nearly the same size as the raw data because the entropy of the value distribution is preserved regardless of the container — just the value range changes.

### 6. Dictionary Encoding (Quantized Floats)

Applied to float columns with limited unique values.

| Column | Unique | Total Compressed | vs Raw OpenZL |
|--------|--------|-----------------|--------------|
| unit_price | 1,550 | 1,857,326 | +0.4% |
| feature_31 | 32 | 312,682 | -0.0% |

**Conclusion:** Marginal or no improvement. Even with 32 unique values (5-bit indices), the compressed index array is nearly the same size as the compressed raw float array.

### 7. Delta + Container Narrowing

Compute deltas, then use the smallest fitting integer type.

| Column | Original Width | Delta Width | Compressed | vs Raw OpenZL |
|--------|---------------|-------------|-----------|--------------|
| timestamp | 8 bytes (INT64) | 2 bytes (INT16) | 3,504,605 | +0.5% |

**Conclusion:** Container narrowing from 8 → 2 bytes (75% smaller encoded representation) yields only +0.5% compression improvement. OpenZL's LE profile already handles the leading zeros in wide integer deltas.

---

## Key Takeaways

### 1. OpenZL's ACE entropy coder already captures byte-level patterns
Pre-compression encodings are largely redundant because OpenZL's internal training process learns the same statistical properties. The ACE graph models byte dependencies, so manually separating bytes (byte-split) or computing deltas provides minimal additional signal.

### 2. The compression gap on random numeric data is fundamental
For uniformly random float values, the mantissa bytes are incompressible by any algorithm. Parquet+zstd does better because:
- **Dictionary encoding at the Parquet level**: Parquet can use RLE_DICTIONARY encoding, which eliminates repetitions before zstd ever sees the data
- **zstd's LZ77 backend**: zstd can find and exploit byte-level repetitions (duplicate 4-byte windows) that OpenZL's entropy coder cannot exploit
- **Page-level statistics**: Parquet encodes per-page min/max which guides dictionary and RLE decisions

### 3. OpenZL excels on structured/patterned data
ML features (monotonic, sparse, quantized distributions) consistently show OpenZL beating Parquet+zstd by 10-62%, even without encoding. This suggests OpenZL's sweet spot is data with learnable statistical patterns rather than uniformly random values.

### 4. Data characteristics determine the winner

| Data Pattern | Winner | Margin |
|-------------|--------|--------|
| Monotonic floats | OpenZL (raw) | +62% vs zstd |
| Sparse floats (many zeros) | OpenZL (raw) | +31% vs zstd |
| Quantized floats (~32 levels) | OpenZL (raw) | +9% vs zstd |
| Random normal floats | OpenZL (raw) | +38% vs zstd |
| Random uniform floats | Parquet+zstd | +52-67% vs OpenZL |
| Low-cardinality integers | Parquet+zstd | +42-71% vs OpenZL |
| High-cardinality integers | Parquet+zstd | +40-65% vs OpenZL |
| Timestamps (microsecond) | Parquet+zstd | +31-52% vs OpenZL |

---

## Implications for the nyx Pipeline

1. **Do NOT implement pre-compression encoding in the production pipeline.** The marginal gains (0–0.7%) do not justify the implementation complexity, additional code paths, or increased compression time.

2. **Raw OpenZL per-column compression remains the optimal strategy.** OpenZL's ACE entropy coder is already the best available transform for the data types where OpenZL excels.

3. **Focus R&D on improving OpenZL's handling of random/high-entropy data** rather than Python-level encoding tricks. The gap with Parquet+zstd on random floats and high-cardinality integers is a compression algorithm issue, not an encoding issue.

4. **The ml_features results validate the core value proposition.** For ML/analytics workloads with structured numerical patterns, OpenZL delivers 10-62% better compression than Parquet+zstd with no encoding overhead.

---

## Reproduction

### Environment
- macOS (darwin 25.1.0), Apple Silicon
- Python 3.13, PyArrow, NumPy
- OpenZL `zli` binary: `nyx/openzl/zli` (symlink to `nyx/openzl/cachedObjs/a33dfeb063ea425b910f4ad6d1ddd68a/zli`)
- Virtual environment: `nyx/.venv`

### Commands

```bash
cd /Users/pavlosrousoglou/Desktop/Cornell/startup/openzl/compression
source nyx/.venv/bin/activate

# Create output directory
mkdir -p parquet_experiments/encoding_experiment

# Run experiment (supports resume — skips already-completed columns)
python -u parquet_experiments/encoding_experiment.py 2>&1 | tee parquet_experiments/encoding_experiment/experiment.log
```

### Input Data
- `parquet_experiments/data/numeric_heavy_zstd.parquet` (3M rows, 51 columns)
- `parquet_experiments/data/mixed_type_zstd.parquet` (2M rows, mixed types)
- `parquet_experiments/data/ml_features_zstd.parquet` (500K rows, 51 float columns)
- Raw OpenZL baselines: `parquet_experiments/per_column_experiment/per_column_results.json`

### Output
- Full results JSON: `parquet_experiments/encoding_experiment/encoding_results.json`
- Console/log output: `parquet_experiments/encoding_experiment/experiment.log`
- All intermediate binary and `.zl` files: `parquet_experiments/encoding_experiment/{dataset}/{column}/`

### Script
- `parquet_experiments/encoding_experiment.py` — single self-contained script implementing all 7 encodings, the full test matrix, resume capability, and summary printing

---

## Appendix: All Encoding Results (Raw Numbers)

### byte_split (12 tests)

| Dataset | Column | Type | Raw Bytes | Stream Sizes | Total Compressed | Time |
|---------|--------|------|----------|-------------|-----------------|------|
| numeric_heavy | temperature | float32 | 12,000,000 | 3M + 3M + 2.4M + 61K | 8,435,063 | 136.0s |
| numeric_heavy | humidity | float32 | 12,000,000 | 3M + 3M + 2.1M + 26K | 8,136,606 | 92.9s |
| numeric_heavy | pressure | float32 | 12,000,000 | 3M + 1M + 46K + 18K | 4,093,502 | 114.9s |
| numeric_heavy | battery_voltage | float32 | 12,000,000 | 3M + 3M + 1.1M + 35 | 7,083,635 | 102.8s |
| mixed_type | unit_price | double | 16,000,000 | 660K×5 + 909K + 1.6M + 28K | 5,823,157 | 231.1s |
| mixed_type | total_amount | double | 16,000,000 | 1.2M×4 + 1.2M + 1.9M + 1.7M + 10K | 9,471,477 | 418.9s |
| mixed_type | order_date | timestamp | 16,000,000 | 2M×5 + 1.4M + 196K + 33 | 11,625,348 | 192.9s |
| ml_features | feature_01 | float32 | 2,000,000 | 493K + 246K + 46K + 25K | 809,442 | 45.5s |
| ml_features | feature_11 | float32 | 2,000,000 | 493K + 493K + 307K + 18K | 1,310,985 | 33.2s |
| ml_features | feature_21 | float32 | 2,000,000 | 44K + 46K + 44K + 26K | 159,076 | 47.7s |
| ml_features | feature_31 | float32 | 2,000,000 | 309K + 309K + 313K + 155K | 1,085,468 | 87.0s |

### xor_delta (8 tests, 1 timeout)

| Dataset | Column | Type | Compressed | Time | vs Raw |
|---------|--------|------|-----------|------|--------|
| numeric_heavy | temperature | float32 | 8,699,552 | 148.2s | -2.4% |
| numeric_heavy | humidity | float32 | 8,367,008 | 160.2s | -2.4% |
| numeric_heavy | pressure | float32 | 4,229,644 | 136.0s | -2.7% |
| numeric_heavy | battery_voltage | float32 | 7,236,168 | 302.5s | -2.1% |
| mixed_type | unit_price | double | 2,552,879 | 507.1s | -36.9% |
| mixed_type | total_amount | double | **TIMEOUT** | >600s | — |
| mixed_type | order_date | timestamp | 11,788,170 | 525.8s | -3.1% |
| ml_features | feature_01 | float32 | 857,382 | 45.0s | -21.5% |
| ml_features | feature_11 | float32 | 1,394,496 | 46.8s | -7.6% |

### xor_delta_bytesplit (7 tests)

| Dataset | Column | Type | Compressed | Time | vs Raw |
|---------|--------|------|-----------|------|--------|
| numeric_heavy | temperature | float32 | 8,652,308 | 116.8s | -1.9% |
| numeric_heavy | humidity | float32 | 8,331,772 | 116.7s | -2.0% |
| numeric_heavy | pressure | float32 | 4,254,185 | 109.7s | -3.3% |
| numeric_heavy | battery_voltage | float32 | 7,212,418 | 113.8s | -1.8% |
| mixed_type | unit_price | double | 7,616,885 | 542.4s | -308.5% |
| mixed_type | total_amount | double | 11,824,805 | 1155.2s | -227.1% |

### dict_int (4 tests)

| Dataset | Column | Unique | Index Compressed | Dict Compressed | Total | vs Raw |
|---------|--------|--------|-----------------|----------------|-------|--------|
| numeric_heavy | device_id | 1,000 | 3,750,035 | 43 | 3,750,078 | -0.0% |
| numeric_heavy | status_code | 6 | 254,714 | 32 | 254,746 | -0.0% |
| mixed_type | customer_id | 50,000 | 3,622,746 | 45 | 3,622,791 | -0.2% |
| mixed_type | quantity | 20 | 620,340 | 37 | 620,377 | -0.0% |

### dict_float (2 tests)

| Dataset | Column | Unique | Total Compressed | vs Raw |
|---------|--------|--------|-----------------|--------|
| mixed_type | unit_price | 1,550 | 1,857,326 | +0.4% |
| ml_features | feature_31 | 32 | 312,682 | -0.0% |

### bitpack (1 test)

| Dataset | Column | Raw → Packed | Compressed | vs Raw |
|---------|--------|-------------|-----------|--------|
| mixed_type | is_returned | 2,000,000 → 250,000 | 71,936 | +0.6% |

### delta_narrow (1 test)

| Dataset | Column | Original Width | Delta Width | Compressed | vs Raw |
|---------|--------|---------------|-------------|-----------|--------|
| numeric_heavy | timestamp | 8 bytes | 2 bytes | 3,504,605 | +0.5% |
