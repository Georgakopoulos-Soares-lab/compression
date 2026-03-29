# Lessons Learned

## Baseline Context
- DNS TLV records: ~5,600 per GenericChunk, ~55 KB raw TLV per chunk
- Current Snappy (Kafka-level) achieves ~35 KB per chunk (the baseline to beat)
- Text representation (tab-delimited) is 7x WORSE than Snappy on binary because:
  - UUIDs expand from 16 raw bytes to 36-byte hex strings
  - Timestamps expand from 8 bytes to 16-byte decimal strings
  - Tab separators and empty column placeholders add overhead
- OpenZL excels on structured columnar data, not opaque binary blobs
- The approach: decompose TLV into typed columnar streams (like genomics codecs)

## Key Numbers
- Snappy baseline: ~35 KB per 5,600-record chunk
- zstd on text (50 MB): 4.1x
- zstd on text (1 MB): 3.9x
- Non-UUID text columns compress 9.1x with zstd
- UUID columns compress only 1.9x (near-random hex)
- Even 100x on non-UUID + zstd on UUID = 5.6x overall on text → still loses to Snappy on binary

## Experiment 1: Baseline (Iteration 1)
- **What**: Initial SDDL schema with named fixed headers + Byte[_rem] catch-all for variable fields
- **Result**: 82 KB original → 69 KB columnar → **12.1 KB OpenZL** (6.6x vs original, 5.6x vs columnar)
- **vs Snappy**: **65.5% smaller** (12.1 KB vs 35 KB) — BEATS Snappy significantly
- **Lesson**: Even a basic columnar decomposition with minimal SDDL (only time_deltas, client_port, inet_family named; rest as Byte[_rem]) already beats Snappy by 3x. Breaking out more columns should improve further.
- **Training**: Single chunk, 60 sec, 4 threads, sddl profile
- **Round-trip**: Bit-exact verified

## Experiment 2: Named Fixed Columns in SDDL (Iteration 2)
- **What**: Added size-prefixed column headers to binary format. SDDL now names CLIENT_ADDRESS and VIEW as separate `Byte[col_size]` sections instead of lumping into `Byte[_rem]`.
- **Result**: 82 KB original → **11.7 KB OpenZL** (7.0x vs original)
- **vs v1**: 5.7% improvement (12.1 KB → 11.7 KB)
- **vs Snappy**: **67.5% smaller** (11.7 KB vs 35 KB)
- **Lesson**: Breaking columns out of `_rem` into named sections improves compression. Each named section gets its own entropy model. OpenZL's CBOR serialization supports `Byte[field_ref]` syntax for dynamically-sized sections.
- **SDDL bug**: OpenZL fails if the total SDDL is malformed, not if there are too many fields. The initial failure was from a different syntax issue in the schema file.
- **Next**: Break out more of `_rem` — the variable columns (DNS_MESSAGE, DEVICE_ID) and the field order map.

## Experiment 3: Separate Variable Section in SDDL (Iteration 3)
- **What**: Added `var_blob_size + var_blob` to separate Section C (variable columns) from Section D (field order map) in SDDL
- **Result**: 11,698 bytes — essentially identical to v2 (11,663 bytes), within noise
- **Lesson**: Separating variable section from field order map doesn't help. The entropy model treats them similarly whether named or in `_rem`. The big wins came from naming the fixed columns (v2).
- **SDDL CBOR bug**: Field naming matters — `var_blob_size` + `var_blob` triggers CBOR error, but `extra_size` + `extra_data` works. OpenZL may have reserved names or parsing issues with underscore-heavy names. Use simple field names.
- **Next**: Try training on multiple diverse chunks instead of single chunk. Also try longer training time.

## Experiment 4: Multi-chunk Training + Speed Benchmarks (Iteration 4)
- **What**: Trained on 20 chunks instead of 1, measured speed of compress/decompress
- **Result**: Multi-chunk training produced corrupt compressor (CBOR serialization bug in OpenZL when training on many SDDL files)
- **Speed**: Single-chunk v2 compressor: **21.5ms compress, 20.2ms decompress** per 80 KB chunk
- **Throughput**: 80 KB / 21.5ms = ~3.7 MB/sec compress, ~3.9 MB/sec decompress
- **Lesson**: OpenZL SDDL training has CBOR bugs with multiple training files. Stick to single-chunk training for now. The 21ms compress time is fast enough for the current 160 KB/sec nom-dns-vertica throughput but too slow for 97 MB/sec nom-dns-base.
- **Lesson**: Subprocess overhead dominates — 21ms per 80 KB chunk is mostly zli process spawn time, not compression CPU time.
- **Next**: Try longer training time on single chunk. Consider in-process OpenZL for speed.

## Experiment 5-6: Schema Robustness + Multi-chunk Validation (Iterations 5-6)
- **What**: Tested v1 schema across ALL 20 chunks including chunks with different column counts (2 or 3 fixed columns)
- **Result**: v1 schema (Byte[_rem] catch-all) works on ALL chunks regardless of column count
- **All 20 chunks**: 100% success rate, 5.9x average ratio, 74.3% smaller than Snappy
- **Speed**: 158 MB/s compress, 1,783 MB/s decompress (0.4ms per 80 KB chunk)
- **Lesson**: Named columns in SDDL (v2) give ~5-10% better compression but ONLY work when the column set matches exactly. The v1 schema with `Byte[_rem]` is robust to different column sets. For production, robustness > marginal ratio gains.
- **Lesson**: OpenZL SDDL training has CBOR serialization bugs triggered by: (1) specific field names with underscores, (2) two consecutive `Byte[field_ref]` sections, (3) multi-file training. Work around by using simple names and single-file training.
- **Lesson**: Longer training time (300s vs 60s) gives no improvement — the model converges fully on a single chunk in 60s.
- **Bottom line**: v1 schema is the production choice. 5.9x average ratio, all chunks pass, robust.

## Experiment 7: zstd on Raw TLV vs OpenZL on Columnar (Iteration 7) — CRITICAL
- **What**: Benchmarked zstd (default and level 19) directly on raw TLV binary — no preprocessing needed
- **Result**:
  - zstd default on raw TLV: **7.7x** (141 KB total, 80.3% vs Snappy)
  - zstd level 19 on raw TLV: **8.8x** (123 KB total, 82.8% vs Snappy)
  - OpenZL on columnar: **5.9x** (184 KB total, 74.3% vs Snappy)
- **zstd WINS on every single chunk.** No exceptions.
- **Lesson**: zstd on raw binary TLV outperforms OpenZL on preprocessed columnar binary. The TLV format already has enough local redundancy (repeated type bytes, similar-length fields, sequential timestamps) for zstd's LZ-based matching to exploit. The columnar decomposition actually HURTS because it separates correlated data that zstd compresses well together.
- **Lesson**: The overhead of preprocessing (columnar encoding adds metadata: column counts, offsets, bitmaps) outweighs any per-column modeling benefit.
- **Critical conclusion**: For DNS TLV data, the simplest possible approach (replace Snappy with zstd in librdkafka: `compression.codec = "zstd"`) gives the best results. No OpenZL, no preprocessing, no SDDL schemas needed. One config change.

## Experiment 8: OpenZL Serial on Raw TLV (Iteration 8) — BREAKTHROUGH
- **What**: Trained OpenZL with `serial` profile directly on raw TLV binary — zero preprocessing
- **Result**: 8.1x average (134 KB total), beats zstd default (7.7x, 141 KB), loses to zstd-19 (8.8x, 123 KB)
- **vs Snappy**: 81.3% smaller
- **Critical lesson**: The columnar preprocessor was COUNTERPRODUCTIVE. OpenZL serial on raw TLV (8.1x) > OpenZL SDDL on columnar (5.9x). The preprocessing overhead (metadata, offsets, bitmaps) and column separation destroyed the local correlations that both OpenZL and zstd exploit.
- **Practical recommendation**:
  - **Simplest win**: Change `compression.codec = "snappy"` to `"zstd"` in libnomkafka. One line change, 7.7x → 80% savings.
  - **Better with effort**: Train OpenZL serial on TLV samples, deploy as custom codec. 8.1x → 81% savings.
  - **Best ratio**: zstd level 19. 8.8x → 83% savings, but significantly slower.
- **Speed**: OpenZL serial compresses at 150 MB/s, decompresses at 2,538 MB/s. Fast enough for nom-dns-vertica (160 KB/sec) but tight for nom-dns-base (97 MB/sec).

## Experiment 9: Multi-chunk Training (Iteration 9)
- **What**: Trained OpenZL serial on 10 chunks with 300s instead of 1 chunk with 60s
- **Result**: Identical — 133,822 B (8.1x). Model converged on single chunk already.
- **Lesson**: More training data doesn't help when the data is homogeneous (DNS TLV from same source).

## Experiment 10: zstd Trained Dictionary (Iteration 10) — NEW WINNER
- **What**: Trained a 64 KB zstd dictionary on 10 TLV chunks, then compressed with dictionary
- **Result**:
  - zstd+dict default: **9.1x** (119 KB) — beats OpenZL serial (8.1x)
  - zstd-19+dict: **10.8x** (101 KB) — best overall, 86% smaller than Snappy
- **Why it works**: zstd dictionary pre-seeds the compression context with learned byte patterns from TLV data. This gives zstd the same "trained model" advantage as OpenZL but within zstd's native framework — no Python, no subprocess, no temp files.
- **Practical advantage**: zstd with dictionary is a C library, integrates directly into librdkafka or nom-link C++ code. No external processes needed.
- **Implementation**: librdkafka doesn't natively support zstd dictionaries, but the application can pre-compress with dictionary before handing to Kafka (set `compression.type=none`). Or use the GenericChunk application-level compression with a custom zstd-dict codec.

## Experiment 11: Complete Speed Benchmarks (Iteration 11) — DEFINITIVE
- **What**: Measured compress/decompress speed for all methods on 80 KB chunk
- **Critical finding**: zstd+dict achieves **15.2x at 1,528 MB/s** — same speed as Snappy (1,563 MB/s) but 6.5x better compression
- **OpenZL is too slow**: 3 MB/s (subprocess overhead). Cannot handle 97 MB/s nom-dns-base throughput.
- **zstd-19 variants are too slow**: 7-10 MB/s. Cannot handle production throughput.
- **Only Snappy, zstd default, and zstd+dict can handle production throughput** (all >1,300 MB/s)

## Experiment 12: Dictionary Size Optimization (Iteration 12) — FINAL ANSWER
- **What**: Tested dictionary sizes from 16KB to 256KB, filtered to production-sized chunks (>30KB)
- **Result**: 256KB dictionary on full-size chunks: **16.8x at 1,599 MB/s** — faster than Snappy, 7x better compression
- **Per-chunk breakdown**: 73.8 KB avg → 4.4 KB avg. Best chunk: 27.3x. Worst full chunk: 8.7x.
- **Speed**: 0.049ms compress, 0.009ms decompress per 80KB chunk. 1,599 MB/s throughput. FASTER THAN SNAPPY.
- **Savings at 8.4 TB/day**: **7.9 TB/day saved** (94% reduction in storage)
- **Dictionary sizes**: 16KB→8x, 64KB→11x, 128KB→12.5x, 256KB→16.8x. Bigger dictionary = better ratio at same speed.
- **Small chunk degradation**: Chunks <10 KB only get 3-5x. But these are rare in production (cache-serve batches at 80KB).
- **Bottom line**: zstd+256KB dictionary is the definitive production solution. No OpenZL, no preprocessing, no SDDL. Just zstd with a 256KB dictionary trained on production TLV samples.

## DEFINITIVE RANKING (production-sized chunks, speed + ratio):
1. **zstd + 256KB dict: 16.8x at 1,599 MB/s** — best overall. Faster than Snappy, 7x better compression. Saves 7.9 TB/day.
2. **zstd + 64KB dict: ~11x at 1,528 MB/s** — smaller dictionary, slightly less compression
3. **zstd-19 + dict: 10.8x at 10 MB/s** — better ratio but too slow for production
4. **zstd-19: 8.8x at 7 MB/s** — too slow
5. **OpenZL serial: 8.1x at 3 MB/s** — too slow, needs external binary
6. **zstd default: 8.0x at 1,322 MB/s** — simplest change (1 line), production viable
7. **OpenZL SDDL+columnar: 5.9x at 158 MB/s** — worst ratio, most complex
8. **Snappy: 2.3x at 1,563 MB/s** — current baseline

## Experiment 13: Dictionary Size Sweep (Iteration 13)
- **What**: Tested dictionaries from 16KB to 1024KB on full-size production chunks
- **Result**: 256KB is the sweet spot. 512KB and 1024KB give same or slightly worse ratio.
- **Round-trip**: All 20 chunks verified byte-identical.

## Experiment 14: zstd+dict on nom-dns-vertica TEXT (Iteration 14)
- **What**: Tested zstd+dict on the tab-delimited text format (post-tovertica)
- **Result**: Only 3.7-3.9x — text format is too bloated for good ratios
- **Lesson**: Binary TLV (16.8x) >> text (3.9x). Compress at the binary level, not after text conversion.

## Experiment 15: Dictionary Saved (Iteration 15)
- Saved optimal 256KB dictionary to `artifacts/dns_tlv_zstd_256k.dict`
- Dictionary ID: 1076879155
- Trained on 20 TLV chunks from production `nom-dns-base` topic

## Experiment 16: Double Compression (Iteration 16)
- **What**: Tested app-level zstd-dict + Kafka-level zstd (double compression)
- **Result**: Double compression adds 10 bytes (0.0% overhead) — Kafka-level zstd on already-compressed data is a no-op
- **Lesson**: When using app-level zstd-dict, set `compression.type=none` in Kafka. No benefit from double compression, just wastes CPU.

## Experiment 17: Batch Consistency (Iteration 17)
- **What**: Verified compression ratios are consistent across different batch sizes (2, 5, 10, 14 chunks)
- **Result**: Individual chunks: 16.8x. In batches of 2-5: ~20.8x (batch benefits). In batch of 14: 16.8x.
- **Production numbers**: 8.4 TB/day → 0.50 TB/day compressed = **7.9 TB/day saved (94%)**
- **Monthly savings**: **237 TB/month**

## PRODUCTION RECOMMENDATIONS:
- **Quick win (1 line)**: Change `compression.codec = "snappy"` to `"zstd"` in libnomkafka → 8x, saves 6.7 TB/day
- **Optimal (moderate effort)**: Deploy zstd with 256KB trained dictionary as application-level compression in GenericChunk → 16.8x, saves 7.9 TB/day, FASTER than current Snappy. Set Kafka `compression.type=none`.
- **NOT recommended**: OpenZL (too slow at 3 MB/s, no ratio advantage over zstd+dict)
- **Dictionary saved**: `artifacts/dns_tlv_zstd_256k.dict` (256 KB, trained on production TLV)

## Agent Error Log (2026-03-29)
- **Mistake**: Assumed subprocess usage — project already uses C API (CCtx/DCtx directly in dns_compress.cpp)
- **Mistake**: Assumed small KB files — production uses 5MB batches in tovertica buffer
- **Mistake**: Assumed TLV binary format — nom-dns-vertica is tab-delimited text
- **Mistake**: Recommended batching — already batching at Kafka level
- **Lesson**: ALWAYS read tasks/lessons.md and existing source (dns/dns_compress.cpp, dns/benchmark.cpp, dns/results.csv) BEFORE making recommendations. The project has extensive experiment history.

## Experiment 18: Stacking Compressors (Iteration 18)
- **What**: Tried OpenZL serial on zstd-dict compressed output (stacking compressors)
- **Result**: Makes output WORSE (+22 bytes). Compressed data is near-random, incompressible.
- **Lesson**: Never stack compressors.

## Experiment 20: SDDL Bug Root Cause (Iteration 20)
- **What**: Investigated the real cause of SDDL training failures
- **Root cause**: Binary/schema MISALIGNMENT, not field count or name limits. When the SDDL described fields that didn't exist in the binary (because preprocessor wasn't rebuilt), OpenZL read garbage values and crashed during CBOR serialization.
- **Lesson**: OpenZL SDDL has NO field count limit. It supports hundreds of named fields. Always rebuild the preprocessor when changing the SDDL.

## Experiment 21: Batch Size Scaling (Iteration 21) — KEY FINDING
- **What**: Tested all methods at 80 KB, 500 KB, 1 MB, 5 MB, 10 MB batch sizes
- **Results**:
  - zstd and OpenZL serial PLATEAU at ~1 MB — larger batches don't improve ratio
  - zstd+dict wins at every batch size by wide margin
  - OpenZL SDDL gets WORSE with larger batches (metadata overhead scales linearly)
- **Lesson**: Larger batches do NOT help OpenZL. The hypothesis that "metadata overhead is amortized by larger batches" is wrong — the compressor's window size is the limiting factor, not metadata.
- **Conclusion**: Batch size is irrelevant for choosing a compression method. zstd+dict wins regardless.

## Experiment 22: 1 GB Training + Batch Size Scaling (Iteration 22) — REVISED FINDINGS
- **What**: Trained OpenZL serial on 100 MB (single file), retested at all batch sizes. Also tested zstd dictionary quality vs training data diversity.
- **Results**:
  - OpenZL serial (100M trained): 8.8x stable at 1-10 MB batches
  - zstd+dict (14 specialized chunks): 18.3x at 80 KB but degrades to 7.8x at 10 MB
  - zstd+dict (50 diverse chunks): 10.3x at 80 KB, 8.8x at 5 MB — more stable
  - zstd+dict (200 diverse chunks): 8.4x at 80 KB, 8.2x at 10 MB — most stable
- **Critical insight**: The 18.3x result from earlier was OVERFITTING. The specialized dictionary (14 chunks) memorized specific byte patterns. With diverse data, zstd+dict degrades to 8-10x.
- **Revised conclusion**: At production scale with diverse DNS traffic, zstd+dict and OpenZL serial converge to similar ratios (~8.5-8.8x). The choice is about speed and deployment complexity, not ratio.
- **OpenZL's advantage**: Stable ratio regardless of batch size. zstd+dict's ratio depends heavily on dictionary quality/diversity tradeoff.
- **zstd's advantage**: 1,500 MB/s vs 3 MB/s. No external binary. C library call.
- **1 GB training bug**: Multi-file serial training (11 x 100MB files) produced corrupt CBOR. Single-file training (1 x 100MB) worked fine. OpenZL bug with many training files.

## Experiment 23: Definitive Batch-Size Benchmark (33K diverse chunks)
- **What**: Final benchmark across 80 KB to 50 MB with 33,135 diverse chunks, zstd dicts of different diversity, and OpenZL serial trained on 100 MB
- **Results (at 50 MB)**: OpenZL serial 8.8x > zstd+dict 8.1x = zstd 8.1x
- **Results (at 80 KB)**: zstd+dict-50 10.3x > OpenZL 8.3x > zstd 8.0x
- **zstd+dict diversity tradeoff**: 50-chunk dict: 10.3x at 80 KB but 8.1x at 50 MB. 500-chunk dict: 7.8x everywhere. The more diverse the dictionary, the less effective it is.
- **OpenZL serial is the most consistent**: 8.3-8.8x across ALL batch sizes, no tuning needed
- **SDDL 1 GB training**: FAILED (0-byte compressor output). Multi-file SDDL training still broken.

## REVISED PRODUCTION RECOMMENDATIONS:
- **Simple (1 line)**: `compression.codec = "zstd"` → 8.0x consistently. Best effort/reward ratio.
- **Small batches (80 KB)**: zstd+dict with ~50 training chunks → 10.3x. Best at single-chunk level.
- **Large batches (5-50 MB)**: OpenZL serial → 8.8x. Only method that consistently beats plain zstd at scale. But 500x slower (3 MB/s vs 1,500 MB/s).
- **The 18.3x number was overfitting**: A specialized dictionary on 14 similar chunks. Not representative of production diversity.

## Experiment 19: zstd Level Sweep with Dictionary (Iteration 19)
- **What**: Tested all zstd levels (1-22) with 256KB dictionary
- **Results**:
  - Level 1: 12.8x at 1,981 MB/s
  - **Level 2: 19.1x at 2,260 MB/s** — BEST speed+ratio for production
  - Level 3 (default): 19.8x at 1,630 MB/s
  - Level 5: 21.9x at 516 MB/s
  - Level 12: 24.5x at 232 MB/s
  - Level 15: 25.4x at 125 MB/s — minimum viable for 97 MB/s throughput
  - Level 19: 26.1x at 12 MB/s — too slow
- **Recommendation**: Use **level 2** for production. Best speed (2,260 MB/s) with excellent ratio (19.1x). Or level 3 (default) for slightly better ratio at still-excellent speed.
- **All levels 1-15 can handle nom-dns-base throughput (97 MB/sec)**

## DNS TLV Field Distribution (50,000 records)
- timestamp: 1 unique (constant per chunk batch)
- node-id: 3 unique → dictionary
- qtype: 10 unique, 90% = A → dictionary
- rcode: 5 unique, 97% = noerror → dictionary
- flags: always 0 → drop
- 6 columns always empty → drop
- count: 7 unique, 82% = 3 → dictionary
- domains: 4,340 unique → moderate compression
- UUIDs: ~unique per record → minimal compression

## Deployment Lessons

### nom-tell requires sudo
`nom-tell` reads auth credentials from `/usr/local/nom/etc/auth/` which is only readable by root. Running as `centos` gives "bad auth" or "not a known service name". Always use `sudo /usr/local/nom/sbin/nom-tell`.

### transform.add, not transform.update for Config Hub-provisioned objects
Objects provisioned by Config Hub (i.e., objects that exist when the environment is instantiated) cannot be updated via `transform.update` in the command channel. They can only be overwritten using `transform.add`. This applies to all standard pipeline transforms like `dns-tovertica`.

### kafka-configs for topic config changes (not kafka-topics --alter --config)
Newer Kafka versions (3.x+) removed `--alter --config` from `kafka-topics`. Use `kafka-configs --alter --entity-type topics --entity-name <topic> --add-config <key>=<value>` instead.

### RPM install requires service restart to pick up new binary
Installing a new RPM does NOT automatically restart the service. Always `systemctl restart nom-link` after `rpm -Uvh`.

## Production Deployment Results (2026-03-24)

### OpenZL Compression on nom-dns-vertica (Live)
- **Buffer size**: 5MB (`compress-buffer-size=5242880`)
- **Flush interval**: 10s (`compress-flush-seconds=10`)
- **Compression ratio**: **5.29x** consistently across all 4 DC link nodes
- **Input per buffer**: ~5.96 MB (~1.44M strings)
- **Output per buffer**: ~1.13 MB compressed
- **Compression speed**: 65-70 MB/s (well above the ~160 KB/s nom-dns-vertica throughput)
- **Latency per buffer**: ~88-96 ms
- **Data Loader**: No errors, no droppedMalformed, services running normally
