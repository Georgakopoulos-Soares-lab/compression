# DNS TSV Compression — C++ Tools

In-process compression/decompression of DNS TSV files using a trained OpenZL compressor.
Achieves **~5.7x** compression ratio on `nom-dns-vertica` data (vs zstd-9's ~4x, snappy's ~2.2x).

## Prerequisites

```bash
brew install snappy zstd     # for benchmark only
```

OpenZL must be built first:
```bash
cd nyx/openzl && make zli -j8
```

## Build

```bash
cd dns
make all
```

## Usage

### Compress
```bash
./dns_compress input.tsv output.zl ../nyx/models/lossless_dns/dns_csv.zl_compressor
```

### Decompress
```bash
./dns_decompress output.zl roundtrip.tsv
```

### Benchmark
```bash
./benchmark <data_dir> <compressor> [--runs 3] [--sizes 1,2,5,10,20,50]

# Example:
./benchmark ../data/DNS/training_clean ../nyx/models/lossless_dns/dns_csv.zl_compressor
```

## Trained Compressor

The trained compressor is at `nyx/models/lossless_dns/dns_csv.zl_compressor` (13 KB).
It was trained on 5x50 MB files from different Kafka partitions with full ACE exploration (~30 min).

To retrain:
```bash
# 1. Prepare clean training data (strip empty lines)
mkdir -p data/DNS/training_clean
for f in data/DNS/dns-capture-vertica/nom-dns-vertica_p*_0001.tsv; do
  grep -v '^[[:space:]]*$' "$f" > "data/DNS/training_clean/$(basename $f)"
done

# 2. Train
nyx/openzl/zli train data/DNS/training_clean \
  --output nyx/models/lossless_dns/dns_csv.zl_compressor \
  --profile csv --profile-arg $'\t' \
  --use-all-samples --threads $(sysctl -n hw.ncpu) \
  --max-time-secs 1800 --force
```

## Architecture

- **No Python wrapper** — direct C++ calls to OpenZL's `CCtx`/`DCtx` API
- **No subprocess overhead** — compressor loaded in-process via `createCompressorFromSerialized()`
- **Empty line stripping** — batch separators from `nom-kafka-dump` are stripped before compression
- **Benchmark** uses `snappy::Compress()` and `ZSTD_compress()` in-process for fair comparison
