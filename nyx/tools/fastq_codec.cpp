/*
 * fastq_codec.cpp — Lossless FASTQ encoder/decoder for the Nyx compression pipeline.
 *
 * v3 changes over v2:
 *   - Illumina header parsing with dictionary encoding (header_mode=1)
 *   - Sequential read number dropping (reconstructed on decode)
 *   - '@' stripped from headers (prepended during decode)
 *   - Atomic work-stealing for parallel encoding
 *   - Decoder supports v1, v2, and v3 meta formats
 *
 * v2 changes over v1:
 *   - Extended meta header: stores header LCP prefix, quality layout, fixed seq len
 *   - headers.bin stores suffixes only (common prefix stripped)
 *   - quality.bin may be in columnar layout (position-major) for fixed-length reads
 *   - Quality encoding always raw (no per-record delta) in v2
 *
 * Usage:
 *   fastq_codec encode    <input.fastq> <output_dir> [num_threads]
 *   fastq_codec decode    <streams_dir> <output.fastq>
 *   fastq_codec validate  <streams_dir>
 *
 * Compile:
 *   g++ -O3 -std=c++17 -pthread -o fastq_codec fastq_codec.cpp
 */

#include "codec_common.h"
#include <thread>
#include <atomic>
#include <algorithm>
#include <unordered_map>
#include <unordered_set>
#include <unistd.h>
#include <cstring>

// ============================================================================
// Constants
// ============================================================================

static constexpr u32 META_MAGIC   = 0x4E584651; // "NXFQ" little-endian
static constexpr u32 META_VERSION_V1 = 1;
static constexpr u32 META_VERSION_V2 = 2;
static constexpr u32 META_VERSION_V3 = 3;

static constexpr u8 QUAL_RAW   = 0;
static constexpr u8 QUAL_DELTA = 1;

// v2/v3 quality layout modes
static constexpr u8 QLAYOUT_PER_RECORD    = 0;
static constexpr u8 QLAYOUT_COLUMNAR      = 1;  // v2 layout=1: single quality.bin, column-major
static constexpr u8 QLAYOUT_PER_POSITION  = 2;  // layout=2: per-position files + delta encoding
static constexpr u8 QLAYOUT_PER_POS_RAW   = 3;  // layout=3: per-position files, raw (no delta)

// Max sequence length for columnar/per-position quality (limits file count)
static constexpr u32 MAX_COLUMNAR_SEQ_LEN = 1000;

// Header encoding modes (v3)
static constexpr u8 HDRMODE_LCP      = 0;  // LCP prefix stripping (v2 compat)
static constexpr u8 HDRMODE_ILLUMINA = 1;  // Illumina structured parsing

// Meta encoding modes (v3, packed into upper nibble of byte [15])
static constexpr u8 META_FULL    = 0;  // 48 bytes/record (default)
static constexpr u8 META_COMPACT = 1;  // template + u32 header_lens only

// Wrapping modes (v3, stored in meta global header after Illumina/LCP block)
// When wrapping is constant, seq_wrap.bin and qual_wrap.bin are empty —
// the wrapping pattern is stored once in the global header.
static constexpr u8 WRAPMODE_PER_RECORD = 0;  // default, wrapping in per-record files
static constexpr u8 WRAPMODE_CONSTANT   = 1;  // all records share identical wrapping

// ============================================================================
// Metadata struct (same layout for v1, v2, v3 per-record data)
// ============================================================================

#pragma pack(push, 1)
struct FastqRecordMeta {
    u32 seq_len;
    u32 header_len;        // v2: suffix length; v3 Illumina: compact binary length
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
    u8  quality_mode;      // v2/v3: always QUAL_RAW
    u8  pad[2];
};
#pragma pack(pop)

static_assert(sizeof(FastqRecordMeta) == 48, "FastqRecordMeta must be 48 bytes");

// ============================================================================
// Packed binary format: NQF1 / NQF2 / NQF3
// ============================================================================

static constexpr char NQF1_MAGIC[4] = {'N', 'Q', 'F', '1'};  // Illumina fixed-length
static constexpr char NQF2_MAGIC[4] = {'N', 'Q', 'F', '2'};  // Illumina variable-length
static constexpr char NQF3_MAGIC[4] = {'N', 'Q', 'F', '3'};  // Generic (LCP)

static constexpr u32 NQF_VERSION = 1;
static constexpr u32 NQF_VERSION_COMPACT = 2;  // Compact per-record metadata

#pragma pack(push, 1)
struct FastqPackedHeader {
    char magic[4];          // "NQF1", "NQF2", or "NQF3"
    u32  version;           // 1
    u32  num_records;
    u8   newline_style;     // 0=LF, 1=CRLF
    u8   has_trailing_nl;   // 0 or 1
    u8   stream_flags;      // SFLAG_NO_N | SFLAG_NO_IUPAC | SFLAG_NO_CASE
    u8   wrap_mode;         // 0=per-record, 1=constant
    u32  fixed_seq_len;     // >0 for NQF1, 0 for NQF2/NQF3
    u32  info_block_size;   // size of info block that follows
    u32  total_hdr;
    u32  total_plus;
    u32  total_nmask;
    u32  total_acgt;
    u32  total_bases;
    u32  total_exc;
    u32  total_case;
    u32  total_seq_wr;
    u32  total_qual_wr;
    u32  total_quality;
    u8   _reserved[16];
};
#pragma pack(pop)

static_assert(sizeof(FastqPackedHeader) == 80, "FastqPackedHeader must be 80 bytes");

#pragma pack(push, 1)
struct FastqPackedRecordMeta {
    u32 seq_len;
    u32 header_len;
    u32 plus_len;
    u32 nmask_bytes;
    u32 acgt_bytes;
    u32 bases_bytes;
    u32 exc_bytes;
    u32 case_bytes;
    u32 seq_wr_bytes;
    u32 qual_wr_bytes;
    u8  case_mode;
    u8  pad[3];
};
#pragma pack(pop)

static_assert(sizeof(FastqPackedRecordMeta) == 44, "FastqPackedRecordMeta must be 44 bytes");

// Compact per-record metadata for NQF1 v2 (8 bytes vs 44)
// Only stores fields that actually vary; constant/zero fields inferred from header.
#pragma pack(push, 1)
struct FastqCompactMeta1 {
    u16 header_len;
    u16 plus_len;
    u16 nmask_bytes;
    u16 bases_bytes;
};
#pragma pack(pop)

static_assert(sizeof(FastqCompactMeta1) == 8, "FastqCompactMeta1 must be 8 bytes");

// Stream-presence flags for packed format (reuse FASTA constants from codec_common.h)
static constexpr u8 SFLAG_NO_N     = 0x01;
static constexpr u8 SFLAG_NO_IUPAC = 0x02;
static constexpr u8 SFLAG_NO_CASE  = 0x04;

// ============================================================================
// Parsed FASTQ record
// ============================================================================

struct FastqRecord {
    std::string header;    // v3: without leading '@'
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
// Illumina header parsing (v3)
// ============================================================================

struct ParsedIlluminaHeader {
    std::string prefix;      // before .READNUM
    int64_t     read_num;    // the numeric part after prefix.
    std::string instrument;
    std::string run;
    std::string flowcell;
    std::string lane;
    std::string tile;
    std::string x;
    std::string y;
    std::string pair_suffix; // e.g. "/1", "/2" — empty if none
    bool        parsed;
};

// Parse Illumina header format (without leading '@'):
//   PREFIX.READNUM INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y
static ParsedIlluminaHeader parse_illumina_header(const std::string& hdr) {
    ParsedIlluminaHeader h{};
    h.parsed = false;
    h.read_num = 0;
    if (hdr.size() < 3) return h;

    // Find space separating PREFIX.READNUM from INSTRUMENT:...
    size_t sp = hdr.find(' ');
    if (sp == std::string::npos || sp < 2) return h;

    // Find dot separating PREFIX from READNUM (search backward from space)
    size_t dot = std::string::npos;
    for (size_t i = sp; i > 0; i--) {
        if (hdr[i - 1] == '.') { dot = i - 1; break; }
    }
    if (dot == 0 || dot == std::string::npos) return h;

    h.prefix = hdr.substr(0, dot);

    // Parse read number
    for (size_t i = dot + 1; i < sp; i++) {
        if (hdr[i] < '0' || hdr[i] > '9') return h;
        h.read_num = h.read_num * 10 + (hdr[i] - '0');
    }

    // Parse colon-separated fields: INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y
    const char* info = hdr.c_str() + sp + 1;
    size_t info_len = hdr.size() - sp - 1;

    size_t colons[6];
    int ncol = 0;
    for (size_t i = 0; i < info_len && ncol < 6; i++) {
        if (info[i] == ':') colons[ncol++] = i;
    }
    if (ncol < 6) return h;

    h.instrument = std::string(info, colons[0]);
    h.run        = std::string(info + colons[0] + 1, colons[1] - colons[0] - 1);
    h.flowcell   = std::string(info + colons[1] + 1, colons[2] - colons[1] - 1);
    h.lane       = std::string(info + colons[2] + 1, colons[3] - colons[2] - 1);
    h.tile       = std::string(info + colons[3] + 1, colons[4] - colons[3] - 1);
    h.x          = std::string(info + colons[4] + 1, colons[5] - colons[4] - 1);
    h.y          = std::string(info + colons[5] + 1, info_len - colons[5] - 1);

    // Strip paired-end suffix (/1, /2) from Y if present
    size_t slash_pos = h.y.find('/');
    if (slash_pos != std::string::npos) {
        h.pair_suffix = h.y.substr(slash_pos);
        h.y = h.y.substr(0, slash_pos);
    }

    h.parsed = true;
    return h;
}

// ============================================================================
// Encode context (shared across all records in an encode run)
// ============================================================================

struct EncodeContext {
    u8 header_mode;  // HDRMODE_LCP or HDRMODE_ILLUMINA
    u8 wrap_mode;    // WRAPMODE_PER_RECORD or WRAPMODE_CONSTANT
    std::string lcp_prefix;  // for LCP mode (already @-stripped)
    // For Illumina mode:
    bool instrument_constant;  // true: instrument stored once; false: per-record dict index
    std::unordered_map<std::string, u8> instrument_map;  // only used when !instrument_constant
    std::unordered_map<std::string, u8> run_map;
    std::unordered_map<std::string, u8> fc_map;
    std::unordered_map<std::string, u8> lane_map;
    std::unordered_map<std::string, u8> tile_map;
};

// ============================================================================
// Illumina meta block: build for writing to meta.bin
// ============================================================================

struct IlluminaMeta {
    u8 flags;  // bit 0: read_num_sequential, bit 1: prefix_constant, bit 2: instrument_constant
    std::string constant_prefix;
    std::string constant_instrument;  // non-empty only when flags & 0x04
    std::vector<std::string> instrument_dict;  // non-empty only when !(flags & 0x04)
    std::vector<std::string> run_dict;
    std::vector<std::string> fc_dict;
    std::vector<std::string> lane_dict;
    std::vector<std::string> tile_dict;
};

static std::vector<u8> build_illumina_block(const IlluminaMeta& im) {
    std::vector<u8> block;

    block.push_back(im.flags);

    auto write_str = [&](const std::string& s) {
        u16 len = static_cast<u16>(s.size());
        block.push_back(len & 0xFF);
        block.push_back((len >> 8) & 0xFF);
        block.insert(block.end(), s.begin(), s.end());
    };

    write_str(im.constant_prefix);
    write_str(im.constant_instrument);

    auto write_dict = [&](const std::vector<std::string>& dict) {
        u16 count = static_cast<u16>(dict.size());
        block.push_back(count & 0xFF);
        block.push_back((count >> 8) & 0xFF);
        for (const auto& s : dict) write_str(s);
    };

    write_dict(im.instrument_dict);  // empty if instrument_constant
    write_dict(im.run_dict);
    write_dict(im.fc_dict);
    write_dict(im.lane_dict);
    write_dict(im.tile_dict);

    return block;
}

static IlluminaMeta read_illumina_block(FILE* fm) {
    IlluminaMeta im{};
    im.flags = read_u8(fm);

    auto read_str = [&]() -> std::string {
        u8 buf[2];
        read_bytes(fm, buf, 2);
        u16 len = buf[0] | (buf[1] << 8);
        std::string s(len, '\0');
        if (len > 0) read_bytes(fm, &s[0], len);
        return s;
    };

    im.constant_prefix = read_str();
    im.constant_instrument = read_str();

    auto read_dict = [&]() -> std::vector<std::string> {
        u8 buf[2];
        read_bytes(fm, buf, 2);
        u16 count = buf[0] | (buf[1] << 8);
        std::vector<std::string> dict(count);
        for (u16 i = 0; i < count; i++) dict[i] = read_str();
        return dict;
    };

    im.instrument_dict = read_dict();
    im.run_dict = read_dict();
    im.fc_dict = read_dict();
    im.lane_dict = read_dict();
    im.tile_dict = read_dict();

    return im;
}

// Read illumina block from raw meta bytes (for validate)
static IlluminaMeta read_illumina_block_from_bytes(const u8* data, size_t len) {
    IlluminaMeta im{};
    size_t off = 0;
    if (off >= len) return im;
    im.flags = data[off++];

    auto read_str = [&]() -> std::string {
        if (off + 2 > len) return "";
        u16 slen = data[off] | (data[off + 1] << 8);
        off += 2;
        if (off + slen > len) return "";
        std::string s((const char*)data + off, slen);
        off += slen;
        return s;
    };

    im.constant_prefix = read_str();
    im.constant_instrument = read_str();

    auto read_dict = [&]() -> std::vector<std::string> {
        if (off + 2 > len) return {};
        u16 count = data[off] | (data[off + 1] << 8);
        off += 2;
        std::vector<std::string> dict(count);
        for (u16 i = 0; i < count; i++) dict[i] = read_str();
        return dict;
    };

    im.instrument_dict = read_dict();
    im.run_dict = read_dict();
    im.fc_dict = read_dict();
    im.lane_dict = read_dict();
    im.tile_dict = read_dict();

    return im;
}

// ============================================================================
// Parse a batch of FASTQ records from memory-mapped data.
// v3: '@' is stripped from headers.
// ============================================================================

static constexpr u32 BATCH_SIZE = 500000;

static u32 parse_batch(const char* data, size_t size, size_t& pos,
                        std::vector<FastqRecord>& records, u32 max_records) {
    records.clear();
    u32 count = 0;

    while (pos < size && count < max_records) {
        if (data[pos] != '@') { pos++; continue; }

        FastqRecord rec;

        // Header line (skip leading '@')
        size_t hdr_start = pos + 1;
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
// v3: uses EncodeContext for header mode
// ============================================================================

static EncodedRecord encode_one(const FastqRecord& rec, const EncodeContext& ctx) {
    EncodedRecord er{};
    const std::string& seq = rec.raw_seq;
    const std::string& qual = rec.raw_qual;
    u32 L = static_cast<u32>(seq.size());

    er.meta.seq_len = L;
    er.meta.plus_len = static_cast<u32>(rec.plus_comment.size());

    // Header encoding
    if (ctx.header_mode == HDRMODE_ILLUMINA) {
        // Parse and encode as compact binary
        auto illum = parse_illumina_header(rec.header);
        // [instr_idx(u8, if !instrument_constant)] + run_idx(u8) + fc_idx(u8) + lane_idx(u8) + tile_idx(u8)
        // + varint(x_int) + varint(y_int)
        if (!ctx.instrument_constant) {
            er.header_buf.push_back(ctx.instrument_map.at(illum.instrument));
        }
        er.header_buf.push_back(ctx.run_map.at(illum.run));
        er.header_buf.push_back(ctx.fc_map.at(illum.flowcell));
        er.header_buf.push_back(ctx.lane_map.at(illum.lane));
        er.header_buf.push_back(ctx.tile_map.at(illum.tile));
        // X and Y as varint integers (saves ~8 bytes/record vs u16_len + ascii)
        u64 x_int = 0, y_int = 0;
        for (char c : illum.x) x_int = x_int * 10 + (c - '0');
        for (char c : illum.y) y_int = y_int * 10 + (c - '0');
        encode_varint(er.header_buf, x_int);
        encode_varint(er.header_buf, y_int);
    } else {
        // LCP mode: store suffix (@ already stripped by parser)
        const std::string& prefix = ctx.lcp_prefix;
        if (!prefix.empty() && rec.header.size() >= prefix.size() &&
            rec.header.compare(0, prefix.size(), prefix) == 0) {
            er.header_buf.assign(rec.header.begin() + prefix.size(), rec.header.end());
        } else {
            er.header_buf.assign(rec.header.begin(), rec.header.end());
        }
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

    // Sequence wrapping (skip if constant — stored once in global header)
    if (ctx.wrap_mode == WRAPMODE_CONSTANT) {
        er.meta.seq_wrap_bytes = 0;
        er.meta.qual_wrap_bytes = 0;
    } else {
        er.seq_wrap_buf = encode_wrapping(rec.seq_line_lengths, L);
        er.meta.seq_wrap_bytes = static_cast<u32>(er.seq_wrap_buf.size());
        er.qual_wrap_buf = encode_wrapping(rec.qual_line_lengths, L);
        er.meta.qual_wrap_bytes = static_cast<u32>(er.qual_wrap_buf.size());
    }

    // Quality: always raw in v2/v3 (no delta)
    er.meta.quality_mode = QUAL_RAW;
    er.quality_buf.resize(L);
    for (u32 i = 0; i < L; i++) {
        er.quality_buf[i] = static_cast<u8>(qual[i]);
    }
    er.meta.quality_bytes = L;

    return er;
}

// ============================================================================
// ENCODE — v3 with Illumina header parsing, @ stripping, work-stealing
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

    // Parse first batch to determine header mode, prefix, and uniform length
    size_t parse_pos = 0;
    std::vector<FastqRecord> records;
    u32 first_batch_count = parse_batch(mf.data, mf.size, parse_pos, records, BATCH_SIZE);
    if (first_batch_count == 0) {
        fprintf(stderr, "No FASTQ records found\n");
        return 1;
    }

    // --- Illumina header analysis (full pre-scan of all batches) ---
    EncodeContext ctx{};
    u8 header_mode = HDRMODE_LCP;
    IlluminaMeta illumina_meta{};

    bool all_illumina = true;
    std::string constant_prefix, constant_instrument;
    bool prefix_constant = true, instrument_constant = true, read_num_sequential = true;
    std::unordered_set<std::string> instrument_vals, run_vals, fc_vals, lane_vals, tile_vals;

    // Analyze first batch
    for (size_t i = 0; i < records.size(); i++) {
        auto illum = parse_illumina_header(records[i].header);
        if (!illum.parsed) { all_illumina = false; break; }

        if (i == 0) {
            constant_prefix = illum.prefix;
            constant_instrument = illum.instrument;
        } else {
            if (illum.prefix != constant_prefix) prefix_constant = false;
            if (illum.instrument != constant_instrument) instrument_constant = false;
        }
        if ((int64_t)(i + 1) != illum.read_num) read_num_sequential = false;

        instrument_vals.insert(illum.instrument);
        run_vals.insert(illum.run);
        fc_vals.insert(illum.flowcell);
        lane_vals.insert(illum.lane);
        tile_vals.insert(illum.tile);
    }

    // Pre-scan remaining batches for complete dictionary building
    // We need ALL unique values before we can build dictionaries
    size_t prescan_pos = parse_pos;  // save position after first batch
    int64_t prescan_read_num = static_cast<int64_t>(records.size()) + 1;
    u32 prescan_batches = 0;

    if (all_illumina) {
        std::vector<FastqRecord> prescan_records;
        while (true) {
            u32 bc = parse_batch(mf.data, mf.size, prescan_pos, prescan_records, BATCH_SIZE);
            if (bc == 0) break;
            prescan_batches++;

            for (size_t i = 0; i < prescan_records.size(); i++) {
                auto illum = parse_illumina_header(prescan_records[i].header);
                if (!illum.parsed) { all_illumina = false; break; }
                if (illum.prefix != constant_prefix) prefix_constant = false;
                if (illum.instrument != constant_instrument) instrument_constant = false;
                if (illum.read_num != prescan_read_num) read_num_sequential = false;
                prescan_read_num++;

                instrument_vals.insert(illum.instrument);
                run_vals.insert(illum.run);
                fc_vals.insert(illum.flowcell);
                lane_vals.insert(illum.lane);
                tile_vals.insert(illum.tile);
            }
            if (!all_illumina) break;
        }
        if (prescan_batches > 0) {
            fprintf(stderr, "v3 encoder: pre-scanned %u additional batches for dictionary building\n",
                    prescan_batches);
        }
    }

    if (all_illumina && prefix_constant && read_num_sequential &&
        instrument_vals.size() <= 255 &&
        run_vals.size() <= 255 && fc_vals.size() <= 255 &&
        lane_vals.size() <= 255 && tile_vals.size() <= 255) {

        header_mode = HDRMODE_ILLUMINA;

        // Build sorted dictionaries and index maps
        auto build_dict = [](const std::unordered_set<std::string>& vals)
            -> std::pair<std::vector<std::string>, std::unordered_map<std::string, u8>> {
            std::vector<std::string> sv(vals.begin(), vals.end());
            std::sort(sv.begin(), sv.end());
            std::unordered_map<std::string, u8> m;
            for (size_t i = 0; i < sv.size(); i++) m[sv[i]] = static_cast<u8>(i);
            return {sv, m};
        };

        auto [run_dict, run_map]   = build_dict(run_vals);
        auto [fc_dict, fc_map]     = build_dict(fc_vals);
        auto [lane_dict, lane_map] = build_dict(lane_vals);
        auto [tile_dict, tile_map] = build_dict(tile_vals);

        ctx.header_mode = HDRMODE_ILLUMINA;
        ctx.instrument_constant = instrument_constant;
        ctx.run_map  = std::move(run_map);
        ctx.fc_map   = std::move(fc_map);
        ctx.lane_map = std::move(lane_map);
        ctx.tile_map = std::move(tile_map);

        // flags: bit 0 = read_num_sequential, bit 1 = prefix_constant, bit 2 = instrument_constant
        illumina_meta.flags = 0x01 | 0x02;  // read_num_seq + prefix_const always set here
        if (instrument_constant) {
            illumina_meta.flags |= 0x04;
            illumina_meta.constant_instrument = constant_instrument;
            // instrument_dict stays empty
        } else {
            // Dictionary-encode instrument
            auto [instr_dict, instr_map] = build_dict(instrument_vals);
            ctx.instrument_map = std::move(instr_map);
            illumina_meta.instrument_dict = std::move(instr_dict);
            // constant_instrument stays empty
        }
        illumina_meta.constant_prefix = constant_prefix;
        illumina_meta.run_dict  = std::move(run_dict);
        illumina_meta.fc_dict   = std::move(fc_dict);
        illumina_meta.lane_dict = std::move(lane_dict);
        illumina_meta.tile_dict = std::move(tile_dict);

        fprintf(stderr, "v3 encoder: Illumina mode (prefix=%s, instrument=%s%s, "
                "dicts: instr=%zu run=%zu fc=%zu lane=%zu tile=%zu)\n",
                constant_prefix.c_str(),
                instrument_constant ? constant_instrument.c_str() : "dict",
                instrument_constant ? " [const]" : "",
                illumina_meta.instrument_dict.size(),
                illumina_meta.run_dict.size(), illumina_meta.fc_dict.size(),
                illumina_meta.lane_dict.size(), illumina_meta.tile_dict.size());
    } else {
        header_mode = HDRMODE_LCP;
        ctx.header_mode = HDRMODE_LCP;
        ctx.lcp_prefix = compute_lcp(records);

        if (all_illumina) {
            fprintf(stderr, "v3 encoder: Illumina detected but constraints not met "
                    "(prefix_const=%d, instr_const=%d, seq_readnum=%d, "
                    "instr=%zu, run=%zu, fc=%zu, lane=%zu, tile=%zu), falling back to LCP\n",
                    prefix_constant, instrument_constant, read_num_sequential,
                    instrument_vals.size(), run_vals.size(), fc_vals.size(),
                    lane_vals.size(), tile_vals.size());
        }
    }

    u32 prefix_len = static_cast<u32>(ctx.lcp_prefix.size());

    // Detect uniform sequence length for per-position quality
    u32 fixed_seq_len = check_uniform_seq_len(records);
    u8 quality_layout = (fixed_seq_len > 0 && fixed_seq_len <= MAX_COLUMNAR_SEQ_LEN)
                        ? QLAYOUT_PER_POS_RAW : QLAYOUT_PER_RECORD;

    // Detect constant wrapping: if all records in the first batch have identical
    // seq and qual wrapping, store once in global header and skip per-record files.
    u8 wrap_mode = WRAPMODE_PER_RECORD;
    std::vector<u8> constant_seq_wrap, constant_qual_wrap;
    if (!records.empty()) {
        auto first_sw = encode_wrapping(records[0].seq_line_lengths,
                                        static_cast<u32>(records[0].raw_seq.size()));
        auto first_qw = encode_wrapping(records[0].qual_line_lengths,
                                        static_cast<u32>(records[0].raw_seq.size()));
        bool all_same = true;
        for (size_t i = 1; i < records.size() && all_same; i++) {
            auto sw = encode_wrapping(records[i].seq_line_lengths,
                                      static_cast<u32>(records[i].raw_seq.size()));
            auto qw = encode_wrapping(records[i].qual_line_lengths,
                                      static_cast<u32>(records[i].raw_seq.size()));
            if (sw != first_sw || qw != first_qw) all_same = false;
        }
        if (all_same) {
            wrap_mode = WRAPMODE_CONSTANT;
            constant_seq_wrap = std::move(first_sw);
            constant_qual_wrap = std::move(first_qw);
        }
    }
    ctx.wrap_mode = wrap_mode;

    fprintf(stderr, "v3 encoder: header_mode=%s, quality_layout=%s, fixed_seq_len=%u, wrap=%s\n",
            header_mode == HDRMODE_ILLUMINA ? "illumina" : "lcp",
            quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
            quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
            quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record",
            fixed_seq_len,
            wrap_mode == WRAPMODE_CONSTANT ? "constant" : "per-record");

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

    // Write v3 meta global header
    // Byte [15] packs: (meta_mode << 4) | header_mode
    // meta_mode is determined after encoding; written as 0 initially, patched at end
    write_u32(f_meta, META_MAGIC);           // [0-3]
    write_u32(f_meta, META_VERSION_V3);      // [4-7]
    write_u32(f_meta, 0);                    // [8-11]  num_records placeholder
    write_u8(f_meta, newline_style);         // [12]
    write_u8(f_meta, has_trailing_nl);       // [13]
    write_u8(f_meta, quality_layout);        // [14]
    write_u8(f_meta, header_mode);           // [15]  placeholder, patched at end
    write_u32(f_meta, fixed_seq_len);        // [16-19]

    if (header_mode == HDRMODE_ILLUMINA) {
        auto illumina_block = build_illumina_block(illumina_meta);
        u32 block_size = static_cast<u32>(illumina_block.size());
        write_u32(f_meta, block_size);       // [20-23]
        write_bytes(f_meta, illumina_block.data(), illumina_block.size());
    } else {
        write_u32(f_meta, prefix_len);       // [20-23]
        if (prefix_len > 0) {
            write_bytes(f_meta, ctx.lcp_prefix.data(), prefix_len);
        }
    }

    // Write wrap_mode and constant wrapping data (v3)
    write_u8(f_meta, wrap_mode);
    if (wrap_mode == WRAPMODE_CONSTANT) {
        u32 sw_len = static_cast<u32>(constant_seq_wrap.size());
        u32 qw_len = static_cast<u32>(constant_qual_wrap.size());
        write_u32(f_meta, sw_len);
        write_bytes(f_meta, constant_seq_wrap.data(), sw_len);
        write_u32(f_meta, qw_len);
        write_bytes(f_meta, constant_qual_wrap.data(), qw_len);
        fprintf(stderr, "v3 encoder: constant wrapping (%u + %u bytes stored once)\n",
                sw_len, qw_len);
    }

    // Process batches (first batch already parsed)
    u32 total_records = 0;
    u32 batch_num = 0;
    bool first_batch = true;
    int64_t expected_read_num = 1;  // for Illumina read number verification

    // Compact meta tracking: if all records have identical meta except header_len,
    // we can store one template + per-record header_lens (4 bytes vs 48 bytes/record)
    bool compact_possible = true;
    bool compact_template_set = false;
    FastqRecordMeta compact_template{};
    std::vector<u32> header_lens;
    header_lens.reserve(1000000);
    long per_record_meta_offset = 0;  // file offset where per-record metas begin

    // Delta encoding state for per-position quality (persists across batches)
    std::vector<u8> prev_byte;
    if (quality_layout == QLAYOUT_PER_POSITION) {
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

        // Verify constant wrapping in subsequent batches
        if (wrap_mode == WRAPMODE_CONSTANT && batch_num > 0) {
            for (u32 i = 0; i < batch_count; i++) {
                auto sw = encode_wrapping(records[i].seq_line_lengths,
                                          static_cast<u32>(records[i].raw_seq.size()));
                auto qw = encode_wrapping(records[i].qual_line_lengths,
                                          static_cast<u32>(records[i].raw_seq.size()));
                if (sw != constant_seq_wrap || qw != constant_qual_wrap) {
                    fprintf(stderr, "Error: record %u has different wrapping than "
                            "first batch. Cannot use constant wrapping mode.\n",
                            total_records + i);
                    return 1;
                }
            }
        }

        // Verify Illumina read numbers in subsequent batches
        if (header_mode == HDRMODE_ILLUMINA) {
            for (u32 i = 0; i < batch_count; i++) {
                auto illum = parse_illumina_header(records[i].header);
                if (!illum.parsed) {
                    fprintf(stderr, "Error: record %u: Illumina header parse failed "
                            "in batch %u\n", total_records + i, batch_num + 1);
                    return 1;
                }
                if (illum.read_num != expected_read_num) {
                    fprintf(stderr, "Error: record %u: read_num %lld != expected %lld\n",
                            total_records + i,
                            (long long)illum.read_num, (long long)expected_read_num);
                    return 1;
                }
                expected_read_num++;
            }
        }

        // Encode batch (parallel with work-stealing)
        std::vector<EncodedRecord> encoded(batch_count);
        if (num_threads <= 1 || batch_count <= 1) {
            for (u32 i = 0; i < batch_count; i++) {
                encoded[i] = encode_one(records[i], ctx);
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
                        encoded[i] = encode_one(records[i], ctx);
                    }
                });
            }
            for (auto& th : threads) th.join();
        }

        // Capture file offset for per-record metas (first batch only)
        if (batch_num == 0 && first_batch) {
            // Actually set after we exit the first-batch flag below
        }
        if (total_records == 0 && per_record_meta_offset == 0) {
            per_record_meta_offset = ftell(f_meta);
        }

        // Write batch to streams, track compact meta
        for (u32 i = 0; i < batch_count; i++) {
            const auto& er = encoded[i];

            // Track compact meta eligibility
            header_lens.push_back(er.meta.header_len);
            if (compact_possible) {
                if (!compact_template_set) {
                    compact_template = er.meta;
                    compact_template.header_len = 0;
                    compact_template_set = true;
                } else {
                    FastqRecordMeta check = er.meta;
                    check.header_len = 0;
                    if (memcmp(&check, &compact_template, sizeof(FastqRecordMeta)) != 0) {
                        compact_possible = false;
                    }
                }
            }

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
        for (u32 p = 0; p < fixed_seq_len; p++) {
            fclose(qual_pos_files[p]);
        }
    } else {
        fclose(f_quality);
    }

    // Determine meta mode
    u8 meta_mode = META_FULL;
    if (compact_possible && total_records > 0 && compact_template_set) {
        meta_mode = META_COMPACT;
        // Rewrite per-record section as: template(48) + header_lens(4*N)
        fseek(f_meta, per_record_meta_offset, SEEK_SET);
        write_bytes(f_meta, &compact_template, sizeof(FastqRecordMeta));
        write_bytes(f_meta, header_lens.data(), header_lens.size() * sizeof(u32));
        // Truncate file
        long end_pos = ftell(f_meta);
        fflush(f_meta);
        if (ftruncate(fileno(f_meta), end_pos) != 0) {
            fprintf(stderr, "Warning: ftruncate failed for meta.bin\n");
        }
        fprintf(stderr, "v3 encoder: compact meta (template + %u header_lens, "
                "%.1f MB vs %.1f MB full)\n",
                total_records,
                (48.0 + total_records * 4.0) / (1024 * 1024),
                (total_records * 48.0) / (1024 * 1024));
    }

    // Seek back and write the actual record count and meta_mode
    fseek(f_meta, 8, SEEK_SET);
    write_u32(f_meta, total_records);
    // Patch byte [15] with (meta_mode << 4) | header_mode
    fseek(f_meta, 15, SEEK_SET);
    write_u8(f_meta, (meta_mode << 4) | header_mode);

    fclose(f_meta); fclose(f_headers); fclose(f_plus);
    fclose(f_nmask); fclose(f_acgtmask); fclose(f_bases2);
    fclose(f_exceptions); fclose(f_case); fclose(f_seq_wrap);
    fclose(f_qual_wrap);

    fprintf(stderr, "Encoded %u records in %u batch(es) (%d thread%s), "
            "newline=%s, trailing_nl=%d, header=%s, quality=%s, meta=%s\n",
            total_records, batch_num, num_threads,
            num_threads == 1 ? "" : "s",
            newline_style == NL_CRLF ? "CRLF" : "LF", has_trailing_nl,
            header_mode == HDRMODE_ILLUMINA ? "illumina" : "lcp",
            quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
            quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
            quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record",
            meta_mode == META_COMPACT ? "compact" : "full");
    return 0;
}

// ============================================================================
// DECODE — streaming, supports v1, v2, and v3 meta formats
//
// Memory strategy:
//   - Stream files (headers.bin, etc.) are memory-mapped with MADV_SEQUENTIAL.
//     The OS pages in data as needed and releases pages behind the cursor,
//     keeping physical RSS low (~100-200 MB) regardless of file size.
//   - Per-record metas are read from meta.bin in batches (not all at once).
//   - Per-position quality files are read in batches and transposed on-the-fly,
//     avoiding the full NxL quality matrix allocation.
// ============================================================================

static constexpr u32 DECODE_BATCH = 500000;

static int do_decode(const char* streams_dir, const char* output_path) {
    std::string dir(streams_dir);

    // ---- Parse meta.bin global header ----
    std::string meta_path = dir + "/meta.bin";
    FILE* fm = fopen(meta_path.c_str(), "rb");
    if (!fm) { fprintf(stderr, "Cannot open %s\n", meta_path.c_str()); return 1; }

    u32 magic = read_u32(fm);
    if (magic != META_MAGIC) {
        fprintf(stderr, "Bad magic: 0x%08X\n", magic); fclose(fm); return 1;
    }
    u32 version = read_u32(fm);
    if (version != META_VERSION_V1 && version != META_VERSION_V2 && version != META_VERSION_V3) {
        fprintf(stderr, "Unsupported version: %u\n", version); fclose(fm); return 1;
    }

    u32 num_records = read_u32(fm);
    u8 newline_style = read_u8(fm);
    u8 has_trailing_nl = read_u8(fm);

    // v2/v3 extended header fields
    u8 quality_layout = QLAYOUT_PER_RECORD;
    u8 header_mode = HDRMODE_LCP;
    u8 meta_mode = META_FULL;
    u32 fixed_seq_len = 0;
    std::string prefix;
    IlluminaMeta illumina_meta{};

    if (version >= META_VERSION_V2) {
        quality_layout = read_u8(fm);        // [14]
        u8 mode_byte = read_u8(fm);          // [15]
        fixed_seq_len = read_u32(fm);        // [16-19]

        // v3: byte [15] packs (meta_mode << 4) | header_mode
        header_mode = mode_byte & 0x0F;
        meta_mode = (mode_byte >> 4) & 0x0F;

        if (version >= META_VERSION_V3 && header_mode == HDRMODE_ILLUMINA) {
            u32 illumina_block_size = read_u32(fm);  // [20-23]
            (void)illumina_block_size;
            illumina_meta = read_illumina_block(fm);
        } else {
            header_mode = HDRMODE_LCP;
            u32 prefix_len = read_u32(fm);   // [20-23]
            if (prefix_len > 0) {
                prefix.resize(prefix_len);
                read_bytes(fm, &prefix[0], prefix_len);
            }
        }
    } else {
        // v1: skip 6 reserved bytes
        u8 skip_reserved[6];
        read_bytes(fm, skip_reserved, 6);
    }

    // ---- Read wrap_mode and constant wrapping data (v3) ----
    u8 wrap_mode = WRAPMODE_PER_RECORD;
    std::vector<u8> constant_seq_wrap, constant_qual_wrap;
    if (version >= META_VERSION_V3) {
        wrap_mode = read_u8(fm);
        if (wrap_mode == WRAPMODE_CONSTANT) {
            u32 sw_len = read_u32(fm);
            constant_seq_wrap.resize(sw_len);
            read_bytes(fm, constant_seq_wrap.data(), sw_len);
            u32 qw_len = read_u32(fm);
            constant_qual_wrap.resize(qw_len);
            read_bytes(fm, constant_qual_wrap.data(), qw_len);
            fprintf(stderr, "Decoder: constant wrapping (%u + %u bytes)\n", sw_len, qw_len);
        }
    }

    // ---- Compact meta template (read once, reuse per batch) ----
    FastqRecordMeta compact_template{};
    if (meta_mode == META_COMPACT && num_records > 0) {
        read_bytes(fm, &compact_template, sizeof(FastqRecordMeta));
        fprintf(stderr, "Decoder: compact meta (template + %u header_lens)\n", num_records);
    }
    // fm is now positioned at per-record data; keep open for batch reading

    // ---- Memory-map stream files (OS manages paging) ----
    MappedFile headers_mf, plus_mf, nmask_mf, acgtmask_mf, bases2_mf;
    MappedFile exceptions_mf, case_mf, seq_wrap_mf, qual_wrap_mf;

    if (!headers_mf.open((dir + "/headers.bin").c_str())) return 1;
    if (!plus_mf.open((dir + "/plus.bin").c_str())) return 1;
    if (!nmask_mf.open((dir + "/nmask.bin").c_str())) return 1;
    if (!acgtmask_mf.open((dir + "/acgtmask.bin").c_str())) return 1;
    if (!bases2_mf.open((dir + "/bases2.bin").c_str())) return 1;
    if (!exceptions_mf.open((dir + "/exceptions.bin").c_str())) return 1;
    if (!case_mf.open((dir + "/case.bin").c_str())) return 1;
    // Only mmap wrapping files if not constant mode
    if (wrap_mode != WRAPMODE_CONSTANT) {
        if (!seq_wrap_mf.open((dir + "/seq_wrap.bin").c_str())) return 1;
        if (!qual_wrap_mf.open((dir + "/qual_wrap.bin").c_str())) return 1;
    }

    // ---- Quality: open per-position file handles OR mmap quality.bin ----
    bool use_per_position = false;
    bool use_delta = false;
    std::vector<FILE*> qual_pos_fps;
    MappedFile quality_mf;

    if (version >= META_VERSION_V2 &&
        (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) &&
        fixed_seq_len > 0 && num_records > 0) {
        use_delta = (quality_layout == QLAYOUT_PER_POSITION);
        // Probe for per-position files
        char fname[64];
        snprintf(fname, sizeof(fname), "quality_pos_0000.bin");
        FILE* probe = fopen((dir + "/" + fname).c_str(), "rb");
        if (probe) {
            fclose(probe);
            use_per_position = true;
            qual_pos_fps.resize(fixed_seq_len, nullptr);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                snprintf(fname, sizeof(fname), "quality_pos_%04u.bin", p);
                qual_pos_fps[p] = fopen((dir + "/" + fname).c_str(), "rb");
                if (!qual_pos_fps[p]) {
                    fprintf(stderr, "Cannot open %s\n", fname);
                    // Cleanup
                    for (auto fp : qual_pos_fps) if (fp) fclose(fp);
                    fclose(fm);
                    return 1;
                }
            }
        } else {
            // Fall back to quality.bin (columnar in single file)
            if (!quality_mf.open((dir + "/quality.bin").c_str())) {
                fclose(fm); return 1;
            }
            fprintf(stderr, "Note: per-position files not found, falling back to quality.bin\n");
        }
    } else {
        if (!quality_mf.open((dir + "/quality.bin").c_str())) {
            fclose(fm); return 1;
        }
    }

    // ---- Open output ----
    FILE* out = fopen(output_path, "wb");
    if (!out) {
        fprintf(stderr, "Cannot create %s\n", output_path);
        for (auto fp : qual_pos_fps) if (fp) fclose(fp);
        fclose(fm);
        return 1;
    }
    // Use 4 MB output buffer for fewer syscalls
    std::vector<char> out_buf(4 * 1024 * 1024);
    setvbuf(out, out_buf.data(), _IOFBF, out_buf.size());

    const char* nl = (newline_style == NL_CRLF) ? "\r\n" : "\n";
    size_t nl_len = (newline_style == NL_CRLF) ? 2 : 1;

    // ---- Stream cursors (into mmapped data) ----
    size_t hdr_off = 0, plus_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, sw_off = 0, qw_off = 0, q_off = 0;

    // Delta state for per-position quality (persists across batches)
    std::vector<u8> delta_prevs;
    if (use_per_position && use_delta) {
        delta_prevs.assign(fixed_seq_len, 0);
    }

    // ---- Batch processing loop ----
    for (u32 batch_start = 0; batch_start < num_records; batch_start += DECODE_BATCH) {
        u32 batch_end = std::min(batch_start + DECODE_BATCH, num_records);
        u32 batch_count = batch_end - batch_start;

        // ---- Read batch metas from meta.bin ----
        std::vector<FastqRecordMeta> batch_metas(batch_count);
        if (meta_mode == META_COMPACT) {
            std::vector<u32> hlens(batch_count);
            read_bytes(fm, hlens.data(), batch_count * sizeof(u32));
            for (u32 i = 0; i < batch_count; i++) {
                batch_metas[i] = compact_template;
                batch_metas[i].header_len = hlens[i];
            }
        } else {
            read_bytes(fm, batch_metas.data(), batch_count * sizeof(FastqRecordMeta));
        }

        // ---- Read batch quality (per-position mode) ----
        std::vector<u8> batch_quality;
        if (use_per_position) {
            batch_quality.resize((u64)batch_count * fixed_seq_len);
            std::vector<u8> pos_buf(batch_count);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                size_t nread = fread(pos_buf.data(), 1, batch_count, qual_pos_fps[p]);
                if (nread != batch_count) {
                    fprintf(stderr, "Warning: quality_pos_%04u.bin short read "
                            "(%zu vs %u) at batch %u\n", p, nread, batch_count, batch_start);
                }
                if (use_delta) {
                    u8 prev = delta_prevs[p];
                    for (u32 r = 0; r < batch_count; r++) {
                        u8 val = static_cast<u8>((prev + pos_buf[r]) & 0xFF);
                        prev = val;
                        batch_quality[(u64)r * fixed_seq_len + p] = val;
                    }
                    delta_prevs[p] = prev;
                } else {
                    for (u32 r = 0; r < batch_count; r++) {
                        batch_quality[(u64)r * fixed_seq_len + p] = pos_buf[r];
                    }
                }
            }
        } else if (!use_per_position && quality_mf.data &&
                   version >= META_VERSION_V2 && quality_layout == QLAYOUT_COLUMNAR &&
                   fixed_seq_len > 0) {
            // Columnar quality in single file: transpose batch on-the-fly
            batch_quality.resize((u64)batch_count * fixed_seq_len);
            for (u32 p = 0; p < fixed_seq_len; p++) {
                u64 col_base = (u64)p * num_records + batch_start;
                for (u32 r = 0; r < batch_count; r++) {
                    batch_quality[(u64)r * fixed_seq_len + p] =
                        (u8)quality_mf.data[col_base + r];
                }
            }
        }
        // For QLAYOUT_PER_RECORD or quality.bin fallback from per-position:
        // access quality_mf directly via q_off cursor (no batch buffer needed)

        // ---- Decode records in this batch ----
        for (u32 bi = 0; bi < batch_count; bi++) {
            u32 r = batch_start + bi;
            const FastqRecordMeta& rm = batch_metas[bi];

            // Write header
            if (version >= META_VERSION_V3) {
                fwrite("@", 1, 1, out);

                if (header_mode == HDRMODE_ILLUMINA) {
                    const u8* hdr = (const u8*)headers_mf.data + hdr_off;
                    size_t off = 0;

                    std::string instrument_str;
                    bool instr_const = (illumina_meta.flags & 0x04) != 0;
                    if (instr_const) {
                        instrument_str = illumina_meta.constant_instrument;
                    } else {
                        u8 instr_idx = hdr[off++];
                        instrument_str = illumina_meta.instrument_dict[instr_idx];
                    }

                    u8 run_idx  = hdr[off++];
                    u8 fc_idx   = hdr[off++];
                    u8 lane_idx = hdr[off++];
                    u8 tile_idx = hdr[off++];
                    // X and Y as varint integers
                    const u8* vptr = hdr + off;
                    const u8* vend = hdr + rm.header_len;
                    u64 x_int = decode_varint(vptr, vend);
                    u64 y_int = decode_varint(vptr, vend);

                    fwrite(illumina_meta.constant_prefix.data(),
                           1, illumina_meta.constant_prefix.size(), out);
                    fprintf(out, ".%u ", r + 1);

                    fwrite(instrument_str.data(), 1, instrument_str.size(), out);
                    fwrite(":", 1, 1, out);
                    fwrite(illumina_meta.run_dict[run_idx].data(),
                           1, illumina_meta.run_dict[run_idx].size(), out);
                    fwrite(":", 1, 1, out);
                    fwrite(illumina_meta.fc_dict[fc_idx].data(),
                           1, illumina_meta.fc_dict[fc_idx].size(), out);
                    fwrite(":", 1, 1, out);
                    fwrite(illumina_meta.lane_dict[lane_idx].data(),
                           1, illumina_meta.lane_dict[lane_idx].size(), out);
                    fwrite(":", 1, 1, out);
                    fwrite(illumina_meta.tile_dict[tile_idx].data(),
                           1, illumina_meta.tile_dict[tile_idx].size(), out);
                    fwrite(":", 1, 1, out);
                    fprintf(out, "%llu", (unsigned long long)x_int);
                    fwrite(":", 1, 1, out);
                    fprintf(out, "%llu", (unsigned long long)y_int);
                } else {
                    if (!prefix.empty()) {
                        fwrite(prefix.data(), 1, prefix.size(), out);
                    }
                    fwrite(headers_mf.data + hdr_off, 1, rm.header_len, out);
                }
            } else {
                if (version == META_VERSION_V2 && !prefix.empty()) {
                    fwrite(prefix.data(), 1, prefix.size(), out);
                }
                fwrite(headers_mf.data + hdr_off, 1, rm.header_len, out);
            }
            fwrite(nl, 1, nl_len, out);
            hdr_off += rm.header_len;

            u32 L = rm.seq_len;

            // Decode sequence
            auto raw_seq = decode_sequence(
                L,
                (const u8*)nmask_mf.data + nm_off, rm.nmask_bytes,
                (const u8*)acgtmask_mf.data + am_off, rm.acgtmask_bytes,
                (const u8*)bases2_mf.data + b2_off, rm.bases2_bytes,
                (const u8*)exceptions_mf.data + ex_off, rm.exceptions_bytes,
                (const u8*)case_mf.data + cs_off, rm.case_bytes, rm.case_mode);

            nm_off += rm.nmask_bytes;
            am_off += rm.acgtmask_bytes;
            b2_off += rm.bases2_bytes;
            ex_off += rm.exceptions_bytes;
            cs_off += rm.case_bytes;

            // Write sequence lines
            std::vector<u32> seq_lines;
            if (wrap_mode == WRAPMODE_CONSTANT) {
                seq_lines = decode_wrapping(constant_seq_wrap.data(),
                                            static_cast<u32>(constant_seq_wrap.size()), L);
            } else {
                seq_lines = decode_wrapping(
                    (const u8*)seq_wrap_mf.data + sw_off, rm.seq_wrap_bytes, L);
                sw_off += rm.seq_wrap_bytes;
            }

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
                fwrite(plus_mf.data + plus_off, 1, rm.plus_len, out);
            }
            fwrite(nl, 1, nl_len, out);
            plus_off += rm.plus_len;

            // Decode quality
            std::vector<u8> qual_bytes(L);
            if (use_per_position || (batch_quality.size() > 0)) {
                // Per-position or columnar: use batch buffer
                memcpy(qual_bytes.data(),
                       batch_quality.data() + (u64)bi * fixed_seq_len, L);
            } else if (version >= META_VERSION_V2) {
                // Per-record quality from mmapped quality.bin
                memcpy(qual_bytes.data(), quality_mf.data + q_off, L);
            } else {
                if (rm.quality_mode == QUAL_RAW) {
                    memcpy(qual_bytes.data(), quality_mf.data + q_off, L);
                } else {
                    // QUAL_DELTA (v1 only)
                    const u8* qd = (const u8*)quality_mf.data + q_off;
                    if (L > 0) {
                        qual_bytes[0] = qd[0];
                        for (u32 i = 1; i < L; i++) {
                            qual_bytes[i] = static_cast<u8>(
                                (qual_bytes[i - 1] + qd[i]) & 0xFF);
                        }
                    }
                }
            }
            q_off += rm.quality_bytes;

            // Write quality lines
            std::vector<u32> qual_lines;
            if (wrap_mode == WRAPMODE_CONSTANT) {
                qual_lines = decode_wrapping(constant_qual_wrap.data(),
                                             static_cast<u32>(constant_qual_wrap.size()), L);
            } else {
                qual_lines = decode_wrapping(
                    (const u8*)qual_wrap_mf.data + qw_off, rm.qual_wrap_bytes, L);
                qw_off += rm.qual_wrap_bytes;
            }

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

        if (batch_start == 0 || (batch_start + batch_count) == num_records ||
            batch_start % (DECODE_BATCH * 10) == 0) {
            fprintf(stderr, "Decoded %u / %u records...\r",
                    batch_start + batch_count, num_records);
        }
    }

    // ---- Cleanup ----
    fclose(fm);
    fclose(out);
    for (auto fp : qual_pos_fps) if (fp) fclose(fp);

    fprintf(stderr, "\nDecoded %u records (version=%u, header=%s, streaming)\n",
            num_records, version,
            header_mode == HDRMODE_ILLUMINA ? "illumina" : "lcp");
    return 0;
}

// ============================================================================
// Illumina header analysis (shared by encode and encode-packed)
// ============================================================================

struct IlluminaAnalysisResult {
    bool all_illumina = true;
    bool prefix_constant = true;
    bool instrument_constant = true;
    bool read_num_sequential = true;
    bool has_pair_suffix = false;
    bool pair_suffix_constant = true;
    std::string constant_prefix;
    std::string constant_instrument;
    std::string constant_pair_suffix;
    std::unordered_set<std::string> instrument_vals;
    std::unordered_set<std::string> run_vals;
    std::unordered_set<std::string> fc_vals;
    std::unordered_set<std::string> lane_vals;
    std::unordered_set<std::string> tile_vals;
    u32 total_records = 0;
};

static IlluminaAnalysisResult analyze_illumina_headers(
    const char* data, size_t file_size,
    const std::vector<FastqRecord>& first_batch,
    size_t continue_pos)
{
    IlluminaAnalysisResult res{};

    // Analyze first batch
    for (size_t i = 0; i < first_batch.size(); i++) {
        auto illum = parse_illumina_header(first_batch[i].header);
        if (!illum.parsed) { res.all_illumina = false; break; }

        if (i == 0) {
            res.constant_prefix = illum.prefix;
            res.constant_instrument = illum.instrument;
            if (!illum.pair_suffix.empty()) {
                res.has_pair_suffix = true;
                res.constant_pair_suffix = illum.pair_suffix;
            }
        } else {
            if (illum.prefix != res.constant_prefix) res.prefix_constant = false;
            if (illum.instrument != res.constant_instrument) res.instrument_constant = false;
            if (res.has_pair_suffix) {
                if (illum.pair_suffix.empty() || illum.pair_suffix != res.constant_pair_suffix)
                    res.pair_suffix_constant = false;
            } else if (!illum.pair_suffix.empty()) {
                res.has_pair_suffix = true;
                res.pair_suffix_constant = false;
            }
        }
        if ((int64_t)(i + 1) != illum.read_num) res.read_num_sequential = false;

        res.instrument_vals.insert(illum.instrument);
        res.run_vals.insert(illum.run);
        res.fc_vals.insert(illum.flowcell);
        res.lane_vals.insert(illum.lane);
        res.tile_vals.insert(illum.tile);
    }
    res.total_records = static_cast<u32>(first_batch.size());

    // Pre-scan remaining batches for complete dictionary building
    if (res.all_illumina) {
        size_t prescan_pos = continue_pos;
        int64_t prescan_read_num = static_cast<int64_t>(first_batch.size()) + 1;
        std::vector<FastqRecord> prescan_records;
        u32 prescan_batches = 0;

        while (true) {
            u32 bc = parse_batch(data, file_size, prescan_pos, prescan_records, BATCH_SIZE);
            if (bc == 0) break;
            prescan_batches++;
            res.total_records += bc;

            for (size_t i = 0; i < prescan_records.size(); i++) {
                auto illum = parse_illumina_header(prescan_records[i].header);
                if (!illum.parsed) { res.all_illumina = false; break; }
                if (illum.prefix != res.constant_prefix) res.prefix_constant = false;
                if (illum.instrument != res.constant_instrument) res.instrument_constant = false;
                if (illum.read_num != prescan_read_num) res.read_num_sequential = false;
                prescan_read_num++;

                if (res.has_pair_suffix) {
                    if (illum.pair_suffix.empty() || illum.pair_suffix != res.constant_pair_suffix)
                        res.pair_suffix_constant = false;
                } else if (!illum.pair_suffix.empty()) {
                    res.has_pair_suffix = true;
                    res.pair_suffix_constant = false;
                }

                res.instrument_vals.insert(illum.instrument);
                res.run_vals.insert(illum.run);
                res.fc_vals.insert(illum.flowcell);
                res.lane_vals.insert(illum.lane);
                res.tile_vals.insert(illum.tile);
            }
            if (!res.all_illumina) break;
        }
        if (prescan_batches > 0) {
            fprintf(stderr, "Illumina pre-scan: %u additional batches, %u total records\n",
                    prescan_batches, res.total_records);
        }
    }

    return res;
}

// ============================================================================
// ENCODE-PACKED — produce NQF1/NQF2/NQF3 packed binary chunk(s)
// ============================================================================

static int do_encode_packed(const char* input_path, const char* output_dir, int num_chunks) {
    MappedFile mf;
    if (!mf.open(input_path)) return 1;
    if (mf.size == 0) { fprintf(stderr, "Input file is empty\n"); return 1; }

    u8 newline_style = detect_newline_style(mf.data, mf.size);
    u8 has_trailing_nl = detect_trailing_newline(mf.data, mf.size);

    mkdir_recursive(output_dir);

    // --- Full pre-scan to determine format variant ---
    size_t parse_pos = 0;
    std::vector<FastqRecord> first_batch;
    u32 first_batch_count = parse_batch(mf.data, mf.size, parse_pos, first_batch, BATCH_SIZE);
    if (first_batch_count == 0) {
        fprintf(stderr, "No FASTQ records found\n");
        return 1;
    }

    // Illumina analysis
    auto illum_result = analyze_illumina_headers(mf.data, mf.size, first_batch, parse_pos);

    // Detect fixed sequence length
    u32 fixed_seq_len = check_uniform_seq_len(first_batch);
    if (fixed_seq_len > 0 && illum_result.total_records > first_batch.size()) {
        // Verify remaining batches also have uniform length
        size_t verify_pos = parse_pos;
        std::vector<FastqRecord> verify_records;
        while (true) {
            u32 bc = parse_batch(mf.data, mf.size, verify_pos, verify_records, BATCH_SIZE);
            if (bc == 0) break;
            u32 batch_fixed = check_uniform_seq_len(verify_records);
            if (batch_fixed != fixed_seq_len) { fixed_seq_len = 0; break; }
        }
    }

    // Detect constant wrapping from first batch
    u8 wrap_mode_global = WRAPMODE_PER_RECORD;
    std::vector<u8> constant_seq_wrap, constant_qual_wrap;
    if (!first_batch.empty()) {
        auto first_sw = encode_wrapping(first_batch[0].seq_line_lengths,
                                         static_cast<u32>(first_batch[0].raw_seq.size()));
        auto first_qw = encode_wrapping(first_batch[0].qual_line_lengths,
                                         static_cast<u32>(first_batch[0].raw_seq.size()));
        bool all_same = true;
        for (size_t i = 1; i < first_batch.size() && all_same; i++) {
            auto sw = encode_wrapping(first_batch[i].seq_line_lengths,
                                       static_cast<u32>(first_batch[i].raw_seq.size()));
            auto qw = encode_wrapping(first_batch[i].qual_line_lengths,
                                       static_cast<u32>(first_batch[i].raw_seq.size()));
            if (sw != first_sw || qw != first_qw) all_same = false;
        }
        if (all_same) {
            wrap_mode_global = WRAPMODE_CONSTANT;
            constant_seq_wrap = std::move(first_sw);
            constant_qual_wrap = std::move(first_qw);
        }
    }

    // Determine NQF variant
    bool use_illumina = false;
    const char* nqf_magic = NQF3_MAGIC;

    if (illum_result.all_illumina && illum_result.prefix_constant &&
        illum_result.read_num_sequential &&
        illum_result.instrument_vals.size() <= 255 &&
        illum_result.run_vals.size() <= 255 && illum_result.fc_vals.size() <= 255 &&
        illum_result.lane_vals.size() <= 255 && illum_result.tile_vals.size() <= 255) {

        use_illumina = true;
        if (fixed_seq_len > 0 && fixed_seq_len <= MAX_COLUMNAR_SEQ_LEN) {
            nqf_magic = NQF1_MAGIC;
        } else {
            nqf_magic = NQF2_MAGIC;
        }
    } else {
        nqf_magic = NQF3_MAGIC;
    }

    fprintf(stderr, "Packed format: %.4s (illumina=%s, fixed_seq_len=%u, wrap=%s)\n",
            nqf_magic,
            use_illumina ? "yes" : "no",
            fixed_seq_len,
            wrap_mode_global == WRAPMODE_CONSTANT ? "constant" : "per-record");

    // Build EncodeContext and IlluminaMeta
    EncodeContext ctx{};
    IlluminaMeta illumina_meta{};
    std::string lcp_prefix;

    if (use_illumina) {
        auto build_dict = [](const std::unordered_set<std::string>& vals)
            -> std::pair<std::vector<std::string>, std::unordered_map<std::string, u8>> {
            std::vector<std::string> sv(vals.begin(), vals.end());
            std::sort(sv.begin(), sv.end());
            std::unordered_map<std::string, u8> m;
            for (size_t i = 0; i < sv.size(); i++) m[sv[i]] = static_cast<u8>(i);
            return {sv, m};
        };

        auto [run_dict, run_map]   = build_dict(illum_result.run_vals);
        auto [fc_dict, fc_map]     = build_dict(illum_result.fc_vals);
        auto [lane_dict, lane_map] = build_dict(illum_result.lane_vals);
        auto [tile_dict, tile_map] = build_dict(illum_result.tile_vals);

        ctx.header_mode = HDRMODE_ILLUMINA;
        ctx.instrument_constant = illum_result.instrument_constant;
        ctx.run_map  = std::move(run_map);
        ctx.fc_map   = std::move(fc_map);
        ctx.lane_map = std::move(lane_map);
        ctx.tile_map = std::move(tile_map);

        illumina_meta.flags = 0x01 | 0x02;  // read_num_sequential + prefix_constant
        if (illum_result.instrument_constant) {
            illumina_meta.flags |= 0x04;
            illumina_meta.constant_instrument = illum_result.constant_instrument;
        } else {
            auto [instr_dict, instr_map] = build_dict(illum_result.instrument_vals);
            ctx.instrument_map = std::move(instr_map);
            illumina_meta.instrument_dict = std::move(instr_dict);
        }
        illumina_meta.constant_prefix = illum_result.constant_prefix;
        illumina_meta.run_dict  = std::move(run_dict);
        illumina_meta.fc_dict   = std::move(fc_dict);
        illumina_meta.lane_dict = std::move(lane_dict);
        illumina_meta.tile_dict = std::move(tile_dict);
    } else {
        ctx.header_mode = HDRMODE_LCP;
        ctx.lcp_prefix = compute_lcp(first_batch);
        lcp_prefix = ctx.lcp_prefix;
    }
    ctx.wrap_mode = wrap_mode_global;

    // Build info_block (shared by all chunks)
    std::vector<u8> info_block;
    if (use_illumina) {
        info_block = build_illumina_block(illumina_meta);
    } else {
        u32 prefix_len = static_cast<u32>(lcp_prefix.size());
        info_block.insert(info_block.end(), (u8*)&prefix_len, (u8*)&prefix_len + 4);
        if (prefix_len > 0)
            info_block.insert(info_block.end(), lcp_prefix.begin(), lcp_prefix.end());
    }
    if (wrap_mode_global == WRAPMODE_CONSTANT) {
        u32 sw_len = static_cast<u32>(constant_seq_wrap.size());
        u32 qw_len = static_cast<u32>(constant_qual_wrap.size());
        info_block.insert(info_block.end(), (u8*)&sw_len, (u8*)&sw_len + 4);
        info_block.insert(info_block.end(), constant_seq_wrap.begin(), constant_seq_wrap.end());
        info_block.insert(info_block.end(), (u8*)&qw_len, (u8*)&qw_len + 4);
        info_block.insert(info_block.end(), constant_qual_wrap.begin(), constant_qual_wrap.end());
    }

    // --- Find chunk boundaries ---
    std::vector<size_t> chunk_starts;
    chunk_starts.push_back(0);

    if (num_chunks > 1) {
        size_t chunk_size = mf.size / num_chunks;
        for (int c = 1; c < num_chunks; c++) {
            size_t target = c * chunk_size;
            // Scan forward to find a valid FASTQ record start (@)
            while (target < mf.size) {
                if (mf.data[target] == '@' &&
                    (target == 0 || mf.data[target - 1] == '\n')) {
                    // Verify this is a real header, not a quality score:
                    // scan forward past header line, seq lines, look for '+'
                    size_t probe = target;
                    // skip header line
                    while (probe < mf.size && mf.data[probe] != '\n') probe++;
                    if (probe < mf.size) probe++; // skip \n
                    // skip at least one seq line
                    if (probe < mf.size && mf.data[probe] != '+') {
                        while (probe < mf.size && mf.data[probe] != '\n') probe++;
                        if (probe < mf.size) probe++;
                    }
                    // check for + line
                    if (probe < mf.size && mf.data[probe] == '+') {
                        break; // valid FASTQ record start
                    }
                }
                target++;
            }
            if (target < mf.size && target > chunk_starts.back()) {
                chunk_starts.push_back(target);
            }
        }
    }
    chunk_starts.push_back(mf.size); // sentinel
    int actual_chunks = static_cast<int>(chunk_starts.size()) - 1;

    // --- Encode each chunk ---
    for (int c = 0; c < actual_chunks; c++) {
        size_t start = chunk_starts[c];
        size_t end   = chunk_starts[c + 1];

        // Parse all records in this chunk
        std::vector<FastqRecord> records;
        size_t pos = start;
        parse_batch(mf.data, end, pos, records, UINT32_MAX);

        u32 nr = static_cast<u32>(records.size());
        if (nr == 0) continue;

        // Encode all records
        std::vector<EncodedRecord> encoded(nr);
        for (u32 i = 0; i < nr; i++) {
            encoded[i] = encode_one(records[i], ctx);
        }
        records.clear();
        records.shrink_to_fit();

        // Detect stream-presence flags
        bool has_any_n = false, has_any_iupac = false, has_any_case = false;
        for (u32 i = 0; i < nr; i++) {
            if (encoded[i].meta.exceptions_bytes > 0) has_any_iupac = true;
            if (encoded[i].meta.case_mode != CASE_NONE) has_any_case = true;
            if (!has_any_n) {
                for (u8 b : encoded[i].nmask_buf) {
                    if (b != 0) { has_any_n = true; break; }
                }
            }
            if (has_any_n && has_any_iupac && has_any_case) break;
        }

        u8 stream_flags = 0;
        if (!has_any_n) {
            stream_flags |= SFLAG_NO_N;
            for (u32 i = 0; i < nr; i++) {
                encoded[i].nmask_buf.clear();
                encoded[i].meta.nmask_bytes = 0;
            }
        }
        if (!has_any_iupac) {
            stream_flags |= SFLAG_NO_IUPAC;
            for (u32 i = 0; i < nr; i++) {
                encoded[i].acgtmask_buf.clear();
                encoded[i].exceptions_buf.clear();
                encoded[i].meta.acgtmask_bytes = 0;
                encoded[i].meta.exceptions_bytes = 0;
            }
        }
        if (!has_any_case) {
            stream_flags |= SFLAG_NO_CASE;
        }

        // Verify constant wrapping for subsequent chunks
        if (wrap_mode_global == WRAPMODE_CONSTANT) {
            // Wrapping buffers will be empty — already set by encode_one with ctx.wrap_mode
        }

        // Build header
        bool is_nqf1 = (memcmp(nqf_magic, NQF1_MAGIC, 4) == 0);
        bool use_compact_meta = is_nqf1;  // NQF1 always uses compact metadata (v2)

        FastqPackedHeader hdr{};
        memcpy(hdr.magic, nqf_magic, 4);
        hdr.version = use_compact_meta ? NQF_VERSION_COMPACT : NQF_VERSION;
        hdr.num_records = nr;
        hdr.newline_style = newline_style;
        hdr.has_trailing_nl = (c == actual_chunks - 1) ? has_trailing_nl : 1;
        hdr.stream_flags = stream_flags;
        hdr.wrap_mode = wrap_mode_global;
        hdr.fixed_seq_len = is_nqf1 ? fixed_seq_len : 0;
        hdr.info_block_size = static_cast<u32>(info_block.size());

        // Compute totals
        for (u32 i = 0; i < nr; i++) {
            const auto& m = encoded[i].meta;
            hdr.total_hdr     += m.header_len;
            hdr.total_plus    += m.plus_len;
            hdr.total_nmask   += m.nmask_bytes;
            hdr.total_acgt    += m.acgtmask_bytes;
            hdr.total_bases   += m.bases2_bytes;
            hdr.total_exc     += m.exceptions_bytes;
            hdr.total_case    += m.case_bytes;
            hdr.total_seq_wr  += m.seq_wrap_bytes;
            hdr.total_qual_wr += m.qual_wrap_bytes;
            hdr.total_quality += m.quality_bytes;
        }

        // For compact metadata (v2): store global case_mode in reserved[0]
        if (use_compact_meta && nr > 0) {
            hdr._reserved[0] = encoded[0].meta.case_mode;
        }

        // Write chunk file
        char chunk_name[512];
        snprintf(chunk_name, sizeof(chunk_name), "%s/chunk_%06d.bin", output_dir, c);
        FILE* out = fopen(chunk_name, "wb");
        if (!out) { fprintf(stderr, "Cannot create %s\n", chunk_name); return 1; }

        // 1. Header (80 bytes)
        fwrite(&hdr, sizeof(FastqPackedHeader), 1, out);

        // 2. Info block
        write_bytes(out, info_block.data(), info_block.size());

        // 3. Per-record metadata
        if (use_compact_meta) {
            // NQF1 v2: compact 8-byte metadata (only variable fields)
            for (u32 i = 0; i < nr; i++) {
                FastqCompactMeta1 cm{};
                cm.header_len  = static_cast<u16>(encoded[i].meta.header_len);
                cm.plus_len    = static_cast<u16>(encoded[i].meta.plus_len);
                cm.nmask_bytes = static_cast<u16>(encoded[i].meta.nmask_bytes);
                cm.bases_bytes = static_cast<u16>(encoded[i].meta.bases2_bytes);
                fwrite(&cm, sizeof(FastqCompactMeta1), 1, out);
            }
        } else {
            // NQF2/NQF3: full 44-byte metadata
            for (u32 i = 0; i < nr; i++) {
                FastqPackedRecordMeta pm{};
                pm.seq_len      = encoded[i].meta.seq_len;
                pm.header_len   = encoded[i].meta.header_len;
                pm.plus_len     = encoded[i].meta.plus_len;
                pm.nmask_bytes  = encoded[i].meta.nmask_bytes;
                pm.acgt_bytes   = encoded[i].meta.acgtmask_bytes;
                pm.bases_bytes  = encoded[i].meta.bases2_bytes;
                pm.exc_bytes    = encoded[i].meta.exceptions_bytes;
                pm.case_bytes   = encoded[i].meta.case_bytes;
                pm.seq_wr_bytes = encoded[i].meta.seq_wrap_bytes;
                pm.qual_wr_bytes= encoded[i].meta.qual_wrap_bytes;
                pm.case_mode    = encoded[i].meta.case_mode;
                fwrite(&pm, sizeof(FastqPackedRecordMeta), 1, out);
            }
        }

        // 4. Payload buffers
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].header_buf.data(), encoded[i].header_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].plus_buf.data(), encoded[i].plus_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].nmask_buf.data(), encoded[i].nmask_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].acgtmask_buf.data(), encoded[i].acgtmask_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].bases2_buf.data(), encoded[i].bases2_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].exceptions_buf.data(), encoded[i].exceptions_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].case_buf.data(), encoded[i].case_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].seq_wrap_buf.data(), encoded[i].seq_wrap_buf.size());
        for (u32 i = 0; i < nr; i++)
            write_bytes(out, encoded[i].qual_wrap_buf.data(), encoded[i].qual_wrap_buf.size());

        // 5. Quality data
        if (is_nqf1) {
            // Column-major: for each position p, write quality[p] for all records
            for (u32 p = 0; p < fixed_seq_len; p++) {
                for (u32 i = 0; i < nr; i++) {
                    write_u8(out, encoded[i].quality_buf[p]);
                }
            }
        } else {
            // Row-major: concatenate per-record quality
            for (u32 i = 0; i < nr; i++)
                write_bytes(out, encoded[i].quality_buf.data(), encoded[i].quality_buf.size());
        }

        fclose(out);

        size_t meta_bytes = use_compact_meta ? (nr * sizeof(FastqCompactMeta1))
                                              : (nr * sizeof(FastqPackedRecordMeta));
        long chunk_file_size = 80 + info_block.size() + meta_bytes
            + hdr.total_hdr + hdr.total_plus + hdr.total_nmask + hdr.total_acgt
            + hdr.total_bases + hdr.total_exc + hdr.total_case
            + hdr.total_seq_wr + hdr.total_qual_wr + hdr.total_quality;
        fprintf(stderr, "  chunk %d: %u records, %.4s, %ld bytes\n",
                c, nr, nqf_magic, chunk_file_size);
    }

    fprintf(stderr, "Encoded into %d chunk(s), format=%.4s, newline=%s, trailing_nl=%d\n",
            actual_chunks, nqf_magic,
            newline_style == NL_CRLF ? "CRLF" : "LF", has_trailing_nl);
    return 0;
}

// ============================================================================
// DECODE-PACKED — reconstruct FASTQ from NQF packed binary chunk(s)
// ============================================================================

#include <dirent.h>

static int do_decode_packed(const char* input_arg, const char* output_path) {
    // Find chunk files
    std::vector<std::string> chunk_files;

    struct stat st;
    if (stat(input_arg, &st) != 0) {
        fprintf(stderr, "Cannot access %s\n", input_arg);
        return 1;
    }

    if (S_ISDIR(st.st_mode)) {
        DIR* d = opendir(input_arg);
        if (!d) { fprintf(stderr, "Cannot open directory %s\n", input_arg); return 1; }
        struct dirent* entry;
        while ((entry = readdir(d)) != nullptr) {
            std::string name = entry->d_name;
            if (name.size() > 4 &&
                name.compare(0, 6, "chunk_") == 0 &&
                name.compare(name.size() - 4, 4, ".bin") == 0) {
                chunk_files.push_back(std::string(input_arg) + "/" + name);
            }
        }
        closedir(d);
        std::sort(chunk_files.begin(), chunk_files.end());
    } else {
        chunk_files.push_back(input_arg);
    }

    if (chunk_files.empty()) {
        fprintf(stderr, "No chunk files found in %s\n", input_arg);
        return 1;
    }

    FILE* out = fopen(output_path, "wb");
    if (!out) { fprintf(stderr, "Cannot create %s\n", output_path); return 1; }

    // 4 MB output buffer
    std::vector<char> out_buf(4 * 1024 * 1024);
    setvbuf(out, out_buf.data(), _IOFBF, out_buf.size());

    u32 total_decoded = 0;

    for (size_t ci = 0; ci < chunk_files.size(); ci++) {
        auto file_data = read_file_bytes(chunk_files[ci]);

        if (file_data.size() < sizeof(FastqPackedHeader)) {
            fprintf(stderr, "Chunk %s too small (%zu bytes)\n",
                    chunk_files[ci].c_str(), file_data.size());
            fclose(out);
            return 1;
        }

        FastqPackedHeader hdr;
        memcpy(&hdr, file_data.data(), sizeof(FastqPackedHeader));

        // Validate magic
        bool is_nqf1 = (memcmp(hdr.magic, NQF1_MAGIC, 4) == 0);
        bool is_nqf2 = (memcmp(hdr.magic, NQF2_MAGIC, 4) == 0);
        bool is_nqf3 = (memcmp(hdr.magic, NQF3_MAGIC, 4) == 0);
        if (!is_nqf1 && !is_nqf2 && !is_nqf3) {
            fprintf(stderr, "Bad magic in %s: %.4s\n", chunk_files[ci].c_str(), hdr.magic);
            fclose(out);
            return 1;
        }
        if (hdr.version != NQF_VERSION && hdr.version != NQF_VERSION_COMPACT) {
            fprintf(stderr, "Unsupported version %u in %s\n",
                    hdr.version, chunk_files[ci].c_str());
            fclose(out);
            return 1;
        }

        u32 nr = hdr.num_records;
        const char* nl = (hdr.newline_style == NL_CRLF) ? "\r\n" : "\n";
        size_t nl_len = (hdr.newline_style == NL_CRLF) ? 2 : 1;

        // Parse info_block
        size_t offset = sizeof(FastqPackedHeader);
        const u8* info_data = file_data.data() + offset;

        u8 header_mode = (is_nqf1 || is_nqf2) ? HDRMODE_ILLUMINA : HDRMODE_LCP;
        IlluminaMeta illum_meta{};
        std::string lcp_prefix;
        std::vector<u8> const_seq_wrap, const_qual_wrap;

        if (header_mode == HDRMODE_ILLUMINA) {
            illum_meta = read_illumina_block_from_bytes(info_data, hdr.info_block_size);
        } else {
            // LCP: u32 prefix_len + bytes
            u32 prefix_len = 0;
            if (hdr.info_block_size >= 4) {
                memcpy(&prefix_len, info_data, 4);
                if (prefix_len > 0)
                    lcp_prefix = std::string((const char*)info_data + 4, prefix_len);
            }
        }

        // Parse constant wrapping from info_block (if applicable)
        if (hdr.wrap_mode == WRAPMODE_CONSTANT) {
            size_t ib_off = 0;
            if (header_mode == HDRMODE_ILLUMINA) {
                // Illumina block was already parsed; find where constant wrap starts
                // The illumina block is variable-length, so skip it:
                // We need to re-parse to find the end. Use a simpler approach:
                // rebuild the illumina block to find its size.
                auto rebuilt = build_illumina_block(illum_meta);
                ib_off = rebuilt.size();
            } else {
                u32 prefix_len = 0;
                memcpy(&prefix_len, info_data, 4);
                ib_off = 4 + prefix_len;
            }
            // Now read constant wrapping
            if (ib_off + 4 <= hdr.info_block_size) {
                u32 sw_len;
                memcpy(&sw_len, info_data + ib_off, 4);
                ib_off += 4;
                const_seq_wrap.assign(info_data + ib_off, info_data + ib_off + sw_len);
                ib_off += sw_len;
            }
            if (ib_off + 4 <= hdr.info_block_size) {
                u32 qw_len;
                memcpy(&qw_len, info_data + ib_off, 4);
                ib_off += 4;
                const_qual_wrap.assign(info_data + ib_off, info_data + ib_off + qw_len);
                ib_off += qw_len;
            }
        }

        offset += hdr.info_block_size;

        // Read per-record metadata
        bool compact_meta = (hdr.version == NQF_VERSION_COMPACT);
        size_t meta_size = compact_meta ? (nr * sizeof(FastqCompactMeta1))
                                        : (nr * sizeof(FastqPackedRecordMeta));
        if (file_data.size() < offset + meta_size) {
            fprintf(stderr, "Truncated metadata in %s\n", chunk_files[ci].c_str());
            fclose(out);
            return 1;
        }

        std::vector<FastqPackedRecordMeta> metas(nr);
        if (compact_meta) {
            // NQF1 v2: expand compact 8-byte metadata to full struct
            u8 global_case_mode = hdr._reserved[0];
            const u8* meta_raw = file_data.data() + offset;
            for (u32 i = 0; i < nr; i++) {
                FastqCompactMeta1 cm;
                memcpy(&cm, meta_raw + i * sizeof(FastqCompactMeta1), sizeof(FastqCompactMeta1));
                metas[i] = {};
                metas[i].seq_len      = hdr.fixed_seq_len;
                metas[i].header_len   = cm.header_len;
                metas[i].plus_len     = cm.plus_len;
                metas[i].nmask_bytes  = cm.nmask_bytes;
                metas[i].bases_bytes  = cm.bases_bytes;
                metas[i].acgt_bytes   = 0;  // inferred from stream_flags
                metas[i].exc_bytes    = 0;
                metas[i].case_bytes   = 0;
                metas[i].seq_wr_bytes = 0;  // inferred from wrap_mode
                metas[i].qual_wr_bytes= 0;
                metas[i].case_mode    = global_case_mode;
            }
        } else {
            // v1: full 44-byte metadata
            memcpy(metas.data(), file_data.data() + offset, meta_size);
        }
        offset += meta_size;

        // Payload starts at offset
        const u8* payload = file_data.data() + offset;

        // Compute payload buffer offsets from totals
        size_t off_hdr      = 0;
        size_t off_plus     = off_hdr + hdr.total_hdr;
        size_t off_nmask    = off_plus + hdr.total_plus;
        size_t off_acgt     = off_nmask + hdr.total_nmask;
        size_t off_bases    = off_acgt + hdr.total_acgt;
        size_t off_exc      = off_bases + hdr.total_bases;
        size_t off_case     = off_exc + hdr.total_exc;
        size_t off_seq_wr   = off_case + hdr.total_case;
        size_t off_qual_wr  = off_seq_wr + hdr.total_seq_wr;
        size_t off_quality  = off_qual_wr + hdr.total_qual_wr;

        // Stream cursors
        size_t cur_hdr = 0, cur_plus = 0, cur_nm = 0, cur_am = 0, cur_b2 = 0;
        size_t cur_ex = 0, cur_cs = 0, cur_sw = 0, cur_qw = 0, cur_q = 0;

        // Global read number counter for Illumina reconstruction
        // (continues across chunks)
        static u32 global_read_num_base = 0;
        if (ci == 0) global_read_num_base = 0;

        for (u32 r = 0; r < nr; r++) {
            const auto& rm = metas[r];
            u32 L = rm.seq_len;

            // --- Write header line ---
            fwrite("@", 1, 1, out);

            if (header_mode == HDRMODE_ILLUMINA) {
                const u8* hdr_data = payload + off_hdr + cur_hdr;
                size_t hoff = 0;

                std::string instrument_str;
                bool instr_const = (illum_meta.flags & 0x04) != 0;
                if (instr_const) {
                    instrument_str = illum_meta.constant_instrument;
                } else {
                    u8 instr_idx = hdr_data[hoff++];
                    instrument_str = illum_meta.instrument_dict[instr_idx];
                }

                u8 run_idx  = hdr_data[hoff++];
                u8 fc_idx   = hdr_data[hoff++];
                u8 lane_idx = hdr_data[hoff++];
                u8 tile_idx = hdr_data[hoff++];

                const u8* vptr = hdr_data + hoff;
                const u8* vend = hdr_data + rm.header_len;
                u64 x_int = decode_varint(vptr, vend);
                u64 y_int = decode_varint(vptr, vend);

                // Write: PREFIX.READNUM INSTRUMENT:RUN:FLOWCELL:LANE:TILE:X:Y
                fwrite(illum_meta.constant_prefix.data(),
                       1, illum_meta.constant_prefix.size(), out);
                fprintf(out, ".%u ", total_decoded + r + 1);
                fwrite(instrument_str.data(), 1, instrument_str.size(), out);
                fwrite(":", 1, 1, out);
                fwrite(illum_meta.run_dict[run_idx].data(),
                       1, illum_meta.run_dict[run_idx].size(), out);
                fwrite(":", 1, 1, out);
                fwrite(illum_meta.fc_dict[fc_idx].data(),
                       1, illum_meta.fc_dict[fc_idx].size(), out);
                fwrite(":", 1, 1, out);
                fwrite(illum_meta.lane_dict[lane_idx].data(),
                       1, illum_meta.lane_dict[lane_idx].size(), out);
                fwrite(":", 1, 1, out);
                fwrite(illum_meta.tile_dict[tile_idx].data(),
                       1, illum_meta.tile_dict[tile_idx].size(), out);
                fwrite(":", 1, 1, out);
                fprintf(out, "%llu", (unsigned long long)x_int);
                fwrite(":", 1, 1, out);
                fprintf(out, "%llu", (unsigned long long)y_int);
            } else {
                // LCP mode: prepend prefix + suffix
                if (!lcp_prefix.empty()) {
                    fwrite(lcp_prefix.data(), 1, lcp_prefix.size(), out);
                }
                fwrite(payload + off_hdr + cur_hdr, 1, rm.header_len, out);
            }
            fwrite(nl, 1, nl_len, out);
            cur_hdr += rm.header_len;

            // --- Decode sequence ---
            auto raw_seq = decode_sequence(
                L,
                payload + off_nmask + cur_nm, rm.nmask_bytes,
                payload + off_acgt + cur_am, rm.acgt_bytes,
                payload + off_bases + cur_b2, rm.bases_bytes,
                payload + off_exc + cur_ex, rm.exc_bytes,
                payload + off_case + cur_cs, rm.case_bytes, rm.case_mode);

            cur_nm += rm.nmask_bytes;
            cur_am += rm.acgt_bytes;
            cur_b2 += rm.bases_bytes;
            cur_ex += rm.exc_bytes;
            cur_cs += rm.case_bytes;

            // --- Write sequence lines ---
            std::vector<u32> seq_lines;
            if (hdr.wrap_mode == WRAPMODE_CONSTANT) {
                seq_lines = decode_wrapping(const_seq_wrap.data(),
                                             static_cast<u32>(const_seq_wrap.size()), L);
            } else {
                seq_lines = decode_wrapping(
                    payload + off_seq_wr + cur_sw, rm.seq_wr_bytes, L);
                cur_sw += rm.seq_wr_bytes;
            }

            u32 seq_pos = 0;
            for (size_t k = 0; k < seq_lines.size(); k++) {
                u32 ll = seq_lines[k];
                fwrite(raw_seq.data() + seq_pos, 1, ll, out);
                seq_pos += ll;
                fwrite(nl, 1, nl_len, out);
            }

            // --- Write plus line ---
            fwrite("+", 1, 1, out);
            if (rm.plus_len > 0) {
                fwrite(payload + off_plus + cur_plus, 1, rm.plus_len, out);
            }
            fwrite(nl, 1, nl_len, out);
            cur_plus += rm.plus_len;

            // --- Decode quality ---
            std::vector<u8> qual_bytes(L);
            if (is_nqf1 && hdr.fixed_seq_len > 0) {
                // Column-major: quality[p] is at offset p * nr + r
                for (u32 p = 0; p < hdr.fixed_seq_len; p++) {
                    qual_bytes[p] = payload[off_quality + (u64)p * nr + r];
                }
            } else {
                // Row-major: sequential per-record
                memcpy(qual_bytes.data(), payload + off_quality + cur_q, L);
                cur_q += L;
            }

            // --- Write quality lines ---
            std::vector<u32> qual_lines;
            if (hdr.wrap_mode == WRAPMODE_CONSTANT) {
                qual_lines = decode_wrapping(const_qual_wrap.data(),
                                              static_cast<u32>(const_qual_wrap.size()), L);
            } else {
                qual_lines = decode_wrapping(
                    payload + off_qual_wr + cur_qw, rm.qual_wr_bytes, L);
                cur_qw += rm.qual_wr_bytes;
            }

            u32 qual_pos = 0;
            for (size_t k = 0; k < qual_lines.size(); k++) {
                u32 ll = qual_lines[k];
                fwrite(qual_bytes.data() + qual_pos, 1, ll, out);
                qual_pos += ll;

                bool is_last_record = (ci == chunk_files.size() - 1 && r == nr - 1);
                bool is_last_line = (k == qual_lines.size() - 1);
                if (is_last_record && is_last_line && !hdr.has_trailing_nl) {
                    // Don't write trailing newline
                } else {
                    fwrite(nl, 1, nl_len, out);
                }
            }
        }

        total_decoded += nr;
        fprintf(stderr, "  chunk %zu: decoded %u records (total: %u)\n",
                ci, nr, total_decoded);
    }

    fclose(out);
    fprintf(stderr, "Decoded %u records from %zu chunk(s)\n",
            total_decoded, chunk_files.size());
    return 0;
}

// ============================================================================
// VALIDATE — check stream invariants (v1, v2, v3)
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
    if (version != META_VERSION_V1 && version != META_VERSION_V2 && version != META_VERSION_V3) {
        fprintf(stderr, "FAIL: unsupported version %u\n", version);
        return 1;
    }

    // Parse v2/v3 extended header
    u8 quality_layout = QLAYOUT_PER_RECORD;
    u8 header_mode = HDRMODE_LCP;
    u8 meta_mode = META_FULL;
    u32 fixed_seq_len = 0;
    u32 prefix_len = 0;
    size_t global_hdr_size = 0;

    if (version >= META_VERSION_V2) {
        if (meta.size() < 24) {
            fprintf(stderr, "FAIL: meta.bin too small for v2/v3 header\n");
            return 1;
        }
        quality_layout = meta[14];
        u8 mode_byte = meta[15];
        header_mode = mode_byte & 0x0F;
        meta_mode = (mode_byte >> 4) & 0x0F;
        memcpy(&fixed_seq_len, meta.data() + 16, 4);

        if (version >= META_VERSION_V3 && header_mode == HDRMODE_ILLUMINA) {
            u32 illumina_block_size;
            memcpy(&illumina_block_size, meta.data() + 20, 4);
            global_hdr_size = 24 + illumina_block_size;
        } else {
            header_mode = HDRMODE_LCP;
            memcpy(&prefix_len, meta.data() + 20, 4);
            global_hdr_size = 24 + prefix_len;
        }
    } else {
        global_hdr_size = 20;
    }

    // Parse wrap_mode (v3)
    u8 wrap_mode = WRAPMODE_PER_RECORD;
    if (version >= META_VERSION_V3) {
        if (meta.size() <= global_hdr_size) {
            fprintf(stderr, "FAIL: meta.bin too small for wrap_mode\n");
            return 1;
        }
        wrap_mode = meta[global_hdr_size];
        global_hdr_size += 1;  // wrap_mode byte
        if (wrap_mode == WRAPMODE_CONSTANT) {
            // Skip constant wrapping data: u32 sw_len + sw_data + u32 qw_len + qw_data
            if (global_hdr_size + 4 > meta.size()) {
                fprintf(stderr, "FAIL: meta.bin too small for constant seq wrapping\n");
                return 1;
            }
            u32 sw_len;
            memcpy(&sw_len, meta.data() + global_hdr_size, 4);
            global_hdr_size += 4 + sw_len;
            if (global_hdr_size + 4 > meta.size()) {
                fprintf(stderr, "FAIL: meta.bin too small for constant qual wrapping\n");
                return 1;
            }
            u32 qw_len;
            memcpy(&qw_len, meta.data() + global_hdr_size, 4);
            global_hdr_size += 4 + qw_len;
        }
    }

    if (meta.size() < global_hdr_size) {
        fprintf(stderr, "FAIL: meta.bin too small for global header\n");
        return 1;
    }

    // Validate per-record meta size
    size_t expected_meta;
    if (meta_mode == META_COMPACT) {
        expected_meta = global_hdr_size + sizeof(FastqRecordMeta) + num_records * sizeof(u32);
    } else {
        expected_meta = global_hdr_size + num_records * sizeof(FastqRecordMeta);
    }
    if (meta.size() < expected_meta) {
        fprintf(stderr, "FAIL: meta.bin too small for %u records (meta_mode=%u)\n",
                num_records, meta_mode);
        return 1;
    }

    fprintf(stderr, "Validating %u records (version=%u, nl=%s, trail_nl=%d",
            num_records, version,
            nl_style == NL_CRLF ? "CRLF" : "LF", trail_nl);
    if (version >= META_VERSION_V2) {
        fprintf(stderr, ", quality=%s, fixed_seq_len=%u, header=%s, meta=%s, wrap=%s",
                quality_layout == QLAYOUT_PER_POS_RAW ? "per-position-raw" :
                quality_layout == QLAYOUT_PER_POSITION ? "per-position-delta" :
                quality_layout == QLAYOUT_COLUMNAR ? "columnar" : "per-record",
                fixed_seq_len,
                header_mode == HDRMODE_ILLUMINA ? "illumina" : "lcp",
                meta_mode == META_COMPACT ? "compact" : "full",
                wrap_mode == WRAPMODE_CONSTANT ? "constant" : "per-record");
    }
    fprintf(stderr, ")...\n");

    // mmap stream files for validation
    MappedFile headers_mf, plus_mf, nmask_mf, acgtmask_mf, bases2_mf;
    MappedFile exceptions_mf, case_mf, seq_wrap_mf, qual_wrap_mf;
    if (!headers_mf.open((dir + "/headers.bin").c_str())) return 1;
    if (!plus_mf.open((dir + "/plus.bin").c_str())) return 1;
    if (!nmask_mf.open((dir + "/nmask.bin").c_str())) return 1;
    if (!acgtmask_mf.open((dir + "/acgtmask.bin").c_str())) return 1;
    if (!bases2_mf.open((dir + "/bases2.bin").c_str())) return 1;
    if (!exceptions_mf.open((dir + "/exceptions.bin").c_str())) return 1;
    if (!case_mf.open((dir + "/case.bin").c_str())) return 1;
    if (wrap_mode != WRAPMODE_CONSTANT) {
        if (!seq_wrap_mf.open((dir + "/seq_wrap.bin").c_str())) return 1;
        if (!qual_wrap_mf.open((dir + "/qual_wrap.bin").c_str())) return 1;
    }

    // Wrap mmapped data for bounds-checking macros
    const u8* headers_data = (const u8*)headers_mf.data;
    const u8* plus_data = (const u8*)plus_mf.data;
    const u8* nmask_data = (const u8*)nmask_mf.data;
    const u8* acgtmask_data = (const u8*)acgtmask_mf.data;
    const u8* bases2_data = (const u8*)bases2_mf.data;
    const u8* exceptions_data = (const u8*)exceptions_mf.data;
    const u8* seq_wrap_data = (const u8*)seq_wrap_mf.data;
    const u8* qual_wrap_data = (const u8*)qual_wrap_mf.data;

    // Quality validation depends on layout
    MappedFile quality_mf;
    if (version >= META_VERSION_V2 &&
        (quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW) &&
        fixed_seq_len > 0) {
        for (u32 p = 0; p < fixed_seq_len; p++) {
            char fname[64];
            snprintf(fname, sizeof(fname), "quality_pos_%04u.bin", p);
            std::string fpath = dir + "/" + fname;
            struct stat st;
            if (stat(fpath.c_str(), &st) == 0) {
                if ((size_t)st.st_size != num_records) {
                    fprintf(stderr, "FAIL: %s size %lld != expected %u records\n",
                            fname, (long long)st.st_size, num_records);
                    errors++;
                }
            } else {
                fprintf(stderr, "FAIL: cannot stat %s\n", fname);
                errors++;
            }
        }
    } else {
        if (!quality_mf.open((dir + "/quality.bin").c_str())) return 1;
        if (version >= META_VERSION_V2 && quality_layout == QLAYOUT_COLUMNAR && fixed_seq_len > 0) {
            u64 expected_qual = (u64)num_records * fixed_seq_len;
            if (quality_mf.size != expected_qual) {
                fprintf(stderr, "FAIL: quality.bin size %zu != expected %llu for columnar layout\n",
                        quality_mf.size, (unsigned long long)expected_qual);
                errors++;
            }
        }
    }

    // Build per-record metas (handle compact mode)
    std::vector<FastqRecordMeta> val_metas(num_records);
    if (meta_mode == META_COMPACT && num_records > 0) {
        FastqRecordMeta tmpl;
        memcpy(&tmpl, meta.data() + global_hdr_size, sizeof(FastqRecordMeta));
        const u8* hlen_base = meta.data() + global_hdr_size + sizeof(FastqRecordMeta);
        for (u32 i = 0; i < num_records; i++) {
            val_metas[i] = tmpl;
            u32 hl;
            memcpy(&hl, hlen_base + i * sizeof(u32), sizeof(u32));
            val_metas[i].header_len = hl;
        }
    } else {
        for (u32 i = 0; i < num_records; i++) {
            memcpy(&val_metas[i], meta.data() + global_hdr_size + i * sizeof(FastqRecordMeta),
                   sizeof(FastqRecordMeta));
        }
    }

    size_t hdr_off = 0, plus_off = 0, nm_off = 0, am_off = 0, b2_off = 0;
    size_t ex_off = 0, cs_off = 0, sw_off = 0, qw_off = 0, q_off = 0;

    for (u32 r = 0; r < num_records; r++) {
        const FastqRecordMeta& rm = val_metas[r];
        u32 L = rm.seq_len;

        // Header bounds
        if (hdr_off + rm.header_len > headers_mf.size) {
            fprintf(stderr, "FAIL: record[%u]: header overflows\n", r); errors++;
        }
        hdr_off += rm.header_len;

        // Plus bounds
        if (plus_off + rm.plus_len > plus_mf.size) {
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
        if (nm_off + rm.nmask_bytes <= nmask_mf.size) {
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
        if (am_off + rm.acgtmask_bytes <= acgtmask_mf.size) {
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
            const u8* ptr = exceptions_data + ex_off;
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

        // Sequence and quality wrapping
        if (wrap_mode == WRAPMODE_CONSTANT) {
            // Constant wrapping: per-record wrap bytes should be 0
            if (rm.seq_wrap_bytes != 0) {
                fprintf(stderr, "FAIL: record[%u]: constant wrap but seq_wrap_bytes=%u\n",
                        r, rm.seq_wrap_bytes); errors++;
            }
            if (rm.qual_wrap_bytes != 0) {
                fprintf(stderr, "FAIL: record[%u]: constant wrap but qual_wrap_bytes=%u\n",
                        r, rm.qual_wrap_bytes); errors++;
            }
        } else {
            // Sequence wrapping
            if (sw_off < seq_wrap_mf.size) {
                u8 wm = seq_wrap_data[sw_off];
                if (wm == WRAP_COMPACT && rm.seq_wrap_bytes >= 9) {
                    u32 width, last_len;
                    memcpy(&width, seq_wrap_data + sw_off + 1, 4);
                    memcpy(&last_len, seq_wrap_data + sw_off + 5, 4);
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
                    memcpy(&num_lines, seq_wrap_data + sw_off + 1, 4);
                    u32 total = 0;
                    for (u32 k = 0; k < num_lines && (sw_off + 5 + (k+1)*4) <= seq_wrap_mf.size; k++) {
                        u32 ll;
                        memcpy(&ll, seq_wrap_data + sw_off + 5 + k * 4, 4);
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
            if (qw_off < qual_wrap_mf.size) {
                u8 wm = qual_wrap_data[qw_off];
                if (wm == WRAP_COMPACT && rm.qual_wrap_bytes >= 9) {
                    u32 width, last_len;
                    memcpy(&width, qual_wrap_data + qw_off + 1, 4);
                    memcpy(&last_len, qual_wrap_data + qw_off + 5, 4);
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
                    memcpy(&num_lines, qual_wrap_data + qw_off + 1, 4);
                    u32 total = 0;
                    for (u32 k = 0; k < num_lines && (qw_off + 5 + (k+1)*4) <= qual_wrap_mf.size; k++) {
                        u32 ll;
                        memcpy(&ll, qual_wrap_data + qw_off + 5 + k * 4, 4);
                        total += ll;
                    }
                    if (total != L) {
                        fprintf(stderr, "FAIL: record[%u]: qual EXPLICIT sum=%u != L=%u\n",
                                r, total, L); errors++;
                    }
                }
            }
            qw_off += rm.qual_wrap_bytes;
        }

        // Quality bytes
        if (version >= META_VERSION_V2 &&
            (quality_layout == QLAYOUT_COLUMNAR || quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW)) {
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
        if (version >= META_VERSION_V2) {
            if (rm.quality_mode != QUAL_RAW) {
                fprintf(stderr, "FAIL: record[%u]: v2/v3 quality_mode=%u, expected 0 (RAW)\n",
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
    check("headers.bin",    hdr_off,  headers_mf.size);
    check("plus.bin",       plus_off, plus_mf.size);
    check("nmask.bin",      nm_off,   nmask_mf.size);
    check("acgtmask.bin",   am_off,   acgtmask_mf.size);
    check("bases2.bin",     b2_off,   bases2_mf.size);
    check("exceptions.bin", ex_off,   exceptions_mf.size);
    check("case.bin",       cs_off,   case_mf.size);
    if (wrap_mode != WRAPMODE_CONSTANT) {
        check("seq_wrap.bin",   sw_off,   seq_wrap_mf.size);
        check("qual_wrap.bin",  qw_off,   qual_wrap_mf.size);
    }
    if (!(version >= META_VERSION_V2 &&
          (quality_layout == QLAYOUT_COLUMNAR || quality_layout == QLAYOUT_PER_POSITION || quality_layout == QLAYOUT_PER_POS_RAW))) {
        check("quality.bin", q_off, quality_mf.size);
    }

    if (errors == 0) {
        fprintf(stderr, "OK: all %u records pass invariant checks\n", num_records);
    } else {
        fprintf(stderr, "FAILED: %d invariant violation(s)\n", errors);
    }
    return errors > 0 ? 1 : 0;
}

// ============================================================================
// ENCODE-CSV — produce meta.bin + part_NNN.tsv files for CSV profile
// ============================================================================

static constexpr char FQMETA1_MAGIC[7] = {'F','Q','M','E','T','A','1'};

// Flags byte for FQMETA1
static constexpr u8 CSVF_ALL_ILLUMINA        = 0x01;
static constexpr u8 CSVF_PREFIX_CONST         = 0x02;
static constexpr u8 CSVF_INSTRUMENT_CONST     = 0x04;
static constexpr u8 CSVF_READNUM_SEQUENTIAL   = 0x08;
static constexpr u8 CSVF_PAIR_SUFFIX_CONST    = 0x10;
static constexpr u8 CSVF_HAS_TRAILING_NL      = 0x20;
static constexpr u8 CSVF_NEWLINE_CRLF         = 0x40;

// Plus comment modes
static constexpr u8 PLUS_EMPTY       = 0;  // "+" only (no comment)
static constexpr u8 PLUS_HEADER_COPY = 1;  // "+" followed by header text
static constexpr u8 PLUS_STORED      = 2;  // stored in plus.bin

// Wrapping modes for CSV
static constexpr u8 CSV_WRAP_NONE     = 0;  // single-line seq/qual
static constexpr u8 CSV_WRAP_CONSTANT = 1;  // all records share same wrap pattern
static constexpr u8 CSV_WRAP_STORED   = 2;  // per-record wrap in wrap.bin

static int do_encode_csv(const char* input_path, const char* output_dir, int num_parts) {
    MappedFile mf;
    if (!mf.open(input_path)) return 1;
    if (mf.size == 0) { fprintf(stderr, "Input file is empty\n"); return 1; }

    u8 newline_style = detect_newline_style(mf.data, mf.size);
    u8 has_trailing_nl = detect_trailing_newline(mf.data, mf.size);

    mkdir_recursive(output_dir);

    // =====================================================================
    // PASS 1: Lightweight scan — detect metadata without storing records
    // =====================================================================
    // This pass only tracks counters and booleans, keeping memory O(1)
    // relative to input size. Illumina analysis reuses existing streaming
    // infrastructure (first batch + prescan batches).

    // 1a. Illumina analysis (streaming — uses its own batched prescan)
    size_t illum_pos = 0;
    std::vector<FastqRecord> first_batch;
    parse_batch(mf.data, mf.size, illum_pos, first_batch, BATCH_SIZE);
    auto illum_result = analyze_illumina_headers(mf.data, mf.size, first_batch, illum_pos);
    bool all_illumina = illum_result.all_illumina;
    first_batch.clear();  // free memory

    // 1b. Count records + detect plus_mode + wrap_mode (streaming, O(1) memory)
    u32 total_records = 0;
    bool plus_any_nonempty = false;
    bool plus_all_header_copy = true;
    bool wrap_any_wrapped = false;
    bool wrap_all_same = true;
    u32 wrap_first_width = 0;
    bool wrap_first_set = false;

    {
        size_t scan_pos = 0;
        std::vector<FastqRecord> batch;
        while (true) {
            u32 bc = parse_batch(mf.data, mf.size, scan_pos, batch, BATCH_SIZE);
            if (bc == 0) break;
            for (auto& rec : batch) {
                // Plus mode detection
                if (!rec.plus_comment.empty()) {
                    plus_any_nonempty = true;
                    if (rec.plus_comment != rec.header)
                        plus_all_header_copy = false;
                } else {
                    if (plus_any_nonempty) plus_all_header_copy = false;
                }
                // Wrap mode detection
                bool seq_wrapped = rec.seq_line_lengths.size() > 1;
                bool qual_wrapped = rec.qual_line_lengths.size() > 1;
                if (seq_wrapped || qual_wrapped) {
                    wrap_any_wrapped = true;
                    if (!wrap_first_set && seq_wrapped && rec.seq_line_lengths.size() >= 2) {
                        wrap_first_width = rec.seq_line_lengths[0];
                        wrap_first_set = true;
                    } else if (seq_wrapped && rec.seq_line_lengths[0] != wrap_first_width) {
                        wrap_all_same = false;
                    }
                }
                total_records++;
            }
            // batch is cleared by next parse_batch call
        }
    }
    if (total_records == 0) {
        fprintf(stderr, "No FASTQ records found\n");
        return 1;
    }
    fprintf(stderr, "Pass 1 complete: %u records scanned\n", total_records);

    u8 plus_mode = PLUS_EMPTY;
    if (!plus_any_nonempty) plus_mode = PLUS_EMPTY;
    else if (plus_all_header_copy) plus_mode = PLUS_HEADER_COPY;
    else plus_mode = PLUS_STORED;
    fprintf(stderr, "Plus mode: %s\n",
            plus_mode == PLUS_EMPTY ? "empty" :
            plus_mode == PLUS_HEADER_COPY ? "header_copy" : "stored");

    u8 csv_wrap_mode = CSV_WRAP_NONE;
    u32 constant_wrap_width = 0;
    if (!wrap_any_wrapped) {
        csv_wrap_mode = CSV_WRAP_NONE;
    } else if (wrap_all_same && wrap_first_width > 0) {
        csv_wrap_mode = CSV_WRAP_CONSTANT;
        constant_wrap_width = wrap_first_width;
    } else {
        csv_wrap_mode = CSV_WRAP_STORED;
    }
    fprintf(stderr, "Wrap mode: %s",
            csv_wrap_mode == CSV_WRAP_NONE ? "none" :
            csv_wrap_mode == CSV_WRAP_CONSTANT ? "constant" : "stored");
    if (csv_wrap_mode == CSV_WRAP_CONSTANT)
        fprintf(stderr, " (width=%u)", constant_wrap_width);
    fprintf(stderr, "\n");

    // --- Build dictionaries (from analysis result — already in memory, small) ---
    auto build_sorted_dict = [](const std::unordered_set<std::string>& vals)
        -> std::pair<std::vector<std::string>, std::unordered_map<std::string, int>> {
        std::vector<std::string> sv(vals.begin(), vals.end());
        std::sort(sv.begin(), sv.end());
        std::unordered_map<std::string, int> m;
        for (size_t i = 0; i < sv.size(); i++) m[sv[i]] = (int)i;
        return {sv, m};
    };

    std::vector<std::string> instr_dict, run_dict, fc_dict, lane_dict, tile_dict;
    std::unordered_map<std::string, int> instr_map, run_map, fc_map, lane_map, tile_map;

    if (all_illumina) {
        if (!illum_result.instrument_constant) {
            auto [id, im] = build_sorted_dict(illum_result.instrument_vals);
            instr_dict = std::move(id); instr_map = std::move(im);
        }
        auto [rd, rm] = build_sorted_dict(illum_result.run_vals);
        auto [fd, fm] = build_sorted_dict(illum_result.fc_vals);
        auto [ld, lm] = build_sorted_dict(illum_result.lane_vals);
        auto [td, tm] = build_sorted_dict(illum_result.tile_vals);
        run_dict = std::move(rd);   run_map = std::move(rm);
        fc_dict = std::move(fd);    fc_map = std::move(fm);
        lane_dict = std::move(ld);  lane_map = std::move(lm);
        tile_dict = std::move(td);  tile_map = std::move(tm);

        fprintf(stderr, "Illumina mode: prefix=%s%s, instrument=%s%s, "
                "runs=%zu, flowcells=%zu, lanes=%zu, tiles=%zu\n",
                illum_result.constant_prefix.c_str(),
                illum_result.prefix_constant ? " (const)" : " (varies)",
                illum_result.constant_instrument.c_str(),
                illum_result.instrument_constant ? " (const)" : " (varies)",
                run_dict.size(), fc_dict.size(), lane_dict.size(), tile_dict.size());
        if (illum_result.has_pair_suffix) {
            fprintf(stderr, "  Pair suffix: %s%s\n",
                    illum_result.constant_pair_suffix.c_str(),
                    illum_result.pair_suffix_constant ? " (const)" : " (varies)");
        }
    } else {
        fprintf(stderr, "Generic mode (non-Illumina headers)\n");
    }

    // --- Write meta.bin ---
    std::string meta_path = std::string(output_dir) + "/meta.bin";
    {
        FILE* fmeta = fopen(meta_path.c_str(), "wb");
        if (!fmeta) { fprintf(stderr, "Cannot open %s\n", meta_path.c_str()); return 1; }

        fwrite(FQMETA1_MAGIC, 1, 7, fmeta);

        u32 nr = total_records;
        fwrite(&nr, 4, 1, fmeta);

        u8 flags = 0;
        if (all_illumina) flags |= CSVF_ALL_ILLUMINA;
        if (illum_result.prefix_constant) flags |= CSVF_PREFIX_CONST;
        if (illum_result.instrument_constant) flags |= CSVF_INSTRUMENT_CONST;
        if (illum_result.read_num_sequential) flags |= CSVF_READNUM_SEQUENTIAL;
        if (illum_result.has_pair_suffix && illum_result.pair_suffix_constant)
            flags |= CSVF_PAIR_SUFFIX_CONST;
        if (has_trailing_nl) flags |= CSVF_HAS_TRAILING_NL;
        if (newline_style == NL_CRLF) flags |= CSVF_NEWLINE_CRLF;
        fwrite(&flags, 1, 1, fmeta);

        // plus_mode and wrap_mode
        fwrite(&plus_mode, 1, 1, fmeta);
        fwrite(&csv_wrap_mode, 1, 1, fmeta);

        auto write_meta_str = [&](const std::string& s) {
            int16_t len = (int16_t)s.size();
            fwrite(&len, 2, 1, fmeta);
            if (len > 0) fwrite(s.c_str(), 1, len, fmeta);
        };

        if (illum_result.prefix_constant) write_meta_str(illum_result.constant_prefix);
        if (illum_result.instrument_constant) write_meta_str(illum_result.constant_instrument);
        if (illum_result.has_pair_suffix && illum_result.pair_suffix_constant)
            write_meta_str(illum_result.constant_pair_suffix);

        auto write_meta_dict = [&](const std::vector<std::string>& dict) {
            int32_t dsize = (int32_t)dict.size();
            fwrite(&dsize, 4, 1, fmeta);
            for (auto& s : dict) write_meta_str(s);
        };

        if (all_illumina) {
            if (!illum_result.instrument_constant) write_meta_dict(instr_dict);
            write_meta_dict(run_dict);
            write_meta_dict(fc_dict);
            write_meta_dict(lane_dict);
            write_meta_dict(tile_dict);
        }

        if (csv_wrap_mode == CSV_WRAP_CONSTANT) {
            fwrite(&constant_wrap_width, 4, 1, fmeta);
        }

        u32 marker = 0xDEADBEEF;
        fwrite(&marker, 4, 1, fmeta);
        fclose(fmeta);
        fprintf(stderr, "Meta written: %s\n", meta_path.c_str());
    }

    // =====================================================================
    // PASS 2: Stream records to output files (O(batch_size) memory)
    // =====================================================================
    // Re-scan the file, writing sidecars and TSV on the fly.

    u32 records_per_part = (total_records + num_parts - 1) / num_parts;
    u32 records_written = 0;

    // Open sidecar files if needed
    FILE* fp_plus = nullptr;
    FILE* fp_wrap = nullptr;
    if (plus_mode == PLUS_STORED) {
        std::string plus_path = std::string(output_dir) + "/plus.bin";
        fp_plus = fopen(plus_path.c_str(), "wb");
        if (!fp_plus) { fprintf(stderr, "Cannot open plus.bin\n"); return 1; }
    }
    if (csv_wrap_mode == CSV_WRAP_STORED) {
        std::string wrap_path = std::string(output_dir) + "/wrap.bin";
        fp_wrap = fopen(wrap_path.c_str(), "wb");
        if (!fp_wrap) { fprintf(stderr, "Cannot open wrap.bin\n"); return 1; }
    }

    // Open first TSV part
    int current_part = 0;
    char part_path[512];
    snprintf(part_path, sizeof(part_path), "%s/part_%03d.tsv", output_dir, current_part);
    FILE* fp_tsv = fopen(part_path, "wb");
    if (!fp_tsv) { fprintf(stderr, "Cannot open %s\n", part_path); return 1; }
    u32 part_start = 0;
    u32 part_records = 0;
    size_t part_bytes = 0;

    // Streaming write buffer (reused across batches)
    std::string tsv_buf;
    tsv_buf.reserve(16 * 1024 * 1024);  // 16 MiB write buffer

    size_t pass2_pos = 0;
    std::vector<FastqRecord> batch;
    while (true) {
        u32 bc = parse_batch(mf.data, mf.size, pass2_pos, batch, BATCH_SIZE);
        if (bc == 0) break;

        tsv_buf.clear();

        for (auto& rec : batch) {
            // Write sidecar data for this record
            if (fp_plus) {
                fwrite(rec.plus_comment.data(), 1, rec.plus_comment.size(), fp_plus);
                fputc('\n', fp_plus);
            }
            if (fp_wrap) {
                u16 num_seq_lines = static_cast<u16>(rec.seq_line_lengths.size());
                u16 num_qual_lines = static_cast<u16>(rec.qual_line_lengths.size());
                fwrite(&num_seq_lines, 2, 1, fp_wrap);
                for (auto ll : rec.seq_line_lengths) {
                    u16 v = static_cast<u16>(ll);
                    fwrite(&v, 2, 1, fp_wrap);
                }
                fwrite(&num_qual_lines, 2, 1, fp_wrap);
                for (auto ll : rec.qual_line_lengths) {
                    u16 v = static_cast<u16>(ll);
                    fwrite(&v, 2, 1, fp_wrap);
                }
            }

            // Build TSV line
            if (all_illumina) {
                auto illum = parse_illumina_header(rec.header);

                if (!illum_result.instrument_constant) {
                    auto it_i = instr_map.find(illum.instrument);
                    tsv_buf += std::to_string(it_i->second);
                    tsv_buf += '\t';
                }

                auto it_r = run_map.find(illum.run);
                tsv_buf += std::to_string(it_r->second);
                tsv_buf += '\t';
                auto it_f = fc_map.find(illum.flowcell);
                tsv_buf += std::to_string(it_f->second);
                tsv_buf += '\t';
                auto it_l = lane_map.find(illum.lane);
                tsv_buf += std::to_string(it_l->second);
                tsv_buf += '\t';
                auto it_t = tile_map.find(illum.tile);
                tsv_buf += std::to_string(it_t->second);
                tsv_buf += '\t';
                tsv_buf += illum.x;
                tsv_buf += '\t';
                tsv_buf += illum.y;
                tsv_buf += '\t';
                tsv_buf += rec.raw_seq;
                tsv_buf += '\t';
                tsv_buf += rec.raw_qual;
                tsv_buf += '\n';
            } else {
                tsv_buf += rec.header;
                tsv_buf += '\t';
                tsv_buf += rec.raw_seq;
                tsv_buf += '\t';
                tsv_buf += rec.raw_qual;
                tsv_buf += '\n';
            }

            records_written++;
            part_records++;

            // Check if we need to switch to next part
            if (part_records >= records_per_part && current_part < num_parts - 1) {
                // Flush current buffer to current part
                if (!tsv_buf.empty()) {
                    fwrite(tsv_buf.data(), 1, tsv_buf.size(), fp_tsv);
                    part_bytes += tsv_buf.size();
                    tsv_buf.clear();
                }
                fprintf(stderr, "Part %d: %u records, %zu bytes -> %s\n",
                        current_part, part_records, part_bytes, part_path);
                fclose(fp_tsv);

                // Open next part
                current_part++;
                snprintf(part_path, sizeof(part_path), "%s/part_%03d.tsv", output_dir, current_part);
                fp_tsv = fopen(part_path, "wb");
                if (!fp_tsv) { fprintf(stderr, "Cannot open %s\n", part_path); return 1; }
                part_start = records_written;
                part_records = 0;
                part_bytes = 0;
            }
        }

        // Flush batch buffer to current part
        if (!tsv_buf.empty()) {
            fwrite(tsv_buf.data(), 1, tsv_buf.size(), fp_tsv);
            part_bytes += tsv_buf.size();
        }
    }

    // Close final part
    fprintf(stderr, "Part %d: %u records, %zu bytes -> %s\n",
            current_part, part_records, part_bytes, part_path);
    fclose(fp_tsv);

    if (fp_plus) fclose(fp_plus);
    if (fp_wrap) fclose(fp_wrap);

    fprintf(stderr, "CSV encode complete: %u records, %d part(s)\n", records_written, num_parts);
    return 0;
}

// ============================================================================
// DECODE-CSV — reconstruct FASTQ from meta.bin + part_NNN.tsv files
// ============================================================================

static int do_decode_csv(const char* csv_dir, const char* output_path) {
    std::string meta_path = std::string(csv_dir) + "/meta.bin";
    FILE* fmeta = fopen(meta_path.c_str(), "rb");
    if (!fmeta) { fprintf(stderr, "Cannot open %s\n", meta_path.c_str()); return 1; }

    char magic[7];
    if (fread(magic, 1, 7, fmeta) != 7 || memcmp(magic, FQMETA1_MAGIC, 7) != 0) {
        fprintf(stderr, "Bad meta magic\n");
        fclose(fmeta);
        return 1;
    }

    u32 num_records;
    if (fread(&num_records, 4, 1, fmeta) != 1) { fclose(fmeta); return 1; }

    u8 flags;
    if (fread(&flags, 1, 1, fmeta) != 1) { fclose(fmeta); return 1; }

    bool all_illumina        = flags & CSVF_ALL_ILLUMINA;
    bool prefix_constant     = flags & CSVF_PREFIX_CONST;
    bool instrument_constant = flags & CSVF_INSTRUMENT_CONST;
    bool readnum_sequential  = flags & CSVF_READNUM_SEQUENTIAL;
    bool pair_suffix_const   = flags & CSVF_PAIR_SUFFIX_CONST;
    bool has_trailing_nl     = flags & CSVF_HAS_TRAILING_NL;
    bool newline_crlf        = flags & CSVF_NEWLINE_CRLF;

    u8 plus_mode, csv_wrap_mode;
    if (fread(&plus_mode, 1, 1, fmeta) != 1) { fclose(fmeta); return 1; }
    if (fread(&csv_wrap_mode, 1, 1, fmeta) != 1) { fclose(fmeta); return 1; }

    auto read_meta_str = [&]() -> std::string {
        int16_t len;
        if (fread(&len, 2, 1, fmeta) != 1) return "";
        std::string s(len, '\0');
        if (len > 0 && fread(&s[0], 1, len, fmeta) != (size_t)len) return "";
        return s;
    };

    std::string constant_prefix, constant_instrument, constant_pair_suffix;
    if (prefix_constant) constant_prefix = read_meta_str();
    if (instrument_constant) constant_instrument = read_meta_str();
    if (pair_suffix_const) constant_pair_suffix = read_meta_str();

    auto read_meta_dict = [&]() -> std::vector<std::string> {
        int32_t dsize;
        if (fread(&dsize, 4, 1, fmeta) != 1) return {};
        std::vector<std::string> dict(dsize);
        for (int i = 0; i < dsize; i++) dict[i] = read_meta_str();
        return dict;
    };

    std::vector<std::string> instr_dict, run_dict, fc_dict, lane_dict, tile_dict;
    if (all_illumina) {
        if (!instrument_constant) instr_dict = read_meta_dict();
        run_dict = read_meta_dict();
        fc_dict = read_meta_dict();
        lane_dict = read_meta_dict();
        tile_dict = read_meta_dict();
    }

    u32 constant_wrap_width = 0;
    if (csv_wrap_mode == CSV_WRAP_CONSTANT) {
        if (fread(&constant_wrap_width, 4, 1, fmeta) != 1) { fclose(fmeta); return 1; }
    }

    u32 marker;
    if (fread(&marker, 4, 1, fmeta) != 1 || marker != 0xDEADBEEF) {
        fprintf(stderr, "Bad meta marker\n");
        fclose(fmeta);
        return 1;
    }
    fclose(fmeta);

    const char* nl = newline_crlf ? "\r\n" : "\n";
    int nl_len = newline_crlf ? 2 : 1;

    // Read plus.bin if needed
    std::vector<std::string> plus_comments;
    if (plus_mode == PLUS_STORED) {
        std::string plus_path = std::string(csv_dir) + "/plus.bin";
        MappedFile pmf;
        if (!pmf.open(plus_path.c_str())) return 1;
        const char* d = pmf.data;
        size_t sz = pmf.size;
        size_t pos = 0;
        while (pos < sz) {
            size_t start = pos;
            while (pos < sz && d[pos] != '\n') pos++;
            plus_comments.push_back(std::string(d + start, pos - start));
            if (pos < sz) pos++; // skip \n
        }
    }

    // Read wrap.bin if needed
    struct WrapInfo { std::vector<u16> seq_ll; std::vector<u16> qual_ll; };
    std::vector<WrapInfo> wrap_infos;
    if (csv_wrap_mode == CSV_WRAP_STORED) {
        std::string wrap_path = std::string(csv_dir) + "/wrap.bin";
        auto wrap_data = read_file_bytes(wrap_path);
        const u8* wp = wrap_data.data();
        const u8* wend = wp + wrap_data.size();
        while (wp + 2 <= wend) {
            WrapInfo wi;
            u16 nseq; memcpy(&nseq, wp, 2); wp += 2;
            for (u16 j = 0; j < nseq && wp + 2 <= wend; j++) {
                u16 v; memcpy(&v, wp, 2); wp += 2;
                wi.seq_ll.push_back(v);
            }
            if (wp + 2 > wend) break;
            u16 nqual; memcpy(&nqual, wp, 2); wp += 2;
            for (u16 j = 0; j < nqual && wp + 2 <= wend; j++) {
                u16 v; memcpy(&v, wp, 2); wp += 2;
                wi.qual_ll.push_back(v);
            }
            wrap_infos.push_back(std::move(wi));
        }
    }

    // Discover part_*.tsv files
    std::vector<std::string> part_paths;
    {
        char path_buf[512];
        for (int i = 0; i < 1000; i++) {
            snprintf(path_buf, sizeof(path_buf), "%s/part_%03d.tsv", csv_dir, i);
            struct stat st;
            if (stat(path_buf, &st) == 0) {
                part_paths.push_back(path_buf);
            } else {
                break;
            }
        }
    }
    if (part_paths.empty()) {
        fprintf(stderr, "No part_*.tsv files found in %s\n", csv_dir);
        return 1;
    }

    // Helper to write a string with wrapping
    auto write_wrapped = [&](FILE* fp, const char* data, int len,
                              const std::vector<u16>* wrap_ll, u32 const_width) {
        if (wrap_ll && !wrap_ll->empty()) {
            // Per-record or constant wrapping
            int pos = 0;
            for (size_t k = 0; k < wrap_ll->size(); k++) {
                int line_len = (*wrap_ll)[k];
                if (line_len > len - pos) line_len = len - pos;
                fwrite(data + pos, 1, line_len, fp);
                pos += line_len;
                if (k + 1 < wrap_ll->size()) fwrite(nl, 1, nl_len, fp);
            }
        } else if (const_width > 0 && (u32)len > const_width) {
            // Constant-width wrapping
            int pos = 0;
            while (pos < len) {
                int chunk = std::min((int)const_width, len - pos);
                fwrite(data + pos, 1, chunk, fp);
                pos += chunk;
                if (pos < len) fwrite(nl, 1, nl_len, fp);
            }
        } else {
            fwrite(data, 1, len, fp);
        }
    };

    FILE* out = fopen(output_path, "wb");
    if (!out) { fprintf(stderr, "Cannot open %s\n", output_path); return 1; }

    u32 total_decoded = 0;
    auto to_int = [](const char* s, int len) -> int {
        int v = 0;
        for (int i = 0; i < len; i++) v = v * 10 + (s[i] - '0');
        return v;
    };

    for (auto& part_path : part_paths) {
        MappedFile pmf;
        if (!pmf.open(part_path.c_str())) { fclose(out); return 1; }
        if (pmf.size == 0) continue;

        const char* d = pmf.data;
        size_t fsize = pmf.size;
        size_t lpos = 0;

        while (lpos < fsize) {
            size_t line_start = lpos;
            while (lpos < fsize && d[lpos] != '\n') lpos++;
            size_t line_end = lpos;
            if (line_end > line_start && d[line_end - 1] == '\r') line_end--;
            if (lpos < fsize) lpos++;

            if (line_end == line_start) continue;

            const char* line = d + line_start;
            int line_len = (int)(line_end - line_start);

            int tab_positions[12];
            int num_tabs = 0;
            for (int j = 0; j < line_len && num_tabs < 12; j++) {
                if (line[j] == '\t') tab_positions[num_tabs++] = j;
            }

            auto field = [&](int idx) -> std::pair<const char*, int> {
                int start = (idx == 0) ? 0 : tab_positions[idx - 1] + 1;
                int end = (idx < num_tabs) ? tab_positions[idx] : line_len;
                return {line + start, end - start};
            };

            int64_t read_num = (int64_t)(total_decoded + 1);

            // Determine wrap info for this record
            const std::vector<u16>* seq_wrap_ll = nullptr;
            const std::vector<u16>* qual_wrap_ll = nullptr;
            u32 wrap_w = 0;
            if (csv_wrap_mode == CSV_WRAP_STORED && total_decoded < wrap_infos.size()) {
                seq_wrap_ll = &wrap_infos[total_decoded].seq_ll;
                qual_wrap_ll = &wrap_infos[total_decoded].qual_ll;
            } else if (csv_wrap_mode == CSV_WRAP_CONSTANT) {
                wrap_w = constant_wrap_width;
            }

            if (all_illumina) {
                int col = 0;
                std::string instrument_str;
                if (!instrument_constant) {
                    auto [is, il] = field(col++);
                    int iid = to_int(is, il);
                    instrument_str = instr_dict[iid];
                }
                auto [r_s, r_l] = field(col++);
                auto [f_s, f_l] = field(col++);
                auto [l_s, l_l] = field(col++);
                auto [t_s, t_l] = field(col++);
                auto [x_s, x_l] = field(col++);
                auto [y_s, y_l] = field(col++);
                auto [seq_s, seq_l] = field(col++);
                auto [qual_s, qual_l] = field(col++);

                int rid = to_int(r_s, r_l);
                int fid = to_int(f_s, f_l);
                int lid = to_int(l_s, l_l);
                int tid = to_int(t_s, t_l);

                fputc('@', out);
                if (prefix_constant)
                    fwrite(constant_prefix.data(), 1, constant_prefix.size(), out);
                fprintf(out, ".%lld ", (long long)read_num);
                if (instrument_constant)
                    fwrite(constant_instrument.data(), 1, constant_instrument.size(), out);
                else
                    fwrite(instrument_str.data(), 1, instrument_str.size(), out);
                fputc(':', out);
                fwrite(run_dict[rid].data(), 1, run_dict[rid].size(), out);
                fputc(':', out);
                fwrite(fc_dict[fid].data(), 1, fc_dict[fid].size(), out);
                fputc(':', out);
                fwrite(lane_dict[lid].data(), 1, lane_dict[lid].size(), out);
                fputc(':', out);
                fwrite(tile_dict[tid].data(), 1, tile_dict[tid].size(), out);
                fputc(':', out);
                fwrite(x_s, 1, x_l, out);
                fputc(':', out);
                fwrite(y_s, 1, y_l, out);
                if (pair_suffix_const)
                    fwrite(constant_pair_suffix.data(), 1, constant_pair_suffix.size(), out);
                fwrite(nl, 1, nl_len, out);

                write_wrapped(out, seq_s, seq_l, seq_wrap_ll, wrap_w);
                fwrite(nl, 1, nl_len, out);

                // Plus line
                fputc('+', out);
                if (plus_mode == PLUS_HEADER_COPY) {
                    // Reconstruct from header
                    if (prefix_constant)
                        fwrite(constant_prefix.data(), 1, constant_prefix.size(), out);
                    fprintf(out, ".%lld ", (long long)read_num);
                    if (instrument_constant)
                        fwrite(constant_instrument.data(), 1, constant_instrument.size(), out);
                    else
                        fwrite(instrument_str.data(), 1, instrument_str.size(), out);
                    fputc(':', out);
                    fwrite(run_dict[rid].data(), 1, run_dict[rid].size(), out);
                    fputc(':', out);
                    fwrite(fc_dict[fid].data(), 1, fc_dict[fid].size(), out);
                    fputc(':', out);
                    fwrite(lane_dict[lid].data(), 1, lane_dict[lid].size(), out);
                    fputc(':', out);
                    fwrite(tile_dict[tid].data(), 1, tile_dict[tid].size(), out);
                    fputc(':', out);
                    fwrite(x_s, 1, x_l, out);
                    fputc(':', out);
                    fwrite(y_s, 1, y_l, out);
                    if (pair_suffix_const)
                        fwrite(constant_pair_suffix.data(), 1, constant_pair_suffix.size(), out);
                } else if (plus_mode == PLUS_STORED && total_decoded < plus_comments.size()) {
                    fwrite(plus_comments[total_decoded].data(), 1,
                           plus_comments[total_decoded].size(), out);
                }
                fwrite(nl, 1, nl_len, out);

                write_wrapped(out, qual_s, qual_l, qual_wrap_ll, wrap_w);
            } else {
                // Generic: 3 columns
                auto [hdr_s, hdr_l] = field(0);
                auto [seq_s, seq_l] = field(1);
                auto [qual_s, qual_l] = field(2);

                fputc('@', out);
                fwrite(hdr_s, 1, hdr_l, out);
                fwrite(nl, 1, nl_len, out);

                write_wrapped(out, seq_s, seq_l, seq_wrap_ll, wrap_w);
                fwrite(nl, 1, nl_len, out);

                // Plus line
                fputc('+', out);
                if (plus_mode == PLUS_HEADER_COPY) {
                    fwrite(hdr_s, 1, hdr_l, out);
                } else if (plus_mode == PLUS_STORED && total_decoded < plus_comments.size()) {
                    fwrite(plus_comments[total_decoded].data(), 1,
                           plus_comments[total_decoded].size(), out);
                }
                fwrite(nl, 1, nl_len, out);

                write_wrapped(out, qual_s, qual_l, qual_wrap_ll, wrap_w);
            }

            total_decoded++;
            if (total_decoded < num_records || has_trailing_nl) {
                fwrite(nl, 1, nl_len, out);
            }
        }
    }

    fclose(out);
    fprintf(stderr, "CSV decode complete: %u records\n", total_decoded);
    return 0;
}

// ============================================================================
// main
// ============================================================================

static void usage() {
    fprintf(stderr,
        "Usage:\n"
        "  fastq_codec encode         <input.fastq> <output_dir> [num_threads]\n"
        "  fastq_codec decode         <streams_dir> <output.fastq>\n"
        "  fastq_codec encode-packed  <input.fastq> <output_dir> [num_chunks]\n"
        "  fastq_codec decode-packed  <packed_dir>  <output.fastq>\n"
        "  fastq_codec encode-csv     <input.fastq> <output_dir> [num_parts]\n"
        "  fastq_codec decode-csv     <csv_dir>     <output.fastq>\n"
        "  fastq_codec validate       <streams_dir>\n");
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
    } else if (cmd == "encode-packed") {
        if (argc < 4 || argc > 5) {
            fprintf(stderr, "encode-packed: <input.fastq> <output_dir> [num_chunks]\n");
            return 1;
        }
        int chunks = 1;
        if (argc == 5) chunks = std::max(1, atoi(argv[4]));
        return do_encode_packed(argv[2], argv[3], chunks);
    } else if (cmd == "decode-packed") {
        if (argc != 4) {
            fprintf(stderr, "decode-packed: <packed_dir> <output.fastq>\n");
            return 1;
        }
        return do_decode_packed(argv[2], argv[3]);
    } else if (cmd == "encode-csv") {
        if (argc < 4 || argc > 5) {
            fprintf(stderr, "encode-csv: <input.fastq> <output_dir> [num_parts]\n");
            return 1;
        }
        int parts = 1;
        if (argc == 5) parts = std::max(1, atoi(argv[4]));
        return do_encode_csv(argv[2], argv[3], parts);
    } else if (cmd == "decode-csv") {
        if (argc != 4) {
            fprintf(stderr, "decode-csv: <csv_dir> <output.fastq>\n");
            return 1;
        }
        return do_decode_csv(argv[2], argv[3]);
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
