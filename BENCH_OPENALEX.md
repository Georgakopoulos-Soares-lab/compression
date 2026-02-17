# OpenAlex Compression Benchmark (`nyx openalex-benchmark`)

Benchmark suite for evaluating compression on OpenAlex academic metadata
snapshots. Compares JSONL text vs ASTBIN binary representations across
multiple compressors including schema-aware nyx/OpenZL compression.

## Downloading OpenAlex Data

OpenAlex snapshots are publicly available on S3 (no credentials needed):

```bash
# Install AWS CLI if needed
brew install awscli   # macOS
# apt install awscli  # Linux

# Download a subset (~8 GB compressed, ~50+ GB decompressed)
export ENTITY=authors
export TARGET_GB=8
export OUT="data/openalex_${ENTITY}_${TARGET_GB}gb"
mkdir -p "$OUT"

python3 - <<'PY'
import os, subprocess, pathlib, sys

entity = os.environ["ENTITY"]
target = int(float(os.environ["TARGET_GB"]) * (1024**3))
out = pathlib.Path(os.environ["OUT"])
out.mkdir(parents=True, exist_ok=True)

p = subprocess.run(
    ["aws","s3","ls","--recursive","--no-sign-request",
     f"s3://openalex/data/{entity}/"],
    check=True, capture_output=True, text=True
)

rows = []
for line in p.stdout.splitlines():
    parts = line.split()
    if len(parts) >= 4 and parts[3].endswith(".gz"):
        rows.append((int(parts[2]), parts[3]))

rows.sort(reverse=True)
picked, total = [], 0
for size, key in rows:
    if total >= target:
        break
    picked.append((size, key))
    total += size

print(f"Picking {len(picked)} files totaling ~{total/(1024**3):.2f} GiB",
      file=sys.stderr)

for size, key in picked:
    dest = out / pathlib.Path(key).name
    if dest.exists():
        continue
    subprocess.run(
        ["aws","s3","cp","--no-sign-request",
         f"s3://openalex/{key}", str(dest)],
        check=True
    )
PY
```

Or download a single date's snapshot directly:

```bash
mkdir -p data/openalex_authors
aws s3 sync \
  "s3://openalex/data/authors/updated_date=2023-02-24/" \
  data/openalex_authors/ --no-sign-request
```

Each `.gz` file is an independent gzipped JSONL shard (~460 MB compressed,
~3 GB decompressed). You can use any subset for benchmarking.

## Running the Benchmark

### Full benchmark

```bash
nyx openalex-benchmark \
  --input-gz-dir data/openalex_authors_8gb \
  --outdir bench_out/authors \
  --shard-mb 128 \
  --runs 5
```

### Smoke test (quick validation)

```bash
nyx openalex-benchmark \
  --input-gz-dir data/openalex_authors_8gb \
  --outdir bench_out/smoke \
  --limit-records 10000 \
  --runs 2 --warmup 0
```

### All options

```
nyx openalex-benchmark [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--input-gz-dir` | (required) | Directory with OpenAlex `.gz` files |
| `-o, --outdir` | `bench_out` | Output directory |
| `--pattern` | `*.gz` | Glob pattern for input files |
| `--shard-mb` | `128` | Target JSONL shard size in MiB |
| `--runs` | `5` | Timed measurement runs |
| `--warmup` | `1` | Warmup runs (excluded) |
| `--keep-temp` | off | Keep decompressed temp files |
| `-f, --force` | off | Overwrite existing output |
| `--zstd-level` | `7` | Zstandard level |
| `--gzip-level` | `9` | Gzip level |
| `--pigz-level` | `9` | Pigz level |
| `--nyx-sddl` | `astbin_v1.sddl` | SDDL schema for nyx |
| `--nyx-cmd` | (see docs) | Nyx compress template |
| `--nyx-dec` | (see docs) | Nyx decompress template |
| `--limit-records` | unlimited | Stop after N records (smoke test) |
| `-v, --verbose` | off | Verbose output |

## Pipeline

### Step 1: JSONL Sharding

- Discovers `.gz` files in `--input-gz-dir` (sorted lexicographically)
- Streams decompressed JSONL lines via Python `gzip` module
- Canonicalizes each JSON record (sorted keys, compact separators)
- Writes fixed-size shard files: `artifacts/jsonl/shard_00001.jsonl`, ...
- Produces `artifacts/jsonl/index.json` with provenance mapping

### Step 2: ASTBIN Conversion

- For each JSONL shard, produces a binary ASTBIN v1 shard
- All records in a shard are wrapped in a JSON array and serialized
  to the ASTBIN columnar format (token stream + typed value pools)
- Output: `artifacts/astbin/shard_00001.astbin`, ...
- SHA-256 checksums recorded in `artifacts/manifest.json`

### Step 3: Benchmarking

Compressors tested:

| Compressor | Level | Artifacts | Notes |
|-----------|-------|-----------|-------|
| gzip | 9 | JSONL, ASTBIN | Baseline |
| pigz | 9 | JSONL, ASTBIN | Parallel gzip |
| zstd | **7** | JSONL, ASTBIN | Default level 7 |
| nyx (OpenZL) | SDDL | ASTBIN only | Schema-aware |

Each (shard, compressor) pair:
1. Compress with `time.perf_counter()` timing
2. Decompress
3. Verify: `sha256(decompressed) == sha256(original)`
4. Repeat `--runs` times (plus `--warmup` warmup runs)

## Output

```
bench_out/
  env.json              # Tool versions, platform info
  results.json          # Full results with config
  results.csv           # Tabular results for import
  artifacts/
    manifest.json       # Per-shard metadata + SHA-256 hashes
    jsonl/
      shard_00001.jsonl
      shard_00002.jsonl
      ...
      index.json        # Record provenance index
    astbin/
      shard_00001.astbin
      shard_00002.astbin
      ...
  compressed/
    jsonl/
      gzip/
      pigz/
      zstd/
    astbin/
      gzip/
      pigz/
      zstd/
      nyx/
  decompressed/         # Only if --keep-temp
```

## Interpreting Results

The benchmark produces an aggregated table like:

```
| Artifact   | Compressor       |      Input |   Compressed |   Ratio | ...
| jsonl      | zstd -7          |    1.2 GiB |    123.4 MiB |   9.95x | ...
| astbin     | zstd -7          |  987.6 MiB |     98.7 MiB |  10.01x | ...
| astbin     | nyx -sddl        |  987.6 MiB |     76.5 MiB |  12.91x | ...
```

### Key comparisons

1. **zstd on JSONL vs zstd on ASTBIN** — Does the binary columnar layout
   help generic compressors? ASTBIN groups similar data types together
   (all tokens, all string offsets, all ints, etc.) which may improve
   dictionary-based compression.

2. **nyx on ASTBIN vs zstd on ASTBIN** — OpenZL's schema-aware compression
   knows the ASTBIN field structure via SDDL. It can learn per-field
   strategies (delta coding for offsets, specialized models for the
   constrained token alphabet, etc.). This measures the incremental benefit
   of schema awareness over generic compression.

3. **Ratio vs throughput trade-off** — Better ratio often means slower
   compression. Check C MB/s and D MB/s columns to evaluate whether the
   ratio improvement is worth the speed cost.

## Required Tools

| Tool | Required? | Install (macOS) | Install (Linux) |
|------|-----------|-----------------|-----------------|
| Python 3.8+ | Yes | Pre-installed | `apt install python3` |
| gzip | Yes | Pre-installed | Pre-installed |
| pigz | Recommended | `brew install pigz` | `apt install pigz` |
| zstd | Recommended | `brew install zstd` | `apt install zstd` |
| nyx (OpenZL) | Optional | `pip install -e ./nyx && nyx build` | Same |
| AWS CLI | For download | `brew install awscli` | `apt install awscli` |
