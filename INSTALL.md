# Installation

## Prerequisites

| tool | why |
|---|---|
| `git` | fetching OpenZL |
| `make`, `g++` (C++17) | building OpenZL and the transforms |
| `cmake` ≥ 3.20 | OpenZL's xgboost dependency |
| `python3` | training-sample carving |
| `curl` | downloading reference data (optional) |
| `tar`, `gzip` | archive handling |

Optional, only for benchmarking against baselines: `pigz`, `zstd`, `xz`, `7z`,
and [SPRING](https://github.com/shubhamchandak94/SPRING) for FASTQ.
`bcftools` is only needed if you want to rebuild the VCF validation corpus.

## Build

```bash
git clone <this repo> biocompress
cd biocompress
bash scripts/build_all.sh
```

That script:

1. clones OpenZL and checks out the pinned commit — **0.2.5, `d262127`**
   (`scripts/get_openzl.sh`)
2. applies the wide-CSV limit patch, which raises OpenZL's input/dispatch limits
   from 2048 to 4096 — required for the VCF `panel` archetype (1000G phase 3 has
   2504 sample columns) (`scripts/patch_openzl.sh`, each substitution verified)
3. builds `openzl/zli`
4. builds the C++ transforms into `tools/`

It takes a lock so two concurrent builds cannot race on the shared `openzl/`
directory.

For FASTQ, additionally:

```bash
bash scripts/fastq/build_nyxfqz.sh   # -> openzl/nyxfqz_v2
```

The FASTQ sub-project keeps its own OpenZL checkout; the benchmark job pins it
to the same commit and applies the same patch, so all three pipelines build
against an identical OpenZL.

## Verify the build

```bash
openzl/zli --version                 # Demo CLI for OpenZL. Version 0.2.5
ls tools/                            # biocompress_preprocessor, fasta_postprocess,
                                     # vcf_preprocessing, vcf_postprocess, ...
scripts/vcf/vcfzl archetypes         # 8 archetypes, all "ready"
```

## Smoke test

Each pipeline can prove its own losslessness:

```bash
# FASTA
scripts/fastazl compress  test.fna test.fazl --verify

# VCF
scripts/vcf/vcfzl compress test.vcf test.vcfz --verify

# FASTQ
openzl/nyxfqz_v2 compress artifacts/fastq_models/fastq_illumina.zc \
    test.fastq test.nyxz 8 500
openzl/nyxfqz_v2 decompress test.nyxz back.fastq
cmp test.fastq back.fastq
```

A broader FASTA/VCF round-trip suite over edge cases and real files:

```bash
bash scripts/get_validation_corpus.sh
bash scripts/validate_roundtrip.sh
```

## Shipped models

The trained models are committed, so nothing needs training to use the tool:

```
artifacts/fasta_model.zlc                   FASTA  (1)
artifacts/fastq_models/fastq_illumina.zc    FASTQ  (1)
artifacts/vcf_models/<archetype>.zlc        VCF    (8)
artifacts/vcf_models/archetypes.tsv         the archetype registry
```

If a model is missing, each pipeline falls back to a generic OpenZL profile —
still byte-exact, lower ratio.

## Reference data (optional)

Only needed to reproduce the benchmarks:

```bash
bash scripts/get_validation_corpus.sh   # FASTA/VCF round-trip corpus
bash scripts/vcf/get_corpus.sh          # 26-file VCF archetype corpus (~15 GB)
bash scripts/fastq/download_fastq.sh    # FASTQ benchmark datasets (~13 GB compressed)
```

## Troubleshooting

**`zli train` exits 0 but writes an empty compressor.** Known OpenZL behaviour
when a sample exceeds the training size limit or the ACE stage runs out of
memory. The pipelines validate the model (non-empty + a real test compress)
before trusting it and fall back to a generic profile otherwise.

**"Compressor format version is not set"** when training the FASTQ model —
OpenZL ≥ 0.2.4 requires the format version on the compressor before it is
serialized; already handled in `nyxfqz_v2.cpp`. If you see it, your
`nyxfqz_v2` binary is stale — rebuild.

**Do not name a shell variable `WORK`** in scripts here. Some HPC sites
(Stampede3 among them) export `WORK` as a system path, which silently shadows a
script-local value and sends output somewhere unwritable.
