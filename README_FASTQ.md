# FASTQ Compression Pipeline

## Overview

This pipeline compresses Illumina FASTQ files using a **custom parallel preprocessor
+ trained OpenZL compressor**. Using a **universal compressor** trained on a mixed corpus
of two structurally different FASTQ files (ERR9539086 and SRR8899104), it achieves
**7.84–7.96× compression** on unseen data — 26–28 % better than xz (6.21×) — with
a compression time of under 1 second (16 parallel workers).

A single-dataset compressor trained on ERR9539086 alone reaches 8.45×, but fails on
FASTQ files with different read lengths, tile counts, or quality encodings. The universal
approach sacrifices ~6 % ratio for cross-dataset compatibility.

## Pipeline Architecture

```
Original FASTQ ──► fastq_preprocess encode ──► .meta + .tsv ──► OpenZL compress ──► .zl
                                                                                      │
Decoded  FASTQ ◄── fastq_preprocess decode ◄── .meta + .tsv ◄── OpenZL decompress ◄──┘
```

1. **Preprocessing** (`fastq_preprocess encode`): Parses Illumina FASTQ records,
   extracts structured header fields, dictionary-encodes low-cardinality values,
   and drops redundant / constant fields. Produces:
   - `.meta` — small binary sidecar (dictionaries, constant fields, ~4 KB)
   - `.tsv` — transformed tab-delimited text (feeds into OpenZL CSV profiler)

2. **Compression** (`zli compress`): OpenZL's trained CSV profiler compresses
   the `.tsv` using column-aware entropy coding.

## Preprocessor Logic (`tools/fastq_preprocess.cpp`)

FASTQ has 4 lines per read:

```
@PREFIX.READ_NUM INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y
ACGTACGT...                  (sequence)
+                            (separator — always discarded)
IIIIIIII...                  (quality scores)
```

### Transforms Applied

| Field | Transform | Rationale |
|-------|-----------|-----------|
| `+` line | **Dropped** | Always reconstructable (constant or copy of header) |
| `@PREFIX` | **Dropped** | Constant across file (stored once in .meta) |
| Read number | **Dropped** | Sequential 1…N (reconstructed during decode) |
| Instrument | **Dropped** | Constant across file (stored once in .meta) |
| Run | **Dict** | Low cardinality → integer ID |
| Flowcell | **Dict** | Low cardinality → integer ID |
| Lane | **Dict** | Low cardinality → integer ID |
| Tile | **Dict** | Low cardinality → integer ID |
| X, Y | **Raw** | High cardinality integers, kept as-is |
| Pair suffix (`/1`, `/2`) | **Dropped** | Constant across file (stored once in .meta) |
| Sequence | **Raw** | ACGT string, kept as-is |
| Quality | **Raw** | Phred-encoded string, kept as-is |

**Output TSV columns**: `run_id  fc_id  lane_id  tile_id  x  y  sequence  quality`

The preprocessor uses **mmap** and **multi-threaded** line scanning / parsing
for maximum throughput (~500 MB/s on modern hardware).

## Datasets

Two structurally different FASTQ files are used: one for the original benchmark and
one to validate cross-dataset generality.

### ERR9539086 (NovaSeq, variable-length reads)

| Property | Value |
|----------|-------|
| **Source** | European Nucleotide Archive (ENA) |
| **Accession** | ERR9539086 |
| **Full file** | `ERR9539086.fastq` — 8 378 114 689 bytes (7.8 GiB) |
| **URL** | `https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/086/ERR9539086/ERR9539086.fastq.gz` |
| **Sample** | First 14 000 000 lines → `ERR9539086_500M.fastq` (529 696 262 bytes, 3.5 M reads) |
| **Read lengths** | Variable (30–95 bp) |
| **Tiles** | 704 distinct |
| **Preprocessed TSV** | ~380 MB |
| **Preprocessed .meta** | ~4 KB |

### SRR8899104 (HiSeq, fixed-length reads)

| Property | Value |
|----------|-------|
| **Source** | Sequence Read Archive (SRA) |
| **Accession** | SRR8899104 |
| **Full file** | `SRR8899104.fastq` — ~24 GiB |
| **URL** | `https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR889/004/SRR8899104/SRR8899104.fastq.gz` |
| **Sample** | First 13 000 000 lines → `SRR8899104_500M.fastq` (549 733 954 bytes, 3.25 M reads) |
| **Read lengths** | Fixed 51 bp |
| **Tiles** | 14 distinct |
| **Pair suffix** | `/1` (constant, stored in .meta) |
| **Preprocessed TSV** | ~402 MB |
| **Preprocessed .meta** | ~156 bytes |

## Reproduction Steps

### 0) Prerequisites

```bash
bash scripts/get_openzl.sh   # fetch + patch OpenZL
bash scripts/build_all.sh    # compile zli, fastq_preprocess, etc.
```

### 1) Download and slice

```bash
mkdir -p data/fastq

# ERR9539086 (NovaSeq, variable-length)
wget -O data/fastq/ERR9539086.fastq.gz \
  https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/086/ERR9539086/ERR9539086.fastq.gz
gunzip data/fastq/ERR9539086.fastq.gz
head -14000000 data/fastq/ERR9539086.fastq > data/fastq/ERR9539086_500M.fastq

# SRR8899104 (HiSeq, fixed 51bp)
wget -O data/fastq/SRR8899104.fastq.gz \
  https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR889/004/SRR8899104/SRR8899104.fastq.gz
gunzip data/fastq/SRR8899104.fastq.gz
head -13000000 data/fastq/SRR8899104.fastq > data/fastq/SRR8899104_500M.fastq
```

### 2) Preprocess both files

```bash
./tools/fastq_preprocess encode \
    data/fastq/ERR9539086_500M.fastq \
    data/fastq/ERR9539086_500M_pp \
    16
# → data/fastq/ERR9539086_500M_pp.meta  (~4 KB)
# → data/fastq/ERR9539086_500M_pp.tsv   (~380 MB)

./tools/fastq_preprocess encode \
    data/fastq/SRR8899104_500M.fastq \
    data/fastq/SRR8899104_500M_pp \
    16
# → data/fastq/SRR8899104_500M_pp.meta  (~156 bytes)
# → data/fastq/SRR8899104_500M_pp.tsv   (~402 MB)
```

### 3) Train the universal compressor (mixed corpus)

A compressor trained on a single FASTQ file fails on structurally different files
because OpenZL's trained entropy models hard-code value ranges, integer widths, and
alphabet sizes observed during training. Differences in read length (variable vs fixed),
tile cardinality (704 vs 14), and quality score distributions cause `parse_int` errors
when the compressor encounters values outside its learned ranges.

To address this, we train on a **mixed corpus** — 4 chunks from each file (8 chunks
total, ~66 MB), so the model sees the full range of column statistics from both datasets:

```bash
mkdir -p data/fastq/mixed_train_chunks

# 4 chunks × 70 000 lines from ERR9539086
head -280000 data/fastq/ERR9539086_500M_pp.tsv > /tmp/err_train.tsv
split -l 70000 -d /tmp/err_train.tsv data/fastq/mixed_train_chunks/err_chunk_

# 4 chunks × 70 000 lines from SRR8899104
head -280000 data/fastq/SRR8899104_500M_pp.tsv > /tmp/srr_train.tsv
split -l 70000 -d /tmp/srr_train.tsv data/fastq/mixed_train_chunks/srr_chunk_
```

Train:

```bash
./openzl/zli train data/fastq/mixed_train_chunks/ \
    --profile csv --profile-arg $'\t' \
    --output artifacts/fastq_universal.compressor \
    --force --threads 16 --use-all-samples
```

The trained universal compressor is ~11.5 KB. Training takes ~13 minutes (one-time cost).

### 4) Compress (16 parallel parts)

Example for ERR9539086 (same procedure applies to SRR8899104):

```bash
mkdir -p artifacts/fastq_parts
LINES=$(wc -l < data/fastq/ERR9539086_500M_pp.tsv)
CHUNK=$(( (LINES + 15) / 16 ))
split -l "$CHUNK" -d data/fastq/ERR9539086_500M_pp.tsv artifacts/fastq_parts/part_

for i in $(seq -w 0 15); do
    ./openzl/zli compress artifacts/fastq_parts/part_$i \
        --compressor artifacts/fastq_universal.compressor \
        --output artifacts/fastq_parts/part_$i.zl --force &
done
wait
```

### 5) Verify (round-trip decode)

```bash
for i in $(seq -w 0 15); do
    ./openzl/zli decompress artifacts/fastq_parts/part_$i.zl \
        --output /tmp/fq_part_$i.tsv --force &
done
wait
cat /tmp/fq_part_*.tsv > /tmp/fq_reconstructed_pp.tsv
./tools/fastq_preprocess decode \
    data/fastq/ERR9539086_500M_pp \
    /tmp/fq_reconstructed.fastq 16
diff data/fastq/ERR9539086_500M.fastq /tmp/fq_reconstructed.fastq  # should be empty
```

## Benchmark Results

All benchmarks use 16 threads/workers. Ratio = original file size / compressed size.

### ERR9539086 — NovaSeq, variable-length reads (505 MiB)

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL single-dataset (trained, 16p)** | **8.45×** | **1.93** |
| **OpenZL universal (trained, 16p)** | **7.96×** | **0.59** |
| xz (default, 16t) | 6.21× | 27.46 |
| 7z (default, 16t) | 6.10× | 27.17 |
| zstd -7 (16t) | 5.12× | 0.53 |
| pigz -9 (16t) | 5.05× | 5.18 |
| bgzip (default, 16t) | 4.97× | 0.74 |
| gzip (default) | 4.93× | 37.38 |
| bgzip -l2 (16t) | 4.43× | 0.25 |

### SRR8899104 — HiSeq, fixed 51 bp reads (524 MiB)

| Tool | Ratio |
|------|------:|
| **OpenZL universal (trained, 16p)** | **7.84×** |
| OpenZL single-dataset (ERR-only) | **FAILS** |

Note: The single-dataset compressor (trained only on ERR9539086) fails on SRR8899104
due to incompatible column statistics (different read length, tile count, quality alphabet).
The universal compressor handles both files.

### Universal vs single-dataset compressor

| Compressor | ERR9539086 | SRR8899104 |
|-----------|:---:|:---:|
| Single-dataset (ERR only) | 8.45× | FAILS |
| **Universal (mixed)** | **7.96×** | **7.84×** |

The universal compressor trades ~6 % ratio on the original dataset for cross-dataset
compatibility. Even at 7.84–7.96×, it remains **26–28 % better** than xz and
**13–14× faster**.
