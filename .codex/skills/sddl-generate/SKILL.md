---
name: sddl-generate
description: >
  Generate SDDL schema + preprocessing script for any input file. When the user
  provides a file path (e.g. "Generate for: /data/foo.fasta"), immediately analyze
  the file, decide a canonical binary layout, and write two NEW files: custom.sddl
  and preprocess.py. Never modify existing files.
---

# SDDL + Preprocess Generator

You are an expert SDDL schema designer and binary-format engineer.

## How This Skill Works (Two-Stage Pipeline)

This skill has two stages. Both happen in a SINGLE invocation — the user triggers
the skill and provides a file path in the same message (or the next one).

**Stage 1 — LEARN:** Read the OpenZL SDDL documentation and study our existing
FASTA exemplar (schema + preprocessor) so you deeply understand how to design
SDDL schemas and matching preprocessors.

**Stage 2 — GENERATE:** Analyze the user's input file, reason about the optimal
binary layout, and write two NEW files: `custom.sddl` + `preprocess.py`.

The key insight: you are NOT filling templates. You are REASONING from first
principles using the SDDL spec + exemplar patterns to design a novel, optimal
layout for whatever file format the user gives you.

---

## STAGE 1: LEARN (Do this FIRST, before any generation)

### Step 1A: Read the OpenZL SDDL Documentation

Browse and study these official pages. If web access is unavailable, rely on
the distilled reference in Section "SDDL LANGUAGE REFERENCE" below.

Priority order (most important first):
1. `https://openzl.org/sddl/sddl-for-llm/` — Complete LLM-optimized spec
2. `https://openzl.org/sddl/best-practices/` — Design guidelines
3. `https://openzl.org/sddl/instant-parse/` — Critical performance concept
4. `https://openzl.org/sddl/reference/` — Quick syntax reference
5. `https://openzl.org/sddl/arrays-collections/` — SoA patterns
6. `https://openzl.org/sddl/alignment-padding/` — Layout control
7. `https://openzl.org/sddl/core-concepts/` — Fundamentals

If you can fetch even just the first 2-3, you'll have enough context.

### Step 1B: Read the Existing FASTA Exemplar Files

Read these files from the repository — they are your working examples of a
well-designed SDDL schema and its matching preprocessor:

1. **`nyx/schemas/fasta_packed.sddl`** — Our production FASTA schema (FAV4 format).
   Study the layout: magic, num_records, offset arrays, per-record metadata,
   payload totals, padding, data blobs, trailing `Byte[_rem]`.

2. **`nyx/tools/genomic_preprocessor.cpp`** — The C++ preprocessor. Focus on
   `process_fasta_packed_chunk()` (around line 434). Study how it:
   - Parses text FASTA records (splits at `>`)
   - Strips delimiters and newlines
   - Packs bases into 4-bit nibbles
   - Builds offset arrays as running prefix sums
   - Writes the binary output matching the SDDL schema
   - Pads to 4-byte alignment

3. **`nyx/nyx/core/preprocessor.py`** — The Python wrapper. Study the interface:
   subprocess invocation, chunk naming (`chunk_NNNNN.<format>.bin`), error handling.

### Step 1C: Internalize These Patterns

After reading, you should understand:

**From the SDDL docs:**
- Every multi-byte type needs explicit LE/BE suffix
- Instant-parse = all offsets/sizes computable from params/constants alone
- `Bytes until`, `Type[]`, `current_position()` break instant-parse
- `@instant_parse` annotation enforces at compile time
- SoA (`soa Type[N]`) groups same-field values contiguously
- `pad_to N` = exact size, `pad_align N` = round up to multiple

**From the FASTA exemplar:**
- Magic + num_records header pattern
- Prefix-sum offset arrays (N+1 entries) for variable-length fields
- Explicit payload totals + padding sizes stored as separate fields
- SoA layout: all headers together, then all sequences together
- 4-bit base packing with seq_lengths for lossless odd-length restore
- Trailing `Byte[_rem]` for forward-compat
- 4-byte alignment on all payloads

**From the preprocessor:**
- Streaming record-by-record parsing (never loads whole file)
- Split at record boundaries for chunking
- Binary output order must EXACTLY match SDDL field order
- Chunk naming convention: `chunk_NNNNN.<format>.bin`

Print a brief confirmation: "Learned from SDDL docs + FASTA exemplar. Ready to generate."

---

## STAGE 2: GENERATE (After learning, when user provides a file path)

The user will provide a file path, e.g.:
- "Generate for: /data/foo.csv"
- "/data/reads.fastq"
- "Here's the file: /data/bar.vcf into ./out"

### Phase 1: Analyze the Input File

**Step 1A: Detect Format**

Read the first 8192 bytes. Detect format:

| Signal | Format |
|--------|--------|
| First non-empty line starts with `>` | FASTA |
| First non-empty line starts with `@`, 3rd line starts with `+` | FASTQ |
| First line starts with `##fileformat=VCF` | VCF |
| First 4 bytes are known magic (PNG, TIFF, etc.) | That binary format |
| Tab-separated with consistent column count | TSV |
| Comma-separated with consistent column count | CSV |
| JSON `{` or `[` | JSON |
| None of above | Generic |

**Step 1B: Sample Statistics**

Read ~10 MB or ~10,000 records (whichever is smaller). Collect:
- Record count, avg/min/max record size
- Field count per record (if tabular)
- Character frequency distribution
- Alphabet size and symbol distribution
- Whether records are fixed-length or variable-length
- Header/metadata patterns, common prefixes
- Delimiter patterns

**Step 1C: Print Detection Summary**

```
=== SDDL Generator: Analysis ===
File: <path>
Detected format: <format>
File size: <size>
Records sampled: <N>
Record size: avg=<X> min=<Y> max=<Z> bytes
Alphabet: <symbols> (size=<N>)
Fixed-length records: <yes/no>
```

### Phase 2: Reason About Optimal Layout

This is the critical thinking step. DO NOT just pick a template. REASON about
what layout will maximize compression for THIS specific file, using what you
learned from the SDDL docs and the FASTA exemplar.

Consider these heuristics in priority order:

**Heuristic 1: SoA (Columnar) Over AoS**

ALWAYS prefer SoA. Group all values of the same field together. This is why our
FASTA exemplar puts all headers in one blob and all sequences in another — OpenZL
trains per-field compression models, so homogeneous streams compress dramatically
better.

For tabular data (TSV/CSV): one stream per column.
For record-oriented data: one stream per field type.

**Heuristic 2: Instant-Parse Maximization**

Design so ALL field offsets are computable from header values alone:
- Put `num_records` count in the header
- Use offset arrays (prefix-sum, N+1 entries) for variable-length fields
- Store payload lengths explicitly as U32 values
- NEVER use `Bytes until <delimiter>` — use explicit lengths
- NEVER use `Type[]` (auto-sized) — use `Type[N]` with known N

**Heuristic 3: Symbol Packing**

For restricted alphabets, pack symbols to reduce entropy:
- DNA (A,C,G,T + N): 4-bit nibble, 2 bases/byte
- DNA (A,C,G,T only): 2-bit, 4 bases/byte
- Amino acids (20 + special): 5-bit with escape
- Small categorical sets: dictionary-encode to uint8/uint16

Always store enough metadata for lossless reconstruction (e.g., seq_lengths
for odd-length packed sequences).

**Heuristic 4: Entropy Reduction**

Normalize data before layout:
- Case normalization (uppercase all bases)
- Whitespace/newline removal (implicit in layout)
- Delimiter removal (don't store `>`, `@`, `+`, `\t` — implicit)
- Header deduplication (common prefix stored once + suffixes)
- Dictionary encoding for low-cardinality columns

**Heuristic 5: Explicit Lengths and Padding**

For every variable-length payload:
- Store `<field>_total` (unpadded byte count)
- Store `<field>_pad` (padding bytes to reach alignment)
- Data blob = `Byte[<field>_total + <field>_pad]`
- Pad to 4-byte boundaries

**Heuristic 6: Magic + Version Header**

Every layout starts with:
```
magic       : Byte[4]     # 4-char identifier
num_records : U32          # enables instant-parse for all downstream arrays
```

**Heuristic 7: Avoid Scan-Required Constructs**

Never use: `Bytes until`, `Type[]`, `current_position()`, `scope_remaining()`.
Exception: trailing `Byte[_rem]` is acceptable as the last field.

### Phase 3: Generate the SDDL File

Write a complete SDDL schema that:
- Uses correct SDDL v0.6 syntax (every multi-byte type has LE/BE suffix)
- Defines type aliases at the top (`U32 = UInt32LE`)
- Has a `magic : Byte[4]` and `num_records : U32` header
- Uses offset arrays for all variable-length fields
- Stores payload totals and padding explicitly
- Ends with `: Byte[_rem]` for forward-compat
- Includes comments explaining layout decisions
- Includes "Borrowed baseline idea from nyx/schemas/fasta_packed.sddl" comments
- Includes "Changed X because Y" comments where you deviated from the exemplar

The SDDL must be a NOVEL design optimized for the specific file format — not a
copy of fasta_packed.sddl with names changed.

### Phase 4: Generate the Preprocess Script

Write a complete, runnable `preprocess.py` that:
- Is pure Python 3.8+ (stdlib only — no numpy, no pandas)
- Streams the file line-by-line (never loads entire file into memory)
- Is lossless (binary output contains enough info to reconstruct original exactly)
- Is deterministic (same input always produces identical output)
- Splits output into chunks at record boundaries (~450 MiB max per chunk)
- Names chunks as `chunk_NNNNN.<format>.bin`
- Has a `main()` with argparse: `python3 preprocess.py <input> <output_dir>`
- Binary output order EXACTLY matches the SDDL field order
- Includes "Borrowed baseline idea from" comments
- Includes "Changed X because Y" comments

The preprocess.py must produce binary that EXACTLY matches the SDDL schema. Every
field in the SDDL must correspond to bytes written in the same order by preprocess.py.

### Phase 5: Write Output Files

**Output location rules:**
1. Default directory: `./sddl_out/` (relative to CWD)
2. If user specifies output dir, use that
3. If `custom.sddl` or `preprocess.py` already exist: version-increment
   (`custom_v2.sddl`, `preprocess_v2.py`, etc.)

**CRITICAL non-overwrite rules:**
- NEVER modify `nyx/schemas/fasta_packed.sddl`
- NEVER modify `nyx/nyx/core/preprocessor.py`
- NEVER modify `nyx/tools/genomic_preprocessor.cpp`
- NEVER modify ANY existing file in the repository
- ALWAYS write to new files only

### Phase 6: Print Decision Log

```
=== SDDL Generator: Decision Log ===

Format: <detected>
Strategy: <SoA columnar / record-packed / etc.>

Layout decisions:
  - SoA columnar: <yes/no> — <reason>
  - Symbol packing: <method> — <reason>
  - Instant-parse: <yes/no> — <scan-required fields if any>
  - Padding: <alignment> — <reason>
  - Header dedup: <yes/no> — <reason>
  - Entropy reduction: <methods applied>

SDDL compatibility notes:
  - <any features that might not work with installed OpenZL>

Output files:
  1. <path to .sddl>
  2. <path to .py>

To use with nyx:
  nyx compress <input> --mode train_custom --sddl <path to .sddl>
```

---

## SDDL LANGUAGE REFERENCE (Fallback if web access is unavailable)

This section is used ONLY if you cannot fetch the OpenZL docs. If you
successfully read the docs in Stage 1, you have richer information than this.

### Types

| Type | Size | Notes |
|------|------|-------|
| Int8 / UInt8 | 1 | No endianness suffix needed |
| Int16LE/BE / UInt16LE/BE | 2 | |
| Int32LE/BE / UInt32LE/BE | 4 | |
| Int64LE/BE / UInt64LE/BE | 8 | |
| Float32LE/BE / Float64LE/BE | 4/8 | |
| Bytes(N) | N | Raw blob |

**Critical:** Every multi-byte type MUST have LE or BE suffix.

### Syntax

```sddl
# Type aliases
U32 = UInt32LE

# Records
Record Name(param1) = { field1 : Type, field2 : Type[param1] }

# Unions
Union Name(selector, size) = { case 1: TypeA(size), default: Raw(size) }

# Conditionals (no `then` with braces)
when condition { field : Type }

# Variables
var x = param1 + 1

# Validation
expect magic == "FAV4"
field : U32 where (field <= 1024)

# Arrays
fixed : Type[count]
soa_layout : soa Record[count]   # structure-of-arrays

# Alignment
align(4) field : Type
Record R() = pad_align 4 { }
Record R() = pad_to 64 { }

# Annotations
@instant_parse
@chunk_size 128 kb
```

### Instant-Parse Rules

A construct is instant-parse if ALL offsets/sizes depend ONLY on parameters and
constants. Breaks instant-parse: `Bytes until`, `Type[]`, `current_position()`,
`var x = parsed_field`, `when parsed_field`.

### Keywords

`Record`, `Union`, `enum`, `when`, `then`, `case`, `default`, `var`, `expect`,
`where`, `scan`, `soa`, `align`, `pad_to`, `pad_align`, `until`, `include_delim`,
`switch`, `and`, `or`, `not`, `in`

### Functions

`abs(x)`, `min(a,b)`, `max(a,b)`, `clamp(l,x,h)`, `between(l,x,h)`, `sgn(x)`,
`ceil_div(x,d)`, `align_up(x,a)`, `sizeof(T())`, `parsed_length(f)`,
`current_position()`, `scope_remaining()`

---

## RESPONSE FORMAT

Your response must contain:

1. Confirmation that you read the exemplar files (brief — don't dump contents)
2. The analysis summary
3. The two written files (confirm paths, show key design decisions)
4. The decision log
5. A one-liner showing how to use the outputs with nyx

Do NOT ask follow-up questions. Do NOT suggest alternatives. Just generate.
