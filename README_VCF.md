# VCF — `scripts/vcf/vcfzl`

Byte-exact VCF compression with **8 shipped archetype models** and automatic
archetype detection. No training on your data.

## Use

```bash
# compress — the archetype is detected from the file's structure
scripts/vcf/vcfzl compress calls.vcf calls.vcfz

# compress and prove the round trip in the same run
scripts/vcf/vcfzl compress calls.vcf calls.vcfz --verify

# decompress
scripts/vcf/vcfzl decompress calls.vcfz restored.vcf

# ask what archetype a file is, without compressing
scripts/vcf/vcfzl classify calls.vcf
VCFZL_CLASSIFY_DEBUG=1 scripts/vcf/vcfzl classify calls.vcf   # + the evidence

# list the shipped archetypes
scripts/vcf/vcfzl archetypes

# inspect an archive
scripts/vcf/vcfzl inspect calls.vcfz
```

Options: `--threads N`, `--archetype <id|auto>` (default `auto`), `--verify`.

Input may be plain `.vcf` or gzip/bgzip-compressed (`.vcf.gz`, `.bgz`) —
detected by magic bytes, not by file extension.

## The 8 archetypes

One trained model per archetype, in `artifacts/vcf_models/<id>.zlc`. The
registry (`artifacts/vcf_models/archetypes.tsv`) records the rule, the training
source and whether that source is real or derived.

| id | what it is | detected by | typical sources |
|---|---|---|---|
| `sites-annotated` | sites-only, annotation-heavy INFO | no FORMAT column; `CLNSIG`/`ANN=`/`CSQ=`/`GENEINFO`/`COSMIC` on >5 % of rows, or outnumbering frequency keys | ClinVar, COSMIC, VEP/SnpEff output, CIViC |
| `sites-frequency` | sites-only, allele-frequency INFO | no FORMAT column; INFO dominated by `AF`/`AC`/`AN`/`gnomAD`/`TOPMED` | gnomAD sites, dbSNP, 1000G sites-only |
| `single-sample` | one germline sample | exactly 1 sample column, no gVCF or somatic evidence | GIAB HG002/3/4, GATK / DeepVariant / DRAGEN single-sample |
| `gvcf` | single sample with reference blocks | `##ALT=<ID=NON_REF>` or HaplotypeCaller `-ERC` in the header, or >10 % of rows carry `<NON_REF>`, or >50 % carry `END=` | GATK HaplotypeCaller `-ERC GVCF`, DeepVariant gVCF |
| `somatic` | tumour/normal call set | somatic-caller or `##SAMPLE` header, `SOMATIC` INFO on >20 % of rows, or a 2-sample tumour/normal-named pair | SEQC2 HCC1395, Mutect2, Strelka2, VarScan2 |
| `family-trio` | small pedigree | 2–20 sample columns | GIAB Ashkenazi trio, 1000G trios |
| `cohort` | joint-genotyped cohort | 21–999 sample columns | GTEx, 1000G subsets |
| `panel` | large reference panel | ≥1000 sample columns | 1000G phase 3, HGDP |

### The classifier is structural, not a lookup

`vcfzl classify` reads the **complete header plus the first 20 000 data rows**
and applies the rules above. It never looks at the filename and never consults a
list of known files, so a VCF it has never seen is bucketed the same way as the
training sources.

`VCFZL_CLASSIFY_DEBUG=1` prints the evidence:

```
classify: cohort  [ncol=109 nsamp=100 rows=20000 nonref=0 end=13 som_info=0
                   som_hdr=0 gvcf_hdr=0 tn=0 info_defs=27 info_used=22 anno=0 freq=20000]
```

### If the guess is wrong

It costs ratio, never correctness. Every body part is compressed with a
three-step fallback — **archetype model → `--profile csv` (tab) → `--profile serial`** —
so a part the model cannot handle still compresses losslessly. The count of
parts that needed a fallback is stored in the archive and shown by
`vcfzl inspect`.

You can also force a choice: `--archetype panel`.

## Archive format

`.vcfz` is a tar containing:

```
FORMAT          version tag
checksum        SHA-256 over the compressed parts + header + manifest
manifest.json   part list and sizes
archetype       the id used
header.vcf      the ## and #CHROM lines, verbatim
zl/*.zl         compressed body parts
fallback_parts  how many parts used a fallback profile
```

`vcfzl decompress` verifies the format tag and checksum before decoding.

## Performance

`scripts/vcf/vcfzl` beats `gzip -9`, `zstd -19 --long=27` and
`xz -9e --block-size=192MiB` on every archetype we have measured; the margin is
largest on the genotype-heavy archetypes (`panel`, `cohort`) where column-wise
dispatch pays off most. Current numbers live in
`results/benchmark_vcfzl_*.csv`.

Auto-detect accuracy over a 26-file validation corpus (3+ real files per
archetype, built by `scripts/vcf/get_corpus.sh`) is reported by
`batch_files/benchmark_vcf_corpus.slurm` into `results/vcf_corpus_*.csv`.

## Regenerating the models (maintainers)

Not needed to use the tool.

```bash
bash scripts/vcf/get_archetype_data.sh          # fetch/derive the training sources
bash scripts/vcf/train_vcf_library.sh --force   # -> artifacts/vcf_models/*.zlc
scripts/vcf/vcfzl archetypes                    # confirm 8/8 ready
```

Each archetype trains several OpenZL profiles (`csv`, `lz`) and keeps whichever
compresses that archetype's own chunks smallest; the winner is recorded in
`artifacts/vcf_models/<id>.profile`.
