/*
 * fasta_codec.cpp — Lossless FASTA encoder/decoder for the Nyx compression pipeline.
 *
 * Usage:
 *   fasta_codec encode    <input.fasta> <output_dir> [num_threads]
 *   fasta_codec decode    <streams_dir> <output.fasta>
 *   fasta_codec validate  <streams_dir>
 *
 * ENCODE produces 8 stream files in output_dir:
 *   meta.bin, headers.bin, nmask.bin, acgtmask.bin,
 *   bases2.bin, exceptions.bin, case.bin, wrapping.bin
 *
 * DECODE reads those 8 stream files and reconstructs the original FASTA
 * byte-for-byte (same case, IUPAC codes, line wrapping, newline style,
 * trailing-newline presence).
 *
 * Compile:
 *   g++ -O3 -std=c++17 -pthread -o fasta_codec fasta_codec.cpp
 */

#include "codec_common.h"
#include <atomic>
#include <thread>

// ============================================================================
// FASTA-specific constants and metadata
// ============================================================================

static constexpr u32 META_MAGIC   = 0x4346584E; // "NXFC" little-endian
static constexpr u32 META_VERSION = 1;

#pragma pack(push, 1)
struct RecordMeta {
    u32 seq_len;          // L: total sequence length (chars, no line breaks)
    u32 header_len;       // header bytes (without leading '>')
    u32 nmask_bytes;      // byte size of bit-packed N-mask
    u32 acgtmask_bytes;   // byte size of bit-packed ACGT-mask
    u32 bases2_bytes;     // byte size of 2-bit packed bases
    u32 exceptions_bytes; // byte size of exceptions blob
    u32 case_bytes;       // byte size of case data
    u32 wrap_bytes;       // byte size of wrapping data
    u8  case_mode;        // 0=none, 1=bitmask, 2=sparse
    u8  pad[3];           // alignment padding
};
#pragma pack(pop)

static_assert(sizeof(RecordMeta) == 36, "RecordMeta must be 36 bytes");

// ============================================================================
// Parsed FASTA record
// ============================================================================

struct FastaRecord {
    std::string header;
    std::string raw_seq;
    std::vector<u32> line_lengths;
};

// ============================================================================
// Encoded record buffers (for parallel encoding)
// ============================================================================

struct EncodedRecord {
    RecordMeta meta;
    std::vector<u8> header_buf;
    std::vector<u8> nmask_buf;
    std::vector<u8> acgtmask_buf;
    std::vector<u8> bases2_buf;
    std::vector<u8> exceptions_buf;
    std::vector<u8> case_buf;
    std::vector<u8> wrapping_buf;
};

// ============================================================================
// Parse a batch of FASTA records from memory-mapped data.
// Updates `pos` in-place so subsequent calls continue where we left off.
// Returns the number of records parsed (0 means EOF).
// ============================================================================

static constexpr u32 BATCH_SIZE = 500000; // records per batch

static u32 parse_batch(const char* data, size_t size, size_t& pos,
                        std::vector<FastaRecord>& records, u32 max_records) {
    records.clear();
    u32 count = 0;

    while (pos < size && count < max_records) {
        if (data[pos] != '>') { pos++; continue; }

        FastaRecord rec;

        // Header line (skip leading '>')
        size_t hdr_start = pos + 1;
        while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
        rec.header = std::string(data + hdr_start, pos - hdr_start);
        skip_newline(data, size, pos);

        // Sequence lines
        while (pos < size && data[pos] != '>') {
            size_t line_start = pos;
            while (pos < size && data[pos] != '\n' && data[pos] != '\r') pos++;
            u32 line_len = static_cast<u32>(pos - line_start);
            rec.raw_seq.append(data + line_start, line_len);
            rec.line_lengths.push_back(line_len);
            skip_newline(data, size, pos);
        }

        records.push_back(std::move(rec));
        count++;
    }
    return count;
}

// ============================================================================
// Encode a single record into buffers (thread-safe, no I/O)
// ============================================================================

static EncodedRecord encode_one(const FastaRecord& rec) {
    EncodedRecord er{};
    const std::string& seq = rec.raw_seq;
    u32 L = static_cast<u32>(seq.size());

    er.meta.seq_len = L;
    er.meta.header_len = static_cast<u32>(rec.header.size());

    // Header bytes
    er.header_buf.assign(rec.header.begin(), rec.header.end());

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

    // Wrapping
    er.wrapping_buf = encode_wrapping(rec.line_lengths, L);
    er.meta.wrap_bytes = static_cast<u32>(er.wrapping_buf.size());

    return er;
}

// ============================================================================
// ENCODE (with optional parallelism)
// ============================================================================

static int do_encode(const char* input_path, const char* output_dir, int num_threads) {
    MappedFile mf;
    if (!mf.open(input_path)) return 1;
    if (mf.size == 0) { fprintf(stderr, "Input file is empty\n"); return 1; }

    // Detect newline style and trailing newline
    u8 newline_style = detect_newline_style(mf.data, mf.size);
    u8 has_trailing_nl = detect_trailing_newline(mf.data, mf.size);

    // Open all output files
    mkdir_recursive(output_dir);
    std::string dir(output_dir);

    auto open_stream = [&](const char* name) -> FILE* {
        std::string path = dir + "/" + name;
        FILE* f = fopen(path.c_str(), "wb");
        if (!f) fprintf(stderr, "Cannot create %s\n", path.c_str());
        return f;
    };

    FILE* f_meta       = open_stream("meta.bin");
    FILE* f_headers    = open_stream("headers.bin");
    FILE* f_nmask      = open_stream("nmask.bin");
    FILE* f_acgtmask   = open_stream("acgtmask.bin");
    FILE* f_bases2     = open_stream("bases2.bin");
    FILE* f_exceptions = open_stream("exceptions.bin");
    FILE* f_case       = open_stream("case.bin");
    FILE* f_wrapping   = open_stream("wrapping.bin");

    if (!f_meta || !f_headers || !f_nmask || !f_acgtmask ||
        !f_bases2 || !f_exceptions || !f_case || !f_wrapping) return 1;

    // Write meta.bin global header with placeholder record count (updated at end)
    write_u32(f_meta, META_MAGIC);
    write_u32(f_meta, META_VERSION);
    write_u32(f_meta, 0); // placeholder — will seek back to update
    write_u8(f_meta, newline_style);
    write_u8(f_meta, has_trailing_nl);
    u8 reserved[6] = {0};
    write_bytes(f_meta, reserved, 6);

    // Process records in batches to bound memory usage
    size_t parse_pos = 0;
    u32 total_records = 0;
    std::vector<FastaRecord> records;
    u32 batch_num = 0;

    while (parse_pos < mf.size) {
        // Parse one batch
        u32 batch_count = parse_batch(mf.data, mf.size, parse_pos,
                                       records, BATCH_SIZE);
        if (batch_count == 0) break;

        // Encode batch (parallel)
        std::vector<EncodedRecord> encoded(batch_count);
        if (num_threads <= 1 || batch_count <= 1) {
            for (u32 i = 0; i < batch_count; i++) {
                encoded[i] = encode_one(records[i]);
            }
        } else {
            int nt = std::min(num_threads, static_cast<int>(batch_count));
            std::atomic<u32> next_idx{0};
            std::vector<std::thread> threads;
            for (int t = 0; t < nt; t++) {
                threads.emplace_back([&]() {
                    while (true) {
                        u32 i = next_idx.fetch_add(1, std::memory_order_relaxed);
                        if (i >= batch_count) break;
                        encoded[i] = encode_one(records[i]);
                    }
                });
            }
            for (auto& th : threads) th.join();
        }

        // Write batch to streams
        for (u32 i = 0; i < batch_count; i++) {
            const auto& er = encoded[i];
            write_bytes(f_meta, &er.meta, sizeof(RecordMeta));

            write_bytes(f_headers,    er.header_buf.data(),    er.header_buf.size());
            write_bytes(f_nmask,      er.nmask_buf.data(),     er.nmask_buf.size());
            write_bytes(f_acgtmask,   er.acgtmask_buf.data(),  er.acgtmask_buf.size());
            write_bytes(f_bases2,     er.bases2_buf.data(),     er.bases2_buf.size());
            write_bytes(f_exceptions, er.exceptions_buf.data(), er.exceptions_buf.size());
            write_bytes(f_case,       er.case_buf.data(),       er.case_buf.size());
            write_bytes(f_wrapping,   er.wrapping_buf.data(),   er.wrapping_buf.size());
        }

        total_records += batch_count;
        batch_num++;
        fprintf(stderr, "  batch %u: encoded %u records (total: %u)\n",
                batch_num, batch_count, total_records);

        // Free batch memory before next iteration
        records.clear();
        records.shrink_to_fit();
    }

    // Seek back and write the actual record count
    fseek(f_meta, 8, SEEK_SET); // offset of record count field
    write_u32(f_meta, total_records);

    fclose(f_meta); fclose(f_headers); fclose(f_nmask); fclose(f_acgtmask);
    fclose(f_bases2); fclose(f_exceptions); fclose(f_case); fclose(f_wrapping);

    fprintf(stderr, "Encoded %u records in %u batch(es) (%d thread%s), newline=%s, trailing_nl=%d\n",
            total_records, batch_num, num_threads, num_threads == 1 ? "" : "s",
            newline_style == NL_CRLF ? "CRLF" : "LF", has_trailing_nl);
    return 0;
}

// ============================================================================
// DECODE
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
    if (version != META_VERSION) {
        fprintf(stderr, "Unsupported version: %u\n", version); fclose(fm); return 1;
    }
    u32 num_records = read_u32(fm);
    u8 newline_style = read_u8(fm);
    u8 has_trailing_nl = read_u8(fm);
    u8 skip_reserved[6]; read_bytes(fm, skip_reserved, 6);

    std::vector<RecordMeta> metas(num_records);
    for (u32 i = 0; i < num_records; i++) {
        read_bytes(fm, &metas[i], sizeof(RecordMeta));
    }
    fclose(fm);

    // Read stream files
    auto headers_data    = read_file_bytes(dir + "/headers.bin");
    auto nmask_data      = read_file_bytes(dir + "/nmask.bin");
    auto acgtmask_data   = read_file_bytes(dir + "/acgtmask.bin");
    auto bases2_data     = read_file_bytes(dir + "/bases2.bin");
    auto exceptions_data = read_file_bytes(dir + "/exceptions.bin");
    auto case_data       = read_file_bytes(dir + "/case.bin");
    auto wrapping_data   = read_file_bytes(dir + "/wrapping.bin");

    FILE* out = fopen(output_path, "wb");
    if (!out) { fprintf(stderr, "Cannot create %s\n", output_path); return 1; }

    const char* nl = (newline_style == NL_CRLF) ? "\r\n" : "\n";
    size_t nl_len = (newline_style == NL_CRLF) ? 2 : 1;

    // Stream cursors
    size_t hdr_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, wr_off = 0;

    for (u32 r = 0; r < num_records; r++) {
        const RecordMeta& rm = metas[r];

        // Write header (prepend '>' stripped during encode)
        fwrite(">", 1, 1, out);
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

        // Decode wrapping using shared helper
        auto line_lengths = decode_wrapping(
            wrapping_data.data() + wr_off, rm.wrap_bytes, L);
        wr_off += rm.wrap_bytes;

        // Write sequence lines
        u32 seq_pos = 0;
        for (size_t k = 0; k < line_lengths.size(); k++) {
            u32 ll = line_lengths[k];
            fwrite(raw_seq.data() + seq_pos, 1, ll, out);
            seq_pos += ll;

            bool is_last = (r == num_records - 1) && (k == line_lengths.size() - 1);
            if (is_last && !has_trailing_nl) {
                // Don't write trailing newline
            } else {
                fwrite(nl, 1, nl_len, out);
            }
        }
    }

    fclose(out);
    fprintf(stderr, "Decoded %u records\n", num_records);
    return 0;
}

// ============================================================================
// VALIDATE — check stream invariants without decoding
// ============================================================================

static int do_validate(const char* streams_dir) {
    std::string dir(streams_dir);
    int errors = 0;

    auto meta = read_file_bytes(dir + "/meta.bin");
    static constexpr size_t GLOBAL_HDR = 20;
    if (meta.size() < GLOBAL_HDR) {
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
    if (version != META_VERSION) {
        fprintf(stderr, "FAIL: unsupported version %u\n", version);
        return 1;
    }

    size_t expected_meta = GLOBAL_HDR + num_records * sizeof(RecordMeta);
    if (meta.size() < expected_meta) {
        fprintf(stderr, "FAIL: meta.bin too small for %u records\n", num_records);
        return 1;
    }

    fprintf(stderr, "Validating %u records (nl=%s, trail_nl=%d)...\n",
            num_records, nl_style == NL_CRLF ? "CRLF" : "LF", trail_nl);

    auto headers_data    = read_file_bytes(dir + "/headers.bin");
    auto nmask_data      = read_file_bytes(dir + "/nmask.bin");
    auto acgtmask_data   = read_file_bytes(dir + "/acgtmask.bin");
    auto bases2_data     = read_file_bytes(dir + "/bases2.bin");
    auto exceptions_data = read_file_bytes(dir + "/exceptions.bin");
    auto case_data       = read_file_bytes(dir + "/case.bin");
    auto wrapping_data   = read_file_bytes(dir + "/wrapping.bin");

    size_t hdr_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, wr_off = 0;

    for (u32 r = 0; r < num_records; r++) {
        RecordMeta rm;
        memcpy(&rm, meta.data() + GLOBAL_HDR + r * sizeof(RecordMeta), sizeof(RecordMeta));
        u32 L = rm.seq_len;

        // Header bounds
        if (hdr_off + rm.header_len > headers_data.size()) {
            fprintf(stderr, "FAIL: record[%u]: header overflows\n", r); errors++;
        }
        hdr_off += rm.header_len;

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

        // Wrapping
        if (wr_off < wrapping_data.size()) {
            u8 wm = wrapping_data[wr_off];
            if (wm == WRAP_COMPACT && rm.wrap_bytes >= 9) {
                u32 width, last_len;
                memcpy(&width, wrapping_data.data() + wr_off + 1, 4);
                memcpy(&last_len, wrapping_data.data() + wr_off + 5, 4);
                u32 total;
                if (width == 0) { total = last_len; }
                else if (last_len == width) { total = (L / width) * width; }
                else { total = ((L > width) ? (L - last_len) / width : 0) * width + last_len; }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: COMPACT total=%u != L=%u\n",
                            r, total, L); errors++;
                }
            } else if (wm == WRAP_EXPLICIT && rm.wrap_bytes >= 5) {
                u32 num_lines;
                memcpy(&num_lines, wrapping_data.data() + wr_off + 1, 4);
                u32 total = 0;
                for (u32 k = 0; k < num_lines && (wr_off + 5 + (k+1)*4) <= wrapping_data.size(); k++) {
                    u32 ll;
                    memcpy(&ll, wrapping_data.data() + wr_off + 5 + k * 4, 4);
                    total += ll;
                }
                if (total != L) {
                    fprintf(stderr, "FAIL: record[%u]: EXPLICIT sum=%u != L=%u\n",
                            r, total, L); errors++;
                }
            }
        }
        wr_off += rm.wrap_bytes;
    }

    // Verify all streams consumed exactly
    auto check = [&](const char* name, size_t cursor, size_t total) {
        if (cursor != total) {
            fprintf(stderr, "FAIL: %s: consumed %zu / %zu bytes\n", name, cursor, total);
            errors++;
        }
    };
    check("headers.bin",    hdr_off, headers_data.size());
    check("nmask.bin",      nm_off,  nmask_data.size());
    check("acgtmask.bin",   am_off,  acgtmask_data.size());
    check("bases2.bin",     b2_off,  bases2_data.size());
    check("exceptions.bin", ex_off,  exceptions_data.size());
    check("case.bin",       cs_off,  case_data.size());
    check("wrapping.bin",   wr_off,  wrapping_data.size());

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
        "  fasta_codec encode    <input.fasta> <output_dir> [num_threads]\n"
        "  fasta_codec decode    <streams_dir> <output.fasta>\n"
        "  fasta_codec validate  <streams_dir>\n");
}

int main(int argc, char* argv[]) {
    if (argc < 2) { usage(); return 1; }
    std::string cmd = argv[1];

    if (cmd == "encode") {
        if (argc < 4 || argc > 5) {
            fprintf(stderr, "encode: <input.fasta> <output_dir> [num_threads]\n");
            return 1;
        }
        int threads = 1;
        if (argc == 5) threads = std::max(1, atoi(argv[4]));
        return do_encode(argv[2], argv[3], threads);
    } else if (cmd == "decode") {
        if (argc != 4) {
            fprintf(stderr, "decode: <streams_dir> <output.fasta>\n");
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
