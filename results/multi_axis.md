# Multi-axis summary

Verified round trips only. **Pareto-optimal** means no single tool in the
comparison beats NYX on both compression ratio and compression speed;
a tool that wins one axis by losing the other does not dominate.

**NYX is Pareto-optimal on 54/54 inputs across all formats.**

## VCF

| input | NYX ratio | comp s | best peak GB | beaten on ratio by | beats zstd+xz on ratio | on speed | lightest | Pareto-optimal |
|---|---|---|---|---|---|---|---|---|
| 1000G.chr20.phase3.vcf | 468.67x | 105 | 3.27 | - | yes | NO | - | yes |
| 1000G.chr22.phase3.vcf | 431.80x | 46 | 1.22 (budget) | - | yes | yes | yes | yes |
| HG004_GRCh38_v4.2.1.vcf | 52.15x | 10 | 5.17 | - | yes | yes | - | yes |
| HG002_GRCh38_v4.2.1.vcf | 49.67x | 8 | 1.66 (budget) | - | yes | yes | yes | yes |
| seqc2_hcc1395_ssnv.vcf | 30.80x | 4 | 0.30 | - | yes | yes | - | yes |
| gnomad_v4.1_genomes_chrY.vcf | 19.30x | 24 | 6.24 | - | yes | yes | - | yes |
| gvcf_nfcore_test2.g.vcf | 18.32x | 5 | 0.23 | - | yes | yes | - | yes |
| civic_nightly.vcf | 17.18x | 0 | 0.04 | zstd, xz, 7z | NO | yes | - | yes |
| clinvar_GRCh38.vcf | 16.86x | 12 | 1.91 (budget) | - | yes | yes | yes | yes |
| seqc2_hcc1395_ssnv_superset.vcf | 6.48x | 19 | 7.24 | - | yes | yes | - | yes |

VCF: Pareto-optimal on **10/10**; highest ratio of any codec on 9/10; beats zstd -19 and xz -9e on ratio 9/10, on compression speed 9/10, on both 8/10; lighter than both on peak memory on 3/3.

## FASTQ

| input | NYX ratio | comp s | best peak GB | beaten on ratio by | beats zstd+xz on ratio | on speed | lightest | Pareto-optimal |
|---|---|---|---|---|---|---|---|---|
| DRR206632.fastq | 11.92x | 28 | 2.09 | spring | yes | yes | yes | yes |
| ERR9539079.fastq | 8.77x | 219 | 2.18 | spring | yes | yes | yes | yes |
| ERR9539093.fastq | 8.30x | 186 | 2.11 | - | yes | yes | yes | yes |
| ERR4186945_1.fastq | 8.12x | 266 | 1.92 | spring | yes | yes | yes | yes |
| ERR9539086.fastq | 7.87x | 136 | 2.13 | - | yes | yes | yes | yes |
| SRR8899104.fastq | 7.28x | 296 | 2.29 | spring | yes | yes | yes | yes |

FASTQ: Pareto-optimal on **6/6**; highest ratio of any codec on 2/6; beats zstd -19 and xz -9e on ratio 6/6, on compression speed 6/6, on both 6/6; lighter than both on peak memory on 6/6.

## FASTA

| input | NYX ratio | comp s | best peak GB | beaten on ratio by | beats zstd+xz on ratio | on speed | lightest | Pareto-optimal |
|---|---|---|---|---|---|---|---|---|
| wheat_IWGSC.fa | 5.49x | 151 | 3.89 | naf-20, zstd, xz, 7z | NO | yes | NO | yes |
| GRCm39.fa | 4.82x | 15 | 2.57 | naf-20 | yes | yes | NO | yes |
| GRCh38.p14.fa | 4.81x | 16 | 2.62 | naf-20 | yes | yes | NO | yes |
| T2T-CHM13v2.0.fa | 4.43x | 20 | 3.01 | naf-20 | yes | yes | NO | yes |

FASTA: Pareto-optimal on **4/4**; highest ratio of any codec on 0/4; beats zstd -19 and xz -9e on ratio 3/4, on compression speed 4/4, on both 3/4; lighter than both on peak memory on 0/4.

## BED

| input | NYX ratio | comp s | best peak GB | beaten on ratio by | beats zstd+xz on ratio | on speed | lightest | Pareto-optimal |
|---|---|---|---|---|---|---|---|---|
| cd_E001_15_coreMarks_dense.bed | 30.68x | 2 | 0.44 | - | yes | yes | NO | yes |
| E003_15_coreMarks_dense.bed | 30.21x | 2 | 0.40 | - | yes | yes | NO | yes |
| cd_E066_15_coreMarks_dense.bed | 29.67x | 2 | 0.45 | - | yes | yes | NO | yes |
| cd_E032_15_coreMarks_hg38lift_dense.bed | 28.54x | 2 | 0.39 | - | yes | yes | NO | yes |
| cd_E097_15_coreMarks_hg38lift_dense.bed | 28.01x | 2 | 0.40 | - | yes | yes | NO | yes |
| cs_E001_15_coreMarks_hg38lift_segments.bed | 12.69x | 2 | 0.42 | - | yes | NO | NO | yes |
| cs_E096_15_coreMarks_hg38lift_segments.bed | 12.53x | 2 | 0.42 | - | yes | NO | NO | yes |
| E114_DNase.narrowPeak | 12.29x | 2 | 0.45 | - | yes | NO | NO | yes |
| cs_E032_15_coreMarks_hg38lift_segments.bed | 12.06x | 1 | 0.42 | - | yes | NO | NO | yes |
| cs_E065_15_coreMarks_hg38lift_segments.bed | 11.92x | 2 | 0.42 | - | yes | NO | NO | yes |
| np_E119-H3K36me3.narrowPeak | 11.32x | 2 | 0.40 | - | yes | NO | NO | yes |
| np_E021-H3K4me1.narrowPeak | 10.38x | 2 | 0.40 | - | yes | NO | NO | yes |
| np_E008-H2BK15ac.narrowPeak | 10.22x | 1 | 0.23 | - | yes | NO | NO | yes |
| np_E042-H3K9me3.narrowPeak | 9.74x | 1 | 0.30 | - | yes | NO | NO | yes |
| screen_ccre_GRCh38.bed | 9.22x | 4 | 1.27 | - | yes | yes | NO | yes |
| encode_tfbs_clustered_hg38.bed | 9.00x | 3 | 2.95 | - | yes | yes | NO | yes |
| np_E065-H3K27ac.narrowPeak | 9.00x | 2 | 0.39 | - | yes | NO | NO | yes |
| np_E001-H3K27me3.narrowPeak | 8.88x | 1 | 0.32 | - | yes | NO | NO | yes |
| np_E103-H3K4me3.narrowPeak | 8.70x | 1 | 0.31 | - | yes | NO | NO | yes |
| fantom_cage_peaks_hg38.bed | 8.45x | 3 | 0.46 | - | yes | NO | NO | yes |
| E003_H3K36me3.broadPeak | 7.23x | 3 | 0.88 | - | yes | NO | NO | yes |
| np_E084-H3K4me3.narrowPeak | 6.51x | 1 | 0.21 | - | yes | NO | NO | yes |
| bp_E034-H3K9me3.broadPeak | 6.22x | 3 | 0.71 | - | yes | NO | NO | yes |
| bp_E001-H3K27me3.broadPeak | 6.03x | 2 | 0.57 | - | yes | NO | NO | yes |
| bp_E063-H3K27me3.broadPeak | 5.84x | 2 | 0.59 | - | yes | NO | NO | yes |
| bp_E115-H3K36me3.broadPeak | 5.73x | 2 | 0.53 | - | yes | NO | NO | yes |
| bp_E011-H3K4me3.broadPeak | 5.34x | 2 | 0.55 | - | yes | NO | NO | yes |
| bp_E090-H3K27ac.broadPeak | 5.22x | 2 | 0.54 | - | yes | NO | NO | yes |
| E003_H3K4me1.gappedPeak | 4.64x | 2 | 0.44 | - | yes | NO | NO | yes |
| gp_E016-H3K4me1.gappedPeak | 4.58x | 2 | 0.54 | - | yes | NO | NO | yes |
| gp_E078-H3K4me3.gappedPeak | 4.34x | 1 | 0.29 | - | yes | NO | NO | yes |
| gp_E045-H3K4me3.gappedPeak | 4.27x | 1 | 0.22 | - | yes | NO | NO | yes |
| gp_E110-H3K9ac.gappedPeak | 4.15x | 1 | 0.28 | - | yes | NO | NO | yes |
| gp_E001-H3K27me3.gappedPeak | 4.10x | 1 | 0.26 | - | yes | NO | NO | yes |

BED: Pareto-optimal on **34/34**; highest ratio of any codec on 34/34; beats zstd -19 and xz -9e on ratio 34/34, on compression speed 7/34, on both 7/34; lighter than both on peak memory on 0/34.

## Thread scaling

| format | input | threads | ratio | comp s | MB/s | peak GB |
|---|---|---|---|---|---|---|
| VCF | HG002_GRCh38_v4.2.1.vcf | 1 | 49.67x | 87.4 | 32 | 0.50 |
| VCF | HG002_GRCh38_v4.2.1.vcf | 2 | 49.67x | 46.3 | 61 | 0.82 |
| VCF | HG002_GRCh38_v4.2.1.vcf | 4 | 49.67x | 19.0 | 150 | 1.41 |
| VCF | HG002_GRCh38_v4.2.1.vcf | 8 | 49.67x | 9.7 | 292 | 2.74 |
| VCF | HG002_GRCh38_v4.2.1.vcf | 16 | 49.67x | 6.1 | 464 | 5.14 |
| FASTA | GRCm39.fa | 1 | 4.83x | 74.5 | 37 | 1.01 |
| FASTA | GRCm39.fa | 2 | 4.83x | 45.8 | 61 | 1.50 |
| FASTA | GRCm39.fa | 4 | 4.83x | 24.4 | 114 | 2.35 |
| FASTA | GRCm39.fa | 8 | 4.83x | 17.3 | 160 | 3.12 |
| FASTA | GRCm39.fa | 16 | 4.82x | 15.7 | 176 | 2.53 |
| FASTQ | ERR9539086.fastq | 1 | 8.29x | 1800.6 | 5 | 4.91 |
| FASTQ | ERR9539086.fastq | 2 | 8.29x | 952.0 | 9 | 2.62 |
| FASTQ | ERR9539086.fastq | 4 | 8.29x | 490.6 | 17 | 1.46 |
| FASTQ | ERR9539086.fastq | 8 | 8.29x | 245.2 | 34 | 1.11 |
| FASTQ | ERR9539086.fastq | 16 | 8.27x | 144.8 | 58 | 1.39 |
