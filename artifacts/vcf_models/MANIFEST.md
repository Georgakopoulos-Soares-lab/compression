# VCF model library

The fixed set of OpenZL compressors that `scripts/vcf/vcfzl` dispatches to.
There is **no runtime training and no silent fallback** — `vcfzl compress`
requires `--archetype <id>`, that id must be in `archetypes.tsv`, and
`<id>.zlc` must exist here or the command errors out.

## Archetypes

| id | selector | trained from |
|----|----------|--------------|
| `sites-annotated` | no FORMAT column, many INFO keys | ClinVar-class annotation VCF |
| `sites-frequency` | no FORMAT column, few INFO keys | gnomAD/dbSNP sites VCF |
| `single-sample` | 1 sample column | one germline sample (GATK/DeepVariant) |
| `gvcf` | `##ALT=<ID=NON_REF>` or many `<NON_REF>` ALTs | single-sample gVCF |
| `somatic` | exactly 2 sample columns | tumor/normal Mutect2 VCF |
| `family-trio` | 3–20 sample columns | 1000G trio |
| `cohort` | 21–999 sample columns | joint-genotyped cohort |
| `panel` | ≥1000 sample columns | 1000G phase-3 panel |

Column count alone is a weak key (e.g. an 8-column ClinVar vs an 8-column
gnomAD-sites file compress very differently), which is why the selector also
looks at the header and the genotype layout. `vcfzl classify <file>` prints
its best guess; the user still passes `--archetype` explicitly.

## Building / rebuilding

```
scripts/vcf/train_vcf_library.sh            # train every archetype with a source on disk
scripts/vcf/train_vcf_library.sh --force    # retrain all
scripts/vcf/vcfzl archetypes                # show which are ready vs MISSING
```

Each model trains with `zli train --profile csv --profile-arg '\t'` on the
first 5 body chunks (~200 MiB) of its source VCF. To add a still-missing
archetype: obtain a representative VCF, set its path in `archetypes.tsv`
(replacing the `TODO:` note), and re-run the training script.

## Decompression does not need this library

`.vcfz` archives embed the OpenZL graph in each part, so `vcfzl decompress`
only needs `zli` + `tools/vcf_postprocess`. The archetype recorded in the
archive is informational (shown by `vcfzl inspect`).
