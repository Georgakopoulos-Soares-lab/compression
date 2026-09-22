# NYX — format-aware compression for omics files

NYX losslessly compresses **FASTA, FASTQ, VCF and BED** files. It rewrites each
file into homogeneous streams with a reversible, format-specific transform, then
compresses those streams with pre-trained [OpenZL](https://github.com/facebook/openzl)
graphs. **Nothing is trained on your data**, every round trip is byte-exact, and
one input file always produces exactly one output file, however many threads it
uses.

```bash
nyx compress reads.fastq          # -> reads.fastq.nyx
nyx decompress reads.fastq.nyx    # -> reads.fastq, byte for byte
```

## Install

Requirements: Linux, `g++` with C++17 (GCC 9 or newer), `make`, `git`, `zlib`,
and Python 3.8+ only for the plotting and benchmark scripts.

```bash
git clone https://github.com/Georgakopoulos-Soares-lab/compression
cd compression
bash scripts/build_all.sh
```

That fetches a pinned OpenZL (0.2.5), patches it, builds it, and builds all four
codecs. It takes a few minutes. Check it with:

```bash
./nyx test <any FASTA, FASTQ, VCF or BED file>
```

which compresses, decompresses, compares against the original and keeps nothing.
Put `nyx` on your `PATH` with a symlink if you like — it finds its own directory:

```bash
ln -s "$PWD/nyx" ~/.local/bin/nyx
```

## Use

One command handles every format. The format is read from the file's **content**,
not its name, so a VCF called `.txt` is still a VCF and gzip/bgzip input is read
through transparently.

```bash
nyx compress   calls.vcf                    # -> calls.vcf.nyx
nyx compress   genome.fa.gz archive.nyx     # explicit output name
nyx compress   reads.fastq --verify         # decompress and compare before exiting
nyx compress   peaks.narrowPeak --threads 8 --max-mem-mb 2000
nyx decompress calls.vcf.nyx                # -> calls.vcf
nyx info       calls.vcf.nyx                # what this file is
nyx test       calls.vcf                    # round-trip check, keeps nothing
```

| option | meaning |
|---|---|
| `--threads N` | worker threads (default: every core) |
| `--max-mem-mb N` | memory budget in MB. Defaults: 4000 for FASTA and BED, 500 for FASTQ; VCF uses 64 MB blocks unless a budget is given |
| `--verify` | decompress the new archive and compare it with the input |
| `--force`, `-f` | overwrite an existing output file |
| `--format F` | `vcf`, `fasta`, `fastq` or `bed`, if detection gets it wrong |
| `--quiet`, `-q` | print nothing unless something fails |

Peak memory follows the budget, not the size of the input: an 18.4 GB VCF
compresses inside 1.3 GB at `--max-mem-mb 2000`. The per-format tools can also be
called directly — `tools/nyx_vcf`, `tools/nyx_bed`, `scripts/fastazl`,
`openzl/nyxfqz_v2` — which is what the benchmark scripts do.

## What it gives you

One 16-core node, whole files, every row paired with a full decompression and a
byte comparison. The CSVs behind this are in `results/`.

| input | size | NYX | best other codec |
|---|---|---|---|
| 1000 Genomes chr20, panel VCF | 18.4 GB | **468.7×** | xz -9e 232.3× |
| 1000 Genomes chr22, panel VCF | 11.2 GB | **431.8×** | xz -9e 214.6× |
| GIAB HG004, single-sample VCF | 2.8 GB | **52.2×** | xz -9e 38.7× |
| ClinVar, sites VCF | 1.9 GB | **16.9×** | xz -9e 15.4× |
| ChromHMM segmentation, BED | 53 MB | **30.2×** | xz -9e 13.4× |
| 34-file BED corpus, all dialects | 1.17 GB | **9.05×** | xz -9e 6.19× |
| ERR9539086, FASTQ | 8.4 GB | **7.87×** | SPRING 7.50× |
| GRCm39, FASTA | 2.8 GB | 4.82× | NAF -20 5.00× |

Where each format stands, stated plainly:

- **VCF** — the highest ratio of any codec tested on 9 of 10 files, up to 2.0× the
  best general-purpose codec, at 14–37× the speed of `zstd -19`.
- **BED** — the highest ratio on all 34 files, 1.2–2.3× the best general-purpose
  codec. On files under ~50 MB, `xz` with a blocked stream compresses faster.
- **FASTQ** — a better ratio than every general-purpose codec on all six runs, and
  than SPRING on the libraries without duplicate reads, at 2–3× SPRING's speed and
  a third of its memory. On libraries with many duplicate reads (small-RNA,
  amplicon) SPRING compresses 1.3–2.0× smaller; `scripts/fastq/duplication_profile.sh`
  tells you which case a library is, in seconds, before you compress it.
- **FASTA** — NAF at its strongest setting compresses 3–5% smaller on mammalian
  assemblies, and much smaller on bread wheat. NYX gets within 3–5% of it while
  compressing 57–73× faster, and beats every general-purpose codec on ratio and
  speed at once.

Across all 54 files benchmarked, **no codec produced a smaller file than NYX in
less time**. The tools that do compress smaller take 1.2× to 73× longer.
`python3 src/multi_axis.py --results results` regenerates that analysis per file.

## Exactness

Every block is decoded again in-process and compared with its source before the
encoder accepts it; anything that does not reproduce exactly is stored verbatim.
An empty file, a byte-order mark, CR-only line endings, ragged columns, or a file
that is not the format at all is stored rather than refused or mangled, so the
tool is total: any file goes in and the same bytes come out.

```bash
bash tests/test_vcf_roundtrip.sh   # 155 cases
bash tests/test_seq_roundtrip.sh   # 36 FASTA/FASTQ cases
bash tests/test_bed_roundtrip.sh   # 49 BED cases
```

## Models

The shipped models are in `artifacts/`. They are trained offline, by us, on
public data that is **not** in the benchmark above, and are never retrained at
compression time — see `artifacts/fastq_models/MANIFEST.md` for the FASTQ
accessions and `scripts/*/train_*.sh` for how each set is produced. A shipped
model is used for a stream only when it beats OpenZL's untrained profile on that
stream, measured per stream, so a file unlike anything in the training data
falls back rather than paying for the mismatch.

## Reproducing the paper

```bash
bash scripts/vcf/download_vcf.sh            # corpora (large)
bash scripts/vcf/benchmark_nyx_vcf.sh --threads 16
bash scripts/vcf/ablation_nyx_vcf.sh  --threads 16
bash scripts/benchmark_fasta.sh       --threads 16
bash scripts/fastq/benchmark_fastq.sh --threads 16
bash scripts/bed/benchmark_bed.sh     --threads 16
python3 src/plot_paper_figures.py --results results --out plots/paper
python3 src/make_paper_tables.py  --results results --out results/paper_tables.md
```

Figures and tables read from `results/*.csv`, so they regenerate without
re-running a single benchmark.

## Layout

```
nyx                          single entry point, format auto-detected
tools/nyx_vcf.cpp            VCF codec  (haplotype PBWT, per-key streams, OpenZL)
tools/nyx_bed.cpp            BED codec  (per-column encodings, cross-column references)
tools/nyx/nyxfqz_v2.cpp      FASTQ codec (identifier/sequence/quality streams)
tools/biocompress_preprocessor.cpp, tools/fasta_postprocess.cpp, scripts/fastazl
                             FASTA codec (FAV5 packed transform)
tools/nyx_stream.h           stream container shared by the VCF and BED codecs
artifacts/                   shipped models
scripts/                     build, download, training and benchmark drivers
results/                     benchmark CSVs; every figure and table reads from here
tests/                       edge-case and hostile-input round-trip suites
docs/                        per-format notes and what each competing tool preserves
```

## Citing

Patsakis M, Margaris A, Chronopoulos T, Mouratidis I, Georgakopoulos-Soares I.
*Byte-exact, format-aware compression for FASTA, FASTQ, BED and VCF files.*
(manuscript in preparation)

## License

NYX is released under the [PolyForm Noncommercial License 1.0.0](LICENSE.md):
free to use, modify and redistribute for research, teaching and any other
noncommercial purpose, including at universities, hospitals, public research
institutes and government bodies, whatever the source of their funding. For any
other use, write to the corresponding authors.

OpenZL is fetched at build time and carries its own license.
