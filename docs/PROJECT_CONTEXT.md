# Project Context: Nyx — OpenZL Genomic Compression CLI

> **Auto-generated reference.** Status: Python CLI Architecture (post-migration). Last scanned: 2026-02-28

## 1. Executive Summary

**Nyx** is a Python CLI tool that orchestrates [Meta's OpenZL](https://github.com/facebook/openzl) compression framework to provide seamless, schema-aware compression for genomic data formats (FASTA, FASTQ, VCF) as well as generic compression for any file type. It is a hybrid architecture: a **Python CLI orchestrator** (`click`-based) drives C++ binaries — OpenZL's `zli`, a custom `genomic_preprocessor`, and purpose-built `fasta_codec`/`fastq_codec` lossless codecs — via `subprocess` calls.

Nyx supports two compression pipelines:

1. **Schema-aware compression** (`nyx compress`): Preprocesses genomic data into SDDL-described binary formats, trains domain-specific compressors, compresses in parallel, and bundles results into `.nyx` archives.
2. **Lossless stream compression** (`nyx compress-lossless`): For FASTA, encodes into packed binary chunks (NXF2 for nucleotide, NXFP for protein) described by SDDL schemas, trains a single domain-specific compressor, and compresses all chunks. For FASTQ, decomposes into typed binary streams and compresses each independently. Both bundle into `.zlfasta`/`.zlfastq` containers with byte-exact reconstruction. Auto-detects nucleotide vs protein FASTA.

Nyx replaces an earlier bash-script-driven pipeline with a proper Python package that can be `pip install`-ed and invoked as a single `nyx compress genome.fasta` command.

## 2. Architecture & Data Flow

### 2.1 High-Level Design

Nyx follows a **CLI orchestrator + native binary** pattern. The Python layer handles argument parsing, file type detection, progress display, temporary directory management, archive bundling, and competitor benchmarking. All computationally expensive work — preprocessing multi-GB genomic files, training compression models, compressing/decompressing data — is delegated to two C++ binaries via `subprocess`:

| Binary | Source | Built By | Purpose |
|--------|--------|----------|---------|
| `zli` | OpenZL (facebook/openzl) | `make` inside `nyx/openzl/` | Training compressors, compressing, decompressing, benchmarking, inspecting profiles |
| `genomic_preprocessor` | `nyx/tools/genomic_preprocessor.cpp` | `g++ -O3 -std=c++17` | Converting text FASTA/FASTQ/VCF into structured binary chunks matching SDDL schemas |
| `fasta_codec` | `nyx/tools/fasta_codec.cpp` + `codec_common.h` | `g++ -O3 -std=c++17 -pthread` | Lossless FASTA ↔ binary stream encoding/decoding (parallel) |
| `fastq_codec` | `nyx/tools/fastq_codec.cpp` + `codec_common.h` | `g++ -O3 -std=c++17 -pthread` | Lossless FASTQ ↔ binary stream encoding/decoding (parallel) |

### 2.2 The Python <-> OpenZL Bridge

**Mechanism:** `subprocess.run()` and `subprocess.Popen()` calls to the `zli` binary.

**Bridge files:**

| File | Wraps | Key Functions |
|------|-------|---------------|
| `nyx/nyx/core/openzl.py` | `zli` binary | `compress()`, `decompress()`, `train()`, `train_async()`, `benchmark()`, `inspect()`, `list_profiles()`, `passthrough()` |
| `nyx/nyx/core/preprocessor.py` | `genomic_preprocessor` binary | `preprocess()` |
| `nyx/nyx/core/codec.py` | `fasta_codec` binary | `encode()`, `decode()`, `encode_packed()`, `decode_packed()`, `encode_protein_packed()`, `decode_protein_packed()` |
| `nyx/nyx/core/fastq_codec.py` | `fastq_codec` binary | `encode()`, `decode()`, `validate()` |
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

### 2.4 Lossless Pipeline — FASTA (Packed NXF2/NXFP)

```
nyx compress-lossless genome.fasta
    │
    ▼
[Python: detect file type]  ─── "fasta"
    │
    ▼
[Python: detect subtype]  ─── detect_fasta_subtype() → "nucleotide" or "protein"
    │
    ▼
[C++: encode-packed / encode-protein-packed]  ─── fasta_codec (parallel)
    │  Nucleotide: NXF2 packed binary (48B header + 36B×N metadata + 7 payloads)
    │  Protein:    NXFP packed binary (32B header + 20B×N metadata + 4 payloads)
    │  Multi-chunk splitting if input >500 MiB
    ▼
[C++: train SDDL compressor]  ─── zli train --profile sddl (if --train)
    │  Single compressor for entire packed format
    ▼
[C++: compress chunks]  ─── zli compress (parallel via ThreadPoolExecutor)
    │
    ▼
[Python: bundle container]  ─── .zlfasta (magic + CRC32)
    │
    ▼
genome.fasta.zlfasta
```

### 2.5 Lossless Pipeline — FASTQ (Per-Stream)

```
nyx compress-lossless reads.fastq
    │
    ▼
[Python: detect file type]  ─── "fastq"
    │
    ▼
[C++: codec encode]  ─── fastq_codec (parallel, multi-threaded)
    │  Decomposes into typed binary streams:
    │  headers, nmask, acgtmask, bases2, exceptions, case, seq_wrap, qual_wrap, quality, plus
    ▼
[C++: OpenZL compress each stream]  ─── zli compress (serial profile or trained)
    │
    ▼
[Python: bundle container]  ─── .zlfastq (magic + CRC32)
    │
    ▼
reads.fastq.zlfastq
```

Decompression reverses the pipeline: extract container → decompress streams/chunks → C++ codec decode → byte-identical original file.

## 3. The Python CLI

**Entry Point:** `nyx/nyx/cli.py` → function `main()` registered as `nyx` console script in `pyproject.toml`

**Framework:** [Click](https://click.palletsprojects.com/) (>= 8.0) with [tqdm](https://tqdm.github.io/) (>= 4.60) for progress bars

**Installation:** `pip install -e ./nyx` then `nyx build` to compile C++ binaries

### 3.1 Command Reference

| Command | Key Arguments / Flags | Purpose | Implementation |
|---------|----------------------|---------|----------------|
| `nyx compress` | `<file> [-o PATH] [--mode MODE] [--sddl PATH] [--type TYPE] [--threads N] [--max-time-secs N] [--compress-jobs N] [--target-train-mib N] [--trainer ALGO] [--no-clustering] [--benchmark] [--keep-temp] [-v] [-f]` | Schema-aware compression. Detects type, preprocesses, trains, compresses, bundles `.nyx` archive. Optionally benchmarks against competitors. | `nyx/nyx/commands/compress.py:compress_cmd` |
| `nyx decompress` | `<file.nyx> [-o PATH] [-f] [--keep-temp] [-v]` | Extracts `.nyx` archive, decompresses all chunks via `zli decompress` | `nyx/nyx/commands/decompress.py:decompress_cmd` |
| `nyx compress-lossless` | `<file> [-o PATH] [-t TYPE] [--threads N] [--train] [--models-dir PATH] [--no-trained] [-v] [-f]` | **Unified lossless compression.** Auto-detects FASTA/FASTQ, encodes into typed binary streams, compresses each with OpenZL, bundles into `.zlfasta`/`.zlfastq`. Byte-exact reconstruction. | `nyx/nyx/commands/compress_lossless.py:compress_lossless_cmd` |
| `nyx decompress-lossless` | `<file> [-o PATH] [-v] [-f]` | **Unified lossless decompression.** Auto-detects container type from magic bytes (`ZLFASTA\0`/`ZLFASTQ\0`), decompresses streams, reconstructs original file. | `nyx/nyx/commands/decompress_lossless.py:decompress_lossless_cmd` |
| `nyx compress-lossless-fastq` | `<file> [-o PATH] [--train] [--models-dir PATH] [--no-trained] [--train-sample-mib N] [--max-time-secs N] [--train-threads N] [--compress-jobs N] [-v] [-f]` | Format-specific FASTQ lossless compression. Illumina headers auto-detected and dictionary-encoded. Parallel stream compression. Trained models reusable across all FASTQ files. | `nyx/nyx/commands/compress_lossless_fastq.py` |
| `nyx decompress-lossless-fastq` | `<file> [-o PATH] [-v] [-f]` | Format-specific FASTQ lossless decompression. Parallel stream decompression. Supports v1/v2/v3 archives. | `nyx/nyx/commands/decompress_lossless_fastq.py` |
| `nyx train` | `<sample_dir> -o PATH [-p PROFILE] [--profile-arg PATH] [-c COMPRESSOR] [--threads N] [--max-time-secs N] [--use-all-samples] [--no-ace-successors] [--no-clustering] [--trainer ALGO] [-f] [-v]` | Direct passthrough to `zli train`. Unknown flags forwarded to zli. | `nyx/nyx/commands/train.py:train_cmd` |
| `nyx benchmark` | `<input_dir> [-v]` | Direct passthrough to `zli benchmark`. Unknown flags forwarded. | `nyx/nyx/commands/benchmark.py:benchmark_cmd` |
| `nyx inspect` | `<compressor_file> [-v]` | Inspect a trained compressor (JSON output). Passthrough to `zli inspect`. | `nyx/nyx/commands/inspect_cmd.py:inspect_cmd` |
| `nyx list-profiles` | `[-v]` | List available OpenZL compression profiles. Passthrough to `zli list-profiles`. | `nyx/nyx/commands/list_profiles.py:list_profiles_cmd` |
| `nyx build` | `[-j N] [-v]` | Clones pinned OpenZL, compiles `zli`, `fasta_codec`, `fastq_codec`, and `genomic_preprocessor`. One-time setup. | `nyx/nyx/commands/build.py:build_cmd` |
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
    │   ├── fasta_packed.sddl             # FAV4 binary format schema (29 lines)
    │   ├── nucleotide_fasta.sddl         # NXF2 packed binary format schema
    │   └── protein_fasta.sddl            # NXFP protein packed binary format schema
    │
    ├── scripts/                          # Build and data-prep scripts (called by Python)
    │   ├── build.sh                      # Clones OpenZL + compiles zli + codecs + preprocessor
    │   ├── get_openzl.sh                 # Standalone OpenZL clone/pin script
    │   └── make_train_sample.py          # Record-safe FASTA training sampler (Python, stdlib only)
    │
    ├── tools/                            # C++ source code
    │   ├── codec_common.h                # Shared header: mmap, varint, bit packing, sequence encode/decode
    │   ├── fasta_codec.cpp               # Lossless FASTA ↔ binary streams (parallel encoding)
    │   ├── fastq_codec.cpp               # Lossless FASTQ ↔ binary streams (parallel encoding)
    │   └── genomic_preprocessor.cpp      # 886-line C++17 preprocessor (FASTA/FASTQ/VCF → binary chunks)
    │
    ├── models/                           # Trained compressor models
    │   ├── lossless/                     # FASTA compressors (nucleotide_fasta.zl_compressor, protein_fasta.zl_compressor)
    │   └── lossless_fastq/               # FASTQ per-stream compressors (*.zl_compressor)
    │
    ├── tests/                            # Pytest test suite (180 tests)
    │   ├── __init__.py
    │   ├── test_lossless.py              # FASTA round-trip tests (111 tests across 7 tiers)
    │   ├── test_lossless_fastq.py        # FASTQ round-trip tests (69 tests across 4 tiers)
    │   └── fixtures/                     # Test input files
    │       ├── *.fasta                   # FASTA test fixtures (nucleotide + protein edge cases)
    │       └── *.fastq                   # FASTQ test fixtures (CRLF, no-trailing-newline, etc.)
    │
    ├── nyx/                              # Python package source (the importable module)
    │   ├── __init__.py                   # __version__ = "0.1.0"
    │   ├── cli.py                        # Click group entry point; registers all commands
    │   │
    │   ├── commands/                     # CLI command implementations
    │   │   ├── __init__.py               # Empty
    │   │   ├── compress.py               # nyx compress — schema-aware pipeline (504 lines)
    │   │   ├── compress_lossless.py      # nyx compress-lossless — unified FASTA/FASTQ (auto-detect)
    │   │   ├── compress_lossless_fastq.py  # nyx compress-lossless-fastq — explicit FASTQ
    │   │   ├── decompress.py             # nyx decompress — .nyx archive extraction
    │   │   ├── decompress_lossless.py    # nyx decompress-lossless — unified (auto-detect container)
    │   │   ├── decompress_lossless_fastq.py # nyx decompress-lossless-fastq — explicit FASTQ
    │   │   ├── train.py                  # nyx train — passthrough to zli train
    │   │   ├── benchmark.py              # nyx benchmark — passthrough to zli benchmark
    │   │   ├── inspect_cmd.py            # nyx inspect — passthrough to zli inspect
    │   │   ├── list_profiles.py          # nyx list-profiles — passthrough to zli list-profiles
    │   │   └── build.py                  # nyx build — invokes scripts/build.sh
    │   │
    │   ├── core/                         # Business logic and binary wrappers
    │   │   ├── __init__.py               # Empty
    │   │   ├── openzl.py                 # zli subprocess wrapper (268 lines)
    │   │   ├── codec.py                  # fasta_codec subprocess wrapper (encode/decode/validate)
    │   │   ├── fastq_codec.py            # fastq_codec subprocess wrapper (encode/decode/validate)
    │   │   ├── zlfasta.py                # .zlfasta container read/write (magic: ZLFASTA\0)
    │   │   ├── zlfastq.py                # .zlfastq container read/write (magic: ZLFASTQ\0)
    │   │   ├── preprocessor.py           # genomic_preprocessor subprocess wrapper (74 lines)
    │   │   ├── detect.py                 # File type auto-detection (content + extension) (71 lines)
    │   │   ├── archive.py                # .nyx tar archive read/write (113 lines)
    │   │   ├── sample.py                 # Training sample creation wrapper (67 lines)
    │   │   ├── benchmark.py              # Competitor benchmarks (gzip/pigz/zstd) (250 lines)
    │   │   └── config.py                 # Schema registry, defaults, constants (45 lines)
    │   │
    │   └── utils/                        # Utility modules
    │       ├── __init__.py               # Empty
    │       └── paths.py                  # Binary/schema path resolution (find_fasta_codec, find_fastq_codec, etc.)
    │
    ├── openzl/                           # [GITIGNORED] OpenZL source checkout + built zli binary
    ├── bin/                              # [GITIGNORED] Compiled binaries (codecs + preprocessor)
    ├── .venv/                            # [GITIGNORED] Python virtual environment
    └── nyx.egg-info/                     # [GITIGNORED] Python package metadata
```

## 5. Core Components — Detailed Breakdown

### 5.1 CLI Entry Point: `nyx/nyx/cli.py`

- **Purpose:** Defines the Click `@click.group()` and registers all subcommands.
- **Entry point registration:** `pyproject.toml` → `[project.scripts]` → `nyx = "nyx.cli:main"`
- **Commands registered:** `compress`, `decompress`, `compress-lossless`, `decompress-lossless`, `compress-lossless-fastq`, `decompress-lossless-fastq`, `train`, `benchmark`, `inspect`, `list-profiles`, `build`
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

### 5.4 Lossless Compress Command: `nyx/nyx/commands/compress_lossless.py`

- **Purpose:** Unified lossless compression for FASTA and FASTQ with byte-exact reconstruction. Auto-detects input format and FASTA subtype (nucleotide vs protein).
- **Key features:**
  - Auto-detection via `detect_filetype()` (or explicit `--type fasta`/`--type fastq`/`--type protein`)
  - FASTA subtype auto-detection via `detect_fasta_subtype()` — uses E/F/I/L/P/Q heuristic
  - **FASTA (nucleotide):** Encodes into NXF2 packed binary chunks via `codec.encode_packed()`, trains single SDDL compressor (`nucleotide_fasta.zl_compressor`), compresses all chunks in parallel
  - **FASTA (protein):** Encodes into NXFP packed binary chunks via `codec.encode_protein_packed()`, trains single SDDL compressor (`protein_fasta.zl_compressor`), compresses all chunks in parallel
  - **FASTQ:** Routes to per-stream compression path (legacy approach with `fastq_codec.encode()`)
  - Group training via `--group-train-dir` — samples first chunk from each FASTA file in directory
  - Auto-chunking for large files (>500 MiB NXF2/NXFP chunks) to stay within zli limits
  - Bundles into `.zlfasta` or `.zlfastq` container

- **FASTA Pipeline (NXF2/NXFP):**

```
input.fasta
    │
    ▼
[detect type] → "fasta" → [detect subtype] → "nucleotide" or "protein"
    │
    ▼
[C++ encode-packed / encode-protein-packed] → NXF2/NXFP packed binary chunks
    │
    ▼ (optional)
[train single SDDL compressor] → nucleotide_fasta.zl_compressor / protein_fasta.zl_compressor
    │
    ▼
[OpenZL compress chunks in parallel] → .zl compressed chunks
    │
    ▼
[bundle container] → input.fasta.zlfasta
```

- **FASTQ Pipeline (per-stream):** Same as before — `fastq_codec encode` → per-stream OpenZL compression → `.zlfastq` container

### 5.5 Lossless Decompress Command: `nyx/nyx/commands/decompress_lossless.py`

- **Purpose:** Unified lossless decompression from `.zlfasta` or `.zlfastq` containers. Auto-detects container type from magic bytes.
- **Magic bytes:** `ZLFASTA\0` (8 bytes) for FASTA, `ZLFASTQ\0` (8 bytes) for FASTQ
- **Packed format detection:** `_is_packed_format()` checks for `chunk_*.bin` entries → routes to packed decompression path
- **Subtype detection:** `_detect_packed_subtype()` reads magic from first decompressed chunk — "NXF2" → `codec.decode_packed()`, "NXFP" → `codec.decode_protein_packed()`
- **Parallel decompression:** All chunks/streams decompressed in parallel via `ThreadPoolExecutor`
- **Pipeline (packed FASTA):** Extract container → decompress all chunks in parallel → detect NXF2/NXFP → decode via C++ codec → original file
- **Pipeline (per-stream FASTQ):** Extract container → decompress streams → reassemble chunked streams → decode via C++ codec → original file
- **Output:** Byte-identical reconstruction of the original input file

### 5.6 C++ Lossless Codecs

#### 5.6.1 Shared Header: `nyx/tools/codec_common.h`

- **Purpose:** Common code shared between `fasta_codec.cpp` and `fastq_codec.cpp`, eliminating duplication.
- **Contents:**
  - Type aliases (`u8`, `u16`, `u32`, `u64`)
  - `MappedFile` — memory-mapped file I/O wrapper (mmap + MADV_SEQUENTIAL)
  - Binary I/O helpers (`write_u32_le`, `read_u32_le`, `write_varint`, `read_varint`)
  - Bit packing utilities (`BitWriter`, `BitReader`)
  - Base encoding (`base_to_2bit`, `twobit_to_base`)
  - `SequenceEncoding` struct — result of encoding a sequence into N-mask, ACGT-mask, 2-bit bases, exceptions, case bits
  - `encode_sequence()` — thread-safe per-sequence encoding (no I/O)
  - `decode_sequence()` — reconstructs raw sequence from stream buffers; handles absent streams via `SFLAG_NO_N`, `SFLAG_NO_IUPAC`, `SFLAG_NO_CASE` flags
  - `encode_wrapping()` / `decode_wrapping()` — line-wrap position encoding
  - `detect_newline_style()` — detects `\n` vs `\r\n` for CRLF support
  - `detect_trailing_newline()` — detects whether file ends with a newline
  - Stream-presence flags: `SFLAG_NO_N` (0x01), `SFLAG_NO_IUPAC` (0x02), `SFLAG_NO_CASE` (0x04) — when set, corresponding payload buffers are omitted from packed binary
  - Case encoding constants: `CASE_NONE` (0), `CASE_MASK` (1), `CASE_SPARSE` (2) — shared by nucleotide and protein codecs
  - `mkdir_recursive()` — cross-platform directory creation

#### 5.6.2 FASTA Codec: `nyx/tools/fasta_codec.cpp`

- **Purpose:** Lossless FASTA ↔ binary encoding/decoding with parallel encoding support. Handles both nucleotide and protein FASTA.
- **Formats supported:**
  - **Legacy per-stream** (`encode`/`decode`): Splits into 8 individual stream files (headers, nmask, acgtmask, bases2, exceptions, case, wrapping, meta). Used by old pipeline.
  - **NXF2 packed nucleotide** (`encode-packed`/`decode-packed`): Self-contained packed binary chunks. 48-byte `PackedHeader` + 36-byte `RecordMeta` array + 7 payload buffers. Stream-presence flags (`SFLAG_NO_N`, `SFLAG_NO_IUPAC`, `SFLAG_NO_CASE`) allow omitting empty streams.
  - **NXFP packed protein** (`encode-protein-packed`/`decode-protein-packed`): Simpler packed binary for amino acid sequences. 32-byte `ProteinPackedHeader` + 20-byte `ProteinRecordMeta` array + 4 payload buffers (headers, seq, case, wrapping). Sequences stored as raw uppercase bytes (1 byte per AA).
- **3-phase parallel encoding (all modes):**
  1. **Parse** (sequential): Scan mmap'd file for `>` headers + sequence data → `FastaRecord` list
  2. **Encode** (parallel): `std::thread` workers call `encode_one()` / `encode_protein_one()` per record — thread-safe, no shared state
  3. **Write** (sequential): Concatenate per-record results into packed binary or stream files — deterministic output regardless of thread count
- **Multi-chunk splitting:** `encode-packed` and `encode-protein-packed` accept `num_chunks` parameter, split records across multiple `chunk_NNNNNN.bin` files (required for >500 MiB inputs due to zli limit)
- **CLI:** `fasta_codec encode|decode|validate|encode-packed|decode-packed|encode-protein-packed|decode-protein-packed <args> [num_threads|num_chunks]`

#### 5.6.3 FASTQ Codec: `nyx/tools/fastq_codec.cpp`

- **Purpose:** Lossless FASTQ ↔ binary stream encoding/decoding with parallel encoding support.
- **Size:** ~1200 lines
- **Format version:** v3 (backward-compatible decoder for v1, v2, v3)
- **Metadata magic:** `0x4E584651` ("NXFQ")
- **Record metadata (48 bytes):** All FASTA fields + plus-line offset/length, quality offset/length, quality encoding type, seq/qual wrapping info

- **v3 meta header layout:**
  - `[0-3]`: magic "NXFQ"
  - `[4-7]`: version (3)
  - `[8-11]`: num_records
  - `[12]`: newline_style (0=LF, 1=CRLF)
  - `[13]`: has_trailing_nl
  - `[14]`: quality_layout (0=per-record, 1=columnar, 2=per-position+delta, 3=per-position+raw)
  - `[15]`: header_mode (0=LCP, 1=Illumina)
  - `[16-19]`: fixed_seq_len
  - `[20-23]`: prefix_len (LCP mode) or illumina_block_size (Illumina mode)
  - `[24..]`: LCP prefix bytes or Illumina dictionary block

- **Header modes:**
  - `LCP (header_mode=0)`: Common prefix stripped from all headers, stored once. Suffixes in `headers.bin`. `@` symbol stripped in v3 (prepended on decode).
  - `Illumina (header_mode=1)`: Auto-detected when all headers match `@PREFIX.READNUM INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y` (7 colon-separated fields). Dictionary encoding for low-cardinality fields (run, flowcell, lane, tile → u8 indices). Sequential read numbers dropped (reconstructed 1..N on decode). `@` stripped. ~4.4x header size reduction before compression.

- **Illumina dictionary block:** flags byte + constant prefix + constant instrument + sorted dictionaries for run/flowcell/lane/tile + per-record binary (4 dict indices + variable-length X/Y coordinates).

- **Fallback:** If any header in the first batch fails Illumina parsing, the entire file falls back to LCP mode. No data loss, no format change.

- **Quality encoding:** Raw (v2/v3 always use `quality_mode=0`). Per-position quality files (`quality_pos_XXXX.bin`) for fixed-length reads ≤1000bp.

- **Threading:** Atomic work-stealing (`std::atomic<u32>` with `fetch_add`) for balanced load across encoding threads.

- **Additional streams (vs FASTA):** `plus.bin`, `seq_wrap.bin`, `qual_wrap.bin`, `quality.bin` (or `quality_pos_*.bin`)
- **CLI:** `fastq_codec encode|decode|validate <args> [num_threads]`

### 5.7 Lossless Stream Encoding — Detailed Algorithm

#### Nucleotide Encoding

For each sequence in a nucleotide FASTA/FASTQ record, `encode_sequence()` produces:

1. **N-mask (`nmask.bin`):** L bits, one per base. `1` = position is N/n, `0` = non-N.
2. **S_nonN construction:** Remove all N positions from the sequence → S_nonN (length L').
3. **ACGT-mask (`acgtmask.bin`):** L' bits over S_nonN. `1` = standard base (ACGT/acgt), `0` = IUPAC ambiguity or other exception.
4. **2-bit bases (`bases2.bin`):** For each `1` in ACGT-mask, encode the base as 2 bits: A=00, C=01, G=10, T=11. MSB-first packing.
5. **Exceptions (`exceptions.bin`):** For each `0` in ACGT-mask, store the raw byte.
6. **Case bits (`case.bin`):** L bits, one per base. `1` = uppercase, `0` = lowercase.
7. **Wrapping (`wrapping.bin`):** Line-break positions within the sequence, encoded as varint deltas.

This decomposition achieves high compression because each stream has low entropy and consistent patterns (e.g., 2-bit bases are ~2 bits/base instead of 8, N-mask is sparse for clean genomes).

#### Protein Encoding

For each sequence in a protein FASTA record, `encode_protein_one()` produces:

1. **Uppercase sequence (`seq_buf`):** Raw uppercase bytes (1 byte per amino acid). Full alphabet: 20 standard AAs + X, *, -, B, Z, J, U, O.
2. **Case bits (`case_buf`):** Same encoding as nucleotide (CASE_NONE/CASE_MASK/CASE_SPARSE from `codec_common.h`).
3. **Wrapping (`wrapping_buf`):** Same as nucleotide (`encode_wrapping()` from `codec_common.h`).

Protein encoding is simpler than nucleotide — no N-mask, ACGT-mask, 2-bit bases, or IUPAC exceptions. The raw uppercase byte representation lets OpenZL learn the amino acid frequency distribution via SDDL training.

### 5.8 Container Formats

#### `.zlfasta` container

- **Magic:** `ZLFASTA\0` (8 bytes)
- **Entries (packed format):** One or more compressed `chunk_NNNNNN.bin` entries (each a compressed NXF2 or NXFP packed binary)
- **Entries (legacy format):** 7 compressed streams + `meta.bin` (uncompressed record metadata)
- **Structure:** Magic + entry count (u32) + per-entry headers (name length, name, original size, compressed size) + entry data + CRC32 footer
- **Module:** `nyx/nyx/core/zlfasta.py` — `create_zlfasta()`, `extract_zlfasta()`

#### `.zlfastq` container

- **Magic:** `ZLFASTQ\0` (8 bytes)
- **Entries:** 10 compressed streams + `meta.bin` (uncompressed record metadata)
- **Structure:** Same layout as `.zlfasta` but with additional FASTQ-specific streams
- **Module:** `nyx/nyx/core/zlfastq.py` — `create_zlfastq()`, `extract_zlfastq()`

### 5.9 OpenZL Wrapper: `nyx/nyx/core/openzl.py`

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

### 5.10 Preprocessor Wrapper: `nyx/nyx/core/preprocessor.py`

- **Purpose:** Wraps the `genomic_preprocessor` binary.
- **Single function:** `preprocess(input_file, output_dir, threads, filetype, verbose)` → returns sorted list of generated chunk `Path` objects.
- **Error handling:** `PreprocessorError` raised if exit code is non-zero or no chunks are produced.

### 5.11 File Type Detection: `nyx/nyx/core/detect.py`

- **Purpose:** Auto-detects whether an input file is FASTA, FASTQ, VCF, or unknown. Also detects FASTA subtype (nucleotide vs protein).
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

- **FASTA subtype detection** (`detect_fasta_subtype()`):
  - Reads first 10KB, scans sequence lines (non-header lines)
  - If any character in `{E, F, I, L, P, Q}` (amino acids NOT valid IUPAC nucleotide codes) → `"protein"`
  - Otherwise → `"nucleotide"`
  - Standard bioinformatics heuristic; case-insensitive

### 5.12 Archive Module: `nyx/nyx/core/archive.py`

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

### 5.13 Config & Schema Registry: `nyx/nyx/core/config.py`

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

### 5.14 Benchmark Module: `nyx/nyx/core/benchmark.py`

- **Purpose:** Runs competitor compressors (gzip, pigz, zstd) on the original input file and produces a comparison table.
- **Data class:** `BenchmarkResult` with properties `ratio`, `savings_pct`, `speed_mbps`.
- **Execution:** Each competitor runs in a `threading.Thread` with a tqdm progress bar showing elapsed time and estimated progress (by polling output file size).
- **Output table:** Formatted columns for Compressor, Compressed size, Ratio, Savings %, Speed (MB/s), Time. Nyx marked with `*`.

### 5.15 Path Resolution: `nyx/nyx/utils/paths.py`

- **Purpose:** Locates binaries (`zli`, `genomic_preprocessor`), schemas, and scripts at runtime.
- **`_nyx_root()`:** Returns `Path(__file__).resolve().parents[2]` — the `nyx/` directory containing `pyproject.toml`.
- **Search order for all binaries:** Environment variable → local build path → system PATH → `FileNotFoundError`.
- **Functions:** `find_zli()`, `find_preprocessor()`, `find_schema(name)`, `find_make_train_sample()`

### 5.16 C++ Preprocessor: `nyx/tools/genomic_preprocessor.cpp`

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

### 5.17 SDDL Schemas: `nyx/schemas/`

#### `fasta_packed.sddl`

- **Purpose:** Defines the FAV4 binary wire format consumed by `zli train --profile sddl`.
- **Fields:** `magic` (Byte[4]), `num_records` (U32), `hdr_offsets` (U32[N+1]), `seq_offsets` (U32[N+1]), `seq_lengths` (U32[N]), `hdr_total` (U32), `seq_total` (U32), `hdr_pad` (U32), `seq_pad` (U32), `headers` (Byte[...]), `sequences` (Byte[...]), `: Byte[_rem]` (permissive trailer).
- **All integers:** Little-endian 32-bit unsigned (`U32 = UInt32LE`).
- **Why this matters:** By teaching OpenZL the field types, it can learn separate compression models for each — delta coding for monotonic offset arrays, specialized models for the constrained 5-symbol packed alphabet, etc.

#### `nucleotide_fasta.sddl`

- **Purpose:** Defines the NXF2 packed binary format for nucleotide FASTA.
- **Structure:** 48-byte `PackedHeader` (magic, version, num_records, newline_style, has_trailing_nl, stream_flags, reserved, 7 total_* size fields) → `RecordMeta[num_records]` (36 bytes each: seq_len, hdr_len, raw_seq_len, n_count, except_count, non_n_count, nmask_bytes, acgtmask_bytes, case_bytes, wrapping_bytes, bases2_bytes, case_mode, pad) → 7 payload buffers (hdr, nmask, acgtmask, bases2, exceptions, case, wrapping) → `Byte[_rem]`.
- **Stream-presence flags:** `stream_flags` field controls which payload buffers are present; absent buffers have zero total size.

#### `protein_fasta.sddl`

- **Purpose:** Defines the NXFP packed binary format for protein FASTA.
- **Structure:** 32-byte `ProteinPackedHeader` (magic "NXFP", version, num_records, newline_style, has_trailing_nl, stream_flags, reserved, 4 total_* size fields) → `ProteinRecordMeta[num_records]` (20 bytes each: seq_len, hdr_len, case_bytes, wrap_bytes, case_mode, pad) → 4 payload buffers (hdr, seq, case, wrapping) → `Byte[_rem]`.
- **Protein sequences:** Stored as raw uppercase bytes (1 byte per amino acid) — OpenZL learns the AA frequency distribution via SDDL training.

### 5.18 Build Script: `nyx/scripts/build.sh`

- **Purpose:** Single script that clones OpenZL (pinned commit `e40fe9f3`), builds `zli` via `make`, and compiles `genomic_preprocessor` via `g++`.
- **Invoked by:** `nyx build` command (`nyx/nyx/commands/build.py`).
- **Pinned commit:** `e40fe9f314283047147d573d113fa7d17eabf7ac` (overridable via `OPENZL_COMMIT`).
- **Build flags:** `env -u CFLAGS -u CXXFLAGS ... make -j$(nproc) MOREFLAGS="-pthread"` for OpenZL; `g++ -O3 -std=c++17 -pthread` for the preprocessor.
- **Outputs:** `nyx/openzl/zli` and `nyx/bin/genomic_preprocessor`.

### 5.19 Training Sampler: `nyx/scripts/make_train_sample.py`

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
requires-python = ">=3.8"
dependencies = ["click>=8.0", "tqdm>=4.60"]

[project.scripts]
nyx = "nyx.cli:main"
```

Build backend: setuptools (>= 68.0) + wheel.

### 9.2 Full Dependency Inventory

| Dependency | Type | Version | Declared In | Purpose |
|-----------|------|---------|-------------|---------|
| Python | Runtime | >= 3.8 | `pyproject.toml` | CLI orchestration |
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
| Python 3.8+ | Yes | Pre-installed or `pyenv install 3.8.20` | `apt install python3 python3-pip` |
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

### 12.3 `nyx compress-lossless genome.fasta` (FASTA — packed NXF2/NXFP)

1. **CLI parsing:** `compress_lossless_cmd()` — parses Click args
2. **Type detection:** `detect_filetype(input_path)` → `"fasta"` (or `--type` override)
3. **Subtype detection:** `detect_fasta_subtype(input_path)` → `"nucleotide"` or `"protein"` (or `--type protein` override)
4. **Encode-packed:** `codec.encode_packed()` or `codec.encode_protein_packed()` → subprocess: `fasta_codec encode-packed <input> <dir> [chunks]` → NXF2/NXFP packed binary chunk files
5. **Auto-chunking:** If any chunk >500 MiB, re-encode with more chunks
6. **Train (if `--train`):** `openzl.train(profile="sddl", profile_arg=<sddl>)` → trains single compressor on packed binary sample → saves `nucleotide_fasta.zl_compressor` or `protein_fasta.zl_compressor` to `models/lossless/`
7. **Compress chunks:** Parallel `openzl.compress()` with trained compressor or serial fallback → `.zl` data
8. **Bundle:** `create_zlfasta(output_path, entries)` → `.zlfasta` container with magic bytes + CRC32

### 12.4 `nyx decompress-lossless genome.fasta.zlfasta`

1. **CLI parsing:** `decompress_lossless_cmd()` — parses args
2. **Detect container:** Read first 8 bytes → `ZLFASTA\0` → format = "fasta"
3. **Extract:** `extract_zlfasta(input_path, extract_dir)` → individual entry files
4. **Detect packed format:** `_is_packed_format()` checks for `chunk_*.bin` entries
5. **Decompress chunks:** Parallel `openzl.decompress()` for all chunks → decompressed packed binary
6. **Detect subtype:** `_detect_packed_subtype()` reads magic from first chunk — "NXF2" → nucleotide, "NXFP" → protein
7. **Decode:** `codec.decode_packed()` or `codec.decode_protein_packed()` → subprocess: `fasta_codec decode-packed <dir> <output>` → byte-identical original file

## 13. Testing

### 13.1 Current State

The project has **180 automated tests** covering lossless compression round-trips:

| Test File | Tests | Coverage |
|-----------|-------|----------|
| `nyx/tests/test_lossless.py` | 111 | FASTA lossless encode/decode round-trips (nucleotide + protein) |
| `nyx/tests/test_lossless_fastq.py` | 69 | FASTQ lossless encode/decode round-trips |

### 13.2 Test Architecture

Tests are organized in 7 tiers (FASTA) / 4 tiers (FASTQ):

1. **Fixture round-trips:** Each test fixture encoded into streams/packed binary, decoded back, compared byte-for-byte.
2. **Programmatic edge cases:** Synthetic inputs generated in Python (empty sequences, single-base, long sequences, etc.).
3. **Validation tests:** Run the C++ codec's `validate` command on each fixture to verify stream invariants.
4. **Multi-threaded round-trips:** Same as tier 1 but with `--threads 4` to verify parallel encoding produces identical output.
5. **Protein codec round-trips:** `encode-protein-packed` → `decode-protein-packed` for protein fixtures, including multi-chunk splitting and NXFP binary structure validation.
5b. **Protein edge cases:** Single AA, all lowercase, all uppercase, stops, gaps, X chars, empty sequence, no trailing newline.
6. **FASTA subtype detection:** Verifies `detect_fasta_subtype()` correctly identifies nucleotide vs protein fixtures.
7. **Protein full pipeline:** Full `compress-lossless --type protein` → `decompress-lossless` round-trip with byte-identical verification.

### 13.3 Test Fixtures

**Nucleotide FASTA fixtures** (`nyx/tests/fixtures/*.fasta`): minimal, multi-record, wrapped sequences, mixed case, IUPAC ambiguity codes, long sequences, edge cases, CRLF line endings, no trailing newline.

**Protein FASTA fixtures** (`nyx/tests/fixtures/protein_*.fasta`): basic (hemoglobin, insulin, titin, SARS-CoV-2 spike), extended (B/Z/J/U/O/X chars, stops, gaps, single AA, all lowercase, unusual wrapping, empty sequence, long headers).

**FASTQ fixtures** (`nyx/tests/fixtures/*.fastq`): minimal, multi-record, wrapped reads, mixed case, IUPAC ambiguity, varying quality scores, long reads, plus-line comments, edge cases, CRLF line endings, no trailing newline.

### 13.4 Running Tests

```bash
# Activate the venv first
source nyx/.venv/bin/activate

# Run all tests
pytest nyx/tests/ -v

# Run only FASTA tests
pytest nyx/tests/test_lossless.py -v

# Run only FASTQ tests
pytest nyx/tests/test_lossless_fastq.py -v
```

**Note:** Tests require the `fasta_codec` and `fastq_codec` binaries to be built (`nyx build`).

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

### 14.3 Lossless Compression Has Full Test Coverage

The lossless pipeline (`compress-lossless` / `decompress-lossless`) has 121 automated tests covering FASTA and FASTQ round-trips, edge cases, CRLF handling, and multi-threaded encoding. The schema-aware pipeline (`compress` / `decompress`) still relies on manual testing.

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
| **`fasta_codec`** | C++ binary for lossless FASTA ↔ binary stream encoding/decoding |
| **`fastq_codec`** | C++ binary for lossless FASTQ ↔ binary stream encoding/decoding |
| **`codec_common.h`** | Shared C++ header with mmap, varint, bit packing, and sequence encode/decode |
| **`.zlfasta`** | Lossless FASTA container: magic bytes + compressed streams + CRC32 |
| **`.zlfastq`** | Lossless FASTQ container: magic bytes + compressed streams + CRC32 |
| **N-mask** | Bit stream marking N/n positions in a sequence (1=N, 0=non-N) |
| **ACGT-mask** | Bit stream over non-N positions marking standard bases vs exceptions |
| **2-bit encoding** | Encoding standard bases as 2 bits: A=00, C=01, G=10, T=11, MSB-first |
| **Stream separation** | Decomposing genomic records into typed streams for independent compression |
| **GRCm39** | Genome Reference Consortium Mouse Build 39 — default test genome (~2.76 GB) |
