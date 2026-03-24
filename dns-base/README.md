# DNS Base — TLV Binary Compression with OpenZL

Compresses raw DNS TLV binary payloads (from Akamai's `nom-dns-base` Kafka topic)
using OpenZL. Achieves **8.21x** compression ratio vs Snappy's **4.24x** — a **1.94x
improvement** — with decompression speed within 19% of Snappy.

## Results Summary

```
                    Snappy      OpenZL       OpenZL
                   (current)   (trained)   (untrained)
Ratio:              4.24x       8.21x       7.85x
Compressed size:    27.9 MB     14.4 MB     15.0 MB
Compress speed:     1,351 MB/s  141 MB/s    198 MB/s
Decompress speed:   2,928 MB/s  2,366 MB/s  2,304 MB/s
```

Benchmark on 2,000 TLV chunks (118 MB total, avg 59 KB/chunk). Best of 5 iterations.

## Data Format

The input data is **raw TLV (Type-Length-Value) binary** extracted from Kafka
GenericChunk messages. Each chunk contains a batch of DNS query/response records.

### GenericChunk wire format
```
[4B uint32  total_length]
[1B uint8   compression_flag]     (always 0 — no app-level compression)
[4B uint32  header_length]
[NB         protobuf_header]      (type, source, format, timestamp, nodeID)
[4B uint32  data_length]
[NB         tlv_payload]          (batch of DNS records in TLV encoding)
```

### TLV record format (inside the payload)
```
Per record:
  [4B uint32  record_length]
  [8B uint64  start_time]
  [4B uint32  flags]
  [2B uint16  client_port]
  [1B uint8   inet_family]        (4=IPv4, 6=IPv6)
  [TLV fields follow...]

Per field:
  [1B uint8   type_id]            (0-37, see DnsQuery TLVType enum)
  [2B uint16  data_length]
  [NB         data_bytes]
```

## Prerequisites

- Python 3 with `python-snappy` (`pip install python-snappy`)
- OpenZL `zli` binary (built from the repo root: `../openzl/zli`)

## Directory Contents

```
dns-base/
├── README.md                   # This file
├── dns_tlv_compressor.zl       # Pre-trained OpenZL compressor for DNS TLV data
├── extract_generic_chunks.py   # Extracts TLV payloads from raw GenericChunk .bin files
├── benchmark_snappy_tlv.py     # Snappy benchmark script for TLV files
├── RESULTS.txt                 # Detailed benchmark results
└── openzl-raw.zip              # Raw data archive (not in repo, obtain separately)
```

## Full Reproduction Procedure

### Step 1: Extract TLV payloads from raw data

The `openzl-raw.zip` archive contains `dns-raw.tar.gz`, which holds 81
length-prefixed GenericChunk binary files (`nom-dns-base_NNNN.bin`, ~50 MB each,
4 GB total) collected from Kafka `nom-dns-base` topic. It also contains this
`extract_generic_chunks.py` script and a data collection README.

```bash
# Extract the zip, then the inner tarball
unzip openzl-raw.zip
tar -xzf openzl-raw/dns-raw.tar.gz -C openzl-raw/
# Produces: openzl-raw/dns-raw/ directory with nom-dns-base_NNNN.bin files

# Decompose GenericChunks into individual TLV payloads
python3 extract_generic_chunks.py \
    --input-dir openzl-raw/dns-raw \
    --output-dir dns-extracted

# Output:
#   dns-extracted/tlv/          — one .tlv file per GenericChunk payload
#   dns-extracted/full/         — complete GenericChunk per chunk
#   dns-extracted/headers/      — decoded protobuf headers (human-readable)
#   dns-extracted/manifest.csv  — per-chunk sizes, record counts, metadata
#   dns-extracted/combined_tlv_payloads.bin — all TLV concatenated
```

### Step 2: Prepare training and benchmark sets

Split the extracted TLV files into a training set (~200 files) and a benchmark
set (~2000 files).

```bash
mkdir -p tlv-train tlv-bench

# Training set: 200 files from the middle of the dataset
ls dns-extracted/tlv/*.tlv | sort | sed -n '100,299p' | while read f; do
    cp "$f" tlv-train/
done

# Benchmark set: first 2000 files
ls dns-extracted/tlv/*.tlv | sort | head -2000 | while read f; do
    cp "$f" tlv-bench/
done
```

### Step 3: Train OpenZL compressor

```bash
ZLI=../openzl/zli    # Path to the zli binary

$ZLI train tlv-train \
    -p serial \
    --use-all-samples \
    -o dns_tlv_compressor.zl
```

Parameters:
- **Profile**: `serial` (raw bytes — no CSV/structured parsing, treats data as opaque binary)
- **Trainer**: `greedy` (default, no flag needed)
- **`--use-all-samples`**: Use all files in the training directory (ignores default size limits)

Expected output:
```
Using all provided training samples, total size 12302498
Benchmarking untrained compressor...
210 files: 12302498 -> 1556291 (7.91),  201.49 MB/s  2307.81 MB/s
Training ACE graph 1 / 1: ACE progress
Benchmarking trained compressor...
210 files: 12302498 -> 1488402 (8.27),  143.72 MB/s  2490.14 MB/s
Training improved compression ratio by 4.56%
```

This produces `dns_tlv_compressor.zl` (~326 bytes). Training takes < 1 minute.

### Step 4: Benchmark OpenZL on TLV data

```bash
# OpenZL trained benchmark
$ZLI benchmark tlv-bench -c dns_tlv_compressor.zl -n 5

# Expected output:
# 2000 files: 118045149 -> 14377731 (8.21),  141.41 MB/s  2366.35 MB/s

# OpenZL untrained baseline (for comparison)
$ZLI benchmark tlv-bench -p serial -n 5

# Expected output:
# 2000 files: 118045149 -> 15039496 (7.85),  198.08 MB/s  2303.54 MB/s
```

### Step 5: Benchmark Snappy on TLV data

```bash
pip install python-snappy

python3 benchmark_snappy_tlv.py tlv-bench 5

# Expected output:
# Files:          2000
# Total raw:      118.0 MB
# Avg chunk size: 59.0 KB
# Iterations:     5
#
# Compressed:     27.9 MB
# Ratio:          4.24x
#
# Compress:       1351 MB/s  (best of 5: 0.087s)
# Decompress:     2928 MB/s  (best of 5: 0.040s)
#
# Per-chunk ratio: min=1.00x  max=8.90x  avg=3.93x
# Per-chunk compressed: min=0.1 KB  max=21.6 KB  avg=13.9 KB
```

### Step 6: Compress / Decompress individual files

```bash
# Compress a single TLV file
$ZLI compress tlv-bench/chunk_000001.tlv \
    -c dns_tlv_compressor.zl \
    -o chunk_000001.tlv.zl

# Decompress it back
$ZLI decompress chunk_000001.tlv.zl \
    -c dns_tlv_compressor.zl \
    -o chunk_000001_restored.tlv

# Verify lossless roundtrip
diff chunk_000001.tlv chunk_000001_restored.tlv && echo "MATCH"

# Compress an entire directory
mkdir -p tlv-compressed
$ZLI compress tlv-bench -c dns_tlv_compressor.zl -o tlv-compressed

# Decompress an entire directory
mkdir -p tlv-decompressed
$ZLI decompress tlv-compressed -c dns_tlv_compressor.zl -o tlv-decompressed
```

## How This Fits in the Production Pipeline

In production, Akamai's DNS pipeline uses **Snappy at the Kafka transport layer**
(via librdkafka `compression.codec = "snappy"`). The application never sees
compression — it's transparent.

To integrate OpenZL, compression moves to the **application level** inside the
GenericChunk:

1. In `chunk.cc` (nomchunk library), replace `snappy::RawCompress` /
   `snappy::RawUncompress` with OpenZL compress/decompress calls
2. Set `want_compression_ = true` on chunks before serialization
3. Set `compression.codec = "none"` on librdkafka producers
4. Load the trained `dns_tlv_compressor.zl` model at process startup
5. The protobuf header stays uncompressed (consumers can route without decompressing)

Benefits:
- **2x better compression ratio** (8.21x vs 4.24x) → 48% less Kafka disk
- **Near-identical decompression speed** (2,366 vs 2,928 MB/s)
- **Mirror hops become zero-cost** — compressed payload passes through unmodified
- **Same data, same pipeline** — only the compressor changes

## Quick Reference

```bash
# One-shot: extract, train, benchmark (assuming openzl-raw.zip is present)
unzip openzl-raw.zip
tar -xzf openzl-raw/dns-raw.tar.gz -C openzl-raw/
python3 extract_generic_chunks.py --input-dir openzl-raw/dns-raw --output-dir dns-extracted
mkdir -p tlv-train tlv-bench
ls dns-extracted/tlv/*.tlv | sort | sed -n '100,299p' | while read f; do cp "$f" tlv-train/; done
ls dns-extracted/tlv/*.tlv | sort | head -2000 | while read f; do cp "$f" tlv-bench/; done
ZLI=../openzl/zli
$ZLI train tlv-train -p serial --use-all-samples -o dns_tlv_compressor.zl
$ZLI benchmark tlv-bench -c dns_tlv_compressor.zl -n 5
python3 benchmark_snappy_tlv.py tlv-bench 5
```
