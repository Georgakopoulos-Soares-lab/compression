# `fasta_packed_v5.sddl` — the FAV5 container description

Describes the lossless packed-FASTA container produced by
`tools/biocompress_preprocessor <in> <outdir> <threads> fasta_packed`, so OpenZL
**splits it into its component streams** instead of compressing it as one opaque
byte blob.

> **Do not add comments to the `.sddl` file.** The SDDL v1 parser in OpenZL
> 0.2.5 rejects `#` comment lines and inline comments — the failure surfaces as a
> confusing `A1CBOR "writeFailed"` / *Corruption detected* error rather than a
> parse error. Keep the schema comment-free and document it here.

Use it with:

```bash
openzl/zli compress <chunk> --profile sddl --profile-arg schemas/fasta_packed_v5.sddl -o out.zl
```

`scripts/fasta_compress_chunks.sh` accepts `sddl:<schema>` as a pseudo-model and
does exactly this.

> SDDL can **compress** on OpenZL 0.2.5 but cannot be **trained** — 
> `zli train --profile sddl` fails serializing the trained graph (same A1CBOR
> error). So it is evaluated as an *untrained* candidate against the trained
> `lz` / `serial` models in `scripts/train_fasta_model.sh`, and the winner is
> recorded in `artifacts/fasta_model.profile`.

## Fields

| field | type | meaning |
|---|---|---|
| `magic` | `Byte[4]` | `"FAV5"` |
| `flags` | `Byte` | bit0 = this chunk ends with `\n` |
| `reserved` | `Byte[3]` | padding |
| `preamble_len` | `U32` | bytes before the first `>` |
| `num_records` | `U32` | FASTA records in this chunk |
| `n_seqpos` | `U64` | sequence positions (bases + exception bytes) |
| `n_base` | `U64` | positions that are `[ACGTacgt]` |
| `n_caseruns` | `U64` | upper/lower RLE runs over base positions |
| `n_excruns` | `U64` | exception runs (N, IUPAC, `\r`, …) |
| `n_linelens` | `U64` | `== sum(rec_nlines)` |
| `hdr_bytes` | `U64` | `== sum(hdr_lens)` |
| `preamble` | `Byte[preamble_len]` | leading bytes, verbatim |
| `hdr_lens` | `U32[num_records]` | header length per record |
| `rec_nlines` | `U32[num_records]` | sequence lines per record |
| `line_lens` | `U32[n_linelens]` | bytes per sequence line |
| `case_runs` | `U64[n_caseruns]` | case-mask run lengths |
| `exc_gaps` / `exc_lens` / `exc_bytes` | `U64[]` / `U64[]` / `Byte[]` | exception runs |
| `packed2bit` | `Byte[(n_base + 3) / 4]` | ACGT at 2 bits/base |
| `headers` | `Byte[hdr_bytes]` | header text, verbatim |

Keep this in sync with `process_fasta_packed_chunk()` in
`tools/biocompress_preprocessor.cpp`.

## Measured effect

On a FAV5 chunk from GRCm39 (mouse), untrained:

| profile | compressed | ratio |
|---|---|---|
| `serial` | 3 953 kB (chunk 6 480 kB) | 1.56× |
| **`sddl`** | **3 884 kB** | **1.67×** |

The gain comes from `packed2bit` reaching an LZ codec and `line_lens` (nearly
all the same value) collapsing, instead of everything sharing one generic
entropy stage.
