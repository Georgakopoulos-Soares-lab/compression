# VCF Compression Pipeline

## Overview

This pipeline compresses VCF (Variant Call Format) files using **OpenZL's CSV profiler**
with optional training. The VCF is first split into a header sidecar (meta lines) and a
pure tab-delimited data table, then optionally chunked and compressed.

Two datasets were benchmarked:

| Dataset | OpenZL Ratio | Best Baseline | Notes |
|---------|:------------:|:-------------:|-------|
| **ClinVar** (38 cols) | **13.13×** | xz 12.58× | OpenZL wins (+4.4 %) |
| **1000 Genomes chr22** (2 513 cols) | 33.14× (untrained) | xz 138.93× | Very wide VCF — xz wins |

ClinVar's moderate column count (38 columns) is a good fit for the CSV profiler.
The 1000 Genomes file has 2 513 sample columns — the extremely wide table favours
general-purpose tools that exploit the high sample-column redundancy.

## Pipeline Architecture

```
VCF (.vcf/.vcf.gz)
   │
   ├── make_vcf_sample.py ──► .meta.txt   (## header lines)
   │                      ──► .table.tsv  (#CHROM header + data rows)
   │
   ├── tabular_chunker   ──► chunk_00, chunk_01, … (line-safe TSV chunks)
   │
   └── zli compress (--profile csv --profile-arg $'\t') ──► .zl
```

1. **VCF splitter** (`scripts/vcf/make_vcf_sample.py`): Separates the VCF into
   `## meta` header lines and a tab-delimited data table. Optionally takes a
   `--target-mib` argument to create a size-bounded sample (default: 500 MiB).

2. **Tabular chunker** (`tools/tabular_chunker`): Splits the TSV into chunks of
   a target size without breaking lines. Optionally repeats the header in each chunk.

3. **Compression** (`zli compress`): Each chunk is compressed independently using
   the CSV profiler, either with a trained compressor or the built-in untrained one.

## Datasets

### ClinVar

| Property | Value |
|----------|-------|
| **Source** | NCBI ClinVar |
| **URL** | `https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz` |
| **Columns** | 38 (fixed fields + INFO sub-fields) |
| **Benchmark table** | `clinvar_bench_500MiB.table.tsv` — 524 281 352 bytes |
| **Trained compressor** | `clinvar_csv_train_200MiB_t16.compressor` (~11 KB) |

### 1000 Genomes (chr22)

| Property | Value |
|----------|-------|
| **Source** | 1000 Genomes Project, Phase 3, chromosome 22 |
| **URL** | `https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz` |
| **Columns** | 2 513 (9 fixed + 2 504 samples) |
| **Benchmark table** | `vcf_chr22_bench_500MiB.table.tsv` — 524 262 634 bytes |
| **Compressor** | Untrained CSV profiler (training failed on 2 500+ columns) |

## Reproduction Steps

### 0) Prerequisites

```bash
bash scripts/get_openzl.sh   # fetch + patch OpenZL
bash scripts/build_all.sh    # compile zli, tabular_chunker, etc.
pip3 install --user gzip      # Python 3, gzip is in stdlib — nothing extra needed
```

### 1) Download ClinVar

```bash
mkdir -p data/vcf
wget -O data/vcf/clinvar.vcf.gz \
  https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/clinvar.vcf.gz
```

Or use the helper script:

```bash
bash scripts/vcf/download_vcf.sh
```

### 2) Create the 500 MiB benchmark sample

```bash
python3 scripts/vcf/make_vcf_sample.py \
    --input data/vcf/clinvar.vcf.gz \
    --out-prefix data/vcf/clinvar_bench_500MiB \
    --target-mib 500
# → data/vcf/clinvar_bench_500MiB.meta.txt
# → data/vcf/clinvar_bench_500MiB.table.tsv  (~500 MiB)
```

### 3) Create training chunks (~200 MiB)

```bash
python3 scripts/vcf/make_vcf_sample.py \
    --input data/vcf/clinvar.vcf.gz \
    --out-prefix data/vcf/clinvar_train_200MiB \
    --target-mib 200

mkdir -p data/vcf/train_chunks
split -l 70000 -d data/vcf/clinvar_train_200MiB.table.tsv data/vcf/train_chunks/chunk_
```

### 4) Train the compressor

```bash
./openzl/zli train data/vcf/train_chunks/ \
    --profile csv --profile-arg $'\t' \
    --output artifacts/clinvar_csv_trained.compressor \
    --force --threads 16 --use-all-samples
```

The trained compressor is ~11 KB.

### 5) Create benchmark chunks & compress

```bash
# Use the run script for a full automated benchmark (preprocess + compress + baselines + plot):
bash scripts/vcf/run_vcf_benchmark.sh data/vcf/clinvar.vcf.gz

# Or manually:
mkdir -p artifacts/vcf_clinvar_parts
LINES=$(wc -l < data/vcf/clinvar_bench_500MiB.table.tsv)
CHUNK=$(( (LINES + 15) / 16 ))
split -l "$CHUNK" -d data/vcf/clinvar_bench_500MiB.table.tsv artifacts/vcf_clinvar_parts/part_

for f in artifacts/vcf_clinvar_parts/part_*; do
    [[ "$f" == *.zl ]] && continue
    ./openzl/zli compress "$f" \
        --compressor artifacts/clinvar_csv_trained.compressor \
        --output "$f.zl" --force &
done
wait

INPUT=$(stat -c%s data/vcf/clinvar_bench_500MiB.table.tsv)
OUTPUT=0; for f in artifacts/vcf_clinvar_parts/part_*.zl; do OUTPUT=$((OUTPUT + $(stat -c%s "$f"))); done
echo "ratio = $(echo "scale=2; $INPUT/$OUTPUT" | bc)x"
```

## 1000 Genomes (chr22) — Untrained

```bash
# Download
wget -O data/vcf/ALL.chr22.vcf.gz \
  "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz"

# Sample
python3 scripts/vcf/make_vcf_sample.py \
    --input data/vcf/ALL.chr22.vcf.gz \
    --out-prefix data/vcf/vcf_chr22_bench_500MiB \
    --target-mib 500

# Chunk & compress (untrained — no compressor argument)
mkdir -p artifacts/vcf_1000g_parts
LINES=$(wc -l < data/vcf/vcf_chr22_bench_500MiB.table.tsv)
CHUNK=$(( (LINES + 15) / 16 ))
split -l "$CHUNK" -d data/vcf/vcf_chr22_bench_500MiB.table.tsv artifacts/vcf_1000g_parts/part_

for f in artifacts/vcf_1000g_parts/chunk_*; do
    [[ "$f" == *.zl ]] && continue
    ./openzl/zli compress "$f" \
        --profile csv --profile-arg $'\t' \
        --output "$f.zl" --force &
done
wait
```

## Benchmark Results

### ClinVar (38 columns, 500 MiB table)

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL CSV (trained, 16t)** | **13.13×** | **6.53** |
| xz (default, 16t) | 12.58× | 10.42 |
| 7z (default, 16t) | 11.71× | 10.30 |
| zstd -7 (16t) | 11.40× | 0.32 |
| pigz -9 (16t) | 10.95× | 1.14 |
| gzip (default) | 10.34× | 7.30 |
| bgzip (default, 16t) | 9.20× | 0.56 |
| bgzip -l2 (16t) | 7.40× | 0.36 |

### 1000 Genomes chr22 (2 513 columns, 500 MiB table)

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| xz (default, 16t) | 138.93× | 2.56 |
| 7z (default, 16t) | 117.38× | 3.98 |
| zstd -7 (16t) | 84.52× | 0.16 |
| pigz -9 (16t) | 63.37× | 2.98 |
| gzip (default) | 56.14× | 5.50 |
| bgzip (default, 16t) | 49.97× | 0.48 |
| OpenZL CSV (untrained, 16t) | 33.14× | 8.43 |
| bgzip -l2 (16t) | 33.33× | 0.25 |

> **Note**: The 1000 Genomes chr22 file has 2 504 sample columns with very repetitive
> genotype strings (`0|0`, `0|1`, etc.). General-purpose byte-level compressors
> (especially xz / LZMA2) exploit this repetition very efficiently. Training an
> OpenZL compressor on 2 500+ columns was not successful (the column dispatch exceeds
> reasonable limits). A specialized genotype encoder would be needed to match xz here.
