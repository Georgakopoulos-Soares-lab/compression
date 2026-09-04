# Benchmark data behind the manuscript

Final, whole-file runs. These are the exact numbers reported in the paper.

| File | Run | Contents |
|---|---|---|
| `fasta_whole_file_3458801.csv` | SLURM 3458801 | Five assemblies x gzip/pigz/zstd/xz/7z/NYX, plus the HARC reordering ablation |
| `fastq_whole_file_3458771.csv` | SLURM 3458771 | Eight Illumina runs x gzip/pigz/zstd/7z/xz/SPRING/NYX |
| `vcf_corpus_3462183.csv`       | SLURM 3462183 | VCF archetype corpus (results deferred to a later section) |
| `genozip_benchmarks.csv`       | earlier harness | Genozip, on a different copy of the inputs — not comparable with the above |

Columns: `ratio` is uncompressed/compressed bytes; times are wall-clock seconds;
peak memory is maximum resident set size. `roundtrip = OK` means a full
decompression compared byte-identical to the input.

Job logs for these and for superseded runs are not kept in the repository.
