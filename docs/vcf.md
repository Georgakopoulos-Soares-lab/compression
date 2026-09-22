# VCF — `tools/nyx_vcf`

> These are the VCF codec's own flags and archive names. Most users want the
> single entry point instead: `nyx compress <file>` writes one `.nyx` file and
> `nyx decompress` restores it. See the top-level README.

Byte-exact VCF compression. A reversible, format-aware transform rewrites the
file into homogeneous streams; a fixed set of pre-trained OpenZL graphs
compresses them. **No training happens on your data.**

## Use

```bash
tools/nyx_vcf compress   calls.vcf calls.nvcf            # plain, .gz and .bgz input
tools/nyx_vcf compress   calls.vcf calls.nvcf --verify   # + prove the round trip
tools/nyx_vcf decompress calls.nvcf restored.vcf
tools/nyx_vcf inspect    calls.nvcf
tools/nyx_vcf stats      calls.vcf                       # where the bytes go, per stream
```

Options: `--threads N`, `--block-mb N` (default 64), `--models DIR`,
`--no-selfcheck`, `--verify`, `--quiet`.

Input format is detected from magic bytes, not the file extension.

## What the transform does

The body is cut into independently-decodable blocks. Each block is parsed into
named streams, and each stream is compressed on its own:

| stream | contents |
|---|---|
| `chrom` `id` `ref` `alt` `qual` `filter` | one value per variant, NUL-separated |
| `pos.delta` | zigzag varint delta of POS; `pos.exc` holds any POS whose text is not a canonical integer |
| `info.keyset` / `info.dict` | the ordered `(key, has '=')` pattern of each INFO field |
| `info.v.<KEY>` | one value stream per INFO key |
| `fmt.id` / `fmt.dict` | the FORMAT string of each variant |
| `gt.runlen` `gt.runsym` `gt.nruns` | the genotype matrix after a **haplotype-level positional Burrows-Wheeler transform**, run-length coded |
| `gt.sep` `gt.exc` | phasing, ploidy, and any GT that did not parse |
| `fs.<fmt>.<j>` | one column-major stream per non-GT FORMAT subfield |

Value streams are then rewritten by whichever of these applies, recursively:

- **integer** — all values canonical unsigned integers → fixed-width numeric, or
  zigzag varint self-deltas where those are smaller (monotone keys such as `END`)
- **decimal** — fixed-point decimals → sign / integer part / fraction length /
  fraction value (allele frequencies)
- **integer list** — comma-separated integers → element counts + element values
  (`AD`, `ADALL`, `PL`)
- **annotation** — VEP/SnpEff `CSQ`/`ANN`-style records → one stream per field
  position, across all records
- **dictionary** — low-cardinality text → dictionary + index stream

### Why PBWT

The genotype matrix is essentially the whole file for a cohort or panel VCF. A
haplotype-level PBWT sorts haplotypes by their reversed prefix at every variant,
which puts identical-by-descent haplotypes next to each other and turns each
row into a handful of long runs. On a 200k-variant x 2504-sample slice of 1000G
chr22 the genotype block alone goes from 2.00 GB to 3.2 MB.

Sample-level PBWT (permuting samples rather than haplotypes) is roughly half as
good, and no PBWT at all costs about 4x.

## Exactness

Every block is decoded again in-process and compared against the source bytes
before it is accepted. A block that does not reproduce exactly is stored
verbatim instead, so the transform is exact on **any** input, including ones it
models badly. `inspect` reports how many blocks took that path.
`--no-selfcheck` turns the check off; `--verify` additionally decompresses the
finished archive and compares it to the input.

Soft cases that round-trip: empty fields, trailing tabs, CRLF, a missing final
newline, `.` in any column, duplicate INFO keys, flag-only INFO keys, samples
carrying fewer FORMAT subfields than declared, mixed ploidy, unphased and
mixed-separator genotypes, and multi-allelic sites.

## Archive format

```
magic "NYXVCF\0" + version
nsamples, ncolumns, header length
header frame                      the ## and #CHROM lines
block payloads                    one length table + one OpenZL frame per stream
block index                       per block: compressed size, raw size, stream count, kind
trailer                           index offset, original size
```

Decompression needs no models: OpenZL frames are self-describing. The shipped
models only affect how well compression does, never whether decompression works.

## Regenerating the models (maintainers)

Not needed to use the tool.

```bash
scripts/vcf/train_nyx_vcf.sh          # -> artifacts/nyx_vcf_models/<class>.zlc
```

Streams are grouped into about a dozen **classes** (`gt.runlen`, `text`,
`varint`, `idx`, ...) and one compressor is trained per class over a corpus
spanning every archetype. Classes never name a particular INFO or FORMAT key,
so a file carrying keys the trainer never saw still routes every stream to a
shipped model. A class with no model, or a trained graph that rejects a stream,
falls back to OpenZL's generic profile — costing ratio, never correctness.

## Benchmarking

```bash
scripts/vcf/benchmark_nyx_vcf.sh --threads 16 --out results/vcf_bench.csv
```

No row is reported unless the round trip is byte-for-byte identical. See
`docs/vcf-competitors.md` for why GTShark and VCFShark are not in the table.
