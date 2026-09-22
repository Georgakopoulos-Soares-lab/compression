// harc_common.h
// Shared helpers for harc_preprocessor.cpp (encoder) and harc_decode.cpp
// (decoder / validator).
//
// Base packing convention matches the existing FAV4 preprocessor exactly
// (dev/README.md "4-bit base packing" / tools/biocompress_preprocessor.cpp
// pack_base()): A=0 C=1 G=2 T=3 N=4 (anything else -> N), two bases/byte,
// first base in the high nibble, second in the low nibble, odd trailing
// base padded into the high nibble with the low nibble = 0.
#pragma once
#include <cstdint>
#include <string>
#include <vector>
#include <fstream>
#include <stdexcept>
#include <cstring>
#include <sys/mman.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

// ---- mmap'd input file (same shape as biocompress_preprocessor.cpp) ----
struct MappedFile {
    const char* data;
    size_t size;
    int fd;

    explicit MappedFile(const std::string& path) {
        fd = open(path.c_str(), O_RDONLY);
        if (fd == -1) throw std::runtime_error("Could not open file: " + path);
        struct stat sb;
        if (fstat(fd, &sb) == -1) { close(fd); throw std::runtime_error("Could not stat file: " + path); }
        size = sb.st_size;
        if (size == 0) { data = nullptr; return; }
        data = (const char*)mmap(NULL, size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (data == MAP_FAILED) { close(fd); throw std::runtime_error("mmap failed: " + path); }
        madvise((void*)data, size, MADV_SEQUENTIAL);
    }
    ~MappedFile() {
        if (data && data != MAP_FAILED) munmap((void*)data, size);
        if (fd != -1) close(fd);
    }
    MappedFile(const MappedFile&) = delete;
};

// ---- 4-bit base codes (matches pack_base() in biocompress_preprocessor.cpp) ----
inline uint8_t base_to_code(char c) {
    switch (c) {
        case 'A': case 'a': return 0;
        case 'C': case 'c': return 1;
        case 'G': case 'g': return 2;
        case 'T': case 't': return 3;
        default: return 4; // N or anything unrecognized (matches upstream behavior)
    }
}
inline char code_to_base(uint8_t v) {
    switch (v) {
        case 0: return 'A';
        case 1: return 'C';
        case 2: return 'G';
        case 3: return 'T';
        default: return 'N';
    }
}

// Packs seq[begin .. begin+len) into 4-bit codes appended to out.
inline void pack_bases(const std::string& seq, size_t begin, size_t len, std::vector<uint8_t>& out) {
    for (size_t i = 0; i < len; i += 2) {
        uint8_t hi = base_to_code(seq[begin + i]);
        uint8_t lo = (i + 1 < len) ? base_to_code(seq[begin + i + 1]) : 0;
        out.push_back((uint8_t)((hi << 4) | lo));
    }
}
inline std::string unpack_bases(const uint8_t* packed, size_t len) {
    std::string out;
    out.resize(len);
    for (size_t i = 0; i < len; i++) {
        uint8_t byte = packed[i / 2];
        uint8_t code = (i % 2 == 0) ? (byte >> 4) : (byte & 0x0F);
        out[i] = code_to_base(code);
    }
    return out;
}
// Canonicalize a raw base string through the same code<->base mapping the
// packed format uses, so validation compares apples-to-apples (a stray IUPAC
// ambiguity code like 'R' becomes 'N' on both sides, same as FAV4 already does).
inline std::string canonicalize(const std::string& raw) {
    std::string out(raw.size(), 'N');
    for (size_t i = 0; i < raw.size(); i++) out[i] = code_to_base(base_to_code(raw[i]));
    return out;
}

// ---- little-endian binary writer/reader ----
inline void write_u32(std::vector<uint8_t>& buf, uint32_t v) {
    buf.push_back((uint8_t)(v & 0xFF)); buf.push_back((uint8_t)((v >> 8) & 0xFF));
    buf.push_back((uint8_t)((v >> 16) & 0xFF)); buf.push_back((uint8_t)((v >> 24) & 0xFF));
}
inline void write_u16(std::vector<uint8_t>& buf, uint16_t v) {
    buf.push_back((uint8_t)(v & 0xFF)); buf.push_back((uint8_t)((v >> 8) & 0xFF));
}
inline void write_u8(std::vector<uint8_t>& buf, uint8_t v) { buf.push_back(v); }
inline void write_bytes(std::vector<uint8_t>& buf, const uint8_t* p, size_t n) { buf.insert(buf.end(), p, p + n); }
inline void write_bytes(std::vector<uint8_t>& buf, const void* p, size_t n) { write_bytes(buf, (const uint8_t*)p, n); }
inline void write_magic(std::vector<uint8_t>& buf, const char m[4]) {
    buf.push_back((uint8_t)m[0]); buf.push_back((uint8_t)m[1]); buf.push_back((uint8_t)m[2]); buf.push_back((uint8_t)m[3]);
}
inline void pad_to(std::vector<uint8_t>& buf, uint32_t pad_len) { for (uint32_t i = 0; i < pad_len; i++) buf.push_back(0); }

struct ByteReader {
    const uint8_t* p; size_t pos = 0, n;
    ByteReader(const uint8_t* p_, size_t n_) : p(p_), n(n_) {}
    uint32_t u32() {
        uint32_t v = (uint32_t)p[pos] | ((uint32_t)p[pos+1]<<8) | ((uint32_t)p[pos+2]<<16) | ((uint32_t)p[pos+3]<<24);
        pos += 4; return v;
    }
    uint16_t u16() { uint16_t v = (uint16_t)(p[pos] | (p[pos+1]<<8)); pos += 2; return v; }
    uint8_t  u8()  { return p[pos++]; }
    const uint8_t* bytes(size_t k) { const uint8_t* r = p + pos; pos += k; return r; }
    void skip(size_t k) { pos += k; }
};

inline std::vector<uint8_t> read_whole_file(const std::string& path) {
    std::ifstream in(path, std::ios::binary | std::ios::ate);
    if (!in) throw std::runtime_error("cannot open " + path);
    std::streamsize size = in.tellg();
    in.seekg(0, std::ios::beg);
    std::vector<uint8_t> buf((size_t)size);
    if (size > 0 && !in.read(reinterpret_cast<char*>(buf.data()), size)) throw std::runtime_error("read failed: " + path);
    return buf;
}
inline void write_whole_file(const std::string& path, const std::vector<uint8_t>& buf) {
    std::ofstream out(path, std::ios::binary);
    if (!out) throw std::runtime_error("cannot open for write " + path);
    out.write(reinterpret_cast<const char*>(buf.data()), (std::streamsize)buf.size());
}

// ---- range-based FASTA parsing, shared by the encoder and the validator ----
struct FastaRecOwned { std::string header; std::string seq; };

// Parses FASTA records fully contained in [start, end). Mirrors the line
// scanning in biocompress_preprocessor.cpp's process_fasta_packed_chunk.
inline std::vector<FastaRecOwned> parse_fasta_range(const char* start, const char* end) {
    std::vector<FastaRecOwned> recs;
    const char* cursor = start;
    while (cursor < end) {
        if (*cursor == '>') {
            const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
            if (!line_end) line_end = end;
            recs.push_back(FastaRecOwned{});
            recs.back().header.assign(cursor + 1, line_end - (cursor + 1));
            cursor = line_end + 1;
        } else {
            const char* line_end = (const char*)memchr(cursor, '\n', end - cursor);
            if (!line_end) line_end = end;
            if (!recs.empty()) {
                std::string& seq = recs.back().seq;
                for (const char* p = cursor; p < line_end; ++p) {
                    if (*p == '\n' || *p == '\r') continue;
                    seq.push_back(*p);
                }
            }
            cursor = line_end + 1;
        }
    }
    return recs;
}
