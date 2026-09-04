# NYX FASTQ Compressor

**Scope:** the NYX FASTQ codec, now part of the `compression` repo (source `tools/nyx/`, scripts `scripts/fastq/`).
**Compute target:** Stampede3 pvc partition (Intel, `-A BCS25105`)
**Build:** `bash scripts/fastq/build_nyxfqz.sh` -> binary at `openzl/nyxfqz_v2` (v2 is current; v1 `tools/nyx/nyxfqz.cpp` is the frozen reference)
**OpenZL:** `openzl/` @ `d262127` (0.2.5) — the SAME checkout the FASTA and VCF pipelines build against; pinned by `scripts/get_openzl.sh`.
**Paper benchmark:** `sbatch batch_files/benchmark_fastq.slurm` (from the parent `compression` repo; builds nyxfqz_v2 + runs; results -> `results/benchmark_fastq_<jobid>.txt`). Baseline args match the FASTA/VCF benchmarks.

## Memory (2026-08-30)

Whole-file compression was reaching **~50 GB RSS** on a 27 GB FASTQ at 48
threads, because each worker owns an OpenZL Compressor + CCtx (~0.5 GB working
set, roughly independent of chunk size) and the pool was unbounded. Fixed with
`memBudgetWorkers()` (in `nyxfqz_v2.cpp`): the compress/decompress thread pool is
clamped so `nWorkers * ~512 MB <= budget`, where budget is `NYX_MAX_MEM_MB`, else
70% of `/proc/meminfo` MemAvailable, else 8 GB. `NYX_WORKER_MB` overrides the
per-worker estimate; `NYX_MEM_LOG=1` logs the clamp. Round-trip verified lossless
after the change. Remaining overhead: the compressed frames accumulate in RAM
before the archive is written (~archive size, e.g. ~4 GB for a 27 GB input) —
not yet streamed.

## Architecture

- **NYX** uses Meta's OpenZL ML-compression framework
- `openzl/nyx/nyxfqz.cpp` — single source (~2200 lines): pack/compress/decompress/train
- `packFastq()` — converts raw FASTQ bytes to a tagged container with separate streams for SEQ, QUAL, IDs
- `unpackContainer()` — inverse of packFastq; reconstructs raw FASTQ
- Compression: `compressChunkBytes(compressor, raw)` -> calls packFastq -> OpenZL ML model
- Training: `nyxfqz train <sampleDir> <out.zc>` -> learns XGBoost clustering + entropy coding from packed containers

## Models (`compressors/dual/`)

| Model | Description |
|---|---|
| `fastq_var.zc` | Variable-length Illumina; SEQ routes to zstd via ML decision |
| `fastq_fixed.zc` | Fixed-length Illumina; same SEQ routing |
| `fastq_nano_zstdseq.zc` | Nanopore; requires `NYX_SEQ_ROUTE=zstd` at runtime |
| `fastq_nano_lz.zc` | Nanopore with bigWindowLz (slower, not recommended) |
| `fastq_illumina_zstdseq.zc` | Illumina + explicit zstd SEQ (same result as var/fixed default) |

## Chunk Architecture

- `scanChunkBoundaries()`: splits by byte target = `memMB/threads` (default: 500MB/14 ~= 35MB/chunk)
- `packFastq()` further splits at `kRecordsPerChunk = 128*1024` records per sub-chunk
- `cmdCompress` (single .zc) and `cmdCompressPicker` (directory) both call workers in parallel
- Archive format: NYXZCHK1 (single model) or NYXZCHK2 (picker with per-chunk model+route table)

## Container Format (Tagged Segments)

Each segment: `[4B len][1B elt_width][4B tag][data]`

| Tag | Value | Description |
|---|---|---|
| TAG_META | 1 | |
| TAG_SEQLEN | 2 | |
| TAG_QUALLEN | 3 | |
| TAG_PLUSLEN | 4 | |
| TAG_SEQ | 5 | |
| TAG_QUAL | 6 | |
| TAG_PLUS | 7 | |
| TAG_HDRLEN | 8 | |
| TAG_HEADER | 9 | |
| TAG_RAW | 10 | |
| TAG_HDVAL_BASE | 1000 | uint64 numeric header columns |
| TAG_HDWID_BASE | 2000 | digit widths |
| TAG_HTXTLEN_BASE | 3000 | text header column lengths |
| TAG_HTXT_BASE | 4000 | text header columns |
| TAG_QCTX_BASE | 5000 | quality demux by prev byte, 257 streams, qualMode=1 |
| TAG_QUAL_RLE_CHAR | 50 | run chars (qualMode=2, RLE quality) — NEW |
| TAG_QUAL_RLE_CNT | 51 | run lengths uint8 (qualMode=2) — NEW |
| TAG_QUAL_RLE_NRUNS | 52 | per-record run count uint32 (qualMode=2) — NEW |

`META.qualMode`: 0=raw TAG_QUAL, 1=demux by prev byte, 2=RLE (new)

## Benchmark Results (x86 Stampede3, 2026-07-11, 500MB slices, t=14, mem=500MB)

| Dataset | NYX ratio | SPRING ratio | NYX time | SPRING time | NYX mem |
|---|---|---|---|---|---|
| ERR9539086 var Illumina | 7.14x | 7.45x | 20.7s | 15.9s | 4.0GB |
| ERR9539079 var Illumina | 7.79x | 8.92x | 20.0s | 14.9s | 3.3GB |
| ERR9539093 var Illumina | 7.46x | 8.23x | 20.1s | 17.9s | 3.2GB |
| SRR8899104 fixed Illumina | 7.11x | 9.45x | 15.4s | 9.7s | 3.8GB |
| SRR1770413 fixed Illumina | 5.75x | 9.89x | 23.4s | 12.9s | 2.6GB |
| SRR062634 fixed Illumina | 4.36x | 5.16x | 20.9s | 20.6s | 3.8GB |
| ERR4186945 fixed Illumina | 7.80x | 9.43x | 22.7s | 20.9s | 3.6GB |
| DRR206632 fixed 50bp | 11.00x | 22.50x | 17.1s | 9.2s | 3.3GB |
| DRR152860 nano human | 1.98x | 2.08x | 22.1s | 45.6s | 3.9GB |
| DRR1046509 nano Methylobac | 3.12x | 3.27x | 20.8s | 33.3s | 3.6GB |
| DRR1048352 nano Ectobac | 3.06x | 3.08x | 20.9s | 9.9s | 3.6GB |

Whole-file (27GB SRR8899104): NYX 6.73x 780s 44.7GB vs SPRING 9.63x 415s 4.87GB

## Gap Analysis (Confirmed by Code Inspection)

1. **SEQ routing**: `fastq_var.zc` and `fastq_fixed.zc` already route TAG_SEQ to zstd via their ML decision. Forcing `NYX_SEQ_ROUTE=zstd` has zero effect. The ML model is already optimal for unsorted reads.
2. **Read sorting (tested, HURTS)**: Sorting reads by SEQ prefix (`NYX_SORT_READS=1`) makes ratio WORSE on all datasets because it scrambles sequential IDs whose compression benefits from sequential order. For metagenomics (99% singleton reads), there is no genomic overlap to exploit.
3. **Quality**: SPRING achieves 3MB on 173MB quality for ERR9539086. NYX likely achieves ~6-10MB. Most of this gap is the quality coder, not read reordering. SPRING uses run-length + context model; NYX uses per-prev-byte demux (257 streams).
4. **QUAL is near order-1 bound for Nanopore** (2.2% overhead, benchmarks.txt). But this analysis was for Nanopore — Illumina quality is highly binned (NovaSeq: 4 bins) with long runs, far more compressible.

## Improvement Plan

### Phase 1 (DONE, RLE REJECTED): Quality RLE Codec

Implemented qualMode=2 (RLE) with tags TAG_QUAL_RLE_CHAR=50 / CNT=51 / NRUNS=52,
plus `computeRleStats()` and env override `NYX_FORCE_QUALRLE`. Round-trip verified
lossless. **Then measured and REJECTED it as the auto default.**

A/B on variable-Illumina slice (ERR9539086, avg run length 15.6), same model,
forcing each quality mode (total archive size incl. SEQ/IDs):

| Mode | Ratio |
|---|---|
| raw   | 7.680 (best) |
| demux | 7.674 |
| rle   | 7.485 (worst) |

**RLE loses**, even after retraining. OpenZL's entropy/context coder already
exploits quality runs implicitly (repeated symbols cost ~0 bits); explicit RLE
adds framing overhead (nruns uint32 stream) and loses cross-stream context by
splitting into char/count/nruns. RLE only ever *triggered* on variable Illumina
(where it loses) and never on fixed Illumina (run 1.9) or Nanopore, so auto-RLE
was pure downside. **Now: `qualRle = false` by default** in `packFastq()`; the
code path stays behind `NYX_FORCE_QUALRLE=1` for experiments only.

Net Phase-1 win came from the **retrain on fresh data**, not RLE. Models retrained
2026-07-11 with 12k reads x 4 samples/model (Illumina) and 100 reads x 4 (Nanopore
— reads avg 15.7kb, so byte-budget ~12MB matches the OpenZL training-arena limit).
Arena limit is on TOTAL corpus bytes across samples, not read count (~14-25MB).

### Phase 2: Minimizer-Based Read Clustering (High-Coverage Genomic)

- Compute minimum (strand-canonical) minimizer per read (k=15, w=11)
- Radix-sort reads by minimizer before compression -> overlapping reads become adjacent
- Coverage detection: HLL k-mer cardinality or minimizer collision rate; skip for low-coverage
- Align chunk boundaries to minimizer bucket boundaries -> eliminates cross-chunk penalty
- Store delta-encoded permutation for lossless mode (deltas are small for high-coverage: ~genome_size/coverage spacing -> uint8 or uint16 per element)
- After minimizer reordering, drop back to fast LZ for SEQ (not zstd) — speed win

### Phase 3: 2-Bit Sequence Packing

- Replace ASCII ACGT in TAG_SEQ with 2-bit encoding; pull IUPAC/N into exception stream
- 4x more history visible in LZ window -> more matches for high-coverage data
- Naturally combines with Phase 2 (minimizer reordering + 2-bit pack)

## Training Data Available

- **Variable Illumina:** `data/work/ERR9539086.whole.fastq` (7.9GB), `data/fastq/ERR9539079.fastq.gz`, `data/fastq/ERR9539093.fastq.gz`
- **Fixed Illumina:** `data/work/SRR8899104.whole.fastq` (27GB), `data/fastq/ERR4186945_1.fastq.gz`, `data/fastq/DRR206632.fastq.gz`, `data/fastq/SRR1770413_1.fastq.gz`, `data/fastq/SRR062634_1.fastq.gz`
- **Nanopore:** `data/fastq/DRR152860_1.fastq.gz`, `data/fastq/DRR1046509_1.fastq.gz`, `data/fastq/DRR1048352_1.fastq.gz`

## Build Notes

- cmake downloaded to `.tools/cmake-3.30.5-linux-x86_64/` (Stampede3 has no system cmake)
- `scripts/build.sh` patched to find that cmake
- Custom OpenZL files preserved via `rsync --exclude`: `openzl/nyx/nyxfqz.cpp`, `openzl/build-scripts/make/zldefs.make`, `openzl/nyxfqz.make`

## Key Environment Variables

| Variable | Description |
|---|---|
| `NYX_SEQ_ROUTE=zstd` | Force SEQ stream to plain zstd |
| `NYX_FORCE_SEQ_BIGLZ=1` | Force SEQ to bigWindowLz |
| `NYX_SORT_READS=1` | Enable read sorting (disabled by default; hurts ratio due to ID compression) |
| `NYX_FORCE_QUALDEMUX=0\|1` | Override adaptive quality demux decision |
| `NYX_FORCE_QUALRLE=0\|1` | Override RLE quality decision (new) |
| `NYX_PICKER_LOG=1` | Log per-chunk picker decisions |
| `NYX_PICKER_DRYRUN=1` | Run picker without compressing |
| `NYX_CLUSTER=auto\|1\|0` | Global order-preserving read clustering (NYXZCHK3). auto=benefit-gated, 1=force, 0/unset=off. See SRR_CLUSTERING_HANDOFF.md |
| `NYX_CLUSTER_GATE=<0..1>` | Auto-gate threshold (default 0.98; sorted/orig sample size ratio below which we cluster) |
| `NYX_CLUSTER_LOG=1` | Log the auto-gate decision |
| `NYX_CLUSTER_PERCHUNK=1` | Old per-chunk clustering (experiments only; global path preferred) |
| `NYX_MAX_MEM_MB=<MB>` | Cap the compress/decompress worker pool to this memory budget. Default: 70% of MemAvailable, else 8192. |
| `NYX_WORKER_MB=<MB>` | Per-worker working-set estimate for the clamp (default 512). |
| `NYX_MEM_LOG=1` | Log when the worker pool is clamped. |

## Read Clustering (Phase 2, DONE 2026-07-12)

`NYX_CLUSTER=auto` reorders ALL reads by canonical minimizer (k=15) up front, then
compresses the reordered stream with the normal parallel chunker + a global
permutation for exact order-preserving reconstruction. Decouples clustering SCOPE
(whole file) from compression PARALLELISM (many small chunks) → full ratio gain at
full thread utilization. A whole-record zstd benefit trial gates it on so it only
triggers for medium/high-coverage genomic data (SRR1770413, deep E.coli) and skips
metagenomic/low-coverage/local-redundancy data (ERR, SRR062634, SRR8899104). Lossless.
Archive: NYXZCHK3 = [magic][nChunks][threads][origSize][nRecords][permFrame][frames].
New training data: `data/fastq/DRR016013_1.fastq.gz` (deep E.coli, clustering demo),
`DRR058063_1` (E.coli MiSeq), `ERR9539078` (NovaSeq variable).
Model for clustered fixed data: `compressors/dual/fastq_fixed_cluster.zc`.
