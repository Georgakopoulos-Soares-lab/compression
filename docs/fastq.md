# FASTQ — `openzl/nyxfqz_v2` (source: `tools/nyx/nyxfqz_v2.cpp`)

> These are the FASTQ codec's own flags and archive names. Most users want the
> single entry point instead: `nyx compress <file>` writes one `.nyx` file and
> `nyx decompress` restores it. See the top-level README.

Byte-exact FASTQ compression with **one shipped universal Illumina model**. No
training on your data.

## Build

The FASTQ codec is a self-contained sub-project with its own OpenZL checkout
(pinned to the same commit as the rest of the repo):

```bash
bash scripts/fastq/build_nyxfqz.sh   # -> openzl/nyxfqz_v2
```

## Use

```bash
MODEL=artifacts/fastq_models/fastq_illumina.zc

# compress:  <model> <in.fastq> <out.nyxz> [threads] [memMB]
openzl/nyxfqz_v2 compress "$MODEL" reads.fastq reads.nyxz 16 500

# decompress
openzl/nyxfqz_v2 decompress reads.nyxz restored.fastq

cmp reads.fastq restored.fastq      # byte-identical
```

`threads` defaults to your core count, `memMB` to 400.

### Read clustering

`NYX_CLUSTER=auto` reorders reads by minimizer before compression and stores a
permutation so the original order is restored exactly. It is **benefit-gated**:
a cheap probe runs first and clustering is skipped when it would not help
(metagenomic or low-coverage data), because the reorder costs time.

```bash
NYX_CLUSTER=auto openzl/nyxfqz_v2 compress "$MODEL" reads.fastq reads.nyxz 16 500
```

`NYX_CLUSTER=1` forces it on, `0` off. `NYX_CLUSTER_LOG=1` prints the gate
decision.

## Memory control

Peak RAM is bounded by a single knob:

```bash
NYX_MAX_MEM_MB=8000 openzl/nyxfqz_v2 compress "$MODEL" big.fastq big.nyxz 48 500
```

The worker pool is sized so `reserved buffers + nWorkers × per-worker` fits the
budget. `NYX_MEM_LOG=1` prints the clamp when it fires.

Design choices that keep the peak down:

- **one shared OpenZL `Compressor`** across all workers (each worker only owns a
  `CCtx`), instead of every worker deserializing its own copy of the trained graph
- **the archive is streamed to disk** as frames complete, with a bounded in-order
  writer, instead of the whole compressed output being assembled in RAM
- **clustering blocks are capped at 256 MiB**, and the source block is freed as
  soon as the reordered copy exists
- **decompression memory-maps the archive** and decodes frames in place — no copy
  of the archive or of individual frames

## What is preserved

Byte-exact. Read identifiers (including the numeric and text columns they are
split into), sequence, the `+` line, and quality all round-trip exactly, in the
original read order, for both fixed- and variable-length reads.

## How it works

```text
reads.fastq ─► packFastq (tagged streams) ─► [optional minimizer reorder]
                                                  │
                                      per-chunk OpenZL compress ─► reads.nyxz
```

`packFastq` splits records into separate streams — identifiers (further
decomposed into numeric and text columns), sequence lengths, sequence, quality
(optionally demultiplexed by preceding byte) — so each gets an appropriate
codec. Chunks are record-aligned and compress in parallel.

Archive formats: `NYXZCHK1` (single model), `NYXZCHK2` (per-chunk model picker),
`NYXZCHK3` (globally clustered, carries the permutation).

## Where it stands against SPRING

Honest summary, from `results/benchmark_fastq_*.txt`:

- **Ratio:** we win on some datasets and lose on others. SPRING is a
  FASTQ-specialist with a stronger quality-stream model; on several fixed-length
  Illumina sets it compresses better than we do. We are not claiming to beat it
  across the board.
- **Speed:** competitive, and faster than SPRING on the larger files.
- **Scope:** SPRING is FASTQ-only. This is the same framework and the same
  OpenZL build used for FASTA and VCF.

The design goal for FASTQ is *comparable ratio at better memory and speed*, not
best-in-class ratio.

## Environment variables

| variable | effect |
|---|---|
| `NYX_MAX_MEM_MB` | total RAM ceiling for the worker pool (default: 70 % of `MemAvailable`, else 8192) |
| `NYX_WORKER_MB` | per-worker working-set estimate used by the clamp (default 384) |
| `NYX_MEM_LOG=1` | log when the pool is clamped |
| `NYX_CLUSTER=auto\|1\|0` | order-preserving minimizer read clustering |
| `NYX_CLUSTER_LOG=1` | log the clustering benefit-gate decision |
| `NYX_CLUSTER_BLOCK_MB` | clustering block size (default: capped at 256) |
| `NYX_CLEVEL` | override the compression level (default 12, or 19 when clustering) |
| `NYX_SEQ_ROUTE=zstd` | force the sequence stream to plain zstd |
| `NYX_MODEL_DIR` | where to look for models |

## Regenerating the model (maintainers)

Not needed to use the tool.

```bash
bash scripts/fastq/train_fastq_models.sh     # -> artifacts/fastq_models/*.zc (held-out corpus)
```

Trains one model on a mixed corpus — variable- and fixed-length reads, half
packed with clustering and half without — so a single model covers every stream
shape it will meet at runtime. The script verifies byte-exact round trips before
it finishes.
