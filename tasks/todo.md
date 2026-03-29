# Cross-Chunk Buffering for Tovertica Transform

## Implementation Steps
- [x] Read handoff document and implementation plan
- [x] Read target files (transform-tovertica.cc/.hh)
- [x] Read reference files (transform-grouper.cc/.hh, transform.hh)
- [x] Modify transform-tovertica.hh — add members and declarations
- [x] Modify transform-tovertica.cc — constructor config parsing
- [x] Modify transform-tovertica.cc — refactor process() into buffered/unbuffered
- [x] Modify transform-tovertica.cc — add flush_compress_buffer(), flush(), end_of_input()
- [ ] Build verification (requires kelpie build nom-link on build host)
- [x] Diff review — manual walkthrough complete

---

# DNS TLV Compression — Experiments

## Baseline (DONE)
- [x] Run baseline compression: 82 KB → 12.1 KB (6.6x, beats Snappy 35 KB by 65.5%)
- [x] Record: original 82 KB, columnar 69 KB, OpenZL 12.1 KB per chunk
- [x] Compare against Snappy: OpenZL WINS by 65.5% (12.1 KB vs 35 KB)

## SDDL Schema Optimization (SUPERSEDED — zstd+dict is better)
- [x] Break CLIENT_ADDRESS and VIEW out of `Byte[_rem]` → 5.7% improvement (v2)
- [x] Separate variable section from field order → no improvement (v3)
- [~] Remaining SDDL items CANCELLED — columnar approach (5.9x) beaten by zstd raw (8.0x)

## Compression Method Comparison (DONE)
- [x] OpenZL SDDL columnar: 5.9x — worst, most complex
- [x] OpenZL serial on raw TLV: 8.1x, 3 MB/s — too slow
- [x] zstd default on raw TLV: 8.0x, 1,322 MB/s — simple 1-line change
- [x] zstd+64KB dict: 9.1x, 1,528 MB/s
- [x] zstd+256KB dict (default level): 16.8x, 1,599 MB/s
- [x] zstd+256KB dict level 2: 19.1x, 2,260 MB/s — OPTIMAL
- [x] zstd+256KB dict level 19: 26.1x, 12 MB/s — too slow
- [x] Stacking compressors (OpenZL on zstd): makes it worse, never do this

## Dictionary Optimization (DONE)
- [x] Dictionary size sweep: 16KB→8x, 64KB→9x, 128KB→10x, 256KB→17x, 512KB→16x
- [x] 256KB is the sweet spot — ratio plateaus beyond
- [x] zstd level sweep: levels 1-15 all handle 97 MB/s production throughput
- [x] Level 2 is optimal speed+ratio: 19.1x at 2,260 MB/s

## Verification (DONE)
- [x] Round-trip verified on all 20 chunks (byte-identical)
- [x] Double compression tested (wasteful — set Kafka compression.type=none)
- [x] Batch-level ratios verified (consistent across batch sizes)
- [x] nom-dns-vertica text format tested (only 3.9x — binary TLV much better)
- [x] Multi-chunk training tested (no improvement over single chunk)

## Production Deployment (TODO — not part of this investigation)
- [ ] Implement Option A: change libnomkafka compression.codec to "zstd"
- [ ] Implement Option B: add zstd-dict as GenericChunk compression type in nomchunk
- [ ] Deploy dictionary to cache-serve, nom-link, Data Loader VMs
- [ ] Measure production impact on Kafka storage and replication bandwidth

## Artifacts
- [x] Trained dictionary: `artifacts/dns_tlv_zstd_256k.dict` (256 KB)
- [x] Results doc: `DNS_COMPRESSION_RESULTS.md`
- [x] Full experiment log: `tasks/lessons.md` (19 experiments)
- [x] DNS TLV preprocessor: `tools/dns_tlv_preprocess.cpp` (for analysis, not needed for production)
