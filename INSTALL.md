# Installation / Setup

This folder is a self-contained demo for the **FASTA Packed (FAV4)** pipeline:

1) download a FASTA
2) create a ~200MiB training sample (record-safe)
3) preprocess to `.fasta_packed.bin` chunks (matches the SDDL schema)
4) train an OpenZL compressor on those chunks
5) compress / decompress and validate

## Prerequisites

- `git`
- `make`
- C/C++ toolchain with C++17 support (`g++`)
- `python3`
- `wget`
- `gzip`

## One-command build + run

From this directory:

```bash
bash scripts/run_demo.sh
```

## Train on ~200MiB, then test on full FASTA

This is the workflow you described (bounded training time, then full-file test):

```bash
bash scripts/run_train_250_and_test_full.sh
```

Defaults:

- `TARGET_MIB=200`
- `THREADS=16`
- `MAX_TIME_SECS=1800` (≈ 30 minutes)

Example override:

```bash
TARGET_MIB=200 THREADS=16 MAX_TIME_SECS=1800 bash scripts/run_train_250_and_test_full.sh
```

### Slurm (recommended on clusters)

```bash
sbatch --export=ALL,TARGET_MIB=200,THREADS=16,MAX_TIME_SECS=1800 scripts/slurm_train_250_and_test_full.sbatch
```

## Manual steps

```bash
# 1) Fetch OpenZL (pinned commit)
bash scripts/get_openzl.sh

# 2) Build OpenZL + build the chunker tool
bash scripts/build_all.sh

# 3) Download FASTA
bash scripts/download_fasta.sh

# 4) Create a ~200MiB training FASTA
python3 scripts/make_train_sample.py --in data/*.fna --out out/train_200MiB.fasta --target-mib 200

# 5) Preprocess to schema-matching chunks
./tools/biocompress_preprocessor out/train_200MiB.fasta chunks 16 fasta_packed

# 6) Train
./openzl/zli train chunks --profile sddl --profile-arg schemas/fasta_packed.sddl \
  --output artifacts/fasta_packed_200MiB.compressor --force --threads 16 --use-all-samples

# 7) Compress/decompress/validate (see scripts/run_demo.sh)
```

## Notes

- If you want to use a different FASTA, set `FASTA_URL` (and optionally `FASTA_OUT`) when running `scripts/download_fasta.sh`.
- The OpenZL revision is pinned in `scripts/get_openzl.sh` for reproducibility.
