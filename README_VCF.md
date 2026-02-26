# VCF Compression Pipeline

## Overview

This pipeline compresses VCF (Variant Call Format) files using a **header/body split
preprocessor + trained OpenZL CSV profiler**. On the 1000 Genomes chr22 VCF (800 MiB body,
2 513 columns), it achieves **173.72× compression** — 25 % better than xz (138.36×) —
in under 2 seconds with 16 parallel workers.

## Pipeline Architecture

```
VCF                                                 .vcfbody.zl (one per part)
 │                                                       │
 ├── vcf_preprocessing ──► header.vcf                    │
 │                     ──► body_parts/part_*.vcfbody     │
 │                     ──► manifest.json                 │
 │                                                       │
 └── zli compress (--compressor csv_tab_trained.zlc) ────┘
                                                         │
 Reassembled VCF ◄── vcf_postprocess ◄── zli decompress ◄┘
```

1. **Preprocessing** (`vcf_preprocessing`): Separates the VCF into:
   - `header.vcf` — all `##` meta-information and `#CHROM` header lines (stored as-is, ~36 KB)
   - `body_parts/part_NNNNNN.vcfbody` — line-safe chunks of data rows (~40 MiB each)
   - `manifest.json` — metadata for reassembly (file list, byte sizes)

2. **Compression** (`zli compress`): Each `.vcfbody` chunk is compressed independently
   using a **trained CSV tab profiler** that learned column-specific entropy models
   from the genotype data.

3. **Decompression + Reassembly** (`vcf_postprocess`): Decompresses chunks and
   concatenates header + body parts back into the original VCF (byte-identical).

## Why This Approach Works

The 1000 Genomes chr22 VCF has **2 504 sample columns** containing highly repetitive
genotype strings (`0|0`, `0|1`, `1|1`, `./.`). The trained CSV profiler learns per-column
entropy models that exploit this repetition far more efficiently than byte-level compressors.

The 40 MiB chunk size keeps individual compression jobs within OpenZL's memory limits and
enables parallel processing.

## Dataset

| Property | Value |
|----------|-------|
| **Source** | 1000 Genomes Project, Phase 3, chromosome 22 |
| **URL** | `https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz` |
| **Full file** | ~11 GiB uncompressed |
| **Benchmark body** | 838 862 116 bytes (800 MiB), first 800 MiB of data rows |
| **Body columns** | 2 513 (9 fixed + 2 504 sample genotypes) |
| **Parts** | 21 chunks × ~40 MiB each |

## Reproduction Steps

### 0) Prerequisites

```bash
bash scripts/get_openzl.sh     # fetch OpenZL source
bash scripts/patch_openzl.sh   # raise limits for wide CSV (2500+ columns)
bash scripts/build_all.sh      # compile zli + all preprocessors
```

### 1) Download the VCF

```bash
mkdir -p data/vcf
wget -O data/vcf/ALL.chr22.vcf.gz \
  "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz"
gunzip data/vcf/ALL.chr22.vcf.gz
```

Or use the helper script:

```bash
bash scripts/vcf/download_vcf.sh
```

### 2) Preprocess: split header + chunk body (~40 MiB parts)

```bash
./tools/vcf_preprocessing data/vcf/ALL.chr22.vcf out/vcf_pack \
    --threads 16 --target-mib 800 --max-chunk-mib 40 --force
# → out/vcf_pack/header.vcf           (~36 KB)
# → out/vcf_pack/body_parts/part_*.vcfbody  (21 parts, ~40 MiB each)
# → out/vcf_pack/manifest.json
```

### 3) Train the CSV tab profiler (optional — pre-trained `.zlc` is included)

Use a few body parts as training data:

```bash
mkdir -p out/vcf_train
cp out/vcf_pack/body_parts/part_000000.vcfbody out/vcf_train/

./openzl/zli train out/vcf_train/ \
    --profile csv --profile-arg $'\t' \
    --output artifacts/csv_tab_trained.zlc \
    --force --threads 16 --use-all-samples
```

The trained compressor is ~18 KB. A pre-trained `artifacts/csv_tab_trained.zlc` is included
in this repository.

### 4) Compress all parts

```bash
bash scripts/vcf/vcf_compress_parts.sh \
    --pack out/vcf_pack \
    --compressor artifacts/csv_tab_trained.zlc \
    --zli ./openzl/zli \
    --jobs 16
```

Or manually:

```bash
mkdir -p out/vcf_pack/zl
for f in out/vcf_pack/body_parts/*.vcfbody; do
    ./openzl/zli compress "$f" \
        --compressor artifacts/csv_tab_trained.zlc \
        --output "out/vcf_pack/zl/$(basename "$f").zl" --force &
done
wait
```

### 5) Verify compression ratio

```bash
RAW=0; for f in out/vcf_pack/body_parts/*.vcfbody; do RAW=$((RAW + $(stat -c%s "$f"))); done
ZL=0;  for f in out/vcf_pack/zl/*.zl; do ZL=$((ZL + $(stat -c%s "$f"))); done
echo "Ratio: $(echo "scale=2; $RAW/$ZL" | bc)x"
# Expected: ~173.72x
```

### 6) Decompress + reassemble (round-trip)

```bash
# Decompress each part
for f in out/vcf_pack/zl/*.zl; do
    base=$(basename "$f" .zl)
    ./openzl/zli decompress "$f" \
        --output "out/vcf_pack/body_parts/${base}.dec" --force &
done
wait

# Reassemble header + body parts into a single VCF
./tools/vcf_postprocess out/vcf_pack out/vcf_reconstructed.vcf \
    --threads 16 --chunk-suffix .dec

# Verify round-trip correctness
head -c 838862116 <(tail -n +<header_lines+1> data/vcf/ALL.chr22.vcf) | \
    diff - <(tail -n +<header_lines+1> out/vcf_reconstructed.vcf)
```

## Benchmark Results

Input size: 838 862 116 bytes (800 MiB VCF body). Ratio = input / output.

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL (trained CSV, 16w)** | **173.72×** | **1.36** |
| xz (default, 16t) | 138.36× | 3.35 |
| 7z (default, 16t) | 117.23× | 10.57 |
| zstd -7 (16t) | 83.49× | 0.73 |
| pigz -9 (16t) | 63.02× | 4.38 |
| bgzip (default, 16t) | 50.32× | 2.00 |
| bgzip -l2 (16t) | 43.64× | 2.17 |

OpenZL achieves **25 % higher compression** than xz while being more than **2× faster**.

## Files in This Repository

| File | Description |
|------|-------------|
| `tools/vcf_preprocessing.cpp` | Header/body split + parallel chunking |
| `tools/vcf_postprocess.cpp` | Reassembly from decompressed chunks |
| `scripts/vcf/vcf_compress_parts.sh` | Parallel compression wrapper script |
| `scripts/vcf/download_vcf.sh` | Download helper |
| `artifacts/csv_tab_trained.zlc` | Pre-trained CSV tab profiler (~18 KB) |
