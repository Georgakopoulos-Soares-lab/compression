# FASTA Packed (FAV4) OpenZL pipeline (dev)

This folder is a self-contained, reproducible pipeline that:

1) downloads a large reference FASTA (default: NCBI GRCm39 mouse genome)
2) creates the exact “~200MiB” *record-safe* training sample used by the pipeline
3) preprocesses FASTA into a schema-matching binary format (`FAV4`, 4-bit packed bases)
4) trains an OpenZL compressor on the packed training chunk(s)
5) compresses the full packed FASTA and reports ratios vs the original text FASTA
6) runs a pigz `-9` baseline with timing

If you only want to run it, see [INSTALL.md](INSTALL.md). This README explains the *what/why* and includes the exact commands.

---

## Repo status: “clean” and ready to run

This repo is considered “clean” when generated outputs are absent (or ignored): `chunks*`, `out*`, `artifacts*`.

To reset back to a clean state at any time:

```bash
bash scripts/clean_generated.sh
```

Then the pipeline can be run again from scratch.

---

## Quickstart (the same pipeline you just ran)

```bash
# defaults: TARGET_MIB=200 THREADS=16 MAX_TIME_SECS=1800 (misi wra)
bash scripts/run_train_250_and_test_full.sh
```

Common overrides:

```bash
TARGET_MIB=200 THREADS=16 MAX_TIME_SECS=1800 COMPRESS_JOBS=4 NO_ACE_SUCCESSORS=1 \
  VALIDATE_FULL=1 bash scripts/run_train_250_and_test_full.sh
```

---

## Step-by-step: download → sample → train → full test

All commands below run from this `dev/` directory.

### 0) Fetch OpenZL and build everything

```bash
bash scripts/get_openzl.sh
bash scripts/build_all.sh
```

Build products:

- `openzl/zli`
- `tools/biocompress_preprocessor`

### 1) Download the FASTA (default dataset)

Download + decompress (idempotent):

```bash
bash scripts/download_fasta.sh
```

By default, this downloads:

- `https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/635/GCF_000001635.27_GRCm39/GCF_000001635.27_GRCm39_genomic.fna.gz`

Outputs:

- `data/GCF_000001635.27_GRCm39_genomic.fna.gz`
- `data/GCF_000001635.27_GRCm39_genomic.fna`

Override the dataset if you want:

```bash
FASTA_URL='https://.../your.fna.gz' bash scripts/download_fasta.sh
```

### 2) Create the exact ~200MiB training FASTA (record-safe)

The training sample is created by copying *whole FASTA records* (never splitting a record):

```bash
python3 scripts/make_train_sample.py \
  --in data/GCF_000001635.27_GRCm39_genomic.fna \
  --out out/train_200MiB.fasta \
  --target-mib 200
```

Note: because we keep whole records, the output often lands near (but not exactly) 200MiB.
On the default GRCm39 file, the pipeline typically writes 41 records and ~202,465,410 bytes.

### 3) Preprocess training FASTA → packed binary (`FAV4`)

This converts text FASTA to schema-matching `.fasta_packed.bin`:

```bash
./tools/biocompress_preprocessor out/train_200MiB.fasta chunks_train_200MiB 1 fasta_packed
```

Why `1` thread here? With the default chunk sizing, using one thread makes it more likely to emit a single training chunk.

### 4) Train OpenZL on the packed training data

```bash
./openzl/zli train chunks_train_200MiB \
  --profile sddl --profile-arg schemas/fasta_packed.sddl \
  --output artifacts/fasta_packed_train_200MiB_t16.compressor \
  --force --threads 16 --use-all-samples --max-time-secs 1800 \
  --no-ace-successors
```

The `--no-ace-successors` flag is used by default for robustness when compressing *unseen* chunks from the full FASTA.

### 5) Preprocess full FASTA → packed binary chunks

```bash
./tools/biocompress_preprocessor \
  data/GCF_000001635.27_GRCm39_genomic.fna \
  chunks_full 16 fasta_packed
```

### 6) Compress full chunks (parallel across chunk files)

```bash
find chunks_full -maxdepth 1 -type f -name '*.fasta_packed.bin' -print0 | \
  xargs -0 -P 4 -I {} ./openzl/zli compress "{}" \
    --compressor artifacts/fasta_packed_train_200MiB_t16.compressor \
    --output "{}.zl" --force
```

### 7) Optional: decompress + validate (byte-for-byte)

```bash
for bin in chunks_full/*.fasta_packed.bin; do
  ./openzl/zli decompress "$bin.zl" --output "$bin.dec" --force
  cmp -s "$bin" "$bin.dec" || { echo "Mismatch: $bin"; exit 1; }
  rm -f "$bin.dec"
done
echo "Full validation OK"
```

### 8) pigz -9 baseline (original FASTA)

```bash
pigz -9 -p 16 -c data/GCF_000001635.27_GRCm39_genomic.fna > out/pigz/GCF_000001635.27_GRCm39_genomic.fna.gz
```

---

## Binary format: `.fasta_packed.bin` and the SDDL schema

Schema: [schemas/fasta_packed.sddl](schemas/fasta_packed.sddl)

Each packed chunk file is a container of FASTA records. Conceptually it’s a “columnar-ish” layout:

- headers are concatenated into one byte array (`headers`)
- sequences are concatenated into one byte array (`sequences`), but bases are 4-bit packed (2 bases/byte)
- offsets arrays map each record to its slice of `headers` and `sequences`

Fields (in order):

- `magic` (4 bytes): ASCII `FAV4`
- `num_records` (U32)
- `hdr_offsets` (U32[num_records+1]): prefix sums into `headers`
- `seq_offsets` (U32[num_records+1]): prefix sums into `sequences` (packed bytes)
- `seq_lengths` (U32[num_records]): original sequence lengths in bases (needed because packing is 2 bases/byte)
- `hdr_total`, `seq_total` (U32): payload lengths (unpadded)
- `hdr_pad`, `seq_pad` (U32): padding amounts (we align payloads to 4 bytes)
- `headers` (Byte[hdr_total + hdr_pad])
- `sequences` (Byte[seq_total + seq_pad])

The trailing `: Byte[_rem]` in the SDDL is a permissive “consume remainder” so the schema won’t break if extra bytes appear.

### 4-bit base packing

The packed sequence stream stores bases as nibbles:

- `A=0`, `C=1`, `G=2`, `T=3`, `N=4` (and any unknown mapped to `N`)

Two bases per byte: first base in the high nibble, second base in the low nibble.
If the number of bases is odd, the last base is stored in the high nibble and the low nibble is padded with 0.

---

## C++ preprocessor: how it produces `FAV4`

Source: [tools/biocompress_preprocessor.cpp](tools/biocompress_preprocessor.cpp)

The preprocessor does two main jobs:

1) **Chunking**
   - mmaps the input FASTA for fast scanning
   - chooses chunk byte-ranges and *snaps* each range to FASTA record boundaries (`>` at start-of-line)
   - each worker thread converts one snapped range into one `.fasta_packed.bin`

2) **Parsing + packing (`process_fasta_packed_chunk`)**
   - whenever it sees a line starting with `>` it starts a new record and stores the header bytes without the leading `>`
   - sequence lines are concatenated (newlines ignored) and each base is mapped to a 4-bit value
   - bases are packed two-per-byte; a single trailing base is padded into the high nibble
   - `hdr_offsets` / `seq_offsets` are prefix sums (they start with 0 and append after each record)
   - `seq_lengths` stores the original base count per record so exact reconstruction is possible
   - `headers` and `sequences` payloads are padded to a 4-byte boundary (`hdr_pad`, `seq_pad`)

This is intentionally simple and “binary friendly” for OpenZL: the schema gives structure, and the preprocessor produces stable bytes that are easy to round-trip validate with `cmp`.

---

## Expected results (sanity checklist)

After a successful run of:

```bash
bash scripts/clean_generated.sh
bash scripts/run_train_250_and_test_full.sh
```

you should see these files/directories:

- `data/`
  - `GCF_000001635.27_GRCm39_genomic.fna.gz` (downloaded)
  - `GCF_000001635.27_GRCm39_genomic.fna` (decompressed)
- `out/`
  - `train_200MiB.fasta` (record-safe training FASTA; size near the target)
  - `timing/`
    - `train.time`, `prep_full.time`, `openzl_comp_full.time` (and `pigz.time` if pigz exists)
  - `pigz/` (only if `pigz` is installed)
    - `GCF_000001635.27_GRCm39_genomic.fna.gz`
- `artifacts/`
  - `fasta_packed_train_200MiB_t16.compressor`
- `chunks_train_200MiB/`
  - `chunk_00000.fasta_packed.bin` (often a single chunk with the default settings)
  - `*.fasta_packed.bin.zl` (training chunk compressed outputs)
- `chunks_full/`
  - `chunk_*.fasta_packed.bin` (packed full-file chunks)
  - `chunk_*.fasta_packed.bin.zl` (compressed full-file chunks)

Console output should include summary lines like:

- `Original FASTA size: ... MiB`
- `Training packed payload size: ... MiB`
- `Training-set validation OK`
- `Full packed input total: ... MiB`
- `OpenZL vs original FASTA: in=... out=... ratio=...x`
- `pigz -9 vs original FASTA: in=... out=... ratio=...x` (only if pigz exists)
- `Timing (seconds):` followed by the timing files.

Notes:

- Exact compressed sizes and ratios depend on the OpenZL commit, CPU, and training time budget, but the file layout and the “validation OK” result should be consistent.
- If `VALIDATE_FULL=1` is enabled, a successful run ends with `Full validation OK`.
