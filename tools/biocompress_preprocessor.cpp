#include <iostream>
#include <fstream>
#include <vector>
#include <string>
#include <thread>
#include <mutex>
#include <atomic>
#include <algorithm>
#include <cstring>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <filesystem>
#include <map>
#include <set>

namespace fs = std::filesystem;

// ============================================================================
// Utilities
// ============================================================================

struct MappedFile {
    const char* data;
    size_t size;
    int fd;

    MappedFile(const std::string& path) {
        fd = open(path.c_str(), O_RDONLY);
        if (fd == -1) {
            throw std::runtime_error("Could not open file: " + path);
        }
        struct stat sb;
        if (fstat(fd, &sb) == -1) {
            close(fd);
            throw std::runtime_error("Could not stat file: " + path);
        }
        size = sb.st_size;
        data = (const char*)mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (data == MAP_FAILED) {
            close(fd);
            throw std::runtime_error("mmap failed");
        }
        // Advise kernel we will read sequentially
        madvise((void*)data, size, MADV_SEQUENTIAL);
    }

    ~MappedFile() {
        if (data != MAP_FAILED) {
            munmap((void*)data, size);
        }
        if (fd != -1) {
            close(fd);
        }
    }
};

void write_u32(std::ofstream& out, uint32_t val) {
    out.write(reinterpret_cast<const char*>(&val), 4);
}

void pad_stream(std::ofstream& out, size_t size, size_t align = 4) {
    size_t rem = size % align;
    if (rem != 0) {
        size_t pad = align - rem;
        static const char zeros[16] = {0};
        out.write(zeros, pad);
    }
}

// Compute a common prefix across the first N FASTQ headers in the file.
// This is used to collapse the duplicated machine/run identifier.
std::string compute_common_prefix(const char* start, const char* end, size_t max_records = 10000) {
    std::string prefix;
    size_t cursor_idx = 0;
    const char* cursor = start;
    size_t found = 0;

    while (cursor < end && found < max_records) {
        // Find start of a header line (must start at file or after a newline and '@')
        const char* at = (const char*)memchr(cursor, '@', end - cursor);
        if (!at) break;
        // ensure '@' is at start of line
        if (at != start && *(at-1) != '\n') {
            cursor = at + 1;
            continue;
        }

        const char* line_end = (const char*)memchr(at, '\n', end - at);
        if (!line_end) break;

        std::string hdr(at, line_end - at);
        if (found == 0) {
            prefix = hdr;
        } else {
            // truncate prefix to match hdr
            size_t i = 0;
            size_t lim = std::min(prefix.size(), hdr.size());
            while (i < lim && prefix[i] == hdr[i]) ++i;
            prefix.resize(i);
            if (prefix.empty()) break;
        }

        found++;
        cursor = line_end + 1;
    }

    return prefix;
}

// Global extracted common prefix (filled in main before threading)
static std::string GLOBAL_COMMON_PREFIX;

// ============================================================================
// FASTQ Processor
// ============================================================================

void process_fastq_chunk(const char* start, const char* end, const std::string& output_path) {
    std::vector<uint32_t> hdr_offsets = {0};
    std::vector<uint32_t> seq_offsets = {0};
    std::vector<uint32_t> qual_offsets = {0};
    
    std::vector<char> hdrs;
    std::vector<char> seqs;
    std::vector<char> quals;
    
    // Reserve some memory to avoid reallocations (heuristic)
    size_t estimated_records = (end - start) / 300; // Rough guess
    hdr_offsets.reserve(estimated_records);
    seq_offsets.reserve(estimated_records);
    qual_offsets.reserve(estimated_records);

    const char* cursor = start;
    while (cursor < end) {
        // Line 1: Header (@...)
        const char* line1_end = (const char*)memchr(cursor, '\n', end - cursor);
        if (!line1_end) break;
        
        // Line 2: Sequence
        const char* line2_start = line1_end + 1;
        if (line2_start >= end) break;
        const char* line2_end = (const char*)memchr(line2_start, '\n', end - line2_start);
        if (!line2_end) break;
        
        // Line 3: Plus (+)
        const char* line3_start = line2_end + 1;
        if (line3_start >= end) break;
        const char* line3_end = (const char*)memchr(line3_start, '\n', end - line3_start);
        if (!line3_end) break;
        
        // Line 4: Quality
        const char* line4_start = line3_end + 1;
        if (line4_start >= end) break;
        const char* line4_end = (const char*)memchr(line4_start, '\n', end - line4_start);
        if (!line4_end) break; // Or handle last line without newline?
        
        // Append Data (stripping newlines)
        hdrs.insert(hdrs.end(), cursor, line1_end);
        seqs.insert(seqs.end(), line2_start, line2_end);
        quals.insert(quals.end(), line4_start, line4_end);
        
        hdr_offsets.push_back(hdrs.size());
        seq_offsets.push_back(seqs.size());
        qual_offsets.push_back(quals.size());
        
        cursor = line4_end + 1;
    }
    
    // Write Binary
    std::ofstream out(output_path, std::ios::binary);
    out.write("FQV3", 4);
    
    uint32_t num_records = hdr_offsets.size() - 1;
    write_u32(out, num_records);
    
    out.write((const char*)hdr_offsets.data(), hdr_offsets.size() * 4);
    out.write((const char*)seq_offsets.data(), seq_offsets.size() * 4);
    out.write((const char*)qual_offsets.data(), qual_offsets.size() * 4);
    
    uint32_t hdr_total = hdrs.size();
    uint32_t seq_total = seqs.size();
    uint32_t qual_total = quals.size();
    
    write_u32(out, hdr_total);
    write_u32(out, seq_total);
    write_u32(out, qual_total);
    
    uint32_t hdr_pad = (4 - (hdr_total % 4)) % 4;
    uint32_t seq_pad = (12 - (seq_total % 12)) % 12; // Matching python script
    uint32_t qual_pad = (12 - (qual_total % 12)) % 12;
    
    write_u32(out, hdr_pad);
    write_u32(out, seq_pad);
    write_u32(out, qual_pad);
    
    out.write(hdrs.data(), hdrs.size());
    pad_stream(out, hdrs.size(), 4);
    
    out.write(seqs.data(), seqs.size());
    pad_stream(out, seqs.size(), 12);
    
    out.write(quals.data(), quals.size());
    pad_stream(out, quals.size(), 12);
    
    out.close();
}

// ============================================================================
// FASTQ V4 Processor (Header Dedup + 4-bit Packing)
// ============================================================================

// Helper to pack base char to 4-bit value (0..4 where 4==N)
inline uint8_t pack_base(char c) {
    switch(c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        case 'N': case 'n': return 4;
        default: return 4; // Treat unknown as N
    }
}

void process_fastq_v4_chunk(const char* start, const char* end, const std::string& output_path) {
    std::vector<uint16_t> len_bytes; // original read lengths (bases)
    std::vector<char> suffixes; // header suffixes, null-terminated per record
    std::vector<char> seqs; // packed: 4-bit per base, 2 bases per byte
    std::vector<char> quals; // raw quality bytes

    // Reserve memory
    size_t estimated_records = (end - start) / 300;
    len_bytes.reserve(estimated_records);
    suffixes.reserve(estimated_records * 16);
    seqs.reserve((end - start) / 2);
    quals.reserve(end - start);

    const char* cursor = start;
    while (cursor < end) {
        // Line 1: Header
        const char* l1_end = (const char*)memchr(cursor, '\n', end - cursor);
        if (!l1_end) break;

        // Line 2: Sequence
        const char* l2_start = l1_end + 1;
        if (l2_start >= end) break;
        const char* l2_end = (const char*)memchr(l2_start, '\n', end - l2_start);
        if (!l2_end) break;

        // Line 3: Plus
        const char* l3_start = l2_end + 1;
        if (l3_start >= end) break;
        const char* l3_end = (const char*)memchr(l3_start, '\n', end - l3_start);
        if (!l3_end) break;

        // Line 4: Quality
        const char* l4_start = l3_end + 1;
        if (l4_start >= end) break;
        const char* l4_end = (const char*)memchr(l4_start, '\n', end - l4_start);
        if (!l4_end) break;

        // Header suffix (collapse GLOBAL_COMMON_PREFIX if present)
        size_t hdr_len = l1_end - cursor;
        std::string hdr(cursor, hdr_len);
        size_t pre_len = GLOBAL_COMMON_PREFIX.size();
        std::string suffix;
        if (pre_len > 0 && hdr.size() >= pre_len && hdr.compare(0, pre_len, GLOBAL_COMMON_PREFIX) == 0) {
            suffix = hdr.substr(pre_len);
        } else {
            suffix = hdr; // no common prefix match
        }
        // store suffix + null terminator so we can split later
        suffixes.insert(suffixes.end(), suffix.begin(), suffix.end());
        suffixes.push_back('\0');

        // Sequence: pack 2 bases per byte (4-bit each)
        size_t s_len = l2_end - l2_start;
        for (size_t i = 0; i < s_len; i += 2) {
            uint8_t high = pack_base(l2_start[i]) & 0xF;
            uint8_t low = (i + 1 < s_len) ? (pack_base(l2_start[i+1]) & 0xF) : 0;
            uint8_t b = (high << 4) | low;
            seqs.push_back((char)b);
        }

        // Qualities: store raw (we can try RLE/delta later)
        size_t q_len = l4_end - l4_start;
        quals.insert(quals.end(), l4_start, l4_end);

        // Record original length (number of bases)
        len_bytes.push_back((uint16_t)s_len);

        cursor = l4_end + 1;
    }

    // Write Binary FQV4
    std::ofstream out(output_path, std::ios::binary);
    out.write("FQV4", 4);

    uint32_t num_records = len_bytes.size();
    write_u32(out, num_records);

    // Write common prefix
    uint16_t prefix_len = GLOBAL_COMMON_PREFIX.size();
    out.write(reinterpret_cast<const char*>(&prefix_len), 2);
    if (prefix_len > 0) out.write(GLOBAL_COMMON_PREFIX.data(), prefix_len);

    // Write Lengths (U16)
    if (!len_bytes.empty()) out.write((const char*)len_bytes.data(), len_bytes.size() * 2);

    // Write Totals
    uint32_t suffix_total = suffixes.size();
    uint32_t seq_total = seqs.size();
    uint32_t qual_total = quals.size();

    // Pad seqs to 4-byte alignment and suffix/quals to 4-byte alignment (safer for OpenZL)
    uint32_t seq_pad = (4 - (seq_total % 4)) % 4;
    for (uint32_t i = 0; i < seq_pad; ++i) seqs.push_back('\0');
    seq_total += seq_pad;

    uint32_t suffix_pad = (4 - (suffix_total % 4)) % 4;
    for (uint32_t i = 0; i < suffix_pad; ++i) suffixes.push_back('\0');
    suffix_total += suffix_pad;

    uint32_t qual_pad = (4 - (qual_total % 4)) % 4;
    for (uint32_t i = 0; i < qual_pad; ++i) quals.push_back('\0');
    qual_total += qual_pad;

    write_u32(out, suffix_total);
    write_u32(out, seq_total);
    write_u32(out, qual_total);

    // Write Data Streams
    if (suffix_total > 0) out.write(suffixes.data(), suffix_total);
    if (seq_total > 0) out.write(seqs.data(), seq_total);
    if (qual_total > 0) out.write(quals.data(), qual_total);

    out.close();
}

// ============================================================================
// FASTA Processor (FAV3 - existing logic)
// ============================================================================

void process_fasta_chunk(const char* start, const char* end, const std::string& output_path) {
    std::vector<uint32_t> hdr_offsets = {0};
    std::vector<uint32_t> seq_offsets = {0};
    
    std::vector<char> hdrs;
    std::vector<char> seqs;
    
    int32_t line_width = -1;
    
    const char* cursor = start;
    
    // If we start in the middle of a sequence, we shouldn't. 
    // The splitter guarantees we start at '>'
    
    while (cursor < end) {
        if (*cursor == '>') {
            // Header line
            const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
            if (!line_end) line_end = end;
            
            // Skip '>'
            hdrs.insert(hdrs.end(), cursor + 1, line_end);
            hdr_offsets.push_back(hdrs.size());
            
            // Prepare for sequence
            // If this is not the first record, push the previous sequence length
            if (hdr_offsets.size() > 2) { // >2 because we pushed one just now, and initialized with 0
                 // Wait, logic:
                 // hdr_offsets = [0]
                 // Found >rec1. hdrs=[rec1], hdr_offsets=[0, 4]
                 // Found >rec2. We need to finish rec1 sequence.
                 // But we are accumulating sequence bytes continuously.
                 // So we push seq_offsets ONLY when we hit a new header OR end.
                 seq_offsets.push_back(seqs.size());
            }
            
            cursor = line_end + 1;
        } else {
            // Sequence line
            const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
            if (!line_end) line_end = end;
            
            size_t len = line_end - cursor;
            if (line_width == -1 && len > 0) line_width = len;
            
            seqs.insert(seqs.end(), cursor, line_end);
            cursor = line_end + 1;
        }
    }
    // Finish last record
    if (hdr_offsets.size() > 1) {
        seq_offsets.push_back(seqs.size());
    }
    
    // Write Binary
    std::ofstream out(output_path, std::ios::binary);
    out.write("FAV3", 4);
    
    uint32_t num_records = hdr_offsets.size() - 1;
    write_u32(out, num_records);
    write_u32(out, (line_width == -1) ? 0 : line_width);
    
    out.write((const char*)hdr_offsets.data(), hdr_offsets.size() * 4);
    out.write((const char*)seq_offsets.data(), seq_offsets.size() * 4);
    
    uint32_t hdr_total = hdrs.size();
    uint32_t seq_total = seqs.size();
    
    write_u32(out, hdr_total);
    write_u32(out, seq_total);
    
    uint32_t hdr_pad = (4 - (hdr_total % 4)) % 4;
    uint32_t seq_pad = (4 - (seq_total % 4)) % 4;
    
    write_u32(out, hdr_pad);
    write_u32(out, seq_pad);
    
    out.write(hdrs.data(), hdrs.size());
    pad_stream(out, hdrs.size(), 4);
    
    out.write(seqs.data(), seqs.size());
    pad_stream(out, seqs.size(), 4);
    
    out.close();
}

// ============================================================================
// FASTA Packed Processor (FAV5 - LOSSLESS)
// ----------------------------------------------------------------------------
// FAV4 was lossy: it case-folded (a->A), collapsed every non-ACGTN byte to N,
// dropped '\r', and stored no line layout, so the original FASTA could not be
// reconstructed. FAV5 is byte-exact. Each chunk decodes to its exact input
// range (chunks are cut on record boundaries, so concatenating the decoded
// chunks in index order reproduces the file). Reconstruction: tools/fasta_postprocess.
//
// Container (little-endian, no padding). All RLE arrays are length-prefixed by
// the header counts. Exceptions (N runs, IUPAC codes, '\r', gaps, ...) are
// run-length encoded so long N stretches cost ~24 bytes each, not O(len).
//   "FAV5"  magic
//   u8   flags            bit0 = this range ends with '\n'
//   u8[3] reserved
//   u32  preamble_len
//   u32  num_records
//   u64  n_seqpos         total sequence positions (bases + exception bytes)
//   u64  n_base           positions whose byte is [ACGTacgt]
//   u64  n_caseruns       upper/lower RLE over base positions (first run = upper)
//   u64  n_excruns        exception runs
//   u64  n_linelens       == sum(rec_nlines)
//   u64  hdr_bytes        == sum(hdr_lens)
//   Byte[preamble_len]                preamble
//   u32[num_records]                  hdr_lens    (header text after '>', excl '\n')
//   u32[num_records]                  rec_nlines
//   u32[n_linelens]                   line_lens   (raw bytes per sequence line, no '\n')
//   u64[n_caseruns]                   case_runs
//   u64[n_excruns]                    exc_gaps    (base+exc positions since prev run end)
//   u64[n_excruns]                    exc_lens
//   Byte[n_excruns]                   exc_bytes   (the repeated literal byte)
//   Byte[ceil(n_base/4)]              packed2bit  (A=0 C=1 G=2 T=3, low bits first)
//   Byte[hdr_bytes]                   headers
// ============================================================================

static inline int base_code(char c) {
    switch (c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return -1;
    }
}
static inline bool is_lower_base(char c) { return c >= 'a' && c <= 'z'; }

template <class T>
static void put_le(std::vector<uint8_t>& b, T v) {
    for (size_t i = 0; i < sizeof(T); i++) b.push_back(static_cast<uint8_t>((v >> (8 * i)) & 0xFF));
}

void process_fasta_packed_chunk(const char* start, const char* end, const std::string& output_path) {
    std::vector<uint8_t>  headers, exc_bytes, packed2bit;
    std::vector<uint32_t> hdr_lens, rec_nlines, line_lens;
    std::vector<uint64_t> case_runs, exc_gaps, exc_lens;

    // case RLE
    bool have_case = false, cur_lower = false;
    uint64_t cur_run = 0;
    auto case_push = [&](bool lower) {
        if (!have_case) { if (lower) case_runs.push_back(0); have_case = true; cur_lower = lower; cur_run = 1; return; }
        if (lower == cur_lower) cur_run++;
        else { case_runs.push_back(cur_run); cur_lower = lower; cur_run = 1; }
    };
    // exception RLE
    bool exc_active = false; uint8_t exc_byte = 0; uint64_t exc_start = 0, exc_len = 0, prev_exc_end = 0;
    auto exc_flush = [&]() {
        if (!exc_active) return;
        exc_gaps.push_back(exc_start - prev_exc_end);
        exc_lens.push_back(exc_len);
        exc_bytes.push_back(exc_byte);
        prev_exc_end = exc_start + exc_len;
        exc_active = false;
    };
    // 2-bit packer
    int pend_n = 0; uint8_t pend_byte = 0;
    auto pack_push = [&](int code) {
        pend_byte |= static_cast<uint8_t>((code & 3) << (2 * pend_n));
        if (++pend_n == 4) { packed2bit.push_back(pend_byte); pend_byte = 0; pend_n = 0; }
    };

    uint64_t seqpos = 0, n_base = 0;

    const char* first_gt = static_cast<const char*>(memchr(start, '>', end - start));
    const char* preamble_end = first_gt ? first_gt : end;
    std::vector<uint8_t> preamble(reinterpret_cast<const uint8_t*>(start),
                                 reinterpret_cast<const uint8_t*>(preamble_end));
    const char* cursor = preamble_end;

    while (cursor < end && *cursor == '>') {
        const char* nl = static_cast<const char*>(memchr(cursor, '\n', end - cursor));
        const char* hdr_end = nl ? nl : end;
        headers.insert(headers.end(), cursor + 1, hdr_end);
        hdr_lens.push_back(static_cast<uint32_t>(hdr_end - (cursor + 1)));
        cursor = nl ? nl + 1 : end;

        uint32_t nlines = 0;
        while (cursor < end && *cursor != '>') {
            const char* lnl = static_cast<const char*>(memchr(cursor, '\n', end - cursor));
            const char* line_end = lnl ? lnl : end;
            line_lens.push_back(static_cast<uint32_t>(line_end - cursor));
            for (const char* p = cursor; p < line_end; ++p) {
                int code = base_code(*p);
                if (code < 0) {
                    uint8_t c = static_cast<uint8_t>(*p);
                    if (exc_active && c == exc_byte && seqpos == exc_start + exc_len) exc_len++;
                    else { exc_flush(); exc_active = true; exc_byte = c; exc_start = seqpos; exc_len = 1; }
                } else {
                    case_push(is_lower_base(*p));
                    pack_push(code);
                    n_base++;
                }
                seqpos++;
            }
            nlines++;
            cursor = lnl ? lnl + 1 : end;
        }
        rec_nlines.push_back(nlines);
    }

    exc_flush();
    if (have_case) case_runs.push_back(cur_run);
    if (pend_n) packed2bit.push_back(pend_byte);

    std::vector<uint8_t> buf;
    buf.insert(buf.end(), {'F','A','V','5'});
    buf.push_back((end > start && end[-1] == '\n') ? 1u : 0u);
    buf.insert(buf.end(), {0,0,0});
    put_le<uint32_t>(buf, static_cast<uint32_t>(preamble.size()));
    put_le<uint32_t>(buf, static_cast<uint32_t>(hdr_lens.size()));
    put_le<uint64_t>(buf, seqpos);
    put_le<uint64_t>(buf, n_base);
    put_le<uint64_t>(buf, static_cast<uint64_t>(case_runs.size()));
    put_le<uint64_t>(buf, static_cast<uint64_t>(exc_gaps.size()));
    put_le<uint64_t>(buf, static_cast<uint64_t>(line_lens.size()));
    put_le<uint64_t>(buf, static_cast<uint64_t>(headers.size()));

    buf.insert(buf.end(), preamble.begin(), preamble.end());
    for (uint32_t v : hdr_lens)   put_le<uint32_t>(buf, v);
    for (uint32_t v : rec_nlines) put_le<uint32_t>(buf, v);
    for (uint32_t v : line_lens)  put_le<uint32_t>(buf, v);
    for (uint64_t v : case_runs)  put_le<uint64_t>(buf, v);
    for (uint64_t v : exc_gaps)   put_le<uint64_t>(buf, v);
    for (uint64_t v : exc_lens)   put_le<uint64_t>(buf, v);
    buf.insert(buf.end(), exc_bytes.begin(), exc_bytes.end());
    buf.insert(buf.end(), packed2bit.begin(), packed2bit.end());
    buf.insert(buf.end(), headers.begin(), headers.end());

    std::ofstream out(output_path, std::ios::binary);
    out.write(reinterpret_cast<const char*>(buf.data()), buf.size());
    out.close();
}

// ============================================================================
// VCF Processor (Simplified)
// ============================================================================

struct VcfHeaderInfo {
    std::string raw_header;
    std::map<std::string, uint32_t> chrom_map;
    std::map<std::string, uint32_t> filter_map;
};

VcfHeaderInfo parse_vcf_header(const char* start, const char* end) {
    VcfHeaderInfo info;
    const char* cursor = start;
    const char* header_end = start;
    
    while (cursor < end) {
        if (*cursor != '#') {
            header_end = cursor;
            break;
        }
        const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
        if (!line_end) break;
        
        std::string line(cursor, line_end - cursor);
        info.raw_header += line + "\n";
        
        if (line.rfind("##contig=<", 0) == 0) {
            size_t id_pos = line.find("ID=");
            if (id_pos != std::string::npos) {
                size_t end_pos = line.find_first_of(",>", id_pos);
                std::string id = line.substr(id_pos + 3, end_pos - (id_pos + 3));
                if (info.chrom_map.find(id) == info.chrom_map.end()) {
                    info.chrom_map[id] = info.chrom_map.size();
                }
            }
        } else if (line.rfind("##FILTER=<", 0) == 0) {
            size_t id_pos = line.find("ID=");
            if (id_pos != std::string::npos) {
                size_t end_pos = line.find_first_of(",>", id_pos);
                std::string id = line.substr(id_pos + 3, end_pos - (id_pos + 3));
                if (info.filter_map.find(id) == info.filter_map.end()) {
                    info.filter_map[id] = info.filter_map.size();
                }
            }
        }
        
        cursor = line_end + 1;
    }
    
    if (info.filter_map.find("PASS") == info.filter_map.end()) info.filter_map["PASS"] = info.filter_map.size();
    if (info.filter_map.find(".") == info.filter_map.end()) info.filter_map["."] = info.filter_map.size();
    
    return info;
}

void process_vcf_chunk(const char* start, const char* end, const std::string& output_path, const VcfHeaderInfo& header) {
    // Buffers
    std::vector<uint32_t> chrom_ids, pos_list, filter_ids;
    std::vector<char> id_data, ref_data, alt_data, qual_data, info_data, genotype_data;
    
    std::vector<uint32_t> id_offsets = {0}, ref_offsets = {0}, alt_offsets = {0}, 
                          qual_offsets = {0}, info_offsets = {0}, genotype_offsets = {0};
                          
    const char* cursor = start;
    while (cursor < end) {
        if (*cursor == '#') { // Skip header lines if they appear in chunk (shouldn't if split correctly)
             const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
             if (!line_end) break;
             cursor = line_end + 1;
             continue;
        }
        
        const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
        if (!line_end) line_end = end;
        
        // Parse columns (tab separated)
        // CHROM POS ID REF ALT QUAL FILTER INFO FORMAT...
        
        std::vector<std::string> cols;
        const char* col_start = cursor;
        while (col_start < line_end) {
            const char* col_end = (const char*)memchr(col_start, '\t', line_end - col_start);
            if (!col_end) col_end = line_end;
            cols.emplace_back(col_start, col_end - col_start);
            col_start = col_end + 1;
        }
        
        if (cols.size() >= 8) {
            // CHROM
            auto it = header.chrom_map.find(cols[0]);
            chrom_ids.push_back(it != header.chrom_map.end() ? it->second : 0); // Default to 0 if unknown?
            
            // POS
            try { pos_list.push_back(std::stoi(cols[1])); } catch(...) { pos_list.push_back(0); }
            
            // ID
            id_data.insert(id_data.end(), cols[2].begin(), cols[2].end());
            id_offsets.push_back(id_data.size());
            
            // REF
            ref_data.insert(ref_data.end(), cols[3].begin(), cols[3].end());
            ref_offsets.push_back(ref_data.size());
            
            // ALT
            alt_data.insert(alt_data.end(), cols[4].begin(), cols[4].end());
            alt_offsets.push_back(alt_data.size());
            
            // QUAL
            qual_data.insert(qual_data.end(), cols[5].begin(), cols[5].end());
            qual_offsets.push_back(qual_data.size());
            
            // FILTER
            auto fit = header.filter_map.find(cols[6]);
            filter_ids.push_back(fit != header.filter_map.end() ? fit->second : 0);
            
            // INFO
            info_data.insert(info_data.end(), cols[7].begin(), cols[7].end());
            info_offsets.push_back(info_data.size());
            
            // GENOTYPE (Rest of line)
            // Reconstruct tab separated string for remaining columns
            std::string gt;
            for (size_t i = 8; i < cols.size(); ++i) {
                if (i > 8) gt += "\t";
                gt += cols[i];
            }
            genotype_data.insert(genotype_data.end(), gt.begin(), gt.end());
            genotype_offsets.push_back(genotype_data.size());
        }
        
        cursor = line_end + 1;
    }
    
    // Write Binary
    std::ofstream out(output_path, std::ios::binary);
    out.write("VCF3", 4);
    
    uint32_t hdr_len = header.raw_header.size();
    write_u32(out, hdr_len);
    out.write(header.raw_header.data(), hdr_len);
    
    uint32_t num_records = chrom_ids.size();
    write_u32(out, num_records);
    
    out.write((const char*)chrom_ids.data(), num_records * 4);
    out.write((const char*)pos_list.data(), num_records * 4);
    out.write((const char*)filter_ids.data(), num_records * 4);
    
    out.write((const char*)id_offsets.data(), id_offsets.size() * 4);
    out.write((const char*)ref_offsets.data(), ref_offsets.size() * 4);
    out.write((const char*)alt_offsets.data(), alt_offsets.size() * 4);
    out.write((const char*)qual_offsets.data(), qual_offsets.size() * 4);
    out.write((const char*)info_offsets.data(), info_offsets.size() * 4);
    out.write((const char*)genotype_offsets.data(), genotype_offsets.size() * 4);
    
    write_u32(out, id_data.size());
    write_u32(out, ref_data.size());
    write_u32(out, alt_data.size());
    write_u32(out, qual_data.size());
    write_u32(out, info_data.size());
    write_u32(out, genotype_data.size());
    
    uint32_t id_pad = (4 - (id_data.size() % 4)) % 4;
    uint32_t ref_pad = (4 - (ref_data.size() % 4)) % 4;
    uint32_t alt_pad = (4 - (alt_data.size() % 4)) % 4;
    uint32_t qual_pad = (4 - (qual_data.size() % 4)) % 4;
    uint32_t info_pad = (4 - (info_data.size() % 4)) % 4;
    uint32_t genotype_pad = (4 - (genotype_data.size() % 4)) % 4;
    
    write_u32(out, id_pad);
    write_u32(out, ref_pad);
    write_u32(out, alt_pad);
    write_u32(out, qual_pad);
    write_u32(out, info_pad);
    write_u32(out, genotype_pad);
    
    out.write(id_data.data(), id_data.size()); pad_stream(out, id_data.size());
    out.write(ref_data.data(), ref_data.size()); pad_stream(out, ref_data.size());
    out.write(alt_data.data(), alt_data.size()); pad_stream(out, alt_data.size());
    out.write(qual_data.data(), qual_data.size()); pad_stream(out, qual_data.size());
    out.write(info_data.data(), info_data.size()); pad_stream(out, info_data.size());
    out.write(genotype_data.data(), genotype_data.size()); pad_stream(out, genotype_data.size());
    
    out.close();
}

// ============================================================================
// Main Logic
// ============================================================================

enum FileType { UNKNOWN, FASTQ, FASTA, VCF, FASTQ_V4, FASTA_PACKED };

FileType detect_type(const char* data, size_t size) {
    if (size < 100) return UNKNOWN;
    std::string head(data, std::min(size, (size_t)1024));
    
    if (head.rfind("##fileformat=VCF", 0) == 0) return VCF;
    if (head[0] == '>') return FASTA;
    if (head[0] == '@') {
        // Check 3rd line
        size_t l1 = head.find('\n');
        if (l1 != std::string::npos) {
            size_t l2 = head.find('\n', l1 + 1);
            if (l2 != std::string::npos) {
                if (l2 + 1 < head.size() && head[l2 + 1] == '+') return FASTQ;
            }
        }
    }
    return UNKNOWN;
}

int main(int argc, char* argv[]) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <input_file> <output_dir> <num_threads> [type]" << std::endl;
        return 1;
    }

    std::string input_path = argv[1];
    std::string output_dir = argv[2];
    int num_threads = std::stoi(argv[3]);
    std::string type_str = (argc > 4) ? argv[4] : "";

    try {
        MappedFile file(input_path);
        
        FileType type = UNKNOWN;
        if (type_str == "fastq") type = FASTQ;
        else if (type_str == "fastq_v4") type = FASTQ_V4;
        else if (type_str == "fasta") type = FASTA;
        else if (type_str == "fasta_packed") type = FASTA_PACKED;
        else if (type_str == "vcf") type = VCF;
        else if (type_str == "") type = detect_type(file.data, file.size);
        
        if (type == UNKNOWN) {
            std::cerr << "Unknown file type. Please specify fastq, fastq_v4, fasta, or vcf." << std::endl;
            return 1;
        }

        fs::create_directories(output_dir);

        // Calculate split points dynamically with per-type caps.
        size_t max_chunk_bytes = 450ull * 1024ull * 1024ull; // default 450 MiB
        if (type == VCF) {
            max_chunk_bytes = 300ull * 1024ull * 1024ull; // tighter cap for VCF to avoid OpenZL allocator failures
        }
        const size_t requested_chunks = static_cast<size_t>(std::max(1, num_threads));

        size_t size_based_chunks = (file.size + max_chunk_bytes - 1) / max_chunk_bytes;
        if (size_based_chunks == 0) size_based_chunks = 1;

        size_t chunk_count = std::max(requested_chunks, size_based_chunks);
        size_t target_chunk_size = (file.size + chunk_count - 1) / chunk_count;

        // For very small files, boundary snapping may collapse trailing empty chunks.

        std::vector<const char*> split_points;
        split_points.reserve(chunk_count + 1);
        split_points.push_back(file.data);

        for (size_t i = 1; i < chunk_count; ++i) {
            const char* target = file.data + i * target_chunk_size;
            const char* end = file.data + file.size;
            if (target >= end) {
                split_points.push_back(end);
                continue;
            }

            // Adjust to record boundary
            const char* cursor = target;
            while (cursor < end) {
                if (type == FASTQ || type == FASTQ_V4) {
                    if (*cursor == '@' && *(cursor-1) == '\n') {
                        const char* l1 = (const char*)memchr(cursor, '\n', end - cursor);
                        if (l1) {
                            const char* l2 = (const char*)memchr(l1+1, '\n', end - (l1+1));
                            if (l2 && l2+1 < end && *(l2+1) == '+') {
                                break;
                            }
                        }
                    }
                } else if (type == FASTA || type == FASTA_PACKED) {
                    if (*cursor == '>' && *(cursor-1) == '\n') break;
                } else if (type == VCF) {
                    if (*cursor != '#' && *(cursor-1) == '\n') break; // Start of record
                }
                cursor++;
            }
            split_points.push_back(cursor);
        }
        split_points.push_back(file.data + file.size);

        // VCF Header Pre-pass
        VcfHeaderInfo vcf_header;
        if (type == VCF) {
            vcf_header = parse_vcf_header(file.data, split_points[1]); // Header is in first chunk
        }

        // Launch worker threads (limit concurrency to requested num_threads while supporting extra chunks)
        const size_t total_chunks = split_points.size() - 1;
        const size_t worker_count = std::max<size_t>(1, std::min<size_t>(total_chunks, requested_chunks));
        std::atomic<size_t> next_chunk{0};

        auto process_chunk = [&](size_t idx) {
            const char* start = split_points[idx];
            const char* end = split_points[idx + 1];
            if (start >= end) return;

            char filename[256];
            if (type == FASTQ) snprintf(filename, sizeof(filename), "chunk_%05zu.fastq.bin", idx);
            else if (type == FASTQ_V4) snprintf(filename, sizeof(filename), "chunk_%05zu.fastq_v4.bin", idx);
            else if (type == FASTA) snprintf(filename, sizeof(filename), "chunk_%05zu.fasta.bin", idx);
            else if (type == FASTA_PACKED) snprintf(filename, sizeof(filename), "chunk_%05zu.fasta_packed.bin", idx);
            else if (type == VCF) snprintf(filename, sizeof(filename), "chunk_%05zu.vcf.bin", idx);

            std::string out_path = output_dir + "/" + filename;

            if (type == FASTQ) process_fastq_chunk(start, end, out_path);
            else if (type == FASTQ_V4) process_fastq_v4_chunk(start, end, out_path);
            else if (type == FASTA) process_fasta_chunk(start, end, out_path);
            else if (type == FASTA_PACKED) process_fasta_packed_chunk(start, end, out_path);
            else if (type == VCF) process_vcf_chunk(start, end, out_path, vcf_header);
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
        
        std::cout << "Processing complete. Output in " << output_dir << std::endl;

    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }

    return 0;
}
