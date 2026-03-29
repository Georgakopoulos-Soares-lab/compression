# DNS Data Collection — Complete Guide

Step-by-step guide to collect DNS data from a live deployment for OpenZL
compression analysis. Covers all scripts, what they produce, and how to
reproduce the collection from scratch.

## Environment

- **Deployment template**: `~/work/sst-template-migration/head/SB_load_192_dest/run/194`
- **Terraform directory**: `<template>/terraform`
- **SSH config**: `<template>/terraform/ssh.config`
- **SSH user**: `centos` (port 9922 externally)
- **Reference docs**:
  - `~/work/sst-template-migration/head/deployment-and-pipeline-reference.md`
  - `~/work/sst-template-migration/head/dns-pipeline-compression-reference.md`

### Relevant Kafka Clusters

| Cluster | Brokers | DNS Topics |
|---------|---------|------------|
| POP Kafka | pop-kafka0/1/2 (10.0.0.82-84:9093) | nom-dns-base (~18.7 GB/partition, 8 partitions) |
| DC1 Kafka | dc1-kafka0/1/2 (10.0.0.8-10:9093) | nom-dns-vertica (~1.7 GB/partition, 20 partitions) |

### Prerequisites on the Kafka broker VM

```bash
sudo pip3 install kafka-python-ng python-snappy
```

`python-snappy` is required because Kafka stores messages with Snappy compression
at the batch level (via librdkafka). The Python consumer needs it to decompress.

---

## Scripts

All scripts are in `~/personal/compression/`.

### 1. `collect_raw_chunks.py` — Collect raw GenericChunk binary from Kafka

Consumes raw Kafka messages and saves them as length-prefixed binary files.
Each Kafka message is one GenericChunk containing ~5,600 DNS records.

**Run on**: A Kafka broker VM (e.g., pop-kafka0 for nom-dns-base)

**File format**: Each message is stored as `[4B uint32 length][message bytes]`,
so multiple messages are packed into each output file and can be split apart later.

```bash
# SCP to the broker
scp -F <terraform>/ssh.config collect_raw_chunks.py centos@pop-kafka0:/tmp/

# Collect 4 GB of raw chunks (50 MB per file)
python3 /tmp/collect_raw_chunks.py \
    --brokers 10.0.0.82:9093 \
    --topic nom-dns-base \
    --output-dir /tmp/dns-raw \
    --max-file-mb 50 \
    --max-total-mb 4000
```

**Output**:
```
/tmp/dns-raw/
├── nom-dns-base_0001.bin    # ~50 MB, length-prefixed GenericChunks
├── nom-dns-base_0002.bin
├── ...
└── nom-dns-base_NNNN.bin
```

**Options**:
| Flag | Default | Description |
|------|---------|-------------|
| `--brokers` | required | Kafka bootstrap servers |
| `--topic` | required | Topic to consume |
| `--output-dir` | required | Where to write files |
| `--max-file-mb` | 50 | Rotate file at this size |
| `--max-total-mb` | 0 (unlimited) | Stop after total bytes |
| `--max-messages` | 0 (unlimited) | Stop after N messages |
| `--tls` | off | Enable TLS |

### 2. `extract_generic_chunks.py` — Decompose GenericChunks offline

Reads the binary files from step 1 and splits each GenericChunk into three parts:
1. **Full chunk** — the complete GenericChunk binary (for baseline measurement)
2. **Protobuf header** — decoded header fields (type, source, format, nodeID, etc.)
3. **TLV payload** — the raw DNS TLV records (input for the preprocessor)

Also produces a combined TLV file (all payloads concatenated) and a manifest CSV.

**Run on**: Your Mac (offline, no Kafka needed)

```bash
python3 extract_generic_chunks.py \
    --input-dir ~/Desktop/openzl-dns/dns-raw \
    --output-dir ~/Desktop/openzl-dns/dns-extracted
```

**Output**:
```
dns-extracted/
├── full/                          # Complete GenericChunk per chunk
│   ├── chunk_000001.bin
│   └── ...
├── headers/                       # Decoded protobuf headers (human-readable)
│   ├── chunk_000001.txt
│   └── ...
├── tlv/                           # Raw TLV payloads (preprocessor input)
│   ├── chunk_000001.tlv
│   └── ...
├── combined_tlv_payloads.bin      # All TLV concatenated (for bulk training)
└── manifest.csv                   # Per-chunk sizes, record counts, metadata
```

**Options**:
| Flag | Default | Description |
|------|---------|-------------|
| `--input-dir` | required | Directory with .bin files from step 1 |
| `--output-dir` | required | Where to write extracted files |
| `--no-individual-files` | off | Skip per-chunk files, only combined + manifest |

### 3. `collect_dns_data.sh` — Collect decoded text from Kafka

Wraps `nom-kafka-dump` to collect decoded tab-delimited DNS records with file
rotation. This is the human-readable text representation — useful for profiling
column distributions and designing SDDL schemas.

**Run on**: A Kafka broker VM (where `nom-kafka-dump` is installed)

```bash
# SCP to the broker
scp -F <terraform>/ssh.config collect_dns_data.sh centos@pop-kafka0:/tmp/

# Collect 2 GB of decoded text (250 MB per partition, 8 partitions)
for p in 1 2 3 5 7 8 9 11; do
    nohup /tmp/collect_dns_data.sh \
        --brokers 10.0.0.82:9093 \
        --topic nom-dns-base \
        --partition $p \
        --output-dir /tmp/dns-capture-base \
        --max-mb 50 \
        --max-total-mb 250 \
        > /tmp/dns-capture-base-p${p}.log 2>&1 &
done
```

**Output**: `nom-dns-base_p1_0001.tsv`, `_p1_0002.tsv`, etc. (tab-delimited, one DNS record per line)

**Options**:
| Flag | Default | Description |
|------|---------|-------------|
| `--brokers` | required | Kafka bootstrap servers |
| `--topic` | required | Topic to consume |
| `--output-dir` | required | Where to write files |
| `--partition` | all | Specific partition (for parallel collection) |
| `--max-mb` | 50 | Rotate file at this size |
| `--max-total-mb` | 0 (unlimited) | Stop after total bytes |

### 4. `kafka_to_file_dumper.py` — Generic Kafka-to-file dumper

A Python-based Kafka consumer that writes raw message values to rotating files.
Works for any topic, but messages with binary content (GenericChunk) will produce
binary files. Best for topics with text/JSONL content (like nom-telemetry).

**Run on**: Any VM with Python 3 and kafka-python-ng

```bash
python3 kafka_to_file_dumper.py \
    --brokers 10.0.0.8:9093 \
    --topic nom-telemetry \
    --output-dir /tmp/telemetry-capture \
    --max-file-mb 50
```

---

## Data Collection Procedures

### Procedure A: Collect raw binary GenericChunks (for TLV preprocessing)

This is what you need for building the DNS TLV preprocessor and SDDL schema.

**On your Mac:**
```bash
# 1. SCP the collection script
scp -F ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform/ssh.config \
    ~/personal/compression/collect_raw_chunks.py centos@pop-kafka0:/tmp/

# 2. SSH to the broker and install dependencies
ssh -F ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform/ssh.config \
    -o StrictHostKeyChecking=no centos@pop-kafka0
sudo pip3 install kafka-python-ng python-snappy

# 3. Run the collection (on the broker)
python3 /tmp/collect_raw_chunks.py \
    --brokers 10.0.0.82:9093 \
    --topic nom-dns-base \
    --output-dir /tmp/dns-raw \
    --max-file-mb 50 \
    --max-total-mb 4000

# 4. When done, compress and download (on the broker)
tar -czf /tmp/dns-raw.tar.gz -C /tmp dns-raw

# 5. SCP to your Mac
scp -F ~/work/sst-template-migration/head/SB_load_192_dest/run/194/terraform/ssh.config \
    centos@pop-kafka0:/tmp/dns-raw.tar.gz ~/Desktop/openzl-dns/

# 6. Extract and decompose offline (on your Mac)
cd ~/Desktop/openzl-dns
tar -xzf dns-raw.tar.gz
python3 ~/personal/compression/extract_generic_chunks.py \
    --input-dir dns-raw \
    --output-dir dns-extracted
```

### Procedure B: Collect decoded text (for column profiling)

**On your Mac:**
```bash
# 1. SCP the collection script
scp -F <terraform>/ssh.config \
    ~/personal/compression/collect_dns_data.sh centos@pop-kafka0:/tmp/

# 2. SSH and run (one collector per partition for diversity)
ssh -F <terraform>/ssh.config -o StrictHostKeyChecking=no centos@pop-kafka0
chmod +x /tmp/collect_dns_data.sh
for p in 1 2 3 5 7 8 9 11; do
    nohup /tmp/collect_dns_data.sh \
        --brokers 10.0.0.82:9093 \
        --topic nom-dns-base \
        --partition $p \
        --output-dir /tmp/dns-capture-base \
        --max-mb 50 --max-total-mb 250 \
        > /tmp/dns-capture-base-p${p}.log 2>&1 &
done

# 3. Monitor progress
du -sh /tmp/dns-capture-base/

# 4. When done, kill remaining collectors
ps aux | grep collect_dns | grep -v grep | awk '{print $2}' | xargs -r kill
ps aux | grep 'nom-kafka-dump --brokers' | grep -v grep | awk '{print $2}' | xargs -r kill

# 5. Compress and download
tar -czf /tmp/dns-capture-base.tar.gz -C /tmp dns-capture-base
# Then SCP to Mac
```

### Procedure C: Collect from nom-dns-vertica (DC1 Kafka)

Same as Procedure B but targeting DC1 Kafka:

```bash
# On dc1-kafka0:
for p in 0 1 3 5 6 7 9 11 12 13 15 17 18 19 21 23 24 25 27 29; do
    nohup /tmp/collect_dns_data.sh \
        --brokers 10.0.0.8:9093 \
        --topic nom-dns-vertica \
        --partition $p \
        --output-dir /tmp/dns-capture-vertica \
        --max-mb 50 --max-total-mb 100 \
        > /tmp/dns-capture-vertica-p${p}.log 2>&1 &
done
```

---

## Data Already Collected

The following data has been collected and is at `~/Desktop/openzl-dns/`:

| File | Size | Source | ∂Contents |
|------|------|--------|----------|
| `dns-capture-base.tar.gz` | 112 MB | POP Kafka nom-dns-base | ~2 GB decoded text from 8 partitions |
| `dns-capture-vertica.tar.gz` | 558 MB | DC1 Kafka nom-dns-vertica | ~2 GB decoded text from 20 partitions |
| `dns-chunk-sizes.tar.gz` | 1.2 KB | DC1 Kafka nom-dns-vertica | CSV of 200 chunk sizes (avg 34.6 KB) |
| `dns-raw.tar.gz` | (in progress) | POP Kafka nom-dns-base | ~4 GB raw GenericChunk binary |

---

## GenericChunk Binary Format

Each raw Kafka message from DNS topics is a GenericChunk:

```
[4B uint32  total_length]
[1B uint8   compression_flag]     (0=none, 1=snappy — cache-serve sets 0)
[4B uint32  header_length]
[NB         protobuf_header]      (ChunkHeader: type, source, format, timestamp, nodeID)
[4B uint32  data_length]
[NB         tlv_payload]          (batch of DNS records in TLV encoding)
```

Note: Kafka-level Snappy compression (applied by librdkafka) is transparent —
the consumer library decompresses it before delivering the message. The
`compression_flag` byte above refers to application-level compression inside
the GenericChunk, which cache-serve always sets to 0.

### TLV Record Format (inside the payload)

```
Per record:
  [4B uint32  record_length]
  [8B uint64  start_time]
  [4B uint32  flags]
  [2B uint16  client_port]
  [1B uint8   inet_family]        (4=IPv4, 6=IPv6)
  [TLV fields follow...]

Per field:
  [1B uint8   type_id]            (0-37, see DnsQuery.java TLVType enum)
  [2B uint16  data_length]
  [NB         data_bytes]
```

### Decoded Text Format (from nom-kafka-dump)

21 tab-delimited columns per line:

```
Col  0: timestamp (microseconds)
Col  1: query-uuid
Col  2: client-id
Col  3: query-name (domain)
Col  4: (usually empty)
Col  5: core-domain
Col  6: node-id
Col  7-13: (usually empty)
Col 14: qtype (A, AAAA, PTR, etc.)
Col 15: rcode (noerror, nxdomain, refused, etc.)
Col 16: flags1
Col 17: flags2
Col 18: numeric-id
Col 19: (empty)
Col 20: count
```

---

## Profiling Results (from 50,000 decoded records)

| Column | Unique Values | Empty% | Top Value | Top% | Suggested Transform |
|--------|---------------|--------|-----------|------|---------------------|
| 0 (timestamp) | 1 | 0% | 1774143900000000 | 100% | Delta encoding |
| 1 (uuid) | ~49,346 | 0% | — | — | Raw 16-byte binary |
| 2 (client-id) | ~49,935 | 0% | — | — | Raw binary |
| 3 (domain) | 4,340 | 0% | apple.com | 16.1% | Dictionary or raw |
| 4 | 3 | 99.9% | (empty) | 99.9% | Drop |
| 5 (core-domain) | 4,340 | 0% | apple.com | 16.1% | Dictionary or raw |
| 6 (node-id) | 3 | 0% | (one UUID) | 59.8% | Dictionary (1 byte) |
| 7-13 | 1 each | 100% | (empty) | 100% | Drop |
| 14 (qtype) | 10 | 0% | A | 90.5% | Dictionary (1 byte) |
| 15 (rcode) | 5 | 0% | noerror | 96.7% | Dictionary (1 byte) |
| 16 (flags1) | 1 | 0% | 0 | 100% | Drop (constant) |
| 17 (flags2) | 1 | 0% | 0 | 100% | Drop (constant) |
| 18 (numeric-id) | ~49,367 | 0% | — | — | Raw |
| 19 | 1 | 100% | (empty) | 100% | Drop |
| 20 (count) | 7 | 0% | 3 | 81.7% | Dictionary (1 byte) |

---

## Compression Baselines

| Method | Input | Compressed | Ratio |
|--------|-------|------------|-------|
| **Snappy on TLV binary** (current pipeline) | ~963 KB (5,600 records as text) | **35 KB** | **~27x** (vs text) |
| zstd default on text (50 MB) | 50 MB | 12.8 MB | 4.1x |
| zstd level 19 on text (50 MB) | 50 MB | 11.0 MB | 4.8x |
| zstd on 1 MB text chunk | 1 MB | 269 KB | 3.9x |

**Target for OpenZL on columnar binary**: < 35 KB per 5,600 records (must beat Snappy).
