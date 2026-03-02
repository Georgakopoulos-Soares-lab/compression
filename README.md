# OpenZL Compression Pipelines

Three self-contained, reproducible pipelines for schema-based columnar compression using [OpenZL](https://github.com/facebook/openzl):

| Pipeline | Data Type | Schema | Preprocessor |
|----------|-----------|--------|--------------|
| **FASTA** | Genomic sequences (`.fna`) | `schemas/fasta_packed.sddl` | `tools/biocompress_preprocessor.cpp` |
| **GeoJSON** | Any GeoJSON FeatureCollection | Auto-generated from data | `tools/geojson_to_bin_universal.cpp` |
| **LiDAR** | KITTI Velodyne point clouds | `schemas/lidar.sddl` | `tools/lidar_preprocessor.cpp` || **VCF** | Variant Call Format (`.vcf`) | `schemas/vcf_columnar.sddl` | `tools/vcf_preprocessor.cpp` |
---

## Repository Structure

```
scripts/
  common/          # Shared: build, fetch OpenZL, clean
    build_all.sh
    get_openzl.sh
    clean_all.sh
  fasta/           # FASTA genomics pipeline
    download_fasta.sh
    make_train_sample.py
    run_pipeline.sh
    run_demo.sh
    slurm_pipeline.sbatch
  geojson/         # General GeoJSON pipeline
    download_geojson.sh
    make_train_sample.py
    run_pipeline.sh
    compare_compressors.sh
    clean.sh
  lidar/           # LiDAR point cloud pipeline
    download_kitti.sh
    run_pipeline.sh
    clean.sh
  vcf/             # VCF variant pipeline
    download_vcf.sh
    make_train_sample.py
    run_pipeline.sh
    run_large_benchmark.sh
    clean.sh
tools/             # All preprocessors (C++/Python source)
  biocompress_preprocessor.cpp
  geojson_to_bin_universal.cpp
  lidar_preprocessor.cpp
  vcf_preprocessor.cpp
  scan_geojson_schema.py
schemas/           # SDDL schemas (GeoJSON schemas are auto-generated)
  fasta_packed.sddl
  lidar.sddl
  vcf_columnar.sddl
data/              # Downloaded datasets (gitignored)
openzl/            # OpenZL checkout (gitignored)
```

---

## Prerequisites

- `git`, `make`, C/C++ toolchain with C++17 (`g++` / `clang++`)
- `python3`, `wget` or `curl`
- `gzip` (FASTA), `unzip` (LiDAR)

## Quick Start

### Build everything

```bash
bash scripts/common/build_all.sh
```

This fetches OpenZL (pinned commit) and compiles all preprocessors.

### Run a pipeline

```bash
# FASTA (download → sample → preprocess → train → compress → validate)
bash scripts/fasta/run_pipeline.sh

# GeoJSON (pass any GeoJSON file)
bash scripts/geojson/run_pipeline.sh data/citylots.json

# LiDAR (downloads KITTI, builds dataset, trains, benchmarks)
bash scripts/lidar/run_pipeline.sh

# VCF — quick smoke-test (ClinVar GRCh38, ~30 MB)
bash scripts/vcf/run_pipeline.sh

# VCF — use a specific VCF file
bash scripts/vcf/run_pipeline.sh data/my_variants.vcf

# VCF — download a different built-in dataset first, then run pipeline
DATASET=1000g_chr22 bash scripts/vcf/download_vcf.sh
bash scripts/vcf/run_pipeline.sh data/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf
```

### FASTA quick demo

```bash
bash scripts/fasta/run_demo.sh
```

### FASTA on Slurm

```bash
sbatch --export=ALL,TARGET_MIB=200,THREADS=16,MAX_TIME_SECS=1800 \
  scripts/fasta/slurm_pipeline.sbatch
```

### Compare GeoJSON compressors

After running the GeoJSON pipeline once:

```bash
bash scripts/geojson/compare_compressors.sh data/citylots.json data/citylots.compressor
```

### VCF large-scale benchmark

For a rigorous, real-world compression ratio assessment use the 1000 Genomes
phase 3 whole-genome **sites-only** VCF (~1.1 GB download → ~14 GB / 84 M variants).
The benchmark automatically compares OpenZL against 7 state-of-the-art compressors:

| # | Tool | Notes |
|---|------|-------|
| 1 | `pigz -9` | Parallel gzip — same algorithm as `gzip`, just multithreaded |
| 2 | `bzip2 -9` | Burrows-Wheeler; widely used in bioinformatics |
| 3 | `bgzip` | BGZF blocked gzip — the genomics random-access standard (tabix/htslib) |
| 4 | `xz -9` | LZMA2 — typically the best ratio of all general-purpose compressors |
| 5 | `zstd -19` | Zstandard — best ratio + parallel, very fast |
| 6 | `bcftools BCF` | Binary VCF format: bit-packed genotypes + integer field IDs, then BGZFed |
| 7 | `genozip` | Purpose-built VCF/genomics compressor; usually best-in-class for VCF |

```bash
# Runs everything: download → preprocess → train (1 GiB sample, 2 h max) → compress → report
bash scripts/vcf/run_large_benchmark.sh

# Tune parallelism and training budget
THREADS=32 TARGET_MIB=2000 MAX_TIME_SECS=14400 bash scripts/vcf/run_large_benchmark.sh

# Enable full round-trip validation (adds ~1 h)
VALIDATE_FULL=1 bash scripts/vcf/run_large_benchmark.sh

# Skip download — point at a VCF you already have
bash scripts/vcf/run_large_benchmark.sh /path/to/your_large.vcf
```

**Available VCF datasets** (`DATASET=` for `download_vcf.sh`):

| `DATASET` | Source | Download | Uncompressed | Variants |
|---|---|---|---|---|
| `clinvar` (default) | NCBI ClinVar GRCh38 | ~6 MB | ~30 MB | ~1 M |
| `1000g_chr22` | 1000G phase 3 chr22 + genotypes | ~500 MB | ~3–4 GB | ~1.1 M × 2504 samples |
| `1000g_wgs_sites` | 1000G phase 3 WGS sites-only | ~1.1 GB | ~14 GB | ~84 M |

**Disk requirements for the large benchmark:** ~30–40 GB free.
**Expected wall time:** ~1–2 hours (training capped at `MAX_TIME_SECS`=1800 s by default; use `pigz` for the fastest gzip baseline).

---

## Cleaning Up

```bash
# Remove all generated outputs (keeps data downloads, sources, openzl checkout)
bash scripts/common/clean_all.sh

# Clean a specific GeoJSON experiment
bash scripts/geojson/clean.sh data/citylots.json

# Clean LiDAR outputs
bash scripts/lidar/clean.sh

# Clean VCF outputs
bash scripts/vcf/clean.sh
```

---

## Environment Overrides

All pipelines accept environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `THREADS` | `16` | Parallelism for training & compression |
| `TARGET_MIB` | `200` | Training sample size (MiB) |
| `MAX_TIME_SECS` | `1800` | Max training time (~30 min) |
| `VALIDATE_FULL` | `1` | Enable full decompression validation |
| `KEEP_CHUNKS` | `0` | Retain chunk directories after large benchmark |
| `DATASET` | `clinvar` | VCF dataset preset for `download_vcf.sh` |

Example:

```bash
TARGET_MIB=200 THREADS=8 MAX_TIME_SECS=600 bash scripts/fasta/run_pipeline.sh
```
