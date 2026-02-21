/*
 * fastq_codec.cpp — Lossless FASTQ encoder/decoder for the Nyx compression pipeline.
 *
 * v2 changes over v1:
 *   - Extended meta header: stores header LCP prefix, quality layout, fixed seq len
 *   - headers.bin stores suffixes only (common prefix stripped)
 *   - quality.bin may be in columnar layout (position-major) for fixed-length reads
 *   - Quality encoding always raw (no per-record delta) in v2
 *   - Decoder supports both v1 and v2 meta formats
 *
 * Usage:
 *   fastq_codec encode    <input.fastq> <output_dir> [num_threads]
 *   fastq_codec decode    <streams_dir> <output.fastq>
 *   fastq_codec validate  <streams_dir>
 *
 * ENCODE produces 11 stream files in output_dir:
 *   meta.bin, headers.bin, plus.bin, nmask.bin, acgtmask.bin,
 *   bases2.bin, exceptions.bin, case.bin, seq_wrap.bin,
 *   qual_wrap.bin, quality.bin
 *
 * Compile:
 *   g++ -O3 -std=c++17 -pthread -o fastq_codec fastq_codec.cpp
 */

#include "codec_common.h"
#include <thread>
#include <algorithm>

// ============================================================================
// Constants
// ============================================================================

static constexpr u32 META_MAGIC   = 0x4E584651; // "NXFQ" little-endian
static constexpr u32 META_VERSION_V1 = 1;
static constexpr u32 META_VERSION_V2 = 2;

static constexpr u8 QUAL_RAW   = 0;
static constexpr u8 QUAL_DELTA = 1;

// v2 quality layout modes
static constexpr u8 QLAYOUT_PER_RECORD    = 0;
static constexpr u8 QLAYOUT_COLUMNAR      = 1;  // v2 layout=1: single quality.bin, column-major
static constexpr u8 QLAYOUT_PER_POSITION  = 2;  // v3 layout=2: per-position files + delta encoding
static constexpr u8 QLAYOUT_PER_POS_RAW   = 3;  // v3 layout=3: per-position files, raw (no delta)

// Max sequence length for columnar/per-position quality (limits file count)
static constexpr u32 MAX_COLUMNAR_SEQ_LEN = 1000;

// ============================================================================
// Metadata struct (same layout for v1 and v2 per-record data)
// ============================================================================

#pragma pack(push, 1)
struct FastqRecordMeta {
    u32 seq_len;
    u32 header_len;        // v2: suffix length (prefix stripped)
    u32 plus_len;
    u32 nmask_bytes;
    u32 acgtmask_bytes;
    u32 bases2_bytes;
    u32 exceptions_bytes;
    u32 case_bytes;
    u32 seq_wrap_bytes;
    u32 qual_wrap_bytes;
    u32 quality_bytes;
    u8  case_mode;
    u8  quality_mode;      // v2: always QUAL_RAW
    u8  pad[2];
};
#pragma pack(pop)

static_assert(sizeof(FastqRecordMeta) == 48, "FastqRecordMeta must be 48 bytes");

// ============================================================================
// Parsed FASTQ record
// ============================================================================

struct FastqRecord {
    std::string header;
    std::string raw_seq;
    std::vector<u32> seq_line_lengths;
    std::string plus_comment;
    std::string raw_qual;
    std::vector<u32> qual_line_lengths;
};

// ============================================================================
// Encoded record buffers (for parallel encoding)
// ============================================================================

struct EncodedRecord {
    FastqRecordMeta meta;
    std::vector<u8> header_buf;
    std::vector<u8> plus_buf;
    std::vector<u8> nmask_buf;
    std::vector<u8> acgtmask_buf;
    std::vector<u8> bases2_buf;
    std::vector<u8> exceptions_buf;
    std::vector<u8> case_buf;
    std::vector<u8> seq_wrap_buf;
    std::vector<u8> qual_wrap_buf;
    std::vector<u8> quality_buf;
};

// ============================================================================
// Parse a batch of FASTQ records from memory-mapped data.
// ============================================================================

static constexpr u32 BATCH_SIZE = 500000;

static u32 parse_batch(const char* data, size_t size, size_t& pos,
                        std::vector<FastqRecord>& records, u32 max_records) {
    records.clear();
    u32 count = 0;

    while (pos < size && count < max_records) {
        if (data[pos] != '@') { pos++; continue; }

        FastqRecord rec;

        // Header line
        size_t hdr_start = pos;
        while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
        rec.header = std::string(data + hdr_start, pos - hdr_start);
        skip_newline(data, size, pos);

        // Sequence lines until '+' line
        while (pos < size && data[pos] != '+') {
            size_t line_start = pos;
            while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
            u32 line_len = static_cast<u32>(pos - line_start);
            rec.raw_seq.append(data + line_start, line_len);
            rec.seq_line_lengths.push_back(line_len);
            skip_newline(data, size, pos);
        }

        // Plus line
        if (pos < size && data[pos] == '+') {
            size_t plus_start = pos + 1;
            while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
            rec.plus_comment = std::string(data + plus_start, pos - plus_start);
            skip_newline(data, size, pos);
        }

        // Quality lines
        u32 qual_needed = static_cast<u32>(rec.raw_seq.size());
        u32 qual_collected = 0;
        while (qual_collected < qual_needed && pos < size) {
            size_t line_start = pos;
            while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
            u32 line_len = static_cast<u32>(pos - line_start);
            rec.raw_qual.append(data + line_start, line_len);
            rec.qual_line_lengths.push_back(line_len);
            qual_collected += line_len;
            skip_newline(data, size, pos);
        }

        records.push_back(std::move(rec));
        count++;
    }
    return count;
}

// ============================================================================
// Compute longest common prefix of headers in a batch
// ============================================================================

static std::string compute_lcp(const std::vector<FastqRecord>& records) {
    if (records.empty()) return "";
    const std::string& first = records[0].header;
    size_t prefix_len = first.size();
    for (size_t i = 1; i < records.size() && prefix_len > 0; i++) {
        const std::string& h = records[i].header;
        size_t min_len = std::min(prefix_len, h.size());
        size_t j = 0;
        while (j < min_len && first[j] == h[j]) j++;
        prefix_len = j;
    }
    return first.substr(0, prefix_len);
}

// ============================================================================
// Check if all records in a batch have the same sequence length
// ============================================================================

static u32 check_uniform_seq_len(const std::vector<FastqRecord>& records) {
    if (records.empty()) return 0;
    u32 len = static_cast<u32>(records[0].raw_seq.size());
    if (len == 0) return 0;
    for (size_t i = 1; i < records.size(); i++) {
        if (static_cast<u32>(records[i].raw_seq.size()) != len) return 0;
    }
    return len;
}

// ============================================================================
// Encode a single FASTQ record (thread-safe, no I/O)
// v2: strips header prefix, always raw quality
// ============================================================================

static EncodedRecord encode_one(const FastqRecord& rec, const std::string& prefix) {
    EncodedRecord er{};
    const std::string& seq = rec.raw_seq;
    const std::string& qual = rec.raw_qual;
    u32 L = static_cast<u32>(seq.size());

    er.meta.seq_len = L;
    er.meta.plus_len = static_cast<u32>(rec.plus_comment.size());

    // Header: strip prefix, store suffix only
    if (!prefix.empty() && rec.header.size() >= prefix.size() &&
        rec.header.compare(0, prefix.size(), prefix) == 0) {
        er.header_buf.assign(rec.header.begin() + prefix.size(), rec.header.end());
    } else {
        er.header_buf.assign(rec.header.begin(), rec.header.end());
    }
    er.meta.header_len = static_cast<u32>(er.header_buf.size());

    // Plus buffer
    er.plus_buf.assign(rec.plus_comment.begin(), rec.plus_comment.end());

    // Sequence encoding (N-mask, ACGT-mask, bases2, exceptions, case)
    auto se = encode_sequence(seq);
    er.nmask_buf      = std::move(se.nmask_packed);
    er.acgtmask_buf   = std::move(se.acgtmask_packed);
    er.bases2_buf     = std::move(se.bases2_packed);
    er.exceptions_buf = std::move(se.exceptions_buf);
    er.case_buf       = std::move(se.case_buf);

    er.meta.nmask_bytes      = se.nmask_bytes;
    er.meta.acgtmask_bytes   = se.acgtmask_bytes;
    er.meta.bases2_bytes     = se.bases2_bytes;
    er.meta.exceptions_bytes = se.exceptions_bytes;
    er.meta.case_bytes       = se.case_bytes;
    er.meta.case_mode        = se.case_mode;

    // Sequence wrapping
    er.seq_wrap_buf = encode_wrapping(rec.seq_line_lengths, L);
    er.meta.seq_wrap_bytes = static_cast<u32>(er.seq_wrap_buf.size());

    // Quality wrapping
    er.qual_wrap_buf = encode_wrapping(rec.qual_line_lengths, L);
    er.meta.qual_wrap_bytes = static_cast<u32>(er.qual_wrap_buf.size());

    // Quality: always raw in v2 (no delta)
    er.meta.quality_mode = QUAL_RAW;
    er.quality_buf.resize(L);
    for (u32 i = 0; i < L; i++) {
        er.quality_buf[i] = static_cast<u8>(qual[i]);
    }
    er.meta.quality_bytes = L;

    return er;
}

// ============================================================================
// ENCODE — v2 with prefix stripping and columnar quality
// ============================================================================

static int do_encode(const char* input_path, const char* output_dir, int num_threads) {
    MappedFile mf;
    if (!mf.open(input_path)) return 1;
    if (mf.size == 0) { fprintf(stderr, "Input file is empty\n"); return 1; }

    u8 newline_style = detect_newline_style(mf.data, mf.size);
    u8 has_trailing_nl = detect_trailing_newline(mf.data, mf.size);

    mkdir_recursive(output_dir);
    std::string dir(output_dir);

    auto open_stream = [&](const char* name) -> FILE* {
        std::string path = dir + "/" + name;
        FILE* f = fopen(path.c_str(), "wb");
        if (!f) fprintf(stderr, "Cannot create %s\n", path.c_str());
        return f;
    };

    // Parse first batch to determine prefix and uniform length
    size_t parse_pos = 0;
    std::vector<FastqRecord> records;
    u32 first_batch_count = parse_batch(mf.data, mf.size, parse_pos, records, BATCH_SIZE);
    if (first_batch_count == 0) {
        fprintf(stderr, "No FASTQ records found\n");
        return 1;
    }

    // Detect header prefix (LCP)
    std::string prefix = compute_lcp(records);
    u32 prefix_len = static_cast<u32>(prefix.size());

    // Detect uniform sequence length for per-position quality
    u32 fixed_seq_len = check_uniform_seq_len(records);
    u8 quality_layout = (fixed_seq_len > 0 && fixed_seq_len <= MAX_COLUMNAR_SEQ_LEN)
                        ? QLAYOUT_PER_POS_RAW : QLAYOUT_PER_RECORD;

    fprintf(stderr, "v2 encoder: prefix_len=%u, quality_layout=%s, fixed_seq_len=%u\n",
            prefix_len,
            quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
            quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
            quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record",
            fixed_seq_len);

    // Open output files
    FILE* f_meta       = open_stream("meta.bin");
    FILE* f_headers    = open_stream("headers.bin");
    FILE* f_plus       = open_stream("plus.bin");
    FILE* f_nmask      = open_stream("nmask.bin");
    FILE* f_acgtmask   = open_stream("acgtmask.bin");
    FILE* f_bases2     = open_stream("bases2.bin");
    FILE* f_exceptions = open_stream("exceptions.bin");
    FILE* f_case       = open_stream("case.bin");
    FILE* f_seq_wrap   = open_stream("seq_wrap.bin");
    FILE* f_qual_wrap  = open_stream("qual_wrap.bin");

    FILE* f_quality = nullptr;
    std::vector<FILE*> qual_pos_files;

    if (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) {
        // Open per-position output files (final names, no temp)
        qual_pos_files.resize(fixed_seq_len);
        for (u32 p = 0; p < fixed_seq_len; p++) {
            char fname[64];
            snprintf(fname, sizeof(fname), "quality_pos_%04u.bin", p);
            qual_pos_files[p] = open_stream(fname);
            if (!qual_pos_files[p]) return 1;
        }
    } else {
        f_quality = open_stream("quality.bin");
        if (!f_quality) return 1;
    }

    if (!f_meta || !f_headers || !f_plus || !f_nmask || !f_acgtmask ||
        !f_bases2 || !f_exceptions || !f_case || !f_seq_wrap || !f_qual_wrap) return 1;

    // Write v2 meta global header
    write_u32(f_meta, META_MAGIC);          // [0-3]
    write_u32(f_meta, META_VERSION_V2);     // [4-7]
    write_u32(f_meta, 0);                   // [8-11]  num_records placeholder
    write_u8(f_meta, newline_style);        // [12]
    write_u8(f_meta, has_trailing_nl);      // [13]
    write_u8(f_meta, quality_layout);       // [14]
    write_u8(f_meta, 0);                    // [15]    reserved
    write_u32(f_meta, fixed_seq_len);       // [16-19]
    write_u32(f_meta, prefix_len);          // [20-23]
    if (prefix_len > 0) {
        write_bytes(f_meta, prefix.data(), prefix_len); // [24..24+N-1]
    }

    // Process batches (first batch already parsed)
    u32 total_records = 0;
    u32 batch_num = 0;
    bool first_batch = true;

    // Delta encoding state for per-position quality (persists across batches)
    std::vector<u8> prev_byte;
    if (quality_layout == QLAYOUT_PER_POSITION) {  // only for delta mode
        prev_byte.assign(fixed_seq_len, 0);
    }

    while (true) {
        if (!first_batch) {
            u32 bc = parse_batch(mf.data, mf.size, parse_pos, records, BATCH_SIZE);
            if (bc == 0) break;
        }
        first_batch = false;
        u32 batch_count = static_cast<u32>(records.size());

        // Verify uniform seq_len if in per-position mode
        if (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) {
            for (u32 i = 0; i < batch_count; i++) {
                if (static_cast<u32>(records[i].raw_seq.size()) != fixed_seq_len) {
                    fprintf(stderr, "Error: record %u has seq_len %zu != fixed %u. "
                            "Cannot use columnar quality layout.\n",
                            total_records + i,
                            records[i].raw_seq.size(), fixed_seq_len);
                    return 1;
                }
            }
        }

        // Encode batch (parallel)
        std::vector<EncodedRecord> encoded(batch_count);
        if (num_threads <= 1 || batch_count <= 1) {
            for (u32 i = 0; i < batch_count; i++) {
                encoded[i] = encode_one(records[i], prefix);
            }
        } else {
            int nt = std::min(num_threads, static_cast<int>(batch_count));
            std::vector<std::thread> threads;
            u32 chunk = (batch_count + nt - 1) / nt;
            for (int t = 0; t < nt; t++) {
                u32 lo = t * chunk;
                u32 hi = std::min(lo + chunk, batch_count);
                if (lo >= hi) break;
                threads.emplace_back([&, lo, hi]() {
                    for (u32 i = lo; i < hi; i++) {
                        encoded[i] = encode_one(records[i], prefix);
                    }
                });
            }
            for (auto& th : threads) th.join();
        }

        // Write batch to streams
        for (u32 i = 0; i < batch_count; i++) {
            const auto& er = encoded[i];
            write_bytes(f_meta, &er.meta, sizeof(FastqRecordMeta));

            write_bytes(f_headers,    er.header_buf.data(),    er.header_buf.size());
            write_bytes(f_plus,       er.plus_buf.data(),      er.plus_buf.size());
            write_bytes(f_nmask,      er.nmask_buf.data(),     er.nmask_buf.size());
            write_bytes(f_acgtmask,   er.acgtmask_buf.data(),  er.acgtmask_buf.size());
            write_bytes(f_bases2,     er.bases2_buf.data(),     er.bases2_buf.size());
            write_bytes(f_exceptions, er.exceptions_buf.data(), er.exceptions_buf.size());
            write_bytes(f_case,       er.case_buf.data(),       er.case_buf.size());
            write_bytes(f_seq_wrap,   er.seq_wrap_buf.data(),  er.seq_wrap_buf.size());
            write_bytes(f_qual_wrap,  er.qual_wrap_buf.data(), er.qual_wrap_buf.size());
        }

        // Quality routing
        if (quality_layout == QLAYOUT_PER_POSITION) {
            // Buffer per position with delta encoding, then flush
            std::vector<std::vector<u8>> col_bufs(fixed_seq_len);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                col_bufs[p].reserve(batch_count);
            }
            for (u32 i = 0; i < batch_count; i++) {
                const auto& q = encoded[i].quality_buf;
                for (u32 p = 0; p < fixed_seq_len; p++) {
                    u8 cur = q[p];
                    u8 delta = static_cast<u8>((cur - prev_byte[p]) & 0xFF);
                    col_bufs[p].push_back(delta);
                    prev_byte[p] = cur;
                }
            }
            for (u32 p = 0; p < fixed_seq_len; p++) {
                write_bytes(qual_pos_files[p], col_bufs[p].data(), col_bufs[p].size());
            }
        } else if (quality_layout == QLAYOUT_PER_POS_RAW) {
            // Buffer per position, raw bytes (no delta), then flush
            std::vector<std::vector<u8>> col_bufs(fixed_seq_len);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                col_bufs[p].reserve(batch_count);
            }
            for (u32 i = 0; i < batch_count; i++) {
                const auto& q = encoded[i].quality_buf;
                for (u32 p = 0; p < fixed_seq_len; p++) {
                    col_bufs[p].push_back(q[p]);
                }
            }
            for (u32 p = 0; p < fixed_seq_len; p++) {
                write_bytes(qual_pos_files[p], col_bufs[p].data(), col_bufs[p].size());
            }
        } else {
            for (u32 i = 0; i < batch_count; i++) {
                write_bytes(f_quality, encoded[i].quality_buf.data(),
                            encoded[i].quality_buf.size());
            }
        }

        total_records += batch_count;
        batch_num++;
        fprintf(stderr, "  batch %u: encoded %u records (total: %u)\n",
                batch_num, batch_count, total_records);

        records.clear();
        records.shrink_to_fit();
    }

    // Finalize quality output
    if (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) {
        // Per-position files are already final — just close them
        for (u32 p = 0; p < fixed_seq_len; p++) {
            fclose(qual_pos_files[p]);
        }
    } else {
        fclose(f_quality);
    }

    // Seek back and write the actual record count
    fseek(f_meta, 8, SEEK_SET);
    write_u32(f_meta, total_records);

    fclose(f_meta); fclose(f_headers); fclose(f_plus);
    fclose(f_nmask); fclose(f_acgtmask); fclose(f_bases2);
    fclose(f_exceptions); fclose(f_case); fclose(f_seq_wrap);
    fclose(f_qual_wrap);

    fprintf(stderr, "Encoded %u records in %u batch(es) (%d thread%s), "
            "newline=%s, trailing_nl=%d, prefix=%u bytes, quality=%s\n",
            total_records, batch_num, num_threads,
            num_threads == 1 ? "" : "s",
            newline_style == NL_CRLF ? "CRLF" : "LF", has_trailing_nl,
            prefix_len,
            quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
            quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
            quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record");
    return 0;
}

// ============================================================================
// DECODE — supports both v1 and v2 meta formats
// ============================================================================

static int do_decode(const char* streams_dir, const char* output_path) {
    std::string dir(streams_dir);

    // Read meta.bin
    std::string meta_path = dir + "/meta.bin";
    FILE* fm = fopen(meta_path.c_str(), "rb");
    if (!fm) { fprintf(stderr, "Cannot open %s\n", meta_path.c_str()); return 1; }

    u32 magic = read_u32(fm);
    if (magic != META_MAGIC) {
        fprintf(stderr, "Bad magic: 0x%08X\n", magic); fclose(fm); return 1;
    }
    u32 version = read_u32(fm);
    if (version != META_VERSION_V1 && version != META_VERSION_V2) {
        fprintf(stderr, "Unsupported version: %u\n", version); fclose(fm); return 1;
    }

    u32 num_records = read_u32(fm);
    u8 newline_style = read_u8(fm);
    u8 has_trailing_nl = read_u8(fm);

    // v2 extended header fields
    u8 quality_layout = QLAYOUT_PER_RECORD;
    u32 fixed_seq_len = 0;
    std::string prefix;

    if (version == META_VERSION_V2) {
        quality_layout = read_u8(fm);       // [14]
        read_u8(fm);                         // [15] reserved
        fixed_seq_len = read_u32(fm);        // [16-19]
        u32 prefix_len = read_u32(fm);       // [20-23]
        if (prefix_len > 0) {
            prefix.resize(prefix_len);
            read_bytes(fm, &prefix[0], prefix_len);
        }
    } else {
        // v1: skip 6 reserved bytes
        u8 skip_reserved[6];
        read_bytes(fm, skip_reserved, 6);
    }

    // Read per-record metadata
    std::vector<FastqRecordMeta> metas(num_records);
    for (u32 i = 0; i < num_records; i++) {
        read_bytes(fm, &metas[i], sizeof(FastqRecordMeta));
    }
    fclose(fm);

    // Read stream files
    auto headers_data    = read_file_bytes(dir + "/headers.bin");
    auto plus_data       = read_file_bytes(dir + "/plus.bin");
    auto nmask_data      = read_file_bytes(dir + "/nmask.bin");
    auto acgtmask_data   = read_file_bytes(dir + "/acgtmask.bin");
    auto bases2_data     = read_file_bytes(dir + "/bases2.bin");
    auto exceptions_data = read_file_bytes(dir + "/exceptions.bin");
    auto case_data       = read_file_bytes(dir + "/case.bin");
    auto seq_wrap_data   = read_file_bytes(dir + "/seq_wrap.bin");
    auto qual_wrap_data  = read_file_bytes(dir + "/qual_wrap.bin");

    // Quality data: depends on layout
    std::vector<u8> quality_data;

    if (version == META_VERSION_V2 &&
        (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) &&
        fixed_seq_len > 0 && num_records > 0) {
        // v3 layout=2/3: read per-position files, transpose to row-major
        u64 total_quals = (u64)num_records * fixed_seq_len;
        quality_data.resize(total_quals);
        bool use_delta = (quality_layout == QLAYOUT_PER_POSITION);

        // Check for per-position files; fall back to quality.bin if not found
        char fname[64];
        snprintf(fname, sizeof(fname), "quality_pos_0000.bin");
        std::string probe_path = dir + "/" + fname;
        FILE* probe = fopen(probe_path.c_str(), "rb");
        if (probe) {
            fclose(probe);
            // Read per-position files, transpose (and undo delta if layout=2)
            for (u32 p = 0; p < fixed_seq_len; p++) {
                snprintf(fname, sizeof(fname), "quality_pos_%04u.bin", p);
                auto pos_data = read_file_bytes(dir + "/" + fname);
                if (pos_data.size() != num_records) {
                    fprintf(stderr, "Warning: %s has %zu bytes, expected %u\n",
                            fname, pos_data.size(), num_records);
                }
                u32 count = std::min((u32)pos_data.size(), num_records);
                if (use_delta) {
                    u8 prev = 0;
                    for (u32 r = 0; r < count; r++) {
                        u8 val = static_cast<u8>((prev + pos_data[r]) & 0xFF);
                        prev = val;
                        quality_data[(u64)r * fixed_seq_len + p] = val;
                    }
                } else {
                    for (u32 r = 0; r < count; r++) {
                        quality_data[(u64)r * fixed_seq_len + p] = pos_data[r];
                    }
                }
            }
        } else {
            // Backward compat: fall back to quality.bin with columnar layout
            fprintf(stderr, "Note: per-position files not found, falling back to quality.bin\n");
            quality_data = read_file_bytes(dir + "/quality.bin");
            u64 expected = (u64)num_records * fixed_seq_len;
            if (quality_data.size() == expected) {
                std::vector<u8> row_major(expected);
                for (u32 p = 0; p < fixed_seq_len; p++) {
                    u64 col_offset = (u64)p * num_records;
                    for (u32 r = 0; r < num_records; r++) {
                        row_major[(u64)r * fixed_seq_len + p] = quality_data[col_offset + r];
                    }
                }
                quality_data = std::move(row_major);
            }
        }
    } else if (version == META_VERSION_V2 && quality_layout == QLAYOUT_COLUMNAR &&
               fixed_seq_len > 0 && num_records > 0) {
        // v2 layout=1: single quality.bin, column-major → untranspose
        quality_data = read_file_bytes(dir + "/quality.bin");
        u64 total_quals = (u64)num_records * fixed_seq_len;
        if (quality_data.size() == total_quals) {
            std::vector<u8> row_major(total_quals);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                u64 col_offset = (u64)p * num_records;
                for (u32 r = 0; r < num_records; r++) {
                    row_major[(u64)r * fixed_seq_len + p] = quality_data[col_offset + r];
                }
            }
            quality_data = std::move(row_major);
        } else {
            fprintf(stderr, "Warning: quality.bin size %zu != expected %llu for columnar layout\n",
                    quality_data.size(), (unsigned long long)total_quals);
        }
    } else {
        // v1 or v2 layout=0: single quality.bin, row-major
        quality_data = read_file_bytes(dir + "/quality.bin");
    }

    FILE* out = fopen(output_path, "wb");
    if (!out) { fprintf(stderr, "Cannot create %s\n", output_path); return 1; }

    const char* nl = (newline_style == NL_CRLF) ? "\r\n" : "\n";
    size_t nl_len = (newline_style == NL_CRLF) ? 2 : 1;

    // Stream cursors
    size_t hdr_off = 0, plus_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, sw_off = 0, qw_off = 0, q_off = 0;

    for (u32 r = 0; r < num_records; r++) {
        const FastqRecordMeta& rm = metas[r];

        // Write header: prefix + suffix (v2) or full header (v1)
        if (version == META_VERSION_V2 && !prefix.empty()) {
            fwrite(prefix.data(), 1, prefix.size(), out);
        }
        fwrite(headers_data.data() + hdr_off, 1, rm.header_len, out);
        fwrite(nl, 1, nl_len, out);
        hdr_off += rm.header_len;

        u32 L = rm.seq_len;

        // Decode sequence using shared helper
        auto raw_seq = decode_sequence(
            L,
            nmask_data.data() + nm_off, rm.nmask_bytes,
            acgtmask_data.data() + am_off, rm.acgtmask_bytes,
            bases2_data.data() + b2_off, rm.bases2_bytes,
            exceptions_data.data() + ex_off, rm.exceptions_bytes,
            case_data.data() + cs_off, rm.case_bytes, rm.case_mode);

        nm_off += rm.nmask_bytes;
        am_off += rm.acgtmask_bytes;
        b2_off += rm.bases2_bytes;
        ex_off += rm.exceptions_bytes;
        cs_off += rm.case_bytes;

        // Write sequence lines
        auto seq_lines = decode_wrapping(
            seq_wrap_data.data() + sw_off, rm.seq_wrap_bytes, L);
        sw_off += rm.seq_wrap_bytes;

        u32 seq_pos = 0;
        for (size_t k = 0; k < seq_lines.size(); k++) {
            u32 ll = seq_lines[k];
            fwrite(raw_seq.data() + seq_pos, 1, ll, out);
            seq_pos += ll;
            fwrite(nl, 1, nl_len, out);
        }

        // Write plus line
        fwrite("+", 1, 1, out);
        if (rm.plus_len > 0) {
            fwrite(plus_data.data() + plus_off, 1, rm.plus_len, out);
        }
        fwrite(nl, 1, nl_len, out);
        plus_off += rm.plus_len;

        // Decode quality
        std::vector<u8> qual_bytes(L);
        if (version == META_VERSION_V2) {
            // v2: always raw (quality_data already untransposed if columnar)
            memcpy(qual_bytes.data(), quality_data.data() + q_off, L);
        } else {
            // v1: honor quality_mode
            if (rm.quality_mode == QUAL_RAW) {
                memcpy(qual_bytes.data(), quality_data.data() + q_off, L);
            } else {
                // QUAL_DELTA
                if (L > 0) {
                    qual_bytes[0] = quality_data[q_off];
                    for (u32 i = 1; i < L; i++) {
                        qual_bytes[i] = static_cast<u8>(
                            (qual_bytes[i - 1] + quality_data[q_off + i]) & 0xFF);
                    }
                }
            }
        }
        q_off += rm.quality_bytes;

        // Write quality lines
        auto qual_lines = decode_wrapping(
            qual_wrap_data.data() + qw_off, rm.qual_wrap_bytes, L);
        qw_off += rm.qual_wrap_bytes;

        u32 qual_pos = 0;
        for (size_t k = 0; k < qual_lines.size(); k++) {
            u32 ll = qual_lines[k];
            fwrite(qual_bytes.data() + qual_pos, 1, ll, out);
            qual_pos += ll;

            bool is_last = (r == num_records - 1) && (k == qual_lines.size() - 1);
            if (is_last && !has_trailing_nl) {
                // Don't write trailing newline
            } else {
                fwrite(nl, 1, nl_len, out);
            }
        }
    }

    fclose(out);
    fprintf(stderr, "Decoded %u records (version=%u)\n", num_records, version);
    return 0;
}

// ============================================================================
// VALIDATE — check stream invariants (v1 and v2)
// ============================================================================

static int do_validate(const char* streams_dir) {
    std::string dir(streams_dir);
    int errors = 0;

    auto meta = read_file_bytes(dir + "/meta.bin");
    if (meta.size() < 16) {
        fprintf(stderr, "FAIL: meta.bin too small (%zu bytes)\n", meta.size());
        return 1;
    }

    u32 magic, version, num_records;
    memcpy(&magic, meta.data(), 4);
    memcpy(&version, meta.data() + 4, 4);
    memcpy(&num_records, meta.data() + 8, 4);
    u8 nl_style = meta[12];
    u8 trail_nl = meta[13];

    if (magic != META_MAGIC) {
        fprintf(stderr, "FAIL: bad magic 0x%08X (expected 0x%08X)\n", magic, META_MAGIC);
        return 1;
    }
    if (version != META_VERSION_V1 && version != META_VERSION_V2) {
        fprintf(stderr, "FAIL: unsupported version %u\n", version);
        return 1;
    }

    // Parse v2 extended header
    u8 quality_layout = QLAYOUT_PER_RECORD;
    u32 fixed_seq_len = 0;
    u32 prefix_len = 0;
    size_t global_hdr_size = 0;

    if (version == META_VERSION_V2) {
        if (meta.size() < 24) {
            fprintf(stderr, "FAIL: meta.bin too small for v2 header\n");
            return 1;
        }
        quality_layout = meta[14];
        // meta[15] = reserved
        memcpy(&fixed_seq_len, meta.data() + 16, 4);
        memcpy(&prefix_len, meta.data() + 20, 4);
        global_hdr_size = 24 + prefix_len;
    } else {
        global_hdr_size = 20;
    }

    if (meta.size() < global_hdr_size) {
        fprintf(stderr, "FAIL: meta.bin too small for global header\n");
        return 1;
    }

    size_t expected_meta = global_hdr_size + num_records * sizeof(FastqRecordMeta);
    if (meta.size() < expected_meta) {
        fprintf(stderr, "FAIL: meta.bin too small for %u records\n", num_records);
        return 1;
    }

    fprintf(stderr, "Validating %u records (version=%u, nl=%s, trail_nl=%d",
            num_records, version,
            nl_style == NL_CRLF ? "CRLF" : "LF", trail_nl);
    if (version == META_VERSION_V2) {
        fprintf(stderr, ", quality=%s, fixed_seq_len=%u, prefix_len=%u",
                quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
                quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
                quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record",
                fixed_seq_len, prefix_len);
    }
    fprintf(stderr, ")...\n");

    auto headers_data    = read_file_bytes(dir + "/headers.bin");
    auto plus_data       = read_file_bytes(dir + "/plus.bin");
    auto nmask_data      = read_file_bytes(dir + "/nmask.bin");
    auto acgtmask_data   = read_file_bytes(dir + "/acgtmask.bin");
    auto bases2_data     = read_file_bytes(dir + "/bases2.bin");
    auto exceptions_data = read_file_bytes(dir + "/exceptions.bin");
    auto case_data       = read_file_bytes(dir + "/case.bin");
    auto seq_wrap_data   = read_file_bytes(dir + "/seq_wrap.bin");
    auto qual_wrap_data  = read_file_bytes(dir + "/qual_wrap.bin");

    // Quality validation depends on layout
    std::vector<u8> quality_data;
    if (version == META_VERSION_V2 &&
        (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) &&
        fixed_seq_len > 0) {
        // v3: validate per-position files
        for (u32 p = 0; p < fixed_seq_len; p++) {
            char fname[64];
            snprintf(fname, sizeof(fname), "quality_pos_%04u.bin", p);
            std::string fpath = dir + "/" + fname;
            auto pos_data = read_file_bytes(fpath);
            if (pos_data.size() != num_records) {
                fprintf(stderr, "FAIL: %s size %zu != expected %u records\n",
                        fname, pos_data.size(), num_records);
                errors++;
            }
        }
    } else {
        quality_data = read_file_bytes(dir + "/quality.bin");
        // For v2 columnar quality: validate total size
        if (version == META_VERSION_V2 && quality_layout == QLAYOUT_COLUMNAR && fixed_seq_len > 0) {
            u64 expected_qual = (u64)num_records * fixed_seq_len;
            if (quality_data.size() != expected_qual) {
                fprintf(stderr, "FAIL: quality.bin size %zu != expected %llu for columnar layout\n",
                        quality_data.size(), (unsigned long long)expected_qual);
                errors++;
            }
        }
    }

    size_t hdr_off = 0, plus_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, sw_off = 0, qw_off = 0, q_off = 0;

    for (u32 r = 0; r < num_records; r++) {
        FastqRecordMeta rm;
        memcpy(&rm, meta.data() + global_hdr_size + r * sizeof(FastqRecordMeta),
               sizeof(FastqRecordMeta));
        u32 L = rm.seq_len;

        // Header bounds
        if (hdr_off + rm.header_len > headers_data.size()) {
            fprintf(stderr, "FAIL: record[%u]: header overflows\n", r); errors++;
        }
        hdr_off += rm.header_len;

        // Plus bounds
        if (plus_off + rm.plus_len > plus_data.size()) {
            fprintf(stderr, "FAIL: record[%u]: plus overflows\n", r); errors++;
        }
        plus_off += rm.plus_len;

        // N-mask size
        u32 expected_nm = (L + 7) / 8;
        if (rm.nmask_bytes != expected_nm) {
            fprintf(stderr, "FAIL: record[%u]: nmask_bytes=%u, expected=%u\n",
                    r, rm.nmask_bytes, expected_nm); errors++;
        }

        // Compute L'
        u32 count_n = 0;
        if (nm_off + rm.nmask_bytes <= nmask_data.size()) {
            for (u32 i = 0; i < L; i++) {
                if ((nmask_data[nm_off + i / 8] >> (7 - (i % 8))) & 1) count_n++;
            }
        }
        u32 Lp = L - count_n;
        nm_off += rm.nmask_bytes;

        // ACGT-mask size
        u32 expected_am = (Lp + 7) / 8;
        if (rm.acgtmask_bytes != expected_am) {
            fprintf(stderr, "FAIL: record[%u]: acgtmask_bytes=%u, expected=%u\n",
                    r, rm.acgtmask_bytes, expected_am); errors++;
        }

        // Count ACGT
        u32 count_acgt = 0;
        if (am_off + rm.acgtmask_bytes <= acgtmask_data.size()) {
            for (u32 j = 0; j < Lp; j++) {
                if ((acgtmask_data[am_off + j / 8] >> (7 - (j % 8))) & 1) count_acgt++;
            }
        }
        am_off += rm.acgtmask_bytes;

        // bases2 size
        u32 expected_b2 = (count_acgt + 3) / 4;
        if (rm.bases2_bytes != expected_b2) {
            fprintf(stderr, "FAIL: record[%u]: bases2_bytes=%u, expected=%u\n",
                    r, rm.bases2_bytes, expected_b2); errors++;
        }
        b2_off += rm.bases2_bytes;

        // Exception count
        u32 expected_exc = Lp - count_acgt;
        u32 actual_exc = 0;
        {
            const u8* ptr = exceptions_data.data() + ex_off;
            const u8* end = ptr + rm.exceptions_bytes;
            while (ptr < end) {
                decode_varint(ptr, end);
                if (ptr < end) { ptr++; actual_exc++; }
            }
        }
        if (actual_exc != expected_exc) {
            fprintf(stderr, "FAIL: record[%u]: exceptions count=%u, expected=%u\n",
                    r, actual_exc, expected_exc); errors++;
        }
        ex_off += rm.exceptions_bytes;

        // Case mode
        if (rm.case_mode == CASE_NONE && rm.case_bytes != 0) {
            fprintf(stderr, "FAIL: record[%u]: case_mode=NONE but case_bytes=%u\n",
                    r, rm.case_bytes); errors++;
        } else if (rm.case_mode == CASE_MASK) {
            u32 expected_cs = (L + 7) / 8;
            if (rm.case_bytes != expected_cs) {
                fprintf(stderr, "FAIL: record[%u]: case_mode=MASK, bytes=%u, expected=%u\n",
                        r, rm.case_bytes, expected_cs); errors++;
            }
        }
        cs_off += rm.case_bytes;

        // Sequence wrapping
        if (sw_off < seq_wrap_data.size()) {
            u8 wm = seq_wrap_data[sw_off];
            if (wm == WRAP_COMPACT && rm.seq_wrap_bytes >= 9) {
                u32 width, last_len;
                memcpy(&width, seq_wrap_data.data() + sw_off + 1, 4);
                memcpy(&last_len, seq_wrap_data.data() + sw_off + 5, 4);
                u32 total;
                if (width == 0) { total = last_len; }
                else if (last_len == width) { total = (L / width) * width; }
                else { total = ((L > width) ? (L - last_len) / width : 0) * width + last_len; }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: seq COMPACT total=%u != L=%u\n",
                            r, total, L); errors++;
                }
            } else if (wm == WRAP_EXPLICIT && rm.seq_wrap_bytes >= 5) {
                u32 num_lines;
                memcpy(&num_lines, seq_wrap_data.data() + sw_off + 1, 4);
                u32 total = 0;
                for (u32 k = 0; k < num_lines && (sw_off + 5 + (k+1)*4) <= seq_wrap_data.size(); k++) {
                    u32 ll;
                    memcpy(&ll, seq_wrap_data.data() + sw_off + 5 + k * 4, 4);
                    total += ll;
                }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: seq EXPLICIT sum=%u != L=%u\n",
                            r, total, L); errors++;
                }
            }
        }
        sw_off += rm.seq_wrap_bytes;

        // Quality wrapping
        if (qw_off < qual_wrap_data.size()) {
            u8 wm = qual_wrap_data[qw_off];
            if (wm == WRAP_COMPACT && rm.qual_wrap_bytes >= 9) {
                u32 width, last_len;
                memcpy(&width, qual_wrap_data.data() + qw_off + 1, 4);
                memcpy(&last_len, qual_wrap_data.data() + qw_off + 5, 4);
                u32 total;
                if (width == 0) { total = last_len; }
                else if (last_len == width) { total = (L / width) * width; }
                else { total = ((L > width) ? (L - last_len) / width : 0) * width + last_len; }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: qual COMPACT total=%u != L=%u\n",
                            r, total, L); errors++;
                }
            } else if (wm == WRAP_EXPLICIT && rm.qual_wrap_bytes >= 5) {
                u32 num_lines;
                memcpy(&num_lines, qual_wrap_data.data() + qw_off + 1, 4);
                u32 total = 0;
                for (u32 k = 0; k < num_lines && (qw_off + 5 + (k+1)*4) <= qual_wrap_data.size(); k++) {
                    u32 ll;
                    memcpy(&ll, qual_wrap_data.data() + qw_off + 5 + k * 4, 4);
                    total += ll;
                }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: qual EXPLICIT sum=%u != L=%u\n",
                            r, total, L); errors++;
                }
            }
        }
        qw_off += rm.qual_wrap_bytes;

        // Quality bytes — for per-record quality, consume from cursor
        // For columnar/per-position quality, quality_bytes still equals seq_len per record
        if (version == META_VERSION_V2 &&
            (quality_layout == QLAYOUT_COLUMNAR || quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW)) {
            // Don't advance q_off — quality is validated globally above
            if (rm.quality_bytes != L) {
                fprintf(stderr, "FAIL: record[%u]: quality_bytes=%u != seq_len=%u\n",
                        r, rm.quality_bytes, L); errors++;
            }
        } else {
            if (rm.quality_bytes != L) {
                fprintf(stderr, "FAIL: record[%u]: quality_bytes=%u != seq_len=%u\n",
                        r, rm.quality_bytes, L); errors++;
            }
            q_off += rm.quality_bytes;
        }

        // Quality mode
        if (version == META_VERSION_V2) {
            if (rm.quality_mode != QUAL_RAW) {
                fprintf(stderr, "FAIL: record[%u]: v2 quality_mode=%u, expected 0 (RAW)\n",
                        r, rm.quality_mode); errors++;
            }
        } else {
            if (rm.quality_mode != QUAL_RAW && rm.quality_mode != QUAL_DELTA) {
                fprintf(stderr, "FAIL: record[%u]: unknown quality_mode=%u\n",
                        r, rm.quality_mode); errors++;
            }
        }
    }

    // Verify all streams consumed exactly
    auto check = [&](const char* name, size_t cursor, size_t total) {
        if (cursor != total) {
            fprintf(stderr, "FAIL: %s: consumed %zu / %zu bytes\n", name, cursor, total);
            errors++;
        }
    };
    check("headers.bin",    hdr_off,  headers_data.size());
    check("plus.bin",       plus_off, plus_data.size());
    check("nmask.bin",      nm_off,   nmask_data.size());
    check("acgtmask.bin",   am_off,   acgtmask_data.size());
    check("bases2.bin",     b2_off,   bases2_data.size());
    check("exceptions.bin", ex_off,   exceptions_data.size());
    check("case.bin",       cs_off,   case_data.size());
    check("seq_wrap.bin",   sw_off,   seq_wrap_data.size());
    check("qual_wrap.bin",  qw_off,   qual_wrap_data.size());
    // For per-record quality, check cursor; for columnar/per-position, already validated globally
    if (!(version == META_VERSION_V2 &&
          (quality_layout == QLAYOUT_COLUMNAR || quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW))) {
        check("quality.bin", q_off, quality_data.size());
    }

    if (errors == 0) {
        fprintf(stderr, "OK: all %u records pass invariant checks\n", num_records);
    } else {
        fprintf(stderr, "FAILED: %d invariant violation(s)\n", errors);
    }
    return errors > 0 ? 1 : 0;
}

// ============================================================================
// main
// ============================================================================

static void usage() {
    fprintf(stderr,
        "Usage:\n"
        "  fastq_codec encode    <input.fastq> <output_dir> [num_threads]\n"
        "  fastq_codec decode    <streams_dir> <output.fastq>\n"
        "  fastq_codec validate  <streams_dir>\n");
}

int main(int argc, char* argv[]) {
    if (argc < 2) { usage(); return 1; }
    std::string cmd = argv[1];

    if (cmd == "encode") {
        if (argc < 4 || argc > 5) {
            fprintf(stderr, "encode: <input.fastq> <output_dir> [num_threads]\n");
            return 1;
        }
        int threads = 1;
        if (argc == 5) threads = std::max(1, atoi(argv[4]));
        return do_encode(argv[2], argv[3], threads);
    } else if (cmd == "decode") {
        if (argc != 4) {
            fprintf(stderr, "decode: <streams_dir> <output.fastq>\n");
            return 1;
        }
        return do_decode(argv[2], argv[3]);
    } else if (cmd == "validate") {
        if (argc != 3) {
            fprintf(stderr, "validate: <streams_dir>\n");
            return 1;
        }
        return do_validate(argv[2]);
    } else {
        fprintf(stderr, "Unknown command: %s\n", cmd.c_str());
        usage();
        return 1;
    }
}
