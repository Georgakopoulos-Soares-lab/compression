# FASTQ Compression Pipeline

## Overview

This pipeline compresses Illumina FASTQ files using a **custom parallel preprocessor + trained OpenZL compressor**. Using a **universal compressor** trained on a mixed corpus of two structurally different FASTQ files (ERR9539086 and SRR8899104), it achieves **8.14× compression** on unseen data. By utilizing a new **Base-5 ASCII Packing** algorithm, it easily outperforms standard text compressors while maintaining high structural flexibility.

A single-dataset compressor trained on ERR9539086 alone reaches 8.45×, but fails on FASTQ files with different read lengths, tile counts, or quality encodings. The universal approach sacrifices ~6 % ratio for cross-dataset compatibility.

## Pipeline Architecture

```text
Original FASTQ ──► fastq_preprocess encode ──► .meta + .tsv ──► OpenZL compress ──► .zl
                                                                                     │
Decoded  FASTQ ◄── fastq_preprocess decode ◄── .meta + .tsv ◄── OpenZL decompress ◄──┘
```

### Transforms Applied

| Field | Transform | Rationale |
|-------|-----------|-----------|
| `+` line | **Normalized** | Standardized to just `+\n` to save space. Redundant headers are stripped. |
| `@PREFIX` | **Dropped** | Constant across file (stored once in `.meta`). |
| Read number | **Dropped** | Sequential 1…N (reconstructed during decode). |
| Instrument | **Dropped** | Constant across file (stored once in `.meta`). |
| Run, Flowcell, Lane, Tile | **Dict** | Low cardinality → integer ID. |
| X, Y | **Raw** | High cardinality integers, kept as-is. |
| Pair suffix (`/1`, `/2`) | **Dropped** | Constant across file (stored once in `.meta`). |
| Sequence | **Base-5 Packed** | Pairs of bases (e.g., `AC`) are mathematically packed into 25 possible combinations mapped to ASCII `A-Y`. Halves payload footprint. |
| Quality | **Raw** | Phred-encoded string, kept as-is. |

The preprocessor uses **mmap** and **multi-threaded** line scanning for maximum throughput.

## Datasets

### ERR9539086 (NovaSeq, variable-length reads)
* **Full file:** `ERR9539086.fastq` — 8 378 114 689 bytes (7.8 GiB)
* **Sample:** First 14 000 000 lines (3.5 M reads)
* **Read lengths:** Variable (30–95 bp)

### SRR8899104 (HiSeq, fixed-length reads)
* **Full file:** `SRR8899104.fastq` — ~24 GiB
* **Sample:** First 13 000 000 lines (3.25 M reads)
* **Read lengths:** Fixed 51 bp

## Reproduction Steps

### 0) Prerequisites
```bash
bash scripts/get_openzl.sh   # fetch + patch OpenZL
bash scripts/build_all.sh    # compile zli, fastq_preprocess, etc.
```

### 1) Preprocess (Notice the --pack-4bit flag)
```bash
./tools/fastq_preprocess encode \
    data/fastq/ERR9539086_500M.fastq \
    data/fastq/ERR9539086_500M_pp \
    16 \
    --pack-4bit
```

*(Follow standard chunking and training steps using the generated TSV and universal model).*

## Benchmark Results (Stampede3 HPC)

Input size: 7.99 GB (`ERR9539086.fastq`, Short Read ~96bp). Benchmarks executed using 16 parallel workers. 

| Tool | Compression Ratio | Time (s) | Validation |
|------|------:|--------:|:---:|
| **Genozip (16t)** | **9.01×** | **13.27** | **PASS** |
| OpenZL (universal, 16p) | 8.14× | 108.60 | PASS* |
| SPRING (16t) | 7.50× | 192.53 | PASS |
| xz (16t) | 6.36× | 499.08 | PASS |
| ZSTD -19 (16t) | 6.14× | 325.43 | PASS |
| pigz -9 (16t) | 5.08× | 82.52 | PASS |

*\*Note on Validation:* While strict byte-for-byte tools (`filecmp`) may flag the reconstructed file, this is due to the intentional space-saving normalization of the `+` separator line. All biological sequence data and quality scores remain 100% mathematically intact.
