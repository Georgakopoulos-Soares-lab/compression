# FASTQ Compression Pipeline

## Overview

This pipeline compresses Illumina FASTQ files using a **custom parallel preprocessor
+ trained OpenZL compressor**. On a 500 MB sample of `ERR9539086.fastq` (3.5 M reads),
it achieves **8.45× compression** — 36 % better than xz (6.21×) — with an end-to-end
time of ~2 seconds (preprocessing + compression).

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
| Sequence | **Raw** | ACGT string, kept as-is |
| Quality | **Raw** | Phred-encoded string, kept as-is |

**Output TSV columns**: `run_id  fc_id  lane_id  tile_id  x  y  sequence  quality`

The preprocessor uses **mmap** and **multi-threaded** line scanning / parsing
for maximum throughput (~500 MB/s on modern hardware).

## Dataset

| Property | Value |
|----------|-------|
| **Source** | European Nucleotide Archive (ENA) |
| **Accession** | ERR9539086 |
| **Full file** | `ERR9539086.fastq` — 8 378 114 689 bytes (7.8 GiB) |
| **URL** | `https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/086/ERR9539086/ERR9539086.fastq.gz` |
| **Sample** | First 14 000 000 lines → `ERR9539086_500M.fastq` (529 696 262 bytes, 3.5 M reads) |
| **Preprocessed TSV** | ~380 MB |
| **Preprocessed .meta** | ~4 KB |

## Reproduction Steps

### 0) Prerequisites

```bash
bash scripts/get_openzl.sh   # fetch + patch OpenZL
bash scripts/build_all.sh    # compile zli, fastq_preprocess, etc.
```

### 1) Download and slice

```bash
mkdir -p data/fastq
wget -O data/fastq/ERR9539086.fastq.gz \
  https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR953/086/ERR9539086/ERR9539086.fastq.gz
gunzip data/fastq/ERR9539086.fastq.gz

# Take a 500 MB sample (first 14M lines = 3.5M reads)
head -14000000 data/fastq/ERR9539086.fastq > data/fastq/ERR9539086_500M.fastq
```

Or use the helper script:

```bash
bash scripts/fastq/download_fastq.sh
```

### 2) Preprocess

```bash
./tools/fastq_preprocess encode \
    data/fastq/ERR9539086_500M.fastq \
    data/fastq/ERR9539086_500M_pp \
    32   # threads (optional, default = hardware concurrency)
# → data/fastq/ERR9539086_500M_pp.meta  (~4 KB)
# → data/fastq/ERR9539086_500M_pp.tsv   (~380 MB)
```

### 3) Train the compressor

Create training chunks (~43 MB total, 8 chunks):

```bash
mkdir -p data/fastq/train_chunks
head -560000 data/fastq/ERR9539086_500M_pp.tsv > /tmp/fq_train.tsv
split -l 70000 -d /tmp/fq_train.tsv data/fastq/train_chunks/chunk_
```

Train:

```bash
./openzl/zli train data/fastq/train_chunks/ \
    --profile csv --profile-arg $'\t' \
    --output artifacts/fastq_trained.compressor \
    --force --threads 64 --use-all-samples
```

The trained compressor is ~11 KB.

### 4) Compress (16 parallel parts)

```bash
mkdir -p artifacts/fastq_parts
LINES=$(wc -l < data/fastq/ERR9539086_500M_pp.tsv)
CHUNK=$(( (LINES + 15) / 16 ))
split -l "$CHUNK" -d data/fastq/ERR9539086_500M_pp.tsv artifacts/fastq_parts/part_

for i in $(seq -w 0 15); do
    ./openzl/zli compress artifacts/fastq_parts/part_$i \
        --compressor artifacts/fastq_trained.compressor \
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
    /tmp/fq_reconstructed.fastq 32
diff data/fastq/ERR9539086_500M.fastq /tmp/fq_reconstructed.fastq  # should be empty
```

## Benchmark Results

Input size: 529 696 262 bytes (original `ERR9539086_500M.fastq`). Ratio = input / output.

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL (trained, 16p)** | **8.45×** | **1.93** |
| xz (default, 16t) | 6.21× | 27.46 |
| 7z (default, 16t) | 6.10× | 27.17 |
| zstd -7 (16t) | 5.12× | 0.53 |
| pigz -9 (16t) | 5.05× | 5.18 |
| bgzip (default, 16t) | 4.97× | 0.74 |
| gzip (default) | 4.93× | 37.38 |
| bgzip -l2 (16t) | 4.43× | 0.25 |

OpenZL achieves **36 % higher compression** than xz with an end-to-end time
(preprocessing + compression) of under 2 seconds.
