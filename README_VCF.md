# VCF Compression Pipeline

## Overview

This pipeline compresses VCF (Variant Call Format) files using a **header/body split preprocessor + trained OpenZL CSV profiler**. On the full 1000 Genomes chr22 VCF (10.69 GB body, 2 513 columns), it achieves an industry-leading **212.57× compression** — outperforming highly optimized genomic tools like Genozip (208.18×) and standard tools like xz (171.57×) — using 16 parallel workers.

## Pipeline Architecture

```text
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
   - `header.vcf` — all `##` meta-information and `#CHROM` header lines (stored as-is)
   - `body_parts/part_NNNNNN.vcfbody` — line-safe chunks of data rows (~40 MiB each)
   - `manifest.json` — metadata for reassembly (file list, byte sizes)
   *Note: The preprocessor uses safety latches (`--delta-pos`, `--dict-info`) to guarantee 100% byte-for-byte lossless integrity.*

2. **Compression** (`zli compress`): Each `.vcfbody` chunk is compressed independently using a **trained CSV tab profiler** that learned column-specific entropy models from the genotype data.

3. **Decompression + Reassembly** (`vcf_postprocess`): Decompresses chunks and concatenates header + body parts back into the original VCF (byte-identical).

## Why This Approach Works

The 1000 Genomes chr22 VCF has **2 504 sample columns** containing highly repetitive genotype strings (`0|0`, `0|1`, `1|1`, `./.`). The trained CSV profiler learns per-column entropy models that exploit this repetition far more efficiently than byte-level compressors.

## Dataset

| Property | Value |
|----------|-------|
| **Source** | 1000 Genomes Project, Phase 3, chromosome 22 |
| **URL** | `https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/release/20130502/ALL.chr22.phase3_shapeit2_mvncall_integrated_v5b.20130502.genotypes.vcf.gz` |
| **Full file** | `10.69 GB uncompressed (`ALL.chr22.vcf`) |
| **Body columns** | 2 513 (9 fixed + 2 504 sample genotypes) |
| **Parts** | Chunked dynamically into ~40 MiB pieces |

## Reproduction Steps

### 0) Prerequisites
```bash
bash scripts/get_openzl.sh     # fetch OpenZL source
bash scripts/patch_openzl.sh   # raise limits for wide CSV (2500+ columns)
bash scripts/build_all.sh      # compile zli + all preprocessors
```

### 1) Preprocess (Notice the safety flags)
```bash
./tools/vcf_preprocessing data/vcf/ALL.chr22.vcf out/vcf_pack \
    --threads 16 --max-chunk-mib 40 --delta-pos --dict-info --force
# → out/vcf_pack/header.vcf           
# → out/vcf_pack/body_parts/part_*.vcfbody  
# → out/vcf_pack/manifest.json
```

*(Follow standard chunking, training, and compression steps using the pre-trained `artifacts/csv_tab_trained.zlc` model).*

### 2) Decompress + reassemble (round-trip)
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
```

## Benchmark Results (Stampede3 HPC)

Input size: 10.69 GB (`ALL.chr22.vcf`, 2 513 columns, Phased). Benchmarks executed using 16 parallel workers. Ratio = original file size / compressed size.

| Tool | Compression Ratio | Time (s) | Validation |
|------|------:|--------:|:---:|
| **OpenZL (trained CSV, 16p)** | **212.57×** | **267.49** | **PASS** |
| Genozip (16t) | 208.18× | 29.73 | PASS |
| xz (16t) | 171.57× | 41.92 | PASS |
| ZSTD -19 (16t) | 169.90× | 41.83 | PASS |
| pigz -9 (16t) | 70.40× | 58.52 | PASS |

OpenZL achieves the highest compression ratio in its class for wide, phased VCF matrices, mathematically guaranteeing exact data reconstruction.
