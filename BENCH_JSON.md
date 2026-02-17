# JSON Compression Benchmark (`nyx json-benchmark`)

Benchmark suite comparing compression of JSON data across three artifact
representations and multiple compressors.

## Quick Start

```bash
# Ensure nyx is installed
pip install -e ./nyx

# Run on a JSON file
nyx json-benchmark data/sample.json -o bench_out

# With custom settings
nyx json-benchmark data/large.json --runs 10 --warmup 2 --zstd-level 7 -o results/
```

## Required External Tools

| Tool | Required? | Install (macOS) | Install (Linux) |
|------|-----------|-----------------|-----------------|
| Python 3.8+ | Yes | Pre-installed | `apt install python3` |
| gzip | Yes | Pre-installed | Pre-installed |
| pigz | Recommended | `brew install pigz` | `apt install pigz` |
| zstd | Recommended | `brew install zstd` | `apt install zstd` |
| nyx (OpenZL) | Optional | `pip install -e ./nyx && nyx build` | Same |

Missing tools are automatically skipped with a note.

## Artifacts

The benchmark generates three representations of the input JSON:

### 1. Original JSON (`original.json`)
The input file copied verbatim. Baseline for comparison.

### 2. TOON-like Normalized Text (`normalized.toon`)
A deterministic, human-readable text representation. **This is NOT a full TOON
(Typed Object-Oriented Notation) implementation** — it is labeled "TOON-like"
because no practical Python TOON library exists. The normalization rules are:

- Object keys sorted lexicographically (Unicode code-point order)
- Arrays preserve original order
- 2-space indentation
- Strings use minimal JSON-compatible escaping
- Numbers: integers as decimal strings, floats in Python `repr()` form
- Booleans: `true` / `false` (lowercase)
- Null: `null`

The output is valid JSON. The purpose is to measure how compressors handle a
canonical, fully normalized text form vs. the original (potentially
inconsistently formatted) JSON.

### 3. ASTBIN v1 Binary (`data.astbin`)
A binary container that encodes the JSON abstract syntax tree. Key properties:

- **Deterministic:** Same JSON always produces identical bytes
- **Columnar layout:** Tokens, args, strings, ints, floats, bools stored in
  separate sections (good for schema-aware compression)
- **String deduplication:** All object keys and string values are interned in
  a sorted string table
- **Type-specific encoding:** int64 for integers, float64 for floats,
  uint8 for booleans

The SDDL schema (`schemas/astbin_v1.sddl`) describes this format for
OpenZL's schema-aware compression.

## Compressors

| Compressor | Default Level | Runs On | Notes |
|-----------|--------------|---------|-------|
| gzip | 9 | All artifacts | Baseline |
| pigz | 9 | All artifacts | Parallel gzip |
| zstd | **7** | All artifacts | Default level is 7 |
| nyx (OpenZL) | SDDL-based | ASTBIN only | Schema-aware compression |

## CLI Reference

```
nyx json-benchmark [OPTIONS] INPUT_FILE
```

### Arguments

| Argument | Description |
|----------|-------------|
| `INPUT_FILE` | Path to input JSON (or JSONL) file |

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `-o, --outdir` | `bench_out` | Output directory |
| `--runs` | `5` | Timed measurement runs per pair |
| `--warmup` | `1` | Warmup runs (not counted) |
| `--keep-temp` | off | Keep decompressed temp files |
| `-f, --force` | off | Overwrite existing output dir |
| `--zstd-level` | `7` | Zstandard compression level |
| `--gzip-level` | `9` | Gzip compression level |
| `--pigz-level` | `9` | Pigz compression level |
| `--nyx-sddl` | `astbin_v1.sddl` | SDDL schema for nyx |
| `--nyx-cmd` | (see below) | Nyx compress command template |
| `--nyx-dec` | (see below) | Nyx decompress command template |
| `-v, --verbose` | off | Verbose output |

### Nyx Command Templates

The `--nyx-cmd` and `--nyx-dec` options accept command templates with placeholders:

- `{input}` — input file path
- `{output}` — output file path
- `{sddl}` — SDDL schema path

Defaults:
```
--nyx-cmd "nyx compress {input} -o {output} --mode train_custom --sddl {sddl} -f"
--nyx-dec "nyx decompress {input} -o {output} -f"
```

Override if your nyx setup uses different flags or paths.

## Output

### Terminal
A markdown table grouped by artifact showing compression ratio, throughput,
and verification status.

### Files
```
<outdir>/
  results.json        # Full results with config and tool versions
  results.csv         # Tabular results for spreadsheet import
  artifacts/          # Generated artifact files
    original.json
    normalized.toon
    data.astbin
  compressed/         # Compressed outputs
  decompressed/       # Decompressed outputs (removed unless --keep-temp)
```

## Interpreting Results

- **Ratio** = `input_bytes / compressed_bytes` (higher is better)
- **C MB/s** = compression throughput (median across runs)
- **D MB/s** = decompression throughput (median across runs)
- **Verify** = SHA-256 round-trip integrity check (PASS/FAIL)

### What to look for

1. **TOON vs JSON:** Does normalization help or hurt compression? Sorted keys
   and consistent formatting often improve dictionary-based compressors.

2. **ASTBIN vs text:** The binary columnar layout groups similar data types
   together, which can dramatically improve schema-aware compression (nyx).
   Generic compressors (gzip/zstd) may or may not benefit.

3. **nyx on ASTBIN:** When OpenZL is trained with the SDDL schema, it can
   learn per-field compression strategies. Compare this against generic
   compressors on the same ASTBIN binary.

## Example

```bash
# Generate a test JSON file
python3 -c "
import json
data = [{'id': i, 'name': f'item_{i}', 'value': i * 1.5, 'active': i % 2 == 0}
        for i in range(10000)]
json.dump(data, open('test.json', 'w'))
"

# Run benchmark
nyx json-benchmark test.json -o test_results --runs 3 --warmup 1

# View results
cat test_results/results.json | python3 -m json.tool
```
