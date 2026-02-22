# OpenZL Compression Pipelines

Three self-contained, reproducible pipelines for schema-based columnar compression using [OpenZL](https://github.com/facebook/openzl):

| Pipeline | Data Type | Schema | Preprocessor |
|----------|-----------|--------|--------------|
| **FASTA** | Genomic sequences (`.fna`) | `schemas/fasta_packed.sddl` | `tools/biocompress_preprocessor.cpp` |
| **GeoJSON** | Any GeoJSON FeatureCollection | Auto-generated from data | `tools/geojson_to_bin_universal.cpp` |
| **LiDAR** | KITTI Velodyne point clouds | `schemas/lidar.sddl` | `tools/lidar_preprocessor.cpp` |

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
tools/             # All preprocessors (C++/Python source)
  biocompress_preprocessor.cpp
  geojson_to_bin_universal.cpp
  lidar_preprocessor.cpp
  scan_geojson_schema.py
schemas/           # SDDL schemas (GeoJSON schemas are auto-generated)
  fasta_packed.sddl
  lidar.sddl
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

---

## Cleaning Up

```bash
# Remove all generated outputs (keeps data downloads, sources, openzl checkout)
bash scripts/common/clean_all.sh

# Clean a specific GeoJSON experiment
bash scripts/geojson/clean.sh data/citylots.json

# Clean LiDAR outputs
bash scripts/lidar/clean.sh
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

Example:

```bash
TARGET_MIB=200 THREADS=8 MAX_TIME_SECS=600 bash scripts/fasta/run_pipeline.sh
```
