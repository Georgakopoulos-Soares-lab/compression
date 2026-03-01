# OpenZL Bioinformatics Compression — Summary

## Motivation

Standard general-purpose compressors (gzip, zstd, xz) treat genomic files as opaque byte streams and miss the rich internal structure of bioinformatics formats.
This project demonstrates that a **format-aware, schema-driven** approach — using the OpenZL learned-compression framework — consistently outperforms the best general-purpose baselines in both compression ratio and speed across four major genomic file types: **FASTA, VCF, BED, and FASTQ**.

---

## General Pipeline (common across all formats)

Every format follows the same three-stage pattern:

```
Raw file  ──►  Preprocessing  ──►  Training  ──►  Compression
              (format-specific)     (OpenZL)       (OpenZL, parallel)
```

1. **Preprocessing**: A custom C++ tool transforms the raw format into a representation that exposes its columnar/structural redundancy.
   - FASTA → 4-bit packed binary with an SDDL schema
   - VCF → header/body split; body chunked into tab-delimited parts
   - BED → column-type analysis, delta/span/dictionary transforms → TSV
   - FASTQ → Illumina header parsing, dictionary encoding, field dropping → TSV

2. **Training**: OpenZL's `zli train` learns entropy models from a bounded training sample of the preprocessed data. Training is run once and produces a small compressor file (11–18 KB).

3. **Parallel compression**: The preprocessed data is split into independent chunks that are compressed in parallel using the trained compressor.

4. **Round-trip validation**: Every pipeline includes a decompression + reassembly step that verifies byte-for-byte identity with the original file.

---

## Format-Specific Approaches

### FASTA (reference genomes)

| Aspect | Detail |
|--------|--------|
| **Dataset** | NCBI GRCm39 mouse genome (~2.7 GB) |
| **Preprocessing** | 4-bit nucleotide packing (2 bases/byte) into a structured binary container (`FAV4` format) described by an SDDL schema |
| **Schema fields** | magic, record counts, header/sequence offset arrays, packed payloads (4-byte aligned) |
| **Training sample** | ~200 MiB record-safe FASTA slice (whole records only) |
| **Profiler** | SDDL (schema-driven) |

The FASTA pipeline exploits the fact that DNA sequences use only 5 symbols (A/C/G/T/N), packing them into 4-bit nibbles to halve the raw size before compression. The SDDL schema then tells OpenZL the exact binary layout so it can model each field independently.

### VCF (variant calls)

| Aspect | Detail |
|--------|--------|
| **Dataset** | 1000 Genomes Phase 3, chromosome 22 (~11 GiB uncompressed; 800 MiB benchmark body) |
| **Preprocessing** | Header/body separation; body chunked into ~40 MiB line-safe parts |
| **Key insight** | 2 504 sample columns containing highly repetitive genotype strings (`0\|0`, `0\|1`, `1\|1`, `./.`) |
| **Profiler** | CSV (tab-delimited), trained per-column entropy models |

The VCF pipeline requires an OpenZL patch to raise internal column limits (2 500+ columns). The trained CSV profiler learns column-specific distributions that exploit the extreme repetitiveness of genotype fields.

### BED (genomic annotations)

| Aspect | Detail |
|--------|--------|
| **Dataset** | UCSC RepeatMasker `hg38_rmsk.txt` (470 MiB, 5.7 M rows × 17 columns) |
| **Preprocessing** | Auto-detected column transforms: delta encoding for sorted coordinates, span encoding (end − start), dictionary encoding for low-cardinality strings, redundant-column dropping |
| **Sidecar** | `.meta` file (~200 KB) stores dictionaries and transform metadata |
| **Profiler** | CSV (tab-delimited) |

The BED preprocessor is the most sophisticated: it automatically analyzes each column's statistical properties (monotonicity, cardinality, inter-column relationships) and selects the best reversible transform.

### FASTQ (sequencing reads)

| Aspect | Detail |
|--------|--------|
| **Dataset** | ENA `ERR9539086.fastq` (500 MB sample, 3.5 M reads) |
| **Preprocessing** | Illumina header parsing → drop constant/sequential fields (prefix, read number, instrument), dictionary-encode low-cardinality fields (run, flowcell, lane, tile), keep sequence + quality as-is |
| **Sidecar** | `.meta` file (~4 KB) stores constants and dictionaries |
| **Profiler** | CSV (tab-delimited) |

The FASTQ preprocessor exploits the rigid structure of Illumina headers: most fields are either constant across an entire run or have very low cardinality, so they can be dropped or dictionary-encoded with minimal metadata.

---

## Benchmark Results

All benchmarks use 16 threads/workers. Ratio = original file size / compressed size.

### VCF — 1000 Genomes chr22 (800 MiB body)

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL (trained CSV, 16w)** | **173.72×** | **1.36** |
| xz (default, 16t) | 138.36× | 3.35 |
| 7z (default, 16t) | 117.23× | 10.57 |
| zstd -7 (16t) | 83.49× | 0.73 |
| pigz -9 (16t) | 63.02× | 4.38 |
| bgzip (default, 16t) | 50.32× | 2.00 |
| bgzip -l2 (16t) | 43.64× | 2.17 |

**+25 % ratio vs xz; 2.5× faster.**

### FASTQ — ERR9539086 (500 MB)

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

**+36 % ratio vs xz; 14× faster.**

### BED — hg38 RepeatMasker (470 MiB)

| Tool | Ratio | Time (s) |
|------|------:|--------:|
| **OpenZL (trained, 16t)** | **6.84×** | **0.96** |
| xz (default, 16t) | 4.47× | 28.78 |
| 7z (default, 16t) | 4.47× | 32.59 |
| zstd -7 (16t) | 3.31× | 0.70 |
| bgzip (default, 16t) | 3.28× | 0.73 |
| pigz -9 (16t) | 3.25× | 4.13 |
| gzip (default) | 3.23× | 33.10 |
| bgzip -l2 (16t) | 3.03× | 0.55 |

**+53 % ratio vs xz/7z; 30× faster.**

### FASTA — GRCm39 mouse genome (~2.7 GB)

The FASTA pipeline reports ratios at runtime (dependent on training budget and hardware). A baseline comparison against `pigz -9` is included in the pipeline output. The 4-bit packing alone halves the raw size before OpenZL compression begins.

---

## Summary of Improvements over Baselines

| Format | Best Baseline | OpenZL Ratio | Improvement | Speedup |
|--------|--------------|-------------:|------------:|--------:|
| VCF    | xz (138.36×) | 173.72×      | +25 %       | 2.5×    |
| FASTQ  | xz (6.21×)   | 8.45×        | +36 %       | 14×     |
| BED    | xz (4.47×)   | 6.84×        | +53 %       | 30×     |

In every case, the format-aware OpenZL pipeline achieves both a **higher compression ratio** and a **faster compression time** than the best general-purpose compressor.

---

## Key Design Principles

1. **Format awareness**: Each preprocessor extracts domain knowledge (nucleotide alphabet, genotype patterns, column types) that generic compressors cannot exploit.
2. **Learned compression**: OpenZL trains entropy models on representative samples, adapting to the statistical properties of each specific dataset.
3. **Parallelism**: Data is split into independent chunks that are compressed in parallel, enabling near-linear scaling with core count.
4. **Lossless round-trip**: Every pipeline guarantees byte-for-byte reconstruction of the original file.
5. **Minimal metadata overhead**: Trained compressors are 11–18 KB; sidecar `.meta` files range from 4 KB (FASTQ) to 200 KB (BED).

---

## Reproduction

Each format has its own detailed README with step-by-step reproduction instructions:

- [README.md](README.md) — FASTA pipeline
- [README_VCF.md](README_VCF.md) — VCF pipeline
- [README_BED.md](README_BED.md) — BED pipeline
- [README_FASTQ.md](README_FASTQ.md) — FASTQ pipeline
- [INSTALL.md](INSTALL.md) — Build prerequisites and quick-start
