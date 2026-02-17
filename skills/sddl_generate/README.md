# SDDL + Preprocess Generator

A Claude Code skill and standalone Python toolkit that analyzes any input file and
automatically generates two outputs:

1. **`custom.sddl`** — An optimal OpenZL SDDL schema for the detected format
2. **`preprocess.py`** — A streaming, lossless Python preprocessor that converts
   the raw file into the binary layout described by the schema

## Quick Start

### As a Claude Code Skill

When the skill is installed, simply provide a file path:

```
Generate for: /data/genome.fasta
```

Claude will immediately analyze the file and generate both outputs.

### As a Standalone CLI

```bash
cd skills/sddl_generate
python3 src/cli.py /data/genome.fasta --output-dir ./out
```

### Using the Generated Files with Nyx

```bash
# 1. Preprocess (converts text -> binary chunks)
python3 ./out/preprocess.py /data/genome.fasta ./chunks/

# 2. Compress with nyx using the generated schema
nyx compress /data/genome.fasta --mode train_custom --sddl ./out/custom.sddl
```

## Supported Formats

| Format | Detection | Packing | Layout |
|--------|-----------|---------|--------|
| FASTA | `>` first line | 4-bit base (2/byte) | SoA columnar (FAV4) |
| FASTQ | `@` + `+` pattern | 4-bit base + header dedup | SoA columnar (FQV5) |
| VCF | `##fileformat=VCF` | Dictionary-encoded CHROM/FILTER | Columnar (VCF4) |
| TSV | Tab-separated | Per-column typing | Columnar (TSV1) |
| CSV | Comma-separated | Per-column typing | Columnar (CSV1) |
| Generic | Fallback | Raw bytes | Chunked blob (GEN1) |

## Architecture

```
skills/sddl_generate/
├── README.md                          # This file
├── skill.md                           # Skill metadata
├── prompts/
│   ├── system.md                      # Distilled SDDL best practices + exemplar patterns
│   └── run.md                         # Runtime behavior specification
├── src/
│   ├── cli.py                         # Main entry point
│   ├── analyzer.py                    # File format detection + sampling
│   ├── repo_exemplar.py              # Patterns extracted from nyx FASTA exemplar
│   ├── sddl_generator.py            # Generates SDDL schema files
│   ├── preprocess_generator.py       # Generates preprocessing scripts
│   └── strategies/
│       ├── fasta_from_exemplar.py    # FASTA-specific patterns and heuristics
│       └── generic_text.py           # Generic fallback strategy
└── tests/
    ├── test_fasta_small.py           # Tests using tiny FASTA fixture
    └── fixtures/
        └── tiny.fa                    # 4-record test FASTA file
```

## Design Principles

1. **Never modify existing files** — Always writes to new files only
2. **Version-safe output** — Increments filenames if collisions exist
3. **Streaming** — Preprocessors handle GB-scale files line-by-line
4. **Lossless** — Binary format preserves enough metadata for exact reconstruction
5. **Instant-parse maximized** — SDDL schemas use explicit sizes, not delimiters
6. **Exemplar-derived** — Patterns borrowed from our proven FASTA implementation

## Key Heuristics

- **SoA over AoS**: Group all values of the same field together (columnar)
- **Instant-parse**: Put counts in header, use offset arrays, avoid `Bytes until`
- **Symbol packing**: 4-bit for DNA (5 symbols), 2-bit if no N bases
- **Entropy reduction**: Uppercase, strip whitespace/delimiters, dedup headers
- **Explicit sizes**: Store totals + padding, never scan for sizes

## Running Tests

```bash
cd skills/sddl_generate
python3 -m pytest tests/ -v

# Or without pytest:
python3 tests/test_fasta_small.py
```
