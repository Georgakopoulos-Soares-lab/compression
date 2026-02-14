Top 3 Opportunities (ranked by market size x feasibility)
1. Parquet / Columnar Data Lakes — the best opportunity
Why this is perfect:
Parquet is the universal format for analytics data. Every data warehouse, every ML pipeline, every data lake uses it. Databricks, Snowflake, AWS Athena, BigQuery, Spark, Trino, dbt — all Parquet.
Here's the punchline: Parquet already does what SDDL does. It decomposes structured data into typed columns. It stores schema metadata in the file. It knows every column's type (INT64, FLOAT, STRING, BOOLEAN). But then it compresses each column with generic compressors — snappy, zstd, gzip. It throws away the type information at the compression layer.
OpenZL could slot in as a Parquet compression codec that actually uses the type information. You wouldn't even need SDDL — Parquet's schema metadata maps directly to OpenZL's typed streams. Train on a sample of column pages, deploy the trained compressor, and every subsequent Parquet file gets 2x+ better compression.
The money:
Company stores 1 PB of Parquet in S3 at \$0.023/GB = \$23K/month
You reduce storage by 50% = \$11.5K/month savings — just on storage
Add query acceleration (less data to scan = faster queries = less compute)
Add cross-region replication savings, data transfer costs
A large enterprise with 10 PB saves \$1.4M/year on storage alone
Go-to-market:
Build an open-source Apache Arrow / Parquet codec backed by OpenZL
Benchmark on TPC-H and real-world datasets — show 2x over zstd
The OSS codec is the growth engine — free, easy to try, builds trust
Monetize with a training service: an automated system that samples your data lake, trains optimal compressors per table/partition, and deploys them. This is the product — the intelligence layer on top of the codec
Pricing: percentage of savings (aligned incentives) or per-TB managed
Competitive moat: Nobody else does trained, schema-aware compression for columnar data. Zstd/snappy are generic. Custom encodings in Parquet (delta, RLE, dictionary) are hardcoded and not learned. OpenZL's training system is genuinely differentiated.
2. Kafka / Event Streaming — fastest path to revenue
Why this works:
Kafka is the backbone of real-time data at every large tech company. LinkedIn processes 7+ trillion messages per day. Confluent Cloud charges by throughput and storage.
The key: Kafka already has a Schema Registry. Every Avro or Protobuf message has a known schema. Messages within a topic are homogeneous — millions of messages all sharing the same structure. This is the ideal scenario for OpenZL: train once on sample messages, compress all subsequent messages with the trained compressor.
Right now, Kafka uses generic compression (snappy, lz4, zstd) at the broker or producer level. It doesn't exploit the schema. That's the gap.
The money:
A company pushing 100 TB/day through Kafka at \$0.10/GB = \$300K/month
Reduce by 50% = \$150K/month savings
You charge 20% of savings = \$30K/month = \$360K/year per customer
10 enterprise customers = \$3.6M ARR
Go-to-market:
Build a Kafka serializer/deserializer (SerDe) that reads schemas from Confluent Schema Registry and compresses with OpenZL
Drop-in replacement — no application code changes
Target Confluent Cloud heavy users (they're already cost-sensitive about throughput)
Demo: "Install our SerDe, same topic, 50% less data, no code changes"
Risk: Latency. Kafka producers care about p99 latency. OpenZL compression must be fast enough for real-time message production. This is an engineering challenge but solvable — OpenZL's compression speeds are already competitive with zstd.
3. ML Training Data — the narrative play
Why this matters:
AI companies are spending insane amounts on infrastructure. GPU costs dominate, but storage and I/O are the #2 bottleneck. Training datasets are massive (tens to hundreds of TB), stored in structured formats (Parquet for feature stores, TFRecord for training pipelines, HDF5 for scientific ML), and iterated on constantly.
The bottleneck in ML training is often data loading — GPUs sit idle waiting for data. Better compression helps two ways: less storage cost AND faster I/O (less data to read from disk/network = faster data loading = higher GPU utilization).
The pitch: "We make AI training data 2x smaller and 20-30% faster to load."
The money:
AI company spends \$5M/month on GPU compute
15% of that is wasted on I/O stalls = \$750K/month wasted
You reduce I/O time by 30% = \$225K/month recovered
Plus storage savings on the training data itself
High willingness to pay — these companies are VC-funded and cost-insensitive on tools that improve training speed
Go-to-market:
Build an OpenZL-backed data loader for PyTorch (replaces gzip/zstd in DataLoader/DataPipe)
Benchmark on public ML datasets (ImageNet metadata, Common Crawl features, LibriSpeech)
Publish results showing faster epoch time due to less I/O
Target ML infrastructure teams at mid-stage AI companies (Series B-D)
Synergy: This overlaps heavily with opportunity #1 (Parquet), since most ML feature stores use Parquet.
The Business Model I'd Actually Build
If I were building this company, here's what I'd do:
The product is not a compressor. The product is a compression intelligence platform.
The open-source piece is the codec — an OpenZL plugin for Parquet, Kafka, and ML data loaders. Free, high quality, builds adoption.
The paid product is the training and optimization layer:
Automatically profiles your data (samples tables, topics, datasets)
Trains optimal compressors per data source
Deploys and manages compressor lifecycle (retrain when data distribution shifts)
Dashboard showing: storage saved, query acceleration, cost reduction
This is a SaaS product, charged on data managed or savings delivered
Phase 1 (0-6 months): Open-source Parquet codec + benchmark suite. Prove the 2x claim. Get stars, blog posts, conference talks.
Phase 2 (6-12 months): Training service MVP. Target 3-5 design partners with large data lakes. Prove ROI on real workloads.
Phase 3 (12-18 months): Add Kafka SerDe and ML data loader. Expand to streaming and ML use cases. Launch cloud-hosted training service.
Phase 4 (18+ months): Enterprise product with automated compressor management, monitoring, and optimization. This is where the real revenue scales.
What I'd challenge you on
A few hard questions you need to answer before going further:
Licensing. OpenZL is BSD-licensed by Meta. You can build on it freely. But Meta could also build everything I described above. What's your moat beyond execution speed? The answer needs to be the training automation and the integration layer — not the compression engine itself.
Decompression compatibility. For the Parquet play, every query engine (Spark, Trino, DuckDB, etc.) needs to decompress your files. OpenZL's universal decompressor helps, but you'd still need to distribute it as a codec plugin to every engine. This is an ecosystem challenge, not a technical one.
The cold start. OpenZL needs training data to be effective. On first use, before training, it's no better than zstd. You need a smooth onboarding path: auto-sample, auto-train, auto-deploy — with zero manual schema writing.
The Parquet question. Can OpenZL actually beat zstd on Parquet column pages? Parquet already applies dictionary encoding, RLE, and delta encoding before compression. The remaining data after those encodings might not have much structure left for OpenZL to exploit. This needs to be benchmarked before committing to the strategy. It's the single highest-risk technical question.
What resonates? Want to dig deeper into any of these, or should we start scoping the Parquet codec proof-of-concept?