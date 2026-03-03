# Nyx

```text
 ███╗   ██╗██╗   ██╗██╗  ██╗
 ████╗  ██║╚██╗ ██╔╝╚██╗██╔╝
 ██╔██╗ ██║ ╚████╔╝  ╚███╔╝
 ██║╚██╗██║  ╚██╔╝   ██╔██╗
 ██║ ╚████║   ██║   ██╔╝ ██╗
 ╚═╝  ╚═══╝   ╚═╝   ╚═╝  ╚═╝
```

Nyx is a CLI tool that wraps [OpenZL](https://github.com/facebook/openzl) to provide seamless, schema-aware compression for genomic data formats (FASTA, FASTQ, VCF) alongside generic compression for any file type.

It orchestrates the full pipeline — file type detection, preprocessing, training, parallel compression, and archive bundling — in a single command.

## Prerequisites

- **Python 3.8+**
- **C++17 compiler** (g++ or clang++)
- **Git** (to clone OpenZL source)
- **Make** (to build OpenZL)
- **pthread** support

### macOS

```bash
xcode-select --install   # Provides clang++, git, make
```

### Linux (Debian/Ubuntu)

```bash
sudo apt install build-essential git python3 python3-pip
```

## Virtual Environment Setup

Nyx requires **Python 3.8**. We recommend using [pyenv](https://github.com/pyenv/pyenv) to install it and creating a dedicated virtual environment.

### Install Python 3.8 with pyenv

```bash
# Install pyenv (skip if already installed)
# macOS
brew install pyenv

# Linux
curl https://pyenv.run | bash
```

```bash
# Install Python 3.8
pyenv install 3.8.20
```

### Create and activate the virtual environment

```bash
# Create a venv inside the nyx/ directory using the pyenv-managed Python 3.8
$(pyenv prefix 3.8.20)/bin/python3.8 -m venv nyx/.venv

# Activate it
source nyx/.venv/bin/activate

# Verify
python --version   # Should print Python 3.8.x
```

> **Tip:** You will need to run `source nyx/.venv/bin/activate` each time you open a new terminal session before using `nyx`.

## Installation

```bash
# 1. Install the Python CLI
pip install -e ./nyx

# 2. Build OpenZL and the genomic preprocessor from source
nyx build
```

The `nyx build` command:
- Clones OpenZL (pinned to a tested commit) into `nyx/openzl/`
- Compiles the `zli` binary
- Compiles the `genomic_preprocessor` binary into `nyx/bin/`

Both `nyx/openzl/` and `nyx/bin/` are gitignored — only source code is tracked.

## Quick Start

```bash
# Lossless compression (auto-detects FASTA/FASTQ/VCF, byte-exact reconstruction)
nyx compress genome.fasta                     # → genome.fasta.zlfasta
nyx compress reads.fastq                      # → reads.fastq.zlfastq
nyx compress variants.vcf                     # → variants.vcf.zlvcf
nyx decompress genome.fasta.zlfasta           # → genome.fasta (byte-identical)
nyx decompress variants.vcf.zlvcf             # → variants.vcf (byte-identical)

# Schema-aware compression (training-based, higher ratio)
nyx compress genome.fasta --mode schema       # → genome.fasta.nyx

# Generic compression (any file)
nyx compress data.bin                         # → data.bin.nyx

# Check version
nyx --version
```

## Commands

### `nyx compress`

**Unified compression** for all file types. Auto-detects the input format and selects the best pipeline.

```
nyx compress <file> [OPTIONS]
```

**Auto-routing (--mode auto, default):**

| Input type | Pipeline | Output extension |
|------------|----------|-----------------|
| FASTA (nucleotide) | Lossless packed NXF2 | `.zlfasta` |
| FASTA (protein) | Lossless packed NXFP | `.zlfasta` |
| FASTQ | Lossless CSV decomposition | `.zlfastq` |
| VCF | Lossless header/body CSV split | `.zlvcf` |
| Other | Generic OpenZL compression | `.nyx` |

FASTA auto-detects nucleotide vs protein. FASTQ auto-detects Illumina vs generic headers for optimal dictionary encoding. VCF splits header from body rows and compresses body parts using OpenZL's CSV profile with tab delimiter.

**Options:**

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Output file path (auto-determined by pipeline) |
| `-t, --type TYPE` | File type: `auto`, `fasta`, `protein`, `fastq`, `vcf`, `generic` (default: auto) |
| `--mode MODE` | Compression mode: `auto`, `lossless`, `schema`, `generic`, `inline` (default: auto) |
| `--train` | Train compressors before compressing (improves ratio) |
| `--models-dir PATH` | Directory for trained compressor models |
| `--no-trained` | Ignore trained compressors, use generic profile |
| `--group-train DIR` | Train from directory of sample files |
| `--sddl PATH` | Custom SDDL schema (forces schema mode) |
| `--threads N` | Encoding/preprocessing threads (default: 1) |
| `--train-threads N` | OpenZL training threads (default: CPU count) |
| `--max-time-secs N` | Training time limit in seconds (default: 1800) |
| `--compress-jobs N` | Parallel compression jobs (default: CPU count) |
| `--train-sample-mib N` | Training sample size in MiB (default: 200) |
| `--trainer ALGO` | Training algorithm: `greedy`, `full-split`, or `bottom-up` (schema mode) |
| `--no-clustering` | Skip clustering during training (schema mode) |
| `--benchmark` | Run competitor benchmarks (gzip, pigz, zstd) |
| `--keep-temp` | Keep temporary directories for debugging |
| `-v, --verbose` | Verbose output |
| `-f, --force` | Overwrite existing output |

**Compression Modes:**

| Mode | When to use | What it does |
|------|-------------|--------------|
| `auto` | Default — picks the best mode automatically | Lossless for FASTA/FASTQ/VCF, generic for other |
| `lossless` | FASTA/FASTQ/VCF — byte-exact reconstruction | C++ codec (FASTA/FASTQ) or Python codec (VCF) → compressed streams → `.zlfasta`/`.zlfastq`/`.zlvcf` |
| `schema` | Genomic files with SDDL schemas | Preprocessor → SDDL training → parallel compression → `.nyx` |
| `generic` | Any file, quick compression | OpenZL's generic `serial` profile → `.nyx` |
| `inline` | Moderate compression without separate training | OpenZL inline training → `.nyx` |

**Examples:**

```bash
# Auto-detect FASTA → lossless compression
nyx compress genome.fasta

# Protein FASTA (auto-detected or explicit)
nyx compress proteins.fasta                         # auto-detects protein
nyx compress proteins.fasta --type protein          # explicit

# FASTQ with training (improves ratio)
nyx compress reads.fastq --train

# VCF compression (auto-detected)
nyx compress variants.vcf                          # → variants.vcf.zlvcf
nyx compress variants.vcf --train                  # train CSV compressor first

# Schema-aware compression
nyx compress genome.fasta --mode schema

# Custom SDDL schema
nyx compress data.bin --sddl my_format.sddl

# Generic compression (fast, no preprocessing)
nyx compress arbitrary_file.dat --mode generic

# Compress and benchmark against gzip, pigz, zstd
nyx compress genome.fasta --benchmark

# Group training from multiple files
nyx compress genome.fasta --train --group-train /path/to/samples/
```

### Benchmarking Competitors

Use the `--benchmark` flag on any compress command to automatically run the same input through generic compressors and print a comparison table:

```bash
nyx compress genome.fasta --benchmark
```

This compresses with nyx first, then runs gzip -1, pigz -9, zstd -3, and zstd -9 on the original file, producing output like:

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

Competitors are auto-detected. If a tool is not installed, it is skipped with a note. To install them:

```bash
# macOS
brew install pigz zstd

# Linux (Debian/Ubuntu)
sudo apt install pigz zstd
```

### `nyx decompress`

**Unified decompression** for all Nyx archives. Auto-detects container format from magic bytes.

```
nyx decompress <file> [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Output file/directory path (auto-determined) |
| `-v, --verbose` | Verbose output |
| `-f, --force` | Overwrite existing output |
| `--keep-temp` | Keep temporary directories |

**Auto-detection:**

| Container | Magic bytes | Output |
|-----------|------------|--------|
| `.zlfasta` | `ZLFASTA\0` | Byte-identical FASTA file |
| `.zlfastq` | `ZLFASTQ\0` | Byte-identical FASTQ file |
| `.zlvcf` | `ZLVCF\0\0\0` | Byte-identical VCF file |
| `.nyx` | tar archive | Decompressed binary chunks |

```bash
nyx decompress genome.fasta.zlfasta                # → genome.fasta
nyx decompress reads.fastq.zlfastq -o reads.fastq  # → reads.fastq
nyx decompress variants.vcf.zlvcf                  # → variants.vcf
nyx decompress data.nyx                            # → data_decompressed/
```

> **Note:** `.nyx` decompression currently outputs binary chunks. Postprocessing (binary chunks back to original text format) will be added in a future release.

### `nyx train`

Train a compressor on sample data. Passes through to OpenZL's training engine.

```bash
nyx train <sample_dir> -o compressor.model -p sddl --profile-arg schema.sddl
```

All standard OpenZL training flags are supported (`--threads`, `--max-time-secs`, `--use-all-samples`, `--no-ace-successors`, etc.). Unknown flags are forwarded directly to `zli`.

### `nyx benchmark`

Benchmark compression on a directory of samples.

```bash
nyx benchmark <input_dir> -p serial --output-csv results.csv
```

### `nyx inspect`

Inspect a trained compressor (outputs JSON).

```bash
nyx inspect compressor.model
```

### `nyx list-profiles`

List all available OpenZL compression profiles.

```bash
nyx list-profiles
```

### `nyx build`

Build OpenZL and the genomic preprocessor from source.

```bash
nyx build           # Auto-detect CPU count
nyx build -j 8      # Use 8 parallel jobs
```

## Progress Bars

All compression operations display tqdm progress bars:

- **Single-step operations** (preprocessing, sample creation) show elapsed time
- **Training** shows a time-budget progress bar based on `--max-time-secs`
- **Chunk compression** shows per-chunk progress with completion percentage and ETA
- **Benchmarking** shows results as each competitor completes

## Architecture

**Lossless pipeline (FASTA/FASTQ, default):**

```
nyx compress genome.fasta
    |
    v
[detect file type] --> "fasta" (nucleotide)
    |
    v
[encode packed] --> NXF2 binary chunks (C++ fasta_codec)
    |
    v
[compress chunks] --> .zl files (parallel, OpenZL SDDL compressor)
    |
    v
[bundle container] --> genome.fasta.zlfasta
```

**Lossless pipeline (VCF):**

```
nyx compress variants.vcf
    |
    v
[detect file type] --> "vcf" (##fileformat=VCF)
    |
    v
[vcf_codec.encode()] --> header.vcf + part_NNN.tsv chunks (Python)
    |
    v
[compress parts] --> .zl files (parallel, OpenZL CSV compressor)
    |
    v
[bundle container] --> variants.vcf.zlvcf
```

**Schema pipeline (--mode schema):**

```
nyx compress genome.fasta --mode schema
    |
    v
[detect + preprocess] --> binary chunks (FAV4 format)
    |
    v
[train compressor] --> trained model (via zli train + SDDL schema)
    |
    v
[compress chunks] --> .zl files (parallel)
    |
    v
[bundle archive] --> genome.fasta.nyx (tar: manifest + compressor + chunks)
```

Nyx is an **orchestrator**. All heavy computation runs in C++ via:
- `zli` — OpenZL's CLI for training, compression, and decompression
- `fasta_codec` / `fastq_codec` — lossless binary stream codecs
- `genomic_preprocessor` — converts text genomic formats to binary chunk format (schema pipeline)

## .nyx Archive Format

A `.nyx` file is a tar archive containing:

```
manifest.json         # Metadata (mode, filetype, schema, chunk count)
compressor.model      # Trained compressor (if applicable)
chunks/
  chunk_00000.*.zl    # Compressed binary chunks
  chunk_00001.*.zl
  ...
```

You can inspect any `.nyx` file with standard `tar`:

```bash
tar tf genome.fasta.nyx
```

## Supported File Types

| Format | Detection | Default mode | Container |
|--------|-----------|-------------|-----------|
| FASTA (.fasta, .fa, .fna) | Content (`>`) + extension | Lossless (NXF2/NXFP packed) | `.zlfasta` |
| FASTQ (.fastq, .fq) | Content (`@...+`) + extension | Lossless (CSV decomposition) | `.zlfastq` |
| VCF (.vcf) | Content (`##fileformat=VCF`) | Lossless (header/body CSV split) | `.zlvcf` |
| Generic (any other) | Fallback | Generic serial | `.nyx` |

## Adding a New File Type

1. Add preprocessing support in `tools/genomic_preprocessor.cpp`
2. Create an SDDL schema in `schemas/`
3. Add an entry to `SCHEMA_REGISTRY` in `nyx/core/config.py`
4. Rebuild: `nyx build`

## Environment Variables

| Variable | Description |
|----------|-------------|
| `NYX_ZLI` | Override path to `zli` binary |
| `NYX_PREPROCESSOR` | Override path to `genomic_preprocessor` binary |

## Directory Structure

```
nyx/
├── pyproject.toml              # Package configuration
├── README.md                   # This file
├── .gitignore                  # Ignores openzl/, bin/, __pycache__/
├── schemas/                    # SDDL schema files
│   └── fasta_packed.sddl
├── scripts/                    # Build and utility scripts
│   ├── build.sh                # Compiles openzl + codecs + preprocessor
│   ├── get_openzl.sh           # Clones pinned OpenZL commit
│   └── make_train_sample.py    # Creates record-safe training samples
├── tools/                      # C++ source code
│   ├── codec_common.h          # Shared header (mmap, varint, bit packing, sequence encode/decode)
│   ├── fasta_codec.cpp         # FASTA ↔ binary streams (parallel encoding)
│   ├── fastq_codec.cpp         # FASTQ ↔ binary streams (parallel encoding)
│   └── genomic_preprocessor.cpp # FASTA/FASTQ/VCF → binary chunks (for schema-aware compression)
├── models/                     # Trained per-stream compressor models
│   ├── lossless/               # FASTA stream compressors
│   ├── lossless_fastq/         # FASTQ stream compressors
│   ├── lossless_fastq_csv/     # FASTQ CSV-profile compressors
│   └── lossless_vcf/           # VCF CSV-profile compressor
├── tests/                      # Pytest test suite (268 tests)
│   ├── __init__.py
│   ├── test_lossless.py        # FASTA round-trip tests
│   ├── test_lossless_fastq.py  # FASTQ round-trip tests
│   └── fixtures/               # Test FASTA/FASTQ input files
├── openzl/                     # [gitignored] OpenZL source + built binary
├── bin/                        # [gitignored] Compiled binaries (codecs + preprocessor)
└── nyx/                        # Python package
    ├── cli.py                  # CLI entry point
    ├── commands/               # Click command implementations
    │   ├── compress.py         # nyx compress (unified — all pipelines)
    │   ├── decompress.py       # nyx decompress (unified — all formats)
    │   ├── _lossless.py        # Internal lossless pipeline functions
    │   ├── train.py
    │   ├── benchmark.py
    │   ├── inspect_cmd.py
    │   ├── list_profiles.py
    │   └── build.py
    ├── core/                   # Pipeline logic
    │   ├── openzl.py           # zli subprocess wrapper
    │   ├── codec.py            # FASTA codec Python wrapper
    │   ├── fastq_codec.py      # FASTQ codec Python wrapper
    │   ├── vcf_codec.py        # VCF header/body splitter + reassembler (pure Python)
    │   ├── zlfasta.py          # .zlfasta container read/write
    │   ├── zlfastq.py          # .zlfastq container read/write
    │   ├── zlvcf.py            # .zlvcf container read/write
    │   ├── preprocessor.py     # genomic_preprocessor wrapper
    │   ├── detect.py           # File type auto-detection
    │   ├── archive.py          # .nyx archive read/write
    │   ├── sample.py           # Training sample creation
    │   ├── benchmark.py        # Competitor benchmarks (gzip, pigz, zstd)
    │   └── config.py           # Schema registry + defaults
    └── utils/
        └── paths.py            # Binary/schema path resolution
```

## Troubleshooting

**"Cannot find zli binary"**
Run `nyx build` to compile OpenZL from source. Requires git, make, and a C++17 compiler.

**"No SDDL schema available for 'fastq'"**
FASTQ and VCF schemas are not yet created for the schema pipeline. Use `--sddl <path>` with your own schema, or `--mode generic` for generic compression. Note: FASTQ lossless compression (the default) does not require an SDDL schema.

**Build fails on macOS**
Ensure Xcode command line tools are installed: `xcode-select --install`

**Build fails on Linux**
Ensure build-essential is installed: `sudo apt install build-essential`
