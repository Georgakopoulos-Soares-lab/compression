# FASTQ model library (NYX)

Trained OpenZL compressors for the FASTQ codec (`tools/nyx/nyxfqz_v2.cpp`).
`nyx compress` and the benchmark scripts read this directory by default
(`NYX_FASTQ_MODELS` / `NYX_MODEL_DIR`).

## Provenance

**No file in the paper's FASTQ benchmark was used to train these models, and no
study that contributed a benchmark file did either.** Training uses the first
40,000 reads of each of eight public runs fetched by
`scripts/fastq/download_heldout_fastq.sh`, one pair per kind of library the
benchmark contains:

| kind | benchmark files (not used in training) | training runs |
|---|---|---|
| A NovaSeq human ancient-DNA WGS, short trimmed | ERR9539079, ERR9539086, ERR9539093 | ERR12161649, ERR11138821 |
| B NovaSeq human WGS, fixed length | ERR4186945_1 | ERR11235414, ERR11217152 |
| C HiSeq/NovaSeq transcriptomic | DRR206632 | ERR10216234, ERR11844657 |
| D small RNA / short fixed reads | SRR8899104 | ERR12664699, ERR11175705 |

Retrain with `scripts/fastq/train_fastq_models.sh [outdir]`; it refuses to run
on a benchmark file and verifies a byte-exact round trip on held-out reads.
`download_heldout_fastq.sh` also lists two further ancient-DNA runs
(ERR13081220, ERR12246909) whose read lengths match the benchmark's more
closely; models trained with them were measured and were not better, so the
shipped models are the eight-run set.

The Nanopore models were trained on long-read data of their own and no
published result depends on them.

| model | data type | notes |
|-------|-----------|-------|
| `fastq_var.zc` | variable-length Illumina (NovaSeq) | SEQ -> zstd |
| `fastq_fixed.zc` | fixed-length Illumina | SEQ -> zstd |
| `fastq_fixed_cluster.zc` | fixed-length Illumina, clustered | for `NYX_CLUSTER` reordered body |
| `fastq_illumina_zstdseq.zc` | Illumina, explicit zstd SEQ | |
| `fastq_illumina.zc` | symlink to `fastq_var.zc` | name kept for older callers |
| `fastq_nano*.zc` | Nanopore | long reads; `nano_zstdseq` is the recommended one |

The per-chunk picker prices these candidates on a chunk from the middle of the
file (`calibrateModel`) and uses the winner, so the choice is measured, not
predicted.
