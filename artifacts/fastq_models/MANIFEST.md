# FASTQ model library (NYX)

Trained OpenZL compressors for the FASTQ codec (`tools/nyx/nyxfqz_v2.cpp`).
These sit alongside the VCF models; `scripts/fastq/paper_bench.sh` reads this directory by default (`NYX_MODEL_DIR`).

| model | data type | notes |
|-------|-----------|-------|
| `fastq_var.zc` | variable-length Illumina (NovaSeq) | SEQ -> zstd |
| `fastq_fixed.zc` | fixed-length Illumina | SEQ -> zstd |
| `fastq_fixed_cluster.zc` | fixed-length Illumina, clustered | for `NYX_CLUSTER` reordered body |
| `fastq_illumina_zstdseq.zc` | Illumina, explicit zstd SEQ | |
| `fastq_nano.zc`, `fastq_nano_zstdseq.zc`, `fastq_nano_lz.zc`, `fastq_nano_a4.zc` | Nanopore | long reads; `nano_zstdseq` is the recommended one |

The per-chunk picker (`nyxfqz_v2 compress <dir>`) or `nyx_model()` in the paper
benchmark selects among these by a data-type + redundancy probe. Retrain with
`scripts/fastq/retrain_paper_models.sh`.
