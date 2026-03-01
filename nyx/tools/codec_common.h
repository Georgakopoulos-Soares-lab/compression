/*
 * codec_common.h — Shared utilities for FASTA and FASTQ lossless codecs.
 *
 * Provides: type aliases, constants, memory-mapped I/O, binary helpers,
 *           varint encoding, bit-packing, 2-bit base encoding, newline
 *           detection, and wrapping encode/decode helpers.
 *
 * Header-only; all functions are static or inline to avoid link issues
 * when included from multiple translation units.
 */

#ifndef CODEC_COMMON_H
#define CODEC_COMMON_H

#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include <fcntl.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

// ============================================================================
// Type aliases
// ============================================================================

using u8  = uint8_t;
using u16 = uint16_t;
using u32 = uint32_t;
using u64 = uint64_t;

// ============================================================================
// Constants
// ============================================================================

static constexpr u8 NL_LF   = 0;
static constexpr u8 NL_CRLF = 1;

static constexpr u8 CASE_NONE   = 0;
static constexpr u8 CASE_MASK   = 1;
static constexpr u8 CASE_SPARSE = 2;

static constexpr u8 WRAP_COMPACT  = 0x01;
static constexpr u8 WRAP_EXPLICIT = 0x02;

// ============================================================================
// Memory-mapped file (read-only)
// ============================================================================

struct MappedFile {
    const char* data = nullptr;
    size_t      size = 0;
    int         fd   = -1;

    bool open(const char* path) {
        fd = ::open(path, O_RDONLY);
        if (fd < 0) { perror(path); return false; }

        struct stat st;
        if (fstat(fd, &st) < 0) { perror("fstat"); ::close(fd); fd = -1; return false; }
        size = static_cast<size_t>(st.st_size);

        if (size == 0) {
            data = nullptr;
            return true;
        }

        data = static_cast<const char*>(
            mmap(nullptr, size, PROT_READ, MAP_PRIVATE, fd, 0));
        if (data == MAP_FAILED) {
            perror("mmap"); ::close(fd); fd = -1; data = nullptr; return false;
        }
        madvise(const_cast<char*>(data), size, MADV_SEQUENTIAL);
        return true;
    }

    ~MappedFile() {
        if (data && size > 0) munmap(const_cast<char*>(data), size);
        if (fd >= 0) ::close(fd);
    }

    MappedFile() = default;
    MappedFile(const MappedFile&) = delete;
    MappedFile& operator=(const MappedFile&) = delete;
};

// ============================================================================
// Binary I/O helpers
// ============================================================================

static inline void write_u8(FILE* f, u8 v)   { fwrite(&v, 1, 1, f); }
static inline void write_u32(FILE* f, u32 v) { fwrite(&v, 4, 1, f); }

static inline void write_bytes(FILE* f, const void* data, size_t len) {
    if (len > 0) fwrite(data, 1, len, f);
}

static inline u8  read_u8(FILE* f)  { u8  v = 0; if (fread(&v, 1, 1, f) != 1) {} return v; }
static inline u32 read_u32(FILE* f) { u32 v = 0; if (fread(&v, 4, 1, f) != 4) {} return v; }

static inline void read_bytes(FILE* f, void* buf, size_t len) {
    if (len > 0) { if (fread(buf, 1, len, f) != len) {} }
}

static inline std::vector<u8> read_file_bytes(const std::string& path) {
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) { fprintf(stderr, "Cannot open %s\n", path.c_str()); exit(1); }
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    std::vector<u8> buf(sz);
    if (sz > 0) { if (fread(buf.data(), 1, sz, f) != (size_t)sz) {} }
    fclose(f);
    return buf;
}

// ============================================================================
// Varint encoding (unsigned LEB128)
// ============================================================================

static inline void encode_varint(std::vector<u8>& out, u64 value) {
    do {
        u8 byte = value & 0x7F;
        value >>= 7;
        if (value != 0) byte |= 0x80;
        out.push_back(byte);
    } while (value != 0);
}

static inline u64 decode_varint(const u8*& ptr, const u8* end) {
    u64 result = 0;
    int shift = 0;
    while (ptr < end) {
        u8 byte = *ptr++;
        result |= static_cast<u64>(byte & 0x7F) << shift;
        if ((byte & 0x80) == 0) return result;
        shift += 7;
    }
    fprintf(stderr, "Truncated varint\n");
    exit(1);
}

// ============================================================================
// Bit packing helpers (MSB-first: bit 7 of byte 0 = position 0)
// ============================================================================

static inline std::vector<u8> pack_bits(const std::vector<bool>& bits) {
    size_t n = bits.size();
    size_t nbytes = (n + 7) / 8;
    std::vector<u8> out(nbytes, 0);
    for (size_t i = 0; i < n; i++) {
        if (bits[i]) {
            out[i / 8] |= (0x80 >> (i % 8));
        }
    }
    return out;
}

static inline std::vector<bool> unpack_bits(const u8* data, size_t nbits) {
    std::vector<bool> out(nbits);
    for (size_t i = 0; i < nbits; i++) {
        out[i] = (data[i / 8] >> (7 - (i % 8))) & 1;
    }
    return out;
}

static inline std::vector<u8> pack_2bit(const std::vector<u8>& values) {
    size_t n = values.size();
    size_t nbytes = (n + 3) / 4;
    std::vector<u8> out(nbytes, 0);
    for (size_t i = 0; i < n; i++) {
        int shift = 6 - 2 * (i % 4);
        out[i / 4] |= (values[i] & 0x03) << shift;
    }
    return out;
}

static inline std::vector<u8> unpack_2bit(const u8* data, size_t nvalues) {
    std::vector<u8> out(nvalues);
    for (size_t i = 0; i < nvalues; i++) {
        int shift = 6 - 2 * (i % 4);
        out[i] = (data[i / 4] >> shift) & 0x03;
    }
    return out;
}

// ============================================================================
// 2-bit base encoding: A=0, C=1, G=2, T=3
// ============================================================================

static inline u8 base_to_2bit(char c) {
    switch (c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return 0; // should never be called for non-ACGT
    }
}

static inline char twobit_to_base(u8 v) {
    static const char table[] = "ACGT";
    return table[v & 0x03];
}

// ============================================================================
// Newline detection helpers
// ============================================================================

static inline u8 detect_newline_style(const char* data, size_t size) {
    for (size_t i = 0; i < size; i++) {
        if (data[i] == '\n') {
            return (i > 0 && data[i - 1] == '\r') ? NL_CRLF : NL_LF;
        }
    }
    return NL_LF;
}

static inline u8 detect_trailing_newline(const char* data, size_t size) {
    if (size == 0) return 0;
    return (data[size - 1] == '\n') ? 1 : 0;
}

static inline void skip_newline(const char* data, size_t size, size_t& pos) {
    if (pos < size && data[pos] == '\r') pos++;
    if (pos < size && data[pos] == '\n') pos++;
}

// ============================================================================
// Sequence encoding helpers (shared by FASTA and FASTQ)
// ============================================================================

struct SequenceEncoding {
    std::vector<u8> nmask_packed;
    std::vector<u8> acgtmask_packed;
    std::vector<u8> bases2_packed;
    std::vector<u8> exceptions_buf;
    std::vector<u8> case_buf;
    u8  case_mode;
    u32 nmask_bytes;
    u32 acgtmask_bytes;
    u32 bases2_bytes;
    u32 exceptions_bytes;
    u32 case_bytes;
};

static inline SequenceEncoding encode_sequence(const std::string& seq) {
    SequenceEncoding enc{};
    u32 L = static_cast<u32>(seq.size());

    // --- N-mask ---
    std::vector<bool> nmask(L);
    u32 count_n = 0;
    for (u32 i = 0; i < L; i++) {
        char c = seq[i];
        if (c == 'N' || c == 'n') {
            nmask[i] = true;
            count_n++;
        }
    }
    enc.nmask_packed = pack_bits(nmask);
    enc.nmask_bytes = static_cast<u32>(enc.nmask_packed.size());

    // --- Build S_nonN ---
    std::vector<char> s_nonN;
    s_nonN.reserve(L - count_n);
    for (u32 i = 0; i < L; i++) {
        if (!nmask[i]) s_nonN.push_back(seq[i]);
    }
    u32 Lp = static_cast<u32>(s_nonN.size());

    // --- ACGT-mask over S_nonN ---
    std::vector<bool> acgtmask(Lp);
    u32 count_acgt = 0;
    for (u32 j = 0; j < Lp; j++) {
        char upper = s_nonN[j] & ~0x20;
        if (upper == 'A' || upper == 'C' || upper == 'G' || upper == 'T') {
            acgtmask[j] = true;
            count_acgt++;
        }
    }
    enc.acgtmask_packed = pack_bits(acgtmask);
    enc.acgtmask_bytes = static_cast<u32>(enc.acgtmask_packed.size());

    // --- 2-bit packed bases ---
    std::vector<u8> base_values;
    base_values.reserve(count_acgt);
    for (u32 j = 0; j < Lp; j++) {
        if (acgtmask[j]) base_values.push_back(base_to_2bit(s_nonN[j]));
    }
    enc.bases2_packed = pack_2bit(base_values);
    enc.bases2_bytes = static_cast<u32>(enc.bases2_packed.size());

    // --- Exceptions ---
    u32 prev_pos = 0;
    for (u32 j = 0; j < Lp; j++) {
        if (!acgtmask[j]) {
            encode_varint(enc.exceptions_buf, j - prev_pos);
            char c = s_nonN[j];
            if (c >= 'a' && c <= 'z') c = c - 'a' + 'A';
            enc.exceptions_buf.push_back(static_cast<u8>(c));
            prev_pos = j;
        }
    }
    enc.exceptions_bytes = static_cast<u32>(enc.exceptions_buf.size());

    // --- Case preservation ---
    u32 count_lower = 0;
    for (u32 i = 0; i < L; i++) {
        if (seq[i] >= 'a' && seq[i] <= 'z') count_lower++;
    }

    if (count_lower == 0) {
        enc.case_mode = CASE_NONE;
    } else if (L > 0 && count_lower < L / 16) {
        enc.case_mode = CASE_SPARSE;
        encode_varint(enc.case_buf, count_lower);
        u32 prev = 0;
        for (u32 i = 0; i < L; i++) {
            if (seq[i] >= 'a' && seq[i] <= 'z') {
                encode_varint(enc.case_buf, i - prev);
                prev = i;
            }
        }
    } else {
        enc.case_mode = CASE_MASK;
        std::vector<bool> case_bits(L);
        for (u32 i = 0; i < L; i++) {
            if (seq[i] >= 'a' && seq[i] <= 'z') case_bits[i] = true;
        }
        enc.case_buf = pack_bits(case_bits);
    }
    enc.case_bytes = static_cast<u32>(enc.case_buf.size());

    return enc;
}

// ============================================================================
// Sequence decoding helper (shared by FASTA and FASTQ decoders)
// ============================================================================

static inline std::vector<char> decode_sequence(
    u32 L,
    const u8* nmask_data, u32 nmask_bytes,
    const u8* acgtmask_data, u32 acgtmask_bytes,
    const u8* bases2_data, u32 bases2_bytes,
    const u8* exceptions_data, u32 exceptions_bytes,
    const u8* case_data, u32 case_bytes, u8 case_mode)
{
    // N-mask: if nmask_bytes==0 with L>0, stream was omitted (no N's)
    std::vector<bool> nmask;
    u32 Lp;
    if (nmask_bytes > 0) {
        nmask = unpack_bits(nmask_data, L);
        Lp = 0;
        for (u32 i = 0; i < L; i++) { if (!nmask[i]) Lp++; }
    } else {
        nmask.assign(L, false);
        Lp = L;
    }

    // ACGT-mask: if acgtmask_bytes==0 with Lp>0, stream was omitted (all ACGT)
    std::vector<bool> acgtmask;
    u32 count_acgt;
    if (acgtmask_bytes > 0) {
        acgtmask = unpack_bits(acgtmask_data, Lp);
        count_acgt = 0;
        for (u32 j = 0; j < Lp; j++) { if (acgtmask[j]) count_acgt++; }
    } else {
        acgtmask.assign(Lp, true);
        count_acgt = Lp;
    }

    // 2-bit bases
    auto base_values = unpack_2bit(bases2_data, count_acgt);

    // Exceptions
    const u8* ex_ptr = exceptions_data;
    const u8* ex_end = ex_ptr + exceptions_bytes;
    struct Exc { u32 pos; char sym; };
    std::vector<Exc> exceptions;
    {
        u32 pos = 0;
        while (ex_ptr < ex_end) {
            u64 delta = decode_varint(ex_ptr, ex_end);
            pos += static_cast<u32>(delta);
            if (ex_ptr >= ex_end) break;
            char sym = static_cast<char>(*ex_ptr++);
            exceptions.push_back({pos, sym});
        }
    }

    // Rebuild S_nonN
    std::vector<char> s_nonN(Lp);
    {
        u32 base_idx = 0, exc_idx = 0;
        for (u32 j = 0; j < Lp; j++) {
            if (acgtmask[j]) {
                s_nonN[j] = twobit_to_base(base_values[base_idx++]);
            } else {
                if (exc_idx < exceptions.size() && exceptions[exc_idx].pos == j) {
                    s_nonN[j] = exceptions[exc_idx].sym;
                    exc_idx++;
                } else {
                    s_nonN[j] = '?';
                }
            }
        }
    }

    // Rebuild full sequence
    std::vector<char> raw_seq(L);
    {
        u32 nonN_idx = 0;
        for (u32 i = 0; i < L; i++) {
            if (nmask[i]) {
                raw_seq[i] = 'N';
            } else {
                raw_seq[i] = s_nonN[nonN_idx++];
            }
        }
    }

    // Apply case
    if (case_mode == CASE_MASK) {
        auto case_bits = unpack_bits(case_data, L);
        for (u32 i = 0; i < L; i++) {
            if (case_bits[i] && raw_seq[i] >= 'A' && raw_seq[i] <= 'Z') {
                raw_seq[i] = raw_seq[i] + ('a' - 'A');
            }
        }
    } else if (case_mode == CASE_SPARSE) {
        const u8* cs_ptr = case_data;
        const u8* cs_end = cs_ptr + case_bytes;
        u64 count = decode_varint(cs_ptr, cs_end);
        u32 pos = 0;
        for (u64 k = 0; k < count && cs_ptr < cs_end; k++) {
            u64 delta = decode_varint(cs_ptr, cs_end);
            pos += static_cast<u32>(delta);
            if (pos < L && raw_seq[pos] >= 'A' && raw_seq[pos] <= 'Z') {
                raw_seq[pos] = raw_seq[pos] + ('a' - 'A');
            }
        }
    }

    return raw_seq;
}

// ============================================================================
// Wrapping encode/decode helpers
// ============================================================================

static inline std::vector<u8> encode_wrapping(const std::vector<u32>& ll, u32 L) {
    std::vector<u8> buf;
    u32 num_lines = static_cast<u32>(ll.size());

    if (num_lines == 0) {
        buf.push_back(WRAP_EXPLICIT);
        u32 zero = 0;
        buf.insert(buf.end(), (u8*)&zero, (u8*)&zero + 4);
    } else {
        bool uniform = true;
        u32 width = ll[0];
        for (u32 k = 0; k + 1 < num_lines; k++) {
            if (ll[k] != width) { uniform = false; break; }
        }
        u32 last_len = ll[num_lines - 1];
        if (num_lines == 1) { uniform = true; width = last_len; }

        if (uniform && (num_lines == 1 || last_len <= width)) {
            buf.push_back(WRAP_COMPACT);
            buf.insert(buf.end(), (u8*)&width, (u8*)&width + 4);
            buf.insert(buf.end(), (u8*)&last_len, (u8*)&last_len + 4);
        } else {
            buf.push_back(WRAP_EXPLICIT);
            buf.insert(buf.end(), (u8*)&num_lines, (u8*)&num_lines + 4);
            for (u32 k = 0; k < num_lines; k++) {
                u32 val = ll[k];
                buf.insert(buf.end(), (u8*)&val, (u8*)&val + 4);
            }
        }
    }
    return buf;
}

static inline std::vector<u32> decode_wrapping(
    const u8* wr_data, u32 wrap_bytes, u32 L)
{
    const u8* ptr = wr_data;
    u8 wrap_mode = *ptr++;
    std::vector<u32> line_lengths;

    if (wrap_mode == WRAP_COMPACT) {
        u32 width, last_len;
        memcpy(&width, ptr, 4); ptr += 4;
        memcpy(&last_len, ptr, 4); ptr += 4;
        if (L > 0) {
            if (width == 0) {
                line_lengths.push_back(last_len);
            } else {
                u32 full_lines = (last_len == width)
                    ? (L / width)
                    : (L > width ? (L - last_len) / width : 0);
                for (u32 k = 0; k < full_lines; k++) line_lengths.push_back(width);
                u32 remaining = L - full_lines * width;
                if (remaining > 0) line_lengths.push_back(remaining);
            }
        }
    } else {
        // WRAP_EXPLICIT
        u32 num_lines;
        memcpy(&num_lines, ptr, 4); ptr += 4;
        line_lengths.resize(num_lines);
        for (u32 k = 0; k < num_lines; k++) {
            memcpy(&line_lengths[k], ptr, 4); ptr += 4;
        }
    }
    return line_lengths;
}

// ============================================================================
// Directory creation helper
// ============================================================================

static inline void mkdir_recursive(const std::string& dir) {
    std::string path;
    for (size_t i = 0; i < dir.size(); i++) {
        path += dir[i];
        if (dir[i] == '/' || i == dir.size() - 1) {
            mkdir(path.c_str(), 0755);
        }
    }
}

#endif // CODEC_COMMON_H
