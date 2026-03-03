# Nyx CLI Reference

Complete reference for all Nyx commands, flags, and options.

---

## Global Usage

```
nyx [OPTIONS] COMMAND [ARGS]...
```

### Global Options

| Flag | Description |
|------|-------------|
| `--version` | Show the installed Nyx version and exit. |
| `--help` | Show the top-level help message and exit. |

### Available Commands

| Command | Description |
|---------|-------------|
| [`compress`](#nyx-compress) | Compress a file (FASTA, FASTQ, VCF, or any file). |
| [`decompress`](#nyx-decompress) | Decompress a Nyx archive (`.zlfasta`, `.zlfastq`, `.zlvcf`, or `.nyx`). |
| [`train`](#nyx-train) | Train a compressor on sample data. |
| [`benchmark`](#nyx-benchmark) | Benchmark compression on a directory of samples. |
| [`inspect`](#nyx-inspect) | Inspect a trained compressor (outputs JSON). |
| [`list-profiles`](#nyx-list-profiles) | List available OpenZL compression profiles. |
| [`build`](#nyx-build) | Build OpenZL and the genomic preprocessor from source. |

---

## `nyx compress`

Unified compression command for all file types. Auto-detects the input format and selects the best compression pipeline.

```
nyx compress <INPUT_FILE> [OPTIONS]
```

### How Auto-Routing Works

When `--mode` is set to `auto` (the default), Nyx inspects the input file and routes it to the best pipeline:

| Input type | Pipeline | Output extension |
|------------|----------|-----------------|
| FASTA (nucleotide) | Lossless packed NXF2 | `.zlfasta` |
| FASTA (protein) | Lossless packed NXFP | `.zlfasta` |
| FASTQ | Lossless CSV decomposition | `.zlfastq` |
| VCF | Lossless header/body CSV split | `.zlvcf` |
| Other | Generic OpenZL compression | `.nyx` |

FASTA files are automatically classified as nucleotide or protein by scanning the first ~10 KB for amino-acid-only characters (`E`, `F`, `I`, `L`, `P`, `Q`). FASTQ files auto-detect Illumina vs generic headers internally for optimal dictionary encoding.

### Compression Modes (`--mode`)

| Mode | When to use | What it does |
|------|-------------|--------------|
| `auto` | Default — picks the best mode automatically. | Lossless for FASTA/FASTQ/VCF, schema or generic for everything else. |
| `lossless` | FASTA/FASTQ/VCF files — byte-exact reconstruction required. | C++ codec (FASTA/FASTQ) or Python codec (VCF) encodes file into streams, compresses with OpenZL, bundles into a `.zlfasta`, `.zlfastq`, or `.zlvcf` container. |
| `schema` | Genomic files with an SDDL schema available. | Preprocessor converts to binary chunks, trains an OpenZL compressor on the SDDL schema, compresses all chunks in parallel, bundles into a `.nyx` archive. |
| `generic` | Any file — quick, no preprocessing. | OpenZL's generic `serial` profile compresses the file as-is into a `.nyx` archive. |
| `inline` | Moderate compression without a separate training step. | OpenZL compresses with inline training. No separate train pass required. Outputs a `.nyx` archive. |

### File Type Override (`--type`)

| Value | Effect |
|-------|--------|
| `auto` | Auto-detect from file extension and content (default). |
| `fasta` | Force FASTA (nucleotide) processing. |
| `protein` | Force protein FASTA processing. |
| `fastq` | Force FASTQ processing. |
| `vcf` | Force VCF processing. |
| `generic` | Force generic (non-genomic) processing. |

File type detection checks content first (magic bytes / structural patterns), then falls back to file extension. Recognized extensions: `.fasta`, `.fa`, `.fna`, `.fas` (FASTA), `.fastq`, `.fq` (FASTQ), `.vcf` (VCF).

### All Flags

#### Output and Behavior

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-o`, `--output` | PATH | Auto-determined | Output file path. If omitted, Nyx appends the appropriate extension (`.zlfasta`, `.zlfastq`, `.zlvcf`, or `.nyx`) to the input filename. |
| `-t`, `--type` | CHOICE | `auto` | Override file type detection. Choices: `auto`, `fasta`, `protein`, `fastq`, `vcf`, `generic`. |
| `--mode` | CHOICE | `auto` | Compression mode. Choices: `auto`, `lossless`, `schema`, `generic`, `inline`. |
| `-f`, `--force` | FLAG | off | Overwrite the output file if it already exists. |
| `-v`, `--verbose` | FLAG | off | Print detailed progress and debugging information. |
| `--benchmark` | FLAG | off | After compressing, run gzip, pigz, and zstd on the same input and print a comparison table. |
| `--keep-temp` | FLAG | off | Keep temporary directories after compression (useful for debugging). |

#### Lossless Pipeline Flags

These flags apply when the compression mode is `lossless` (the default for FASTA, FASTQ, and VCF files).

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--train` | FLAG | off | Train per-stream compressors on the input file before compressing. Improves compression ratio at the cost of additional time. |
| `--models-dir` | DIRECTORY | Built-in models | Directory to store and load trained compressor models. Each stream type gets its own `.compressor` file. |
| `--no-trained` | FLAG | off | Ignore any pre-trained compressor models and fall back to the generic OpenZL profile for all streams. |
| `--group-train` | DIRECTORY | none | Train compressors from a directory of sample files instead of the input file. Useful when you have representative sample data. |

#### Schema Pipeline Flags

These flags apply when the compression mode is `schema`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--sddl` | PATH | Auto from registry | Path to a custom SDDL schema file. Providing this flag forces `--mode schema`. |
| `--trainer` | CHOICE | `greedy` | Training algorithm used by OpenZL. Choices: `greedy`, `full-split`, `bottom-up`. |
| `--no-clustering` | FLAG | off | Skip the clustering step during OpenZL training. |

#### Performance Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--threads` | INTEGER | `1` | Number of threads for the C++ encoding/preprocessing step. |
| `--train-threads` | INTEGER | CPU count | Number of threads for the OpenZL training step. |
| `--max-time-secs` | INTEGER | `1800` | Maximum time budget (in seconds) for the OpenZL training step. Training stops when this limit is reached and returns the best compressor found so far. |
| `--compress-jobs` | INTEGER | CPU count | Number of parallel jobs for compressing chunks. Each job runs an independent `zli compress` process. |
| `--train-sample-mib` | INTEGER | `200` | Size of the training sample in MiB. For large input files, Nyx extracts a record-safe sample of approximately this size for training. |

### Examples

```bash
# Auto-detect FASTA and compress losslessly
nyx compress genome.fasta                        # → genome.fasta.zlfasta

# Auto-detect FASTQ and compress losslessly
nyx compress reads.fastq                         # → reads.fastq.zlfastq

# Protein FASTA (auto-detected from content)
nyx compress proteins.fasta                      # → proteins.fasta.zlfasta

# Protein FASTA (explicitly specified)
nyx compress proteins.fasta --type protein       # → proteins.fasta.zlfasta

# Train compressors first for better ratio
nyx compress genome.fasta --train

# Train from a directory of representative samples
nyx compress genome.fasta --train --group-train /path/to/samples/

# Schema-aware compression (uses SDDL schema)
nyx compress genome.fasta --mode schema          # → genome.fasta.nyx

# Custom SDDL schema
nyx compress data.bin --sddl my_format.sddl      # → data.bin.nyx

# Generic compression for any file
nyx compress data.bin                            # → data.bin.nyx
nyx compress data.bin --mode generic             # explicit generic mode

# Inline training (no separate train step)
nyx compress data.bin --mode inline              # → data.bin.nyx

# Custom output path
nyx compress genome.fasta -o /tmp/compressed.zlfasta

# Overwrite existing output
nyx compress genome.fasta -f

# Auto-detect VCF and compress losslessly
nyx compress variants.vcf                        # → variants.vcf.zlvcf

# VCF with training (learns per-column compression)
nyx compress variants.vcf --train                # trains CSV compressor, then compresses

# Compress and compare against gzip, pigz, zstd
nyx compress genome.fasta --benchmark

# Performance tuning
nyx compress genome.fasta --threads 8 --compress-jobs 16
nyx compress genome.fasta --train --train-threads 16 --max-time-secs 3600
```

### Benchmark Output

When `--benchmark` is used, Nyx compresses the file first, then runs each installed competitor on the original file and prints a comparison table:

```
========================================================================
  COMPRESSION BENCHMARK RESULTS
========================================================================
  Compressor    Compressed    Ratio   Savings       Speed      Time
------------------------------------------------------------------------
 *nyx             594.5 MiB   4.43x    77.4%    23.2 MB/s    1m54s
  zstd -9         712.8 MiB   3.69x    72.9%    12.5 MB/s    3m28s
  pigz -9         748.2 MiB   3.51x    71.5%    18.3 MB/s    2m31s
  zstd -3         833.0 MiB   3.16x    68.3%   112.4 MB/s      6.4s
  gzip -1         939.1 MiB   2.81x    64.4%    45.2 MB/s     20.7s
========================================================================
  Original size: 2.6 GiB
  * = nyx
```

Competitors are auto-detected. If a tool is not installed, it is skipped. Supported competitors: `gzip`, `pigz`, `zstd`.

---

## `nyx decompress`

Unified decompression command for all Nyx container formats. Auto-detects the format from magic bytes in the first 8 bytes of the file.

```
nyx decompress <INPUT_FILE> [OPTIONS]
```

### Format Auto-Detection

| Container | Magic bytes | Output |
|-----------|------------|--------|
| `.zlfasta` | `ZLFASTA\0` (8 bytes) | Byte-identical FASTA file |
| `.zlfastq` | `ZLFASTQ\0` (8 bytes) | Byte-identical FASTQ file |
| `.zlvcf` | `ZLVCF\0\0\0` (8 bytes) | Byte-identical VCF file |
| `.nyx` | tar archive (no specific magic) | Directory of decompressed binary chunks |

For lossless containers (`.zlfasta`, `.zlfastq`, `.zlvcf`), decompression produces a byte-identical copy of the original file. The output filename is derived by stripping the container extension (e.g., `genome.fasta.zlfasta` becomes `genome.fasta`, `variants.vcf.zlvcf` becomes `variants.vcf`).

For `.nyx` archives, decompression outputs binary chunks into a directory named `<input>_decompressed/`. Postprocessing from binary chunks back to the original text format is planned for a future release.

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-o`, `--output` | PATH | Auto-determined | Output file path (for lossless) or output directory (for `.nyx`). If omitted, derived from the input filename. |
| `-f`, `--force` | FLAG | off | Overwrite existing output file or directory. |
| `-v`, `--verbose` | FLAG | off | Print detailed progress and debugging information. |
| `--keep-temp` | FLAG | off | Keep temporary directories after decompression. |

### Examples

```bash
# Decompress a FASTA lossless archive
nyx decompress genome.fasta.zlfasta              # → genome.fasta

# Decompress a FASTQ lossless archive
nyx decompress reads.fastq.zlfastq               # → reads.fastq

# Decompress a VCF lossless archive
nyx decompress variants.vcf.zlvcf                # → variants.vcf

# Decompress with custom output path
nyx decompress reads.fastq.zlfastq -o reads.fastq

# Decompress a .nyx archive
nyx decompress data.nyx                          # → data_decompressed/

# Overwrite existing output
nyx decompress genome.fasta.zlfasta -f
```

---

## `nyx train`

Train a compressor on a directory of sample data. This is a direct passthrough to OpenZL's `zli train` command. Any unrecognized flags are forwarded directly to `zli`.

```
nyx train <SAMPLE_DIR> [OPTIONS]
```

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-o`, `--output` | PATH | **required** | Output path for the trained compressor file (`.compressor` or `.model`). |
| `-p`, `--profile` | TEXT | none | OpenZL compression profile to use (e.g., `sddl`, `serial`). |
| `--profile-arg` | TEXT | none | Argument for the profile. For `sddl`, this is the path to the SDDL schema file. |
| `-c`, `--compressor` | PATH | none | Path to an existing compressor to retrain (continue training). |
| `--threads` | INTEGER | none | Number of threads for training. |
| `--max-time-secs` | INTEGER | none | Maximum training time in seconds. Training stops at this limit and returns the best compressor found. |
| `--use-all-samples` | FLAG | off | Use all sample files, ignoring OpenZL's default size limits. |
| `--no-ace-successors` | FLAG | off | Disable ACE (Adaptive Compression Ensemble) successor graphs. Recommended for robustness when compressing unseen data. |
| `--no-clustering` | FLAG | off | Skip the clustering step during training. |
| `--trainer` | CHOICE | `greedy` | Training algorithm. Choices: `greedy`, `full-split`, `bottom-up`. |
| `-f`, `--force` | FLAG | off | Overwrite the output file if it already exists. |
| `-v`, `--verbose` | FLAG | off | Verbose output. |

Any additional flags not listed above are passed through directly to `zli train`.

### Examples

```bash
# Train with an SDDL schema
nyx train chunks/ -o compressor.model -p sddl --profile-arg schema.sddl

# Train with time and thread limits
nyx train chunks/ -o compressor.model -p sddl --profile-arg schema.sddl \
  --threads 16 --max-time-secs 1800

# Train with all samples and no ACE successors
nyx train chunks/ -o compressor.model -p sddl --profile-arg schema.sddl \
  --use-all-samples --no-ace-successors

# Retrain an existing compressor
nyx train new_chunks/ -o improved.model -c compressor.model

# Use bottom-up trainer
nyx train chunks/ -o compressor.model -p sddl --profile-arg schema.sddl \
  --trainer bottom-up

# Pass extra flags directly to zli
nyx train chunks/ -o compressor.model -p sddl --profile-arg schema.sddl \
  --some-zli-flag value
```

---

## `nyx benchmark`

Benchmark compression on a directory of sample files. This is a passthrough to OpenZL's `zli benchmark` command. All unrecognized flags are forwarded directly to `zli`.

```
nyx benchmark <INPUT_DIR> [OPTIONS]
```

> **Note:** This command benchmarks OpenZL's internal compression performance on pre-processed binary samples. For comparing Nyx against external compressors (gzip, pigz, zstd) on original files, use `nyx compress --benchmark` instead.

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-v`, `--verbose` | FLAG | off | Verbose output. |

All other flags are passed through directly to `zli benchmark`. Use `--` to separate Nyx flags from `zli` flags if needed.

### Examples

```bash
# Benchmark on a directory of samples
nyx benchmark chunks/

# Benchmark with a specific profile (passed through to zli)
nyx benchmark chunks/ -p serial --output-csv results.csv
```

---

## `nyx inspect`

Inspect a trained compressor file and output its metadata as JSON. This is a passthrough to OpenZL's `zli inspect` command.

```
nyx inspect <COMPRESSOR_FILE> [OPTIONS]
```

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-v`, `--verbose` | FLAG | off | Verbose output. |

All other flags are passed through directly to `zli inspect`.

### Examples

```bash
# Inspect a trained compressor
nyx inspect compressor.model

# Verbose inspection
nyx inspect compressor.model -v
```

---

## `nyx list-profiles`

List all available OpenZL compression profiles. Profiles determine how OpenZL structures its compression graph.

```
nyx list-profiles [OPTIONS]
```

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-v`, `--verbose` | FLAG | off | Show detailed information about each profile. |

### Examples

```bash
# List all profiles
nyx list-profiles

# List with details
nyx list-profiles -v
```

---

## `nyx build`

Build OpenZL and the genomic preprocessor from source. This command must be run once after installing Nyx before any compression or decompression commands can be used.

```
nyx build [OPTIONS]
```

### What It Does

1. Clones the OpenZL repository (pinned to a tested commit) into `nyx/openzl/`.
2. Compiles the `zli` binary (the OpenZL CLI).
3. Compiles the C++ codecs (`fasta_codec`, `fastq_codec`) into `nyx/bin/`.
4. Compiles the `genomic_preprocessor` binary into `nyx/bin/`.

All build artifacts (`nyx/openzl/` and `nyx/bin/`) are gitignored.

### All Flags

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `-j`, `--jobs` | INTEGER | CPU count | Number of parallel build jobs for `make`. |
| `-v`, `--verbose` | FLAG | off | Show full build output from the compiler. |

### Prerequisites

- **C++17 compiler** (g++ or clang++)
- **Git** (to clone OpenZL)
- **Make** (to build OpenZL)
- **pthread** support

```bash
# macOS
xcode-select --install

# Linux (Debian/Ubuntu)
sudo apt install build-essential git
```

### Examples

```bash
# Build with auto-detected parallelism
nyx build

# Build with 8 parallel jobs
nyx build -j 8

# Build with verbose output
nyx build -v
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NYX_ZLI` | Override the path to the `zli` binary. By default, Nyx looks for `zli` inside `nyx/openzl/`. |
| `NYX_PREPROCESSOR` | Override the path to the `genomic_preprocessor` binary. By default, Nyx looks inside `nyx/bin/`. |

---

## Output Formats

### `.zlfasta` — Lossless FASTA Container

A binary container with magic bytes `ZLFASTA\0`. Contains compressed binary streams that, when decompressed, reconstruct the original FASTA file byte-for-byte.

Produced by: `nyx compress <file.fasta>` (default lossless mode).
Decompressed by: `nyx decompress <file.zlfasta>`.

### `.zlfastq` — Lossless FASTQ Container

A binary container with magic bytes `ZLFASTQ\0`. Contains compressed binary streams from CSV/TSV decomposition of FASTQ records that, when decompressed, reconstruct the original FASTQ file byte-for-byte.

Produced by: `nyx compress <file.fastq>` (default lossless mode).
Decompressed by: `nyx decompress <file.zlfastq>`.

### `.zlvcf` — Lossless VCF Container

A binary container with magic bytes `ZLVCF\0\0\0`. Contains a compressed header and body TSV parts from column-aware CSV decomposition of VCF data rows. Decompression reconstructs the original VCF file byte-for-byte.

Container layout: magic (8 bytes) + version (U32LE) + num_entries (U32LE) + entry directory + entry data + CRC32 checksum.

Produced by: `nyx compress <file.vcf>` (default lossless mode).
Decompressed by: `nyx decompress <file.zlvcf>`.

### `.nyx` — Archive Container

A standard tar archive containing:

```
manifest.json           # Metadata (mode, filetype, schema, chunk count)
compressor.model        # Trained compressor (if applicable)
chunks/
  chunk_00000.*.zl      # Compressed binary chunks
  chunk_00001.*.zl
  ...
```

You can inspect any `.nyx` file with standard `tar`:

```bash
tar tf genome.fasta.nyx
```

Produced by: `nyx compress --mode schema`, `nyx compress --mode generic`, `nyx compress --mode inline`.
Decompressed by: `nyx decompress <file.nyx>`.

---

## Supported File Types

| Format | Extensions | Content Detection | Default Mode | Output |
|--------|-----------|-------------------|-------------|--------|
| FASTA (nucleotide) | `.fasta`, `.fa`, `.fna`, `.fas` | First line starts with `>` | Lossless (NXF2 packed) | `.zlfasta` |
| FASTA (protein) | `.fasta`, `.fa`, `.fna`, `.fas` | First line starts with `>`, contains amino-acid-only characters | Lossless (NXFP packed) | `.zlfasta` |
| FASTQ | `.fastq`, `.fq` | First line starts with `@`, third line starts with `+` | Lossless (CSV decomposition) | `.zlfastq` |
| VCF | `.vcf` | Starts with `##fileformat=VCF` | Lossless (header/body CSV split) | `.zlvcf` |
| Generic | Any other | Fallback | Generic serial | `.nyx` |

---

## Quick Start

```bash
# 1. Install the Python CLI
pip install -e ./nyx

# 2. Build OpenZL and codecs from source
nyx build

# 3. Compress
nyx compress genome.fasta                     # → genome.fasta.zlfasta
nyx compress reads.fastq                      # → reads.fastq.zlfastq
nyx compress variants.vcf                     # → variants.vcf.zlvcf
nyx compress data.bin                         # → data.bin.nyx

# 4. Decompress
nyx decompress genome.fasta.zlfasta           # → genome.fasta (byte-identical)
nyx decompress reads.fastq.zlfastq            # → reads.fastq  (byte-identical)
nyx decompress variants.vcf.zlvcf             # → variants.vcf  (byte-identical)

# 5. Check version
nyx --version
```
