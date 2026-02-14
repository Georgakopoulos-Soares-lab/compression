# Developer's Guide: OpenZL Genomic Compression Pipeline

> **Repository purpose:** A self-contained, reproducible pipeline that leverages **Meta's OpenZL framework** to train domain-specific compressors for genomic data. Raw FASTA files are preprocessed into a structured binary format described by an SDDL schema, enabling OpenZL to learn field-specific compression strategies — delta coding for monotonically increasing offset arrays, specialized models for a constrained DNA base alphabet, dictionary approaches for header text — and then compress the data significantly better than general-purpose tools like gzip or zstd. A Python CLI (`nyx`) automates the full pipeline and provides a convenient interface.

---

## 1. How OpenZL Works (The Key Ideas)

### 1.1 Schema-Aware Compression

Most compressors (gzip, zstd, bzip2) treat input as an opaque byte stream. They have no way to know that bytes 0–3 are a magic number, bytes 4–7 are a little-endian integer, and the next N kilobytes are DNA bases packed into nibbles. They apply a one-size-fits-all model.

**OpenZL takes a fundamentally different approach.** You provide a **schema** written in **SDDL** (Simple Data Description Language) that describes the structure of your binary data. OpenZL decomposes each input file into typed fields — integers, byte arrays, packed data — and trains a **separate compression model for each field**. This is why OpenZL can outperform generic compressors on structured data: it knows what the data means.

### 1.2 Training: The Compression Optimizer

OpenZL's `zli train` command is an **offline optimization process**. Given a directory of sample files and an SDDL schema, it:

1. **Parses** each sample file according to the schema, splitting it into typed field streams.
2. **Explores** a search space of compression strategies for each field — delta coding, context models, run-length encoding, various entropy coders, and combinations thereof.
3. **Evaluates** each candidate plan against the training data, looking for the best ratio within time and resource constraints.
4. **Produces** a trained compressor artifact (`.compressor` file) that encodes the winning strategy for every field.

The search is bounded by `--max-time-secs` (default 1800 seconds / 30 minutes). Longer training means more exploration and potentially better ratios, with diminishing returns.

**Key training parameters and what they do:**

| Parameter | What it controls |
|---|---|
| `--max-time-secs N` | Training time budget. More time = more exploration = potentially better ratios. |
| `--threads N` | Parallelism during training (speeds up the search). |
| `--trainer greedy\|full-split\|bottom-up` | Strategy for exploring the compression plan search space. `greedy` is the default and generally robust. |
| `--no-ace-successors` | Disables ACE (Asymmetric Context Encoding) successor models. These can improve ratios on in-distribution data but may hurt generalization to unseen data. **Enabled by default in this pipeline** for robustness. |
| `--use-all-samples` | Uses all sample files in the directory (not just a subset). **Enabled by default.** |
| `--no-clustering` | Skips clustering of samples during training. May help when samples are very uniform. |

### 1.3 Train Once, Compress Many

The trained compressor **generalizes**. A model trained on a ~200 MiB subset of a genome applies efficiently to the full multi-GB genome (or to other genomes of the same species). This is because genomic data is structurally homogeneous — chromosome 1 looks structurally similar to chromosome 19. Training is the expensive one-time cost; compression using the trained model is fast and cheap.

### 1.4 The `zli` CLI

`zli` is OpenZL's command-line tool. The key commands this pipeline uses:

```bash
# Train: learn compression strategies from sample data + SDDL schema
zli train <sample_dir> \
  --profile sddl --profile-arg <schema.sddl> \
  --output <model.compressor> \
  --threads 16 --max-time-secs 1800 \
  --use-all-samples --no-ace-successors --force

# Compress: apply a trained model to data
zli compress <file.bin> \
  --compressor <model.compressor> \
  --output <file.bin.zl> --force

# Decompress: lossless round-trip
zli decompress <file.bin.zl> \
  --output <file.bin> --force

# Inspect: examine what a trained model learned (JSON output)
zli inspect <model.compressor>

# List profiles: see available built-in compression profiles
zli list-profiles

# Benchmark: run OpenZL's built-in benchmarks on sample data
zli benchmark <sample_dir>
```

---

## 2. The SDDL Schema (Heart of the Pipeline)

### 2.1 What SDDL Is

SDDL (Simple Data Description Language) is OpenZL's schema format for describing structured binary data layouts. It tells OpenZL how to decompose a binary file into typed fields so it can compress each field optimally. Without an SDDL schema, OpenZL is just another generic compressor. With a schema, it becomes a **domain-specific compression engine**.

### 2.2 The FAV4 Schema (`fasta_packed.sddl`)

This is the schema we use for FASTA data. It describes the **FAV4** (FASTA Version 4, 4-bit packed) binary format:

```
U32 = UInt32LE

magic       : Byte[4]                    # Always "FAV4" — file identification
num_records : U32                        # How many FASTA records are in this chunk

hdr_offsets : U32[num_records + 1]       # Prefix-sum array into the headers blob
seq_offsets : U32[num_records + 1]       # Prefix-sum array into the sequences blob
seq_lengths : U32[num_records]           # Original base counts (before packing)

hdr_total   : U32                        # Unpadded header payload size
seq_total   : U32                        # Unpadded sequence payload size
hdr_pad     : U32                        # Padding for 4-byte alignment
seq_pad     : U32                        # Padding for 4-byte alignment

headers     : Byte[hdr_total + hdr_pad]  # Concatenated header text (without '>')
sequences   : Byte[seq_total + seq_pad]  # 4-bit packed bases (2 bases per byte)

: Byte[_rem]                             # Permissive trailer — absorbs any trailing bytes
```

### 2.3 Why This Schema Design Matters

Every design choice in the schema is motivated by how it helps OpenZL compress better:

| Field | Type | What OpenZL Learns |
|---|---|---|
| `hdr_offsets` / `seq_offsets` | `U32[N+1]` (prefix sums) | These are **monotonically increasing integers**. OpenZL discovers this and applies **delta coding** — storing the differences between consecutive values instead of the values themselves. Deltas are small, uniform numbers that compress extremely well. |
| `headers` | `Byte[...]` | ASCII text with limited vocabulary (species names, chromosome identifiers). OpenZL can use **dictionary-style** or **context-based** models tuned to this repeating text. |
| `sequences` | `Byte[...]` (4-bit packed) | A **constrained 5-symbol alphabet** (A=0, C=1, G=2, T=3, N=4) packed two per byte. OpenZL trains specialized models on this highly structured, low-entropy data. |
| `seq_lengths` | `U32[N]` | Chromosome lengths. Often similar across records — good for delta or run-length approaches. |
| `magic` | `Byte[4]` | Constant value — OpenZL can predict it perfectly (zero bits). |
| `: Byte[_rem]` | Remainder | Permissive trailer that absorbs any bytes after the expected payload. Prevents schema mismatches from crashing the pipeline. |

**The core insight:** By separating the data into typed fields with distinct statistical properties, we give OpenZL the information it needs to beat any generic compressor. A generic tool sees a flat byte stream and uses one model for everything. OpenZL sees integers, text, and a constrained alphabet, and applies the right tool for each.

---

## 3. The Preprocessing Pipeline (FASTA → Structured Binary)

### 3.1 Why Preprocessing Is Necessary

Raw FASTA is plain text:

```
>NC_000067.7 Mus musculus strain C57BL/6J chromosome 1
CTAACCCTAACCCTAACCCTAACCCTAACCCTAACCCTAACCC
TAACCCTAACCCTAACCCTAACCCTAACCCTAACCCTAACCCC
```

This is terrible input for OpenZL because:
- **Headers and sequences are interleaved** — they have completely different statistical properties but are mixed together.
- **Line breaks are arbitrary** — they carry no information (FASTA wraps sequences at ~70–80 chars per line purely for readability).
- **Bases are ASCII** — each base takes 8 bits, but there are only 5 possible values (A, C, G, T, N), which need only ~2.3 bits each.

The preprocessor solves all three problems.

### 3.2 The C++ Preprocessor (`genomic_preprocessor.cpp`)

An 886-line, zero-dependency C++17 program at `nyx/tools/genomic_preprocessor.cpp`. It converts raw text FASTA into the structured FAV4 binary format that matches our SDDL schema.

**What the conversion does:**

1. **Columnar separation** — Headers are stripped of their `>` prefix and concatenated into a single contiguous blob. Sequences are concatenated into a separate blob. Line breaks are discarded. Offset arrays (`hdr_offsets`, `seq_offsets`) record where each record begins and ends within each blob.

2. **4-bit base packing** — Each DNA base is mapped to a 4-bit value and two bases are packed per byte (high nibble first):

   ```
   Base → Nibble:  A/a = 0   C/c = 1   G/g = 2   T/t = 3   N/n = 4
   Byte layout:    [high nibble: base_i] [low nibble: base_i+1]
   Odd-length sequences: last base in high nibble, low nibble = 0x0
   ```

   This alone halves the sequence payload size before OpenZL even begins.

3. **Schema alignment** — The output binary matches the FAV4 SDDL schema exactly, including 4-byte padding, so `zli train` can parse it directly.

**Performance features:**

| Feature | How |
|---|---|
| Memory-mapped I/O | `mmap` with `MADV_SEQUENTIAL` — zero-copy scanning of multi-GB genomes |
| Parallel chunking | File split at record boundaries; each chunk processed by a separate thread |
| Lock-free work stealing | `std::atomic<size_t>` fetch-add for thread coordination |
| Max chunk sizes | 450 MiB (FASTA/FASTQ), 300 MiB (VCF) — keeps chunks within OpenZL's memory constraints |

**Supported formats:**

| Format | Magic | Binary Layout |
|---|---|---|
| FASTA Packed | `FAV4` | 4-bit packed bases, columnar (primary format) |
| FASTA | `FAV3` | Unpacked ASCII sequences, columnar |
| FASTQ | `FQV3` | Headers + sequences + quality scores |
| FASTQ V4 | `FQV4` | Header deduplication + 4-bit packed bases + qualities |
| VCF | `VCF3` | Columnar decomposition of variant call fields |

**CLI usage:**

```bash
genomic_preprocessor <input_file> <output_dir> <num_threads> [type]
# Example:
genomic_preprocessor genome.fna chunks/ 16 fasta_packed
# Produces: chunks/chunk_00000.fasta_packed.bin, chunks/chunk_00001.fasta_packed.bin, ...
```

### 3.3 Record-Safe Training Samples

Before training, we extract a representative subset of the genome. The sampler (`nyx/scripts/make_train_sample.py`) copies **whole FASTA records** — it never cuts a chromosome mid-sequence. Records that would overshoot the target size are skipped, not truncated. This produces a valid FASTA file that the preprocessor can handle without edge cases.

---

## 4. The Full Compression Pipeline

```
Raw FASTA text (.fna file)
    │
    ▼
[Record-safe sampling] ── Extract ~200 MiB of whole FASTA records
    │
    ▼
[Preprocessing] ── genomic_preprocessor converts text → FAV4 binary chunks
    │                 (columnar layout, 4-bit packed bases, SDDL-matching)
    ▼
[SDDL Schema] ── fasta_packed.sddl describes the binary layout to OpenZL
    │
    ▼
[OpenZL Training] ── zli train reads chunks + schema, learns per-field
    │                   compression strategies (bounded by time budget)
    │                   Produces: .compressor artifact
    ▼
[Full Preprocessing] ── genomic_preprocessor converts the entire file
    │                     into binary chunks (parallel, multi-threaded)
    ▼
[OpenZL Compression] ── zli compress applies the trained model to each chunk
    │                     (parallel, fast — just follows the learned plan)
    ▼
[Archive Bundling] ── compressed chunks + model + manifest → .nyx archive
```

**Key distinction — Training vs. Compression:**

- **Training** is **offline and expensive**. It explores a search space of compression strategies. Run it once on a representative sample.
- **Compression** is **online and cheap**. It applies the pre-learned plan. Run it on any amount of new data.

This is the "**train once, compress many**" paradigm.

---

## 5. Repository Structure

```
compression/                              # Git repository root
├── docs/                                 # Documentation
│   ├── PROJECT_CONTEXT.md                # Authoritative project reference
│   ├── DEVELOPERS_GUIDE.md               # THIS FILE — OpenZL integration deep-dive
│   ├── SETUP_AND_PIPELINE_WALKTHROUGH.md # Setup from zero + conceptual pipeline walkthrough
│   ├── PROJECT_HISTORY.md                # Structured changelog
│   └── CHANGES.md                        # Developer change notes
│
└── nyx/                                  # The active project
    ├── pyproject.toml                    # Python package config (click, tqdm dependencies)
    ├── README.md                         # Install guide and quick reference
    │
    ├── schemas/                          # SDDL schema definitions (consumed by zli train)
    │   └── fasta_packed.sddl             # FAV4 binary format — the heart of the OpenZL integration
    │
    ├── tools/                            # C++ source code
    │   └── genomic_preprocessor.cpp      # 886-line preprocessor: text FASTA → FAV4 binary chunks
    │
    ├── scripts/                          # Build and data-prep scripts
    │   ├── build.sh                      # Clones OpenZL + builds zli + builds preprocessor
    │   ├── get_openzl.sh                 # Standalone OpenZL clone/pin script
    │   └── make_train_sample.py          # Record-safe FASTA training sampler
    │
    └── nyx/                              # Python package (CLI wrapper around OpenZL + preprocessor)
        ├── cli.py                        # Entry point (Click framework)
        ├── commands/                     # CLI commands (compress, decompress, train, benchmark, etc.)
        ├── core/                         # Subprocess wrappers for zli and genomic_preprocessor
        │   ├── openzl.py                 # zli wrapper: train, compress, decompress, inspect, etc.
        │   ├── preprocessor.py           # genomic_preprocessor wrapper
        │   ├── detect.py                 # File type auto-detection (FASTA/FASTQ/VCF)
        │   ├── archive.py                # .nyx tar archive read/write
        │   ├── config.py                 # Schema registry mapping file types → SDDL schemas
        │   └── benchmark.py              # Competitor benchmarks (gzip, pigz, zstd)
        └── utils/paths.py                # Locates zli, preprocessor, and schemas at runtime
```

**Generated at runtime (git-ignored):**

```
nyx/
├── openzl/                        # OpenZL source checkout
│   └── zli                        # Compiled OpenZL CLI binary
├── bin/
│   └── genomic_preprocessor       # Compiled C++ preprocessor
└── .venv/                         # Python virtual environment
```

---

## 6. OpenZL Integration Details

### 6.1 How OpenZL Is Integrated

OpenZL is **cloned from source and built locally**. The pipeline uses only the `zli` CLI binary — no C/C++ API, no FFI, no linking. All calls go through `subprocess`. The C++ code in this repository (`genomic_preprocessor.cpp`) does **not** link against any OpenZL library; it is a separate tool that prepares data for OpenZL.

### 6.2 Version Pinning

OpenZL is pinned to commit `e40fe9f314283047147d573d113fa7d17eabf7ac` for reproducibility. This is defined in `nyx/scripts/build.sh` and can be overridden via the `OPENZL_COMMIT` environment variable.

### 6.3 What `zli` Commands Are Used

| Command | Purpose | When It Runs |
|---------|---------|-------------|
| `zli train <dir> --profile sddl --profile-arg <sddl> ...` | Trains a compressor on preprocessed binary chunks using the SDDL schema | Once per training session |
| `zli compress <file> --compressor <model> --output <file.zl>` | Compresses a binary chunk using a trained model | Once per chunk (parallelized) |
| `zli compress <file> --profile <name> --output <file.zl>` | Compresses using a built-in profile (e.g., `serial`) | For generic non-genomic files |
| `zli decompress <file.zl> --output <file>` | Lossless decompression | On demand |
| `zli inspect <model>` | Dumps the trained model as JSON | Debugging / analysis |
| `zli list-profiles` | Shows available built-in profiles | Discovery |
| `zli benchmark <dir>` | Runs OpenZL's built-in benchmarks | Performance evaluation |

### 6.4 Training Flags Used by Default

| Flag | Default | Why |
|------|---------|-----|
| `--profile sddl` | Always (for genomic) | Uses the SDDL schema to decompose binary into typed fields |
| `--profile-arg <path>` | From schema registry | Points to `fasta_packed.sddl` |
| `--use-all-samples` | `True` | Uses all chunk files in the training directory |
| `--no-ace-successors` | `True` | Improves generalization to unseen data |
| `--max-time-secs` | `1800` (30 min) | Bounds the training optimizer |
| `--threads` | `os.cpu_count()` | Parallelizes the training search |
| `--force` | `True` | Overwrites previous artifacts |

---

## 7. Building and Running

### 7.1 Prerequisites

| Tool | Minimum Version | Why |
|---|---|---|
| C++ Compiler (g++ or clang++) | GCC 9+ / Clang 13+ | Builds OpenZL and the preprocessor (C++17 required) |
| GNU Make | 3.81+ | Builds OpenZL from source |
| Git | 2.0+ | Clones the OpenZL repository |
| Python 3 | 3.13+ | Runs the CLI wrapper |

**Platform support:** Linux (primary) and macOS are both fully supported. The preprocessor uses POSIX `mmap`, which works on both.

### 7.2 Build

```bash
# Install the Python CLI wrapper (also installs click, tqdm)
python3 -m venv nyx/.venv && source nyx/.venv/bin/activate
pip install -e ./nyx

# Build OpenZL (clones, compiles zli) and the preprocessor
nyx build
```

This produces `nyx/openzl/zli` and `nyx/bin/genomic_preprocessor`.

### 7.3 Run via CLI

The Python CLI automates the full pipeline:

```bash
# Compress a FASTA file (auto: preprocess → train → compress → bundle .nyx archive)
nyx compress genome.fasta

# With custom OpenZL training parameters
nyx compress genome.fasta --max-time-secs 3600 --threads 16 --target-train-mib 300

# Compare against gzip, pigz, zstd
nyx compress genome.fasta --benchmark

# Decompress
nyx decompress genome.fasta.nyx -o output/
```

### 7.4 Run the Pipeline Manually (Direct `zli` Usage)

You can bypass the CLI and call the tools directly. This is useful for understanding each step or for custom experiments:

```bash
# 1) Create a ~200 MiB training sample
python3 nyx/scripts/make_train_sample.py \
  --in genome.fna --out train_200MiB.fasta --target-mib 200

# 2) Preprocess into FAV4 binary chunks
nyx/bin/genomic_preprocessor train_200MiB.fasta chunks_train/ 1 fasta_packed

# 3) Train OpenZL on the preprocessed data
nyx/openzl/zli train chunks_train/ \
  --profile sddl --profile-arg nyx/schemas/fasta_packed.sddl \
  --output model.compressor \
  --force --threads 16 --use-all-samples --max-time-secs 1800 --no-ace-successors

# 4) Preprocess the full genome
nyx/bin/genomic_preprocessor genome.fna chunks_full/ 16 fasta_packed

# 5) Compress all chunks using the trained model
for bin in chunks_full/*.fasta_packed.bin; do
  nyx/openzl/zli compress "$bin" --compressor model.compressor --output "$bin.zl" --force
done

# 6) Validate round-trip (lossless check)
for bin in chunks_full/*.fasta_packed.bin; do
  nyx/openzl/zli decompress "$bin.zl" --output "$bin.dec" --force
  cmp -s "$bin" "$bin.dec" && echo "OK: $bin" || echo "MISMATCH: $bin"
  rm -f "$bin.dec"
done
```

---

## 8. Experiments to Try

### Experiment 1: Training Budget vs. Compression Quality

The `--max-time-secs` flag controls how long OpenZL's optimizer searches for better compression strategies. More time = more strategies explored = potentially better ratios, with diminishing returns.

```bash
# 5 minutes — quick and reasonable
nyx compress genome.fasta --max-time-secs 300

# 30 minutes (default) — solid results
nyx compress genome.fasta --max-time-secs 1800

# 2 hours — squeezing out marginal gains
nyx compress genome.fasta --max-time-secs 7200
```

Compare the ratios printed by each run. The first few minutes of training capture most of the gains.

### Experiment 2: Training Sample Size

How much training data does OpenZL need to learn effective strategies?

```bash
# Small sample — faster, but the model sees less variety
nyx compress genome.fasta --target-train-mib 50

# Default — good balance
nyx compress genome.fasta --target-train-mib 200

# Large sample — more representative, but slower training
nyx compress genome.fasta --target-train-mib 500
```

For structurally homogeneous data like a reference genome, even 50 MiB may be sufficient. For highly variable data, a larger sample helps.

### Experiment 3: ACE Successors (Generalization vs. Fit)

ACE (Asymmetric Context Encoding) successor models can improve compression on data similar to the training set, but may hurt on unseen data:

```bash
# With ACE successors — potentially better ratio on training-like data
nyx train chunks/ -o model_ace.compressor -p sddl --profile-arg nyx/schemas/fasta_packed.sddl \
  --threads 16 --max-time-secs 1800 --use-all-samples

# Without ACE successors (default in our pipeline) — more robust generalization
nyx train chunks/ -o model_noace.compressor -p sddl --profile-arg nyx/schemas/fasta_packed.sddl \
  --threads 16 --max-time-secs 1800 --use-all-samples --no-ace-successors
```

Then compress the same data with each model and compare.

### Experiment 4: Schema-Aware vs. Generic Compression

To see **how much the SDDL schema and preprocessing contribute**, compare against OpenZL's generic compression:

```bash
# Schema-aware (preprocessed FAV4 + SDDL) — best ratio
nyx compress genome.fasta --mode train_plain

# Generic OpenZL (no preprocessing, no schema) — faster, worse ratio
nyx compress genome.fasta --mode default

# Inline training (OpenZL trains on the fly, no separate training step)
nyx compress genome.fasta --mode inline_train
```

The `train_plain` mode should significantly outperform `default` because OpenZL has structural knowledge of the data.

### Experiment 5: Packed vs. Unpacked Sequences

The preprocessor supports both unpacked FASTA (`fasta` mode, FAV3 format) and 4-bit packed FASTA (`fasta_packed` mode, FAV4 format). To see how much the 4-bit packing contributes:

```bash
# Preprocess with unpacked format (1 byte per base)
nyx/bin/genomic_preprocessor genome.fna chunks_unpacked/ 16 fasta

# Preprocess with packed format (2 bases per byte)
nyx/bin/genomic_preprocessor genome.fna chunks_packed/ 16 fasta_packed

# Train and compress each, then compare total compressed sizes
```

The packed format should compress better because OpenZL starts with a more information-dense representation.

### Experiment 6: Inspect a Trained Model

After training, you can examine what OpenZL learned:

```bash
nyx inspect model.compressor
```

This outputs JSON describing the compression strategies chosen for each field. Look for delta coding on offset arrays, context models on sequence data, etc.

---

## Appendix A: FAV4 Binary Format Reference

```
Offset  Field                    Type               Size (bytes)
─────────────────────────────────────────────────────────────────
0       magic                    Byte[4]            4          ("FAV4")
4       num_records              U32                4
8       hdr_offsets              U32[N+1]           (N+1)×4
...     seq_offsets              U32[N+1]           (N+1)×4
...     seq_lengths              U32[N]             N×4
...     hdr_total                U32                4
...     seq_total                U32                4
...     hdr_pad                  U32                4
...     seq_pad                  U32                4
...     headers                  Byte[hdr_total+hdr_pad]
...     sequences                Byte[seq_total+seq_pad]
```

### 4-Bit Base Encoding

```
Base:   A=0  C=1  G=2  T=3  N=4   (case-insensitive)
Layout: [high nibble | low nibble] = [base_i | base_i+1]
Odd-length sequences: last base in high nibble, low nibble = 0
```

---

## Appendix B: Glossary

| Term | Definition |
|---|---|
| **OpenZL** | Meta's open-source compression framework. Unlike generic compressors, OpenZL uses schemas to decompose structured data into typed fields and learn specialized compression strategies per field. |
| **`zli`** | OpenZL's command-line tool — trains compressors, compresses, decompresses, benchmarks, inspects models. |
| **SDDL** | Simple Data Description Language — OpenZL's schema format for describing binary data layouts. The schema tells OpenZL how to parse binary files into typed fields. |
| **Profile / Compressor** | A trained compression model (`.compressor` file). Encodes the strategies OpenZL learned during training for each field in the schema. |
| **Training** | OpenZL's offline optimization process: given data + schema, explore compression strategy space, produce a model. |
| **FASTA** | Text format for nucleotide sequences. Records start with `>header`, followed by sequence lines. |
| **FAV4** | "FASTA Version 4" — the project's binary format with 4-bit packed bases and columnar header/sequence layout. Described by `fasta_packed.sddl`. |
| **4-bit packing** | Encoding two DNA bases per byte: A=0, C=1, G=2, T=3, N=4 in high/low nibbles. Halves the raw sequence size. |
| **Chunk** | A portion of the input file split at record boundaries. Each chunk is an independent FAV4 binary file that can be compressed separately. |
| **Record-safe sampling** | Extracting whole FASTA records (never splitting mid-sequence) to create a valid training subset. |
| **ACE** | Asymmetric Context Encoding — an advanced OpenZL model component. Disabled by default for better generalization. |
| **Delta coding** | Storing differences between consecutive values instead of the values themselves. Very effective on monotonically increasing arrays like offset tables. |
| **Compression ratio** | `uncompressed_size / compressed_size`. Higher is better. |
| **`genomic_preprocessor`** | The C++ binary that converts text FASTA/FASTQ/VCF into structured binary chunks matching SDDL schemas. |
| **Nyx** | The Python CLI that automates the pipeline: detect type → preprocess → train → compress → bundle. Convenience wrapper, not the core technology. |
