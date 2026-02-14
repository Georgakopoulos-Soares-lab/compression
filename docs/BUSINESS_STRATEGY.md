# Business Strategy: Building an Enterprise Around Schema-Aware Compression

> **Status:** Working draft for internal discussion
> **Last updated:** 2026-02-13
> **Context:** Strategic analysis of how to productize OpenZL-based compression technology into a venture-scale business

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [The Core Insight](#2-the-core-insight)
3. [Market Opportunity](#3-market-opportunity)
4. [Target Markets](#4-target-markets-ranked)
5. [The Product Vision](#5-the-product-vision)
6. [Business Model](#6-business-model)
7. [Go-to-Market Strategy](#7-go-to-market-strategy)
8. [Product Roadmap](#8-product-roadmap)
9. [Competitive Landscape](#9-competitive-landscape)
10. [Key Risks and Mitigations](#10-key-risks-and-mitigations)
11. [Financial Model](#11-financial-model)
12. [Open Technical Questions](#12-open-technical-questions)
13. [Next Steps](#13-immediate-next-steps)

---

## 1. Executive Summary

We are building an **enterprise data compression platform** powered by Meta's OpenZL framework. Unlike generic compressors (gzip, zstd, lz4) that treat all data as opaque byte streams, our technology exploits the *structure* of data — its schema, types, and patterns — to achieve 2-3x better compression ratios while maintaining high throughput.

**The thesis:** In the era of AI, enterprise data is growing exponentially, is overwhelmingly structured (Parquet, Avro, Protobuf, JSON), and is compressed with tools that ignore its structure. A platform that makes compression intelligent — by automatically learning optimal compression strategies from data schemas and samples — can deliver massive, measurable cost savings to every data-intensive organization.

**This is not an open-source library play.** The open-source compression engine is the technology foundation. The business is a **managed compression intelligence platform** that automates the entire lifecycle: profiling data, training optimal compressors, deploying them across infrastructure, monitoring compression effectiveness, and retraining as data evolves. We sell outcomes (storage reduced, queries accelerated, bandwidth saved), not software.

---

## 2. The Core Insight

### Why generic compression leaves money on the table

Every enterprise compresses its data. S3 objects, Kafka messages, database pages, Parquet files, API responses — all compressed. But virtually all of it uses generic compressors: zstd, gzip, snappy, lz4. These tools treat data as unstructured byte streams, looking for repeated patterns without understanding what the bytes represent.

Meanwhile, the applications producing this data *know* its structure intimately. A Kafka producer knows every message is an Avro record with specific typed fields. A Spark job knows it's writing Parquet columns of INT64 timestamps and FLOAT32 sensor readings. A web server knows its API responses are JSON with a fixed schema.

**This structural knowledge is discarded at the compression layer.** That's the gap.

### What OpenZL does differently

OpenZL takes a description of data structure and builds from it a *specialized compressor* optimized for that specific format. It decomposes structured data into typed streams (integers, floats, strings, booleans), applies type-appropriate transforms (delta coding for timestamps, dictionary encoding for categoricals, bitpacking for flags), and then compresses each stream with codecs tuned to its statistical properties.

The result: **2-3x better compression ratios than generic compressors**, with comparable or better throughput, verified on real-world data:

| Compressor | Ratio on Genomic Data (FASTA) | Notes |
|------------|-------------------------------|-------|
| gzip -1 | 2.8x | Generic, fast |
| zstd -9 | ~3.0x | Generic, good ratio |
| **Our pipeline (OpenZL + schema)** | **4.4x** | Schema-aware, trained |

The key innovation is that OpenZL is *trainable*. Given a schema and sample data, its trainer automatically searches for the best compression graph — the optimal combination of transforms and codecs for each data stream. This trained compressor can then be applied to all future data of the same type.

### Where the schema already exists

The critical strategic insight: **we don't need to create schemas for the world's data formats.** The highest-value markets are ones where the schema already exists as part of the infrastructure:

| Infrastructure | Schema Source | Data Volume |
|---------------|-------------|-------------|
| Apache Parquet / Iceberg / Delta Lake | Embedded in file metadata | Petabytes per enterprise |
| Kafka + Confluent Schema Registry | Avro / Protobuf schemas in registry | Trillions of messages/day industry-wide |
| Protobuf / gRPC services | `.proto` definition files | Billions of RPCs/day |
| SQL databases | Table DDL / column types | Universal |
| ML feature stores | Feature schemas / data catalogs | Tens of TB per AI company |
| IoT / telemetry pipelines | Device manifest / sensor schemas | Growing exponentially |

In each of these, the schema is a first-class citizen of the data infrastructure. Our platform reads these existing schemas, translates them into compression configurations, trains optimal compressors on sample data, and deploys them — all automatically, with zero manual schema engineering.

---

## 3. Market Opportunity

### The numbers

| Market | 2025 Size | Projected Growth | Source |
|--------|-----------|-----------------|--------|
| Cloud storage | $145B | $426B by 2030 (24% CAGR) | GII Research |
| Public cloud spending (total) | $723B | 21.5% YoY growth | Gartner |
| Data compression software | $1.2B | $4.3B by 2032 (7.2% CAGR) | Consegic Business Intelligence |
| Data streaming platforms | Growing rapidly | 90% of IT leaders increasing investment | Confluent 2025 Report |

### Why now?

Three converging trends make this the right moment:

**1. AI is multiplying data volumes.** Storage footprints for AI training runs are scaling from 30 TB in 2025 to 100 TB by 2030 per single training run. Generative AI workloads are the primary accelerant of cloud growth. More data means more storage cost, more network cost, more I/O bottlenecks — all of which compression directly addresses.

**2. Storage I/O is the new bottleneck.** GPU performance increased 225x from 2015-2025, but storage IOPS only doubled. As compute becomes cheaper and faster, the data pipeline — reading, transferring, and writing data — becomes the limiting factor. Better compression directly reduces I/O volume, making the entire pipeline faster.

**3. Structured data is dominant.** The rise of data lakehouses (Iceberg, Delta Lake, Hudi on top of Parquet), schema registries (Confluent, AWS Glue), and typed serialization formats (Protobuf, Avro, Arrow) means that enterprise data is increasingly self-describing. The schema information needed for intelligent compression is already present in the infrastructure.

### Addressable market sizing

Our initial target is enterprises spending >$100K/month on cloud data storage and streaming. A conservative estimate:

- **100 enterprise customers** in the first 3 years
- **Average 50% compression improvement** over generic compressors
- **Average data footprint of 500 TB** per customer
- **Average savings of $50K-$200K/year** per customer (storage + network + query acceleration)
- **We capture 20-30% of savings** as platform fees

This yields **$10M-$60M ARR** at steady state for the initial market segment, with significant expansion potential as we add more data formats and infrastructure integrations.

---

## 4. Target Markets (Ranked)

### Tier 1: Data Lakes and Analytics (Parquet / Iceberg / Delta Lake)

**Why this is #1:**

Apache Parquet is the universal storage format for analytical data. Every data warehouse, every ML pipeline, every data lake uses it. Databricks, Snowflake, AWS Athena, BigQuery, Spark, Trino, dbt — all built on Parquet. As of 2025, the data lakehouse architecture (Parquet + open table formats like Iceberg) has become the dominant enterprise data pattern.

Parquet is a **perfect technical fit** for OpenZL because:
- **The schema is already embedded in the file** — column names, types (INT32, INT64, FLOAT, DOUBLE, STRING, BOOLEAN), nesting structure, and encoding metadata are all present.
- **Data is already columnar** — Parquet separates data into typed column pages, which maps directly to OpenZL's typed-stream model.
- **The compression layer is pluggable** — Parquet's codec interface allows custom compressors (currently supports snappy, gzip, lz4, zstd, brotli). Adding an OpenZL codec is architecturally straightforward.
- **Training is natural** — sample a few row groups from existing files, train a compressor per column type, deploy for all future writes.

**The business case:**
- Enterprise stores 1 PB of Parquet in S3 Standard at ~$0.023/GB/month = **$23,000/month**
- We achieve 50% better compression = **$11,500/month savings** on storage alone
- Add query acceleration: smaller files = less data to scan = faster queries = less compute
- Add cross-region replication: less data replicated = lower transfer costs
- Add data egress: hyperscaler egress at $80-120/TB — smaller data = lower bills
- **Total savings for a 1 PB customer: $150K-$300K/year**

**Market reach:** Parquet is used by virtually every data-driven organization. The open table format ecosystem (Iceberg, Delta Lake, Hudi) is growing rapidly, and all of them sit on top of Parquet. Capturing even 1% of the data lake market represents a massive business.

### Tier 2: Event Streaming (Kafka / Confluent / Pulsar)

**Why this works:**

Kafka is the backbone of real-time data at every large tech company. Confluent Cloud charges ~$0.03/GB for throughput. Messages are overwhelmingly structured — Avro and Protobuf schemas stored in a Schema Registry. Messages within a topic are homogeneous (millions of messages sharing the same schema), making them ideal for trained compression.

**The technical fit:**
- Kafka already has a Schema Registry with full Avro/Protobuf schemas
- Messages per topic are highly homogeneous — train once, compress all
- Kafka supports custom serializers/deserializers (SerDe) — clean integration point
- Compression happens at the producer → all downstream consumers benefit

**The business case:**
- Company pushes 50 TB/day through Confluent Cloud at $0.03/GB = **$45K/month** in throughput alone
- We reduce message sizes by 50% = **$22.5K/month savings**
- Plus: reduced broker storage, faster replication, lower consumer lag
- **Total savings for a high-volume Kafka user: $200K-$500K/year**

**Market context:** 90% of IT leaders plan to increase data streaming investments in 2025. 87% say streaming platforms will increasingly feed AI systems with real-time data. This is a growing, well-funded market.

### Tier 3: AI/ML Data Pipelines

**Why this matters:**

AI companies are spending heavily on infrastructure. While GPU costs dominate, storage and I/O are the #2 bottleneck. GPU performance has outpaced storage performance by over 100x in the last decade, making data loading the critical path in training pipelines. GPUs sit idle waiting for data.

**The technical fit:**
- ML feature stores use Parquet (overlaps with Tier 1)
- Training datasets use TFRecord, HDF5, or custom binary formats — all structured
- Embeddings are typed arrays (FLOAT32/FLOAT16 vectors) — perfect for OpenZL's numeric codecs
- Checkpointing (saving model state) writes massive structured binary blobs

**The business case:**
- AI company spends $5M/month on GPU compute
- 10-15% wasted on I/O stalls = $500K-$750K/month
- Reducing I/O by 30% through better compression = **$150K-$225K/month recovered**
- Plus: storage savings on training datasets, embeddings, checkpoints
- These companies have high willingness to pay for anything that improves training throughput

**The pitch:** "We make your training data 2x smaller and your data loading 30% faster. Your GPUs spend more time computing and less time waiting."

### Tier 4: IoT / Telemetry / Time-Series

**Why it's interesting but later:**

Sensor data, device telemetry, operational metrics — all highly structured, highly repetitive, produced in enormous volumes. Autonomous vehicles generate 1-5 TB/day each. Industrial IoT deployments have millions of sensors. Edge bandwidth constraints make compression critical.

This is a large market but has higher integration complexity (edge devices, custom protocols, real-time constraints). Better suited as a later expansion after the core platform is proven on Tier 1/2.

---

## 5. The Product Vision

### What we are NOT building

We are **not** building:
- An open-source compression library (that's the engine, not the product)
- A collection of SDKs for different formats (that's a feature, not a business)
- A schema catalog (endless, no moat, no recurring revenue)

### What we ARE building

**A managed compression intelligence platform** that:

1. **Automatically discovers and profiles data** — connects to your data infrastructure (S3 buckets, Kafka clusters, databases), identifies data schemas, samples representative data, and characterizes compression opportunities.

2. **Trains optimal compressors** — uses OpenZL's training system to build specialized compressors for each data source. Manages the training pipeline: sampling, training, validation, benchmarking against baselines (zstd, snappy), and selection of the best compressor.

3. **Deploys compressors across your infrastructure** — provides integration points (Parquet codec, Kafka SerDe, database plugin, API proxy) that transparently apply the trained compressors. No application code changes required.

4. **Monitors and optimizes continuously** — tracks compression ratios, throughput, and cost savings in real time. Detects data drift (when new data no longer matches the trained compressor's profile) and automatically retrains. Provides a dashboard showing ROI: storage saved, queries accelerated, bandwidth reduced, dollars saved.

5. **Guarantees compatibility** — all compressed data is decompressible by the standard OpenZL decoder, which we distribute freely. No vendor lock-in on the data. If a customer leaves, their data is still readable.

### The experience for the customer

```
Day 1:  Connect the platform to your S3 bucket / Kafka cluster / database
Day 2:  Platform profiles your data, identifies top compression opportunities
Day 3:  Platform trains compressors, benchmarks them, shows projected savings
Day 7:  Deploy trained compressors (one-click or API)
Day 30: Dashboard shows: "42% storage reduction, $18,400 saved this month"
```

No schema writing. No code changes. No compression expertise required.

---

## 6. Business Model

### Revenue model: Consumption-based with committed tiers

The platform charges based on **data managed** — the volume of data flowing through our compression infrastructure. This aligns our revenue with the customer's data growth (which is always increasing).

| Tier | Data Managed | Price per TB/month | Minimum Commit |
|------|-------------|-------------------|----------------|
| Starter | Up to 50 TB | $5.00 | None (pay-as-you-go) |
| Growth | 50-500 TB | $3.50 | 1-year contract |
| Enterprise | 500 TB+ | $2.00 (negotiated) | 1-year contract, dedicated support |

**Alternative pricing model (savings-based):** Charge a percentage of demonstrated cost savings. More aligned with customer value but harder to measure precisely. Could be offered as an option alongside per-TB pricing.

### Why this works as a business (not just a library)

| Value Layer | Open Source (Free) | Platform (Paid) |
|------------|-------------------|-----------------|
| Compression engine | OpenZL core + universal decoder | - |
| Format codecs | Parquet codec, basic Kafka SerDe | - |
| Automatic profiling & training | - | Managed training pipeline |
| Deployment orchestration | - | One-click deploy, rolling updates |
| Monitoring & retraining | - | Drift detection, auto-retrain |
| Dashboard & ROI tracking | - | Real-time savings analytics |
| Multi-format management | - | Unified view across all data sources |
| Enterprise features | - | SSO, RBAC, audit logging, SLAs |
| Support & SLAs | Community | Dedicated, with SLA guarantees |

The open-source layer gives us distribution and trust. The platform layer gives us revenue and stickiness.

### Unit economics

- **Cost to serve:** Primarily compute for training (one-time per data source, amortized). Compression/decompression runs on the customer's infrastructure. Platform overhead is a lightweight control plane + monitoring.
- **Gross margin:** ~80%+ (SaaS-like, minimal COGS once platform is built)
- **Customer LTV:** High — compression is sticky infrastructure. Once deployed, it's painful to remove.
- **Churn:** Expected to be very low. Compression savings grow with data growth.
- **Expansion:** Natural — customers add more data sources, more formats, more clusters over time.

---

## 7. Go-to-Market Strategy

### Phase 1: Prove the technology (Months 1-4)

**Goal:** Demonstrate that OpenZL-based compression decisively beats generic compressors on real-world enterprise data formats.

**Actions:**
- Build an OpenZL compression codec for Apache Parquet (the highest-value integration)
- Benchmark on standard datasets (TPC-H, TPC-DS) and real-world Parquet files
- Target: demonstrate 1.5-2x better compression ratio than zstd-9 on Parquet column pages
- Publish benchmark results as a technical blog post / whitepaper
- Open-source the basic Parquet codec to build credibility

**Key technical question to answer:** Can OpenZL beat zstd on Parquet column pages, given that Parquet already applies dictionary encoding, RLE, and delta encoding before compression? This must be validated before committing to the strategy.

### Phase 2: Build the platform MVP (Months 4-8)

**Goal:** Build a minimal but functional platform that can profile, train, and deploy compressors for Parquet data in S3.

**MVP scope:**
- Connect to an S3 bucket, scan for Parquet files
- Profile: sample row groups, extract schemas, characterize data
- Train: run OpenZL training on sampled data, produce trained compressors
- Deploy: provide a Spark/Trino/DuckDB plugin that uses trained compressors for reading/writing
- Monitor: basic dashboard showing compression ratios and storage savings vs. baseline

**NOT in MVP:** Kafka integration, multi-cloud, auto-retraining, enterprise auth.

### Phase 3: Design partners (Months 8-14)

**Goal:** Deploy the platform with 3-5 design partners and prove ROI on real production workloads.

**Target design partners:**
- Mid-stage startups (Series B-D) with large data lakes (100 TB+)
- Data-intensive industries: fintech, healthtech, adtech, climate/energy
- Companies already frustrated with storage costs (ask: "what's your monthly S3 bill?")

**Success criteria:**
- Each design partner sees measurable storage cost reduction (target: 30%+)
- At least one partner agrees to a case study
- Identify the top 3 friction points in deployment (these become product priorities)

### Phase 4: Launch and scale (Months 14-20)

**Goal:** Launch commercially, close first 10 paying customers, establish product-market fit.

**Actions:**
- Launch self-serve Starter tier (low friction, pay-as-you-go)
- Launch Growth tier with 1-year commitments
- Add Kafka integration (Schema Registry → OpenZL SerDe)
- Add auto-retraining (detect data drift, retrain compressors automatically)
- Hire sales team focused on data platform and infrastructure buyers
- Speak at data conferences: Data + AI Summit (Databricks), Kafka Summit, re:Invent

### Phase 5: Expand (Months 20-30)

**Goal:** Multi-format, multi-cloud, enterprise-grade platform.

**Expansion vectors:**
- Additional formats: Kafka, TFRecord, HDF5, Protobuf archives
- Additional clouds: GCS, Azure Blob Storage (S3-first, then expand)
- Additional query engines: Snowflake external tables, BigQuery, Athena, Redshift Spectrum
- ML data loader integration (PyTorch, TensorFlow)
- Enterprise features: SOC2 compliance, SSO/SAML, audit logs, VPC deployment
- International expansion

---

## 8. Product Roadmap

### Summary timeline

```
Month 1-4:   Parquet codec + benchmarks (PROVE THE TECHNOLOGY)
Month 4-8:   Platform MVP: profile → train → deploy → monitor
Month 8-14:  Design partners (3-5 real production deployments)
Month 14-20: Commercial launch + Kafka integration
Month 20-30: Multi-format, multi-cloud, enterprise features
```

### Detailed roadmap

#### Q1-Q2 2026: Foundation

| Deliverable | Description | Priority |
|------------|-------------|----------|
| Parquet-OpenZL codec | OpenZL compression codec for Apache Parquet, integrates with Arrow/Spark/DuckDB | P0 |
| Benchmark suite | Automated benchmarks on TPC-H, TPC-DS, real Parquet datasets | P0 |
| Auto-schema translator | Reads Parquet schema metadata, generates OpenZL compression configuration | P0 |
| Auto-trainer | Given Parquet files, samples data, trains optimal compressor per column pattern | P0 |
| Technical blog/whitepaper | Published benchmarks showing OpenZL vs. zstd/snappy on Parquet | P1 |

#### Q3-Q4 2026: Platform MVP

| Deliverable | Description | Priority |
|------------|-------------|----------|
| S3 connector | Scans S3 buckets for Parquet files, extracts metadata | P0 |
| Profiling engine | Samples data, characterizes columns, estimates compression opportunity | P0 |
| Training pipeline | Managed training: sample → train → validate → benchmark → select best | P0 |
| Query engine plugins | DuckDB, Spark, and/or Trino plugins for transparent read/write with trained compressors | P0 |
| Monitoring dashboard | Real-time: compression ratios, storage used, cost savings | P1 |
| CLI tool | `nyx profile`, `nyx train`, `nyx deploy` for manual workflows | P1 |

#### Q1-Q2 2027: Design Partners + Kafka

| Deliverable | Description | Priority |
|------------|-------------|----------|
| Design partner program | Onboard 3-5 partners, dedicated support, feedback loop | P0 |
| Kafka SerDe | Schema Registry integration, transparent message compression | P0 |
| Auto-retraining | Detect data drift, schedule retraining, rolling compressor updates | P1 |
| Billing system | Usage metering, invoicing, self-serve payment | P1 |
| Case studies | Published ROI results from design partners | P1 |

#### Q3-Q4 2027: Scale

| Deliverable | Description | Priority |
|------------|-------------|----------|
| Self-serve launch | Starter tier with automated onboarding | P0 |
| Enterprise tier | SSO, RBAC, audit logging, SLA guarantees | P0 |
| Multi-cloud | GCS and Azure Blob Storage support | P1 |
| ML data loader | PyTorch DataLoader integration for compressed training data | P1 |
| Additional formats | TFRecord, HDF5, Avro files | P2 |

---

## 9. Competitive Landscape

### Direct compression competitors

| Company/Project | What They Do | How We Differ |
|----------------|-------------|---------------|
| **Zstandard (Meta)** | Generic compression library | Does not exploit data structure. We use zstd as a backend codec within OpenZL — we're complementary, not competitive. |
| **LZ4 / Snappy** | Fast generic compressors | Same gap — no schema awareness. Optimized for speed over ratio. |
| **Blosc** | Compression for scientific arrays | Narrow domain (NumPy arrays). Not a platform. No training. |
| **FSST (CWI Amsterdam)** | String compression in databases | Academic, narrow (string-only). Not a product. |
| **Brotli (Google)** | Text/web compression | Optimized for HTTP payloads, not structured data. |

### Adjacent competitive threats

| Company | What They Do | Relationship to Us |
|---------|-------------|-------------------|
| **Databricks** | Data lakehouse platform | Could build similar compression into Delta Lake. Partner or competitor depending on strategy. |
| **Snowflake** | Cloud data warehouse | Heavily optimized internal compression. Potential customer for external data (Iceberg/Parquet). |
| **Confluent** | Kafka platform | Could integrate better compression into Confluent Cloud. Partner opportunity. |
| **Meta (OpenZL authors)** | Created the OpenZL framework | The origin of our core technology. Risk: they could productize it themselves. Mitigation: Meta focuses on internal use; our moat is the platform/automation layer + go-to-market. |

### Our differentiation

No company today offers **automated, schema-aware, trainable compression as a managed platform**. Generic compressors don't exploit structure. Format-specific tools (Parquet encodings, Kafka compression) are hardcoded and not learned. Academic projects demonstrate the potential but are not productized.

Our competitive moat has three layers:
1. **Training automation** — automatically profile data, train compressors, manage lifecycle
2. **Integration breadth** — single platform spanning Parquet, Kafka, databases, ML pipelines
3. **Go-to-market speed** — first mover in the "managed compression intelligence" category

---

## 10. Key Risks and Mitigations

### Technical risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **OpenZL can't beat zstd on Parquet column pages** (Parquet already applies dict/RLE/delta encoding before compression) | HIGH | Must benchmark before committing. If column pages are too pre-processed, target pre-encoding data or propose a deeper Parquet integration that replaces the entire encoding+compression stack. |
| **Decompression compatibility** — every query engine needs the OpenZL decoder | MEDIUM | OpenZL has a universal decoder. Distribute as codec plugins for major engines (Spark, Trino, DuckDB). Open-source these plugins for frictionless adoption. |
| **Training cold start** — OpenZL needs sample data before it's effective | LOW | Auto-sampling is straightforward. First-write path can use generic compression; upgrade to trained compression after automatic training completes. |
| **Latency for real-time workloads (Kafka)** | MEDIUM | OpenZL's compression speeds are competitive with zstd. Must validate p99 latency on Kafka produce path. Can offer deferred training + async compression for latency-sensitive topics. |

### Business risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Meta productizes OpenZL themselves** | MEDIUM | Meta focuses on internal infrastructure. Our value is the platform/automation layer + enterprise packaging, not the compression engine. Also: open-source license (BSD) guarantees our right to use the technology. |
| **Cloud providers build it in** | MEDIUM | AWS/GCP/Azure could add schema-aware compression to their storage services. This would validate the category but potentially commoditize us. Mitigation: move faster, build deeper integrations, and establish the platform as multi-cloud and vendor-neutral. |
| **Enterprise sales cycle is long** | HIGH | Start with self-serve Starter tier to build bottom-up adoption. Design partner program to prove ROI. Focus on measurable, quantifiable savings to shorten procurement decisions. |
| **Difficult to prove ROI precisely** | MEDIUM | Build robust A/B comparison tooling: compress the same data with baseline (zstd) and with our platform, show the difference in real dollars. Make ROI calculation transparent and auditable. |

### Licensing

OpenZL is released under the **BSD license** by Meta. This is a permissive license that allows commercial use, modification, and distribution with no copyleft restrictions. We can build a proprietary platform on top of OpenZL without any licensing concerns.

---

## 11. Financial Model

### Revenue projections (conservative)

| Year | Customers | Avg. Data Managed | Avg. Revenue/Customer | ARR |
|------|-----------|-------------------|----------------------|-----|
| Year 1 | 5 (design partners) | 200 TB | $8K/year (discounted) | $40K |
| Year 2 | 25 | 350 TB | $50K/year | $1.25M |
| Year 3 | 75 | 500 TB | $75K/year | $5.6M |
| Year 4 | 200 | 700 TB | $100K/year | $20M |
| Year 5 | 500 | 1 PB | $120K/year | $60M |

### Cost structure

| Category | Year 1 | Year 2 | Year 3 |
|----------|--------|--------|--------|
| Engineering (team of 4-6) | $800K | $1.2M | $2.0M |
| Infrastructure (platform hosting, training compute) | $50K | $200K | $500K |
| Sales & Marketing | $100K | $400K | $1.0M |
| G&A | $50K | $100K | $200K |
| **Total** | **$1.0M** | **$1.9M** | **$3.7M** |

### Funding requirements

- **Pre-seed / Seed:** $1.5-2.5M to cover 18 months of development through design partner phase
- **Series A trigger:** 5+ paying customers, $1M+ ARR, proven ROI metrics from design partners

### Customer value proposition (example)

For a company with 500 TB of Parquet data in S3:

| Cost Category | Before | After (50% reduction) | Annual Savings |
|--------------|--------|----------------------|----------------|
| S3 Standard storage | $138K/year | $69K/year | $69K |
| Cross-region replication | $55K/year | $27.5K/year | $27.5K |
| Data transfer / egress | $48K/year | $24K/year | $24K |
| Query compute (less data to scan) | $200K/year | $150K/year | $50K |
| **Total** | **$441K/year** | **$270.5K/year** | **$170.5K/year** |

At our Growth tier pricing ($3.50/TB/month), their cost for 250 TB (compressed equivalent) would be ~$10.5K/year. **Net savings: $160K/year. ROI: 15x.**

---

## 12. Open Technical Questions

These must be answered before committing resources to specific market segments.

### Critical (must validate in Phase 1)

1. **Can OpenZL beat zstd on Parquet column pages?**
   Parquet applies dictionary encoding, RLE, and delta encoding before handing data to the compression codec. The residual data after these encodings may have less exploitable structure. We need to benchmark OpenZL against zstd on real Parquet column pages to quantify the improvement.

   *If the answer is "no" or "barely":* Pivot to integrating OpenZL at a deeper level — replacing Parquet's entire encoding+compression stack rather than just the compression codec. Or focus on Kafka first (where no pre-encoding happens).

2. **What is OpenZL's compression throughput on modern hardware?**
   For Kafka, we need single-digit millisecond compression latency per message batch. For Parquet writes, we need throughput competitive with zstd-3 or better. Must benchmark on representative data.

3. **How large is the trained compressor artifact?**
   Trained compressors need to be distributed to every reader. If they're megabytes, they can be embedded in file metadata or stored alongside data. If they're gigabytes, this changes the architecture.

### Important (validate in Phase 2)

4. **How sensitive is compression ratio to data drift?**
   If a trained compressor degrades quickly when data distribution shifts, we need aggressive retraining. If it's robust, we can retrain less frequently. This affects platform complexity and cost.

5. **Can we auto-translate Parquet/Avro/Protobuf schemas to OpenZL configurations without manual SDDL writing?**
   This is required for the "zero schema engineering" promise. Parquet schemas are rich enough (typed columns) that this should be feasible, but needs implementation and validation.

6. **What's the minimum training data size for effective compressors?**
   Smaller = faster onboarding. Need to characterize the compression ratio as a function of training data volume across different data types.

---

## 13. Immediate Next Steps

### This week

- [ ] Finalize and align the team on the Parquet-first strategy
- [ ] Run the grid search (`optimizing_openzl/grid_search.py`) to establish our baseline training parameter knowledge
- [ ] Begin investigating Parquet's internal encoding pipeline to understand where OpenZL can integrate

### This month

- [ ] Build a minimal proof-of-concept: take a Parquet file, extract column pages, compress with OpenZL (trained) vs. zstd, measure the ratio difference
- [ ] If Parquet PoC shows >30% improvement over zstd: commit to Parquet-first roadmap
- [ ] If Parquet PoC shows <15% improvement: investigate deeper integration (replace encoding stack) or pivot to Kafka-first
- [ ] Research Apache Arrow's codec extension interface for Parquet integration path

### This quarter

- [ ] Build the Parquet-OpenZL codec prototype
- [ ] Publish benchmark results
- [ ] Begin platform MVP development (profiling engine + training pipeline)
- [ ] Identify and approach 3-5 design partner candidates

---

*This document is a living strategy brief. It should be updated as we validate technical assumptions and learn from the market.*
