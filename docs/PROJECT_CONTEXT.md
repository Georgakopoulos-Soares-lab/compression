# Project Context: Nyx — OpenZL Genomic Compression CLI

> **Auto-generated reference.** Status: Python CLI Architecture (post-migration). Last scanned: 2026-02-12

## 1. Executive Summary

**Nyx** is a Python CLI tool that orchestrates [Meta's OpenZL](https://github.com/facebook/openzl) compression framework to provide seamless, schema-aware compression for genomic data formats (FASTA, FASTQ, VCF) as well as generic compression for any file type. It is a hybrid architecture: a **Python CLI orchestrator** (`click`-based) drives two C++ binaries — OpenZL's `zli` and a custom `genomic_preprocessor` — via `subprocess` calls. The pipeline detects file types, preprocesses raw text genomics into structured binary formats described by SDDL schemas, trains domain-specific compressors, compresses data in parallel, and bundles results into `.nyx` archives. Nyx replaces an earlier bash-script-driven pipeline with a proper Python package that can be `pip install`-ed and invoked as a single `nyx compress genome.fasta` command.

## 2. Architecture & Data Flow

### 2.1 High-Level Design

Nyx follows a **CLI orchestrator + native binary** pattern. The Python layer handles argument parsing, file type detection, progress display, temporary directory management, archive bundling, and competitor benchmarking. All computationally expensive work — preprocessing multi-GB genomic files, training compression models, compressing/decompressing data — is delegated to two C++ binaries via `subprocess`:

| Binary | Source | Built By | Purpose |
|--------|--------|----------|---------|
| `zli` | OpenZL (facebook/openzl) | `make` inside `nyx/openzl/` | Training compressors, compressing, decompressing, benchmarking, inspecting profiles |
| `genomic_preprocessor` | `nyx/tools/genomic_preprocessor.cpp` | `g++ -O3 -std=c++17` | Converting text FASTA/FASTQ/VCF into structured binary chunks matching SDDL schemas |

### 2.2 The Python <-> OpenZL Bridge

**Mechanism:** `subprocess.run()` and `subprocess.Popen()` calls to the `zli` binary.

**Bridge files:**

| File | Wraps | Key Functions |
|------|-------|---------------|
| `nyx/nyx/core/openzl.py` | `zli` binary | `compress()`, `decompress()`, `train()`, `train_async()`, `benchmark()`, `inspect()`, `list_profiles()`, `passthrough()` |
| `nyx/nyx/core/preprocessor.py` | `genomic_preprocessor` binary | `preprocess()` |
| `nyx/nyx/core/sample.py` | `scripts/make_train_sample.py` | `create_training_sample()` |

**Binary resolution** (`nyx/nyx/utils/paths.py`):
1. Check environment variable (`NYX_ZLI` / `NYX_PREPROCESSOR`)
2. Check local build path (`nyx/openzl/zli` / `nyx/bin/genomic_preprocessor`)
3. Check system `PATH`
4. Raise `FileNotFoundError` with a hint to run `nyx build`

**Error handling:** All subprocess wrappers capture `stdout` and `stderr`, check return codes, and raise typed exceptions (`OpenZLError`, `PreprocessorError`, `SampleError`) with the full command and stderr on failure.

**Async training:** `train_async()` returns a `subprocess.Popen` object for non-blocking training. The `compress` command uses this to display a live time-budget progress bar during training.

### 2.3 Data Pipeline

```
nyx compress genome.fasta
    │
    ▼
[Python: detect file type]  ─── nyx/nyx/core/detect.py
    │  Content-first detection (read first 1KB),
    │  then extension fallback
    ▼
[Python: auto-select mode]  ─── "train_plain" for genomic, "default" otherwise
    │
    ▼ (if train_plain)
[Python: create training sample]  ─── nyx/nyx/core/sample.py
    │  Delegates to scripts/make_train_sample.py
    │  Copies whole FASTA records up to ~200 MiB
    ▼
[C++: preprocess training sample]  ─── genomic_preprocessor
    │  Text FASTA → FAV4 binary chunks (.fasta_packed.bin)
    │  4-bit packed bases, columnar layout, SDDL-matching
    ▼
[C++: train compressor]  ─── zli train + SDDL schema
    │  Learns field-specific compression strategies
    │  Bounded by --max-time-secs (default 1800s)
    │  Produces .compressor artifact
    ▼
[C++: preprocess full file]  ─── genomic_preprocessor
    │  Full input → binary chunks (parallel, multi-threaded)
    ▼
[C++: compress chunks]  ─── zli compress (parallel via ThreadPoolExecutor)
    │  Applies trained compressor to each chunk
    │  Produces .zl compressed files
    ▼
[Python: bundle archive]  ─── nyx/nyx/core/archive.py
    │  Creates .nyx tar archive:
    │    manifest.json + compressor.model + chunks/*.zl
    ▼
genome.fasta.nyx
```

## 3. The Python CLI

**Entry Point:** `nyx/nyx/cli.py` → function `main()` registered as `nyx` console script in `pyproject.toml`

**Framework:** [Click](https://click.palletsprojects.com/) (>= 8.0) with [tqdm](https://tqdm.github.io/) (>= 4.60) for progress bars

**Installation:** `pip install -e ./nyx` then `nyx build` to compile C++ binaries

### 3.1 Command Reference

| Command | Key Arguments / Flags | Purpose | Implementation |
|---------|----------------------|---------|----------------|
| `nyx compress` | `<file> [-o PATH] [--mode MODE] [--sddl PATH] [--type TYPE] [--threads N] [--max-time-secs N] [--compress-jobs N] [--target-train-mib N] [--trainer ALGO] [--no-clustering] [--benchmark] [--keep-temp] [-v] [-f]` | Primary command. Detects type, preprocesses, trains, compresses, bundles `.nyx` archive. Optionally benchmarks against competitors. | `nyx/nyx/commands/compress.py:compress_cmd` |
| `nyx decompress` | `<file.nyx> [-o PATH] [-f] [--keep-temp] [-v]` | Extracts `.nyx` archive, decompresses all chunks via `zli decompress` | `nyx/nyx/commands/decompress.py:decompress_cmd` |
| `nyx train` | `<sample_dir> -o PATH [-p PROFILE] [--profile-arg PATH] [-c COMPRESSOR] [--threads N] [--max-time-secs N] [--use-all-samples] [--no-ace-successors] [--no-clustering] [--trainer ALGO] [-f] [-v]` | Direct passthrough to `zli train`. Unknown flags forwarded to zli. | `nyx/nyx/commands/train.py:train_cmd` |
| `nyx benchmark` | `<input_dir> [-v]` | Direct passthrough to `zli benchmark`. Unknown flags forwarded. | `nyx/nyx/commands/benchmark.py:benchmark_cmd` |
| `nyx inspect` | `<compressor_file> [-v]` | Inspect a trained compressor (JSON output). Passthrough to `zli inspect`. | `nyx/nyx/commands/inspect_cmd.py:inspect_cmd` |
| `nyx list-profiles` | `[-v]` | List available OpenZL compression profiles. Passthrough to `zli list-profiles`. | `nyx/nyx/commands/list_profiles.py:list_profiles_cmd` |
| `nyx build` | `[-j N] [-v]` | Clones pinned OpenZL, compiles `zli` and `genomic_preprocessor`. One-time setup. | `nyx/nyx/commands/build.py:build_cmd` |
| `nyx --version` | | Print version (`0.1.0`) | Built-in Click option |

### 3.2 Compression Modes

| Mode | Trigger | What It Does | Best For |
|------|---------|-------------|----------|
| `train_plain` | Auto for genomic files; or `--mode train_plain` | Auto-selects SDDL schema from registry. Preprocesses → trains → compresses. Full 5-step pipeline. | FASTA (and future FASTQ/VCF) — best compression ratio |
| `train_custom` | `--mode train_custom --sddl <path>` | User provides SDDL schema. Otherwise identical to train_plain. | Any structured binary with a custom schema |
| `default` | Auto for non-genomic; or `--mode default` | Uses OpenZL's generic `serial` profile. No preprocessing, no training. Single-step compress. | Quick compression of arbitrary files |
| `inline_train` | `--mode inline_train` | OpenZL trains inline on the input (no separate training step). For genomic files, still preprocesses first. | Moderate compression without separate training |

### 3.3 Benchmarking

The `--benchmark` flag on `nyx compress` runs the input through generic compressors after the nyx compression completes:

| Competitor | Command | Type |
|-----------|---------|------|
| `gzip -1` | `gzip -1 -k -c <input>` | Fast baseline |
| `pigz -9` | `pigz -9 -k -c <input>` | Parallel gzip, best effort |
| `zstd -3` | `zstd -3 -c --no-progress <input>` | Fast modern |
| `zstd -9` | `zstd -9 -c --no-progress <input>` | Good ratio modern |

Auto-detects availability; skips unavailable tools with a note. Results displayed in a formatted comparison table with ratio, savings %, speed (MB/s), and time.

## 4. Repository Structure

```
compression/                              # Git repository root
├── .gitignore                            # Root: ignores data/, nyx/openzl/, nyx/bin/, __pycache__
├── README.md                             # Legacy README (references old bash pipeline; stale)
│
├── docs/                                 # Documentation
│   ├── PROJECT_CONTEXT.md                # THIS FILE — authoritative project reference
│   ├── DEVELOPERS_GUIDE.md               # Technical deep-dive: architecture, file map, CLI reference
│   ├── SETUP_AND_PIPELINE_WALKTHROUGH.md # Setup from zero + conceptual pipeline walkthrough
│   ├── PROJECT_HISTORY.md                # Structured changelog (maintained by /update-context skill)
│   └── CHANGES.md                        # Developer change notes (consumed by /update-context skill)
│
├── data/                                 # [GITIGNORED] Downloaded FASTA data (shared across nyx)
│
├── .claude/skills/                       # Claude AI skills configuration
│   ├── update-context/SKILL.md
│   ├── update-context/REFERENCE.md
│   ├── hydrate/SKILL.md
│   └── hydrate-full/SKILL.md
│
├── .cursor/skills/                       # Cursor AI skills configuration (mirrors .claude)
│   ├── update-context/SKILL.md
│   ├── update-context/REFERENCE.md
│   ├── hydrate/SKILL.md
│   └── hydrate-full/SKILL.md
│
└── nyx/                                  # ====== THE ACTIVE PROJECT ======
    ├── pyproject.toml                    # Python package config (name=nyx, v0.1.0, deps: click, tqdm)
    ├── README.md                         # Comprehensive CLI docs, install guide, architecture
    ├── .gitignore                        # Ignores openzl/, bin/, .venv/, __pycache__/, nyx_*/
    │
    ├── schemas/                          # SDDL schema definitions
    │   └── fasta_packed.sddl             # FAV4 binary format schema (29 lines)
    │
    ├── scripts/                          # Build and data-prep scripts (called by Python)
    │   ├── build.sh                      # Clones OpenZL + compiles zli + compiles preprocessor
    │   ├── get_openzl.sh                 # Standalone OpenZL clone/pin script
    │   └── make_train_sample.py          # Record-safe FASTA training sampler (Python, stdlib only)
    │
    ├── tools/                            # C++ source code
    │   └── genomic_preprocessor.cpp      # 886-line C++17 preprocessor (FASTA/FASTQ/VCF → binary)
    │
    ├── tests/                            # Test directory (currently empty — __init__.py only)
    │   └── __init__.py
    │
    ├── nyx/                              # Python package source (the importable module)
    │   ├── __init__.py                   # __version__ = "0.1.0"
    │   ├── cli.py                        # Click group entry point; registers all commands
    │   │
    │   ├── commands/                     # CLI command implementations
    │   │   ├── __init__.py               # Empty
    │   │   ├── compress.py               # nyx compress — full pipeline orchestration (504 lines)
    │   │   ├── decompress.py             # nyx decompress — archive extraction + zli decompress
    │   │   ├── train.py                  # nyx train — passthrough to zli train
    │   │   ├── benchmark.py              # nyx benchmark — passthrough to zli benchmark
    │   │   ├── inspect_cmd.py            # nyx inspect — passthrough to zli inspect
    │   │   ├── list_profiles.py          # nyx list-profiles — passthrough to zli list-profiles
    │   │   └── build.py                  # nyx build — invokes scripts/build.sh
    │   │
    │   ├── core/                         # Business logic and binary wrappers
    │   │   ├── __init__.py               # Empty
    │   │   ├── openzl.py                 # zli subprocess wrapper (268 lines)
    │   │   ├── preprocessor.py           # genomic_preprocessor subprocess wrapper (74 lines)
    │   │   ├── detect.py                 # File type auto-detection (content + extension) (71 lines)
    │   │   ├── archive.py                # .nyx tar archive read/write (113 lines)
    │   │   ├── sample.py                 # Training sample creation wrapper (67 lines)
    │   │   ├── benchmark.py              # Competitor benchmarks (gzip/pigz/zstd) (250 lines)
    │   │   └── config.py                 # Schema registry, defaults, constants (45 lines)
    │   │
    │   └── utils/                        # Utility modules
    │       ├── __init__.py               # Empty
    │       └── paths.py                  # Binary/schema path resolution (99 lines)
    │
    ├── openzl/                           # [GITIGNORED] OpenZL source checkout + built zli binary
    ├── bin/                              # [GITIGNORED] Compiled genomic_preprocessor binary
    ├── .venv/                            # [GITIGNORED] Python virtual environment
    └── nyx.egg-info/                     # [GITIGNORED] Python package metadata
```

## 5. Core Components — Detailed Breakdown

### 5.1 CLI Entry Point: `nyx/nyx/cli.py`

- **Purpose:** Defines the Click `@click.group()` and registers all 7 subcommands.
- **Entry point registration:** `pyproject.toml` → `[project.scripts]` → `nyx = "nyx.cli:main"`
- **Commands registered:** `compress`, `decompress`, `train`, `benchmark`, `inspect`, `list-profiles`, `build`
- **Version:** `nyx --version` reports from `nyx/__init__.py` (`0.1.0`)

### 5.2 Compress Command: `nyx/nyx/commands/compress.py`

- **Purpose:** The primary command. Orchestrates the full compression pipeline across all 4 modes.
- **Size:** 504 lines — the largest file in the Python codebase.
- **Key functions:**

| Function | Lines | Description |
|----------|-------|-------------|
| `compress_cmd()` | 58–175 | Click command handler. Parses args, detects file type, auto-selects mode, dispatches to the appropriate pipeline function, reports results, optionally runs benchmarks. |
| `_compress_trained()` | 181–273 | Pipeline for `train_plain` and `train_custom`. 5-step: create sample → preprocess sample → train (async with progress bar) → preprocess full → compress chunks parallel → bundle archive. |
| `_compress_default()` | 280–306 | Pipeline for `default` mode. Single `zli compress` with `serial` profile. |
| `_compress_inline()` | 313–379 | Pipeline for `inline_train`. If genomic: preprocess → compress each chunk with `--train-inline`. If generic: single compress with `--train-inline`. |
| `_compress_chunks_parallel()` | 386–420 | Uses `concurrent.futures.ThreadPoolExecutor` for parallel chunk compression with tqdm progress bar. |
| `_step_progress` | 427–444 | Context manager wrapping tqdm for single-unit pipeline steps. |
| `_wait_with_timer()` | 447–481 | Polls a `subprocess.Popen` while displaying a live time-budget progress bar (used for training). |

- **Mode auto-selection logic (line 77–78):** If `--mode` is omitted, selects `train_plain` if the file is genomic (FASTA/FASTQ/VCF), otherwise `default`.
- **Schema resolution (line 200–203):** For `train_plain`, looks up the SDDL schema from `SCHEMA_REGISTRY[detected]`. For `train_custom`, uses the user-provided `--sddl` path.
- **Archive manifest:** Stores `original_filename`, `filetype`, `mode`, `binary_format`, `schema`, `chunk_count`, `has_compressor`, `nyx_version`.

### 5.3 Decompress Command: `nyx/nyx/commands/decompress.py`

- **Purpose:** Extracts a `.nyx` archive and decompresses all chunks.
- **Flow:** Extract tar → read `manifest.json` → iterate compressed chunks → `zli decompress` each.
- **Limitation:** Output is decompressed binary chunks, not reconstructed original text format. The README explicitly notes: "Postprocessing (binary chunks back to original text format) will be added in a future release."

### 5.4 OpenZL Wrapper: `nyx/nyx/core/openzl.py`

- **Purpose:** Type-safe Python interface to all `zli` CLI commands.
- **Bridge mechanism:** `subprocess.run()` for synchronous calls; `subprocess.Popen()` for async training.
- **Functions:**

| Function | zli Command | Blocking? |
|----------|------------|-----------|
| `compress()` | `zli compress` | Yes |
| `decompress()` | `zli decompress` | Yes |
| `train()` | `zli train` | Yes |
| `train_async()` | `zli train` | No (returns `Popen`) |
| `get_train_cmd()` | — | Returns command list for display |
| `benchmark()` | `zli benchmark` | Yes, returns stdout |
| `inspect()` | `zli inspect` | Yes, returns stdout |
| `list_profiles()` | `zli list-profiles` | Yes, returns stdout |
| `passthrough()` | Any | Streams output, exits with zli's code |

- **Error handling:** `OpenZLError` raised with return code, full command, and stderr.
- **Verbose mode:** When `verbose=True`, prints the full command before execution.

### 5.5 Preprocessor Wrapper: `nyx/nyx/core/preprocessor.py`

- **Purpose:** Wraps the `genomic_preprocessor` binary.
- **Single function:** `preprocess(input_file, output_dir, threads, filetype, verbose)` → returns sorted list of generated chunk `Path` objects.
- **Error handling:** `PreprocessorError` raised if exit code is non-zero or no chunks are produced.

### 5.6 File Type Detection: `nyx/nyx/core/detect.py`

- **Purpose:** Auto-detects whether an input file is FASTA, FASTQ, VCF, or unknown.
- **Strategy:** Content-first (reads first 1KB), then extension fallback.
- **Detection rules:**

| Priority | Check | Result |
|----------|-------|--------|
| 1 | First 16 bytes start with `##fileformat=VCF` | `"vcf"` |
| 2 | First byte is `>` | `"fasta"` |
| 3 | First byte is `@` and 3rd line starts with `+` | `"fastq"` |
| 4 | Extension in `{.fasta, .fa, .fna, .fas}` | `"fasta"` |
| 5 | Extension in `{.fastq, .fq}` | `"fastq"` |
| 6 | Extension `.vcf` | `"vcf"` |
| 7 | None of the above | `None` (generic) |

### 5.7 Archive Module: `nyx/nyx/core/archive.py`

- **Purpose:** Read and write `.nyx` archive files.
- **Format:** Standard tar archive (uncompressed tar wrapper; chunks inside are already compressed).
- **Archive layout:**

```
manifest.json           # JSON metadata (mode, filetype, schema, chunk_count, etc.)
compressor.model        # Trained compressor artifact (if applicable)
chunks/
  chunk_00000.*.zl      # Compressed binary chunks
  chunk_00001.*.zl
  ...
```

- **Security:** `extract_archive()` rejects paths containing `/` prefix or `..` (path traversal protection).
- **Key functions:** `create_archive()`, `extract_archive()`, `get_chunks_from_extract()`, `get_compressor_from_extract()`
- **Archive version:** `NYX_ARCHIVE_VERSION = 1`

### 5.8 Config & Schema Registry: `nyx/nyx/core/config.py`

- **Purpose:** Central configuration and file type → schema mapping.
- **`SCHEMA_REGISTRY`:** Maps detected file types to their preprocessing configuration:

| File Type | `preprocessor_type` | `sddl` | `chunk_extension` |
|-----------|---------------------|--------|-------------------|
| `"fasta"` | `"fasta_packed"` | `"fasta_packed.sddl"` | `".fasta_packed.bin"` |
| `"fastq"` | `"fastq_v4"` | `None` (TODO) | `".fastq_v4.bin"` |
| `"vcf"` | `"vcf"` | `None` (TODO) | `".vcf.bin"` |

- **Defaults:**

| Constant | Value | Description |
|----------|-------|-------------|
| `DEFAULT_THREADS` | `os.cpu_count() or 4` | Thread count for training/preprocessing |
| `DEFAULT_COMPRESS_JOBS` | `4` | Parallel compression jobs |
| `DEFAULT_TRAIN_MIB` | `200` | Training sample size in MiB |
| `DEFAULT_MAX_TIME_SECS` | `1800` | Training time budget (30 minutes) |
| `DEFAULT_PROFILE` | `"serial"` | Generic OpenZL profile for non-genomic files |

### 5.9 Benchmark Module: `nyx/nyx/core/benchmark.py`

- **Purpose:** Runs competitor compressors (gzip, pigz, zstd) on the original input file and produces a comparison table.
- **Data class:** `BenchmarkResult` with properties `ratio`, `savings_pct`, `speed_mbps`.
- **Execution:** Each competitor runs in a `threading.Thread` with a tqdm progress bar showing elapsed time and estimated progress (by polling output file size).
- **Output table:** Formatted columns for Compressor, Compressed size, Ratio, Savings %, Speed (MB/s), Time. Nyx marked with `*`.

### 5.10 Path Resolution: `nyx/nyx/utils/paths.py`

- **Purpose:** Locates binaries (`zli`, `genomic_preprocessor`), schemas, and scripts at runtime.
- **`_nyx_root()`:** Returns `Path(__file__).resolve().parents[2]` — the `nyx/` directory containing `pyproject.toml`.
- **Search order for all binaries:** Environment variable → local build path → system PATH → `FileNotFoundError`.
- **Functions:** `find_zli()`, `find_preprocessor()`, `find_schema(name)`, `find_make_train_sample()`

### 5.11 C++ Preprocessor: `nyx/tools/genomic_preprocessor.cpp`

- **Purpose:** Converts raw text bioinformatics files into structured binary chunks matching SDDL schemas. This is the same preprocessor from the previous architecture, renamed from `biocompress_preprocessor.cpp` to `genomic_preprocessor.cpp`.
- **Size:** 886 lines, zero external dependencies (C++17 stdlib + POSIX).
- **Supported formats:**

| Format | Magic | CLI arg | Description |
|--------|-------|---------|-------------|
| FASTA Packed | `FAV4` | `fasta_packed` | 4-bit packed bases, columnar layout (primary) |
| FASTA | `FAV3` | `fasta` | Unpacked ASCII sequences |
| FASTQ | `FQV3` | `fastq` | Headers + sequences + qualities |
| FASTQ V4 | `FQV4` | `fastq_v4` | Header dedup + 4-bit packed seqs + qualities |
| VCF | `VCF3` | `vcf` | Columnar decomposition of variant calls |

- **Key architectural features:**
  - **Memory-mapped I/O** (`mmap` + `MADV_SEQUENTIAL`) for zero-copy scanning of multi-GB files
  - **Parallel chunking** with split points snapped to record boundaries
  - **Lock-free work stealing** via `std::atomic<size_t>` fetch-add
  - **4-bit base packing:** `A=0, C=1, G=2, T=3, N=4`; two bases per byte (high nibble first)
  - **Max chunk sizes:** 450 MiB (FASTA/FASTQ), 300 MiB (VCF)
- **CLI:** `genomic_preprocessor <input_file> <output_dir> <num_threads> [type]`
- **Output:** `chunk_NNNNN.<format>.bin` files in the output directory

### 5.12 SDDL Schema: `nyx/schemas/fasta_packed.sddl`

- **Purpose:** Defines the FAV4 binary wire format consumed by `zli train --profile sddl`.
- **Fields:** `magic` (Byte[4]), `num_records` (U32), `hdr_offsets` (U32[N+1]), `seq_offsets` (U32[N+1]), `seq_lengths` (U32[N]), `hdr_total` (U32), `seq_total` (U32), `hdr_pad` (U32), `seq_pad` (U32), `headers` (Byte[...]), `sequences` (Byte[...]), `: Byte[_rem]` (permissive trailer).
- **All integers:** Little-endian 32-bit unsigned (`U32 = UInt32LE`).
- **Why this matters:** By teaching OpenZL the field types, it can learn separate compression models for each — delta coding for monotonic offset arrays, specialized models for the constrained 5-symbol packed alphabet, etc.

### 5.13 Build Script: `nyx/scripts/build.sh`

- **Purpose:** Single script that clones OpenZL (pinned commit `e40fe9f3`), builds `zli` via `make`, and compiles `genomic_preprocessor` via `g++`.
- **Invoked by:** `nyx build` command (`nyx/nyx/commands/build.py`).
- **Pinned commit:** `e40fe9f314283047147d573d113fa7d17eabf7ac` (overridable via `OPENZL_COMMIT`).
- **Build flags:** `env -u CFLAGS -u CXXFLAGS ... make -j$(nproc) MOREFLAGS="-pthread"` for OpenZL; `g++ -O3 -std=c++17 -pthread` for the preprocessor.
- **Outputs:** `nyx/openzl/zli` and `nyx/bin/genomic_preprocessor`.

### 5.14 Training Sampler: `nyx/scripts/make_train_sample.py`

- **Purpose:** Creates a ~N MiB training sample by copying whole FASTA records. Never cuts mid-record; skips records that would overshoot the target.
- **CLI:** `python3 make_train_sample.py --in <input.fna> --out <output.fasta> --target-mib <N>`
- **Dependencies:** Python stdlib only (`argparse`, `pathlib`).
- **Called by:** `nyx/nyx/core/sample.py:create_training_sample()` via `subprocess.run()`.

## 6. OpenZL Integration Details

### 6.1 Integration Method

OpenZL is integrated as a **git-cloned dependency, built from source, used exclusively via its `zli` CLI binary through Python `subprocess` calls**. No C/C++ API is used; no FFI/ctypes/pybind11. The Python code never links against OpenZL.

### 6.2 Version Pinning

```bash
OPENZL_COMMIT="${OPENZL_COMMIT:-e40fe9f314283047147d573d113fa7d17eabf7ac}"
```
Defined in `nyx/scripts/build.sh` (line 9) and `nyx/scripts/get_openzl.sh` (line 8).

### 6.3 zli Commands Used

| Command | Python Wrapper | Usage |
|---------|---------------|-------|
| `zli train <dir> --profile sddl --profile-arg <sddl> --output <out> [flags]` | `openzl.train()` / `openzl.train_async()` | Train a compressor on preprocessed binary samples |
| `zli compress <file> --compressor <model> --output <out> [--force]` | `openzl.compress()` | Compress using a trained model |
| `zli compress <file> --profile <p> [--profile-arg <a>] [--train-inline] --output <out>` | `openzl.compress()` | Compress with a named profile (default/inline modes) |
| `zli decompress <file> --output <out> [--force]` | `openzl.decompress()` | Decompress a `.zl` file |
| `zli benchmark <dir> [flags]` | `openzl.benchmark()` | Run OpenZL's built-in benchmarks |
| `zli inspect <model> [flags]` | `openzl.inspect()` | Inspect a trained compressor (JSON) |
| `zli list-profiles` | `openzl.list_profiles()` | List available compression profiles |

### 6.4 Training Flags

All training flags from the previous architecture are preserved and exposed via the Python CLI:

| Flag | Default in Nyx | Exposed via |
|------|---------------|-------------|
| `--profile sddl` | Used for `train_plain`/`train_custom` | Hardcoded in `_compress_trained()` |
| `--profile-arg <path>` | From `SCHEMA_REGISTRY` or `--sddl` | Auto-resolved |
| `--threads <N>` | `os.cpu_count()` | `--threads` CLI flag |
| `--max-time-secs <N>` | `1800` | `--max-time-secs` CLI flag |
| `--use-all-samples` | `True` | Hardcoded in `openzl.py` |
| `--no-ace-successors` | `True` | Hardcoded in `openzl.py` |
| `--no-clustering` | `False` | `--no-clustering` CLI flag |
| `--trainer <algo>` | Not set (OpenZL defaults to greedy) | `--trainer` CLI flag |
| `--force` | `True` | Hardcoded in `openzl.py` |

## 7. .nyx Archive Format

A `.nyx` file is an uncompressed tar archive:

```
manifest.json           # {"version": 1, "original_filename": "...", "filetype": "fasta",
                        #  "mode": "train_plain", "binary_format": "fasta_packed",
                        #  "schema": "fasta_packed.sddl", "chunk_count": N,
                        #  "has_compressor": true, "nyx_version": "0.1.0"}
compressor.model        # Trained compressor (only if has_compressor: true)
chunks/
  chunk_00000.fasta_packed.bin.zl
  chunk_00001.fasta_packed.bin.zl
  ...
```

Inspectable with standard tools: `tar tf genome.fasta.nyx`

## 8. FASTA File Processing

### 8.1 Pipeline

1. **Detection:** `detect.py` reads first 1KB, checks for `>` as first byte → `"fasta"`
2. **Training sample:** `make_train_sample.py` copies whole FASTA records (never splits mid-sequence) until reaching target MiB
3. **Preprocessing:** `genomic_preprocessor` converts text FASTA to FAV4 binary:
   - Memory-maps input with `mmap` + `MADV_SEQUENTIAL`
   - Splits at record boundaries (snaps to `>` at start-of-line)
   - Per chunk: separates headers (without `>`) and sequences; packs bases 2-per-byte
4. **Training:** `zli train` reads FAV4 chunks + SDDL schema, learns per-field compression strategies
5. **Compression:** `zli compress` applies the trained model to each chunk → `.zl` files
6. **Bundling:** Chunks + compressor + manifest bundled into `.nyx` tar archive

### 8.2 4-Bit Base Encoding

```
Base → Nibble:  A/a=0  C/c=1  G/g=2  T/t=3  N/n=4  (anything else)=4
Byte layout:    [high nibble: base_i] [low nibble: base_i+1]
Odd sequences:  Last base in high nibble, low nibble = 0x0
```

### 8.3 Supported File Types

| Format | Detection | Preprocessing | SDDL Schema | Status |
|--------|-----------|---------------|-------------|--------|
| FASTA (.fasta, .fa, .fna) | Content + extension | 4-bit packed (FAV4) | `fasta_packed.sddl` | **Fully supported** |
| FASTQ (.fastq, .fq) | Content + extension | Header dedup + 4-bit (FQV4) | Not yet created | Preprocessor ready, schema TODO |
| VCF (.vcf) | Content + extension | Columnar (VCF3) | Not yet created | Preprocessor ready, schema TODO |
| Generic (any other) | Fallback | None | Uses `serial` profile | Supported (default/inline modes) |

## 9. Build System & Dependencies

### 9.1 Python Package

**File:** `nyx/pyproject.toml`

```toml
[project]
name = "nyx"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["click>=8.0", "tqdm>=4.60"]

[project.scripts]
nyx = "nyx.cli:main"
```

Build backend: setuptools (>= 68.0) + wheel.

### 9.2 Full Dependency Inventory

| Dependency | Type | Version | Declared In | Purpose |
|-----------|------|---------|-------------|---------|
| Python | Runtime | >= 3.13 | `pyproject.toml` | CLI orchestration |
| click | Runtime (pip) | >= 8.0 | `pyproject.toml` | CLI framework |
| tqdm | Runtime (pip) | >= 4.60 | `pyproject.toml` | Progress bars |
| C++ compiler (g++/clang++) | Build-time | C++17 support | `nyx/scripts/build.sh` | Compiles genomic_preprocessor |
| GNU Make | Build-time | 3.81+ | `nyx/scripts/build.sh` | Builds OpenZL |
| Git | Build-time | 2.0+ | `nyx/scripts/build.sh` | Clones OpenZL |
| OpenZL | Build-time + Runtime | Commit `e40fe9f3` | `nyx/scripts/build.sh` | Compression engine (produces `zli`) |
| gzip | Optional (runtime) | Any | `nyx/nyx/core/benchmark.py` | Benchmark competitor |
| pigz | Optional (runtime) | Any | `nyx/nyx/core/benchmark.py` | Benchmark competitor |
| zstd | Optional (runtime) | Any | `nyx/nyx/core/benchmark.py` | Benchmark competitor |

### 9.3 Build Sequence

```bash
pip install -e ./nyx      # Install Python package (editable)
nyx build                  # Invokes scripts/build.sh:
                           #   1. git clone + checkout pinned OpenZL commit
                           #   2. make -j$(nproc) MOREFLAGS="-pthread"  →  nyx/openzl/zli
                           #   3. g++ -O3 -std=c++17 -pthread  →  nyx/bin/genomic_preprocessor
```

## 10. Getting Started

### 10.1 Prerequisites

| Tool | Required? | Install (macOS) | Install (Linux) |
|------|-----------|----------------|-----------------|
| Python 3.13+ | Yes | `brew install python@3.13` or `pyenv install 3.13` | `apt install python3 python3-pip` |
| C++17 compiler | Yes | `xcode-select --install` | `apt install build-essential` |
| Git | Yes | Included with Xcode CLI tools | `apt install git` |
| Make | Yes | Included with Xcode CLI tools | `apt install build-essential` |
| pigz | No (recommended) | `brew install pigz` | `apt install pigz` |
| zstd | No (recommended) | `brew install zstd` | `apt install zstd` |

### 10.2 Step-by-Step from Fresh Clone

```bash
# 1. Clone the repository
git clone <repo-url> compression && cd compression

# 2. Create and activate a virtual environment (recommended)
python3 -m venv nyx/.venv
source nyx/.venv/bin/activate

# 3. Install the Nyx CLI
pip install -e ./nyx

# 4. Build OpenZL and the genomic preprocessor (~2-5 min)
nyx build

# 5. Compress a FASTA file
nyx compress path/to/genome.fasta

# 6. Decompress
nyx decompress genome.fasta.nyx -o output/

# 7. Compress with benchmarking
nyx compress genome.fasta --benchmark
```

### 10.3 Example Commands

```bash
# Schema-aware FASTA compression (auto-detects, uses train_plain mode)
nyx compress reference_genome.fna

# Custom training parameters
nyx compress genome.fna --threads 16 --max-time-secs 3600 --target-train-mib 300

# Custom SDDL schema
nyx compress data.bin --mode train_custom --sddl my_format.sddl

# Generic quick compression (no training)
nyx compress arbitrary_file.dat --mode default

# Inline training
nyx compress genome.fasta --mode inline_train

# Direct zli train passthrough
nyx train chunks/ -o model.compressor -p sddl --profile-arg schema.sddl --threads 16

# Inspect a compressor
nyx inspect model.compressor

# List available profiles
nyx list-profiles
```

## 11. Configuration Reference

### 11.1 CLI Flags (`nyx compress`)

| Flag | Default | Description |
|------|---------|-------------|
| `-o, --output PATH` | `<input>.nyx` | Output archive path |
| `--mode` | Auto (`train_plain` if genomic, `default` otherwise) | Compression mode |
| `--sddl PATH` | From registry | SDDL schema (required for `train_custom`) |
| `--type` | Auto-detected | Override file type: `fasta`, `fastq`, `vcf` |
| `--threads N` | `os.cpu_count()` | Training/preprocessing threads |
| `--max-time-secs N` | `1800` | Training time budget |
| `--compress-jobs N` | `4` | Parallel compression jobs |
| `--target-train-mib N` | `200` | Training sample size in MiB |
| `--trainer ALGO` | `greedy` (OpenZL default) | `greedy`, `full-split`, or `bottom-up` |
| `--no-clustering` | `False` | Skip clustering during training |
| `--benchmark` | `False` | Run competitor benchmarks |
| `--keep-temp` | `False` | Keep temp directories |
| `-v, --verbose` | `False` | Print subprocess commands |
| `-f, --force` | `False` | Overwrite existing output |

### 11.2 Environment Variables

| Variable | Description |
|----------|-------------|
| `NYX_ZLI` | Override path to `zli` binary |
| `NYX_PREPROCESSOR` | Override path to `genomic_preprocessor` binary |
| `OPENZL_REPO` | Override OpenZL git repository URL (build time) |
| `OPENZL_COMMIT` | Override pinned OpenZL commit hash (build time) |
| `JOBS` | Override make parallelism for `nyx build` (alternative to `-j`) |

## 12. Key Code Paths

### 12.1 `nyx compress genome.fasta` (train_plain mode)

1. **CLI parsing:** `nyx/nyx/commands/compress.py:compress_cmd()` — parses Click args
2. **Type detection:** `detect_filetype(input_path)` → reads first 1KB → returns `"fasta"`
3. **Mode selection:** `mode = "train_plain"` (auto for genomic)
4. **Schema lookup:** `SCHEMA_REGISTRY["fasta"]` → `FileTypeConfig(preprocessor_type="fasta_packed", sddl="fasta_packed.sddl", ...)`
5. **Dispatch:** `_compress_trained()` called
6. **Step 1 — Sample:** `sample.create_training_sample()` → subprocess: `python3 make_train_sample.py --in ... --out ... --target-mib 200`
7. **Step 2 — Preprocess sample:** `preprocessor.preprocess(train_sample, chunks_dir, threads=1, filetype="fasta_packed")` → subprocess: `genomic_preprocessor <sample> <dir> 1 fasta_packed`
8. **Step 3 — Train:** `openzl.train_async(profile="sddl", profile_arg=<sddl_path>, ...)` → subprocess.Popen: `zli train <dir> --profile sddl --profile-arg <sddl> --output <model> ...` + `_wait_with_timer()` progress bar
9. **Step 4 — Preprocess full:** `preprocessor.preprocess(input_path, full_dir, threads=N, filetype="fasta_packed")`
10. **Step 5 — Compress:** `_compress_chunks_parallel()` → `ThreadPoolExecutor` → `openzl.compress()` per chunk → subprocess: `zli compress <chunk> --compressor <model> --output <chunk>.zl`
11. **Bundle:** `archive.create_archive()` → tar file with manifest.json + compressor.model + chunks/*.zl
12. **Report:** Print size, ratio, time
13. **Benchmark (if `--benchmark`):** `run_benchmarks()` → gzip -1, pigz -9, zstd -3, zstd -9 → `print_benchmark_table()`

### 12.2 `nyx decompress genome.fasta.nyx`

1. **CLI parsing:** `decompress_cmd()` — parses args
2. **Extract:** `archive.extract_archive(input_path, tmpdir)` → tar extraction with path traversal check → returns manifest dict
3. **Get chunks:** `archive.get_chunks_from_extract(tmpdir)` → sorted list of `chunks/*.zl`
4. **Get compressor:** `archive.get_compressor_from_extract(tmpdir)` → `compressor.model` or None
5. **Decompress each:** `openzl.decompress(chunk, out_file)` → subprocess: `zli decompress <chunk> --output <out>`
6. **Output:** Decompressed binary chunks in output directory

## 13. Testing

### 13.1 Current State

The `nyx/tests/` directory exists but contains only an empty `__init__.py`. **There are no automated tests.** Correctness relies on:
- Round-trip validation during compression (compress → decompress → `cmp -s`)
- Manual testing via `nyx compress` / `nyx decompress`

### 13.2 Testing Framework

No testing framework is configured. `pyproject.toml` does not declare pytest or any test dependencies. To add tests, you would:
1. Add `pytest` to `[project.optional-dependencies]` in `pyproject.toml`
2. Write test files in `nyx/tests/`
3. Run with `pytest nyx/tests/`

## 14. Known Limitations, TODOs & Technical Debt

### 14.1 Missing SDDL Schemas

FASTQ and VCF are detected by `detect.py` and the preprocessor handles them, but the `SCHEMA_REGISTRY` maps them to `sddl=None`. Attempting `nyx compress file.fastq` will fail with "No SDDL schema available yet for 'fastq'". The user must use `--mode train_custom --sddl <path>` or `--mode default`.

**Code reference:** `nyx/nyx/core/config.py` lines 26 and 31:
```python
"fastq": FileTypeConfig(preprocessor_type="fastq_v4", sddl=None, ...)  # TODO: create fastq_v4.sddl
"vcf": FileTypeConfig(preprocessor_type="vcf", sddl=None, ...)          # TODO: create vcf.sddl
```

### 14.2 No Text Reconstruction on Decompress

Decompression outputs binary chunks, not the original text format. The README explicitly flags this: "Postprocessing (binary chunks back to original text format) will be added in a future release."

### 14.3 No Automated Tests

The test directory is empty. There are no unit tests, integration tests, or CI/CD.

### 14.4 Stale Root README

The root `compression/README.md` still references the old bash-script pipeline (`scripts/clean_generated.sh`, `scripts/run_train_250_and_test_full.sh`, `tools/biocompress_preprocessor`). These files no longer exist at the root level. The authoritative documentation is now `nyx/README.md`.

### 14.5 Docs Updated

`docs/DEVELOPERS_GUIDE.md` and `docs/SETUP_AND_PIPELINE_WALKTHROUGH.md` have been updated to reflect the current Nyx CLI architecture.

### 14.6 Hardcoded Training Defaults

`--use-all-samples` and `--no-ace-successors` are hardcoded to `True` in `openzl.py:_build_train_args()` (lines 103–104). There is no CLI flag to override these for `nyx compress`. The `nyx train` passthrough command does expose them.

### 14.7 No Streaming / Incremental Compression

The pipeline requires the entire file to be preprocessed into chunks before compression begins. No streaming support.

### 14.8 Thread-based Parallelism for Compression

`_compress_chunks_parallel()` uses `ThreadPoolExecutor`, which is appropriate since the actual work happens in subprocesses (no GIL contention), but error handling could lose stack traces from failed futures.

### 14.9 Temp Directory Cleanup

Failed runs may leave `nyx_*` temp directories in the system temp folder. These are cleaned on success and on most failures (via `finally` block + `shutil.rmtree`), but a hard crash or `kill -9` would orphan them.

## 15. Adding a New File Type

Per the README's instructions:

1. Add preprocessing logic in `nyx/tools/genomic_preprocessor.cpp` (new `process_*_chunk()` function + `FileType` enum entry + `detect_type()` rule)
2. Create an SDDL schema in `nyx/schemas/` (e.g., `fastq_v4.sddl`)
3. Update `SCHEMA_REGISTRY` in `nyx/nyx/core/config.py` to set the `sddl` field
4. Optionally add extension mappings in `nyx/nyx/core/detect.py:EXTENSION_MAP`
5. Rebuild: `nyx build`

## 16. Glossary

| Term | Definition |
|------|-----------|
| **Nyx** | The Python CLI tool that orchestrates OpenZL compression pipelines |
| **OpenZL** | Meta's open-source, schema-aware compression framework |
| **`zli`** | OpenZL's command-line tool for training, compressing, decompressing |
| **SDDL** | Simple Data Description Language — OpenZL's schema format for describing structured binary data |
| **FASTA** | Text format for nucleotide sequences. Records start with `>` header, followed by sequence lines |
| **FASTQ** | Like FASTA but with quality scores. 4-line records: `@header`, sequence, `+`, quality |
| **VCF** | Variant Call Format — tab-delimited format for genetic variants |
| **FAV4** | "FASTA Version 4" — the project's binary format with 4-bit packed bases and columnar layout |
| **4-bit packing** | Encoding two DNA bases per byte (A=0, C=1, G=2, T=3, N=4) in high/low nibbles |
| **`.nyx` archive** | Tar file containing manifest.json, trained compressor, and compressed chunks |
| **Profile** | A trained compression model (`.compressor`/`.model`) encoding learned strategies |
| **Schema registry** | `SCHEMA_REGISTRY` dict in `config.py` mapping file types to preprocessing config |
| **`train_plain`** | Compression mode: auto-selects schema, preprocesses, trains, compresses (best ratio) |
| **`train_custom`** | Compression mode: user provides SDDL schema, otherwise identical to train_plain |
| **`default`** | Compression mode: generic OpenZL `serial` profile, no preprocessing |
| **`inline_train`** | Compression mode: OpenZL trains inline on input (no separate training step) |
| **Record-safe sampling** | Extracting whole FASTA records without splitting mid-sequence |
| **Chunk** | A portion of input split at record boundaries for parallel processing |
| **ACE** | Asymmetric Context Encoding — advanced OpenZL model component (disabled by default) |
| **Compression ratio** | `uncompressed_size / compressed_size`. Higher is better. |
| **`genomic_preprocessor`** | C++ binary converting text FASTA/FASTQ/VCF to structured binary chunks |
| **GRCm39** | Genome Reference Consortium Mouse Build 39 — default test genome (~2.76 GB) |
