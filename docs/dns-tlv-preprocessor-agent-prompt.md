# Agent Prompt: DNS TLV Preprocessor & SDDL Schema

## Your Task

Build a C++ preprocessor that decomposes DNS TLV binary records into typed columnar
streams, write an SDDL schema describing those streams, train an initial OpenZL
compressor, and benchmark it against Snappy (the current compression in the pipeline).

**Target**: Compress ~5,600 DNS records into fewer than 35 KB (what Snappy currently
achieves on the raw TLV binary). If we beat that, we have a viable path to reducing
8.4 TB/day of DNS data in production.

## Before You Start

1. Read the handoff document at `~/personal/compression/docs/dns-tlv-preprocessor-handoff.md` —
   it has the complete context, all file paths, data format details, column profiling
   results, and the baseline numbers to beat.

2. Read `~/work/sst-template-migration/head/dns-pipeline-compression-reference.md` —
   this explains the full DNS data pipeline, how compression works at each stage,
   and what each nom-link transform does to the data.

3. Switch to the `main` branch of `~/personal/compression/` and study the existing
   genomics preprocessors — especially `tools/biocompress_preprocessor.cpp` (FASTA binary),
   `tools/bed_preprocess.cpp` (auto-detected column transforms), and `schemas/fasta_packed.sddl`
   (SDDL schema syntax). Your DNS preprocessor should follow these patterns.

4. Read the TLV format definition at
   `~/work/java-common/head/link-store/src/main/java/com/nominum/common/link/store/queries/DnsQuery.java`
   (lines 82-121) to understand all field types and their binary representation.

## Steps

1. **Plan**: Enter plan mode. Write a detailed design for the preprocessor, SDDL schema,
   and reconstruction logic. Check in with the user before implementing.

2. **Build the preprocessor** (`tools/dns_tlv_preprocess.cpp`):
   - Parse TLV records → decompose into columnar binary streams
   - Apply domain-specific transforms (delta on timestamps, dictionary on low-cardinality fields)
   - Write columnar binary output described by the SDDL schema
   - Write metadata sidecar for dictionaries and constants
   - Include reconstruction mode for lossless round-trip

3. **Write the SDDL schema** (`schemas/dns_tlv.sddl`):
   - Describe the columnar binary layout so OpenZL models each column independently

4. **Extract the raw TLV data** (already collected):
   ```bash
   cd ~/Desktop/openzl-dns
   tar -xzf dns-raw.tar.gz
   python3 ~/personal/compression/extract_generic_chunks.py \
       --input-dir dns-raw --output-dir dns-extracted
   ```
   This gives you `dns-extracted/tlv/` with individual TLV files and
   `dns-extracted/combined_tlv_payloads.bin` for bulk operations.

5. **Train and benchmark**:
   - Run preprocessor on extracted TLV data → train with `zli train` → compress → measure ratio
   - Compare against Snappy baseline (35 KB per 5,600 records)
   - Verify lossless round-trip

5. **Document results** in tasks/todo.md with compression numbers

## Constraints

- Bit-exact reconstruction is non-negotiable
- Performance matters (97 MB/sec throughput in production)
- Follow existing code patterns from the genomics preprocessors
- Don't assume — ask the user if unsure about anything

## After This Task

Once you have initial results and a working preprocessor, this gets handed to the
Ralph Loop agent for iterative optimization of the SDDL schema and column transforms.
Your job is to provide a solid, working starting point — not the final optimized version.
