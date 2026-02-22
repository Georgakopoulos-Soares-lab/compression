# Installation / Setup

## Prerequisites

- `git`
- `make`
- C/C++ toolchain with C++17 support (`g++`, `clang++`)
- `python3`
- `wget` or `curl`
- `gzip` (FASTA pipeline)
- `unzip` (LiDAR pipeline)

## One-command build

```bash
bash scripts/common/build_all.sh
```

This will:
1. Clone OpenZL (pinned commit) if not already present
2. Build `openzl/zli`
3. Compile all preprocessors into `tools/`

## Run a pipeline

### FASTA

```bash
bash scripts/fasta/run_pipeline.sh
```

Defaults: `TARGET_MIB=200`, `THREADS=16`, `MAX_TIME_SECS=1800`

### GeoJSON

```bash
# Download sample data first
bash scripts/geojson/download_geojson.sh

# Run pipeline
bash scripts/geojson/run_pipeline.sh data/citylots.json
```

### LiDAR

```bash
bash scripts/lidar/run_pipeline.sh
```

Downloads KITTI data automatically if not present.

## Slurm (FASTA)

```bash
sbatch --export=ALL,TARGET_MIB=200,THREADS=16,MAX_TIME_SECS=1800 \
  scripts/fasta/slurm_pipeline.sbatch
```

## Clean generated outputs

```bash
bash scripts/common/clean_all.sh
```
