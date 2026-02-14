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

- **Python 3.13+**
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

Nyx requires **Python 3.13+**. We recommend using [pyenv](https://github.com/pyenv/pyenv) or your system package manager to install it and creating a dedicated virtual environment.

### Install Python 3.13

```bash
# macOS (Homebrew)
brew install python@3.13

# macOS / Linux (pyenv)
pyenv install 3.13
```

### Create and activate the virtual environment

```bash
# Create a venv inside the nyx/ directory
python3 -m venv nyx/.venv

# Activate it
source nyx/.venv/bin/activate

# Verify
python --version   # Should print Python 3.13.x
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
# Compress a FASTA file (auto-detects type, uses schema-aware pipeline)
nyx compress genome.fasta -o genome.nyx

# Decompress
nyx decompress genome.nyx -o output/

# Check version
nyx --version
```

## Commands

### `nyx compress`

The primary command. Compresses a file using one of four modes.

```
nyx compress <file> [OPTIONS]
```

**Options:**

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Output `.nyx` file path (default: `<input>.nyx`) |
| `--mode MODE` | Compression mode (see below). Auto-selected if omitted. |
| `--sddl PATH` | SDDL schema file (required for `train_custom`) |
| `--type TYPE` | Override auto-detected file type (`fasta`, `fastq`, `vcf`) |
| `--threads N` | Threads for training/preprocessing (default: CPU count) |
| `--max-time-secs N` | Training time budget in seconds (default: 1800) |
| `--compress-jobs N` | Parallel compression jobs (default: 4) |
| `--target-train-mib N` | Training sample size in MiB (default: 200) |
| `--trainer ALGO` | Training algorithm: `greedy`, `full-split`, or `bottom-up` (default: greedy) |
| `--no-clustering` | Skip clustering during training |
| `--benchmark` | Run competitor benchmarks (gzip, pigz, zstd) and print comparison table |
| `--keep-temp` | Keep temporary directories for debugging |
| `-v, --verbose` | Show subprocess commands |
| `-f, --force` | Overwrite existing output |

**Compression Modes:**

| Mode | When to use | What it does |
|------|-------------|--------------|
| `train_plain` | Genomic files (FASTA, FASTQ, VCF) | Auto-selects SDDL schema from registry. Preprocesses, trains, compresses. Best compression ratio. |
| `train_custom` | Any file with a custom schema | You provide `--sddl`. Preprocesses, trains, compresses. |
| `default` | Generic files, quick compression | Uses OpenZL's generic `serial` profile. No preprocessing. |
| `inline_train` | Moderate compression without separate training | OpenZL trains inline on the input file. |

If `--mode` is omitted, Nyx picks `train_plain` for recognized genomic files and `default` otherwise.

**Examples:**

```bash
# Schema-aware FASTA compression (best ratio)
nyx compress reference_genome.fna

# Same but with custom training parameters
nyx compress reference_genome.fna --threads 16 --max-time-secs 3600

# Custom schema
nyx compress data.bin --mode train_custom --sddl my_format.sddl

# Generic compression (fast, no training)
nyx compress arbitrary_file.dat --mode default

# Inline training (moderate ratio, no separate training step)
nyx compress genome.fasta --mode inline_train

# Compress and benchmark against gzip, pigz, zstd
nyx compress genome.fasta --benchmark
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

Extracts and decompresses a `.nyx` archive.

```
nyx decompress <file.nyx> [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `-o, --output PATH` | Output directory (default: `<input>_decompressed/`) |
| `-f, --force` | Overwrite existing output |
| `-v, --verbose` | Verbose output |

> **Note:** Decompression currently outputs binary chunks. Postprocessing (binary chunks back to original text format) will be added in a future release.

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

```
nyx compress genome.fasta
    |
    v
[detect file type] --> "fasta"
    |
    v
[create training sample] --> ~200 MiB record-safe subset
    |
    v
[preprocess] --> binary chunks (FAV4 format, 4-bit packed bases)
    |
    v
[train compressor] --> trained model (via zli train + SDDL schema)
    |
    v
[preprocess full file] --> binary chunks
    |
    v
[compress chunks] --> .zl files (parallel, configurable jobs)
    |
    v
[bundle archive] --> genome.fasta.nyx (tar: manifest + compressor + chunks)
```

Nyx is an **orchestrator**. All heavy computation runs in C++ via two binaries:
- `zli` — OpenZL's CLI for training, compression, and decompression
- `genomic_preprocessor` — converts text genomic formats to binary chunk format

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

| Format | Detection | Preprocessing | Schema |
|--------|-----------|---------------|--------|
| FASTA (.fasta, .fa, .fna) | Content (`>`) + extension | 4-bit packed (FAV4) | `fasta_packed.sddl` |
| FASTQ (.fastq, .fq) | Content (`@...+`) + extension | Header dedup + 4-bit (FQV4) | Coming soon |
| VCF (.vcf) | Content (`##fileformat=VCF`) | Structured columns (VCF3) | Coming soon |
| Generic (any other) | Fallback | None | Uses `serial` profile |

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
│   ├── build.sh                # Compiles openzl + preprocessor
│   ├── get_openzl.sh           # Clones pinned OpenZL commit
│   └── make_train_sample.py    # Creates record-safe training samples
├── tools/                      # C++ source code
│   └── genomic_preprocessor.cpp
├── openzl/                     # [gitignored] OpenZL source + built binary
├── bin/                        # [gitignored] Compiled preprocessor binary
└── nyx/                        # Python package
    ├── cli.py                  # CLI entry point
    ├── commands/               # Click command implementations
    │   ├── compress.py
    │   ├── decompress.py
    │   ├── train.py
    │   ├── benchmark.py
    │   ├── inspect_cmd.py
    │   ├── list_profiles.py
    │   └── build.py
    ├── core/                   # Pipeline logic
    │   ├── openzl.py           # zli subprocess wrapper
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

**"No SDDL schema available yet for 'fastq'"**
FASTQ and VCF schemas are not yet created. Use `--mode train_custom --sddl <path>` with your own schema, or `--mode default` for generic compression.

**Build fails on macOS**
Ensure Xcode command line tools are installed: `xcode-select --install`

**Build fails on Linux**
Ensure build-essential is installed: `sudo apt install build-essential`
