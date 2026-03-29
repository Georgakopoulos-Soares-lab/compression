# DNS TLV Preprocessor & SDDL Schema — Agent Handoff

## Mission

Build a C++ preprocessor that decomposes DNS TLV binary records into typed columnar streams, write an SDDL schema describing those streams, and train an initial OpenZL compressor. The goal: beat Snappy compression on the existing binary TLV format by applying domain-aware columnar decomposition — the same approach used for genomic data (FASTA, FASTQ, BED, VCF) in the OpenZL codebase.

## Why This Matters

The DNS data pipeline currently uses Snappy compression on binary TLV records. For ~5,600 records, Snappy produces ~35 KB. We tried compressing the text representation of the same records with zstd and OpenZL — it's 7x WORSE because the text format (tab-delimited, hex UUIDs, decimal timestamps) inflates the data size.

The breakthrough insight: **don't compress the text. Decompose the binary TLV directly into typed columns and describe them with SDDL.** This is exactly how the OpenZL genomics codecs work. A DNS-specific preprocessor that separates timestamps, IP addresses, UUIDs, domain names, flags, etc. into homogeneous columnar streams — then lets OpenZL train per-column entropy models — should achieve compression far beyond what Snappy can do on the raw TLV.

## The Approach (Follow the Genomics Pattern)

The OpenZL codebase on the `main` branch of `~/personal/compression` has four working examples of this pattern:

| Format | Preprocessor | SDDL | Technique |
|---|---|---|---|
| FASTA | `tools/biocompress_preprocessor.cpp` | `schemas/fasta_packed.sddl` | 4-bit base packing, header/sequence separation, offset arrays |
| FASTQ | `tools/fastq_preprocess.cpp` | (uses CSV profile) | Dictionary encoding, constant extraction, metadata sidecar |
| BED | `tools/bed_preprocess.cpp` | (uses CSV profile) | Auto-detected column transforms: delta, dictionary, span, drop |
| VCF | `tools/vcf_preprocessing.cpp` | (uses CSV profile) | Header/body split, chunking, columnar layout |

**Your task**: Build the DNS equivalent — `dns_tlv_preprocess.cpp` + `dns_tlv.sddl`.

## What You Need to Understand

### 1. The DNS TLV binary format

Each GenericChunk from Kafka contains a batch of DNS records encoded as TLV:

```
Per record: [4B length][8B startTime][4B flags][2B clientPort][1B inetFamily][TLV fields...]
Per field:  [1B type][2B length][data bytes]
```

TLV field types (from `DnsQuery.java`):

| Type ID | Name | Data Type | Size |
|---|---|---|---|
| 0 | ENDTIME | uint32 | 4 bytes |
| 1 | DNS_MESSAGE | raw bytes | variable |
| 2 | CLIENT_ADDRESS | IPv4/IPv6 | 4 or 16 bytes |
| 3 | SERVER_ADDRESS | IPv4/IPv6 | 4 or 16 bytes |
| 4 | SERVER_PORT | uint16 | 2 bytes |
| 5 | VIEW | string | variable |
| 6 | ZONE | DNS wire-format name | variable |
| 7+ | ... | various | 23+ more types (policy, tags, device-id, UUIDs, etc.) |

### 2. Column characteristics (from our profiling of 50,000 decoded records)

| Field | Cardinality | Compressibility | Suggested Transform |
|---|---|---|---|
| timestamp | 1 per batch | Trivially compressible | Delta from batch timestamp |
| query-uuid | ~unique per record | Low (but 16 raw bytes, not 36 hex) | Raw binary, no transform |
| client-id | ~unique per record | Low | Raw binary or dictionary if pattern detected |
| domain (query-name) | ~4,340 unique in 50K records | Moderate | Dictionary or raw |
| core-domain | same as domain | Moderate | Dictionary or raw |
| node-id | 3 unique | Extremely high | Dictionary (1 byte index) |
| qtype | 10 unique (90% = A) | Extremely high | Dictionary (1 byte) |
| rcode | 5 unique (97% = noerror) | Extremely high | Dictionary (1 byte) |
| flags | 1 unique (always 0) | Perfect | Constant → drop |
| 6 empty columns | always empty | Perfect | Drop entirely |
| count | 7 unique (82% = 3) | Very high | Dictionary (1 byte) |

### 3. The key advantage over text

| Representation | UUID size | Timestamp size | IP size | Overhead per field |
|---|---|---|---|---|
| Text (tab-delimited) | 36 bytes (hex string) | 16 bytes (decimal) | 7-15 bytes (dotted) | 1 byte (tab) |
| Binary TLV | 16 bytes (raw) | 8 bytes (int64) | 4 bytes (raw IPv4) | 3 bytes (type+length) |
| Columnar binary (our target) | 16 bytes (raw) | 8 bytes (or 2-4 with delta) | 4 bytes (raw) | 0 (schema describes layout) |

## Files to Read

### Product and deployment context
- `~/work/nom-config-hub/head/CLAUDE.md` — Product architecture, telemetry pipeline, OpenZL overview
- `~/work/sst-template-migration/head/deployment-and-pipeline-reference.md` — VM inventory, Kafka clusters, topic formats, tools, all environment details
- `~/work/sst-template-migration/head/dns-pipeline-compression-reference.md` — Complete DNS data flow from cache-serve through nom-link to Data Loader, compression at each stage, transform details
- `~/work/sst-template-migration/head/link_info.txt` — How to read nom-link transform objects
- `~/work/sst-template-migration/head/pop_link_pipeline.txt` — POP nom-link transforms
- `~/work/sst-template-migration/head/dc_link_pipeline.txt` — DC nom-link transforms

### OpenZL codebase (main branch)
- `~/personal/compression/` — **Switch to `main` branch** (`git checkout main`)
- `schemas/fasta_packed.sddl` — SDDL schema example (study the syntax)
- `tools/biocompress_preprocessor.cpp` — FASTA binary preprocessor (pattern to follow)
- `tools/fastq_preprocess.cpp` — FASTQ preprocessor with dictionary encoding
- `tools/bed_preprocess.cpp` — BED preprocessor with auto-detected column transforms (delta, dictionary, span, drop)
- `tools/vcf_preprocessing.cpp` — VCF preprocessor
- `nyx/scripts/build.sh` — How to build zli and tools
- `README*.md`, `ANALYSIS.md`, `HANDOFF.md` — Architecture docs (if present)

### DNS TLV format definitions
- `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/queries/DnsQuery.java` — TLV type enum (lines 82-121), all field types and their IDs
- `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/GenericChunk.java` — GenericChunk wire format
- `~/work/link-protobuf/head/Chunk.proto` — ChunkHeader protobuf definition
- `~/work/nomchunk/head/lib/nomchunk/chunk.cc` — Chunk serialization/deserialization in C++

### nom-link transforms (C++)
- `~/work/nom-link/head/bin/server/transform-tovertica.cc` — ToVertica transform (converts TLV → text, shows field rendering)
- `~/work/nom-link/head/bin/server/transform-split.cc` — Split transform
- `~/work/nom-link/head/bin/server/transform-grouper.cc` — Grouper transform
- `~/work/nom-link/head/bin/server/transform-kafka.cc` — Kafka consumer/producer (shows chunk deserialization/serialization)
- `~/work/nom-link/head/schema/transform-kinds.schema` — ToVertica field list (lines 488-569)

### cache-serve DNS output
- `~/work/cacheserve/head/bin/server/query.cc` — DNS query recording (lines 452-520)
- `~/work/multicore/head/lib/statmon/include/statmon/queryinfo.hh` — TLV encoding (lines 362-451)
- `~/work/libnomkafka/head/statmon.cc` — Batching (80 KB buffer, 1-sec flush)
- `~/work/libnomkafka/head/producer.cc` — Kafka Snappy config

### Captured sample data
- **`~/Desktop/openzl-dns/dns-raw.tar.gz`** — **4 GB raw GenericChunk binary** (71,182 messages from POP Kafka `nom-dns-base`). This is your primary input. Extract with `tar -xzf dns-raw.tar.gz` → `dns-raw/` directory with 81 x 50 MB `.bin` files containing length-prefixed GenericChunks.
- `~/Desktop/openzl-dns/dns-capture-base.tar.gz` — 2 GB decoded `nom-dns-base` text (tab-delimited, from `nom-kafka-dump`) — useful for column profiling
- `~/Desktop/openzl-dns/dns-capture-vertica.tar.gz` — 2 GB decoded `nom-dns-vertica` text
- `~/Desktop/openzl-dns/dns-chunk-sizes/nom-dns-vertica_chunk_sizes.csv` — Snappy baseline: avg 34.6 KB per chunk, 200 samples

### Extraction script
- `~/personal/compression/extract_generic_chunks.py` — Reads the length-prefixed `.bin` files from `dns-raw/` and decomposes each GenericChunk into full/header/tlv components
- `~/personal/compression/collect_raw_chunks.py` — The script used to collect the raw data from Kafka (for reference/re-collection)
- `~/personal/compression/DNS_DATA_COLLECTION_README.md` — Full documentation on all collection scripts and procedures

### Other compression investigation docs
- `~/personal/compression/docs/compression-ratio-investigation-handoff.md` — Telemetry compression POC results and lessons
- `~/personal/compression/docs/compression-optimization-agent-prompt.md` — Prior optimization agent prompt
- `~/personal/compression/docs/telemetry-volume-investigation-prompt.md` — Telemetry volume analysis
- `~/personal/compression/DEPLOY.md` — Sidecar deployment guide

## Deliverables

### 1. DNS TLV Preprocessor (`tools/dns_tlv_preprocess.cpp`)

A C++ program that:
1. Reads raw TLV bytes from `.tlv` files extracted by `extract_generic_chunks.py` (or from `combined_tlv_payloads.bin`)
2. Parses each TLV record (fixed header + variable TLV fields)
3. Decomposes into columnar binary streams:
   - Separate array/stream per field type
   - Apply transforms where beneficial (delta on timestamps, dictionary on low-cardinality fields)
   - Store UUIDs as raw 16 bytes, IPs as raw 4/16 bytes
   - Drop constant/empty fields with metadata notation
4. Writes a binary output file in the columnar layout described by the SDDL schema
5. Writes a small metadata sidecar (`.meta`) with dictionaries, constants, field counts
6. Supports lossless round-trip: a corresponding `dns_tlv_reconstruct` mode must reproduce the exact original TLV bytes

**Follow the patterns in `biocompress_preprocessor.cpp` and `bed_preprocess.cpp`.**

### 2. SDDL Schema (`schemas/dns_tlv.sddl`)

Describes the binary layout of the preprocessor output so OpenZL's SDDL profiler can model each column independently.

### 3. Training and benchmark script

A script that:
1. Runs the preprocessor on sample data
2. Trains an OpenZL model with `zli train`
3. Compresses sample data
4. Measures compression ratio
5. Compares against Snappy baseline (35 KB per 5,600-record chunk)
6. Verifies lossless round-trip

### 4. Initial results

Compression ratio numbers on the captured DNS data, compared against:
- Snappy on raw TLV (current pipeline): ~35 KB per 5,600 records
- zstd on tab-delimited text: ~241 KB per 5,600 records (3.9x)
- Your OpenZL on columnar binary: target < 35 KB (must beat Snappy)

## Sample TLV Binary Data (Already Collected)

**4 GB of raw GenericChunk binary data** has been collected from POP Kafka `nom-dns-base`
and is ready for use at `~/Desktop/openzl-dns/dns-raw.tar.gz`.

### How to get the TLV payloads

```bash
# 1. Extract the raw GenericChunk files
cd ~/Desktop/openzl-dns
tar -xzf dns-raw.tar.gz
# Produces: dns-raw/ with 81 x 50 MB .bin files (length-prefixed GenericChunks)

# 2. Decompose into full/header/tlv components
python3 ~/personal/compression/extract_generic_chunks.py \
    --input-dir dns-raw \
    --output-dir dns-extracted

# Produces:
#   dns-extracted/full/      — complete GenericChunk bytes per chunk
#   dns-extracted/headers/   — decoded protobuf headers (human-readable text)
#   dns-extracted/tlv/       — raw TLV payloads (THIS IS YOUR INPUT)
#   dns-extracted/combined_tlv_payloads.bin — all TLV payloads concatenated
#   dns-extracted/manifest.csv — per-chunk sizes, record counts, metadata
```

The `dns-extracted/tlv/chunk_NNNN.tlv` files are the raw TLV binary payloads — each
one contains ~5,600 DNS records. The `combined_tlv_payloads.bin` is all of them
concatenated for bulk training.

### What the raw data contains

- **71,182 GenericChunks** from POP Kafka `nom-dns-base`
- Each chunk: ~55 KB average (GenericChunk envelope + TLV payload)
- Each chunk contains ~5,600 DNS TLV records
- Total: ~400 million DNS records
- Data was collected from all 8 partitions of `nom-dns-base`
- cache-serve sets `compression_flag=0` in the GenericChunk, so TLV payloads are uncompressed
- Kafka-level Snappy compression was transparently decompressed by the collection script

### Additional reference data

You also have decoded text samples for column profiling (useful for understanding
the data without parsing binary):
- `~/Desktop/openzl-dns/dns-capture-base.tar.gz` — 2 GB decoded text from `nom-kafka-dump`
- `~/Desktop/openzl-dns/dns-capture-vertica.tar.gz` — 2 GB decoded text (post-tovertica format)
- See `~/personal/compression/DNS_DATA_COLLECTION_README.md` for full details on all collected data

### To re-collect (if needed)

```bash
# SCP collection script to POP Kafka broker
scp -F <terraform>/ssh.config ~/personal/compression/collect_raw_chunks.py centos@pop-kafka0:/tmp/

# Install dependencies and run
ssh centos@pop-kafka0
sudo pip3 install kafka-python-ng python-snappy
python3 /tmp/collect_raw_chunks.py \
    --brokers 10.0.0.82:9093 \
    --topic nom-dns-base \
    --output-dir /tmp/dns-raw \
    --max-file-mb 50 \
    --max-total-mb 4000

# Compress, SCP to Mac
tar -czf /tmp/dns-raw.tar.gz -C /tmp dns-raw
```

## Constraints

- **Bit-exact reconstruction is non-negotiable.** `preprocess → compress → decompress → reconstruct` must produce byte-identical TLV output.
- **The preprocessor must handle variable-length records.** DNS TLV records have different fields present depending on the query type and policy matches.
- **Performance matters.** At 97 MB/sec raw TLV throughput, the preprocessor must be fast (C++ is the right choice).
- **Follow existing code patterns.** The preprocessor should look like it belongs alongside the genomics preprocessors in `tools/`.

## Workflow Rules

### 1. Plan First
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### 2. Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One tack per subagent for focused execution

### 3. Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### 4. Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### 5. Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### 6. Autonomous Bug Fixing
When given a bug report: just fix it. Don't ask for hand-holding.
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
- **Plan First**: Write plan to tasks/todo.md with checkable items
- **Verify Plan**: Check in before starting implementation
- **Track Progress**: Mark items complete as you go
- **Explain Changes**: High-level summary at each step
- **Document Results**: Add review section to tasks/todo.md
- **Capture Lessons**: Update tasks/lessons.md after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.
- **Don't Assume, Ask**: If you are unsure about something, ask the user. Never assume.
