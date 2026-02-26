# BED Compression Pipeline

## Overview

This pipeline compresses BED-like tab-delimited genomic files (e.g. UCSC RepeatMasker output)
using a **custom preprocessor + trained OpenZL compressor**. On `hg38_rmsk.txt` (492 MB),
it achieves **6.84× compression** — 53 % better than the best general-purpose tool (xz / 7z at 4.47×)
— in under 1 second with 16 threads.

## Pipeline Architecture

```
Original BED ──► bed_preprocess encode ──► .meta + .tsv ──► OpenZL compress ──► .zl
                                                                                 │
Decoded  BED ◄── bed_preprocess decode ◄── .meta + .tsv ◄── OpenZL decompress ◄──┘
```

1. **Preprocessing** (`bed_preprocess encode`): Analyzes column types and applies
   reversible transforms that reduce redundancy. Produces:
   - `.meta` — small binary sidecar (dictionaries, transform metadata, ~200 KB)
   - `.tsv` — transformed tab-delimited text (feeds into OpenZL CSV profiler)

2. **Compression** (`zli compress`): OpenZL's trained CSV profiler compresses the `.tsv`
   using column-aware entropy coding learned during training.

## Preprocessor Transforms

| ID | Transform | Description | Example |
|----|-----------|-------------|---------|
| 0  | **raw**   | Pass-through | Score columns, free text |
| 1  | **delta** | Sorted integers → differences from previous row (resets at group boundaries) | `1000, 1050, 1200` → `1000, 50, 150` |
| 2  | **span**  | Replace `end` column with `end − start` | start=1000, end=1050 → span=50 |
| 3  | **dict**  | Replace strings with integer indices (dictionary stored in .meta) | `chr1, chr2` → `0, 1` |
| 4  | **drop**  | Remove redundant column (reconstructed during decode from partner + group constant) | `genoLeft = −(chromLen − genoEnd)` |

### Auto-Detection Logic

- **Group column**: First string column matching `chr*` with cardinality < 10 000. Deltas reset at group boundaries.
- **Delta**: Numeric columns that are monotonically non-decreasing within each group (tolerates ≤ 0.1 % violations).
- **Span**: Numeric column `c[r] ≈ prev_col[r] + small_positive` — i.e. end = start + length.
- **Redundant/drop**: Column whose value + another column = constant per group.
- **Dictionary**: String columns with ≤ 20 000 distinct values.

## Dataset

| Property | Value |
|----------|-------|
| **Source** | UCSC RepeatMasker annotation |
| **File** | `hg38_rmsk.txt` |
| **URL** | `https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/rmsk.txt.gz` |
| **Uncompressed size** | 491 811 719 bytes (470 MiB) |
| **Rows × Columns** | 5 699 824 × 17 |
| **Preprocessed TSV** | 338 242 248 bytes |
| **Preprocessed .meta** | 199 482 bytes |

## Reproduction Steps

### 0) Prerequisites

```bash
bash scripts/get_openzl.sh   # fetch + patch OpenZL
bash scripts/build_all.sh    # compile zli, bed_preprocess, etc.
```

### 1) Download the dataset

```bash
mkdir -p data/bed
wget -O data/bed/rmsk.txt.gz \
  https://hgdownload.soe.ucsc.edu/goldenPath/hg38/database/rmsk.txt.gz
gunzip data/bed/rmsk.txt.gz
mv data/bed/rmsk.txt data/bed/hg38_rmsk.txt
```

Or use the helper script:

```bash
bash scripts/bed/download_bed.sh
```

### 2) Preprocess

```bash
./tools/bed_preprocess encode data/bed/hg38_rmsk.txt data/bed/hg38_rmsk_pp
# → data/bed/hg38_rmsk_pp.meta  (~200 KB)
# → data/bed/hg38_rmsk_pp.tsv   (~323 MB)
```

### 3) Train the compressor

Create training chunks (~140 MB from the first ~2.4 M rows):

```bash
mkdir -p data/bed/train_chunks
head -2400000 data/bed/hg38_rmsk_pp.tsv > /tmp/bed_train.tsv
split -l 170000 -d /tmp/bed_train.tsv data/bed/train_chunks/chunk_
```

Train:

```bash
./openzl/zli train data/bed/train_chunks/ \
    --profile csv --profile-arg $'\t' \
    --output artifacts/bed_trained.compressor \
    --force --threads 32 --use-all-samples
```

The trained compressor is ~12 KB.

### 4) Compress (16 parallel parts)

```bash
mkdir -p artifacts/bed_parts
LINES=$(wc -l < data/bed/hg38_rmsk_pp.tsv)
CHUNK=$(( (LINES + 15) / 16 ))
split -l "$CHUNK" -d data/bed/hg38_rmsk_pp.tsv artifacts/bed_parts/part_

for i in $(seq -w 0 15); do
    ./openzl/zli compress artifacts/bed_parts/part_$i \
        --compressor artifacts/bed_trained.compressor \
        --output artifacts/bed_parts/part_$i.zl --force &
done
wait
```

### 5) Verify (round-trip decode)

```bash
for i in $(seq -w 0 15); do
    ./openzl/zli decompress artifacts/bed_parts/part_$i.zl \
        --output /tmp/bed_part_$i.tsv --force &
done
wait
cat /tmp/bed_part_*.tsv > /tmp/bed_reconstructed_pp.tsv
./tools/bed_preprocess decode data/bed/hg38_rmsk_pp /tmp/bed_reconstructed.txt
diff data/bed/hg38_rmsk.txt /tmp/bed_reconstructed.txt   # should be empty
```

## Benchmark Results

Input size: 491 811 719 bytes (original `hg38_rmsk.txt`). Ratio = input / output.

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL (trained, 16t)** | **6.84×** | **0.96** |
| xz (default, 16t) | 4.47× | 28.78 |
| 7z (default, 16t) | 4.47× | 32.59 |
| zstd -7 (16t) | 3.31× | 0.70 |
| bgzip (default, 16t) | 3.28× | 0.73 |
| pigz -9 (16t) | 3.25× | 4.13 |
| gzip (default) | 3.23× | 33.10 |
| bgzip -l2 (16t) | 3.03× | 0.55 |

OpenZL achieves **53 % higher compression** than the best general-purpose baseline
while remaining one of the fastest tools.
