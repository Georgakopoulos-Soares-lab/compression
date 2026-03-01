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

## NXF2 Packed Binary Format — Nucleotide FASTA (2026-02-28)

### fasta_codec.cpp — NXF2 packed format

- **Packed binary format (NXF2)**: New `encode-packed` / `decode-packed` commands producing self-contained packed binary chunks. Each chunk has a 48-byte `PackedHeader` + 36-byte `RecordMeta` array + 7 concatenated payload buffers (headers, nmask, acgtmask, bases2, exceptions, case, wrapping).
- **Stream-presence flags**: `SFLAG_NO_N` (0x01), `SFLAG_NO_IUPAC` (0x02), `SFLAG_NO_CASE` (0x04) in `stream_flags` field. When a stream is entirely absent (e.g., no N characters in any sequence), the corresponding payload buffer is omitted entirely from the binary — both encoder and decoder respect these flags. Saves space and training time.
- **Multi-chunk splitting**: `encode-packed` accepts a `num_chunks` parameter, splits records across multiple `chunk_NNNNNN.bin` files. Required because `zli` cannot compress files >500 MiB.

### codec_common.h — stream-presence flags

- Added `SFLAG_NO_N`, `SFLAG_NO_IUPAC`, `SFLAG_NO_CASE` constants.
- `decode_sequence()` updated to handle absent N-mask, ACGT-mask, and case streams gracefully — treats absent N-mask as "no N's", absent ACGT-mask as "all standard bases", absent case as "all uppercase".

### compress_lossless.py — NXF2 packed pipeline + group training

- **New FASTA path**: `_compress_fasta_packed()` — encodes FASTA into NXF2 packed binary chunks, trains a single SDDL compressor (`nucleotide_fasta.zl_compressor`), compresses all chunks with it, bundles into `.zlfasta` container. Replaces old per-stream encode → compress-each-stream approach for FASTA.
- **Group training**: `--group-train-dir` option to train across multiple FASTA files (samples first chunk from each), producing a universal compressor.
- **Auto-chunking**: Automatically splits large files so no NXF2 chunk exceeds 500 MiB (with safety re-encode if first attempt exceeds limit).

### decompress_lossless.py — packed format support

- **Packed format detection**: `_is_packed_format()` checks for `chunk_*.bin` entry names to route packed vs legacy decompression.
- **Parallel chunk decompression**: All packed chunks decompressed in parallel via `ThreadPoolExecutor`.

### New SDDL schemas

- `nucleotide_fasta.sddl`: Describes the NXF2 packed binary format for OpenZL SDDL training. Defines header, per-record metadata array, and 7 payload buffers with sizes derived from header fields.

### New trained compressors

- `nucleotide_fasta.zl_compressor` (8,384 bytes): Trained SDDL compressor for NXF2 nucleotide FASTA packed binary.

### Old per-stream compressors removed

- Deleted `bases2.bin.zl_compressor`, `case.bin.zl_compressor`, `headers.bin.zl_compressor`, `meta.bin.zl_compressor`, `wrapping.bin.zl_compressor` from `models/lossless/`. These were from the pre-NXF2 per-stream FASTA pipeline and are no longer referenced anywhere.

---

## Protein FASTA Support (2026-02-28)

### fasta_codec.cpp — NXFP protein format

- **Protein packed binary (NXFP)**: New `encode-protein-packed` / `decode-protein-packed` commands. 32-byte `ProteinPackedHeader` + 20-byte `ProteinRecordMeta` array + 4 payload buffers (headers, seq, case, wrapping). Amino acid sequences stored as raw uppercase bytes (1 byte per AA) — no bit-packing, lets OpenZL learn the frequency distribution.
- **Simpler than nucleotide**: No N-mask, ACGT-mask, 2-bit bases, or IUPAC exceptions. Full AA alphabet including X, *, -, B, Z, J, U, O preserved.
- **Reuses existing infrastructure**: `parse_batch()`, `encode_wrapping()`/`decode_wrapping()`, case encoding (CASE_NONE/MASK/SPARSE), `MappedFile`, `SFLAG_NO_CASE`.

### detect.py — protein auto-detection

- `detect_fasta_subtype()`: Reads first 10KB of sequence lines. If any character is in {E, F, I, L, P, Q} (amino acids NOT valid IUPAC nucleotide codes), returns `"protein"`. Otherwise `"nucleotide"`. Standard bioinformatics heuristic.

### codec.py — protein wrappers

- `encode_protein_packed()` and `decode_protein_packed()`: Python subprocess wrappers for the new C++ commands.

### compress_lossless.py — protein pipeline

- **`--type protein`**: Explicit type override for protein FASTA files.
- **Auto-detection**: When `--type auto` and file detected as FASTA, calls `detect_fasta_subtype()` to distinguish nucleotide vs protein.
- `_compress_protein_packed()`: Mirrors `_compress_fasta_packed()` but uses `encode-protein-packed`, `protein_fasta.sddl` schema, and `protein_fasta.zl_compressor`.

### decompress_lossless.py — protein decompression

- `_detect_packed_subtype()`: Reads first 4 bytes (magic) of decompressed chunk — "NXFP" → protein, "NXF2" → nucleotide. Routes to `codec.decode_protein_packed()` for protein.

### New SDDL schemas

- `protein_fasta.sddl`: Describes the NXFP packed binary format. Defines header, per-record metadata array, and 4 payload buffers.

### New trained compressors

- `protein_fasta.zl_compressor` (6,492 bytes): Trained SDDL compressor for NXFP protein FASTA packed binary.

### New test fixtures

- `protein_basic.fasta`: 5 protein records (hemoglobin, insulin, titin, SARS-CoV-2 spike).
- `protein_extended.fasta`: 10 edge case records (extended chars B/Z/J/U/O/X, stops, gaps, single AA, all lowercase, unusual wrapping, empty sequence).

### Tests

- 21 new protein tests across 4 tiers:
  - **Tier 5**: `TestProteinCodecRoundTrip` — single-chunk, multi-chunk, NXFP binary structure validation.
  - **Tier 5b**: `TestProteinEdgeCases` — single AA, all lowercase, all uppercase, stops, gaps, X chars, empty sequence, no trailing newline, multi-record.
  - **Tier 6**: `TestFastaSubtypeDetection` — verifies nucleotide fixtures → "nucleotide", protein fixtures → "protein".
  - **Tier 7**: `TestProteinFullPipeline` — full compress → decompress with `--type protein` and auto-detect.
- All 180 tests passing (111 FASTA + 69 FASTQ).

---

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
