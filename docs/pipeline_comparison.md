# Side-by-Side Implementation Comparison: Current (Root) vs. main_branch

> Generated: 2026-02-26. Compares the FASTA and FASTQ lossless compression pipelines
> between the current `nyx/` implementation and the `main_branch/` snapshot.

---

## 0. Structural Overview

| Aspect | Current (root/nyx/) | main_branch/ |
|--------|---------------------|-------------|
| Architecture | Python CLI (`click`) + C++ codecs | Shell scripts + standalone C++ preprocessors |
| FASTA codec | `fasta_codec.cpp` (lossless stream separation) | `biocompress_preprocessor.cpp` (4-bit packed binary) |
| FASTQ codec | `fastq_codec.cpp` (lossless stream separation) | `fastq_preprocess.cpp` (TSV + metadata sidecar) |
| OpenZL profile | `serial` for all streams | `sddl` for FASTA, `csv` with tab delimiter for FASTQ |
| Container | `.zlfasta`/`.zlfastq` (custom binary with CRC32) | Loose `.zl` files (no unified container) |
| Installable | `pip install -e .` → `nyx` CLI | Manual shell scripts |

---

## A. FASTA Comparison Table

| Component | Current (root) | main_branch/ | Difference | Likely Impact | Risk |
|-----------|---------------|-------------|------------|---------------|------|
| **Parsing** | Detect `>`, read header+multiline seq, preserve line lengths | Detect `>`, read header+multiline seq, strip `>` from header | Root preserves `>` in header stream; main strips it | Negligible — `>` is 1 byte/record | None |
| **Base encoding** | 2-bit packing (A=0,C=1,G=2,T=3) for ACGT only; separate N-mask + ACGT-mask + exceptions | 4-bit packing (A=0,C=1,G=2,T=3,N=4) for all bases including N; unknowns→N | Root separates N/non-ACGT into typed masks; main packs everything into 4 bits | **Root: better ratio** — 2-bit bases compress extremely well; N-mask is highly compressible sparse data. Main wastes 2 extra bits per base. | None |
| **Case preservation** | CASE_NONE/MASK/SPARSE modes; bit-packed or varint | **Lost** — case-insensitive mapping, always outputs uppercase | Root is lossless; **main is lossy for case** | Root: lossless. Main: not byte-identical if input has lowercase. | **main_branch has correctness bug for soft-masked genomes** |
| **Wrapping preservation** | WRAP_COMPACT (9 bytes) or WRAP_EXPLICIT (per-line lengths); byte-exact reconstruction | **Lost** — newlines stripped; no line-width metadata stored | Root is lossless; **main loses original line wrapping** | Root: byte-exact. Main: cannot reconstruct original FASTA formatting. | **main_branch is not lossless** |
| **IUPAC handling** | Full support: N-mask + ACGT-mask + exceptions stream captures R,Y,S,W,K,M,B,D,H,V | All non-ACGTN mapped to N (value 4) | Root preserves all IUPAC codes; **main destroys them** | Root: correct. Main: data loss on IUPAC sequences. | **main_branch has correctness bug** |
| **Newline detection** | Auto-detect LF/CRLF; stored in meta.bin; trailing newline flag | Not handled; no metadata | Root handles CRLF and trailing NL | Root: universal. Main: assumes Unix LF. | **main_branch may fail on Windows FASTA** |
| **OpenZL profile** | `serial` — generic byte-stream compression per stream | `sddl` with `fasta_packed.sddl` schema — format-aware compression | **main uses schema-aware compression** which exploits field boundaries | **main: potentially better ratio** — SDDL lets OpenZL exploit field boundaries (offsets vs headers vs sequences) | Low risk |
| **Training** | Per-stream: 10 MiB sample, skip if ratio > 100×, `--no-ace-successors` | Whole-file: 200 MiB sample, 30-min time limit, `--no-ace-successors`, SDDL profile | main trains on more data with schema awareness | **main: better trained models** — more data + schema = better statistical models | Low risk |
| **Chunking (preprocess)** | 500K records per batch; no file-level chunking in codec (chunking happens post-encoding at stream level) | 450 MiB chunks, snapped to record boundaries, parallel chunk processing | main chunks during preprocessing for parallelism; root chunks post-encoding for OpenZL | **main: faster preprocessing** on large files via parallel chunk encoding | Root's approach is simpler but serial |
| **Chunking (compression)** | 400 MiB max per stream chunk; naive byte-boundary split | No re-chunking — SDDL-aware chunks already sized | Both respect OpenZL size limits | Equivalent | Root splits mid-stream (not record-aware) — minor risk |
| **Streams produced** | 7 typed streams: headers, nmask, acgtmask, bases2, exceptions, case, wrapping + meta.bin | 1 packed binary file per chunk: `chunk_NNNNN.fasta_packed.bin` | Root: many small specialized streams. Main: single binary per chunk. | **Trade-off**: Root's per-stream approach allows per-type compression tuning; main's single-file approach enables SDDL schema optimization | Design choice |
| **Container** | `.zlfasta` binary: magic + directory + data + CRC32 | Loose `.zl` files — no container | Root: self-contained, verifiable. Main: script-managed files. | Root: better UX, integrity checking | main_branch lacks portability |
| **I/O** | mmap input, FILE* stream writes, batch memory cleared | mmap input, ofstream output, vector buffers, parallel atomic counter | Both use mmap; main has more parallelism in preprocessing | **main: faster I/O** for large files | None |
| **Parallelism** | C++: per-batch threading. Python: serial stream-by-stream compression | C++: atomic work-stealing thread pool. Shell: `xargs -P` for parallel `zli` jobs | main has better parallelism model | **main: faster wall-clock** | None |

### FASTA — Key Struct Formats

**Current root — RecordMeta (36 bytes, `#pragma pack(push, 1)`):**
```
Offset  Type  Field
0-3     u32   seq_len        total sequence length (chars, no line breaks)
4-7     u32   header_len     bytes including '>'
8-11    u32   nmask_bytes    bit-packed N-mask byte size
12-15   u32   acgtmask_bytes bit-packed ACGT-mask byte size
16-19   u32   bases2_bytes   2-bit packed bases byte size
20-23   u32   exceptions_bytes
24-27   u32   case_bytes
28-31   u32   wrap_bytes
32      u8    case_mode      0=CASE_NONE, 1=CASE_MASK, 2=CASE_SPARSE
33-35   u8[3] pad
```

**Current root — meta.bin header:**
```
[0-3]   u32   magic = 0x4346584E ("NXFC")
[4-7]   u32   version = 1
[8-11]  u32   num_records (placeholder, written at end)
[12]    u8    newline_style (NL_LF=0, NL_CRLF=1)
[13]    u8    has_trailing_nl
[14-19] u8[6] reserved
[20+]   RecordMeta * num_records
```

**main_branch — FAV4 binary format (per chunk file):**
```
[0-3]   char[4]   magic "FAV4"
[4-7]   u32       num_records
[8+]    u32[N+1]  hdr_offsets    (prefix sum of header sizes)
[...]   u32[N+1]  seq_offsets    (prefix sum of sequence sizes)
[...]   u32[N]    seq_lengths    (original base count per record)
[...]   u32       hdr_total
[...]   u32       seq_total
[...]   u32       hdr_pad
[...]   u32       seq_pad
[...]   u8[]      headers payload  (padded to 4-byte boundary)
[...]   u8[]      sequences payload (4-bit packed, padded)
```

**main_branch — base nibble values:**
```
A/a → 0,  C/c → 1,  G/g → 2,  T/t → 3,  N/n → 4,  unknown → 4
```

---

## B. FASTQ Comparison Table

| Component | Current (root) | main_branch/ | Difference | Likely Impact | Risk |
|-----------|---------------|-------------|------------|---------------|------|
| **Header parsing** | LCP prefix stripping across batch; suffix stored per-record | Full Illumina header decomposition: prefix, read_num, instrument, run, flowcell, lane, tile, X, Y → dictionaries + IDs | **main does deep header structuring**; root does simple prefix dedup | **main: much better header ratio** — dictionary-based encoding replaces repeated strings with small integer IDs | main assumes Illumina format; root is format-agnostic |
| **Header fallback** | Always works (raw headers stored) | Falls back to raw header if non-Illumina (`all_parsed=false`) | Both have fallback; main's is explicit via flags | Equivalent robustness | None |
| **Sequence encoding** | 2-bit ACGT + N-mask + ACGT-mask + exceptions + case | **Plain text** — sequences stored as-is in TSV columns | **Root: binary stream separation. Main: text columns** | **Root: better sequence ratio** — 2-bit packing + typed masks far more compact than ASCII text | None |
| **Quality encoding** | 4 modes: per-record (row-major), columnar, per-position-delta, per-position-raw | **Plain text** — quality scores stored as-is in TSV columns | **Root: specialized quality layouts. Main: text** | **Root: better quality ratio** for fixed-length reads (per-position layout exploits column correlation) | Root's per-position can produce 1000 files |
| **Quality layout selection** | Heuristic: if uniform length ≤ 1000 → per-position-raw; else per-record | None — always text | Root adapts; main doesn't | **Root: dataset-dependent gains** | Root's heuristic only checks first batch |
| **Plus line handling** | Stored as stream (`plus.bin`) — preserves comments after `+` | `+` line reconstructed as constant `"+\n"` | Root preserves `+` comments; **main discards them** | Root: lossless. **main may lose `+` comment data** | **main_branch lossy if plus has comment** |
| **Wrapping** | `seq_wrap.bin` + `qual_wrap.bin` per-record wrapping metadata | Not applicable (TSV is single-line) | Root preserves multiline wrapping; main assumes single-line records | Root: lossless. **Main cannot reconstruct wrapped FASTQ** | **main_branch lossy for multiline FASTQ** |
| **Case preservation** | CASE_NONE/MASK/SPARSE — full case preservation | Text passthrough — case preserved in TSV | Both preserve case (root explicitly, main implicitly via text) | Equivalent | None |
| **IUPAC handling** | Full IUPAC via exceptions stream | Text passthrough — IUPAC preserved in TSV | Both handle IUPAC | Equivalent | None |
| **Newline handling** | LF/CRLF detection + trailing newline flag in meta | Assumes 4-line records with LF; strips `\r` | Root: universal. Main: may corrupt CRLF files | **main_branch may fail on Windows FASTQ** | Moderate |
| **OpenZL profile** | `serial` — generic per-stream | `csv` with tab delimiter — column-aware | **main uses column-aware CSV profiler** | **main: potentially better ratio** for header columns — CSV profiler can exploit inter-column correlations | Low risk |
| **Training** | Per-stream: 10 MiB sample, universal quality compressor for all position files | 8 chunks (~43 MiB total), CSV profile, 64 threads | Different strategies; main trains on text, root on binary streams | Trade-off: root's per-stream training is more targeted; main's CSV training sees all columns together | Neither clearly better |
| **Output format** | 11+ binary `.bin` streams → `.zlfastq` container | `.meta` (4 KB binary) + `.tsv` (~380 MB text) → loose `.zl` files | Root: typed binary streams + container. Main: TSV text + sidecar + loose files | Root: more compact intermediate. Main: human-readable TSV | None |
| **Compression ratio** | Not benchmarked in repo | **8.45×** on 500 MB sample (documented) | main has published benchmarks | Cannot compare directly without running both | Need testing |
| **Container** | `.zlfastq`: magic + directory + CRC32 | No container — loose files | Root: self-contained, checksummed | Root: better UX/integrity | None |
| **Parallelism** | C++ batch-level threading + serial Python compression | C++ parallel newline detection + parallel TSV build + `xargs` parallel compression | Both have C++ threading; main has better end-to-end parallelism | **main: faster wall-clock** | None |
| **Read number encoding** | Stored verbatim in header suffix | Reconstructed as sequential 1..N (if detected as sequential) | **main drops read numbers when sequential** — saves ~4-8 bytes/header | **main: better header ratio** when read numbers are sequential | Assumes sequential numbering; fails silently if not |

### FASTQ — Key Struct Formats

**Current root — FastqRecordMeta (48 bytes, `#pragma pack(push, 1)`):**
```
Offset  Type  Field
0-3     u32   seq_len
4-7     u32   header_len     v2: suffix length (prefix stripped)
8-11    u32   plus_len
12-15   u32   nmask_bytes
16-19   u32   acgtmask_bytes
20-23   u32   bases2_bytes
24-27   u32   exceptions_bytes
28-31   u32   case_bytes
32-35   u32   seq_wrap_bytes
36-39   u32   qual_wrap_bytes
40-43   u32   quality_bytes  (= seq_len)
44      u8    case_mode      0=NONE, 1=MASK, 2=SPARSE
45      u8    quality_mode   v2: 0=RAW, v1: 1=DELTA
46-47   u8[2] pad
```

**Current root — meta.bin header (v2):**
```
[0-3]   u32    magic = 0x4E584651 ("NXFQ")
[4-7]   u32    version = 2
[8-11]  u32    num_records (placeholder)
[12]    u8     newline_style
[13]    u8     has_trailing_nl
[14]    u8     quality_layout  0=per-record, 1=columnar, 2=per-pos-delta, 3=per-pos-raw
[15]    u8     reserved
[16-19] u32    fixed_seq_len (0 if variable)
[20-23] u32    prefix_len
[24+]   u8[]   prefix bytes
[24+P+] FastqRecordMeta * num_records
```

**Current root — quality layout selection (fastq_codec.cpp:292-294):**
```c
u32 fixed_seq_len = check_uniform_seq_len(first_batch);
u8 quality_layout = (fixed_seq_len > 0 && fixed_seq_len <= MAX_COLUMNAR_SEQ_LEN)
                    ? QLAYOUT_PER_POS_RAW : QLAYOUT_PER_RECORD;
// MAX_COLUMNAR_SEQ_LEN = 1000
```

**main_branch — .meta binary format:**
```
[0-5]   char[6]   magic "FQPP01"
[6-9]   i32       num_records
[10]    u8        flags
                    bit 0 (0x01): all_parsed
                    bit 1 (0x02): prefix_constant
                    bit 2 (0x04): instrument_constant
                    bit 3 (0x08): read_num_sequential
[11+]   (variable) constant_prefix   if flags & 0x02: i16 len + bytes
[...]   (variable) constant_instr    if flags & 0x04: i16 len + bytes
[...]   (variable) dictionaries      if flags & 0x01:
                    i32 run_dict_size + [i16 len + bytes] * N
                    i32 fc_dict_size  + [i16 len + bytes] * N
                    i32 lane_dict_size + [i16 len + bytes] * N
                    i32 tile_dict_size + [i16 len + bytes] * N
[...]   u32       trailer = 0xDEADBEEF
```

**main_branch — TSV column layout (mode B, all_parsed=true):**
```
run_id \t fc_id \t lane_id \t tile_id \t x \t y \t sequence \t quality \n
```

---

## C. "Why It's Faster / Better Ratio" Hypothesis List

### Hypotheses for main_branch Advantages

**H1: SDDL profile for FASTA**
- **Why better ratio**: The SDDL profile tells OpenZL exactly where field boundaries are (offsets vs headers vs sequences). OpenZL applies different compression strategies to each field type rather than treating the whole stream as generic bytes.
- **Universal or dataset-dependent**: Universal — any FAV4 binary benefits from schema awareness.
- **New failure modes**: SDDL schema must match binary layout exactly; schema drift = silent corruption or decompression failure.

**H2: CSV profile for FASTQ**
- **Why better ratio**: The CSV/tab profiler decomposes the TSV into per-column streams internally, allowing column-specific entropy coding. Headers decomposed into small integer IDs (run=0, flowcell=1) compress dramatically better than repeated string prefixes.
- **Universal or dataset-dependent**: Best for Illumina-format headers (most FASTQ data). Less effective for non-standard headers.
- **New failure modes**: Requires parseable Illumina headers; falls back to raw header mode otherwise.

**H3: Header dictionary encoding (main_branch FASTQ)**
- **Why better ratio**: Replaces repeated metadata strings (instrument ID, flowcell, lane, tile) with small integer indices. For a typical Illumina run with 1 flowcell, 4 lanes, 100 tiles, each header is reduced from ~80 bytes to ~20 bytes of small integers.
- **Universal or dataset-dependent**: **Highly dataset-dependent** — only works for Illumina format. Non-Illumina headers get no benefit (fallback mode).
- **New failure modes**: If header format changes (new Illumina format, non-standard instruments), parsing fails and reverts to raw headers.

**H4: Sequential read number elimination (main_branch FASTQ)**
- **Why better ratio**: If read numbers are 1, 2, 3, ..., N, they're reconstructed from the record index rather than stored. Saves ~4-8 bytes per record × millions of records.
- **Universal or dataset-dependent**: Dataset-dependent — only applies when read numbers are sequential.
- **New failure modes**: If read numbers are not sequential but detection is wrong, data is silently corrupted.

**H5: Parallel preprocessing with work-stealing (main_branch FASTA)**
- **Why faster**: Atomic work-stealing (`next_chunk.fetch_add(1)`) distributes chunks to threads dynamically, avoiding load imbalance from variable-size chunks.
- **Universal or dataset-dependent**: Universal for large files; no benefit for small files.
- **New failure modes**: None — pure performance optimization.

### Hypotheses for Current (Root) Advantages

**H6: 2-bit packing + typed masks (root, both FASTA and FASTQ)**
- **Why better ratio**: 2-bit bases (4 symbols in 2 bits) are fundamentally more efficient than 4-bit (5 symbols in 4 bits). The N-mask and ACGT-mask are highly compressible (sparse bit vectors). Exceptions are rare in practice.
- **Universal or dataset-dependent**: Universal — always more compact than 4-bit or text.
- **New failure modes**: More complex codec; more code paths to get right.

**H7: Per-position quality layout (root FASTQ)**
- **Why better ratio**: Quality scores at the same position across reads are highly correlated (base-calling quality is position-dependent). Columnar layout groups correlated values together, enabling much better entropy coding.
- **Universal or dataset-dependent**: **Highly effective for fixed-length Illumina reads** (which dominate FASTQ data). No benefit for variable-length reads.
- **New failure modes**: Produces up to 1000 output files; file-system overhead; potential FD limits.

**H8: Full lossless preservation (root)**
- **Why correct**: Preserves case, wrapping, IUPAC codes, CRLF, trailing newlines, plus-line comments.
- main_branch FASTA loses: case, wrapping, IUPAC codes.
- main_branch FASTQ loses: wrapping, plus-line comments.
- **Not a ratio advantage** — a correctness requirement for lossless compression.

---

## D. Recommended Merge Plan

### MUST TAKE from main_branch (high gain, low risk)

**1. SDDL profile for meta.bin (FASTA)**
- After encoding streams with root's codec, use `sddl` profile for `meta.bin` instead of `serial`.
- Schema-aware compression of structured metadata is strictly better.
- **Files**: `nyx/nyx/commands/compress_lossless.py` — change `profile="serial"` to `profile="sddl"` for `meta.bin`; write a small SDDL schema for the `RecordMeta` array structure.
- **Risk**: Low — only affects `meta.bin`; other streams stay `serial`.

**2. Parallel preprocessing with work-stealing thread pool**
- Replace root's batch-level threading with atomic work-stealing across chunks.
- Better load balancing, especially for large files with uneven chunk sizes.
- **Files**: `nyx/tools/fasta_codec.cpp`, `nyx/tools/fastq_codec.cpp` — refactor threading model.
- **Risk**: Low — pure performance; doesn't change output format.

**3. Header dictionary encoding for FASTQ**
- Add Illumina header decomposition to `fastq_codec.cpp` (in addition to existing LCP prefix stripping).
- Dictionary-based encoding of flowcell/lane/tile/instrument dramatically reduces header stream size.
- **Files**: `nyx/tools/fastq_codec.cpp` — add header field parsing + dictionary building; `nyx/tools/codec_common.h` — add dictionary I/O helpers; new streams embedded in `meta.bin` or as separate `*_dict.bin` files.
- **Risk**: Low — add as optional v3 feature with fallback to current LCP mode for non-Illumina headers.

### TAKE WITH MODIFICATIONS (good idea, needs guardrails)

**4. CSV profile for FASTQ quality/sequence columns**
- For per-position quality files, consider SDDL schemas instead of `serial` (describe each file as an array of `u8`).
- Cannot directly use CSV profile on binary streams — would need text reformatting or SDDL.
- **Files**: `nyx/nyx/commands/compress_lossless_fastq.py` — update profile selection logic per stream type.
- **Risk**: Medium — CSV profile expects text; binary streams need SDDL or serial.

**5. Sequential read number elimination**
- Detect sequential read numbers and reconstruct from index during decode.
- Must be opt-in with explicit verification on decode (compare expected vs actual).
- **Files**: `nyx/tools/fastq_codec.cpp` — add sequential detection near `compute_lcp()` area; update meta.bin v3 header with `read_num_sequential` flag.
- **Risk**: Medium — silent corruption if detection logic is wrong; add explicit validation on decode.

**6. Larger training samples**
- Increase `_MAX_TRAIN_SAMPLE_BYTES` from 10 MiB to 50–200 MiB for high-entropy streams (bases2, quality).
- Make configurable per-stream (small streams like `case.bin` don't need large samples).
- **Files**: `nyx/nyx/commands/compress_lossless.py`, `nyx/nyx/commands/compress_lossless_fastq.py` — increase constant or make it per-stream dict.
- **Risk**: Low — only affects training time, not correctness.

### DO NOT TAKE (risk > reward)

**7. 4-bit base packing (main_branch FASTA)**
- Root's 2-bit packing + typed masks is fundamentally more efficient (2 bits/base vs 4 bits/base).
- Moving to 4-bit would **regress compression ratio** for sequences.
- Exception: revisit only if a future SDDL schema could exploit 4-bit structure better than serial compresses 2-bit streams.

**8. TSV intermediate format (main_branch FASTQ)**
- Text TSV is ~380 MB for a 530 MB FASTQ — almost no size reduction before compression.
- Root's binary streams are much more compact intermediates (~40–60% of original).
- Exception: offer a `--debug-tsv` flag for human-readable debugging only.

**9. Lossy behavior (main_branch's case/wrapping/IUPAC losses)**
- The pipeline is advertised as lossless. main_branch silently drops case information, line wrapping, IUPAC codes, and plus-line comments.
- These are correctness bugs, not design choices.
- If a "lossy-fast" mode is desired, implement it as an explicit `--lossy` flag, not as default behavior.

**10. No container format (main_branch loose files)**
- Root's `.zlfasta`/`.zlfastq` containers with CRC32 are strictly better for distribution, integrity, and UX.
- Loose `.zl` files require manual management and offer no integrity checking.

---

## E. Priority Implementation Order

| Priority | Change | Effort | Ratio Impact | Speed Impact |
|----------|--------|--------|-------------|-------------|
| 1 | Illumina header dictionary encoding (FASTQ) | Medium | **High** — 30–50% header reduction | Neutral |
| 2 | SDDL profile for meta.bin (FASTA/FASTQ) | Low | **Medium** — free schema-aware gain | Neutral |
| 3 | Larger training samples | Trivial | **Medium** — better model quality | Slower training |
| 4 | Work-stealing thread pool (C++) | Medium | None | **High** for large files |
| 5 | Sequential read number detection | Medium | **Low-Medium** | Neutral |

---

## F. Correctness Risk Summary

| Bug | Affected Pipeline | Severity |
|-----|------------------|----------|
| Case information lost | main_branch FASTA | **Critical** — soft-masked genomes corrupted |
| IUPAC codes→N | main_branch FASTA | **Critical** — non-ACGTN bases silently changed |
| Line wrapping lost | main_branch FASTA + FASTQ | **High** — output is functionally equivalent but not byte-identical |
| Plus-line comments lost | main_branch FASTQ | **Medium** — rarely used but specified by FASTQ format |
| CRLF handling missing | main_branch FASTA + FASTQ | **Medium** — Windows files may corrupt |
| Sequential read num assumption | main_branch FASTQ | **Low** — only triggers if detection is wrong |

---

*Files referenced in this document:*
- [`nyx/nyx/commands/compress_lossless.py`](nyx/nyx/commands/compress_lossless.py)
- [`nyx/nyx/commands/compress_lossless_fastq.py`](nyx/nyx/commands/compress_lossless_fastq.py)
- [`nyx/nyx/commands/decompress_lossless.py`](nyx/nyx/commands/decompress_lossless.py)
- [`nyx/nyx/commands/decompress_lossless_fastq.py`](nyx/nyx/commands/decompress_lossless_fastq.py)
- [`nyx/tools/fasta_codec.cpp`](nyx/tools/fasta_codec.cpp)
- [`nyx/tools/fastq_codec.cpp`](nyx/tools/fastq_codec.cpp)
- [`nyx/tools/codec_common.h`](nyx/tools/codec_common.h)
- [`nyx/nyx/core/zlfasta.py`](nyx/nyx/core/zlfasta.py)
- [`nyx/nyx/core/zlfastq.py`](nyx/nyx/core/zlfastq.py)
- [`main_branch/tools/biocompress_preprocessor.cpp`](main_branch/tools/biocompress_preprocessor.cpp)
- [`main_branch/tools/fastq_preprocess.cpp`](main_branch/tools/fastq_preprocess.cpp)
- [`main_branch/schemas/fasta_packed.sddl`](main_branch/schemas/fasta_packed.sddl)
