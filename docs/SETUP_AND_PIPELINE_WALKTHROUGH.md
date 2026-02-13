# Setup & End-to-End Pipeline Walkthrough

> This document records everything we installed, every command we ran to set up the environment, and provides a detailed conceptual walkthrough of the OpenZL compression pipeline — what each step does to the data, why it exists, and how the pieces fit together. It is written so that you can reproduce the exact same environment from scratch and understand what OpenZL is doing at every stage.

---

## Part 1: Environment Setup (What We Installed and Why)

### 1.1 Xcode Command Line Tools (macOS)

```bash
xcode-select --install
```

This installs the core developer toolchain:

| Tool | What it is |
|---|---|
| `clang++` / `g++` | The C++ compiler. Compiles OpenZL (hundreds of source files → the `zli` binary) and our custom preprocessor (one file → `genomic_preprocessor`). |
| `make` | Build automation tool. OpenZL's Makefile describes which source files depend on which. `make -j8` compiles 8 files in parallel. |
| `git` | Version control. Clones the OpenZL source from GitHub. |

### 1.2 Python 3.8+

The pipeline uses a Python CLI (`nyx`) to orchestrate the build, preprocessing, and compression steps:

| Platform | How to get it |
|---|---|
| **macOS** | Pre-installed, or `pyenv install 3.8.20` |
| **Ubuntu/Debian** | `sudo apt install python3 python3-pip python3-venv` |

### 1.3 Optional Packages (Recommended)

```bash
# macOS
brew install pigz zstd

# Ubuntu / Debian
sudo apt install pigz zstd
```

| Package | Required? | Why you want it | What happens without it |
|---|---|---|---|
| **pigz** | No | Parallel gzip. Used as a baseline when benchmarking: "How does OpenZL compare to the best gzip can do?" | Benchmark comparison skips pigz. |
| **zstd** | No | Modern compressor by Meta. Another baseline for comparison. | Benchmark comparison skips zstd. |

### 1.4 Verify

```bash
g++ --version            # Apple clang (macOS) or GCC (Linux)
make --version           # GNU Make
python3 --version        # 3.8+
git --version            # 2.0+
```

---

## Part 2: Project Setup (What We Ran and Why)

From the repository root (`compression/`), we ran three commands:

### Step 1: Install the Python CLI

```bash
python3 -m venv nyx/.venv
source nyx/.venv/bin/activate
pip install -e ./nyx
```

Creates a Python virtual environment and installs the `nyx` CLI tool, which automates the pipeline. The `nyx` command is now available in your terminal.

### Step 2: Build OpenZL and the Preprocessor

```bash
nyx build
```

This runs `nyx/scripts/build.sh`, which does three things:

1. **Clones OpenZL** — Downloads Meta's OpenZL repository from `https://github.com/facebook/openzl` and checks out a specific pinned commit (`e40fe9f3`) for reproducibility.

2. **Builds OpenZL** — Runs `make -j` inside the OpenZL checkout. This compiles hundreds of C++ source files and produces `nyx/openzl/zli`, the OpenZL command-line tool that does the actual training, compression, and decompression.

3. **Builds the preprocessor** — Compiles our one-file C++ preprocessor:
   ```
   g++ -O3 -std=c++17 -pthread -o bin/genomic_preprocessor tools/genomic_preprocessor.cpp
   ```
   This tool converts raw FASTA text into the structured binary format that OpenZL needs.

**Time:** ~2-5 minutes.

### Step 3: Verify

```bash
nyx list-profiles       # Should print available OpenZL compression profiles
```

### State After Setup

```
compression/
├── nyx/
│   ├── openzl/                       # OpenZL source + compiled zli binary
│   ├── bin/genomic_preprocessor      # Compiled preprocessor binary
│   ├── .venv/                        # Python virtual environment
│   ├── schemas/fasta_packed.sddl     # SDDL schema (checked in)
│   └── tools/genomic_preprocessor.cpp # Preprocessor source (checked in)
└── docs/                             # Documentation (checked in)
```

---

## Part 3: The Compression Pipeline (What Happens to the Data)

When you run `nyx compress genome.fasta`, the pipeline executes five phases. Each phase transforms the data in a specific way. Here is what happens at each stage and, more importantly, **why**.

### Phase 1: Create a Training Sample

**The problem:** The full mouse genome is 2.76 GB. OpenZL's training optimizer would take a very long time on all of it, and it doesn't need to — genomic data is structurally homogeneous. Chromosome 1 looks statistically similar to chromosome 19.

**What happens:** The sampler (`make_train_sample.py`) copies whole FASTA records from the input until reaching ~200 MiB. "Whole records" is critical — it never cuts a chromosome mid-sequence. Records that would overshoot the target are skipped, not truncated.

**Produces:** A valid, smaller FASTA file (~200 MiB) that's representative of the full genome.

### Phase 2: Preprocess into Structured Binary (FAV4)

**The problem:** Raw FASTA is plain text:

```
>NC_000067.7 Mus musculus strain C57BL/6J chromosome 1
CTAACCCTAACCCTAACCCTAACCCTAACCCTAACCCTAACCC
TAACCCTAACCCTAACCCTAACCCTAACCCTAACCCTAACCCC
```

This is terrible input for OpenZL because headers and sequences are interleaved (completely different statistical properties mixed together), line breaks carry no information, and each base wastes 8 bits when only ~2.3 bits are needed.

**What happens:** The `genomic_preprocessor` converts this text into a structured binary format called **FAV4** (FASTA Version 4, 4-bit packed). Three transformations:

1. **Columnar separation** — Headers go into one contiguous blob, sequences into another. Offset arrays record where each record starts and ends. Now OpenZL can see them as separate typed fields with different statistical properties.

2. **4-bit base packing** — Each base (`A`, `C`, `G`, `T`, `N`) → 4-bit value (0–4). Two bases are packed per byte. This halves the sequence payload before OpenZL even starts.

3. **Schema alignment** — The binary output exactly matches `schemas/fasta_packed.sddl`. This SDDL schema tells OpenZL what each field is: "these are monotonically increasing integers (offset arrays), that's text (headers), this is a constrained 5-symbol alphabet (packed sequences)."

**Produces:** Binary chunk files (`.fasta_packed.bin`) matching the SDDL schema.

**Why this matters:** By decomposing the flat text stream into typed fields, we give OpenZL the information it needs to apply the right compression strategy to each field. Without this step, OpenZL would be no better than gzip.

### Phase 3: Train the Compressor

**The problem:** You want a compression model tuned specifically to the statistical properties of FAV4-formatted genomic data — not a generic one-size-fits-all model.

**What happens:** OpenZL's `zli train` command reads the binary chunks and the SDDL schema. It:

1. **Decomposes** each chunk into typed fields according to the schema — offset arrays, sequence data, header text, length arrays, etc.
2. **Explores** compression strategies for each field independently. For the offset arrays, it might try delta coding (storing differences instead of absolute values — very effective on monotonically increasing sequences). For the packed bases, it might try various context models tuned to the 5-symbol alphabet. For headers, dictionary-based approaches.
3. **Optimizes** under a time budget (default 30 minutes). The optimizer is essentially searching a space of compression plans, evaluating each against the training data.
4. **Produces** a `.compressor` artifact that encodes the winning strategy for every field.

**This is the key insight of OpenZL:** Training is an offline, one-time cost. The model it produces is reusable — you can apply it to any amount of new data of the same format.

**Produces:** A trained compression model (`.compressor` file).

### Phase 4: Compress the Full Genome

**The problem:** Now you want to compress the entire 2.76 GB genome — including the ~2.56 GB that the training phase never saw. This tests whether the model generalizes beyond the training sample.

**What happens:**

1. **Preprocess the full genome** — The preprocessor converts the entire `.fna` file into FAV4 binary chunks, splitting at chromosome boundaries across multiple threads. Each chunk is independently processable.

2. **Compress each chunk** — `zli compress` applies the trained model to each chunk. This is **fast** — it just follows the compression plan learned in Phase 3. No new learning happens. Chunks are compressed in parallel.

**This is the "train once, compress many" paradigm.** A model learned from 200 MiB works on the full genome because the data has the same structure throughout.

**Produces:** Compressed chunks (`.zl` files).

### Phase 5: Bundle and Validate

**What happens:**

1. **Archive creation** — Compressed chunks, the trained model, and a manifest are bundled into a `.nyx` tar archive. The archive is self-contained — it includes everything needed to decompress.

2. **Ratio reporting** — The pipeline prints the original size, compressed size, and compression ratio.

3. **Optional benchmarking** — If requested (`--benchmark`), the original file is also compressed with gzip, pigz, and zstd, and a comparison table is printed showing how OpenZL stacks up against general-purpose compressors.

**The final output looks something like:**

```
Original:    2762.3 MiB
Compressed:    XXX.X MiB
Ratio:        X.XXx

┌──────────────┬────────────┬───────┬─────────┬──────────┐
│ Compressor   │ Compressed │ Ratio │ Savings │ Speed    │
├──────────────┼────────────┼───────┼─────────┼──────────┤
│ nyx (OpenZL) │   XXX MiB  │ X.XXx │  XX.X%  │ XX MB/s  │
│ gzip -1      │   XXX MiB  │ X.XXx │  XX.X%  │ XX MB/s  │
│ pigz -9      │   XXX MiB  │ X.XXx │  XX.X%  │ XX MB/s  │
│ zstd -9      │   XXX MiB  │ X.XXx │  XX.X%  │ XX MB/s  │
└──────────────┴────────────┴───────┴─────────┴──────────┘
```

---

## Part 4: Running the Pipeline Manually (Direct `zli` Usage)

The Python CLI automates everything, but you can also drive the tools directly. This is useful for understanding each step in isolation, for custom experiments, or for integrating into other workflows.

```bash
# 1) Create a ~200 MiB training sample (record-safe)
python3 nyx/scripts/make_train_sample.py \
  --in genome.fna --out train_200MiB.fasta --target-mib 200

# 2) Preprocess into FAV4 binary chunks
nyx/bin/genomic_preprocessor train_200MiB.fasta chunks_train/ 1 fasta_packed

# 3) Train OpenZL (the expensive offline step)
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

# 6) Validate round-trip (byte-for-byte lossless check)
for bin in chunks_full/*.fasta_packed.bin; do
  nyx/openzl/zli decompress "$bin.zl" --output "$bin.dec" --force
  cmp -s "$bin" "$bin.dec" && echo "OK: $bin" || echo "MISMATCH: $bin"
  rm -f "$bin.dec"
done
```

---

## Part 5: Experiments (Varying OpenZL Parameters)

### Training Budget

More time lets OpenZL explore more compression strategies. Diminishing returns apply.

```bash
nyx compress genome.fasta --max-time-secs 300   # 5 min: quick, decent
nyx compress genome.fasta --max-time-secs 1800  # 30 min: solid (default)
nyx compress genome.fasta --max-time-secs 7200  # 2 hours: marginal gains
```

### Training Sample Size

How much data does OpenZL need to learn effective strategies?

```bash
nyx compress genome.fasta --target-train-mib 50   # Less variety, faster
nyx compress genome.fasta --target-train-mib 200  # Good balance (default)
nyx compress genome.fasta --target-train-mib 500  # More representative, slower
```

### ACE Successors

ACE models improve in-distribution compression but may hurt on unseen data. Our pipeline disables them by default for robustness. To experiment, use the `nyx train` command directly (it passes flags straight to `zli train`).

### Schema-Aware vs. Generic

```bash
nyx compress genome.fasta --mode train_plain    # Preprocessed + SDDL (best)
nyx compress genome.fasta --mode default        # Generic OpenZL (no schema)
nyx compress genome.fasta --mode inline_train   # Inline training, no schema
```

The gap between `train_plain` and `default` shows how much the preprocessing + SDDL schema contribute.

---

## Part 6: Cleanup and Reset

### Remove temporary files

The CLI cleans up automatically. If a run was interrupted:

```bash
rm -rf /tmp/nyx_*
```

### Rebuild OpenZL and the preprocessor

```bash
rm -rf nyx/openzl/ nyx/bin/
nyx build
```

### Full reset from scratch

```bash
rm -rf nyx/.venv/ nyx/openzl/ nyx/bin/
python3 -m venv nyx/.venv && source nyx/.venv/bin/activate
pip install -e ./nyx && nyx build
```

---

## Quick Reference: Complete Setup from Zero

```bash
# === System dependencies (one-time) ===
# macOS:
xcode-select --install
brew install pigz zstd

# Ubuntu/Debian:
sudo apt install -y build-essential git python3 python3-pip python3-venv pigz zstd

# === Project setup ===
python3 -m venv nyx/.venv             # Create virtual environment
source nyx/.venv/bin/activate          # Activate it
pip install -e ./nyx                   # Install the CLI
nyx build                              # Build OpenZL + preprocessor (~2-5 min)

# === Run ===
nyx compress genome.fasta --benchmark  # Compress + compare against baselines
```
