// harc_preprocessor.cpp
//
// A from-scratch, HARC/SPRING-style preprocessor adapted for whole-genome
// reference FASTA (as opposed to FASTQ read sets, which is what real
// HARC/SPRING target).
//
// Why the adaptation: HARC's redundancy signal is *sequencing coverage* --
// many reads sampled from overlapping positions of the same molecule. A
// reference genome FASTA has no such coverage; each base occurs exactly
// once. The redundancy that *does* exist is repeat content (transposable
// elements, tandem repeats, paralogous genes, low-complexity regions). So:
//
//   1. Chunk each FASTA record into fixed-length, NON-overlapping
//      "pseudo-reads" (read_len bases each, last read per record may be
//      shorter).
//   2. Build a minimizer index over those pseudo-reads (4 windows/read,
//      k=16 min-hash per window).
//   3. Greedily reorder pseudo-reads by shared-minimizer similarity --
//      HARC's core idea (an approximate read-overlap graph) walked greedily
//      rather than solved exactly, since exact ordering is intractable at
//      genome scale.
//   4. Encode each read either as RAW (4-bit packed, same convention as
//      FAV4) or as a DELTA against the immediately preceding read in the
//      new order (mismatch position + base list), whichever applies.
//
// CLI mirrors tools/biocompress_preprocessor.cpp so it drops into the same
// pipeline shape:
//   harc_preprocessor <input_file> <output_dir> <num_threads> [read_len=200] [mismatch_frac=0.15]
// Produces chunk_NNNNN.harc_packed.bin files in <output_dir>, using the
// SAME record-safe chunk-boundary-snapping algorithm as biocompress
// (max 450 MiB/chunk, snapped to '>' at start-of-line), so for a given
// input file and thread count, HARC and FAV4 chunk boundaries match --
// making per-chunk comparisons apples-to-apples.
//
// Output format "HRC2" (columnar; see schemas/harc_packed.sddl):
//   magic               Byte[4]        "HRC2"
//   num_records         U32
//   hdr_offsets         U32[num_records+1]   prefix sums into `headers`
//   hdr_total, hdr_pad  U32
//   headers             Byte[hdr_total+hdr_pad]
//   read_len            U32            target pseudo-read length
//   num_reads           U32
//   rec_read_offsets    U32[num_records+1]   which reads belong to which record (ORIGINAL order)
//   read_lengths        U32[num_reads]        length in bases, ORIGINAL order
//   order_to_orig       U32[num_reads]        order_to_orig[i] = original read index at reordered position i
//   flags               Byte[num_reads]        REORDERED order: 0=RAW, 1=DELTA
//   ov_start_cur        U16[num_reads]        REORDERED order; DELTA only: overlap start within cur read
//   ov_start_prev       U16[num_reads]        REORDERED order; DELTA only: overlap start within prev read
//   ov_len              U16[num_reads]        REORDERED order; DELTA only: overlap length
//   mismatch_counts     U16[num_reads]        REORDERED order; mismatches within the overlap; 0 for RAW
//   raw_pool_total, raw_pool_pad         U32
//   prefix_pool_total, prefix_pool_pad   U32   (literal bases before the overlap, DELTA reads only)
//   suffix_pool_total, suffix_pool_pad   U32   (literal bases after the overlap, DELTA reads only)
//   mpos_total, mpos_pad                 U32   (bytes; 2 * sum(mismatch_counts); positions relative to overlap start)
//   mbase_total, mbase_pad               U32   (bytes; ceil(sum(mismatch_counts)/2))
//   raw_pool            Byte[..]   4-bit packed bases of RAW reads, reordered order
//   prefix_pool         Byte[..]   4-bit packed literal prefix (cur[0:ov_start_cur]) per DELTA read
//   suffix_pool         Byte[..]   4-bit packed literal suffix (cur[ov_start_cur+ov_len:len]) per DELTA read
//   mismatch_positions  Byte[..]   U16LE offsets relative to overlap start, per DELTA read
//   mismatch_bases      Byte[..]   4-bit packed replacement bases, per DELTA read
//   (trailing remainder, if any, is padding only)
//
// DELTA reconstruction: cur = prefix_literal
//                            + prev[ov_start_prev .. ov_start_prev+ov_len) with mismatches applied
//                            + suffix_literal
// This is a real seed-and-extend alignment against the previous read in the
// new order (anchor = a shared minimizer k-mer, extended outward while
// mismatches stay within budget) -- NOT a fixed zero-offset comparison.
// Fixed-offset comparison was tried first and empirically failed: a repeat
// element lands at a different phase within each same-length window
// depending on how much unique flanking sequence precedes it, so two
// occurrences of the same repeat almost never line up at shift=0. Real
// alignment is what makes the delta path actually fire on repeat content.

#include "harc_common.h"
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <filesystem>
#include <iostream>
#include <thread>
#include <unordered_map>

namespace fs = std::filesystem;

struct PseudoRead {
    uint32_t record_id;
    size_t   seq_begin; // offset into that record's seq string
    uint32_t len;
};

static uint64_t fnv1a(const char* s, size_t n) {
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) { h ^= (uint8_t)s[i]; h *= 1099511628211ULL; }
    return h;
}

struct Minimizer { uint64_t hash; uint32_t pos; }; // pos is 0-based, relative to the READ (not the record)

// 4 minimizers per read: split into 4 windows, take the min k-mer hash per
// window AND remember where it starts, so encode-time alignment can use it
// as a seed anchor (not just a similarity signal for reordering).
static void compute_minimizers(const std::string& seq, size_t begin, uint32_t len,
                                std::vector<Minimizer>& out, uint32_t k = 16, uint32_t windows = 4) {
    out.clear();
    if (len < k) return;
    uint32_t win_count = std::min(windows, len / k);
    if (win_count == 0) win_count = 1;
    uint32_t win_size = len / win_count;
    for (uint32_t w = 0; w < win_count; w++) {
        size_t wstart = begin + (size_t)w * win_size;
        size_t wend = (w + 1 == win_count) ? (begin + len) : (wstart + win_size);
        if (wend - wstart < k) continue;
        uint64_t best = UINT64_MAX; size_t best_pos = wstart;
        for (size_t p = wstart; p + k <= wend; p++) {
            uint64_t h = fnv1a(seq.data() + p, k);
            if (h < best) { best = h; best_pos = p; }
        }
        out.push_back(Minimizer{best, (uint32_t)(best_pos - begin)});
    }
}

// Seed-and-extend: given a shared-hash anchor (cur_pos in `cur`, prev_pos in
// `prev`), compute the full theoretically-overlapping range implied by the
// shift, then measure mismatches across that whole range (cheap: reads are
// only ~read_len bases). Returns false if no usable overlap.
static bool try_align(const char* cur, uint32_t cur_len, const std::string& prev,
                       uint32_t cur_anchor_pos, uint32_t prev_anchor_pos,
                       double mismatch_frac_max,
                       uint32_t& ov_start_cur, uint32_t& ov_start_prev, uint32_t& ov_len,
                       uint32_t& mismatches) {
    int64_t shift = (int64_t)prev_anchor_pos - (int64_t)cur_anchor_pos; // cur[i] <-> prev[i+shift]
    int64_t prev_len = (int64_t)prev.size();
    int64_t i_lo = std::max<int64_t>(0, -shift);
    int64_t i_hi = std::min<int64_t>((int64_t)cur_len, prev_len - shift); // exclusive
    if (i_hi - i_lo < 16) return false; // too short to bother
    uint32_t mism = 0;
    for (int64_t i = i_lo; i < i_hi; i++) {
        if (cur[i] != prev[i + shift]) mism++;
    }
    uint32_t len = (uint32_t)(i_hi - i_lo);
    if ((double)mism > mismatch_frac_max * len) return false;
    ov_start_cur = (uint32_t)i_lo;
    ov_start_prev = (uint32_t)(i_lo + shift);
    ov_len = len;
    mismatches = mism;
    return true;
}

struct HarcStats {
    uint64_t num_reads = 0, n_raw = 0, n_delta = 0, mismatch_total = 0;
};

static HarcStats process_fasta_harc_chunk(const char* start, const char* end, const std::string& output_path,
                                           uint32_t read_len, double mismatch_frac_max) {
    const size_t MAX_BUCKET = 500; // cap per-minimizer bucket size to bound worst-case (e.g. poly-N runs)

    std::vector<FastaRecOwned> records = parse_fasta_range(start, end);

    // ---- headers ----
    std::vector<uint32_t> hdr_offsets(records.size() + 1, 0);
    std::string headers_concat;
    for (size_t i = 0; i < records.size(); i++) {
        hdr_offsets[i] = (uint32_t)headers_concat.size();
        headers_concat += records[i].header;
    }
    hdr_offsets[records.size()] = (uint32_t)headers_concat.size();
    uint32_t hdr_total = (uint32_t)headers_concat.size();
    uint32_t hdr_pad = (4 - (hdr_total % 4)) % 4;

    // ---- chunk each record into non-overlapping pseudo-reads ----
    std::vector<PseudoRead> reads;
    std::vector<uint32_t> rec_read_offsets(records.size() + 1, 0);
    for (size_t r = 0; r < records.size(); r++) {
        rec_read_offsets[r] = (uint32_t)reads.size();
        const std::string& seq = records[r].seq;
        size_t pos = 0;
        while (pos < seq.size()) {
            uint32_t len = (uint32_t)std::min((size_t)read_len, seq.size() - pos);
            reads.push_back(PseudoRead{(uint32_t)r, pos, len});
            pos += len;
        }
    }
    rec_read_offsets[records.size()] = (uint32_t)reads.size();
    uint32_t num_reads = (uint32_t)reads.size();

    std::vector<uint32_t> read_lengths(num_reads);
    for (uint32_t i = 0; i < num_reads; i++) read_lengths[i] = reads[i].len;

    HarcStats stats;
    stats.num_reads = num_reads;

    if (num_reads == 0) {
        // Still emit a well-formed (empty) file so downstream tooling doesn't special-case it.
        std::vector<uint8_t> out;
        write_magic(out, "HRC2");
        write_u32(out, (uint32_t)records.size());
        for (uint32_t v : hdr_offsets) write_u32(out, v);
        write_u32(out, hdr_total); write_u32(out, hdr_pad);
        write_bytes(out, headers_concat.data(), headers_concat.size()); pad_to(out, hdr_pad);
        write_u32(out, read_len);
        write_u32(out, 0);
        for (uint32_t v : rec_read_offsets) write_u32(out, v);
        // no per-read columns (num_reads==0) then the five empty pools:
        write_u32(out, 0); write_u32(out, 0); // raw_pool
        write_u32(out, 0); write_u32(out, 0); // prefix_pool
        write_u32(out, 0); write_u32(out, 0); // suffix_pool
        write_u32(out, 0); write_u32(out, 0); // mismatch_positions
        write_u32(out, 0); write_u32(out, 0); // mismatch_bases
        write_whole_file(output_path, out);
        return stats;
    }

    // ---- minimizer index ----
    std::vector<std::vector<Minimizer>> read_minimizers(num_reads);
    std::unordered_map<uint64_t, std::vector<uint32_t>> min_to_reads;
    min_to_reads.reserve((size_t)num_reads * 2);
    for (uint32_t i = 0; i < num_reads; i++) {
        compute_minimizers(records[reads[i].record_id].seq, reads[i].seq_begin, reads[i].len, read_minimizers[i]);
        for (const Minimizer& m : read_minimizers[i]) {
            auto& bucket = min_to_reads[m.hash];
            if (bucket.size() < MAX_BUCKET) bucket.push_back(i);
        }
    }

    // ---- greedy reorder by shared-minimizer similarity ----
    std::vector<uint8_t> visited(num_reads, 0);
    std::vector<uint32_t> order; order.reserve(num_reads);
    std::unordered_map<uint32_t, uint32_t> candidate_counts;
    for (uint32_t s = 0; s < num_reads; s++) {
        if (visited[s]) continue;
        uint32_t cur = s;
        visited[cur] = 1;
        order.push_back(cur);
        while (true) {
            candidate_counts.clear();
            for (const Minimizer& m : read_minimizers[cur]) {
                auto it = min_to_reads.find(m.hash);
                if (it == min_to_reads.end()) continue;
                for (uint32_t r : it->second) if (!visited[r]) candidate_counts[r]++;
            }
            if (candidate_counts.empty()) break;
            uint32_t best = UINT32_MAX, best_count = 0;
            for (auto& kv : candidate_counts) {
                if (kv.second > best_count || (kv.second == best_count && kv.first < best)) {
                    best = kv.first; best_count = kv.second;
                }
            }
            visited[best] = 1;
            order.push_back(best);
            cur = best;
        }
    }
    std::vector<uint32_t> order_to_orig = order; // order_to_orig[i] = original read index at reordered position i

    // ---- encode in reordered order, columnar, with seed-and-extend alignment ----
    std::vector<uint8_t> flags(num_reads, 0);
    std::vector<uint16_t> ov_start_cur(num_reads, 0), ov_start_prev(num_reads, 0), ov_len_col(num_reads, 0);
    std::vector<uint16_t> mismatch_counts(num_reads, 0);
    std::vector<uint8_t> raw_pool, prefix_pool, suffix_pool, mismatch_positions_bytes, mismatch_bases;

    std::string prev_seq_buf;
    uint32_t prev_orig_idx = UINT32_MAX;
    bool have_prev = false;
    std::vector<uint16_t> mism_pos_tmp;

    for (uint32_t oi = 0; oi < num_reads; oi++) {
        uint32_t ri = order_to_orig[oi];
        const PseudoRead& pr = reads[ri];
        const std::string& seq = records[pr.record_id].seq;
        const char* cur_ptr = seq.data() + pr.seq_begin;
        uint32_t len = pr.len;

        bool use_delta = false;
        uint32_t best_ov_start_cur = 0, best_ov_start_prev = 0, best_ov_len = 0;

        if (have_prev) {
            // Try every shared-hash anchor between cur's and prev's minimizers;
            // keep whichever yields the largest usable overlap.
            for (const Minimizer& cm : read_minimizers[ri]) {
                for (const Minimizer& pm : read_minimizers[prev_orig_idx]) {
                    if (cm.hash != pm.hash) continue;
                    uint32_t os_c, os_p, ov, mism;
                    if (try_align(cur_ptr, len, prev_seq_buf, cm.pos, pm.pos, mismatch_frac_max, os_c, os_p, ov, mism)) {
                        if (ov > best_ov_len) {
                            best_ov_len = ov; best_ov_start_cur = os_c; best_ov_start_prev = os_p;
                            use_delta = true;
                        }
                    }
                }
            }
        }

        if (use_delta) {
            flags[oi] = 1;
            ov_start_cur[oi] = (uint16_t)best_ov_start_cur;
            ov_start_prev[oi] = (uint16_t)best_ov_start_prev;
            ov_len_col[oi] = (uint16_t)best_ov_len;

            mism_pos_tmp.clear();
            for (uint32_t p = 0; p < best_ov_len; p++) {
                if (cur_ptr[best_ov_start_cur + p] != prev_seq_buf[best_ov_start_prev + p]) mism_pos_tmp.push_back((uint16_t)p);
            }
            mismatch_counts[oi] = (uint16_t)mism_pos_tmp.size();
            stats.mismatch_total += mism_pos_tmp.size();
            for (uint16_t p : mism_pos_tmp) { mismatch_positions_bytes.push_back((uint8_t)(p & 0xFF)); mismatch_positions_bytes.push_back((uint8_t)((p >> 8) & 0xFF)); }
            for (size_t i = 0; i < mism_pos_tmp.size(); i += 2) {
                uint8_t hi = base_to_code(cur_ptr[best_ov_start_cur + mism_pos_tmp[i]]);
                uint8_t lo = (i + 1 < mism_pos_tmp.size()) ? base_to_code(cur_ptr[best_ov_start_cur + mism_pos_tmp[i+1]]) : 0;
                mismatch_bases.push_back((uint8_t)((hi << 4) | lo));
            }
            // literal flanks around the aligned overlap
            pack_bases(seq, pr.seq_begin, best_ov_start_cur, prefix_pool);
            uint32_t suffix_begin = best_ov_start_cur + best_ov_len;
            pack_bases(seq, pr.seq_begin + suffix_begin, len - suffix_begin, suffix_pool);
            stats.n_delta++;
        } else {
            flags[oi] = 0;
            pack_bases(seq, pr.seq_begin, len, raw_pool);
            stats.n_raw++;
        }

        prev_seq_buf.assign(cur_ptr, len);
        prev_orig_idx = ri;
        have_prev = true;
    }

    // ---- assemble file ----
    std::vector<uint8_t> out;
    write_magic(out, "HRC2");
    write_u32(out, (uint32_t)records.size());
    for (uint32_t v : hdr_offsets) write_u32(out, v);
    write_u32(out, hdr_total); write_u32(out, hdr_pad);
    write_bytes(out, headers_concat.data(), headers_concat.size()); pad_to(out, hdr_pad);

    write_u32(out, read_len);
    write_u32(out, num_reads);
    for (uint32_t v : rec_read_offsets) write_u32(out, v);
    for (uint32_t v : read_lengths) write_u32(out, v);
    for (uint32_t v : order_to_orig) write_u32(out, v);

    write_bytes(out, flags.data(), flags.size());
    for (uint16_t v : ov_start_cur) write_u16(out, v);
    for (uint16_t v : ov_start_prev) write_u16(out, v);
    for (uint16_t v : ov_len_col) write_u16(out, v);
    for (uint16_t v : mismatch_counts) write_u16(out, v);

    uint32_t raw_pool_total = (uint32_t)raw_pool.size();
    uint32_t raw_pool_pad = (4 - (raw_pool_total % 4)) % 4;
    uint32_t prefix_pool_total = (uint32_t)prefix_pool.size();
    uint32_t prefix_pool_pad = (4 - (prefix_pool_total % 4)) % 4;
    uint32_t suffix_pool_total = (uint32_t)suffix_pool.size();
    uint32_t suffix_pool_pad = (4 - (suffix_pool_total % 4)) % 4;
    uint32_t mpos_total = (uint32_t)mismatch_positions_bytes.size();
    uint32_t mpos_pad = (4 - (mpos_total % 4)) % 4;
    uint32_t mbase_total = (uint32_t)mismatch_bases.size();
    uint32_t mbase_pad = (4 - (mbase_total % 4)) % 4;

    write_u32(out, raw_pool_total);    write_u32(out, raw_pool_pad);
    write_u32(out, prefix_pool_total); write_u32(out, prefix_pool_pad);
    write_u32(out, suffix_pool_total); write_u32(out, suffix_pool_pad);
    write_u32(out, mpos_total);        write_u32(out, mpos_pad);
    write_u32(out, mbase_total);       write_u32(out, mbase_pad);

    write_bytes(out, raw_pool.data(), raw_pool.size()); pad_to(out, raw_pool_pad);
    write_bytes(out, prefix_pool.data(), prefix_pool.size()); pad_to(out, prefix_pool_pad);
    write_bytes(out, suffix_pool.data(), suffix_pool.size()); pad_to(out, suffix_pool_pad);
    write_bytes(out, mismatch_positions_bytes.data(), mismatch_positions_bytes.size()); pad_to(out, mpos_pad);
    write_bytes(out, mismatch_bases.data(), mismatch_bases.size()); pad_to(out, mbase_pad);

    write_whole_file(output_path, out);
    return stats;
}

int main(int argc, char** argv) {
    if (argc < 4) {
        std::fprintf(stderr, "usage: %s <input_file> <output_dir> <num_threads> [read_len=200] [mismatch_frac=0.15]\n", argv[0]);
        return 1;
    }
    std::string input_path = argv[1];
    std::string output_dir = argv[2];
    int num_threads = std::stoi(argv[3]);
    uint32_t read_len = argc > 4 ? (uint32_t)std::stoul(argv[4]) : 200;
    double mismatch_frac = argc > 5 ? std::stod(argv[5]) : 0.15;

    try {
        MappedFile file(input_path);
        fs::create_directories(output_dir);

        if (file.size == 0) {
            std::fprintf(stderr, "empty input: %s\n", input_path.c_str());
            return 1;
        }

        // Same chunk-boundary-snapping algorithm as biocompress_preprocessor.cpp
        // (FASTA/FASTA_PACKED branch) so HARC and FAV4 chunks line up 1:1.
        size_t max_chunk_bytes = 450ull * 1024ull * 1024ull;
        const size_t requested_chunks = (size_t)std::max(1, num_threads);
        size_t size_based_chunks = (file.size + max_chunk_bytes - 1) / max_chunk_bytes;
        if (size_based_chunks == 0) size_based_chunks = 1;
        size_t chunk_count = std::max(requested_chunks, size_based_chunks);
        size_t target_chunk_size = (file.size + chunk_count - 1) / chunk_count;

        std::vector<const char*> split_points;
        split_points.reserve(chunk_count + 1);
        split_points.push_back(file.data);
        for (size_t i = 1; i < chunk_count; ++i) {
            const char* target = file.data + i * target_chunk_size;
            const char* end = file.data + file.size;
            if (target >= end) { split_points.push_back(end); continue; }
            const char* cursor = target;
            while (cursor < end) {
                if (*cursor == '>' && *(cursor - 1) == '\n') break;
                cursor++;
            }
            split_points.push_back(cursor);
        }
        split_points.push_back(file.data + file.size);

        const size_t total_chunks = split_points.size() - 1;
        const size_t worker_count = std::max<size_t>(1, std::min<size_t>(total_chunks, requested_chunks));
        std::atomic<size_t> next_chunk{0};
        std::vector<HarcStats> chunk_stats(total_chunks);

        auto process_chunk = [&](size_t idx) {
            const char* start = split_points[idx];
            const char* end = split_points[idx + 1];
            if (start >= end) return;
            char filename[256];
            std::snprintf(filename, sizeof(filename), "chunk_%05zu.harc_packed.bin", idx);
            std::string out_path = output_dir + "/" + filename;
            chunk_stats[idx] = process_fasta_harc_chunk(start, end, out_path, read_len, mismatch_frac);
        };

        std::vector<std::thread> threads;
        threads.reserve(worker_count);
        for (size_t t = 0; t < worker_count; ++t) {
            threads.emplace_back([&]() {
                while (true) {
                    size_t idx = next_chunk.fetch_add(1);
                    if (idx >= total_chunks) break;
                    process_chunk(idx);
                }
            });
        }
        for (auto& t : threads) t.join();

        uint64_t tot_reads = 0, tot_raw = 0, tot_delta = 0, tot_mismatch = 0;
        for (auto& s : chunk_stats) { tot_reads += s.num_reads; tot_raw += s.n_raw; tot_delta += s.n_delta; tot_mismatch += s.mismatch_total; }
        std::fprintf(stderr,
            "harc_preprocessor: chunks=%zu reads=%llu raw=%llu delta=%llu (%.1f%% delta) "
            "avg_mismatch/delta=%.2f read_len=%u\n",
            total_chunks, (unsigned long long)tot_reads, (unsigned long long)tot_raw, (unsigned long long)tot_delta,
            tot_reads ? 100.0 * tot_delta / tot_reads : 0.0,
            tot_delta ? (double)tot_mismatch / tot_delta : 0.0, read_len);

        std::cout << "Processing complete. Output in " << output_dir << std::endl;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "Error: %s\n", e.what());
        return 1;
    }
    return 0;
}
