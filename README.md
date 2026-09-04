# biocompress — lossless genomic compression on OpenZL

A single framework that compresses the three dominant genomic text formats —
**FASTA**, **FASTQ** and **VCF** — by (1) applying a reversible, format-aware
structural transform that turns the file into homogeneous streams, then (2)
compressing those streams with a **pre-trained** [OpenZL](https://github.com/facebook/openzl)
graph.

Every pipeline is **byte-exact**: the decompressor reproduces the input file
bit-for-bit, including soft-masking, IUPAC codes, line widths, CRLF line
endings and a missing trailing newline.

| format | command | shipped models | user trains anything? |
|---|---|---|---|
| FASTA | `scripts/fastazl` | **1** universal (`artifacts/fasta_model.zlc`) | no |
| FASTQ | `openzl/nyxfqz_v2` | **1** universal Illumina (`artifacts/fastq_models/fastq_illumina.zc`) | no |
| VCF   | `scripts/vcf/vcfzl` | **8**, one per archetype, chosen automatically | no |

Compression is **compress-only at runtime**. The models ship with the repo; the
training scripts exist only so maintainers can regenerate them on a new OpenZL
release.

---

## Install

See [INSTALL.md](INSTALL.md) for the full list. Short version:

```bash
git clone <this repo> biocompress && cd biocompress
bash scripts/build_all.sh          # fetches + patches + builds OpenZL, builds the tools
bash scripts/fastq/build_nyxfqz.sh # only if you need FASTQ
```

`build_all.sh` pins OpenZL to the exact commit used for every published number
(**0.2.5, `d262127`**) and applies the wide-CSV limit patch that the VCF `panel`
archetype needs. All three pipelines build against that same commit.

---

## Quick start

### FASTA — [full guide](README_FASTA.md)

```bash
scripts/fastazl compress   genome.fna genome.fazl --verify
scripts/fastazl decompress genome.fazl restored.fna
cmp genome.fna restored.fna        # byte-identical
```

### FASTQ — [full guide](README_FASTQ.md)

```bash
openzl/nyxfqz_v2 compress \
    artifacts/fastq_models/fastq_illumina.zc reads.fastq reads.nyxz 16 500
openzl/nyxfqz_v2 decompress reads.nyxz restored.fastq
```

### VCF — [full guide](README_VCF.md)

```bash
scripts/vcf/vcfzl compress   calls.vcf calls.vcfz --verify   # archetype auto-detected
scripts/vcf/vcfzl decompress calls.vcfz restored.vcf
scripts/vcf/vcfzl classify   calls.vcf                        # just show the archetype
```

---

## How it works

```text
                 ┌──────────────────────────┐    ┌────────────────────────┐
  input file ──► │ reversible structural    │──► │ pre-trained OpenZL     │──► archive
                 │ transform (per format)   │    │ graph (per format/type)│
                 └──────────────────────────┘    └────────────────────────┘
```

**FASTA → FAV5.** 2-bit ACGT packing, run-length upper/lower case mask,
run-length exception runs for N / IUPAC / `\r`, plus an explicit line-layout
table so wrapping is reproduced exactly.

**FASTQ → tagged container.** Reads are split into separate streams for
identifiers (further split into numeric and text columns), sequence and
quality, with optional order-preserving minimizer clustering.

**VCF → header/body split + column dispatch.** The header is stored verbatim;
the body is cut into line-safe parts and routed to the model for the detected
archetype.

Each format keeps a **fallback**: if a trained graph cannot handle a chunk it is
re-compressed with a generic OpenZL profile. Correctness never depends on the
model being a good fit — only the ratio does.

---

## Benchmarks

The published numbers are produced by the scripts in `batch_files/` (SLURM) and
land in `results/`:

| script | what it measures |
|---|---|
| `batch_files/benchmark_fasta.slurm` | 5 genomes vs gzip / pigz / zstd / 7z / xz |
| `batch_files/benchmark_fastq.slurm` | 8 datasets vs gzip / pigz / zstd / 7z / xz / SPRING |
| `batch_files/benchmark_vcfzl.slurm` | one file per archetype vs gzip / zstd / xz |
| `batch_files/benchmark_vcf_corpus.slurm` | 26-file corpus: archetype auto-detect accuracy + ratios |

Baseline arguments are **identical across all three formats**
(`gzip -9`, `pigz -9`, `zstd -19 --long=27`, `7z -mx=9`, `xz -9e --block-size=192MiB`),
so the tables are directly comparable.

Every benchmark verifies a byte-exact round trip per file and reports it in a
`roundtrip` column. A row without `OK` there is not a valid result.

---

## Repository layout

```
scripts/fastazl              FASTA CLI
scripts/vcf/vcfzl            VCF CLI (classify / compress / decompress / archetypes)
openzl/nyxfqz_v2       FASTQ codec binary
tools/                       C++ transforms (biocompress_preprocessor, fasta_postprocess,
                             vcf_preprocessing, vcf_postprocess, ...)
schemas/fasta_packed_v5.sddl SDDL description of the FAV5 container
artifacts/                   the shipped trained models
scripts/train_*.sh           maintainer-only model regeneration
batch_files/*.slurm          benchmark jobs
results/                     benchmark output (CSV + summary)
```

## License / provenance

OpenZL is Meta's, fetched and patched by `scripts/get_openzl.sh` +
`scripts/patch_openzl.sh`. Everything under `tools/`, `scripts/` and
`tools/nyx/` is this project's.
