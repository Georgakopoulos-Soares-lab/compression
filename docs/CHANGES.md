# Changes

Write your notes about recent changes here. Be as brief or detailed as you want — the `/update-context` skill will process these into structured entries.

When you run `/update-context`, these notes will be:
1. Read and processed into structured changelog entries
2. Appended to `docs/PROJECT_HISTORY.md`
3. This file will be cleared so it's ready for your next round of notes

## Format

No required format. Just write what you did. Examples:

- "Added gzip baseline comparison to benchmarks"
- "Refactored the compression pipeline to support streaming input. Had to change how OpenZL buffers are allocated because the old approach loaded the entire file into memory."
- "Fixed bug where FASTA files with multiple sequences weren't being handled correctly"

---

<!-- Write your changes below this line -->

## FASTQ Lossless Pipeline v3 (2026-02-27)

### fastq_codec.cpp — v3 format rewrite

- **Illumina header parsing**: Auto-detects Illumina-format headers (`@PREFIX.READNUM INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y`). Dictionary-encodes low-cardinality fields (run, flowcell, lane, tile) as u8 indices. Drops sequential read numbers (reconstructed 1..N on decode). ~4.4x header size reduction before compression.
- **`@` stripping**: Like `>` for FASTA, the `@` prefix is no longer stored — it's prepended during decode (v3 only).
- **LCP fallback**: Non-Illumina headers automatically fall back to LCP prefix stripping (existing v2 behavior). No data loss.
- **Atomic work-stealing threading**: Replaced fixed-division batch threading with `std::atomic<u32>` work-stealing for balanced load.
- **Backward compatibility**: Decoder supports v1, v2, and v3 meta formats.
- **v3 meta header**: New `header_mode` byte at offset [15] (0=LCP, 1=Illumina). Illumina mode stores dictionary block + per-record compact binary.

### compress_lossless_fastq.py — training + parallel compression

- **Training sample**: 10 MiB → 200 MiB (configurable via `--train-sample-mib`).
- **Training timeout**: 1800s default (configurable via `--max-time-secs`).
- **New CLI options**: `--train-sample-mib`, `--max-time-secs`, `--train-threads`, `--compress-jobs`.
- **Parallel compression**: Serial stream compression loop replaced with `concurrent.futures.ThreadPoolExecutor`.

### decompress_lossless_fastq.py — parallel decompression

- **Parallel decompression**: Serial decompression loop replaced with `concurrent.futures.ThreadPoolExecutor`.
- Added `_decompress_one` helper with meta.bin backward-compat fallback.

### Tests

- Added `illumina_reads.fastq` fixture (10 records with proper 7-field Illumina headers).
- Updated `validate_fastq_streams()` for v3 format: handles `header_mode`, Illumina block size, version 3.
- All 65 FASTQ tests pass (up from 60). All 60 FASTA tests still pass.
