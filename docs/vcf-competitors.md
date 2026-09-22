# VCF competitors: what each tool actually preserves

Every claim below was checked by decompressing and running `cmp` against the
input, not by trusting an exit status. Input: a 200 000-variant x 2504-sample
slice of 1000 Genomes phase 3 chr22 (2 032 176 394 bytes), plus a
1000-variant cut of the same file.

## Summary

| tool | whole-VCF? | byte-exact? | usable in our table |
|---|---|---|---|
| gzip / pigz -9 | yes | yes | yes |
| zstd -19 --long=27 | yes | yes | yes |
| xz -9e | yes | yes | yes |
| bcftools BCF (`-Ob -l9`) | yes | **no** — different container | as a format baseline only |
| GTShark `compress-db` | **no** | no | genotype-only comparison |
| VCFShark | intended yes | **crashes** | not currently |
| Genozip | yes | not verified here | licence-gated; see below |

## GTShark

`gtshark compress-db in.vcf out` round-trips POS and the genotype matrix and
**discards CHROM, REF, ALT, QUAL, FILTER and INFO**, writing `.` in their place:

```
input : 22  16050075  .  A  G  100  PASS  AC=1;AF=0.000199681;...  GT  0|0 ...
output:     16050075  .  .  .  .    .     .                        GT  0|0 ...
```

That is by design — it is a genotype-database compressor, and the manual
describes it that way. Its output was 4 430 818 bytes for that input, which
looks like 459x but is not a whole-file VCF ratio and must not be presented as
one. If GTShark appears in the paper it has to be on a genotype-only task with
NYX given the same reduced content.

## VCFShark

VCFShark 1.1 **segfaults** on this input on both toolchains available here
(gcc 9.4.0 and gcc 12.2.0), with and without its vendored prebuilt jemalloc:

- 1000-variant file: `Segmentation fault (core dumped)`, an 8 KB archive, and
  `Corrupted archive!` on decompression.
- The full 2 GB file: exits 0, but the round trip drops REF/ALT/QUAL and
  reorders INFO keys — consistent with a partial write rather than a design
  choice.

No VCFShark number is reported until this is resolved. Build notes are in
`../logs/build_thirdparty*.sh`.

## bcftools / BCF

BCF is a different container, so a byte comparison against the source VCF is
meaningless. It is included as the standard-of-practice baseline in the field,
labelled as such, and its ratio is computed against the same decompressed VCF
as every other row.

## Genozip

Genozip is source-available but licence-gated, and the numbers presently in the
manuscript were taken with an earlier harness on a different copy of the inputs.
They are not comparable with anything measured here and are excluded until
re-measured on this machine, with this corpus, under the same round-trip check.
